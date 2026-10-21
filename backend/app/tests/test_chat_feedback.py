"""DevMind AI module."""

# os：设置环境变量（内存库），导入数据库模块前。
import os
# uuid：唯一 id。
import uuid

# pytest。
import pytest
# SQLAlchemy。
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# 在 import 数据库相关代码前，把 DATABASE_URL 设成内存库。
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

# 导入反馈路由、相关数据库模型、反馈请求 schema。
from app.api.routes import feedback as feedback_routes
from app.db.models import AgentRun, Base, ChatFeedback, ChatMessage, ChatSession, Conversation, Repository
from app.schemas.feedback import ChatFeedbackCreate, ChatFeedbackReview


def _db_session():
    """_db_session."""
    # 建内存库 + 建表 + 出会话。
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


# 测试辅助函数：造一整套"对话 + 一条助手消息 + 一次 Agent 运行 + 一个会话"的关联数据，作为反馈测试的舞台。
# 为什么：反馈必须挂在某条助手消息上，且要能追溯到那次 Agent 运行，所以提前把上下游数据都建好。
def _feedback_fixture(db):
    """_feedback_fixture."""
    # 仓库（full_name 里塞个随机串，避免多个测试撞名）。
    repo = Repository(owner="devflow", name="demo", full_name=f"devflow/demo-{uuid.uuid4().hex[:8]}")
    db.add(repo)
    db.flush()
    # 会话（Conversation）。
    conversation = Conversation(repo_id=repo.id, title="反馈测试")
    db.add(conversation)
    db.flush()
    # 本次运行的 id。
    run_id = uuid.uuid4()
    # 用户消息。
    user_message = ChatMessage(
        repo_id=repo.id,
        conversation_id=conversation.id,
        role="user",
        content="为什么 CI 失败？",
    )
    # 助手消息：含路由 ci_debug、工具调用、以及 meta 里记录的 run_id 和 agent 事件。
    assistant_message = ChatMessage(
        repo_id=repo.id,
        conversation_id=conversation.id,
        role="assistant",
        content="CI 是因为测试失败。",
        route="ci_debug",
        tool_calls=[{"tool_name": "ci_debug", "status": "success"}],
        meta={"run_id": str(run_id), "agent_events": [{"type": "tool_use", "tool_name": "ci_debug"}]},
    )
    db.add_all([user_message, assistant_message])
    db.flush()
    # Agent 运行记录：记录了这次回答的路由、工具调用、最终答案。
    db.add(
        AgentRun(
            id=run_id,
            repo_id=repo.id,
            conversation_id=conversation.id,
            user_message=user_message.content,
            route="ci_debug",
            tool_calls=assistant_message.tool_calls,
            final_answer=assistant_message.content,
            status="success",
        )
    )
    # 会话记录（ChatSession，已密封 sealed）：存了完整 transcript。
    db.add(
        ChatSession(
            repo_id=repo.id,
            conversation_id=conversation.id,
            user_message_id=user_message.id,
            assistant_message_id=assistant_message.id,
            route="ci_debug",
            transcript_json={
                "agent_events": [{"type": "tool_use", "tool_name": "ci_debug"}],
                "tool_use_results": [{"type": "tool_result", "status": "success"}],
                "memory_update": {"facts": ["CI failed"]},
            },
            digest="排查 CI 失败",
            status="sealed",
        )
    )
    db.commit()
    # 把建好的关键对象一并返回。
    return repo, conversation, assistant_message, run_id


