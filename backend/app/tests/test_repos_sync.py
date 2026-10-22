"""DevMind AI module."""

# os：设置环境变量（内存数据库），必须在 import 数据库模块前。
import os

# 在导入数据库相关代码前，把 DATABASE_URL 设成内存库。
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

# pytest。
import pytest
# SQLAlchemy。
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# 导入模型、同步请求 schema、以及真实的 sync_repository（仓库同步）函数。
from app.db.models import Base, Document, Issue, Repository
from app.schemas.repos import RepoSyncRequest
from app.services.repos_sync import sync_repository


def _db_session():
    """_db_session."""
    # 建内存库 + 建表 + 出会话。
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


@pytest.mark.asyncio
async def test_sync_repository_persists_facts_without_triggering_rag(monkeypatch) -> None:
    # 假 list_issues：返回一条真实样式的 issue（编号 78、标题、open 状态等）。
    async def fake_list_issues(*_args, **_kwargs):
        """fake_list_issues."""
        return [
            {
                "id": 7800,
                "number": 78,
                "title": "Error: Could not import 'app'.",
                "body": "Realistic issue body",
                "state": "open",
                "labels": [{"name": "bug"}],
                "user": {"login": "contributor"},
                "assignees": [],
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-02T00:00:00Z",
                "closed_at": None,
            }
        ]

    # 一个"哨兵"函数：一旦被调用就抛 AssertionError，用来证明"某段代码没被走到"。
    def fail_if_vector_runtime_is_used(*_args, **_kwargs):
        """fail_if_vector_runtime_is_used."""
        raise AssertionError("GitHub fact sync must not depend on embeddings or Milvus")

    # 把真实的 list_issues 换成假数据。
    monkeypatch.setattr("app.services.repos_sync.list_issues", fake_list_issues)
    # 把向量库相关的三个函数（嵌入、删、插）全换成哨兵——如果同步逻辑误触发了它们，测试立即失败。
    monkeypatch.setattr("app.services.rag.vector_store.embed_texts", fail_if_vector_runtime_is_used)
    monkeypatch.setattr("app.services.rag.vector_store.delete_milvus_documents", fail_if_vector_runtime_is_used)
    monkeypatch.setattr("app.services.rag.vector_store.upsert_milvus_documents", fail_if_vector_runtime_is_used)

    db = _db_session()
    # 造一个 GitHub 仓库记录并落库。
    repo = Repository(
        owner="openai",
        name="openai-quickstart-python",
        full_name="openai/openai-quickstart-python",
        provider="github",
        api_base_url="https://api.github.com",
    )
    db.add(repo)
    db.commit()

    # 执行同步：只同步 issue（PR / workflow_runs 都关掉），limit=1。
    result = await sync_repository(
        db,
        repo,
        RepoSyncRequest(sync_pull_requests=False, sync_workflow_runs=False, limit=1),
    )

    # 从库里把这条 issue 查出来。
    issue = db.query(Issue).filter(Issue.repo_id == repo.id).one()
    # 断言：同步状态 completed。
    assert result.status == "completed"
    # 断言：同步计数正确（1 个 issue，0 个 PR，0 个 workflow）。
    assert result.synced == {"issues": 1, "pull_requests": 0, "workflow_runs": 0}
    # 断言：issue 的编号、标题都被正确落库。
    assert issue.number == 78
    assert issue.title == "Error: Could not import 'app'."
    # 断言：没有生成任何 Document 行——同步事实（issue）与 RAG 建索引是解耦的，同步阶段不碰向量库/文档。
    # 为什么：事实同步是廉价、确定的；RAG 索引是另一环节。把两者解耦，同步接口即使没有向量库也能跑，职责更清晰。
    assert db.query(Document).filter(Document.repo_id == repo.id).count() == 0
