"""DevMind AI module."""

# 导入真实的重排函数 rerank_documents：它负责在初步召回的候选文档里，按"和查询的相关度"重新排序。
# 为什么：RAG 检索先粗召回一大批，再靠 rerank 把最相关的顶到前面；这个函数决定了用户最终看到的答案质量。
from app.services.rag.rerank import rerank_documents


def test_rerank_prefers_exact_path_and_query_terms() -> None:
    # 准备两个候选文档。注意它们的初始向量分（score / vector_score）不一样：general 反而更高(0.8)。
    candidates = [
        {
            # general：一份泛泛的 README，向量分高但关键词分低。
            "id": "general",
            "title": "README setup",
            "snippet": "General local development notes",
            "score": 0.8,
            "vector_score": 0.8,
            "keyword_score": 0.1,
            "metadata": {},
        },
        {
            # auth：一个具体的 auth 中间件代码，向量分只有 0.5，但关键词分满分(1.0)，而且 metadata 里带精确路径。
            "id": "auth",
            "title": "backend/app/auth/middleware.py#1",
            "snippet": "validate token and refresh session when login expires",
            "score": 0.5,
            "vector_score": 0.5,
            "keyword_score": 1.0,
            "metadata": {"path": "backend/app/auth/middleware.py"},
        },
    ]

    # 用查询 "login token auth middleware" 去重排，只取前 2 条。
    results = rerank_documents("login token auth middleware", candidates, limit=2)

    # 断言：重排后第一条是 auth 而不是 general。
    # 为什么：虽然 general 的向量分更高，但 auth 的文件路径精确命中、关键词又满分，rerank 把"精确路径 + 查询词命中"看得比原始向量分更重。
    assert results[0]["id"] == "auth"
    # 断言：第一条的 rerank_score 不低于第二条（排序确实生效，且按降序）。
    assert results[0]["rerank_score"] >= results[1]["rerank_score"]
    # 断言：第一条里带了 rank_reason（重排理由），方便调试/可解释——知道为什么被排到前面。
    assert "rank_reason" in results[0]
