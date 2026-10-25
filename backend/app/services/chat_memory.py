"""聊天记忆与上下文组装服务（chat_memory.py）。

本模块是 DevMind AI 后端的"记忆中枢"，负责把一次对话里产生的所有消息、证据、会话记忆
组织成可以喂给大模型（LLM）的上下文。它被编排层（agents / workflow_orchestrator）和
API 路由在每次请求时调用，用来决定"这次该把哪些历史、哪些记忆、哪些证据"放进提示词里。

关键类：
- MessageStore：对 ChatMessage 表的增删查（消息的持久化与读取）。
- EvidenceStore：把仓库里的 Issue / PR / CI 日志 / 文档等"证据"建索引，支持关键词检索。
- ContextAssembler：把"系统上下文 + 历史消息 + 证据 + 记忆"拼成 AssembledContext。
- ProgressiveContextManager：当上下文太长时，自动把旧消息压缩成摘要（类比：聊天记录太长
  时把旧内容总结成几句话），并带熔断保护避免压缩失败死循环。
- SessionSealer：一轮对话结束后，把这次问答提炼成"会话记忆"存起来。
- MemoryTools：提供给 Agent 的工具函数（搜索证据、读取原始会话、读取历史 Session）。

与其他模块的关系：依赖 context_compression（压缩算法）、llm/client（调用大模型）、
memory_hub（记忆候选采集与召回记录）、rag.*（向量检索与日志脱敏）。
"""


# 标准库：正则（分词）、uuid（生成唯一 ID）、dataclass（轻量数据结构）、时间处理
import re
import uuid
# dataclass：用装饰器快速定义"只装数据、几乎没有方法"的类；field：给字段设置默认值工厂
from dataclasses import dataclass, field
# datetime：时间戳；timedelta：时间间隔；timezone：时区（这里统一用 UTC，避免时区混乱）
from datetime import datetime, timedelta, timezone
# typing.Any：任意类型（写函数签名时偷懒用，表示"什么类型都行"）
from typing import Any

# SQLAlchemy：Python 里最常用的关系型数据库 ORM（对象关系映射）。and_/or_：SQL 的 AND/OR 条件
from sqlalchemy import and_, or_
# Session：数据库会话，所有"增删改查"都通过它；类比成"一次数据库连接的快递员"
from sqlalchemy.orm import Session

# 项目配置（开关、各种压缩阈值都从这里读），settings 是个全局单例
from app.core.config import settings
# 数据库模型（表结构）：每次 import 一个表类，就代表后面能操作对应那张表
from app.db.models import (
    AgentRun,          # 一次 Agent 运行的记录
    ChatMessage,       # 一条聊天消息（user/assistant/system/tool 都存这里）
    ChatSession,       # 一轮完整对话（一问一答+工具调用）被"封口"后的快照
    ChatSession as ChatSessionModel,  # 给同一张表起个别名，下面写返回值类型时用
    Conversation,      # 一个"会话/话题"容器，下面会挂很多消息
    ConversationMemory,  # 针对单个 Conversation 的结构化记忆
    Document,          # RAG 索引过的文档
    EvidenceItem,      # 一条"证据"（Issue/PR/CI/文档的摘要），供检索用
    Issue,             # GitHub Issue
    PullRequest,       # GitHub PR
    Repository,        # 仓库
    ThreadMemory,      # 针对整个线程（跨会话）的结构化记忆
    WorkflowRun,       # 一次 CI/CD 工作流运行记录
)
# 上下文压缩模块：本文件用到它的压缩、预算、摘要等一堆函数/常量
from app.services.context_compression import (
    COMPACT_BOUNDARY_SUBTYPE,                  # 压缩边界消息的 meta 子类型标识
    COMPACT_SUMMARY_SYSTEM_PROMPT,             # 生成压缩摘要时用的系统提示词
    adjust_keep_start_to_preserve_api_invariants,  # 调整保留起点，保证保留的消息对（用户+助手）完整
    build_compaction_summary_payload,          # 把要压缩的消息拼成可发给 LLM 的载荷
    calculate_context_pressure,                # 根据 token 数算出"上下文压力"处于哪个阶段
    clip_to_token_budget,                      # 把文本截到不超过 token 预算
    compress_messages,                         # 把历史消息压缩（保留精确后缀 + 截断中间）
    default_context_budget,                    # 默认的各区块 token 预算
    dumps_compact,                             # 把对象紧凑地序列化成字符串
    ensure_compaction_summary_preserves_key_facts,  # 校验摘要是否漏掉了关键事实，漏了就补
    estimate_tokens,                           # 估算一段文本的 token 数（粗略按字符/词算）
    extract_relevant_snippet,                  # 从长文本里截取和查询最相关的片段
    fallback_memory_update,                    # LLM 不可用时的兜底记忆更新
    fallback_compaction_summary,               # LLM 不可用时的兜底压缩摘要（抽取式）
    format_compaction_summary,                 # 把 LLM 返回的摘要整理成标准格式
    is_compact_boundary_message,               # 判断一条消息是不是"压缩边界"
    messages_after_compact_boundary,           # 取压缩边界之后的消息
    memory_document_text,                      # 把记忆模型渲染成纯文本
    merge_memory,                              # 把旧记忆和新增量合并
    microcompact_messages,                      # 微压缩：在边界之前先轻度压缩（保命用）
    render_structured_memory,                  # 把结构化记忆渲染成可读文本
    structured_memory_from_model,              # 把模型返回的 dict 转成标准记忆结构
)
# LLM 客户端：真正去调大模型（OpenAI 等）的地方
from app.services.llm.client import LLMClient
# memory_hub：负责把"记忆候选"落库、记录"召回事件"（统计哪些记忆被用过）
from app.services.memory_hub import capture_memory_candidates, record_recall_event
# RAG 索引：CI 日志脱敏相关常量和函数
from app.services.rag.indexing import CI_LOG_CHUNK_CHARS, sanitize_ci_log
# RAG 检索：向量相似度搜索文档
from app.services.rag.retrieval import search_similar_documents
# RAG 来源策略：允许进证据的文档来源类型清单
from app.services.rag.source_policy import RAG_SOURCE_TYPES
# 向量库不可用时抛的异常（捕获它后降级成关键词检索）
from app.services.rag.vector_store import MilvusUnavailableError


def _repo_filter(model: Any, repo_id: uuid.UUID | None) -> Any:
    # 生成一个 SQL 过滤条件：repo_id 为 None 时表示"不限仓库"（用 IS NULL），
    # 否则精确匹配某个仓库。为什么：很多查询都要按仓库隔离，这里统一封装避免重复写。
    return model.repo_id.is_(None) if repo_id is None else model.repo_id == repo_id


def _conversation_filter(model: Any, conversation_id: uuid.UUID | None) -> Any:
    # 同上，但针对 conversation_id：为空表示"不限会话"。
    return model.conversation_id.is_(None) if conversation_id is None else model.conversation_id == conversation_id


def _tokens(text: str) -> list[str]:
    # 把文本切成"词元"列表，用于关键词匹配：英文/数字/下划线/连字符 + 中文都算一个词。
    # 为什么：中文不能按空格分词，这里用正则同时覆盖中英文，方便后面做关键词计数。
    return re.findall(r"[a-zA-Z0-9_\-\u4e00-\u9fff]+", text.lower())


def _clip(text: str, limit: int) -> str:
    # 把文本截断到 limit 长度，超出就加"..."。先 strip 掉首尾空白。
    text = (text or "").strip()
    return text if len(text) <= limit else text[: max(0, limit - 3)].rstrip() + "..."


