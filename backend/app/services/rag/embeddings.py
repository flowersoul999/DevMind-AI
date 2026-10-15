"""DevMind AI module."""

import hashlib
import math
import os
import re
from functools import lru_cache
from typing import Any

import httpx

from app.core.config import settings


def _deterministic_tokens(text: str) -> list[str]:
    # 把文本拆成「词元（token）」：英文数字按词切，中文按字和相邻两字组合切。
    # 为什么：embedding 的第一步往往是分词；这里用确定性规则把任意文本变成可统计的 token 序列。
    lowered = (text or "").lower()
    # 英文 / 数字 / 下划线 / 连字符 连成词
    tokens = re.findall(r"[a-z0-9_\-]+", lowered)
    # 中文整段抽出，再拆成单字 + 相邻双字（bigram），这样「登录」和「登陆」会有部分重叠，能体现语义相近
    for segment in re.findall(r"[\u4e00-\u9fff]+", lowered):
        tokens.extend(segment)
        tokens.extend(segment[index : index + 2] for index in range(max(0, len(segment) - 1)))
    return tokens


def deterministic_embedding(text: str, dimensions: int = 128) -> list[float]:
    # 本地「确定性」embedding：不调用任何模型，只用一个哈希函数把文本映射成向量。
    # 为什么：纯属演示 / 测试兜底——没有 API key 也能跑通整条 RAG 管线，但效果远不如真模型。
    # 类比：把文字变成一串能比较相似度的数字坐标（哈希法版本），坐标越接近说明词元越重合。
    vector = [0.0] * dimensions  # 先准备一个全 0 的向量
    for token in _deterministic_tokens(text):
        # 每个词元哈希出一个落入 [0, dimensions) 的下标，并在该位置 +1
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:2], "big") % dimensions
        vector[index] += 1.0
    # 归一化：把向量长度缩放到 1，方便后面用余弦相似度比较（只比方向、不比大小）
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


def _validate_vector_dimensions(vector: list[float], dimensions: int, provider: str) -> list[float]:
    # 校验向量维度是否正确。RAG 的硬性前提：同一批数据必须处在「同一个向量空间」。
    if len(vector) != dimensions:
        raise ValueError(
            f"Embedding provider '{provider}' returned {len(vector)} dimensions; expected {dimensions}. "
            "Refusing to truncate or pad vectors."  # 为什么：绝不允许偷偷补零或截断，否则向量语义就乱套了
        )
    return vector


def normalize_embedding_provider(provider: str) -> str:
    # 把用户填的各种「别名」统一成内部标准名，比如 "openai" / "api" / "remote" 都归一为 "openai_compatible"。
    # 为什么：不同配置写法语义相同，归一后后面只要判断有限的几个标准值即可。
    provider = (provider or "").strip().lower().replace("-", "_")
    aliases = {
        "api": "openai_compatible",
        "openai": "openai_compatible",
        "remote": "openai_compatible",
        "openai_compatible": "openai_compatible",
        "sentence_transformer": "local",
        "sentence_transformers": "local",
        "local_sentence_transformers": "local",
        "local": "local",
        "qwen_local": "qwen_local",
        "dashscope": "dashscope",
        "hash": "deterministic",
        "deterministic": "deterministic",
    }
    resolved = aliases.get(provider)
    if resolved is None:
        raise ValueError(f"Unsupported embedding provider: {provider or '<empty>'}")
    return resolved


def validate_embedding_contract(provider: str, model: str, dimensions: int) -> None:
    # 「向量合约」校验：所有仓库共享同一个 Milvus 集合（向量库），就必须用同一套 provider / model / 维度。
    # 为什么：不同模型 / 维度产出的向量不在一个空间里，混在一条集合里检索会彻底失效。
    configured = (
        normalize_embedding_provider(settings.embedding_provider),
        settings.embedding_model.strip(),
        settings.embedding_dimensions,
    )
    requested = (normalize_embedding_provider(provider), model.strip(), dimensions)
    if requested != configured:
        raise ValueError(
            "Embedding contract mismatch: "
            f"requested provider={requested[0]}, model={requested[1]}, dimensions={requested[2]}; "
            f"configured provider={configured[0]}, model={configured[1]}, dimensions={configured[2]}. "
            "All repositories sharing the Milvus collection must use one vector space."
        )


def embedding_contract() -> dict[str, Any]:
    # 读取当前全局配置里的向量合约（provider / model / dimensions）。
    return {
        "provider": normalize_embedding_provider(settings.embedding_provider),
        "model": settings.embedding_model,
        "dimensions": settings.embedding_dimensions,
    }


def embedding_provider_name() -> str:
    # 便捷封装：只拿归一后的 provider 名。
    return normalize_embedding_provider(settings.embedding_provider)


def _embedding_payload(texts: list[str], dimensions: int, model: str) -> dict[str, Any]:
    # 组装发给 OpenAI 兼容 embedding 接口的 JSON 请求体：模型名 + 待编码文本列表（+可选维度）。
    payload: dict[str, Any] = {
        "model": model,
        "input": texts,
    }
    if dimensions:
        payload["dimensions"] = dimensions  # 部分模型支持指定输出维度
    return payload


