"""
tests/test_rag_qa.py —— 测试 RAG 问答（qa.py）的核心行为
============================================================================

RAG = 检索增强生成。qa.py 负责：先检索相关文档片段 → 让 LLM 基于这些片段作答 → 返回
带编号引用的答案。本测试重点验证三件事：
  1. 确定性 embedding 对中文语义有效（相关句子相似度更高）；
  2. build_sources 会「隐藏服务器内部文件路径」避免泄露；
  3. answer_question 的三种模式：走 LLM 正常答 / 无密钥时抽取式兜底 / 证据冲突时拒绝作答。

全部用 monkeypatch 把「数据库查询、LLM 调用」替换成假实现，不连真实服务。."""

import pytest  # pytest 测试框架

from app.core.config import settings  # 全局配置（用来 mock 掉 llm_api_key）
from app.services.rag.embeddings import deterministic_embedding  # 确定性 embedding（不调外部 API）
from app.services.rag.qa import answer_question, build_sources  # 被测函数
from app.services.rag.retrieval import cosine_similarity  # 余弦相似度


def test_deterministic_embedding_handles_chinese_without_spaces() -> None:
    # 测试：确定性 embedding 能理解「无空格的中文」语义——相关句子向量更接近
    question = deterministic_embedding("员工怎么申请年假？", 128)  # 把问题转成 128 维向量
    relevant = deterministic_embedding("员工年假审批流程和请假规定", 128)  # 语义相关
    unrelated = deterministic_embedding("数据库连接池监控告警", 128)  # 语义无关

    # 断言：问题和「相关句」的相似度 > 和「无关句」的相似度
    assert cosine_similarity(question, relevant) > cosine_similarity(question, unrelated)


def test_build_sources_hides_server_file_path() -> None:
    # 测试：build_sources 在生成引用时，要把服务器内部路径 stored_path 剔除，只留文件名
    sources = build_sources(
        [
            {
                "id": "doc-1",
                "source_id": "source-1",
                "source_type": "knowledge_file",
                "title": "handbook.md#1",
                "snippet": "年假需要先在系统中提交审批。",
                "score": 0.91,
                # metadata 里混入了服务器内部路径，不能暴露给前端
                "metadata": {"filename": "handbook.md", "stored_path": "C:/private/upload/handbook.md"},
            }
        ]
    )

    assert sources[0]["citation"] == 1  # 引用编号从 1 开始
    assert sources[0]["metadata"]["filename"] == "handbook.md"  # 文件名保留
    assert "stored_path" not in sources[0]["metadata"]  # 内部路径被删除，不泄露


@pytest.mark.asyncio  # 标记这是异步测试（pytest-asyncio 会特殊处理）
async def test_answer_question_uses_llm_and_returns_numbered_sources(monkeypatch) -> None:
    # 测试：正常情况——有 LLM key，调用 LLM 作答并带 [1] 编号引用
    monkeypatch.setattr(settings, "llm_api_key", "test-key")  # 假装配了密钥
    # 把检索函数替换成假实现：直接返回一条关于「刷新令牌」的文档
    monkeypatch.setattr(
        "app.services.rag.qa.search_similar_documents",
        lambda *args, **kwargs: [
            {
                "id": "doc-1",
                "source_id": "source-1",
                "source_type": "knowledge_file",
                "title": "auth.md#1",
                "snippet": "访问令牌过期后，客户端使用刷新令牌重试一次。",
                "score": 0.95,
                "metadata": {"filename": "auth.md"},
            }
        ],
    )

    # 一个假的 LLM：检查 prompt 里确实带了「只能依据」和「[资料 1]」，并返回带引用的答案
    class FakeLLM:
        async def chat_text(self, system_prompt: str, user_message: str) -> str:
            assert "只能依据" in system_prompt  # 系统提示要求只依据资料
            assert "[资料 1]" in user_message  # 用户消息里引用了资料 1
            return "访问令牌过期后应使用刷新令牌重试一次。[1]"

    # object() 当假 db 用（不被真正查询）；第二个参数是 repo_id 字符串；传 llm=FakeLLM
    result = await answer_question(object(), "00000000-0000-0000-0000-000000000001", "登录过期后怎么办？", llm=FakeLLM())

    assert result["generation_mode"] == "llm"  # 走的是 LLM 模式
    assert result["retrieval_count"] == 1  # 检索到 1 条
    assert result["sources"][0]["citation"] == 1  # 引用编号
    assert "[1]" in result["answer"]  # 答案里带引用标记


@pytest.mark.asyncio
async def test_answer_question_has_offline_extractive_fallback(monkeypatch) -> None:
    # 测试：没有 LLM 密钥时，降级为「抽取式」回答（直接从检索片段里摘句子，不调 LLM）
    monkeypatch.setattr(settings, "llm_api_key", None)  # 故意设成 None（无密钥）
    monkeypatch.setattr(
        "app.services.rag.qa.search_similar_documents",
        lambda *args, **kwargs: [
            {
                "id": "doc-1",
                "source_id": "source-1",
                "source_type": "knowledge_file",
                "title": "leave.md#1",
                "snippet": "员工申请年假时，需要先提交直属主管审批。审批通过后同步到考勤系统。",
                "score": 0.88,
                "metadata": {},
            }
        ],
    )

    result = await answer_question(object(), "00000000-0000-0000-0000-000000000001", "年假怎么审批？")

    assert result["generation_mode"] == "extractive"  # 抽取式模式
    assert "主管审批" in result["answer"]  # 答案直接摘取了片段里的关键信息
    assert "[1]" in result["answer"]  # 仍然带引用编号


@pytest.mark.asyncio
async def test_answer_question_does_not_call_llm_when_gate_rejects_evidence(monkeypatch) -> None:
    # 测试：当「答案门」判定检索证据自相矛盾（conflict），绝不调用 LLM，直接给 no_evidence
    monkeypatch.setattr(settings, "llm_api_key", "test-key")

    # 假检索：返回一条「旧版本允许部署」的证据，但 answer_gate 判定为 conflict（冲突）
    async def fake_retrieve(*_args, **_kwargs):
        """fake_retrieve."""
        return (
            [
                {
                    "id": "doc-1",
                    "source_id": "source-1",
                    "source_type": "knowledge_file",
                    "title": "old.md",
                    "snippet": "旧版本允许部署。",
                    "score": 0.91,
                    "metadata": {"branch": "legacy"},
                    "retrieval": {"answer_gate": "conflict"},
                }
            ],
            {"answer_gate": {"decision": "conflict", "reason": "高排名证据给出了相反结论"}},
            1,
        )

    monkeypatch.setattr("app.services.rag.qa.retrieve_knowledge", fake_retrieve)

    # 假 db：一旦被查询就报错（证明根本不该走到数据库）
    class FakeDB:
        def query(self):
            """query."""
            raise AssertionError("retrieve_knowledge is mocked")

    # 假 LLM：一旦被调用就报错（证明 gate 拒绝后不应调用 LLM）
    class FailingLLM:
        async def chat_text(self, _system_prompt: str, _user_message: str) -> str:
            raise AssertionError("LLM must not run when the answer gate rejects evidence")

    result = await answer_question(
        FakeDB(),
        "00000000-0000-0000-0000-000000000001",
        "现在允许部署吗？",
        llm=FailingLLM(),
    )

    assert result["generation_mode"] == "no_evidence"  # 无证据模式
    assert result["answer_gate"]["decision"] == "conflict"  # 确实判定冲突
    assert result["retrieval_count"] == 1
    assert "存在冲突" in result["answer"]  # 答案说明证据冲突
