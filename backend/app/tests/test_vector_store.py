"""DevMind AI module."""

# uuid：生成唯一 id。
import uuid

# pytest 框架。
import pytest

# 导入配置、Document 模型，以及向量库相关的真实函数。
from app.core.config import settings
from app.db.models import Document
from app.services.rag.embeddings import embed_knowledge_texts, validate_embedding_contract
from app.services.rag.vector_store import _clip_utf8, _collection_dimensions, upsert_milvus_documents


def test_clip_utf8_limits_encoded_length_without_breaking_characters() -> None:
    # 造一个由 3000 个"猫"字组成的字符串（中文每个字 utf-8 编码占 3 字节，远超限制）。
    value = "\u732b" * 3000
    # 把 utf-8 字节长度裁剪到不超过 8192。
    clipped = _clip_utf8(value, 8192)

    # 断言：裁剪后字节数 <= 8192。
    assert len(clipped.encode("utf-8")) <= 8192
    # 断言：裁剪后不是空串（确实保留了内容）。
    assert clipped
    # 断言：保留下来的字符集合仍然只有"猫"字——说明是按字节边界合法截断，没有把一个中文字切成乱码。
    # 为什么：中文字是多字节，若从中间切断会变成非法字符；正确做法是退到完整字符边界。
    assert set(clipped) == {"\u732b"}


def test_collection_dimension_is_read_from_milvus_schema() -> None:
    # 模拟 Milvus 集合的 schema（字段里 embedding 的 dim 是 "1024"，注意原值是字符串）。
    assert _collection_dimensions(
        {"fields": [{"name": "embedding", "params": {"dim": "1024"}}]}
    ) == 1024


def test_embedding_contract_rejects_per_repository_vector_spaces() -> None:
    # 验证"向量契约"：当 provider/model 和基准不一致时（deterministic vs other-model），必须报错。
    with pytest.raises(ValueError, match="Embedding contract mismatch"):
        validate_embedding_contract("deterministic", "other-model", 384)


def test_explicit_deterministic_provider_is_allowed_without_becoming_a_fallback(monkeypatch) -> None:
    # 把配置里的 embedding 三件套临时设成 deterministic 本地确定性方案（维度 8，便于快速测试）。
    monkeypatch.setattr(settings, "embedding_provider", "deterministic")
    monkeypatch.setattr(settings, "embedding_model", "deterministic-local")
    monkeypatch.setattr(settings, "embedding_dimensions", 8)

    # 显式指定用 deterministic 方案给文本生成向量。
    vectors = embed_knowledge_texts(
        ["token failure"],
        provider="deterministic",
        model="deterministic-local",
        dimensions=8,
    )

    # 断言：1 段文本得到 1 个向量。
    assert len(vectors) == 1
    # 断言：向量维度是 8（和配置一致）。
    assert len(vectors[0]) == 8


def test_embedding_provider_failure_is_not_replaced_with_hash_vectors(monkeypatch) -> None:
    # 配置成 openai_compatible 方案。
    monkeypatch.setattr(settings, "embedding_provider", "openai_compatible")
    monkeypatch.setattr(settings, "embedding_model", "configured-model")
    monkeypatch.setattr(settings, "embedding_dimensions", 8)
    # 把底层"调用 openai 兼容接口"的函数替换成：一调用就抛 RuntimeError("provider offline")（模拟服务挂了）。
    monkeypatch.setattr(
        "app.services.rag.embeddings._embed_openai_compatible",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("provider offline")),
    )

    # 断言：embed_knowledge_texts 会原样把 RuntimeError 抛出来，而不是偷偷用 hash 向量兜底。
    # 为什么：向量一旦用错误的方式生成（比如降级成 hash），语义相似度就全乱了，比直接报错更危险。所以"宁可失败也不降级"。
    with pytest.raises(RuntimeError, match="provider offline"):
        embed_knowledge_texts(
            ["token failure"],
            provider="openai_compatible",
            model="configured-model",
            dimensions=8,
        )


def test_milvus_upsert_rejects_dimension_fitting_before_connecting(monkeypatch) -> None:
    # 配置维度为 4，但下面要插入的向量却是 2 维——维度对不上。
    monkeypatch.setattr(settings, "embedding_dimensions", 4)
    # 造一个待入库的 Document（issue 类型）。
    document = Document(
        id=uuid.uuid4(),
        repo_id=uuid.uuid4(),
        source_type="issue",
        source_id=uuid.uuid4(),
        title="Token failure",
        content="Refresh failed.",
        meta={},
    )

    # 断言：upsert 时直接抛 ValueError，提示"拒绝截断或补零"（Refusing to truncate or pad）。
    # 为什么：在真正连 Milvus 之前就拦下维度不符，避免把错误维度的向量硬塞进去污染向量库（连接很贵，先校验再连）。
    with pytest.raises(ValueError, match="Refusing to truncate or pad"):
        upsert_milvus_documents([document], [[1.0, 0.0]])
