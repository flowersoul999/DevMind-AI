"""会话（Conversation）管理服务（conversations.py）。

本模块管的是"会话"这个容器：一个仓库下可以有多个会话（话题），每个会话挂很多消息、Session、记忆。
它比 chat_memory.py 更"上层"——负责会话的增删查、保证每个仓库至少有一个"默认会话"、
删除会话时连带清理它下面的所有子数据，以及把历史遗留的"无会话"数据回填到默认会话。

关键函数：
- ensure_default_conversation / ensure_conversation：拿到/创建一个活跃会话（前端每次聊天都要先有会话）。
- list_conversations / create_conversation / delete_conversation：会话的列表、新建、删除。
- touch_conversation：每次来消息时更新"会话标题/消息数/更新时间"（顺手用首条用户消息当标题）。
- _backfill_repo_conversation：兼容老数据——把没归到会话的消息/记录补挂到默认会话。

与其他模块的关系：被 API 路由和 chat_memory.py 调用；依赖 db.models 里的各表。."""


# 标准库：uuid（生成/校验唯一 ID）、datetime+timezone（UTC 时间戳）
import uuid
from datetime import datetime, timezone

# SQLAlchemy ORM 会话（所有数据库操作都通过它）
from sqlalchemy.orm import Session

# 项目数据表模型
from app.db.models import (
    AgentRun,         # 一次 Agent 运行
    AgentTaskRun,     # Agent 工作流里单个任务的运行
    AgentWorkflowRun, # Agent 工作流运行
    ChatFeedback,     # 用户对回答的反馈
    ChatMessage,      # 聊天消息
    ChatSession,      # 封口 Session
    Conversation,     # 会话容器
    ConversationMemory,  # 会话级结构化记忆
    EvidenceItem,     # 证据
    MemoryCandidate,  # 记忆候选（待审核）
    RecallEvent,      # 召回事件（记忆被用过）
    Repository,       # 仓库
)


def _default_title(db: Session, repo_id: uuid.UUID) -> str:
    # 生成默认会话标题：如果仓库还没有任何会话就用"默认会话"，否则用"新会话 N"（N 是已有会话数+1）
    existing = db.query(Conversation).filter(Conversation.repo_id == repo_id).count()
    return "默认会话" if existing == 0 else f"新会话 {existing + 1}"


def ensure_default_conversation(db: Session, repo_id: uuid.UUID) -> Conversation:
    # 保证仓库至少有一个"活跃"的默认会话：有就返回最早的那个，没有就新建一个。
    # 为什么：前端每次聊天都得挂在一个会话下，没有会话会报错，所以这里"懒创建"。
    conversation = (
        db.query(Conversation)
        .filter(Conversation.repo_id == repo_id, Conversation.status == "active")
        .order_by(Conversation.created_at.asc())  # 取最早的活跃会话
        .first()
    )
    if conversation:
        return conversation
    conversation = Conversation(repo_id=repo_id, title="默认会话", status="active")
    db.add(conversation)
    db.flush()  # 生成 ID
    return conversation


def ensure_conversation(db: Session, repo_id: uuid.UUID, conversation_id: uuid.UUID | None = None) -> Conversation:
    # 拿到指定会话；如果给了 conversation_id 且能找到"活跃"的就用它，否则退回默认会话。
    # 为什么：用户可能传了一个已删除/不存在的会话 ID，这时要优雅降级到默认会话，而不是报错。
    if conversation_id:
        conversation = (
            db.query(Conversation)
            .filter(Conversation.id == conversation_id, Conversation.repo_id == repo_id, Conversation.status == "active")
            .one_or_none()
        )
        if conversation:
            return conversation
    return ensure_default_conversation(db, repo_id)


def list_conversations(db: Session, repo_id: uuid.UUID) -> list[Conversation]:
    # 列出某仓库下所有活跃会话（最新的排前面）。先确保有默认会话、再回填老数据。
    ensure_default_conversation(db, repo_id)
    _backfill_repo_conversation(db, repo_id)
    db.flush()
    return (
        db.query(Conversation)
        .filter(Conversation.repo_id == repo_id, Conversation.status == "active")
        .order_by(Conversation.updated_at.desc(), Conversation.created_at.asc())  # 最近更新优先
        .all()
    )


def create_conversation(db: Session, repo_id: uuid.UUID, title: str | None = None) -> Conversation:
    # 新建一个会话。标题为空或只有空白时，用 _default_title 自动生成。
    conversation = Conversation(repo_id=repo_id, title=(title or "").strip() or _default_title(db, repo_id))
    db.add(conversation)
    db.flush()
    return conversation


