"""DevMind AI module."""

# os：内存库环境变量（import 数据库模块前）。
import os
# uuid：唯一 id。
import uuid

# 在 import 数据库相关代码前，把 DATABASE_URL 设成内存库。
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

# pytest。
import pytest
# SQLAlchemy。
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# 导入配置、一堆模型、记忆/索引/知识库/qa/检索/策略/向量库相关真实模块。
from app.core.config import settings
from app.db.models import (
    Base,
    ChatMessage,
    ChatSession,
    Conversation,
    ConversationMemory,
    Document,
    EvidenceItem,
    Issue,
    KnowledgeBaseConfig,
    PRFile,
    PRReviewComment,
    PullRequest,
    Repository,
    WorkflowRun,
)
from app.services.chat_memory import ContextAssembler, EvidenceStore, SessionSealer
from app.services.rag.indexing import CI_LOG_CHUNK_CHARS, index_repository_documents
from app.services.rag.knowledge_base import get_or_create_config, retrieve_knowledge
from app.services.rag.qa import rag_status, reindex_documents
from app.services.rag.retrieval import search_similar_documents
from app.services.rag.source_policy import retrieval_mode, should_vectorize
from app.services.rag.vector_store import DocumentPayload, MilvusUnavailableError, add_documents, reset_milvus_store_cache


def _db_session():
    """_db_session."""
    # 建内存库 + 建表 + 出会话。
    engine = create_engine("sqlite+pymlite:///:memory:") if False else create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _repo(db):
    """_repo."""
    # 造一个仓库并 flush。
    repo = Repository(owner="local", name="policy", full_name="local/policy")
    db.add(repo)
    db.flush()
    return repo


def _vector() -> list[float]:
    # 造一个维度等于配置的"全 0 向量、第一维是 1"——用来模拟嵌入结果（可预测）。
    return [1.0, *([0.0] * (settings.embedding_dimensions - 1))]


# 假 Milvus 存储：记录被 upsert 进来的 (文档, 向量) 对。
# 为什么：不必连真 Milvus，就能断言"哪些文档被向量化入库了"。
class _FakeMilvusStore:
    def __init__(self) -> None:
        self.collection_name = "test_collection"
        self.upserts: list[tuple[list[Document], list[list[float]]]] = []

    def upsert_documents(self, documents, vectors) -> None:
        self.upserts.append((list(documents), list(vectors)))

    def delete_documents(self, **_kwargs) -> None:
        return None


def _install_fake_milvus(monkeypatch) -> _FakeMilvusStore:
    # 把全局的 _milvus_store 工厂替换成"永远返回这个假 store"。
    store = _FakeMilvusStore()
    monkeypatch.setattr("app.services.rag.vector_store._milvus_store", lambda: store)
    return store


# 参数化测试：逐一验证"每种 source_type 该走 rag 还是 direct 模式"。
@pytest.mark.parametrize(
    ("source_type", "expected"),
    [
        ("issue", "rag"),
        ("pull_request", "rag"),
        ("workflow_run", "rag"),
        ("project_doc", "rag"),
        ("knowledge_file", "rag"),
        ("memory_note", "rag"),
        ("code_file", "direct"),
        ("project_overview", "direct"),
        ("project_manifest", "direct"),
        ("team_member", "direct"),
        ("weekly_report", "direct"),
        ("chat_session", "direct"),
        ("conversation_memory", "direct"),
        ("thread_memory", "direct"),
        ("future_unknown_type", "direct"),
    ],
)
def test_source_policy_separates_rag_and_direct_data(source_type, expected) -> None:
    # 断言：retrieval_mode 返回期望的模式；should_vectorize 在 rag 时为 True（direct 不进向量库）。
    assert retrieval_mode(source_type) == expected
    assert should_vectorize(source_type) is (expected == "rag")


def test_knowledge_base_defaults_enable_high_score_threshold() -> None:
    db = _db_session()
    repo = _repo(db)

    # 取（没有就建）知识库配置。
    config = get_or_create_config(db, repo.id)

    # 断言：默认开启分数阈值，且阈值是 0.8 左右（高分才进 RAG，低分噪声丢弃）。
    assert config.score_threshold_enabled is True
    assert config.score_threshold == pytest.approx(0.8)


