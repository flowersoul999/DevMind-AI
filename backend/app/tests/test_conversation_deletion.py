"""DevMind AI module."""

# os：内存库环境变量（import 数据库模块前）。
import os
# uuid：唯一 id。
import uuid

# 在 import 数据库相关代码前，把 DATABASE_URL 设成内存库。
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

# SQLAlchemy：建引擎 + 连接钩子（开启外键）。
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

# 导入一堆会被级联清掉的模型，以及真实的 delete_conversation（删会话）函数。
from app.db.models import (
    AgentRun,
    AgentTaskRun,
    AgentWorkflowRun,
    Base,
    ChatFeedback,
    ChatMessage,
    ChatSession,
    Conversation,
    ConversationMemory,
    EvidenceItem,
    MemoryCandidate,
    RecallEvent,
    Repository,
)
from app.services.conversations import delete_conversation


def _db_session():
    """_db_session."""
    # 建内存引擎，并开启外键约束（否则级联删除不生效）。
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        """_enable_foreign_keys."""
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_delete_conversation_removes_session_memory_and_workflow_dependencies() -> None:
    db = _db_session()
    repo = Repository(owner="devflow", name="conversation-delete", full_name=f"devflow/{uuid.uuid4().hex}")
    db.add(repo)
    db.flush()
    # 造两个会话：target（要删的）和 replacement（保留的，用来验证"删一个不影响另一个"）。
    target = Conversation(repo_id=repo.id, title="hi，你好")
    replacement = Conversation(repo_id=repo.id, title="保留会话")
    db.add_all([target, replacement])
    db.flush()

    # 给 target 会话挂一堆子记录：一次 Agent 运行、用户/助手消息、会话、工作流。
    run = AgentRun(
        repo_id=repo.id,
        conversation_id=target.id,
        user_message="hi，你好",
        route="test",
        tool_calls=[],
        final_answer="你好",
    )
    user_message = ChatMessage(repo_id=repo.id, conversation_id=target.id, role="user", content="hi，你好")
    assistant_message = ChatMessage(repo_id=repo.id, conversation_id=target.id, role="assistant", content="你好")
    session = ChatSession(repo_id=repo.id, conversation_id=target.id, route="test", transcript_json={}, digest="你好")
    workflow = AgentWorkflowRun(repo_id=repo.id, conversation_id=target.id, goal="test")
    db.add_all([run, user_message, assistant_message, session, workflow])
    db.flush()

    # 再挂一批更细的依赖：反馈、记忆候选、召回事件、任务运行、会话记忆、证据。
    db.add_all(
        [
            ChatFeedback(
                repo_id=repo.id,
                conversation_id=target.id,
                assistant_message_id=assistant_message.id,
                run_id=run.id,
                rating="helpful",
            ),
            MemoryCandidate(
                repo_id=repo.id,
                conversation_id=target.id,
                session_id=session.id,
                kind="fact",
                title="candidate",
                content="must be deleted before the session",
            ),
            RecallEvent(repo_id=repo.id, conversation_id=target.id, tool_name="memory.search"),
            AgentTaskRun(
                workflow_id=workflow.id,
                task_id="task-1",
                agent_name="test-agent",
                task_type="test",
            ),
            ConversationMemory(repo_id=repo.id, conversation_id=target.id),
            EvidenceItem(
                repo_id=repo.id,
                conversation_id=target.id,
                source_type="test",
                source_id="conversation-delete",
                title="evidence",
                content="delete regression",
            ),
        ]
    )
    db.commit()

    # 删除 target 会话。
    active = delete_conversation(db, repo.id, target.id)
    db.commit()

    # 断言：返回"当前激活会话"是 replacement（删掉 target 后，系统把激活指针切到了另一个会话）。
    assert active.id == replacement.id
    # 断言：target 本身只是被标记为 deleted（软删除，status 变 deleted），而不是从表里物理消失。
    assert db.get(Conversation, target.id).status == "deleted"
    # 遍历所有依赖模型，断言它们对该 repo 的记录都被清空了（没有孤儿数据残留）。
    for model in [
        ChatFeedback,
        MemoryCandidate,
        RecallEvent,
        AgentTaskRun,
        AgentWorkflowRun,
        ChatMessage,
        ChatSession,
        AgentRun,
        ConversationMemory,
        EvidenceItem,
    ]:
        assert db.query(model).count() == 0
