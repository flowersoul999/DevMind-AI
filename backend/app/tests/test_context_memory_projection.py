"""
tests/test_context_memory_projection.py —— 测试「上下文记忆投影」（chat_memory.py）
============================================================================

chat_memory.py 是记忆系统的主力：负责把聊天消息存库、按时间线取回、压缩旧内容、把会话
「封存」成可复查的 transcript。本测试用「内存 SQLite」（不落盘）造数据，验证：

  - 时间线能跨压缩边界、且不返回重复的兼容性副本；
  - 时间戳相同游标也稳定（分页不丢不乱）；
  - 精确召回能读回「压缩边界之前」的原始文字，并记录审计事件；
  - 正式压缩时复用原行、重建工作态（working files）；
  - 封存（seal）时即使 LLM 抽取失败，也保留原始 transcript 并打警告标记。

全程用 SQLAlchemy 内存库，干净、可重复。."""

import uuid  # 生成唯一 id
from datetime import datetime, timedelta, timezone  # 构造带时区的时间戳

import pytest  # 测试框架
from sqlalchemy import create_engine  # 建数据库引擎
from sqlalchemy.orm import Session  # 数据库会话

from app.db.models import ChatMessage, ChatSession, Conversation, RecallEvent, Repository  # 相关表
from app.db.session import Base  # 所有表的元数据（用来建表）
from app.services.chat_memory import (  # 被测的记忆组件
    ContextAssembler,  # 上下文装配器
    MemoryTools,  # 记忆工具（供 Agent 调用）
    MessageStore,  # 消息存储/查询
    ProgressiveContextManager,  # 渐进式上下文管理器
    SessionSealer,  # 会话封存器
)


def _db() -> Session:
    # 建一个「内存版」SQLite（:memory: 不写磁盘），并建好所有表；返回会话
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)  # 按模型定义建表
    return Session(engine)


def _scope(db: Session) -> tuple[Repository, Conversation]:
    # 造一个仓库 + 一个会话作为测试上下文
    repo = Repository(owner="acme", name="context", full_name="acme/context")
    db.add(repo)
    db.flush()  # flush 让 repo.id 生成
    conversation = Conversation(repo_id=repo.id, title="Long context")
    db.add(conversation)
    db.flush()
    return repo, conversation


def _message(
    db: Session,
    repo: Repository,
    conversation: Conversation,
    *,
    role: str,
    content: str,
    at: datetime,
    route: str | None = None,
    tool_calls: list[dict] | None = None,
    meta: dict | None = None,
) -> ChatMessage:
    # 造一条聊天消息并入库
    row = ChatMessage(
        repo_id=repo.id,
        conversation_id=conversation.id,
        role=role,
        content=content,
        route=route,
        tool_calls=tool_calls or [],
        meta=meta or {},
        created_at=at,  # 指定创建时间（测试排序/边界用）
    )
    db.add(row)
    db.flush()
    return row


def test_durable_timeline_crosses_boundary_and_hides_compatibility_copies() -> None:
    # 测试：时间线跨过「压缩边界」，且边界摘要本身不出现在返回里
    db = _db()
    repo, conversation = _scope(db)
    start = datetime.now(timezone.utc)
    first = _message(db, repo, conversation, role="user", content="first exact request", at=start)
    answer = _message(db, repo, conversation, role="assistant", content="first exact answer", at=start + timedelta(seconds=1))
    boundary = _message(  # 一条「压缩边界」系统消息
        db, repo, conversation, role="system", content="summary",
        route="context_compaction", meta={"subtype": "compact_boundary"}, at=start + timedelta(seconds=2),
    )
    _message(  # 边界之后、复制了 answer 内容的一条「保留副本」
        db, repo, conversation, role="assistant", content=answer.content,
        meta={"subtype": "preserved_after_compact", "preserved_from": str(answer.id)}, at=start + timedelta(seconds=3),
    )
    latest = _message(db, repo, conversation, role="user", content="latest request", at=start + timedelta(seconds=4))

    timeline = MessageStore(db).timeline(repo.id, conversation.id, limit=100)  # 取时间线

    # 时间线应含 first、answer、latest，但不含那条边界摘要
    assert [row.id for row in timeline] == [first.id, answer.id, latest.id]
    assert boundary.id not in {row.id for row in timeline}