@pytest.mark.asyncio
async def test_default_score_threshold_filters_results_below_point_eight(monkeypatch) -> None:
    db = _db_session()
    repo = _repo(db)
    # 把底层检索替换成：返回一条 0.81（高）和一条 0.79（低）。
    monkeypatch.setattr(
        "app.services.rag.knowledge_base.search_similar_documents",
        lambda *_args, **_kwargs: [
            {"id": "high-confidence", "score": 0.81},
            {"id": "low-confidence", "score": 0.79},
        ],
    )

    # 走 retrieve_knowledge（带默认阈值过滤）。
    items, trace, _duration_ms = await retrieve_knowledge(
        db,
        repo.id,
        "authentication token",
        retrieval_method="fulltext",
        rerank_enabled=False,
    )

    # 断言：低于 0.8 的那条被过滤掉，只剩 high-confidence；trace 里记录了阈值开启且为 0.8。
    assert [item["id"] for item in items] == ["high-confidence"]
    assert trace["score_threshold_enabled"] is True
    assert trace["score_threshold"] == pytest.approx(0.8)


def test_add_documents_only_embeds_rag_sources(monkeypatch) -> None:
    # 装假 Milvus + 假 embed（返回 _vector）。
    store = _install_fake_milvus(monkeypatch)
    monkeypatch.setattr(
        "app.services.rag.vector_store.embed_texts",
        lambda texts, dimensions: [_vector() for _text in texts],
    )
    db = _db_session()
    repo = _repo(db)

    # 加三种文档：knowledge_file（rag）、team_member（direct）、chat_session（direct）。
    docs = add_documents(
        db,
        repo.id,
        [
            DocumentPayload(source_type="knowledge_file", title="Runbook", content="Rotate the token."),
            DocumentPayload(source_type="team_member", title="Alice", content="Backend engineer"),
            DocumentPayload(source_type="chat_session", title="Session", content="Raw transcript"),
        ],
    )

    by_type = {doc.source_type: doc for doc in docs}
    # 断言：knowledge_file 标记为 rag 模式、team_member 标记为 direct 模式；rag 文档没有 embedding 字段（走 direct 落库、不内嵌向量）。
    assert by_type["knowledge_file"].meta["retrieval_mode"] == "rag"
    assert by_type["team_member"].meta["retrieval_mode"] == "direct"
    assert not hasattr(by_type["knowledge_file"], "embedding")
    # 断言：只有 1 次 upsert，且只把 knowledge_file 送进了向量库（direct 类不进向量库）。
    assert len(store.upserts) == 1
    assert [doc.source_type for doc in store.upserts[0][0]] == ["knowledge_file"]
    assert store.upserts[0][1] == [_vector()]


def test_add_documents_rejects_source_code_documents(monkeypatch) -> None:
    # 关 Milvus + 清缓存。
    monkeypatch.setattr(settings, "milvus_enabled", False)
    reset_milvus_store_cache()
    db = _db_session()
    repo = _repo(db)

    # 试图把 code_file 加进 RAG 文档库——应该被拒（代码走实时工作区搜索，不该进知识库）。
    with pytest.raises(ValueError, match="工作区实时搜索"):
        add_documents(
            db,
            repo.id,
            [DocumentPayload(source_type="code_file", title="auth.py", content="def rotate_token(): pass")],
        )


def test_add_documents_rejects_partial_embedding_batches(monkeypatch) -> None:
    monkeypatch.setattr(settings, "milvus_enabled", False)
    reset_milvus_store_cache()
    # 假 embed 返回空列表（模拟"嵌入失败/数量对不上"）。
    monkeypatch.setattr("app.services.rag.vector_store.embed_texts", lambda _texts, _dimensions: [])
    db = _db_session()
    repo = _repo(db)

    # 加 1 个 raq 文档，但 embed 返回 0 个向量——应当报错。
    with pytest.raises(ValueError, match="embedding count"):
        add_documents(
            db,
            repo.id,
            [DocumentPayload(source_type="knowledge_file", title="Runbook", content="Rotate the token.")],
        )

    # 断言：出错后数据库里没有任何 Document 被写进去（原子性：要么全成要么全不成）。
    assert db.query(Document).count() == 0


