"""
tests/test_rag_retrieval_pipeline.py —— 测试 RAG 检索流水线（retrieval / knowledge_base / answer_gate）
============================================================================

这个测试覆盖「用户提问 → 检索相关文档」的完整链路，以及其中的关键策略：

  - BM25 关键词检索：稀有的错误名（如 ExpiredSignatureError）应排在最前，并暴露匹配痕迹；
  - 元数据过滤：在 BM25 打分「之前」就先按 state=open 等条件过滤；
  - 加权融合（RRF 思路）：向量召回 + 关键词召回加权合并，两边都命中有加成；
  - 父块扩展：把一篇文档拆成的多个 chunk 重新拼回原文（同 source/同分支才扩）；
  - 答案门（answer_gate）：证据不足/需澄清/冲突/可答 四种判定；
  - qwen 重排：拿到原始内容再重排；qwen 超时时降级到启发式重排；
  - 整条流水线 trace 的每一个 stage 都正确记录。

全部用内存 SQLite + monkeypatch，不依赖外部向量库/LLM。."""

import uuid  # 唯一 id

import pytest  # 测试框架
from sqlalchemy import create_engine  # 建引擎
from sqlalchemy.orm import sessionmaker  # 会话工厂

from app.db.models import Base, Document, Repository  # 表定义与模型
from app.services.rag.answer_gate import evaluate_answer_gate  # 答案门
from app.services.rag.knowledge_base import retrieve_knowledge  # 知识检索全流程
from app.services.rag.retrieval import (  # 检索相关函数
    _merge_candidates,  # 融合向量/关键词候选
    expand_parent_context,  # 父块扩展
    search_similar_documents,  # 相似文档检索
)


def _db_session():
    """_db_session."""
    # 建内存 SQLite 并建表，返回会话
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _repo(db):
    """_repo."""
    # 造一个仓库（full_name 带随机 uuid 保证唯一）
    repo = Repository(owner="local", name="retrieval", full_name=f"local/retrieval-{uuid.uuid4()}")
    db.add(repo)
    db.flush()
    return repo


def _document(db, repo, *, title, content, source_id=None, metadata=None):
    """_document."""
    # 造一篇知识库文档并入库
    item = Document(
        repo_id=repo.id,
        source_type="knowledge_file",
        source_id=source_id or uuid.uuid4(),
        title=title,
        content=content,
        meta=metadata or {},
    )
    db.add(item)
    db.flush()
    return item


def test_bm25_prefers_rare_error_name_and_exposes_trace() -> None:
    # 测试：BM25 应把「稀有错误名」相关的文档排第一，并暴露命中词
    db = _db_session()
    repo = _repo(db)
    _document(db, repo, title="General retry notes", content="failure retry token failure retry token")  # 常见词
    rare = _document(  # 含稀有错误名
        db, repo, title="JWT failure",
        content="ExpiredSignatureError is raised when the access token has expired.",
        metadata={"path": "backend/app/auth/token.py"},
    )
    db.commit()

    results = search_similar_documents(
        db, repo.id, "ExpiredSignatureError",
        retrieval_method="fulltext",  # 走全文（BM25）检索
        apply_rerank=False,  # 不做重排，直接看 BM25 结果
    )

    assert results[0]["id"] == str(rare.id)  # 稀有错误名那篇排第一
    assert results[0]["bm25_score"] > 0  # 有 BM25 分
    assert results[0]["keyword_rank"] == 1  # 关键词排名 1
    assert results[0]["retrieval"]["keyword_backend"] == "bm25"  # 后端是 bm25
    assert "expiredsignatureerror" in results[0]["retrieval"]["bm25_matched_terms"]  # 暴露命中词（小写）


def test_metadata_state_filter_is_applied_before_bm25_scoring() -> None:
    # 测试：元数据过滤应在 BM25 打分「之前」生效（只返回 state=open 的）
    db = _db_session()
    repo = _repo(db)
    open_doc = _document(db, repo, title="Open authentication issue", content="authentication token failure", metadata={"state": "open"})
    _document(db, repo, title="Closed authentication issue", content="authentication token failure", metadata={"state": "closed"})
    db.commit()

    results = search_similar_documents(
        db, repo.id, "authentication token",
        retrieval_method="fulltext",
        metadata_filters={"state": "open"},  # 只想要 open 的
        apply_rerank=False,
    )

    assert [item["id"] for item in results] == [str(open_doc.id)]  # 只有 open 那篇


