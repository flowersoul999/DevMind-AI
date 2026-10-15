"""DevMind AI module."""

import importlib.util
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import KnowledgeBaseConfig, RetrievalTestRun
from app.services.context_compression import compress_retrieval_results, estimate_tokens
from app.services.rag.answer_gate import evaluate_answer_gate
from app.services.rag.embeddings import (
    embed_knowledge_query,
    embedding_contract,
    normalize_embedding_provider,
    validate_embedding_contract,
)
from app.services.rag.rerank import rerank_documents
from app.services.rag.retrieval import expand_parent_context, search_similar_documents
from app.services.rag.source_policy import normalize_source_types, should_vectorize


def as_uuid(value: str | uuid.UUID) -> uuid.UUID:
    # 把字符串 / UUID 统一成真正的 uuid.UUID（本模块自己的小工具，与 vector_store 同名但不冲突）。
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def get_or_create_config(db: Session, repo_id: str | uuid.UUID, *, commit: bool = True) -> KnowledgeBaseConfig:
    # 取得（或创建）某个仓库的「知识库配置」：embedding provider / model / 维度等。
    # 为什么：每个仓库复用全局 embedding 合约，首次访问时懒创建一份配置行。
    repo_uuid = as_uuid(repo_id)
    config = db.query(KnowledgeBaseConfig).filter(KnowledgeBaseConfig.repo_id == repo_uuid).one_or_none()
    if config is None:
        contract = embedding_contract()  # 取全局合约作为默认
        config = KnowledgeBaseConfig(
            repo_id=repo_uuid,
            embedding_provider=contract["provider"],
            embedding_model=contract["model"],
            embedding_dimensions=contract["dimensions"],
        )
        db.add(config)
        db.flush()
        if commit:
            db.commit()
            db.refresh(config)
    # 创建后也要校验它仍符合全局合约（防止手改数据库导致向量空间不一致）
    validate_embedding_contract(
        config.embedding_provider,
        config.embedding_model,
        config.embedding_dimensions,
    )
    return config


def config_dict(config: KnowledgeBaseConfig) -> dict[str, Any]:
    # 把配置对象拍平成字典，方便塞进 trace / 响应里。
    return {
        "embedding_provider": config.embedding_provider,
        "embedding_model": config.embedding_model,
        "embedding_dimensions": config.embedding_dimensions,
        "retrieval_method": config.retrieval_method,
        "rerank_enabled": config.rerank_enabled,
        "rerank_provider": config.rerank_provider,
        "rerank_model": config.rerank_model,
        "top_k": config.top_k,
        "score_threshold_enabled": config.score_threshold_enabled,
        "score_threshold": config.score_threshold,
        "chunk_size": config.chunk_size,
        "chunk_overlap": config.chunk_overlap,
    }


def model_catalog() -> dict[str, Any]:
    # 「模型目录」：告知前端当前有哪些 embedding / rerank 模型可用、是否就绪、推荐哪个。
    sentence_transformers_ready = importlib.util.find_spec("sentence_transformers") is not None  # 本地模型依赖是否装了
    qwen_api_ready = bool((settings.rerank_api_key or settings.dashscope_api_key) and settings.rerank_base_url)
    contract = embedding_contract()
    provider = normalize_embedding_provider(str(contract["provider"]))
    # 根据 provider 类型判断 embedding 是否「可用」
    if provider in {"qwen_local", "local"}:
        embedding_available = sentence_transformers_ready
    elif provider == "dashscope":
        embedding_available = bool(settings.dashscope_api_key)
    elif provider == "openai_compatible":
        embedding_available = bool(settings.embedding_api_key or settings.llm_api_key)
    else:
        embedding_available = provider == "deterministic"  # 哈希兜底永远可用
    return {
        "embedding_models": [
            {
                "provider": provider,
                "model": contract["model"],
                "label": f"{contract['model']}（固定向量空间）",
                "dimensions": contract["dimensions"],
                "available": embedding_available,
                "recommended": True,
                "fallback": None,
            },
        ],
        "rerank_models": [
            {
                "provider": "qwen_api",
                "model": "qwen3-rerank",
                "label": "Qwen3-Rerank（百炼 API）",
                "available": qwen_api_ready,
                "recommended": True,
                "fallback": "heuristic",  # 不可用时退回本地启发式重排
            },
            {
                "provider": "heuristic",
                "model": "heuristic-reranker",
                "label": "本地启发式重排",
                "available": True,  # 纯本地规则，永远可用
                "recommended": False,
                "fallback": None,
            },
        ],
        "deepseek": {
            "embedding_available": False,
            "rerank_available": False,
            "note": "DeepSeek 官方 API 当前未提供 Embedding 或 Rerank 接口。",
        },
    }