def test_general_rag_search_excludes_code_and_direct_records(monkeypatch) -> None:
    monkeypatch.setattr(settings, "milvus_enabled", False)
    reset_milvus_store_cache()
    db = _db_session()
    repo = _repo(db)
    # 加一个 knowledge_file（rag）和一个 team_member（direct），不同步向量库。
    add_documents(
        db,
        repo.id,
        [
            DocumentPayload(
                source_type="knowledge_file",
                title="Authentication runbook",
                content="Rotate the access token after authentication failure.",
            ),
            DocumentPayload(
                source_type="team_member",
                title="Token owner",
                content="Maintains authentication token services.",
            ),
        ],
        vectors=[_vector(), _vector()],
        sync_vector_store=False,
    )
    # 让 milvus 检索返回空（关了就用库内全文）。
    monkeypatch.setattr("app.services.rag.retrieval.search_milvus_documents", lambda *_args, **_kwargs: [])

    # 通用检索（不限 source_type）。
    general = search_similar_documents(
        db,
        repo.id,
        "authentication token",
        retrieval_method="fulltext",
        apply_rerank=False,
    )
    # 指定只搜 code_file。
    code = search_similar_documents(
        db,
        repo.id,
        "rotate_access_token",
        source_type="code_file",
        retrieval_method="hybrid",
        apply_rerank=False,
    )
    # 指定只搜 team_member。
    team = search_similar_documents(
        db,
        repo.id,
        "token",
        source_type="team_member",
        retrieval_method="fulltext",
        apply_rerank=False,
    )
    # code_file + vector 方式检索。
    code_with_vector_method = search_similar_documents(
        db,
        repo.id,
        "rotate_access_token",
        source_type="code_file",
        retrieval_method="vector",
        apply_rerank=False,
    )
    # 混合检索 knowledge_file + code_file。
    mixed = search_similar_documents(
        db,
        repo.id,
        "rotate_access_token",
        source_types=["knowledge_file", "code_file"],
        retrieval_method="hybrid",
        limit=2,
        apply_rerank=False,
    )

    # 断言：通用检索只返回 knowledge_file（rag 数据）；code/team 检索都返回空（direct/code 数据不该出现在通用 RAG 里）。
    assert {item["source_type"] for item in general} == {"knowledge_file"}
    assert code == []
    assert code_with_vector_method == []
    # 断言：混合检索结果里最多只有 knowledge_file（code_file 被排除）。
    assert {item["source_type"] for item in mixed} <= {"knowledge_file"}
    assert team == []


def test_vector_search_does_not_fall_back_to_database(monkeypatch) -> None:
    monkeypatch.setattr(settings, "milvus_enabled", False)
    reset_milvus_store_cache()
    db = _db_session()
    repo = _repo(db)
    # 库里有一条 knowledge_file 文档（带 rag 标记）。
    db.add(
        Document(
            repo_id=repo.id,
            source_type="knowledge_file",
            source_id=uuid.uuid4(),
            title="Authentication runbook",
            content="Rotate the access token.",
            meta={"retrieval_mode": "rag"},
        )
    )
    db.commit()

    # 用 vector 方式检索，但 Milvus 被禁用——应当直接抛 MilvusUnavailableError，而不是偷偷回退到数据库检索。
    with pytest.raises(MilvusUnavailableError, match="disabled"):
        search_similar_documents(
            db,
            repo.id,
            "authentication token",
            retrieval_method="vector",
            apply_rerank=False,
        )