def test_weighted_fusion_uses_project_formula_and_dual_recall_bonus() -> None:
    # 测试：向量+关键词加权融合公式，以及「两边都召回」的加成
    vector = [
        {"id": "shared", "score": 1.0, "vector_score": 1.0, "metadata": {"chunk_id": "chunk-shared"}, "retrieval": {"sources": ["vector"], "vector_score": 1.0, "vector_rank": 1}},
        {"id": "vector-only", "score": 0.8, "vector_score": 0.8, "metadata": {"chunk_id": "chunk-vector"}, "retrieval": {"sources": ["vector"], "vector_score": 0.8, "vector_rank": 2}},
    ]
    keyword = [
        {"id": "shared", "score": 1.0, "keyword_score": 1.0, "bm25_score": 8.2, "metadata": {"chunk_id": "chunk-shared"}, "retrieval": {"sources": ["keyword"], "keyword_score": 1.0, "keyword_rank": 1}},
    ]

    results = _merge_candidates(vector, keyword)  # 融合两部分候选

    assert results[0]["id"] == "shared"  # 两边都命中的排第一
    assert results[0]["combined_score"] == pytest.approx(1.0)  # 融合分约等于 1.0
    assert results[0]["retrieval"]["fusion_method"] == "weighted"  # 加权融合
    assert results[0]["retrieval"]["fusion_weights"] == {"vector": 0.62, "keyword": 0.30, "dual_recall_bonus": 0.08}  # 项目公式
    assert results[1]["combined_score"] == pytest.approx(0.496)  # 只向量命中的分更低


def test_parent_expansion_reads_only_same_parent_source_and_version() -> None:
    # 测试：父块扩展只拼「同 source_id + 同分支」的 chunk，且按 child_index 顺序
    db = _db_session()
    repo = _repo(db)
    source_id = uuid.uuid4()
    rows = [
        _document(db, repo, title=f"auth.md#{index + 1}", content=content, source_id=source_id,
                  metadata={"chunk_id": f"chunk-{index}", "parent_id": "parent-auth", "child_index": index, "branch": "main"})
        for index, content in enumerate(["token setup", "refresh token failure", "retry once"])  # 同一篇的 3 个分块
    ]
    _document(db, repo, title="old branch", content="must not be expanded", source_id=source_id,
              metadata={"chunk_id": "old", "parent_id": "parent-auth", "child_index": 2, "branch": "legacy"})  # 同父但分支是 legacy，不应被扩进来
    db.commit()

    items, stage = expand_parent_context(
        db, repo.id,
        [{"id": str(rows[1].id), "source_id": str(source_id), "source_type": "knowledge_file", "metadata": rows[1].meta, "_raw_content": rows[1].content, "retrieval": {}}],
    )

    assert items[0]["parent_expanded"] is True  # 触发了父块扩展
    assert items[0]["_raw_content"] == "token setup\n\nrefresh token failure\n\nretry once"  # 3 块按顺序拼回
    assert "must not be expanded" not in items[0]["_raw_content"]  # legacy 分支的没混进来
    assert stage["additional_chunks"] == 2  # 额外扩了 2 个块


@pytest.mark.parametrize(  # 参数化：一组 (查询, 检索项, 期望判定) 用例
    ("query", "items", "expected"),
    [
        ("token refresh", [], "insufficient_evidence"),  # 没检索到 → 证据不足
        (
            "这个怎么办",
            [{"id": "a", "source_id": "one", "score": 0.9, "snippet": "第一种处理方式"}, {"id": "b", "source_id": "two", "score": 0.88, "snippet": "第二种处理方式"}],
            "ask_clarification",  # 多个互斥方案 → 需澄清
        ),
        (
            "是否允许部署",
            [{"id": "a", "source_id": "one", "score": 0.92, "snippet": "当前版本允许部署。"}, {"id": "b", "source_id": "two", "score": 0.9, "snippet": "当前版本禁止部署。"}],
            "conflict",  # 结论相反 → 冲突
        ),
        (
            "token refresh policy",
            [{"id": "a", "source_id": "one", "score": 0.92, "snippet": "Refresh the token once."}],
            "answer",  # 单一明确 → 可答
        ),
    ],
)
def test_answer_gate_exposes_all_project_decisions(query, items, expected) -> None:
    gate = evaluate_answer_gate(query, items, score_threshold=0.8, threshold_enabled=True)

    assert gate["decision"] == expected  # 判定符合预期
    assert "reason" in gate  # 带了原因说明