def _embed_openai_compatible(
    texts: list[str],
    dimensions: int,
    *,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> list[list[float]]:
    # 调用「OpenAI 兼容」的远程 embedding 服务（兼容接口格式即可，不一定非得是 OpenAI）。
    api_key = api_key or settings.embedding_api_key or os.getenv("OPENAI_API_KEY") or settings.llm_api_key
    base_url = (base_url or settings.embedding_base_url or settings.llm_base_url).rstrip("/")
    model = model or settings.embedding_model
    if not api_key:
        raise RuntimeError("embedding API key is not configured")

    # 为什么：把一批文本一次性发给接口，由远端 GPU 模型把它们变成向量，再逐个校验维度。
    with httpx.Client(timeout=60) as client:
        response = client.post(
            f"{base_url}/embeddings",  # 标准 embeddings 端点
            headers={"Authorization": f"Bearer {api_key}"},
            json=_embedding_payload(texts, dimensions, model),
        )
        response.raise_for_status()  # 非 2xx 直接抛错
        # 接口返回可能有乱序，按 index 排回原顺序，保证「第 i 个向量对应第 i 段文本」
        rows = sorted(response.json().get("data", []), key=lambda item: int(item.get("index", 0)))
        vectors = [item.get("embedding") for item in rows]
        if len(vectors) != len(texts):
            raise ValueError("embedding response length mismatch")  # 返回的向量数和输入数对不上
        return [
            _validate_vector_dimensions([float(value) for value in vector], dimensions, "openai_compatible")
            for vector in vectors
        ]


@lru_cache(maxsize=4)
def _local_sentence_transformer(model_name: str, device: str | None) -> Any:
    # 本地加载 SentenceTransformer 模型，并用 lru_cache 缓存，避免重复加载耗时。
    # 为什么：模型加载很重（几百 MB），缓存起来同一模型只加载一次。
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:  # pragma: no cover - optional local dependency
        raise RuntimeError(
            "sentence-transformers is not installed; install backend/requirements-local-embedding.txt"
        ) from exc

    kwargs = {"device": device} if device else {}
    return SentenceTransformer(model_name, **kwargs)


def _embed_local(
    texts: list[str],
    dimensions: int,
    *,
    model_name: str | None = None,
    is_query: bool = False,
) -> list[list[float]]:
    # 用本地 SentenceTransformer 模型把文本转向量（CPU / 本地 GPU 跑，不联网）。
    resolved_model = model_name or settings.local_embedding_model
    model = _local_sentence_transformer(resolved_model, settings.local_embedding_device)
    encode_kwargs: dict[str, Any] = {}
    # Qwen3 embedding 模型区分「建库文本」和「查询」，查询时加 prompt 能提升召回
    if is_query and "qwen3-embedding" in resolved_model.lower():
        encode_kwargs["prompt_name"] = "query"
    # 批量编码并归一化；convert_to_numpy 得到 numpy 数组
    vectors = model.encode(
        texts,
        batch_size=32,
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
        **encode_kwargs,
    )
    output: list[list[float]] = []
    for vector in vectors:
        values = vector.tolist() if hasattr(vector, "tolist") else list(vector)
        output.append(_validate_vector_dimensions([float(value) for value in values], dimensions, "local"))
    return output


def embed_texts(texts: list[str], dimensions: int | None = None) -> list[list[float]]:
    # 对外便捷函数：用全局配置的 provider 给一批文本生成向量。
    dimensions = dimensions or settings.embedding_dimensions
    return embed_knowledge_texts(
        texts,
        provider=settings.embedding_provider,
        model=settings.embedding_model,
        dimensions=dimensions,
    )


def embed_text(text: str, dimensions: int | None = None) -> list[float]:
    # 单条文本的便捷封装：包一层成列表，取结果第一个向量。
    return embed_texts([text], dimensions=dimensions)[0]


def embed_knowledge_texts(
    texts: list[str],
    *,
    provider: str,
    model: str,
    dimensions: int,
    is_query: bool = False,
) -> list[list[float]]:
    # 核心分发函数：先校验向量合约，再根据 provider 选择具体实现去生成向量。
    # 在 RAG 流水线里的位置：把「切块后的文本」变成「向量」。
    # 输入：文本列表、以及用哪个模型 / 维度。
    # 输出：与输入一一对应的向量列表（每条文本一串浮点数）。
    clean_texts = [text or "" for text in texts]  # 把 None 兜底成空串，避免接口报错
    if not clean_texts:
        return []  # 空输入直接返回空
    validate_embedding_contract(provider, model, dimensions)  # 先卡死「向量空间一致性」
    resolved = normalize_embedding_provider(provider)
    if resolved in {"qwen_local", "local"}:
        return _embed_local(clean_texts, dimensions, model_name=model, is_query=is_query)  # 本地模型
    if resolved == "dashscope":
        return _embed_openai_compatible(
            clean_texts,
            dimensions,
            model=model,
            api_key=settings.dashscope_api_key,
            base_url=settings.dashscope_embedding_base_url,  # 阿里云百炼
        )
    if resolved == "openai_compatible":
        return _embed_openai_compatible(clean_texts, dimensions, model=model)  # 通用兼容接口
    if resolved == "deterministic":
        return [deterministic_embedding(text, dimensions) for text in clean_texts]  # 本地哈希兜底
    raise ValueError(f"Unsupported embedding provider: {resolved}")


def embed_knowledge_query(text: str, *, provider: str, model: str, dimensions: int) -> list[float]:
    # 专门给「用户查询」生成向量，并标记 is_query=True。
    # 为什么：很多模型对「查询」和「文档」用不同方式编码，查得好才能和文档向量正确比对。
    return embed_knowledge_texts(
        [text],
        provider=provider,
        model=model,
        dimensions=dimensions,
        is_query=True,
    )[0]