def delete_conversation(db: Session, repo_id: uuid.UUID, conversation_id: uuid.UUID) -> Conversation:
    # 删除一个会话：把它下面的所有子数据（反馈/记忆候选/召回/工作流/消息/Session/运行/记忆/证据）一起清掉，
    # 然后把该会话标记 status="deleted"（软删除，不真删行），最后返回一个"替代会话"给前端继续用。
    conversation = (
        db.query(Conversation)
        .filter(Conversation.id == conversation_id, Conversation.repo_id == repo_id, Conversation.status == "active")
        .one_or_none()
    )
    if conversation is None:
        # 要删的会话不存在：直接返回一个默认会话（幂等，删不存在的东西也不报错）
        return ensure_default_conversation(db, repo_id)

    # 先收集这个会话下的 Session、Workflow 的 ID（下面删子表要用 IN 条件）
    session_ids = [
        item.id
        for item in db.query(ChatSession.id).filter(ChatSession.conversation_id == conversation.id).all()
    ]
    workflow_ids = [
        item.id
        for item in db.query(AgentWorkflowRun.id).filter(AgentWorkflowRun.conversation_id == conversation.id).all()
    ]

    # 逐个删子表（synchronize_session=False 表示直接走 SQL，不同步内存对象，更快）
    db.query(ChatFeedback).filter(ChatFeedback.conversation_id == conversation.id).delete(synchronize_session=False)
    db.query(MemoryCandidate).filter(MemoryCandidate.conversation_id == conversation.id).delete(synchronize_session=False)
    if session_ids:
        # 由 Session 关联的记忆候选也删（按 session_id 批量删）
        db.query(MemoryCandidate).filter(MemoryCandidate.session_id.in_(session_ids)).delete(synchronize_session=False)
    db.query(RecallEvent).filter(RecallEvent.conversation_id == conversation.id).delete(synchronize_session=False)
    if workflow_ids:
        db.query(AgentTaskRun).filter(AgentTaskRun.workflow_id.in_(workflow_ids)).delete(synchronize_session=False)
    db.query(AgentWorkflowRun).filter(AgentWorkflowRun.conversation_id == conversation.id).delete(synchronize_session=False)
    db.query(ChatMessage).filter(ChatMessage.conversation_id == conversation.id).delete(synchronize_session=False)
    db.query(ChatSession).filter(ChatSession.conversation_id == conversation.id).delete(synchronize_session=False)
    db.query(AgentRun).filter(AgentRun.conversation_id == conversation.id).delete(synchronize_session=False)
    db.query(ConversationMemory).filter(ConversationMemory.conversation_id == conversation.id).delete(synchronize_session=False)
    db.query(EvidenceItem).filter(EvidenceItem.conversation_id == conversation.id).delete(synchronize_session=False)
    conversation.status = "deleted"  # 软删除
    conversation.updated_at = datetime.now(timezone.utc)
    db.flush()

    # 找一个"活跃"会话作为替代（让前端删除后仍有可用会话）；没有就新建默认会话
    replacement = (
        db.query(Conversation)
        .filter(Conversation.repo_id == repo_id, Conversation.status == "active")
        .order_by(Conversation.updated_at.desc(), Conversation.created_at.asc())
        .first()
    )
    return replacement or create_conversation(db, repo_id, "默认会话")


def touch_conversation(
    db: Session,
    conversation: Conversation,
    *,
    user_message: str | None = None,
    increment: int = 0,
) -> None:
    # 每次来消息时"触碰"会话：可能用首条用户消息当标题（如果还叫"默认/新会话"），累加消息数，刷新更新时间。
    if user_message and conversation.message_count == 0 and conversation.title.startswith(("默认会话", "新会话")):
        # 会话还没消息且还是默认名：用首条用户消息前 40 字当标题，更直观
        title = user_message.strip().replace("\n", " ")
        if title:
            conversation.title = title[:40]
    conversation.message_count = (conversation.message_count or 0) + increment  # 累加消息数（可能为 0 即只刷新时间）
    conversation.updated_at = datetime.now(timezone.utc)  # 刷新"最后更新时间"（列表排序用）
    db.flush()


def backfill_all_default_conversations(db: Session) -> None:
    # 给所有仓库补齐默认会话并回填老数据，最后提交。用于一次性迁移/修复历史库。
    for repo in db.query(Repository).all():
        ensure_default_conversation(db, repo.id)
        _backfill_repo_conversation(db, repo.id)
    db.commit()  # 全部处理完一次性提交


def _backfill_repo_conversation(db: Session, repo_id: uuid.UUID) -> None:
    # 把"没有归到任何会话"（conversation_id 为 NULL）的历史数据补挂到默认会话。
    # 为什么：老版本可能没会话概念，数据 conversation_id 是空；这里兼容，避免它们"游离"在默认会话之外。
    conversation = ensure_default_conversation(db, repo_id)
    for model in [ChatMessage, ChatSession, AgentRun]:
        # 批量把这些表里 repo_id 匹配、conversation_id 为空的记录的 conversation_id 设为默认会话
        db.query(model).filter(model.repo_id == repo_id, model.conversation_id.is_(None)).update(
            {model.conversation_id: conversation.id},
            synchronize_session=False,
        )
    # 重新统计默认会话的消息数
    conversation.message_count = db.query(ChatMessage).filter(ChatMessage.conversation_id == conversation.id).count()