def test_hybrid_search_falls_back_to_fulltext_when_milvus_is_disabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "milvus_enabled", False)
    reset_milvus_store_cache()
    db = _db_session()
    repo = _repo(db)
    db.add(
        Document(
            repo_id=repo.id,
            source_type="knowledge_file",
            source_id=uuid.uuid4(),
            title="Authentication runbook",
            content="Rotate the access token after authentication failure.",
            meta={"retrieval_mode": "rag"},
        )
    )
    db.commit()

    # hybrid 检索在 Milvus 禁用时，应优雅回退到 fulltext（而不是报错）。
    results = search_similar_documents(
        db,
        repo.id,
        "authentication token",
        retrieval_method="hybrid",
        apply_rerank=False,
    )

    # 断言：回退后仍能搜到那篇文档。
    assert [item["title"] for item in results] == ["Authentication runbook"]


@pytest.mark.asyncio
async def test_code_file_retrieval_is_rejected_without_query_embedding(monkeypatch) -> None:
    monkeypatch.setattr(settings, "milvus_enabled", False)
    reset_milvus_store_cache()
    # 关键哨兵：如果代码检索去"嵌入查询"了，直接让测试失败——代码检索用关键词，不该嵌向量。
    monkeypatch.setattr(
        "app.services.rag.knowledge_base.embed_knowledge_query",
        lambda *_args, **_kwargs: pytest.fail("code retrieval must not embed the query"),
    )
    db = _db_session()
    repo = _repo(db)
    # 检索 code_file，且把传入的 source_types 里混入空格（" code_file "）验证会先被规整。
    items, trace, _duration_ms = await retrieve_knowledge(
        db,
        repo.id,
        "rotate_access_token",
        source_types=[" code_file ", "code_file"],
        rerank_enabled=False,
    )

    # 断言：code_file 不进 RAG，结果空；且 trace 里 source_types 被规整成纯 ["code_file"]。
    assert items == []
    assert trace["source_types"] == ["code_file"]


def test_reindex_uses_the_global_embedding_contract(monkeypatch) -> None:
    store = _install_fake_milvus(monkeypatch)
    # 哨兵：如果 reindex 用了"全局 embed_texts"预嵌入，直接失败——reindex 必须显式带 provider/model。
    monkeypatch.setattr(
        "app.services.rag.vector_store.embed_texts",
        lambda *_args, **_kwargs: pytest.fail("repository refresh must not pre-embed with global settings"),
    )
    calls: list[tuple[str, str, int, int]] = []

    # 假 embed：记录 (provider, model, dimensions, 文本数)，返回 _vector 列表。
    def fake_embed(texts, *, provider, model, dimensions):
        """fake_embed."""
        """fake_embed."""
        calls.append((provider, model, dimensions, len(texts)))
        return [_vector() for _text in texts]

    monkeypatch.setattr("app.services.rag.qa.embed_knowledge_texts", fake_embed)
    db = _db_session()
    repo = _repo(db)
    # 建配置（用全局 embedding 设置）+ 一条 issue。
    db.add(
        KnowledgeBaseConfig(
            repo_id=repo.id,
            embedding_provider=settings.embedding_provider,
            embedding_model=settings.embedding_model,
            embedding_dimensions=settings.embedding_dimensions,
        )
    )
    db.add(Issue(repo_id=repo.id, number=3, title="Token expiry", body="Refresh once.", state="open"))
    db.commit()

    indexed = reindex_documents(db, repo.id)

    # 断言：索引了 1 条；embed 用到的就是全局 provider/model/dimensions；且只 upsert 了 1 次（遵守契约）。
    assert indexed == 1
    assert calls == [(settings.embedding_provider, settings.embedding_model, settings.embedding_dimensions, 1)]
    assert len(store.upserts) == 1


def test_reindex_rejects_partial_embedding_batches(monkeypatch) -> None:
    monkeypatch.setattr(settings, "milvus_enabled", False)
    reset_milvus_store_cache()
    db = _db_session()
    repo = _repo(db)
    db.add(
        Document(
            repo_id=repo.id,
            source_type="memory_note",
            source_id=uuid.uuid4(),
            title="Decision",
            content="Use short-lived tokens.",
            meta={},
        )
    )
    db.commit()
    # 假 embed 返回空列表（向量数不符）。
    monkeypatch.setattr("app.services.rag.qa.embed_texts", lambda _texts, _dimensions: [])

    # reindex 时向量数量对不上，应当报 "embedding count" 错误。
    with pytest.raises(ValueError, match="embedding count"):
        reindex_documents(db, repo.id)