def runtime_status(config: KnowledgeBaseConfig) -> dict[str, Any]:
    # 结合全局目录和本仓库配置，给出 embedding / rerank 当前是否可用。
    catalog = model_catalog()
    # 在目录里找到与配置匹配的 embedding 模型项
    embedding = next(
        (
            item
            for item in catalog["embedding_models"]
            if item["provider"] == config.embedding_provider and item["model"] == config.embedding_model
        ),
        None,
    )
    # 同理找 rerank 模型项
    rerank = next(
        (
            item
            for item in catalog["rerank_models"]
            if item["provider"] == config.rerank_provider and item["model"] == config.rerank_model
        ),
        None,
    )
    return {
        "embedding": {
            "provider": config.embedding_provider,
            "model": config.embedding_model,
            "dimensions": config.embedding_dimensions,
            "available": bool(embedding and embedding["available"]),
            "fallback": (embedding or {}).get("fallback"),
        },
        "rerank": {
            "enabled": config.rerank_enabled,
            "provider": config.rerank_provider,
            "model": config.rerank_model,
            "available": bool(rerank and rerank["available"]),
            "fallback": (rerank or {}).get("fallback"),
        },
    }


async def _qwen_rerank(
    query: str,
    items: list[dict[str, Any]],
    config: KnowledgeBaseConfig,
    limit: int,
) -> list[dict[str, Any]]:
    # 调用阿里云 Qwen3-Rerank 远程接口做精排（比本地启发式更准）。
    api_key = settings.rerank_api_key or settings.dashscope_api_key
    base_url = (settings.rerank_base_url or "").rstrip("/")
    if not api_key or not base_url:
        raise RuntimeError("Qwen rerank API is not configured")
    endpoint = base_url if base_url.endswith("/reranks") else f"{base_url}/reranks"  # 拼出 reranks 端点
    # 把每条候选拼成一段文本（标题 + 路径 + 正文/摘要），截断到 4000 字
    documents = [
        "\n".join(
            part
            for part in (
                str(item.get("title") or ""),
                str((item.get("metadata") or {}).get("path") or ""),
                str(item.get("_raw_content") or item.get("snippet") or ""),
            )
            if part
        )[:4000]
        for item in items
    ]
    # 异步发 HTTP 请求（因为是 async 函数，用 AsyncClient 不阻塞事件循环）
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            endpoint,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": config.rerank_model,
                "query": query,
                "documents": documents,
                "top_n": min(max(1, limit), len(documents)),  # 只要前 limit 个
                "instruct": settings.qwen_retrieval_instruction,  # 重排指令（让模型更懂「要排什么」）
            },
        )
        response.raise_for_status()
        payload = response.json()
    # 兼容两种返回结构
    rows = payload.get("results") or payload.get("output", {}).get("results") or []
    ranked: list[dict[str, Any]] = []
    for row in rows:
        index = int(row.get("index", -1))
        if index < 0 or index >= len(items):
            continue  # 越界忽略
        score = float(row.get("relevance_score") or row.get("score") or 0.0)
        item = items[index]  # 用返回的下标对应原候选
        ranked.append(
            {
                **item,
                "score": round(score, 4),
                "rerank_score": round(score, 4),
                "rank_reason": "qwen3-rerank",
                "retrieval": {
                    **(item.get("retrieval") or {}),
                    "rerank_mode": "qwen_api",
                    "rerank_model": config.rerank_model,
                    "rerank_score": round(score, 4),
                },
            }
        )
    return ranked


