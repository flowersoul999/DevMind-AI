"""DevMind AI module."""

import math
import re
from datetime import datetime, timezone
from typing import Any

from app.core.config import settings


def _tokens(text: str) -> list[str]:
    # 分词：英文按词、中文按单字 + 相邻双字。重排阶段用同一套分词保证术语一致。
    lowered = text.lower()
    tokens = re.findall(r"[a-z0-9_\-]+", lowered)
    for segment in re.findall(r"[\u4e00-\u9fff]+", lowered):
        tokens.extend(segment)
        tokens.extend(segment[index : index + 2] for index in range(max(0, len(segment) - 1)))
    return tokens


def _normalise(value: float) -> float:
    # 把任何数字夹到 [0, 1] 区间，非数 / 无穷都归零，避免污染排序分数。
    if math.isnan(value) or math.isinf(value):
        return 0.0
    return max(0.0, min(1.0, value))


def _candidate_text(candidate: dict[str, Any]) -> str:
    # 拼出一条候选「可检索文字」（标题 + 路径 + 摘要），用于重排时计算关键词重合。
    metadata = candidate.get("metadata") or {}
    path = str(metadata.get("path") or "")
    return "\n".join(
        [
            str(candidate.get("title") or ""),
            path,
            str(candidate.get("snippet") or ""),
        ]
    ).lower()


def _freshness_bonus(metadata: dict[str, Any]) -> float:
    # 新鲜度奖励：资料越新，重排时略加权（30 天内 +0.04，半年内 +0.02）。
    # 为什么：Issue / PR 这类时效性内容，越近的越可能反映当前状态。
    for key in ("updated_at", "created_at", "merged_at", "closed_at"):
        value = metadata.get(key)
        if not value:
            continue
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        age_days = max(0, (datetime.now(timezone.utc) - parsed).days)
        if age_days <= 30:
            return 0.04
        if age_days <= 180:
            return 0.02
        return 0.0
    return 0.0


def _heuristic_score(query: str, candidate: dict[str, Any]) -> tuple[float, str]:
    # 「启发式重排」：不调外部模型，用一堆规则分数给候选打分并给出可读理由。
    # 类比：像一个老编辑，根据「标题有没有命中、路径对不对、是不是两种检索都召回、够不够新」等给稿子排座次。
    terms = _tokens(query)
    if not terms:
        # 问题没析出关键词（比如纯标点），只能退回原始分数
        return _normalise(float(candidate.get("score") or 0.0)), "base score only"
    text = _candidate_text(candidate)
    title = str(candidate.get("title") or "").lower()
    metadata = candidate.get("metadata") or {}
    path = str(metadata.get("path") or "").lower()
    matched = [term for term in terms if term in text]            # 在整段文字里命中的词
    coverage = len(set(matched)) / max(len(set(terms)), 1)        # 覆盖率：命中词数 / 总词数
    title_hits = sum(1 for term in terms if term in title)        # 标题命中数
    path_hits = sum(1 for term in terms if term in path)          # 路径命中数
    phrase_bonus = 0.12 if query.lower().strip() and query.lower().strip() in text else 0.0  # 整句短语命中额外加分
    source_bonus = 0.08 if len({term for term in terms if term in text}) >= 2 else 0.0        # 命中 >=2 个词加分
    base = _normalise(float(candidate.get("score") or 0.0))       # 召回阶段综合分
    keyword = _normalise(float(candidate.get("keyword_score") or 0.0))  # BM25 关键词分
    vector = _normalise(float(candidate.get("vector_score") or 0.0))    # 向量语义分
    sources = set((candidate.get("retrieval") or {}).get("sources") or [])
    dual_recall_bonus = 0.05 if {"vector", "keyword"}.issubset(sources) else 0.0  # 双路都召回：说明很相关
    freshness_bonus = _freshness_bonus(metadata)                  # 新鲜度
    source_type = str(candidate.get("source_type") or "").lower()
    source_type_bonus = 0.03 if source_type and any(term in source_type for term in terms) else 0.0  # 来源类型名也命中
    # 把各分项按权重加起来，得到一个综合重排分
    score = (
        0.31 * base
        + 0.20 * vector
        + 0.16 * keyword
        + 0.17 * coverage
        + 0.04 * min(title_hits, 3) / 3
        + 0.02 * min(path_hits, 3) / 3
        + phrase_bonus
        + source_bonus
        + dual_recall_bonus
        + freshness_bonus
        + source_type_bonus
    )
    reasons = []
    if coverage:
        reasons.append(f"matched {len(set(matched))}/{len(set(terms))} query terms")
    if title_hits:
        reasons.append("title match")
    if path_hits:
        reasons.append("path match")
    if vector:
        reasons.append("vector candidate")
    if keyword:
        reasons.append("keyword candidate")
    if dual_recall_bonus:
        reasons.append("dual recall")
    if freshness_bonus:
        reasons.append("fresh evidence")
    if source_type_bonus:
        reasons.append("source type match")
    return _normalise(score), ", ".join(reasons) or "弱关键词匹配"


def rerank_documents(query: str, candidates: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    # 启发式重排入口：只重排前 top_n 个候选（其余保持原序追加在后面，节省算力）。
    # 为什么：真正需要精细排序的只有「可能进答案」的前几名，尾部候选没必要逐个算。
    if not candidates:
        return []
    # top_n：取「配置的重排上限」和「limit」中较大者，但不超出候选总数
    top_n = min(max(settings.rag_rerank_top_n, limit), len(candidates))
    head = candidates[:top_n]    # 需要重排的头部
    tail = candidates[top_n:]    # 不参与重排的尾部
    ranked: list[dict[str, Any]] = []
    for candidate in head:
        # 对每条头部候选算启发式分 + 解释
        heuristic_score, heuristic_reason = _heuristic_score(query, candidate)
        ranked.append(
            {
                **candidate,
                "score": round(heuristic_score, 4),
                "rerank_score": round(heuristic_score, 4),  # 重排分单独存一份
                "rank_reason": heuristic_reason,            # 人能看懂的「为什么这么排」
                "retrieval": {
                    **(candidate.get("retrieval") or {}),
                    "rerank_mode": "heuristic",             # 标记这是启发式重排
                    "rerank_score": round(heuristic_score, 4),
                    "rank_reason": heuristic_reason,
                },
            }
        )
    # 按重排分（并列时参考融合分）从高到低排序
    ranked.sort(key=lambda item: (float(item.get("rerank_score") or item.get("score") or 0.0), float(item.get("combined_score") or 0.0)), reverse=True)
    return [*ranked, *tail][:limit]  # 头部重排结果 + 尾部，再截断到 limit