def test_reindex_clears_legacy_metadata_and_status_only_counts_rag(monkeypatch) -> None:
    _install_fake_milvus(monkeypatch)
    monkeypatch.setattr(
        "app.services.rag.vector_store.embed_texts",
        lambda texts, dimensions: [_vector() for _text in texts],
    )
    monkeypatch.setattr(
        "app.services.rag.qa.embed_texts",
        lambda texts, dimensions: [_vector() for _text in texts],
    )
    db = _db_session()
    repo = _repo(db)
    # 加两个 rag 文档：memory_note 和 weekly_report。
    docs = add_documents(
        db,
        repo.id,
        [
            DocumentPayload(source_type="memory_note", title="Decision", content="Use short-lived tokens."),
            DocumentPayload(source_type="weekly_report", title="Weekly", content="Two pull requests merged."),
        ],
    )
    # 给 weekly 文档手动塞一套"旧的 legacy 嵌入元数据"。
    weekly = next(doc for doc in docs if doc.source_type == "weekly_report")
    weekly.meta = {
        **(weekly.meta or {}),
        "embedding_provider": "legacy",
        "embedding_model": "legacy",
        "embedding_dimensions": 2,
    }
    # 再塞几条"老的 direct/code 类文档"（含敏感 patch）。
    db.add_all(
        [
            Document(
                repo_id=repo.id,
                source_type="chat_session",
                source_id=uuid.uuid4(),
                title="Legacy raw chat",
                content="A private raw conversation transcript.",
                meta={},
            ),
            Document(
                repo_id=repo.id,
                source_type="pull_request",
                source_id=uuid.uuid4(),
                title="Legacy PR",
                content="SENSITIVE_PATCH_FROM_OLD_INDEX",
                meta={},
            ),
            Document(
                repo_id=repo.id,
                source_type="workflow_run",
                source_id=uuid.uuid4(),
                title="Legacy passing run",
                content="all tests passed",
                meta={"conclusion": "success"},
            ),
            Document(
                repo_id=repo.id,
                source_type="code_file",
                source_id=uuid.uuid4(),
                title="Legacy source chunk",
                content="def stale_handler(): pass",
                meta={"retrieval_mode": "lexical", "path": "service.py"},
            ),
        ]
    )
    db.commit()

    indexed = reindex_documents(db, repo.id)
    db.refresh(weekly)
    status = rag_status(db, repo.id)

    # 断言：只有 1 个 rag 文档被重新索引（weekly 是 direct 模式、不进 RAG 索引计数）；
    #       weekly 没有 embedding 字段、模式改为 direct、旧的 legacy 嵌入元数据被清掉。
    assert indexed == 1
    assert not hasattr(weekly, "embedding")
    assert weekly.meta["retrieval_mode"] == "direct"
    assert "embedding_provider" not in weekly.meta
    # 断言：rag_status 只统计 1 个文档、1 个 memory_note source_type；
    #       而那几条 legacy 的 chat_session / pull_request / workflow_run / code_file 文档都被清掉了（重索引只保留规范来源）。
    assert status["document_count"] == 1
    assert status["source_types"] == {"memory_note": 1}
    assert db.query(Document).filter(Document.repo_id == repo.id).count() == 2
    assert db.query(Document).filter(Document.title == "Legacy raw chat").count() == 0
    assert db.query(Document).filter(Document.content == "SENSITIVE_PATCH_FROM_OLD_INDEX").count() == 0
    assert db.query(Document).filter(Document.title == "Legacy passing run").count() == 0
    assert db.query(Document).filter(Document.title == "Legacy source chunk").count() == 0