class MessageStore:
    # 消息仓库：封装对 ChatMessage 表的读写，是记忆系统最底层的"存消息"工具。
    def __init__(self, db: Session) -> None:
        # 构造时传入一个数据库会话；self.db 之后所有方法共用这同一个连接
        self.db = db

    def append(
        self,
        *,
        repo_id: uuid.UUID | None,
        conversation_id: uuid.UUID | None,
        role: str,
        content: str,
        route: str | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> ChatMessage:
        # 追加一条消息。*: 表示后面这些参数都必须用"名字=值"的方式传（强制具名，避免传错顺序）。
        # role：谁说的（user/assistant/system/tool）；route：来源路由（哪个功能产生的）。
        # tool_calls：工具调用记录；meta：任意附加元数据（用 dict 装任何东西）。
        message = ChatMessage(
            repo_id=repo_id,
            conversation_id=conversation_id,
            role=role,
            content=content,
            route=route,
            tool_calls=tool_calls or [],  # 没传就给空列表，避免数据库里存 None
            meta=meta or {},              # 没传就给空字典
        )
        self.db.add(message)   # 把新对象放进会话（还没真正写库）
        self.db.flush()        # flush：让数据库生成自增 ID，但还没 commit（事务中途可见）
        return message         # 返回这条消息（调用方通常要用它的 id）

    def recent(self, repo_id: uuid.UUID | None, conversation_id: uuid.UUID | None, limit: int = 20) -> list[ChatMessage]:
        # 返回"当前活跃上下文投影"，而不是完整逐字记录。
        # 为什么：压缩边界之后的消息才是模型真正要看到的"近期窗口"，老消息已被摘要替代。
        # 新结构的压缩边界会保留对原始近期消息的引用，而不是复制；老库可能还存着
        # preserved_after_compact 副本，所以这里兼容两种布局（迁移期）。
        boundary = self.last_compact_boundary(repo_id, conversation_id)
        if boundary is None:
            # 还没有任何压缩边界：直接拿时间线（默认包含 system 消息）
            return self.timeline(repo_id, conversation_id, limit=limit, include_system=True)

        boundary_meta = boundary.meta or {}  # 边界消息自己带的元数据
        preserved_ids = [
            # 从边界元数据里取出"被保留的原始消息 ID 列表"，过滤掉空值
            value
            for value in boundary_meta.get("preserved_message_ids") or []
            if value
        ]
        preserved_rows: list[ChatMessage] = []  # 准备装"被保留的原始消息对象"
        if preserved_ids:
            parsed_ids: list[uuid.UUID] = []  # 把字符串 ID 解析回 uuid 对象
            for value in preserved_ids:
                try:
                    # 尝试解析成 UUID；解析失败（脏数据）就跳过，保证健壮
                    parsed_ids.append(uuid.UUID(str(value)))
                except ValueError:
                    continue
            if parsed_ids:
                # 一次性按 ID 列表查出这些被保留的消息，做成 {id: 行} 字典方便后面按序取
                by_id = {
                    row.id: row
                    for row in self.db.query(ChatMessage)
                    .filter(
                        _repo_filter(ChatMessage, repo_id),
                        _conversation_filter(ChatMessage, conversation_id),
                        ChatMessage.id.in_(parsed_ids),  # SQL 的 IN (...) 条件
                    )
                    .all()
                }
                # 按 preserved_ids 的原始顺序还原（保持时间顺序感）
                preserved_rows = [by_id[item] for item in parsed_ids if item in by_id]

        rows_after_boundary = (
            # 查"边界时间之后"的所有消息（含边界本身也可能在这里，后面会排除）
            self.db.query(ChatMessage)
            .filter(
                _repo_filter(ChatMessage, repo_id),
                _conversation_filter(ChatMessage, conversation_id),
                ChatMessage.created_at >= boundary.created_at,  # 时间 >= 边界时间
            )
            .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())  # 先按时间、再按 ID 升序
            .all()
        )
        # 投影 = 边界消息 + 被保留的原始消息（排在最前，代表"已知摘要 + 关键原文"）
        projected = [boundary, *preserved_rows]
        projected.extend(
            # 再把边界之后的消息接在后面，但要排除：边界本身、已在 preserved 里的、以及老库的副本
            row
            for row in rows_after_boundary
            if row.id != boundary.id
            and str(row.id) not in preserved_ids
            and not (
                preserved_ids
                and (row.meta or {}).get("subtype") == "preserved_after_compact"
            )
        )
        # 只取最后 limit 条（负数切片表示"从末尾往前"）；max(1, limit) 保证至少 1 条
        return projected[-max(1, limit) :]

    def timeline(
        self,
        repo_id: uuid.UUID | None,
        conversation_id: uuid.UUID | None,
        *,
        limit: int | None = 500,
        include_system: bool = False,
        before_message_id: uuid.UUID | None = None,
        after_message_id: uuid.UUID | None = None,
    ) -> list[ChatMessage]:
        # 返回"持久化逐字记录"（完整历史），同时隐藏兼容副本。
        # 为什么：压缩边界改变的是模型"看到的"投影，并不会删除逐字记录。
        # 这是给 UI 历史、精确记忆工具用的"权威读取路径"。
        query = self.db.query(ChatMessage).filter(
            _repo_filter(ChatMessage, repo_id),
            _conversation_filter(ChatMessage, conversation_id),
        )
        if not include_system:
            # 默认不把 system 消息（如压缩边界）放进历史时间线
            query = query.filter(ChatMessage.role != "system")

        if before_message_id:
            # 分页：只要"某条消息之前"的消息。先查出那条消息，拿它的时间/ID 做游标
            before = (
                self.db.query(ChatMessage)
                .filter(
                    _repo_filter(ChatMessage, repo_id),
                    _conversation_filter(ChatMessage, conversation_id),
                    ChatMessage.id == before_message_id,
                )
                .one_or_none()  # 最多一条，没有就返回 None
            )
            if before and before.created_at:
                # 游标条件：created_at 更早，或同一时刻但 ID 更小（ID 自增，越小越早）
                query = query.filter(
                    or_(
                        ChatMessage.created_at < before.created_at,
                        and_(
                            ChatMessage.created_at == before.created_at,
                            ChatMessage.id < before.id,
                        ),
                    )
                )
        if after_message_id:
            # 同理：只要"某条消息之后"的消息
            after = (
                self.db.query(ChatMessage)
                .filter(
                    _repo_filter(ChatMessage, repo_id),
                    _conversation_filter(ChatMessage, conversation_id),
                    ChatMessage.id == after_message_id,
                )
                .one_or_none()
            )
            if after and after.created_at:
                query = query.filter(
                    or_(
                        ChatMessage.created_at > after.created_at,
                        and_(
                            ChatMessage.created_at == after.created_at,
                            ChatMessage.id > after.id,
                        ),
                    )
                )

        rows = query.order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc()).all()
        # 过滤掉老库遗留的 preserved_after_compact 兼容副本（它们不是真正的逐字记录）
        durable = [
            row
            for row in rows
            if (row.meta or {}).get("subtype") != "preserved_after_compact"
        ]
        if limit is None:
            return durable  # 不限制条数，全返回
        if after_message_id and not before_message_id:
            return durable[: max(1, limit)]   # 向后翻页：取前面 limit 条
        return durable[-max(1, limit) :]       # 默认：取最近 limit 条

    def active_for_context(
        self,
        repo_id: uuid.UUID | None,
        conversation_id: uuid.UUID | None,
        limit: int = 1000,
    ) -> list[ChatMessage]:
        # 给"上下文组装"用的别名：就是拿 recent（活跃投影）
        return self.recent(repo_id, conversation_id, limit=limit)

    def all_for_context(self, repo_id: uuid.UUID | None, conversation_id: uuid.UUID | None, limit: int = 200) -> list[ChatMessage]:
        # 取最近 limit 条（按时间倒序），再 reverse 成"由旧到新"的顺序返回
        rows = (
            self.db.query(ChatMessage)
            .filter(_repo_filter(ChatMessage, repo_id), _conversation_filter(ChatMessage, conversation_id))
            .order_by(ChatMessage.created_at.desc())  # 倒序：最新的在最前
            .limit(limit)
            .all()
        )
        return list(reversed(rows))  # 翻成正序，符合"对话天然从早到晚"的阅读习惯

    def last_compact_boundary(self, repo_id: uuid.UUID | None, conversation_id: uuid.UUID | None) -> ChatMessage | None:
        # 找最近的一条"压缩边界"系统消息。最多看最近 50 条 system 消息，从新到旧，
        # 用 is_compact_boundary_message 判定是不是压缩边界（靠 meta.subtype 区分）。
        rows = (
            self.db.query(ChatMessage)
            .filter(_repo_filter(ChatMessage, repo_id), _conversation_filter(ChatMessage, conversation_id), ChatMessage.role == "system")
            .order_by(ChatMessage.created_at.desc())
            .limit(50)
            .all()
        )
        for row in rows:
            if is_compact_boundary_message({"role": row.role, "meta": row.meta or {}}):
                return row
        return None  # 没找到就返回 None