@pytest.mark.asyncio
async def test_qwen_rerank_receives_raw_content_before_compression(monkeypatch) -> None:
    # 测试：qwen 重排应在「压缩之前」拿到原始内容
    db = _db_session()
    repo = _repo(db)
    candidate = {
        "id": "auth", "source_id": str(uuid.uuid4()), "source_type": "knowledge_file", "title": "auth.md",
        "_raw_content": "irrelevant setup. " * 80 + "Refresh the token once after ExpiredSignatureError.",  # 前面冗余，后面相关
        "snippet": "short pre-rerank snippet", "score": 0.91, "metadata": {},
        "retrieval": {"fusion_method": "weighted"},
    }

    def fake_search(*_args, **kwargs):
        """fake_search."""
        """fake_search."""
        kwargs["pipeline_trace"].update({"fusion_method": "weighted", "stages": []})
        return [candidate]

    async def fake_qwen(_query, items, _config, _limit):
        """fake_qwen."""
        # 断言：重排时拿到的还是未压缩的原始内容
        assert items[0]["_raw_content"].startswith("irrelevant setup")
        assert "compression" not in items[0]
        return [{**items[0], "score": 0.95, "rerank_score": 0.95, "retrieval": {**items[0]["retrieval"], "rerank_mode": "qwen_api"}}]

    monkeypatch.setattr("app.services.rag.knowledge_base.search_similar_documents", fake_search)
    monkeypatch.setattr("app.services.rag.knowledge_base._qwen_rerank", fake_qwen)

    items, trace, _duration = await retrieve_knowledge(
        db, repo.id, "ExpiredSignatureError token refresh",
        retrieval_method="fulltext", score_threshold_enabled=False,
    )

    assert items[0]["compression"]["mode"] == "query_aware_extractive"  # 压缩方式是「按查询抽取」
    assert "ExpiredSignatureError" in items[0]["snippet"]  # 片段含关键信息
    stage_names = [stage["name"] for stage in trace["pipeline"]["stages"]]
    # 整条流水线 stage 顺序正确
    assert stage_names == ["rerank", "parent_expansion", "score_threshold", "context_compression", "answer_gate"]
    assert trace["answer_gate"]["decision"] == "answer"


@pytest.mark.asyncio
async def test_qwen_failure_is_traced_and_falls_back_to_heuristic(monkeypatch) -> None:
    # 测试：qwen 重排超时时，降级到启发式重排，并在 trace 里记录 fallback
    db = _db_session()
    repo = _repo(db)

    def fake_search(*_args, **kwargs):
        kwargs["pipeline_trace"].update({"fusion_method": "weighted", "stages": []})
        return [{"id": "auth", "source_id": str(uuid.uuid4()), "source_type": "knowledge_file", "title": "auth middleware",
                 "_raw_content": "auth middleware refreshes the token", "snippet": "auth middleware refreshes the token",
                 "score": 0.9, "metadata": {}, "retrieval": {"sources": ["vector", "keyword"]}}]

    async def fail_qwen(*_args, **_kwargs):
        """fail_qwen."""
        raise TimeoutError("rerank timeout")  # 故意让重排超时

    monkeypatch.setattr("app.services.rag.knowledge_base.search_similar_documents", fake_search)
    monkeypatch.setattr("app.services.rag.knowledge_base._qwen_rerank", fail_qwen)

    items, trace, _duration = await retrieve_knowledge(
        db, repo.id, "auth middleware", retrieval_method="fulltext", score_threshold_enabled=False,
    )

    rerank_stage = next(stage for stage in trace["pipeline"]["stages"] if stage["name"] == "rerank")
    assert items[0]["retrieval"]["rerank_mode"] == "heuristic"  # 降级到启发式
    assert rerank_stage["fallback"] is True  # 标记了 fallback
    assert rerank_stage["fallback_reason"] == "TimeoutError"  # 原因是超时


@pytest.mark.asyncio
async def test_retrieval_trace_contains_every_selected_pipeline_stage() -> None:
    # 测试：关闭重排时，trace 完整记录每一个选定的流水线 stage
    db = _db_session()
    repo = _repo(db)
    _document(db, repo, title="Authentication runbook", content="The auth middleware refreshes expired tokens once.",
              metadata={"path": "backend/app/auth/middleware.py"})
    db.commit()

    items, trace, _duration = await retrieve_knowledge(
        db, repo.id, "auth middleware token",
        retrieval_method="fulltext", rerank_enabled=False, score_threshold_enabled=False,
    )

    stage_names = [stage["name"] for stage in trace["pipeline"]["stages"]]
    # 完整 stage 顺序（关闭 rerank 用 single_retriever_selection 代替）
    assert stage_names == [
        "vector_recall", "keyword_recall", "single_retriever_selection",
        "deduplication_and_parent_aggregation", "rerank", "parent_expansion",
        "score_threshold", "context_compression", "answer_gate",
    ]
    assert trace["fusion_method"] == "fulltext"
    assert trace["answer_gate"]["decision"] == "answer"
    assert items[0]["retrieval"]["keyword_backend"] == "bm25"  # 关键词后端是 bm25
    assert items[0]["retrieval"]["selected_for_context"] is True  # 被选中进入上下文