def test_repository_index_only_embeds_unstructured_historical_fields(monkeypatch) -> None:
    _install_fake_milvus(monkeypatch)
    monkeypatch.setattr(
        "app.services.rag.vector_store.embed_texts",
        lambda texts, dimensions: [_vector() for _text in texts],
    )
    db = _db_session()
    repo = _repo(db)
    # 造 issue（含 state/labels 等结构化字段）、PR（含敏感 patch + 评审评论）、两条 workflow_run（成功/失败）。
    db.add(
        Issue(
            repo_id=repo.id,
            number=7,
            title="Authentication occasionally fails",
            body="Users see an expired-session message.",
            state="open",
            labels=["backend"],
            author="alice",
            assignees=["bob"],
        )
    )
    db.add(
        PullRequest(
            repo_id=repo.id,
            number=9,
            title="Retry token refresh",
            body="Retry once after refresh.",
            state="open",
            author="carol",
            base_branch="main",
            head_branch="fix/retry",
            files=[
                PRFile(
                    filename="auth.py",
                    status="modified",
                    additions=4,
                    deletions=1,
                    patch="SENSITIVE_PATCH_SHOULD_NOT_BE_EMBEDDED",
                )
            ],
            review_comments=[
                PRReviewComment(
                    body="Keep the retry idempotent.",
                    path="auth.py",
                    line=42,
                    author="reviewer",
                )
            ],
        )
    )
    db.add_all(
        [
            WorkflowRun(
                repo_id=repo.id,
                name="passing build",
                status="completed",
                conclusion="success",
                logs_text="all tests passed",
            ),
            WorkflowRun(
                repo_id=repo.id,
                name="failing build",
                status="completed",
                conclusion="failure",
                logs_text="test_refresh_retry failed with 401",
            ),
        ]
    )
    db.flush()

    indexed = index_repository_documents(db, repo.id)
    docs = db.query(Document).filter(Document.repo_id == repo.id).all()
    by_type = {doc.source_type: doc for doc in docs}

    # 断言：索引了 3 个非结构化历史文档（issue / PR / 其中一条 workflow_run）。
    assert indexed == 3
    # 断言：issue 的正文里不含结构化的 "state:" / "labels:" 前缀（这些被抽到 meta 里，而不塞进待嵌入文本）。
    assert "state:" not in by_type["issue"].content
    assert "labels:" not in by_type["issue"].content
    assert by_type["issue"].meta["state"] == "open"
    # 断言：PR 的待嵌入正文里不含敏感 patch，但保留了评审评论；meta.files 记录了改过的文件。
    assert "SENSITIVE_PATCH_SHOULD_NOT_BE_EMBEDDED" not in by_type["pull_request"].content
    assert "Keep the retry idempotent." in by_type["pull_request"].content
    assert by_type["pull_request"].meta["files"] == ["auth.py"]
    # 断言：只有失败的 workflow_run 被索引成文档（成功的"all tests passed"被认为无信息量，跳过）。
    workflow_docs = [doc for doc in docs if doc.source_type == "workflow_run"]
    assert len(workflow_docs) == 1
    assert "test_refresh_retry failed with 401" in workflow_docs[0].content


def test_repository_sync_respects_global_embedding_contract(monkeypatch) -> None:
    store = _install_fake_milvus(monkeypatch)
    # 哨兵：同步若用全局 embed_texts 直接失败——必须用显式 provider/model。
    monkeypatch.setattr(
        "app.services.rag.vector_store.embed_texts",
        lambda *_args, **_kwargs: pytest.fail("sync must not use the global embedding provider"),
    )
    calls: list[tuple[str, str, int]] = []

    def fake_embed(texts, *, provider, model, dimensions):
        calls.append((provider, model, dimensions))
        return [_vector() for _text in texts]

    monkeypatch.setattr("app.services.rag.indexing.embed_knowledge_texts", fake_embed)
    db = _db_session()
    repo = _repo(db)
    db.add(
        KnowledgeBaseConfig(
            repo_id=repo.id,
            embedding_provider=settings.embedding_provider,
            embedding_model=settings.embedding_model,
            embedding_dimensions=settings.embedding_dimensions,
        )
    )
    db.add(Issue(repo_id=repo.id, number=5, title="Token expiry", body="Refresh once.", state="open"))
    db.flush()

    indexed = index_repository_documents(db, repo.id)
    doc = db.query(Document).filter(Document.repo_id == repo.id, Document.source_type == "issue").one()

    # 断言：索引 1 条；embed 用全局 provider/model/dimensions；文档无内嵌 embedding 字段（向量单独进库），meta 记录了 provider；upsert 1 次。
    assert indexed == 1
    assert calls == [(settings.embedding_provider, settings.embedding_model, settings.embedding_dimensions)]
    assert not hasattr(doc, "embedding")
    assert doc.meta["embedding_provider"] == settings.embedding_provider
    assert len(store.upserts) == 1