class EvidenceStore:
    # 证据仓库：把仓库里的各种"事实来源"（Issue/PR/CI 日志/文档/记忆/Session）建成可检索的索引。
    def __init__(self, db: Session) -> None:
        self.db = db  # 保存数据库会话

    def rebuild_repo(self, repo: Repository | None, conversation: Conversation | None = None) -> None:
        # 重建某个仓库（或某会话）的证据索引：先删旧的，再按当前数据重新填。
        # 为什么：证据会随 Issue/PR/文档变化而过时，需要能整体重算。
        repo_id = repo.id if repo else None
        conversation_id = conversation.id if conversation else None
        if conversation_id:
            # 有会话：删"该会话专属"或"会话为空（即项目级）"的证据
            self.db.query(EvidenceItem).filter(
                _repo_filter(EvidenceItem, repo_id),
                or_(EvidenceItem.conversation_id == conversation_id, EvidenceItem.conversation_id.is_(None)),
            ).delete(synchronize_session=False)  # 不在内存会话里同步删除对象，直接走 SQL，更快
        else:
            # 没指定会话：整仓库级别的证据全删
            self.db.query(EvidenceItem).filter(_repo_filter(EvidenceItem, repo_id)).delete(synchronize_session=False)

        if repo:
            # 先把"仓库本身"作为一条证据加进去（让模型知道当前在聊哪个仓库）
            self._add(
                repo_id,
                None,
                "repository",
                str(repo.id),
                repo.full_name,
                "\n".join(filter(None, [repo.description or "", f"默认分支：{repo.default_branch or '未知'}"])),  # 描述 + 默认分支拼成内容
                {"anchor": f"repo:{repo.full_name}"},  # meta：锚点，方便前端跳转
            )
            for issue in self.db.query(Issue).filter(Issue.repo_id == repo.id).all():
                # 每个 Issue 变成一条证据：标题 + 正文 + 状态 + 标签
                self._add(
                    repo.id,
                    None,
                    "issue",
                    str(issue.id),
                    f"Issue #{issue.number}: {issue.title}",
                    "\n".join(filter(None, [issue.title, issue.body or "", f"state: {issue.state}", f"labels: {', '.join(issue.labels or [])}"])),
                    {"number": issue.number, "state": issue.state},
                )
            for pr in self.db.query(PullRequest).filter(PullRequest.repo_id == repo.id).all():
                # 每个 PR 变成一条证据：标题 + 正文 + 状态 + 基/头分支
                self._add(
                    repo.id,
                    None,
                    "pull_request",
                    str(pr.id),
                    f"PR #{pr.number}: {pr.title}",
                    "\n".join(filter(None, [pr.title, pr.body or "", f"state: {pr.state}", f"base: {pr.base_branch}", f"head: {pr.head_branch}"])),
                    {"number": pr.number, "state": pr.state},
                )
            for run in self.db.query(WorkflowRun).filter(WorkflowRun.repo_id == repo.id).all():
                # CI 运行：只保留"失败且有日志"的（成功的 CI 对排查没价值，还占空间）
                if run.conclusion != "failure" or not (run.logs_text or "").strip():
                    continue
                sanitized_logs = sanitize_ci_log(run.logs_text or "")  # 脱敏：把密钥/Token 打码
                safe_logs = sanitized_logs[:CI_LOG_CHUNK_CHARS]  # 日志截到固定字符数，避免证据过长
                self._add(
                    repo.id,
                    None,
                    "workflow_run",
                    str(run.id),
                    f"CI: {run.name}",
                    "\n".join(filter(None, [run.name, run.status, run.conclusion or "", safe_logs])),
                    {
                        "status": run.status,
                        "conclusion": run.conclusion,
                        "logs_truncated": len(sanitized_logs) > CI_LOG_CHUNK_CHARS,  # 标记是否被截断
                        "secrets_redacted": True,  # 标记已脱敏
                    },
                )
            for doc in (
                self.db.query(Document)
                .filter(Document.repo_id == repo.id, Document.source_type.in_(RAG_SOURCE_TYPES))  # 只索引允许的来源类型
                .all()
            ):
                # 已索引文档直接作为证据（标题+内容+原 meta）
                self._add(repo.id, None, doc.source_type, str(doc.source_id), doc.title, doc.content, doc.meta or {})

        # 记忆：有会话用 ConversationMemory，否则用线程级 ThreadMemory
        memories = (
            self.db.query(ConversationMemory).filter(ConversationMemory.conversation_id == conversation_id).all()
            if conversation_id
            else self.db.query(ThreadMemory).filter(_repo_filter(ThreadMemory, repo_id)).all()
        )
        for memory in memories:
            self._add(
                repo_id,
                conversation_id,
                "memory",
                str(memory.id),
                "Thread memory",
                memory_document_text(memory),  # 把记忆渲染成文本
                {"sessions_incorporated": memory.sessions_incorporated},
            )
        # 准备查询最近的 Session 和 AgentRun（各取 30 条）作为证据
        session_query = self.db.query(ChatSession).filter(_repo_filter(ChatSession, repo_id))
        run_query = self.db.query(AgentRun).filter(_repo_filter(AgentRun, repo_id))
        if conversation_id:
            # 限定到具体会话
            session_query = session_query.filter(ChatSession.conversation_id == conversation_id)
            run_query = run_query.filter(AgentRun.conversation_id == conversation_id)
        for session in session_query.order_by(ChatSession.created_at.desc()).limit(30):
            # 每个封口 Session 的摘要作为一条证据
            self._add(repo_id, conversation_id, "session", str(session.id), f"Session digest: {session.route}", session.digest, {"route": session.route})
        for run in run_query.order_by(AgentRun.created_at.desc()).limit(30):
            # 每个 Agent 运行（用户问题 + 最终答案）作为一条证据
            self._add(repo_id, conversation_id, "agent_run", str(run.id), f"Agent run: {run.route}", f"{run.user_message}\n{run.final_answer}", {"route": run.route})
        self.db.flush()  # 一次性刷入库

    def search(self, repo_id: uuid.UUID | None, conversation_id: uuid.UUID | None, query: str, limit: int = 5) -> list[dict[str, Any]]:
        # 混合检索：先试向量库（语义相似）拿一部分，再用本地关键词匹配 EvidenceItem 拿一部分，合并去重。
        query_terms = _tokens(query)  # 把查询拆词
        if not query_terms:
            return []  # 空查询直接返回空，避免后面无意义扫描
        results: list[dict[str, Any]] = []  # 最终结果（统一字典结构）
        seen: set[tuple[str, str]] = set()  # 去重集合：用 (source_type, source_id) 当唯一键
        if repo_id:
            try:
                # 先尝试向量语义检索（可能抛 MilvusUnavailableError）
                rag_results = search_similar_documents(
                    self.db,
                    repo_id,
                    query,
                    source_type=None,
                    limit=max(limit, 8),  # 多取一点，后面会按 limit 截断
                )
            except MilvusUnavailableError:
                # 向量库挂了就降级：当没结果，后面靠关键词兜底
                rag_results = []
            for item in rag_results:
                key = (str(item.get("source_type") or ""), str(item.get("source_id") or ""))
                if key in seen:
                    continue  # 已经加过，跳过
                seen.add(key)
                results.append(
                    {
                        "title": item.get("title") or "",
                        "source_type": item.get("source_type") or "",
                        "source_id": item.get("source_id") or "",
                        "snippet": item.get("snippet") or "",  # 摘要片段
                        "score": item.get("score") or 0,       # 相似度分数
                        "metadata": item.get("metadata") or {},
                        "retrieval": item.get("retrieval") or {},  # 检索来源标记
                    }
                )
                if len(results) >= limit:
                    return results  # 够了就提前返回
        # 关键词检索：从 EvidenceItem 表里按词频打分
        candidates = (
            self.db.query(EvidenceItem)
            .filter(
                _repo_filter(EvidenceItem, repo_id),
                or_(EvidenceItem.conversation_id == conversation_id, EvidenceItem.conversation_id.is_(None)),
            )
            .order_by(EvidenceItem.created_at.desc())
            .limit(500)  # 最多看 500 条候选，控制开销
            .all()
        )
        ranked: list[tuple[int, EvidenceItem]] = []  # (分数, 证据) 列表
        for item in candidates:
            key = (item.source_type, item.source_id)
            if key in seen:
                continue  # 向量检索已收录，跳过避免重复
            haystack = f"{item.title}\n{item.content}".lower()  # 标题+内容拼成待匹配文本（转小写）
            score = sum(haystack.count(term) for term in query_terms)  # 词出现次数之和 = 分数
            if score:
                ranked.append((score, item))  # 只有命中才进入排名
        ranked.sort(key=lambda pair: (pair[0], pair[1].created_at), reverse=True)  # 分数高优先，其次新优先
        for score, item in ranked:
            key = (item.source_type, item.source_id)
            if key in seen:
                continue
            seen.add(key)
            results.append(
                {
                    "title": item.title,
                    "source_type": item.source_type,
                    "source_id": item.source_id,
                    # 从长内容里截取和查询最相关的 90 token 片段作为 snippet
                    "snippet": extract_relevant_snippet(query, item.content, max_tokens=90),
                    "score": score,
                    "metadata": item.meta or {},
                    "retrieval": {"sources": ["evidence_item_keyword"]},  # 标记来自关键词检索
                }
            )
            if len(results) >= limit:
                break  # 达到上限就停
        return results

    def _add(self, repo_id: uuid.UUID | None, conversation_id: uuid.UUID | None, source_type: str, source_id: str, title: str, content: str, meta: dict[str, Any]) -> None:
        # 内部方法：往 EvidenceItem 表插入一条证据。空内容不插（没意义）。
        if not content.strip():
            return
        self.db.add(EvidenceItem(repo_id=repo_id, conversation_id=conversation_id, source_type=source_type, source_id=source_id, title=title, content=content, meta=meta))


@dataclass
class AssembledContext:
    # 一个"装好上下文"的数据包，传给上层拼提示词用。dataclass 自动生成 __init__。
    system_context: str                                  # 系统级上下文（仓库/会话/记忆/证据拼成的文本）
    history_messages: list[dict[str, str]]               # 近期历史消息（role+content）
    evidence: list[dict[str, Any]]                       # 检索到的证据列表
    compression_stats: dict[str, Any] = field(default_factory=dict)  # 压缩统计信息（默认空字典）