@pytest.mark.asyncio
async def test_negative_feedback_persists_notifies_and_traces(monkeypatch):
    """test_negative_feedback_persists_notifies_and_traces."""
    db = _db_session()
    repo, conversation, assistant_message, run_id = _feedback_fixture(db)
    # 用来收集"发出去过哪些通知"的列表。
    notifications = []

    # 假通知函数：把收到的参数收进 notifications，返回 ("sent", None) 表示发送成功。
    async def fake_notification(**kwargs):
        """fake_notification."""
        """fake_notification."""
        notifications.append(kwargs)
        return "sent", None

    # 把真实的"发送负反馈通知"换成假函数，便于断言通知确实被调用、且内容可控。
    monkeypatch.setattr(feedback_routes, "send_negative_feedback_notification", fake_notification)
    # 创建一条"没帮助"的负反馈，原因 inaccurate（不准确）。
    result = await feedback_routes.create_feedback(
        ChatFeedbackCreate(
            repo_id=repo.id,
            conversation_id=conversation.id,
            assistant_message_id=assistant_message.id,
            rating="unhelpful",
            reason="inaccurate",
            comment="真正失败的是类型检查。",
        ),
        db,
    )

    # 断言：反馈里记录的 run_id 能追溯到那次 Agent 运行（关联正确）。
    assert result.run_id == run_id
    # 断言：通知状态是 sent（负反馈确实触发了通知）。
    assert result.notification_status == "sent"
    # 断言：复核状态是 open（待人工处理）。
    assert result.review_status == "open"
    # 断言：确实发了 1 条通知。
    assert len(notifications) == 1

    # 查反馈指标。
    metrics = await feedback_routes.feedback_metrics(repo.id, db)
    # 断言：助手消息数 1、负反馈数 1、负反馈率 1.0、原因计数 accurate:1。
    assert metrics["assistant_messages"] == 1
    assert metrics["unhelpful"] == 1
    assert metrics["negative_feedback_rate"] == 1.0
    assert metrics["reason_counts"] == {"inaccurate": 1}

    # 查反馈溯源（根据 run_id 把整条链路串起来）。
    trace = await feedback_routes.feedback_trace(run_id, db)
    # 断言：trace_id、运行路由、助手消息 id、会话里的工具事件都对得上。
    assert trace["trace_id"] == str(run_id)
    assert trace["run"]["route"] == "ci_debug"
    assert trace["messages"]["assistant"]["id"] == str(assistant_message.id)
    assert trace["session"]["agent_events"][0]["tool_name"] == "ci_debug"

    # 人工复核这条反馈：标记 resolved。
    reviewed = await feedback_routes.review_feedback(
        result.id,
        ChatFeedbackReview(review_status="resolved", review_note="已修复 Prompt"),
        db,
    )
    # 断言：复核状态变成 resolved，且 reviewed_at 时间戳被填上（说明复核动作落库）。
    assert reviewed.review_status == "resolved"
    assert reviewed.reviewed_at is not None


@pytest.mark.asyncio
async def test_feedback_is_idempotent_per_assistant_message(monkeypatch):
    """test_feedback_is_idempotent_per_assistant_message."""
    db = _db_session()
    repo, conversation, assistant_message, _run_id = _feedback_fixture(db)
    notifications = []

    async def fake_notification(**kwargs):
        notifications.append(kwargs)
        return "sent", None

    monkeypatch.setattr(feedback_routes, "send_negative_feedback_notification", fake_notification)
    # 构造同一份"没帮助"反馈 payload。
    payload = ChatFeedbackCreate(
        repo_id=repo.id,
        conversation_id=conversation.id,
        assistant_message_id=assistant_message.id,
        rating="unhelpful",
    )
    # 连续提交两次完全相同的反馈。
    first = await feedback_routes.create_feedback(payload, db)
    second = await feedback_routes.create_feedback(payload, db)

    # 断言：两次返回的是同一条记录（id 相同）——反馈对同一助手消息是幂等的，不会重复插入。
    assert first.id == second.id
    # 断言：库里只有 1 条 ChatFeedback（没有因为重复提交而多出来）。
    assert db.query(ChatFeedback).count() == 1
    # 断言：通知也只发了 1 次（幂等延伸到通知，不会重复打扰）。
    assert len(notifications) == 1

    # 再提交一条"有帮助"反馈（同一助手消息、不同 rating）。
    helpful = await feedback_routes.create_feedback(
        ChatFeedbackCreate(
            repo_id=repo.id,
            conversation_id=conversation.id,
            assistant_message_id=assistant_message.id,
            rating="helpful",
        ),
        db,
    )
    # 断言：变成 helpful、没有原因、且通知状态是 not_applicable（正面反馈不需要发负反馈通知）。
    assert helpful.rating == "helpful"
    assert helpful.reason is None
    assert helpful.notification_status == "not_applicable"