def test_failed_ci_logs_are_redacted_and_bounded_before_embedding(monkeypatch) -> None:
    _install_fake_milvus(monkeypatch)
    monkeypatch.setattr(
        "app.services.rag.vector_store.embed_texts",
        lambda texts, dimensions: [_vector() for _text in texts],
    )
    db = _db_session()
    repo = _repo(db)
    # 一条失败 CI 日志：含 GitHub token 秘密 + 1200 行失败上下文。
    secret = "ghp_" + "a" * 32
    db.add(
        WorkflowRun(
            repo_id=repo.id,
            name="failing build",
            status="completed",
            conclusion="failure",
            logs_text=f"Authorization: Bearer {secret}\nTOKEN={secret}\n" + ("failure context\n" * 1200),
        )
    )
    db.flush()

    index_repository_documents(db, repo.id)
    docs = (
        db.query(Document)
        .filter(Document.repo_id == repo.id, Document.source_type == "workflow_run")
        .order_by(Document.title.asc())
        .all()
    )

    # 断言：索引出多条文档（超长日志被切片）；每条都不含原始 secret、都含 [REDACTED 占位符；
    #       每条长度被 CI_LOG_CHUNK_CHARS 限制住；meta 标记 secrets_redacted=True（入库前已脱敏、已限长）。
    assert len(docs) > 1
    assert all(secret not in doc.content for doc in docs)
    assert "[REDACTED" in "\n".join(doc.content for doc in docs)
    assert all(len(doc.content) <= CI_LOG_CHUNK_CHARS + len("failing build") + 1 for doc in docs)
    assert all(doc.meta["secrets_redacted"] is True for doc in docs)


def test_evidence_store_redacts_failed_ci_logs_before_chat_context() -> None:
    db = _db_session()
    repo = _repo(db)
    secret = "ghp_" + "a" * 32
    db.add(
        WorkflowRun(
            repo_id=repo.id,
            name="failing build",
            status="completed",
            conclusion="failure",
            logs_text=f"MY_API_KEY={secret}\nERROR authentication failed",
        )
    )
    db.flush()

    # 重建证据库（把仓库数据抽成 EvidenceItem）。
    EvidenceStore(db).rebuild_repo(repo)
    evidence = (
        db.query(EvidenceItem)
        .filter(EvidenceItem.repo_id == repo.id, EvidenceItem.source_type == "workflow_run")
        .one()
    )

    # 断言：证据内容里没有原始 secret，且被替换成 "MY_API_KEY=[REDACTED]"；meta.secrets_redacted=True。
    # 为什么：进入聊天上下文的证据也要先脱敏，避免把密钥透给模型/用户。
    assert secret not in evidence.content
    assert "MY_API_KEY=[REDACTED]" in evidence.content
    assert evidence.meta["secrets_redacted"] is True


def test_context_assembler_prefetches_rag_evidence_before_chat_agent(monkeypatch) -> None:
    db = _db_session()
    repo = _repo(db)
    queries: list[str] = []

    # 假 EvidenceStore.search：记录查询词，返回一条 project_doc 证据。
    def fake_search(self, repo_id, conversation_id, query, limit=5):
        """fake_search."""
        queries.append(query)
        return [
            {
                "title": "Authentication retry policy",
                "source_type": "project_doc",
                "source_id": "doc-1",
                "snippet": "Refresh retries must use an idempotency key.",
                "score": 0.91,
                "metadata": {},
                "retrieval": {"sources": ["vector", "keyword"]},
            }
        ]

    monkeypatch.setattr(EvidenceStore, "search", fake_search)
    # 组装聊天上下文（会先预取证据）。
    assembled = ContextAssembler(db).assemble(
        repo,
        None,
        "How should refresh retries work?",
    )

    # 断言：确实用用户问题去搜了证据；第一条证据是 doc-1；system_context 里包含"[Related Evidence]"和那段摘要。
    assert queries == ["How should refresh retries work?"]
    assert assembled.evidence[0]["source_id"] == "doc-1"
    assert "[Related Evidence]" in assembled.system_context
    assert "Refresh retries must use an idempotency key." in assembled.system_context