class ContextAssembler:
    # 上下文组装器：把零散的记忆/证据/历史拼成 AssembledContext。是"拼 prompt 前的最后一步"。
    def __init__(self, db: Session) -> None:
        self.db = db  # 保存数据库会话

    def assemble(self, repo: Repository | None, conversation: Conversation | None, current_message: str, limit: int = 16) -> AssembledContext:
        # 核心方法：组装一次请求要用的完整上下文。
        # repo/conversation：当前仓库与会话（可能为空，表示项目级）；current_message：用户刚说的话。
        budget = default_context_budget()  # 各区块（system/memory/evidence/recent）的 token 预算
        repo_id = repo.id if repo else None
        conversation_id = conversation.id if conversation else None
        message_store = MessageStore(self.db)  # 消息仓库实例
        boundary = message_store.last_compact_boundary(repo_id, conversation_id)  # 最近的压缩边界
        memory = (
            # 取结构化记忆：有会话用 ConversationMemory，否则线程级 ThreadMemory
            self.db.query(ConversationMemory).filter(ConversationMemory.conversation_id == conversation_id).first()
            if conversation_id
            else self.db.query(ThreadMemory).filter(_repo_filter(ThreadMemory, repo_id)).first()
        )
        latest_session = (
            # 取最近一条"已封口"的 Session（带 digest），用于把上一轮摘要带进来
            self.db.query(ChatSession)
            .filter(
                _repo_filter(ChatSession, repo_id),
                _conversation_filter(ChatSession, conversation_id),
                ChatSession.status.in_(["sealed", "sealed_with_warnings"]),  # 只要封口成功的
            )
            .order_by(ChatSession.created_at.desc())
            .first()
        )
        evidence = EvidenceStore(self.db).search(repo_id, conversation_id, current_message, limit=8)  # 用当前问题检索证据（取 8 条）
        history_fetch_limit = max(limit, 30)  # 至少取 30 条原始消息，保证有足够材料可压缩
        recent = message_store.recent(repo_id, conversation_id, limit=history_fetch_limit)  # 取活跃投影
        raw_history = [
            {
                "role": item.role,
                "content": item.content,
                "route": item.route,
                "tool_calls": item.tool_calls or [],
                "meta": item.meta or {},
            }
            # 只保留 user/assistant 消息进历史（system/tool 不进对话历史）
            for item in recent
            if item.role in {"user", "assistant"}
        ]
        micro_stats: dict[str, Any] | None = None  # 微压缩统计（还没做）
        raw_history_tokens = sum(
            # 估算原始历史的 token 总数（role+content+工具调用）
            estimate_tokens(str(item.get("role") or ""))
            + estimate_tokens(str(item.get("content") or ""))
            + (
                estimate_tokens(dumps_compact(item.get("tool_calls")))
                if item.get("tool_calls")
                else 0
            )
            for item in raw_history
        )
        current_message_in_history = bool(
            # 判断"当前用户消息"是不是已经作为最后一条历史存在（避免重复计入）
            raw_history
            and raw_history[-1].get("role") == "user"
            and " ".join(str(raw_history[-1].get("content") or "").split())
            == " ".join(str(current_message or "").split())  # 用去多余空格后的文本比较
        )
        pending_user_tokens = 0 if current_message_in_history else estimate_tokens(current_message)  # 待发的用户消息 token 数
        raw_history_pressure = calculate_context_pressure(raw_history_tokens + pending_user_tokens)  # 原始历史的压力
        history_source = raw_history
        if raw_history_pressure.should_warn:
            # 原始历史就超预算了：先做"微压缩"抢救一下（轻量、保真）
            history_source, micro_stats = microcompact_messages(raw_history, current_message)
        history, history_stats = compress_messages(history_source, budget.recent_tokens)  # 再按预算压缩历史

        def build_context(next_history: list[dict[str, str]], next_evidence: list[dict[str, Any]]) -> str:
            # 内部闭包：把"系统段 + 记忆段 + 证据段"拼成一段 system_context 文本。
            # 为什么用闭包：它需要访问外层 budget/repo/conversation/boundary 等变量，省得传一长串参数。
            system_sections: list[str] = []  # 系统静态段（仓库名、会话标题）
            if repo:
                system_sections.append(f"[Current Repository]\n{repo.full_name}\n[/Current Repository]")  # 标明当前仓库
            if conversation:
                system_sections.append(f"[Current Conversation]\n{conversation.title}\n[/Current Conversation]")  # 标明当前会话

            sections: list[str] = []  # 最终各段会放进这里
            stable_system = clip_to_token_budget("\n\n".join(system_sections), budget.system_tokens)  # 系统静态段截到预算
            if stable_system:
                sections.append(stable_system)  # 非空才加

            memory_sections: list[str] = []  # 记忆相关段
            if boundary and boundary.content:
                # 有压缩边界：把边界摘要、可能的"再水合"工作态、恢复提示拼进来
                transcript_ref = (boundary.meta or {}).get("transcript_ref") or {}  # 精确原文恢复引用
                rehydration = (boundary.meta or {}).get("rehydration") or {}       # 再水合（恢复工作状态）数据
                recovery_hint = (
                    # 如果有 transcript_ref，提示模型：想看精确原文就调这个工具
                    "\nExact transcript recovery: call devflow_get_thread_context with this conversation"
                    if transcript_ref
                    else ""
                )
                if rehydration:
                    # 把"再水合工作状态"（正在编辑的文件/激活的技能/近期工具）作为一段
                    memory_sections.append(
                        "[Rehydrated Working State]\n"
                        + dumps_compact(rehydration)
                        + "\n[/Rehydrated Working State]"
                    )
                memory_sections.append(
                    f"[Compaction Boundary]\n{boundary.content}{recovery_hint}\n[/Compaction Boundary]"  # 压缩边界摘要
                )
            if memory and memory_document_text(memory).strip():
                # 结构化记忆：渲染成文本并截到预算的一半左右
                memory_text = render_structured_memory(memory, max_tokens=max(256, budget.memory_tokens // 2))
                memory_sections.append(f"[Thread Memory: {memory.sessions_incorporated} sessions]\n{memory_text}\n[/Thread Memory]")
            if latest_session and latest_session.digest:
                # 上一轮封口摘要
                memory_sections.append(f"[Previous Session Digest]\n{latest_session.digest}\n[/Previous Session Digest]")
            memory_context = clip_to_token_budget("\n\n".join(memory_sections), budget.memory_tokens)  # 记忆段整体截预算
            if memory_context:
                sections.append(memory_context)
            if next_evidence:
                # 证据段：每条证据一行，按预算均分给每条（至少 120 token）
                evidence_budget = max(120, budget.evidence_tokens // max(len(next_evidence), 1))
                lines = ["[Related Evidence]"]
                for item in next_evidence:
                    snippet = clip_to_token_budget(item["snippet"], evidence_budget)  # 每条证据截到均分预算
                    lines.append(f"- [{item['source_type']}] {item['title']}: {snippet}")
                lines.append("[/Related Evidence]")
                sections.append(clip_to_token_budget("\n".join(lines), budget.evidence_tokens))
            return "\n\n".join(sections)  # 各段用空行拼起来

        system_context = build_context(history, evidence)  # 生成系统上下文
        history_prompt_tokens = sum(
            # 算历史消息占的 token
            estimate_tokens(item.get("role")) + estimate_tokens(item.get("content"))
            for item in history
        )
        initial_prompt_tokens = estimate_tokens(system_context) + history_prompt_tokens + pending_user_tokens  # 初始总 token
        initial_pressure = calculate_context_pressure(initial_prompt_tokens)  # 初始压力
        if initial_pressure.should_warn and micro_stats is None:
            # 压缩后还超预算、且之前没做过微压缩：再补一次微压缩（双保险）
            micro_raw_history, micro_stats = microcompact_messages(raw_history, current_message)
            history, history_stats = compress_messages(micro_raw_history, budget.recent_tokens)
            system_context = build_context(history, evidence)  # 重新生成系统上下文

        history_prompt_tokens = sum(
            estimate_tokens(item.get("role")) + estimate_tokens(item.get("content"))
            for item in history
        )
        assembled_prompt_tokens = estimate_tokens(system_context) + history_prompt_tokens + pending_user_tokens  # 组装后总 token
        final_pressure = calculate_context_pressure(assembled_prompt_tokens)  # 最终压力
        microcompacted_messages = int((micro_stats or {}).get("compacted_messages") or 0)  # 被微压缩掉的条数
        history_coverage = {
            # 统计：历史覆盖度（源有多少、prompt 里剩多少、丢了多少、是否完整精确）
            "source_messages": len(raw_history),
            "prompt_messages": len(history),
            "omitted_messages": int(history_stats.get("omitted_messages") or 0),
            "truncated_messages": int(history_stats.get("truncated_messages") or 0),
            "exact_suffix_messages": int(history_stats.get("exact_suffix_messages") or 0),
            "source_limit_reached": len(recent) >= history_fetch_limit,
            "has_compaction_boundary": boundary is not None,
            "microcompacted_messages": microcompacted_messages,
        }
        history_coverage["complete_and_exact"] = not any(
            # 只有"没丢、没截、没触顶、没边界、没微压缩"才算是完整精确的上下文
            [
                history_coverage["omitted_messages"],
                history_coverage["truncated_messages"],
                history_coverage["source_limit_reached"],
                history_coverage["has_compaction_boundary"],
                history_coverage["microcompacted_messages"],
            ]
        )

        return AssembledContext(
            system_context=system_context,
            history_messages=history,
            evidence=evidence,
            compression_stats={
                # 把各种预算/压力/覆盖率打包，方便上层做监控、调试、决定是否再压缩
                "budget": budget.__dict__,
                "history": history_stats,
                "history_coverage": history_coverage,
                "microcompact": micro_stats,
                "pressure": final_pressure.as_dict(),
                "initial_pressure": initial_pressure.as_dict(),
                "raw_history_pressure": raw_history_pressure.as_dict(),
                "compact_boundary": {
                    "id": str(boundary.id) if boundary else None,
                    "created_at": boundary.created_at.isoformat() if boundary and boundary.created_at else None,
                    "mode": (boundary.meta or {}).get("mode") if boundary else None,
                },
                "system_context_estimated_tokens": estimate_tokens(system_context),
                "history_estimated_tokens": history_prompt_tokens,
                "pending_user_estimated_tokens": pending_user_tokens,
                "assembled_prompt_estimated_tokens": assembled_prompt_tokens,
                "prompt_breakdown": {
                    "system_memory_evidence": estimate_tokens(system_context),
                    "recent_history": history_prompt_tokens,
                    "pending_user": pending_user_tokens,
                    "tool_observation_budget": budget.tool_observation_tokens,
                    "reserved_response": budget.reserved_response_tokens,
                },
                "evidence_items": len(evidence),
                "mode": "progressive_budgeted_context",  # 模式名：渐进式预算上下文
            },
        )


class ProgressiveContextManager:
    # 渐进式上下文管理器：在每次请求前判断"上下文是不是快爆了"，爆了就自动压缩（带熔断）。
    def __init__(self, db: Session, llm: LLMClient | None = None) -> None:
        self.db = db
        self.llm = llm or LLMClient()  # 没传就用默认 LLM 客户端（压缩摘要要调模型）

    async def ensure_headroom(self, repo: Repository | None, conversation: Conversation | None, current_message: str) -> dict[str, Any]:
        # async：异步方法（不阻塞事件循环，等待 LLM 时能让出控制权）。
        # 作用：确保这次请求还有"余量"（headroom）。返回是否尝试压缩、原因、压力数据。
        if repo is None or conversation is None:
            # 没有仓库或会话就不压缩（没目标）
            return {"attempted": False, "reason": "no_repo_or_conversation"}
        assembled = ContextAssembler(self.db).assemble(repo, conversation, current_message)  # 先组装一次看压力
        assembled_pressure = assembled.compression_stats.get("pressure") or {}
        micro_stats = assembled.compression_stats.get("microcompact") or {}
        raw_segment_tokens = self._raw_segment_tokens(repo.id, conversation.id) + estimate_tokens(current_message)  # 原始段 token
        assembled_tokens = int(
            # 已组装上下文的 token 数（优先用 total，否则退回 system 估算）
            assembled.compression_stats.get("assembled_prompt_estimated_tokens")
            or assembled.compression_stats.get("system_context_estimated_tokens")
            or 0
        )
        raw_pressure_state = calculate_context_pressure(raw_segment_tokens)       # 原始段压力
        assembled_pressure_state = calculate_context_pressure(assembled_tokens)   # 组装后压力
        pressure_state = calculate_context_pressure(max(raw_segment_tokens, assembled_tokens))  # 取两者最大
        pressure = {
            # 把各阶段压力汇总成一份报告返回给上层
            **pressure_state.as_dict(),
            "raw_segment_estimated_tokens": raw_segment_tokens,
            "assembled_context_estimated_tokens": assembled_tokens,
            "raw_stage": raw_pressure_state.stage,
            "assembled_stage": assembled_pressure.get("stage") or assembled_pressure_state.stage,
            "microcompact_estimated_tokens_saved": micro_stats.get("estimated_tokens_saved", 0),
            "microcompact_compacted_messages": micro_stats.get("compacted_messages", 0),
        }
        raw_stage = raw_pressure_state.stage          # 原始段所处阶段
        prompt_stage = str(pressure.get("assembled_stage") or assembled_pressure_state.stage)  # 组装后阶段
        stage = str(pressure.get("stage") or "normal")  # 综合阶段
        if not settings.context_auto_compact_enabled:
            # 配置开关关了自动压缩：直接放弃，返回原因
            return {"attempted": False, "reason": "auto_compact_disabled", "pressure": pressure}
        if (
            prompt_stage in {"normal", "warning"}
            and raw_stage in {"auto_compact", "aggressive", "manual_path"}
            and int(micro_stats.get("compacted_messages") or 0) > 0
        ):
            # 微压缩已经把组装后压到正常/警告，无需再整段压缩
            return {
                "attempted": False,
                "reason": "microcompact_sufficient",
                "pressure": pressure,
                "microcompact": micro_stats,
            }
        if stage in {"normal", "warning"}:
            # 综合阶段还正常/警告：不压缩
            return {"attempted": False, "reason": stage, "pressure": pressure}

        failures = self._consecutive_failures(repo.id, conversation.id)  # 连续压缩失败次数
        if failures >= settings.context_max_compaction_failures:
            # 熔断：失败太多就打开断路器，停止尝试，避免无限重试把系统拖垮
            return {
                "attempted": False,
                "reason": "circuit_breaker_open",
                "consecutive_failures": failures,
                "pressure": pressure,
            }

        try:
            # 真正执行压缩（异步等待）
            result = await self._compact(repo, conversation, current_message, pressure, stage)
            return {"attempted": True, **result, "pressure": pressure, "consecutive_failures": 0}
        except Exception as exc:  # 防御性捕获：压缩过程中任何异常都别让请求挂掉
            self.db.rollback()  # 回滚本次事务，保证数据一致性
            failure = self._record_failure(repo.id, conversation.id, current_message, stage, exc)  # 记下这次失败
            self.db.flush()
            return {
                "attempted": True,
                "compacted": False,
                "reason": "compaction_failed",
                "failure_id": str(failure.id),
                "error": str(exc),
                "pressure": pressure,
                "consecutive_failures": failures + 1,  # 失败计数 +1（供下次熔断判断）
            }

    async def _compact(
        self,
        repo: Repository,
        conversation: Conversation,
        current_message: str,
        pressure: dict[str, Any],
        stage: str,
    ) -> dict[str, Any]:
        # 内部：真正执行一次压缩。按阶段由轻到重尝试多种方案，直到压力回到正常/警告。
        store = MessageStore(self.db)
        all_rows = store.active_for_context(repo.id, conversation.id, limit=500)  # 取活跃上下文消息
        all_messages = [self._row_to_message(row) for row in all_rows]  # 行 -> 消息字典
        segment_messages = messages_after_compact_boundary(all_messages, include_boundary=False)  # 边界之后的段
        message_ids = {str(item.get("id")) for item in segment_messages if item.get("id")}  # 段内消息 ID 集合
        segment_rows = [row for row in all_rows if str(row.id) in message_ids]  # 对应回原始行
        candidate_rows = [
            # 只压缩"有内容"的消息（user/assistant/tool 等），system 边界不动
            row
            for row in segment_rows
            if row.role in {"user", "assistant", "tool", "tool_use", "tool_result"}
        ]
        plan_stages = [stage]
        if stage == "auto_compact":
            plan_stages.extend(["aggressive", "manual_path"])  # 由轻到重依次尝试
        elif stage == "aggressive":
            plan_stages.append("manual_path")
        plans = [self._compaction_plan_for_stage(item) for item in plan_stages]  # 每个阶段对应一套参数
        if len(candidate_rows) <= min(int(item["keep_recent"]) for item in plans) + 1:
            # 消息太少（比"最保守方案要保留的数量"还少）：没得压，直接返回
            return {"compacted": False, "reason": "not_enough_messages"}

        attempts: list[dict[str, Any]] = []  # 记录每次尝试（用于返回诊断）
        selected: tuple[
            dict[str, Any],
            list[ChatMessage],
            list[ChatMessage],
            str,
            str,
            dict[str, Any],
            int,
        ] | None = None  # 选中的方案（计划/保留行/压缩行/摘要/模式/保全报告/压缩后 token）
        rehydration = self._rehydration_state(candidate_rows)  # 先算"再水合状态"（正在编辑的文件等）
        for index, plan in enumerate(plans, start=1):
            keep_rows = self._messages_to_keep(candidate_rows, keep_count=int(plan["keep_recent"]))  # 这套方案要保留的最近消息
            keep_ids = {row.id for row in keep_rows}
            summarize_rows = [row for row in candidate_rows if row.id not in keep_ids]  # 其余要被摘要掉
            if not summarize_rows:
                continue  # 没东西可摘要，跳过重试下一套
            summary, mode, preservation_report = await self._summary_for(
                # 调 LLM（或兜底）生成摘要
                repo,
                conversation,
                summarize_rows,
                current_message,
                max_summary_tokens=int(plan["summary_tokens"]),
            )
            mode_prefix = str(plan.get("mode_prefix") or "")
            if mode_prefix:
                mode = f"{mode_prefix}_{mode}"  # 给模式名加前缀（区分 aggressive/manual_path）
            post_compact_tokens = self._post_compact_tokens(
                summary,
                keep_rows,
                current_message=current_message,
                rehydration=rehydration,
            )
            post_pressure = calculate_context_pressure(post_compact_tokens)  # 压缩后压力
            attempts.append(
                {
                    "attempt": index,
                    "plan": plan,
                    "post_compact_estimated_tokens": post_compact_tokens,
                    "post_compact_stage": post_pressure.stage,
                }
            )
            selected = (
                # 记下这一套方案（即使后面可能还有更轻的方案，也先存着）
                plan,
                keep_rows,
                summarize_rows,
                summary,
                mode,
                preservation_report,
                post_compact_tokens,
            )
            if post_pressure.stage in {"normal", "warning"}:
                break  # 压到正常/警告就够了，停止尝试更重的方案（由轻到重的好处：能用轻的不用重的）

        if selected is None:
            return {"compacted": False, "reason": "nothing_to_summarize"}  # 啥都没压成
        plan, keep_rows, summarize_rows, summary, mode, preservation_report, post_compact_tokens = selected
        boundary = self._insert_boundary(
            # 把摘要作为一条 system 消息（压缩边界）插进去
            repo,
            conversation,
            summary,
            mode,
            pressure,
            summarize_rows,
            keep_rows,
            preservation_report,
            rehydration,
        )
        return {
            "compacted": True,
            "mode": mode,
            "stage": stage,
            "boundary_id": str(boundary.id),
            "messages_summarized": len(summarize_rows),
            "messages_preserved": len(keep_rows),
            "post_compact_estimated_tokens": post_compact_tokens,
            "post_compact_pressure": calculate_context_pressure(post_compact_tokens).as_dict(),
            "compaction_plan": plan,
            "compaction_attempts": attempts,
            "rehydration": rehydration,
        }

    def _compaction_plan_for_stage(self, stage: str) -> dict[str, Any]:
        # 根据压力阶段返回一套压缩参数（保留多少条最近消息 / 摘要多少 token / 模式前缀）。
        default_keep = max(2, settings.context_compact_keep_recent_messages)        # 默认保留最近 N 条
        default_summary_tokens = max(256, settings.context_compact_summary_tokens)  # 摘要默认 token 上限
        if stage == "manual_path":
            # 手动路径：压得最狠（保留更少、摘要更短），用于压力极大时
            return {
                "keep_recent": max(2, min(default_keep, max(2, default_keep // 3))),
                "summary_tokens": min(default_summary_tokens, max(256, int(default_summary_tokens * 0.5))),
                "mode_prefix": "manual_path",
            }
        if stage == "aggressive":
            # 激进：中等力度
            return {
                "keep_recent": max(2, min(default_keep, max(3, default_keep // 2))),
                "summary_tokens": min(default_summary_tokens, max(256, int(default_summary_tokens * 0.67))),
                "mode_prefix": "aggressive",
            }
        # 普通 auto_compact：用默认参数
        return {"keep_recent": default_keep, "summary_tokens": default_summary_tokens, "mode_prefix": ""}

    def _messages_to_keep(self, rows: list[ChatMessage], keep_count: int | None = None) -> list[ChatMessage]:
        # 决定压缩时"保留最近多少条"，并保证保留段以 user 消息开头（API 不允许助手消息打头）。
        keep = max(2, keep_count if keep_count is not None else settings.context_compact_keep_recent_messages)
        start_index = max(0, len(rows) - keep)  # 从倒数 keep 条处切
        start_index = adjust_keep_start_to_preserve_api_invariants(
            # 调整起点，避免把一条 user 消息和它的 assistant 回复拆开（保持对话对完整）
            [self._row_to_message(row) for row in rows],
            start_index,
        )
        keep_rows = rows[start_index:]
        if (
            keep_rows
            and keep_rows[0].role == "assistant"
            and start_index > 0
            and rows[start_index - 1].role == "user"
        ):
            # 如果保留段第一条是 assistant，把前一条 user 也补进来（对话必须从 user 开始才合法）
            keep_rows = [rows[start_index - 1], *keep_rows]
        return keep_rows

    async def _summary_for(
        self,
        repo: Repository,
        conversation: Conversation,
        rows: list[ChatMessage],
        current_message: str,
        max_summary_tokens: int,
    ) -> tuple[str, str, dict[str, Any]]:
        # 生成压缩摘要。优先用已有的结构化记忆（session_memory），否则调 LLM 生成，再兜底抽取。
        messages = [self._row_to_message(row) for row in rows]
        memory = self.db.query(ConversationMemory).filter(ConversationMemory.conversation_id == conversation.id).first()
        if memory and memory_document_text(memory).strip():
            # 已有记忆：直接基于它续写摘要（比重新总结整段更稳）
            summary = render_structured_memory(memory, max_tokens=max_summary_tokens)
            continuation = (
                "This conversation was compacted using structured session memory. "
                "Continue from the preserved recent messages and the current user request.\n\n"
                f"{summary}"
            )
            summary, report = ensure_compaction_summary_preserves_key_facts(messages, continuation, max_summary_tokens)
            return summary, "session_memory", report  # 模式：会话记忆

        # 没有记忆：构造载荷发给 LLM 总结
        payload = build_compaction_summary_payload(messages)
        llm_summary = await self.llm.chat_text(
            COMPACT_SUMMARY_SYSTEM_PROMPT,
            f"Current user request that triggered pressure:\n{current_message}\n\nConversation segment to compact:\n{payload}",
        )
        summary = format_compaction_summary(llm_summary)  # 整理成标准格式
        if not summary:
            # LLM 没返回有效摘要：用抽取式兜底（直接摘关键句）
            summary = fallback_compaction_summary(messages, max_tokens=max_summary_tokens)
            summary, report = ensure_compaction_summary_preserves_key_facts(messages, summary, max_summary_tokens)
            return summary, "fallback_extractive", report
        summary, report = ensure_compaction_summary_preserves_key_facts(messages, summary, max_summary_tokens)
        return summary, "full_compaction", report

    def _insert_boundary(
        self,
        repo: Repository,
        conversation: Conversation,
        summary: str,
        mode: str,
        pressure: dict[str, Any],
        summarized_rows: list[ChatMessage],
        keep_rows: list[ChatMessage],
        preservation_report: dict[str, Any],
        rehydration: dict[str, Any],
    ) -> ChatMessage:
        # 把压缩摘要写成一条 system 角色的消息（即"压缩边界"），作为后续上下文的起点。
        # 为什么：用一条 system 消息当"分界牌"，模型一看就知道"之前的历史已被总结到这里"。
        now = datetime.now(timezone.utc)  # 当前 UTC 时间
        latest_source_time = max(
            # 取被压缩/保留消息里最新一条的时间，用来给边界排个"不早于它们"的时间
            (row.created_at for row in [*summarized_rows, *keep_rows] if row.created_at),
            default=None,
        )
        if latest_source_time:
            # 保证边界时间严格晚于源消息，避免排序错乱（同毫秒时加 1 微秒）
            comparable_now = now if latest_source_time.tzinfo else now.replace(tzinfo=None)
            if latest_source_time >= comparable_now:
                now = latest_source_time + timedelta(microseconds=1)
        boundary = ChatMessage(
            repo_id=repo.id,
            conversation_id=conversation.id,
            role="system",       # 角色是 system（这是一条"系统注入的摘要"）
            content=summary,     # 摘要正文
            route="context_compaction",  # 来源路由标记
            tool_calls=[],
            meta={
                "subtype": COMPACT_BOUNDARY_SUBTYPE,  # 标记这是压缩边界（区别于普通 system 消息）
                "trigger": "auto",                    # 自动触发
                "mode": mode,                         # 压缩模式
                "pressure": pressure,                 # 触发时的压力数据（便于复盘）
                "pre_compact_message_id": str(summarized_rows[-1].id) if summarized_rows else None,
                "summarized_from_message_id": str(summarized_rows[0].id) if summarized_rows else None,
                "summarized_to_message_id": str(summarized_rows[-1].id) if summarized_rows else None,
                "preserved_start_message_id": str(keep_rows[0].id) if keep_rows else None,
                "preserved_message_ids": [str(row.id) for row in keep_rows],  # 保留的原始消息 ID（精确恢复用）
                "transcript_ref": {
                    # 精确原文恢复引用：模型想看完整历史就调这个工具
                    "repo_id": str(repo.id),
                    "conversation_id": str(conversation.id),
                    "tool": "devflow_get_thread_context",
                },
                "messages_summarized": len(summarized_rows),
                "messages_preserved": len(keep_rows),
                "summary_estimated_tokens": estimate_tokens(summary),
                "preservation": preservation_report,  # 关键事实保全报告
                "rehydration": rehydration,          # 再水合工作状态
            },
            created_at=now,
        )
        self.db.add(boundary)
        self.db.flush()  # 生成 ID
        return boundary

    def _post_compact_tokens(
        self,
        summary: str,
        keep_rows: list[ChatMessage],
        *,
        current_message: str = "",
        rehydration: dict[str, Any] | None = None,
    ) -> int:
        # 估算"压缩后"这段上下文占多少 token（摘要 + 保留消息 + 当前消息 + 再水合数据）
        return (
            estimate_tokens(summary)
            + sum(estimate_tokens(row.content) for row in keep_rows)
            + estimate_tokens(current_message)
            + estimate_tokens(dumps_compact(rehydration or {}))
        )

    def _rehydration_state(self, rows: list[ChatMessage]) -> dict[str, Any]:
        # 从最近的消息里提取"再水合状态"：正在编辑的文件、已激活的技能、近期用过的工具。
        # 为什么：压缩会丢掉细节，但这些"当前工作状态"很重要，单独抽出来跟着边界走，模型一看到就能"回到工作状态"。
        working_files: list[str] = []
        active_skills: list[str] = []
        recent_tools: list[str] = []

        def remember(target: list[str], value: Any, limit: int) -> None:
            # 内部辅助：往列表里记一个值，去重、超限额就丢最旧的（保留最近 limit 个）
            clean = str(value or "").strip()
            if not clean or clean in target:
                return
            target.append(clean)
            if len(target) > limit:
                del target[0 : len(target) - limit]

        for row in rows:
            calls = [item for item in (row.tool_calls or []) if isinstance(item, dict)]  # 工具调用（dict 才有效）
            events = [item for item in (row.meta or {}).get("agent_events") or [] if isinstance(item, dict)]  # Agent 事件
            for call in calls:
                remember(recent_tools, call.get("tool_name"), 8)  # 记工具名（最多 8）
                arguments = call.get("arguments") or {}
                if isinstance(arguments, dict):
                    # 从参数里抽文件路径（path 或 file_path），记最近 5 个
                    remember(working_files, arguments.get("path") or arguments.get("file_path"), 5)
            for event in events:
                event_type = str(event.get("type") or "")
                if event_type in {"tool_use", "tool_result"}:
                    remember(recent_tools, event.get("tool_name"), 8)  # 事件里的工具名也记
                arguments = event.get("arguments") or {}
                if isinstance(arguments, dict):
                    remember(working_files, arguments.get("path") or arguments.get("file_path"), 5)
                if event_type == "skill_activated":
                    # 技能激活事件：记激活的技能名（最多 6）
                    remember(active_skills, event.get("skill_name") or event.get("skill_title"), 6)

        return {
            "working_files": working_files,
            "active_skills": active_skills,
            "recent_tools": recent_tools,
            "recovery": {
                # 恢复提示：精确原文 / 已封口会话该调哪个工具
                "exact_transcript_tool": "devflow_get_thread_context",
                "sealed_session_tool": "devflow_read_session_events",
            },
        }

    def _raw_segment_tokens(self, repo_id: uuid.UUID, conversation_id: uuid.UUID) -> int:
        # 估算"压缩边界之后原始段"的 token 总数（用于压力判断）
        rows = MessageStore(self.db).active_for_context(repo_id, conversation_id, limit=1000)
        messages = messages_after_compact_boundary([self._row_to_message(row) for row in rows], include_boundary=False)
        total = 0
        for message in messages:
            total += estimate_tokens(str(message.get("role") or ""))
            total += estimate_tokens(str(message.get("content") or ""))
            if message.get("tool_calls"):
                total += estimate_tokens(dumps_compact(message.get("tool_calls")))
        return total

    def _row_to_message(self, row: ChatMessage) -> dict[str, Any]:
        # 把数据库行转成简单的消息字典（统一给压缩/摘要函数用的格式）
        return {
            "id": str(row.id),
            "role": row.role,
            "content": row.content,
            "route": row.route,
            "tool_calls": row.tool_calls or [],
            "meta": row.meta or {},
        }

    def _consecutive_failures(self, repo_id: uuid.UUID, conversation_id: uuid.UUID) -> int:
        # 统计"最近连续"的压缩失败次数（用于熔断）。从最近一条压缩边界之后开始数。
        boundary = MessageStore(self.db).last_compact_boundary(repo_id, conversation_id)
        query = self.db.query(ChatMessage).filter(
            ChatMessage.repo_id == repo_id,
            ChatMessage.conversation_id == conversation_id,
            ChatMessage.role == "system",
            ChatMessage.route == "context_compaction",  # 只看压缩产生的 system 消息
        )
        if boundary and boundary.created_at:
            query = query.filter(ChatMessage.created_at > boundary.created_at)  # 只看边界之后的
        count = 0
        for row in query.order_by(ChatMessage.created_at.desc()).limit(settings.context_max_compaction_failures).all():
            if (row.meta or {}).get("subtype") == "compact_failure":
                count += 1  # 失败计数 +1
            else:
                break  # 遇到非失败消息就停（只数"连续"的）
        return count

    def _record_failure(self, repo_id: uuid.UUID, conversation_id: uuid.UUID, current_message: str, stage: str, exc: Exception) -> ChatMessage:
        # 把一次压缩失败写成一条 system 消息（subtype=compact_failure），供上面统计熔断用
        failure = ChatMessage(
            repo_id=repo_id,
            conversation_id=conversation_id,
            role="system",
            content=clip_to_token_budget(f"Context compaction failed during {stage}: {exc}", 240),
            route="context_compaction",
            tool_calls=[],
            meta={
                "subtype": "compact_failure",  # 标记失败
                "stage": stage,
                "current_message": clip_to_token_budget(current_message, 120),
            },
            created_at=datetime.now(timezone.utc),
        )
        self.db.add(failure)
        return failure


class SessionSealer:
    # 会话封口器：一轮对话（用户问 + 助手答 + 工具调用）结束后，提炼记忆、封口成 ChatSession。
    def __init__(self, db: Session, llm: LLMClient | None = None) -> None:
        self.db = db
        self.llm = llm or LLMClient()

    async def seal(
        self,
        *,
        repo: Repository | None,
        conversation: Conversation | None,
        user_message: ChatMessage,
        assistant_message: ChatMessage,
        route: str,
        tool_calls: list[dict[str, Any]],
        agent_events: list[dict[str, Any]] | None = None,
        model_usage: list[dict[str, Any]] | None = None,
        context_diagnostics: dict[str, Any] | None = None,
    ) -> ChatSessionModel:
        # 封口主流程：先把原始问答落库（保证不丢），再异步抽记忆、更新线程记忆、采集候选。
        agent_events = agent_events or []
        model_usage = model_usage or []
        context_diagnostics = context_diagnostics or {}
        fallback_update = fallback_memory_update(
            # 先算一份"兜底记忆更新"（即使后面 LLM 失败也有东西用）
            user_message.content,
            assistant_message.content,
            route,
            tool_calls,
        )
        fallback_digest = str(fallback_update.get("summary") or "")  # 兜底摘要
        session = ChatSession(
            repo_id=repo.id if repo else None,
            conversation_id=conversation.id if conversation else None,
            user_message_id=user_message.id,
            assistant_message_id=assistant_message.id,
            route=route,
            transcript_json={
                # 把这一轮的完整转录存成 JSON：消息、工具调用、事件、用量、诊断、记忆更新
                "messages": [
                    {"role": "user", "content": user_message.content, "created_at": self._iso(user_message.created_at)},
                    {"role": "assistant", "content": assistant_message.content, "created_at": self._iso(assistant_message.created_at)},
                ],
                "tool_calls": tool_calls,
                "agent_events": agent_events,
                "tool_use_results": [
                    # 只挑出工具使用/结果事件
                    event for event in agent_events if event.get("type") in {"tool_use", "tool_result"}
                ],
                "model_usage": model_usage,
                "context_diagnostics": context_diagnostics,
                "memory_update": fallback_update,
                "sealing": {"phase": "raw_persisted", "errors": []},  # 标记：原始已落库
            },
            digest=fallback_digest,
            status="sealing",   # 状态：封口中
            sealed_at=None,
        )
        self.db.add(session)
        self.db.flush()  # 生成 session.id（后面采集候选要用）

        errors: list[str] = []
        memory_update = fallback_update  # 先用兜底，下面成功就覆盖
        digest = fallback_digest
        try:
            # 调 LLM 增量更新记忆（比兜底更准）
            memory_update = await self._build_memory_update(
                user_message.content,
                assistant_message.content,
                route,
                tool_calls,
                fallback_digest,
                repo_id=repo.id if repo else None,
                conversation_id=conversation.id if conversation else None,
            )
            digest = str(memory_update.pop("_digest", "") or memory_update.get("summary") or fallback_digest)
        except Exception as exc:  # LLM/网络失败绝不能让"已完成的对话轮"失效
            errors.append(f"memory_extraction_failed: {exc}")
            memory_update = fallback_update
            digest = fallback_digest

        try:
            # 用 SQLAlchemy 的嵌套事务（savepoint）：更新线程记忆 + 采集候选，要么都成要么回滚这一段
            with self.db.begin_nested():
                self._update_thread_memory(
                    repo.id if repo else None,
                    conversation.id if conversation else None,
                    digest,
                    memory_update,
                )
                capture_memory_candidates(
                    self.db,
                    repo_id=repo.id if repo else None,
                    conversation_id=conversation.id if conversation else None,
                    session_id=session.id,
                    memory_update=memory_update,
                )
                self.db.flush()
        except Exception as exc:  # 即使富化失败，原始 ChatSession 也要保留
            errors.append(f"memory_persistence_failed: {exc}")

        transcript = dict(session.transcript_json or {})  # 复制一份转录
        transcript["memory_update"] = {
            # 只保留非下划线开头的字段（_ 开头是内部标记，如 _digest）
            key: value for key, value in memory_update.items() if not key.startswith("_")
        }
        transcript["sealing"] = {
            "phase": "completed",
            "errors": errors,
            "evidence_refresh": "deferred_to_next_request",  # 证据刷新推迟到下次请求（懒更新）
        }
        session.transcript_json = transcript
        session.digest = digest
        # 有错误就标 sealed_with_warnings，否则标 sealed（前端能据此提示）
        session.status = "sealed_with_warnings" if errors else "sealed"
        session.sealed_at = datetime.now(timezone.utc)
        self.db.flush()
        return session

    async def _build_memory_update(
        self,
        user_message: str,
        assistant_message: str,
        route: str,
        tool_calls: list[dict[str, Any]],
        digest: str,
        *,
        repo_id: uuid.UUID | None,
        conversation_id: uuid.UUID | None,
    ) -> dict[str, Any]:
        # 调 LLM 生成"增量记忆更新"。返回完整快照（不是只返回本轮增量）。
        fallback = fallback_memory_update(user_message, assistant_message, route, tool_calls)
        if repo_id and conversation_id:
            # 取已有会话记忆
            current_memory: ConversationMemory | ThreadMemory | None = (
                self.db.query(ConversationMemory)
                .filter(ConversationMemory.conversation_id == conversation_id)
                .first()
            )
        else:
            # 否则取线程记忆
            current_memory = (
                self.db.query(ThreadMemory)
                .filter(_repo_filter(ThreadMemory, repo_id))
                .first()
            )
        existing_memory = self._memory_state(current_memory) if current_memory else {}  # 已有记忆状态
        # 下面是发给 LLM 的系统提示词：要求它增量更新软件工程 Agent 的会话记忆，只返回 JSON。
        # 注意：这是提示词字符串，内部保持原样、不插注释（硬性要求）。
        prompt = (
            "请增量更新软件工程 Agent 的数据库会话记忆。"
            "输入包含已有记忆和本轮原始记录；返回更新后的完整快照，而不是只返回本轮增量。"
            "只返回 JSON，包含 digest、summary、facts、decisions、open_questions、tasks、"
            "user_preferences、repo_context、citations。summary 必须描述当前工作状态和下一步；"
            "保留重要文件、函数、命令、错误修复和用户纠正，不要编造。"
        )
        parsed = await self.llm.chat_json(
            # 把提示词 + 输入（已有记忆、本轮问答、工具调用）发给 LLM，要求返回 JSON
            prompt,
            {
                "route": route,
                "digest": digest,
                "existing_memory": existing_memory,
                "user_message": user_message,
                "assistant_message": assistant_message,
                "tool_calls": tool_calls,
            },
        )
        update = structured_memory_from_model(parsed) if parsed else fallback  # 解析成标准结构，失败用兜底
        if not update.get("summary"):
            update["summary"] = fallback["summary"]  # 摘要为空时补兜底摘要
        if parsed:
            update["_update_mode"] = "snapshot"  # 标记这是"整快照"更新
            update["_digest"] = str(parsed.get("digest") or parsed.get("summary") or digest)
        return update

    def _memory_state(self, memory: ConversationMemory | ThreadMemory) -> dict[str, Any]:
        # 把记忆模型对象读成普通字典（方便发给 LLM 做增量更新）
        return {
            "summary": memory.summary or "",
            "facts": getattr(memory, "facts", None) or [],
            "decisions": memory.decisions or [],
            "open_questions": memory.open_questions or [],
            "tasks": getattr(memory, "tasks", None) or [],
            "user_preferences": getattr(memory, "user_preferences", None) or [],
            "repo_context": getattr(memory, "repo_context", None) or [],
            "citations": getattr(memory, "citations", None) or [],
        }

    def _new_memory(self, repo_id: uuid.UUID | None, conversation_id: uuid.UUID | None) -> ConversationMemory | ThreadMemory:
        # 新建一个空记忆对象（各列表字段给空），并落库。有会话建 ConversationMemory，否则 ThreadMemory。
        if repo_id and conversation_id:
            memory = ConversationMemory(
                repo_id=repo_id,
                conversation_id=conversation_id,
                summary="",
                facts=[],
                decisions=[],
                open_questions=[],
                tasks=[],
                user_preferences=[],
                repo_context=[],
                citations=[],
                sessions_incorporated=0,  # 已合并的会话数，从 0 开始
            )
        else:
            memory = ThreadMemory(
                repo_id=repo_id,
                summary="",
                facts=[],
                decisions=[],
                open_questions=[],
                tasks=[],
                user_preferences=[],
                repo_context=[],
                citations=[],
                sessions_incorporated=0,
            )
        self.db.add(memory)
        self.db.flush()
        return memory

    def _update_thread_memory(
        self,
        repo_id: uuid.UUID | None,
        conversation_id: uuid.UUID | None,
        digest: str,
        memory_update: dict[str, Any],
    ) -> ConversationMemory | ThreadMemory | None:
        # 把本轮记忆更新写回记忆对象（不存在就新建）。snapshot 模式直接覆盖，否则做合并。
        if repo_id and conversation_id:
            memory: ConversationMemory | ThreadMemory | None = self.db.query(ConversationMemory).filter(ConversationMemory.conversation_id == conversation_id).first()
        else:
            memory = self.db.query(ThreadMemory).filter(_repo_filter(ThreadMemory, repo_id)).first()
        if not memory:
            memory = self._new_memory(repo_id, conversation_id)  # 没有就建
        if memory_update.get("_update_mode") == "snapshot":
            # 整快照模式：直接用 LLM 返回的结构，summary 截到 1100 token 内
            merged = {
                **structured_memory_from_model(memory_update),
                "summary": clip_to_token_budget(str(memory_update.get("summary") or digest), 1100),
            }
        else:
            # 增量模式：把旧记忆和新增量合并
            merged = merge_memory(self._memory_state(memory), memory_update, digest, max_tokens=1100)
        # 把合并结果逐字段写回记忆对象
        memory.summary = merged["summary"]
        memory.facts = merged["facts"]
        memory.decisions = merged["decisions"]
        memory.open_questions = merged["open_questions"]
        memory.tasks = merged["tasks"]
        memory.user_preferences = merged["user_preferences"]
        memory.repo_context = merged["repo_context"]
        memory.citations = merged["citations"]
        memory.sessions_incorporated = (memory.sessions_incorporated or 0) + 1  # 合并会话数 +1
        return memory

    def _iso(self, value: datetime | None) -> str | None:
        # 把 datetime 格式化成 ISO 字符串（存 JSON 用）；为空返回 None
        return value.isoformat() if value else None


class MemoryTools:
    # 记忆工具集：封装成 Agent 可调用的工具（搜索证据 / 读原始会话 / 读封口会话）。
    def __init__(self, db: Session, repo: Repository | None, conversation: Conversation | None = None) -> None:
        self.db = db
        self.repo = repo
        self.repo_id = repo.id if repo else None
        self.conversation = conversation
        self.conversation_id = conversation.id if conversation else None

    async def search_evidence(self, input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        # 工具：搜索证据。query 兼容 "query" 或 "message" 字段。
        query = str(input_data.get("query") or input_data.get("message") or "")
        results = EvidenceStore(self.db).search(self.repo_id, self.conversation_id, query, limit=int(input_data.get("limit") or 5))
        answer = "No evidence matched this query." if not results else "\n".join(
            # 把结果拼成可读文本
            f"- [{item['source_type']}] {item['title']}: {item['snippet']}" for item in results
        )
        citations = [{"type": item["source_type"], "id": item["source_id"], "title": item["title"]} for item in results]  # 引用列表
        record_recall_event(
            # 记录一次"召回事件"（用于统计记忆/证据被用了多少次、效果好不好）
            self.db,
            repo_id=self.repo_id,
            conversation_id=self.conversation_id,
            tool_name="devflow_search_evidence",
            query=query,
            results=results,
            citations=citations,
            scope="conversation" if self.conversation_id else "project",  # 作用域：会话级 or 项目级
            mode="hybrid",  # 混合检索（向量+关键词）
            metadata={"limit": int(input_data.get("limit") or 5)},
        )
        return {
            "route": "memory_search",
            "tool_calls": [{"tool_name": "search_evidence", "arguments": {"query": query}, "status": "success"}],
            "answer": answer,
            "citations": citations,
            "results": results,
        }

    async def get_thread_context(self, input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        # 工具：读取精确原始会话消息（支持按 message_id / 关键词 / 前后游标 / 分页截取）。
        # 为什么：压缩后模型看不到完整历史，这个工具让它"按需"把某段原始消息拉回来（精确恢复）。
        keyword = str(input_data.get("keyword") or input_data.get("query") or "").strip().lower()
        limit = min(max(int(input_data.get("limit") or 30), 1), 100)  # 限制条数范围 [1,100]
        message_id = self._uuid_or_none(input_data.get("message_id"))       # 单条消息 ID
        before_message_id = self._uuid_or_none(input_data.get("before_message_id"))  # 游标：之前
        after_message_id = self._uuid_or_none(input_data.get("after_message_id"))    # 游标：之后
        offset = max(int(input_data.get("offset") or 0), 0)   # 字符偏移（长消息分段读）
        max_chars = min(max(int(input_data.get("max_chars") or 6000), 200), 16000)  # 总字符上限

        store = MessageStore(self.db)
        if message_id:
            # 指定单条：精确取这一条（排除兼容副本）
            message = (
                self.db.query(ChatMessage)
                .filter(
                    _repo_filter(ChatMessage, self.repo_id),
                    _conversation_filter(ChatMessage, self.conversation_id),
                    ChatMessage.id == message_id,
                    ChatMessage.role.in_(["user", "assistant", "tool", "tool_result"]),  # 只取这些角色
                )
                .one_or_none()
            )
            messages = [message] if message and (message.meta or {}).get("subtype") != "preserved_after_compact" else []
        else:
            # 没指定单条：走时间线，按需过滤关键词、截断到 limit
            messages = store.timeline(
                self.repo_id,
                self.conversation_id,
                limit=None,
                before_message_id=before_message_id,
                after_message_id=after_message_id,
            )
            if keyword:
                terms = _tokens(keyword)
                # 只保留内容里包含任一关键词的消息
                messages = [msg for msg in messages if any(term in msg.content.lower() for term in terms)]
            messages = messages[-limit:]  # 取最后 limit 条

        lines: list[str] = []
        results: list[dict[str, Any]] = []
        remaining_chars = max_chars  # 剩余可输出的字符预算
        for message in messages:
            content = message.content or ""
            chunk_offset = offset if message_id else 0  # 单条消息支持从 offset 起读
            chunk = content[chunk_offset : chunk_offset + remaining_chars]  # 切一段
            if not chunk and content:
                continue  # 这一条已被读完了（offset 超长）就跳过
            chunk_end = chunk_offset + len(chunk)
            truncated = chunk_end < len(content)  # 是否还有后续没读完
            created_at = message.created_at.isoformat() if message.created_at else ""
            # 每一条消息前打一个"坐标头"，方便模型知道这是哪条、读了哪段
            lines.append(
                f"[message_id={message.id} role={message.role} created_at={created_at} chars={chunk_offset}:{chunk_end}/{len(content)}]"
            )
            lines.append(chunk)
            if truncated:
                # 提示还有下文，让模型继续调（带 offset）
                lines.append(
                    f"[内容未读完；继续调用 message_id={message.id}, offset={chunk_end}]"
                )
            results.append(
                {
                    "title": f"{message.role} message",
                    "source_type": "chat_message",
                    "source_id": str(message.id),
                    "snippet": chunk,
                    "metadata": {
                        "role": message.role,
                        "created_at": created_at,
                        "offset": chunk_offset,
                        "next_offset": chunk_end if truncated else None,  # 下一页偏移
                        "total_chars": len(content),
                        "truncated": truncated,
                    },
                }
            )
            remaining_chars -= len(chunk)  # 扣掉已用字符
            if remaining_chars <= 0:
                break  # 预算用尽就停

        citations = [
            {"type": "chat_message", "id": item["source_id"], "title": item["title"]}
            for item in results
        ]
        record_recall_event(
            self.db,
            repo_id=self.repo_id,
            conversation_id=self.conversation_id,
            tool_name="devflow_get_thread_context",
            query=keyword or str(message_id or ""),
            results=results,
            citations=citations,
            scope="conversation",
            mode="exact_transcript",  # 精确原文模式
            metadata={
                "limit": limit,
                "message_id": str(message_id) if message_id else None,
                "before_message_id": str(before_message_id) if before_message_id else None,
                "after_message_id": str(after_message_id) if after_message_id else None,
                "offset": offset,
                "max_chars": max_chars,
            },
        )
        return {
            "route": "thread_context",
            "tool_calls": [{"tool_name": "get_thread_context", "arguments": {"keyword": keyword, "message_id": str(message_id or "")}, "status": "success"}],
            "answer": "\n".join(lines) if lines else "当前会话还没有匹配的消息。",
            "citations": citations,
            "results": results,
        }

    async def read_session_events(self, input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        # 工具：读取已封口 Session 的记录（摘要 + 可选完整转录），支持分页截取。
        session_id = input_data.get("session_id")
        include_transcript = bool(input_data.get("include_transcript", True))  # 是否带完整转录
        offset = max(int(input_data.get("offset") or 0), 0)
        max_chars = min(max(int(input_data.get("max_chars") or 8000), 400), 20000)  # 字符上限
        query = self.db.query(ChatSession).filter(
            _repo_filter(ChatSession, self.repo_id),
            _conversation_filter(ChatSession, self.conversation_id),
        ).order_by(ChatSession.created_at.desc())  # 最新的在前
        if session_id:
            # 指定某条 Session
            parsed_session_id = self._uuid_or_none(session_id)
            if parsed_session_id is None:
                sessions = []  # ID 非法：空结果
            else:
                sessions = query.filter(ChatSession.id == parsed_session_id).limit(1).all()
        else:
            # 不指定：取最近几条（范围 [1,20]）
            sessions = query.limit(min(max(int(input_data.get("limit") or 5), 1), 20)).all()
        lines: list[str] = []
        results: list[dict[str, Any]] = []
        remaining_chars = max_chars
        for session in sessions:
            header = f"Session {session.id} route={session.route} sealed_at={session.sealed_at}"
            payload = session.digest  # 先放摘要
            if include_transcript:
                payload += "\ntranscript=" + dumps_compact(session.transcript_json or {})  # 拼上转录
            chunk_offset = offset if session_id else 0
            chunk = payload[chunk_offset : chunk_offset + remaining_chars]
            chunk_end = chunk_offset + len(chunk)
            truncated = chunk_end < len(payload)
            lines.append(header)
            lines.append(chunk)
            if truncated:
                # 提示还有下文
                lines.append(
                    f"[会话记录未读完；继续调用 session_id={session.id}, offset={chunk_end}。]"
                )
            results.append(
                {
                    "title": f"Session {session.route}",
                    "source_type": "session",
                    "source_id": str(session.id),
                    "snippet": chunk,
                    "metadata": {
                        "sealed_at": session.sealed_at.isoformat() if session.sealed_at else None,
                        "include_transcript": include_transcript,
                        "offset": chunk_offset,
                        "next_offset": chunk_end if truncated else None,
                        "total_chars": len(payload),
                        "truncated": truncated,
                    },
                }
            )
            remaining_chars -= len(chunk)
            if remaining_chars <= 0:
                break
        citations = [
            {"type": "session", "id": item["source_id"], "title": item["title"]}
            for item in results
        ]
        record_recall_event(
            self.db,
            repo_id=self.repo_id,
            conversation_id=self.conversation_id,
            tool_name="devflow_read_session_events",
            query=str(session_id or "latest sessions"),
            results=results,
            citations=citations,
            scope="conversation",
            mode="sealed_transcript",  # 已封口转录模式
            metadata={"include_transcript": include_transcript, "offset": offset, "max_chars": max_chars},
        )
        return {
            "route": "session_events",
            "tool_calls": [{"tool_name": "read_session_events", "arguments": {"session_id": str(session_id or "")}, "status": "success"}],
            "answer": "\n\n".join(lines) if lines else "No sealed sessions found yet.",
            "citations": citations,
            "results": results,
        }

    @staticmethod
    def _uuid_or_none(value: Any) -> uuid.UUID | None:
        # 静态方法：把任意值安全地转成 UUID，转不了（非法/空）就返回 None。
        # 为什么：用户输入的 ID 可能是脏字符串，直接 uuid.UUID 会抛异常，这里兜住。
        if not value:
            return None
        try:
            return uuid.UUID(str(value))
        except (TypeError, ValueError):
            return None