def test_durable_timeline_cursor_is_stable_when_timestamps_are_equal() -> None:
    # 测试：多条消息时间戳相同，分页游标依然稳定（按 id 排序，不丢不重）
    db = _db()
    repo, conversation = _scope(db)
    at = datetime.now(timezone.utc)
    rows = [
        _message(db, repo, conversation, role="user", content=f"same-time-{index}", at=at)
        for index in range(3)  # 3 条同一时刻的消息
    ]
    expected = sorted(rows, key=lambda row: row.id)  # 期望按 id 排序

    newest_page = MessageStore(db).timeline(repo.id, conversation.id, limit=2)  # 取最新 2 条
    older_page = MessageStore(db).timeline(  # 以 newest_page 第一条为界，取更早的 2 条
        repo.id, conversation.id, limit=2, before_message_id=newest_page[0].id,
    )

    # 把两页拼起来，id 顺序应和期望一致
    assert [row.id for row in [*older_page, *newest_page]] == [row.id for row in expected]


@pytest.mark.asyncio
async def test_exact_thread_recall_reads_pre_boundary_text_and_is_audited() -> None:
    # 测试：精确召回能读回「边界之前」的原始文字，并且这次调用被审计记录
    db = _db()
    repo, conversation = _scope(db)
    start = datetime.now(timezone.utc)
    old = _message(db, repo, conversation, role="user", content="UNIQUE-PRE-BOUNDARY original wording", at=start)
    _message(  # 压缩边界
        db, repo, conversation, role="system", content="compressed summary",
        route="context_compaction", meta={"subtype": "compact_boundary", "preserved_message_ids": []}, at=start + timedelta(seconds=1),
    )
    _message(db, repo, conversation, role="assistant", content="new answer", at=start + timedelta(seconds=2))

    tools = MemoryTools(db, repo, conversation)
    result = await tools.get_thread_context({"keyword": "UNIQUE-PRE-BOUNDARY", "limit": 10}, {})  # 按关键词召回
    page = await tools.get_thread_context(  # 按 message_id 取某条、带偏移和字符上限
        {"message_id": str(old.id), "offset": 7, "max_chars": 12}, {},
    )

    assert "UNIQUE-PRE-BOUNDARY original wording" in result["answer"]  # 原文被召回
    assert str(old.id) in result["answer"]  # 结果里带消息 id
    assert old.content[7:19] in page["answer"]  # 按 offset/max_chars 切片后的内容正确
    events = db.query(RecallEvent).filter(RecallEvent.tool_name == "devflow_get_thread_context").all()
    assert len(events) == 2  # 两次调用各记一条审计
    assert all(event.mode == "exact_transcript" for event in events)  # 模式是精确转录


@pytest.mark.asyncio
async def test_session_event_tool_returns_transcript_and_records_recall() -> None:
    # 测试：读取「已封存会话」的 transcript，并记录召回审计
    db = _db()
    repo, conversation = _scope(db)
    session = ChatSession(  # 一条已封存的会话
        repo_id=repo.id, conversation_id=conversation.id, route="chat", digest="short digest",
        transcript_json={"messages": [{"role": "user", "content": "exact sealed text"}]},
        status="sealed", sealed_at=datetime.now(timezone.utc),
    )
    db.add(session)
    db.flush()

    result = await MemoryTools(db, repo, conversation).read_session_events(
        {"session_id": str(session.id), "include_transcript": True}, {},
    )
    continuation = await MemoryTools(db, repo, conversation).read_session_events(
        {"session_id": str(session.id), "include_transcript": True, "offset": 5, "max_chars": 400}, {},
    )

    assert "exact sealed text" in result["answer"]  # transcript 内容被读出
    assert continuation["results"][0]["metadata"]["offset"] == 5  # 分页 offset 生效
    events = db.query(RecallEvent).filter(RecallEvent.tool_name == "devflow_read_session_events").all()
    assert len(events) == 2
    assert all(event.mode == "sealed_transcript" for event in events)  # 模式是封存转录
    assert all(event.result_count == 1 for event in events)