def test_context_assembler_reports_exact_recent_history_coverage() -> None:
    db = _db_session()
    repo = _repo(db)
    # 造一条会话，含 3 条消息（user/assistant/user）。
    conversation = Conversation(repo_id=repo.id, title="Recall recent messages")
    db.add(conversation)
    db.flush()
    db.add_all(
        [
            ChatMessage(repo_id=repo.id, conversation_id=conversation.id, role="user", content="hi"),
            ChatMessage(
                repo_id=repo.id,
                conversation_id=conversation.id,
                role="assistant",
                content="你好！有什么可以帮你？",
            ),
            ChatMessage(
                repo_id=repo.id,
                conversation_id=conversation.id,
                role="user",
                content="我刚刚说了什么？",
            ),
        ]
    )
    db.flush()

    # 组装上下文（问"我刚刚说了什么"）。
    assembled = ContextAssembler(db).assemble(repo, conversation, "我刚刚说了什么？")
    coverage = assembled.compression_stats["history_coverage"]

    # 断言：最近历史被"完整精确"保留（没被压缩截断），prompt 含 3 条、精确后缀 3 条、省略 0 条、无压缩边界。
    assert coverage["complete_and_exact"] is True
    assert coverage["prompt_messages"] == 3
    assert coverage["exact_suffix_messages"] == 3
    assert coverage["omitted_messages"] == 0
    assert coverage["has_compaction_boundary"] is False


@pytest.mark.asyncio
async def test_session_sealing_keeps_raw_memory_out_of_project_rag(monkeypatch) -> None:
    # 假 LLM：提供 chat_text（摘要）和 chat_json（结构化事实提取）。
    class FakeLLM:
        async def chat_text(self, system_prompt, user_prompt):
            """chat_text."""
            return "The team chose short-lived access tokens."

        async def chat_json(self, system_prompt, payload):
            """chat_json."""
            return {
                "summary": "Use short-lived access tokens.",
                "facts": ["Tokens expire after fifteen minutes."],
                "decisions": ["Refresh once before asking the user to sign in."],
                "open_questions": [],
                "tasks": [],
                "user_preferences": [],
                "repo_context": [],
                "citations": [],
            }

    db = _db_session()
    repo = _repo(db)
    conversation = Conversation(repo_id=repo.id, title="Authentication")
    db.add(conversation)
    db.flush()
    user_message = ChatMessage(
        repo_id=repo.id,
        conversation_id=conversation.id,
        role="user",
        content="How should token refresh work?",
    )
    assistant_message = ChatMessage(
        repo_id=repo.id,
        conversation_id=conversation.id,
        role="assistant",
        content="Refresh once, then require sign-in.",
    )
    db.add_all([user_message, assistant_message])
    db.flush()

    # 密封这次会话：把原始对话提炼成摘要/记忆，并落库。
    await SessionSealer(db, llm=FakeLLM()).seal(
        repo=repo,
        conversation=conversation,
        user_message=user_message,
        assistant_message=assistant_message,
        route="chat",
        tool_calls=[],
    )
    db.commit()

    # 断言：生成了 1 条 ChatSession 和 1 条 ConversationMemory；
    #       但原始的聊天/会话记忆类文档（chat_session / conversation_memory / thread_memory）没有被写进项目 RAG 文档库——
    #       原始记忆走专门通道，不污染 RAG 检索。
    assert db.query(ChatSession).filter(ChatSession.conversation_id == conversation.id).count() == 1
    assert db.query(ConversationMemory).filter(ConversationMemory.conversation_id == conversation.id).count() == 1
    assert (
        db.query(Document)
        .filter(Document.source_type.in_(["chat_session", "conversation_memory", "thread_memory"]))
        .count()
        == 0
    )
