"""DevMind AI module."""

# os：设置内存库环境变量（import 数据库模块前）。
import os
# uuid：唯一 id。
import uuid

# 在 import 数据库相关代码前，把 DATABASE_URL 设成内存库。
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

# pytest。
import pytest
# SQLAlchemy：create_engine 建引擎，event 用来挂"连接建立时"的钩子。
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

# 导入 repos 路由里的 delete_repo，以及一堆会被级联删除的模型。
from app.api.routes import repos as repo_routes
from app.db.models import (
    AgentRun,
    AgentTaskRun,
    AgentWorkflowRun,
    Base,
    ChatMessage,
    ChatSession,
    Conversation,
    EvidenceItem,
    KnowledgeGraphEdge,
    MemoryCandidate,
    ProjectIndexState,
    RecallEvent,
    Repository,
    Workspace,
)


def _db_session():
    """_db_session."""
    # 建内存引擎。
    engine = create_engine("sqlite+pysqlite:///:memory:")

    # event.listens_for：给引擎挂一个"每次新建连接"的监听器。
    # 为什么：SQLite 默认不开启外键约束，级联删除（ON DELETE CASCADE）必须显式 PRAGMA foreign_keys=ON 才生效；
    #       这里在每次连接时打开它，确保"删仓库会带着把相关记录一起删掉"这条行为能被测到。
    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        """_enable_foreign_keys."""
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    # 建表。
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


@pytest.mark.asyncio
async def test_delete_repo_removes_all_conversation_and_workflow_dependencies(monkeypatch) -> None:
    db = _db_session()
    # 造一个待删除的仓库。
    repo = Repository(owner="devflow", name="delete-me", full_name=f"devflow/delete-{uuid.uuid4().hex[:8]}")
    db.add(repo)
    db.flush()

    # 一个工作区（包含这个仓库），以及一条会话。
    workspace = Workspace(name="deletion-test", repo_ids=[str(repo.id)])
    conversation = Conversation(repo_id=repo.id, title="delete regression")
    db.add_all([workspace, conversation])
    db.flush()

    # 会话记录 + 一个 Agent 工作流运行（都挂在这条会话上）。
    session = ChatSession(
        repo_id=None,
        conversation_id=conversation.id,
        route="test",
        transcript_json={},
        digest="delete regression",
    )
    workflow = AgentWorkflowRun(
        repo_id=None,
        conversation_id=conversation.id,
        goal="delete regression",
    )
    db.add_all([session, workflow])
    db.flush()

    # 造一大批"依赖这个仓库/会话"的子记录：记忆候选、召回事件、任务运行、Agent 运行、聊天消息、证据、知识图谱边、项目索引状态。
    db.add_all(
        [
            MemoryCandidate(
                repo_id=None,
                conversation_id=conversation.id,
                session_id=session.id,
                kind="fact",
                title="candidate",
                content="must be deleted before the session",
            ),
            RecallEvent(
                repo_id=None,
                conversation_id=conversation.id,
                tool_name="memory.search",
            ),
            AgentTaskRun(
                workflow_id=workflow.id,
                task_id="task-1",
                agent_name="test-agent",
                task_type="test",
            ),
            AgentRun(
                repo_id=None,
                conversation_id=conversation.id,
                user_message="delete",
                route="test",
                tool_calls=[],
                final_answer="deleted",
            ),
            ChatMessage(
                repo_id=None,
                conversation_id=conversation.id,
                role="user",
                content="delete this project",
            ),
            EvidenceItem(
                repo_id=None,
                conversation_id=conversation.id,
                source_type="test",
                source_id="delete-test",
                title="evidence",
                content="delete regression",
            ),
            KnowledgeGraphEdge(
                repo_id=repo.id,
                from_type="issue",
                from_id="1",
                to_type="pull_request",
                to_id="2",
                relation="related",
            ),
            ProjectIndexState(repo_id=repo.id),
        ]
    )
    db.commit()

    # 记录 delete_milvus_documents 被调用时的参数（验证删仓库会同步清理向量库）。
    vector_calls = []

    def fake_delete_milvus_documents(**kwargs):
        """fake_delete_milvus_documents."""
        vector_calls.append(kwargs)
        return True

    monkeypatch.setattr(repo_routes, "delete_milvus_documents", fake_delete_milvus_documents)

    # 真正执行删除仓库。
    result = await repo_routes.delete_repo(repo.id, db)

    # 断言：返回 deleted=True，且 milvus 清理标记为 True。
    assert result["deleted"] is True
    assert result["cleanup"]["milvus_deleted"] is True
    # 断言：向量库删除被精确调用了一次，参数是这个 repo_id。
    assert vector_calls == [{"repo_id": repo.id, "strict": False}]
    # 断言：仓库本身没了。
    assert db.get(Repository, repo.id) is None
    # 断言：所有依赖记录都被级联清空——没有留下孤儿数据。
    assert db.query(Conversation).count() == 0
    assert db.query(ChatSession).count() == 0
    assert db.query(MemoryCandidate).count() == 0
    assert db.query(RecallEvent).count() == 0
    assert db.query(AgentWorkflowRun).count() == 0
    assert db.query(AgentTaskRun).count() == 0
    assert db.query(AgentRun).count() == 0
    assert db.query(ChatMessage).count() == 0
    assert db.query(EvidenceItem).count() == 0
    assert db.query(KnowledgeGraphEdge).count() == 0
    assert db.query(ProjectIndexState).count() == 0
    # 断言：工作区里已经不再包含被删的仓库（repo_ids 变空）。
    assert db.get(Workspace, workspace.id).repo_ids == []