async def retrieve_knowledge(
    db: Session,
    repo_id: str | uuid.UUID,
    query: str,
    *,
    top_k: int | None = None,
    retrieval_method: str | None = None,
    rerank_enabled: bool | None = None,
    score_threshold_enabled: bool | None = None,
    score_threshold: float | None = None,
    source_types: list[str] | None = None,
    metadata_filters: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], int]:
    # 知识库「检索」总流程（异步）：召回 → 重排（Qwen 优先、失败退回启发式）→ 父上下文扩展 → 分数门槛 → 压缩 → 网关。
    # 在流水线里的位置：介于「用户提问」和「生成答案」之间，产出「可以喂给 LLM 的证据 + 全链路 trace」。
    # 输入：问题、仓库、top_k、检索模式、重排 / 门槛开关等。
    # 输出：(检索结果, 配置+trace 字典, 耗时毫秒)。
    started = time.perf_counter()
    config = get_or_create_config(db, repo_id)  # 取配置
    resolved_top_k = min(max(top_k or config.top_k, 1), 20)  # top_k 夹在 [1,20]
    method = retrieval_method or config.retrieval_method
    use_rerank = config.rerank_enabled if rerank_enabled is None else rerank_enabled
    threshold_on = config.score_threshold_enabled if score_threshold_enabled is None else score_threshold_enabled
    threshold = config.score_threshold if score_threshold is None else score_threshold

    requested_source_types = normalize_source_types(source_types)
    effective_source_types = requested_source_types or None
    # 判断是否需要先生成「查询向量」（只有向量 / 混合模式、且存在向量化来源时才需要）
    has_vector_sources = not effective_source_types or any(
        should_vectorize(source_type) for source_type in effective_source_types
    )
    query_embedding = None
    if method in {"vector", "hybrid"} and has_vector_sources:
        query_embedding = embed_knowledge_query(
            query,
            provider=config.embedding_provider,
            model=config.embedding_model,
            dimensions=config.embedding_dimensions,
        )
    # 召回阶段多拉候选：开重排就拉 8 倍，否则 2 倍，再夹到 [20,100]
    candidate_limit = min(max(resolved_top_k * (8 if use_rerank else 2), 20), 100)
    pipeline_trace: dict[str, Any] = {}
    items = search_similar_documents(
        db,
        repo_id,
        query,
        limit=candidate_limit,
        query_embedding=query_embedding,
        retrieval_method=method,
        apply_rerank=False,          # 这一步先不重排，后面单独处理（因为要选 Qwen 还是启发式）
        apply_compression=False,     # 压缩也放后面统一做
        source_types=effective_source_types,
        metadata_filters=metadata_filters,
        pipeline_trace=pipeline_trace,
    )
    rerank_stage: dict[str, Any] = {
        "name": "rerank",
        "input_count": len(items),
        "requested": "qwen_api" if use_rerank and config.rerank_provider == "qwen_api" else "heuristic",
    }
    # 重排阶段：优先用 Qwen API，任何异常自动退回本地启发式重排
    if use_rerank and items:
        if config.rerank_provider == "qwen_api":
            try:
                items = await _qwen_rerank(query, items, config, resolved_top_k)
                if not items:
                    raise RuntimeError("Qwen rerank returned no valid results")
                rerank_stage.update({"mode": "qwen_api", "fallback": False})
            except Exception as exc:
                # 远端失败 → 退回本地启发式，并记录失败原因
                items = rerank_documents(query, items, resolved_top_k)
                rerank_stage.update(
                    {
                        "mode": "heuristic",
                        "fallback": True,
                        "fallback_reason": type(exc).__name__,
                    }
                )
        else:
            items = rerank_documents(query, items, resolved_top_k)
            rerank_stage.update({"mode": "heuristic", "fallback": False})
    elif use_rerank:
        rerank_stage.update({"mode": "not_run", "fallback": False, "reason": "no candidates"})
    else:
        rerank_stage.update({"mode": "disabled", "fallback": False})
    items = items[:resolved_top_k]  # 最终截断到 top_k
    pipeline_trace.setdefault("stages", []).append({**rerank_stage, "output_count": len(items)})

    # 父上下文扩展：把命中碎片的相邻块拼进来
    items, parent_stage = expand_parent_context(db, repo_id, items, neighbor_window=1)
    pipeline_trace["stages"].append(parent_stage)

    before_threshold = len(items)
    if threshold_on:
        # 分数门槛：低于阈值的一律丢弃（防止太不相关的证据混进答案）
        items = [item for item in items if float(item.get("score") or 0.0) >= threshold]
    pipeline_trace["stages"].append(
        {
            "name": "score_threshold",
            "enabled": threshold_on,
            "threshold": threshold if threshold_on else None,
            "input_count": before_threshold,
            "output_count": len(items),
        }
    )

    # 上下文压缩：控制喂给 LLM 的 token 总量，避免超长
    context_budget_tokens = min(1800, max(520, resolved_top_k * 260))
    compression_input_count = len(items)
    items = compress_retrieval_results(
        query,
        items,
        max_tokens_per_item=260,
        max_total_tokens=context_budget_tokens,
    )
    compressed_tokens = sum(estimate_tokens(str(item.get("snippet") or "")) for item in items)
    pipeline_trace["stages"].append(
        {
            "name": "context_compression",
            "input_count": compression_input_count,
            "output_count": len(items),
            "budget_tokens": context_budget_tokens,
            "selected_tokens": compressed_tokens,
        }
    )

    # 答案网关：判断这些证据够不够回答、有没有冲突 / 模糊
    gate = evaluate_answer_gate(
        query,
        items,
        score_threshold=threshold,
        threshold_enabled=threshold_on,
    )
    for item in items:
        retrieval = dict(item.get("retrieval") or {})
        retrieval["answer_gate"] = gate["decision"]
        retrieval["selected_for_context"] = gate["decision"] == "answer"  # 标记是否进入上下文
        item["retrieval"] = retrieval
    pipeline_trace["stages"].append({"name": "answer_gate", **gate})
    duration_ms = round((time.perf_counter() - started) * 1000)
    trace = {
        **config_dict(config),
        "top_k": resolved_top_k,
        "source_types": requested_source_types,
        "filters": {
            "repo_id": str(repo_id),
            "source_types": requested_source_types,
            "metadata": metadata_filters or {},
        },
        "runtime": runtime_status(config),
        "fusion_method": "weighted" if method == "hybrid" else method,
        "context_budget_tokens": context_budget_tokens,
        "answer_gate": gate,
        "pipeline": pipeline_trace,  # 全链路各阶段详情
    }
    return items, trace, duration_ms


def record_retrieval_test(
    db: Session,
    repo_id: str | uuid.UUID,
    query: str,
    retrieval_config: dict[str, Any],
    results: list[dict[str, Any]],
    duration_ms: int,
) -> RetrievalTestRun:
    # 把一次「检索测试」的结果持久化，方便前端对比不同检索配置的效果。
    run = RetrievalTestRun(
        repo_id=as_uuid(repo_id),
        query=query,
        retrieval_config=retrieval_config,
        results_json=results,
        duration_ms=duration_ms,
        created_at=datetime.now(timezone.utc),
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run