@pytest.mark.asyncio
async def test_formal_compaction_reuses_original_rows_and_rehydrates_working_state() -> None:
    # 测试：正式压缩复用原消息行，并重建「工作文件」列表（供后续工具调用恢复上下文）
    class FakeLLM:
        async def chat_text(self, system_prompt: str, user_prompt: str) -> str:
            return "User intent, decisions, errors, pending task and next step are preserved."

    db = _db()
    repo, conversation = _scope(db)
    start = datetime.now(timezone.utc)
    rows: list[ChatMessage] = []
    for index in range(6):
        rows.append(
            _message(
                db, repo, conversation,
                role="user" if index % 2 == 0 else "assistant",  # 奇偶交替 user/assistant
                content=f"message {index} " + ("details " * 80),  # 每条都拉得很长
                route="workspace.read_file" if index == 3 else None,  # 第 3 条是工具调用
                tool_calls=[
                    {"tool_name": "workspace.read_file", "arguments": {"path": "backend/app/auth.py"}, "status": "success"}
                ]
                if index == 3
                else [],
                at=start + timedelta(seconds=index),
            )
        )

    manager = ProgressiveContextManager(db, llm=FakeLLM())  # type: ignore[arg-type]
    result = await manager._compact(  # 触发压缩
        repo, conversation, "continue", {"stage": "auto_compact"}, "auto_compact",
    )

    assert result["compacted"] is True
    assert result["rehydration"]["working_files"] == ["backend/app/auth.py"]  # 工作文件被识别
    # 压缩后不应再出现 preserved_after_compact 的副本行（应该是复用而非复制）
    assert not any(
        (row.meta or {}).get("subtype") == "preserved_after_compact"
        for row in db.query(ChatMessage).all()
    )
    boundary = db.get(ChatMessage, uuid.UUID(result["boundary_id"]))  # 取压缩边界那行
    assert boundary is not None
    preserved_ids = [uuid.UUID(value) for value in boundary.meta["preserved_message_ids"]]
    active = MessageStore(db).active_for_context(repo.id, conversation.id, limit=100)  # 活跃（将进入上下文）的消息
    assert [row.id for row in active if row.role != "system"] == preserved_ids  # 活跃的非系统消息 == 保留的
    assert all(row.id in {item.id for item in rows} for row in active if row.role != "system")  # 都在原行里


@pytest.mark.asyncio
async def test_session_sealer_keeps_raw_transcript_when_extraction_fails() -> None:
    # 测试：封存时若 LLM 抽取失败，仍保留原始 transcript 并标记为「带警告封存」
    class FailingLLM:
        async def chat_json(self, system_prompt: str, payload: dict) -> dict:
            raise TimeoutError("memory model timeout")  # 故意让抽取超时

    db = _db()
    repo, conversation = _scope(db)
    now = datetime.now(timezone.utc)
    user = _message(db, repo, conversation, role="user", content="remember exact input", at=now)
    assistant = _message(db, repo, conversation, role="assistant", content="completed answer", at=now + timedelta(seconds=1))

    sealed = await SessionSealer(db, llm=FailingLLM()).seal(  # type: ignore[arg-type]
        repo=repo, conversation=conversation, user_message=user, assistant_message=assistant, route="chat", tool_calls=[],
    )

    assert sealed.status == "sealed_with_warnings"  # 状态是「带警告封存」
    assert sealed.transcript_json["messages"][0]["content"] == "remember exact input"  # 原始用户输入保留
    assert sealed.transcript_json["messages"][1]["content"] == "completed answer"  # 原始回答保留
    assert "memory_extraction_failed" in sealed.transcript_json["sealing"]["errors"][0]  # 错误被记录


def test_context_assembler_keeps_recent_history_out_of_system_context() -> None:
    # 测试：上下文装配器把「最近的对话」放进 history_messages，而不是塞进 system_context
    db = _db()
    repo, conversation = _scope(db)
    now = datetime.now(timezone.utc)
    _message(db, repo, conversation, role="user", content="UNIQUE-RECENT-MESSAGE", at=now)

    assembled = ContextAssembler(db).assemble(repo, conversation, "UNIQUE-RECENT-MESSAGE")

    assert "UNIQUE-RECENT-MESSAGE" not in assembled.system_context  # 不在系统上下文里
    assert assembled.history_messages[-1]["content"] == "UNIQUE-RECENT-MESSAGE"  # 在 history 末尾
    assert assembled.compression_stats["pending_user_estimated_tokens"] == 0  # 最近这条还没计入压缩
    # 装配出的总 token 应不少于 history 本身的 token
    assert assembled.compression_stats["assembled_prompt_estimated_tokens"] >= assembled.compression_stats["history_estimated_tokens"]
