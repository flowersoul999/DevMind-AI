"""DevMind AI module."""

import heapq
import hashlib
import math
import re
import uuid
from collections import Counter
from datetime import datetime, time, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import Document
from app.services.context_compression import compress_retrieval_results
from app.services.rag.rerank import rerank_documents
from app.services.rag.source_policy import RAG_SOURCE_TYPES, is_searchable_source, normalize_source_types
from app.services.rag.vector_store import MilvusUnavailableError, search_milvus_documents


# 混合检索的「融合权重」与各种阈值。类比：两个评委（向量 + 关键词）给候选打分后，按权重折中成总分。
BM25_K1 = 1.5                 # BM25 饱和参数：词频越大，单条词贡献增长越平缓
BM25_B = 0.75                 # BM25 长度归一参数：长文档 / 短文档之间的惩罚
VECTOR_FUSION_WEIGHT = 0.62   # 向量路分数的融合权重（偏重语义）
KEYWORD_FUSION_WEIGHT = 0.30  # 关键词路分数的融合权重
DUAL_RECALL_BONUS = 0.08      # 两路都召回时的额外加分（两边都觉得相关，更可信）
MAX_CHUNKS_PER_PARENT = 2     # 同一父片段（parent）最多保留几块子碎片，防止一个文档刷屏
NEAR_DUPLICATE_JACCARD = 0.9  # 近似重复的判定阈值：Jaccard 相似度 >=0.9 视为重复


def cosine_similarity(a: list[float], b: list[float]) -> float:
    # 余弦相似度：把两向量的点积除以各自长度，得到「方向接近度」（1 最像，0 无关）。
    # 类比：坐标向量之间的夹角余弦值，只看方向不看长度。
    if not a or not b:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / ((math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))) or 1.0)


def _as_uuid(value: str | uuid.UUID) -> uuid.UUID:
    # 字符串 / UUID 统一成真正的 uuid.UUID 对象。
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _parse_datetime(value: str | None, end_of_day: bool = False) -> datetime | None:
    # 把字符串（支持 "2024-01-31" 或带 T/Z 的 ISO）解析成带时区的 datetime；失败返回 None。
    if not value:
        return None
    try:
        if len(value) == 10:
            # 只有日期：默认归到当天 00:00（或 end_of_day 时 23:59:59）
            parsed_date = datetime.fromisoformat(value).date()
            return datetime.combine(parsed_date, time.max if end_of_day else time.min, tzinfo=timezone.utc)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))  # 末尾 Z 当 UTC
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _metadata_time(doc_meta: dict) -> datetime | None:
    # 从元数据里挑第一个可用的「时间」字段（更新/创建/合并/关闭），用于时间过滤。
    for key in ["updated_at", "created_at", "merged_at", "closed_at"]:
        parsed = _parse_datetime(doc_meta.get(key))
        if parsed:
            return parsed
    return None


def _matches_metadata(doc_meta: dict, filters: dict | None) -> bool:
    # 判断一篇文档是否符合调用方传来的「元数据过滤条件」（标签 / 状态 / 路径 / 时间等）。
    if not filters:
        return True  # 没过滤条件就全部通过

    label = filters.get("label")
    if label and label not in (doc_meta.get("labels") or []):
        return False  # 要求某个标签，但文档没有

    state = filters.get("state")
    if state and doc_meta.get("state") != state:
        return False  # 状态不匹配

    # 路径 / 前缀 / 模块 三类都归一到一个 path 关键词，要求出现在文件名里
    path = (filters.get("path") or filters.get("path_prefix") or filters.get("module") or "").lower()
    if path:
        files = [str(item).lower() for item in doc_meta.get("files") or []]
        if files and not any(path in filename for filename in files):
            return False  # 文档有文件清单但都不含该路径
        if not files and path not in " ".join(str(value).lower() for value in doc_meta.values()):
            return False  # 文档没文件清单，就得在其它字段里找到该路径

    # 时间范围过滤（start_date / updated_after 算起点，end_date / updated_before 算终点）
    start_at = _parse_datetime(filters.get("start_date") or filters.get("updated_after"))
    end_at = _parse_datetime(filters.get("end_date") or filters.get("updated_before"), end_of_day=True)
    doc_time = _metadata_time(doc_meta)
    if start_at and (doc_time is None or doc_time < start_at):
        return False
    if end_at and (doc_time is None or doc_time > end_at):
        return False

    # 其余自定义字段（如 branch / number 等）做精确匹配
    for key, value in filters.items():
        if key in {
            "label",
            "path",
            "path_prefix",
            "module",
            "start_date",
            "end_date",
            "updated_after",
            "updated_before",
        } or value in (None, ""):
            continue  # 上面已特殊处理，或空值跳过
        if key == "branch":
            # 分支字段可能叫 branch / base_branch / head_branch，任意一个匹配即可
            current = doc_meta.get("branch") or doc_meta.get("base_branch") or doc_meta.get("head_branch")
            if current != value:
                return False
            continue
        current = doc_meta.get(key)
        if isinstance(current, list):
            if value not in current:
                return False  # 列表字段：要求 value 在其中
        elif current != value:
            return False  # 标量字段：要求相等
    return True


def _tokens(text: str) -> list[str]:
    # 分词（与 embedding / rerank 同一套规则）：英文按词、中文按单字 + 相邻双字。
    lowered = text.lower()
    tokens = re.findall(r"[a-z0-9_\-]+", lowered)
    for segment in re.findall(r"[\u4e00-\u9fff]+", lowered):
        tokens.extend(segment)
        tokens.extend(segment[index : index + 2] for index in range(max(0, len(segment) - 1)))
    return tokens


def _clip(text: str, limit: int = 260) -> str:
    # 把文本压成带省略号的摘要（折叠多余空白 + 长度裁剪）。
    cleaned = " ".join((text or "").split())
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1].rstrip() + "…"


def _normalise_scores(items: list[dict[str, Any]], field: str) -> None:
    # 把某字段的分数做「最大值归一化」到 [0,1]，让不同来源的分数可比。
    if not items:
        return
    values = [max(0.0, float(item.get(field) or 0.0)) for item in items]
    max_value = max(values) or 1.0  # 防除零
    for item, value in zip(items, values):
        item[field] = round(value / max_value, 4)


def _document_result(doc: Document, score: float, source: str, score_field: str) -> dict[str, Any]:
    # 把 ORM 文档包装成「检索结果字典」，并附带分数、摘要、检索来源追踪信息。
    metadata = doc.meta or {}
    result = {
        "id": str(doc.id),
        "title": doc.title,
        "source_type": doc.source_type,
        "source_id": str(doc.source_id),
        "score": round(max(0.0, score), 4),
        "metadata": metadata,
        "snippet": _clip(doc.content),        # 摘要
        "_raw_content": doc.content,          # 完整正文（内部使用）
        score_field: round(max(0.0, score), 4),
        "retrieval": {"sources": [source], score_field: round(max(0.0, score), 4)},
    }
    return result


def _milvus_vector_candidates(
    repo_id: str | uuid.UUID,
    query: str,
    source_types: list[str],
    fetch_limit: int,
    metadata_filters: dict | None,
    query_embedding: list[float] | None = None,
) -> list[dict[str, Any]]:
    # 「向量召回」：调 Milvus 用查询向量找相似文档，再做元数据过滤 + 分数归一 + 排名记录。
    milvus_results = search_milvus_documents(
        repo_id,
        query,
        source_type=source_types[0] if len(source_types) == 1 else None,  # 单类型直接指定，多类型用 in
        source_types=source_types if len(source_types) > 1 else None,
        limit=fetch_limit,
        query_embedding=query_embedding,
    )
    candidates = []
    for item in milvus_results:
        if not _matches_metadata(item.get("metadata") or {}, metadata_filters):
            continue  # 不满足过滤条件就丢弃
        score = max(0.0, float(item.get("score") or 0.0))
        candidates.append(
            {
                **item,
                "id": str(item.get("id") or item.get("source_id") or ""),
                "score": score,
                "vector_score": score,        # 向量分数另存
                "vector_score_raw": score,    # 原始向量分数（未归一）
                "retrieval": {
                    "sources": ["vector"],
                    "vector_score_raw": score,
                    "vector_backend": "milvus",
                },
            }
        )
    _normalise_scores(candidates, "vector_score")  # 归一化向量分数
    for rank, item in enumerate(candidates, start=1):
        item["score"] = item["vector_score"]
        item["retrieval"]["vector_score"] = item["vector_score"]
        item["vector_rank"] = rank  # 记录向量路排名
        item["retrieval"]["vector_rank"] = rank
    return candidates


def _bm25_document_stats(doc: Document, query_terms: list[str]) -> dict[str, Any]:
    # 为一篇文档计算 BM25 所需的统计：把标题 / 路径 / 符号 / 标签 / 正文分字段按权重统计词频。
    # 为什么分字段加权：标题 / 符号里命中关键词，比正文里命中更有信息量，所以给更高权重。
    metadata = doc.meta or {}
    field_tokens = {
        "title": _tokens(doc.title or ""),
        "path": _tokens(str(metadata.get("path") or "")),
        "symbol": _tokens(str(metadata.get("symbol") or metadata.get("name") or "")),
        "labels": _tokens(" ".join(str(item) for item in metadata.get("labels") or [])),
        "content": _tokens(doc.content or ""),
    }
    field_weights = {"title": 4.0, "path": 3.0, "symbol": 4.5, "labels": 2.5, "content": 1.0}
    counters = {field: Counter(tokens) for field, tokens in field_tokens.items()}
    # 每个查询词在各字段加权后的总词频
    weighted_tf = {
        term: sum(float(counters[field].get(term, 0)) * field_weights[field] for field in field_tokens)
        for term in query_terms
    }
    matched_terms = [term for term in query_terms if weighted_tf.get(term, 0.0) > 0.0]
    document_length = max(1, sum(len(tokens) for tokens in field_tokens.values()))
    return {
        "doc": doc,
        "weighted_tf": weighted_tf,
        "matched_terms": matched_terms,
        "document_length": document_length,
    }


def _bm25_score(
    stats: dict[str, Any],
    *,
    document_count: int,
    document_frequency: Counter[str],
    average_document_length: float,
) -> float:
    # 经典 BM25 打分公式：IDF（词越稀有越重要）× 加权词频 / 长度归一。
    score = 0.0
    document_length = float(stats["document_length"])
    # 长度归一项：文档越长，词频贡献被压得越狠
    length_norm = BM25_K1 * (1.0 - BM25_B + BM25_B * document_length / max(average_document_length, 1.0))
    for term, weighted_tf in stats["weighted_tf"].items():
        if weighted_tf <= 0:
            continue
        df = float(document_frequency.get(term, 0))  # 含该词的文档数
        # IDF：越罕见（df 越小）值越大
        idf = math.log1p((document_count - df + 0.5) / (df + 0.5))
        score += idf * (weighted_tf * (BM25_K1 + 1.0)) / (weighted_tf + length_norm)
    return max(0.0, score)


def _keyword_candidates(
    db: Session,
    repo_id: str | uuid.UUID,
    query: str,
    source_types: list[str],
    fetch_limit: int,
    metadata_filters: dict | None,
) -> list[dict[str, Any]]:
    # 「关键词召回」：基于 BM25 在 Postgres 里做全文式检索（不依赖向量）。
    # 类比：像用搜索引擎的关键词匹配，能抓住「精确词面」相关性，弥补向量偶尔漏掉的精确匹配。
    terms = list(dict.fromkeys(_tokens(query)))  # 去重后的查询词
    if not terms:
        return []  # 没有可用关键词就不走这条路
    # 先按仓库 + 来源类型缩小范围
    q = db.query(Document).filter(
        Document.repo_id == _as_uuid(repo_id),
        Document.source_type.in_(source_types),
    )
    state = (metadata_filters or {}).get("state")
    if state:
        # 用了 JSON 字段里的 state 子字段做精确匹配
        q = q.filter(Document.meta["state"].as_string() == str(state))

    corpus: list[dict[str, Any]] = []
    document_frequency: Counter[str] = Counter()
    total_document_length = 0
    # yield_per(500)：分批从数据库取，避免一次性把全表载入内存
    for doc in q.yield_per(500):
        if not _matches_metadata(doc.meta or {}, metadata_filters):
            continue
        stats = _bm25_document_stats(doc, terms)
        corpus.append(stats)
        total_document_length += int(stats["document_length"])
        document_frequency.update(set(stats["matched_terms"]))  # 统计每个词出现在多少文档

    if not corpus:
        return []
    average_document_length = total_document_length / len(corpus)
    # 用最小堆只保留前 fetch_limit 个最高分候选，避免给全部文档排序
    top_candidates: list[tuple[float, int, dict[str, Any]]] = []
    for sequence, stats in enumerate(corpus):
        doc = stats["doc"]
        score = _bm25_score(
            stats,
            document_count=len(corpus),
            document_frequency=document_frequency,
            average_document_length=average_document_length,
        )
        if score <= 0:
            continue  # 没命中任何关键词就不入候选
        item = _document_result(doc, score, "keyword", "keyword_score")
        item["bm25_score"] = round(score, 4)
        item["bm25_matched_terms"] = list(stats["matched_terms"])
        item["retrieval"].update(
            {
                "keyword_backend": "bm25",
                "bm25_score": round(score, 4),
                "bm25_matched_terms": list(stats["matched_terms"]),
                "bm25_k1": BM25_K1,
                "bm25_b": BM25_B,
            }
        )
        entry = (score, sequence, item)
        if len(top_candidates) < fetch_limit:
            heapq.heappush(top_candidates, entry)  # 堆未满直接入
        elif score > top_candidates[0][0]:
            heapq.heapreplace(top_candidates, entry)  # 比堆顶高就替换（淘汰最低分）
    # 从堆里取出并按分数（并列用原顺序）从高到低排序
    candidates = [
        entry[2]
        for entry in sorted(top_candidates, key=lambda entry: (entry[0], entry[1]), reverse=True)
    ]
    _normalise_scores(candidates, "keyword_score")  # 归一化关键词分数
    for rank, item in enumerate(candidates, start=1):
        item["score"] = item["keyword_score"]
        item["retrieval"]["keyword_score"] = item["keyword_score"]
        item["keyword_rank"] = rank  # 记录关键词路排名
        item["retrieval"]["keyword_rank"] = rank
    return candidates


def _merge_candidates(vector_items: list[dict[str, Any]], keyword_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # 「混合融合」：把向量路和关键词路的候选合并，按 chunk_id 去重，并用加权公式算出综合分（RRF 思路的加权版）。
    # 类比：两个评委各给出排名和分数，这里把两路分数折中成综合分，两边都认的再加 bonus。
    merged: dict[str, dict[str, Any]] = {}
    for source_name, items in [("vector", vector_items), ("keyword", keyword_items)]:
        for item in items:
            metadata = item.get("metadata") or {}
            # 用 chunk_id（没有就用 id / 来源组合）作为去重键，确保同一碎片只留一份
            key = str(
                metadata.get("chunk_id")
                or item.get("id")
                or f"{item.get('source_type')}:{item.get('source_id')}:{item.get('title')}"
            )
            current = merged.get(key)
            if current is None:
                # 第一次见到：以这份为基准，并记录它来自哪路
                current = {**item, "retrieval": {**(item.get("retrieval") or {})}}
                current["retrieval"]["sources"] = list(dict.fromkeys(current["retrieval"].get("sources") or [source_name]))
                merged[key] = current
            else:
                # 已存在：取各分数较大的那个（两路互补），并合并来源、摘要、追踪信息
                current["vector_score"] = max(float(current.get("vector_score") or 0.0), float(item.get("vector_score") or 0.0))
                current["keyword_score"] = max(float(current.get("keyword_score") or 0.0), float(item.get("keyword_score") or 0.0))
                current["vector_score_raw"] = max(
                    float(current.get("vector_score_raw") or 0.0), float(item.get("vector_score_raw") or 0.0)
                )
                current["bm25_score"] = max(float(current.get("bm25_score") or 0.0), float(item.get("bm25_score") or 0.0))
                current["snippet"] = max([str(current.get("snippet") or ""), str(item.get("snippet") or "")], key=len)
                current["_raw_content"] = max(
                    [str(current.get("_raw_content") or ""), str(item.get("_raw_content") or "")], key=len
                )
                current["retrieval"]["sources"] = list(dict.fromkeys([*(current["retrieval"].get("sources") or []), source_name]))
                for trace_key, trace_value in (item.get("retrieval") or {}).items():
                    if trace_key == "sources":
                        continue
                    current["retrieval"][trace_key] = trace_value
    output = []
    for item in merged.values():
        vector_score = float(item.get("vector_score") or 0.0)
        keyword_score = float(item.get("keyword_score") or 0.0)
        # 两路都有分数 → 给额外 bonus（双路召回更可信）
        both_bonus = DUAL_RECALL_BONUS if vector_score and keyword_score else 0.0
        combined = min(1.0, VECTOR_FUSION_WEIGHT * vector_score + KEYWORD_FUSION_WEIGHT * keyword_score + both_bonus)
        item["combined_score"] = round(combined, 4)
        item["fusion_score"] = round(combined, 4)
        item["score"] = round(combined, 4)
        item["retrieval"].update(
            {
                "fusion_method": "weighted",
                "fusion_weights": {
                    "vector": VECTOR_FUSION_WEIGHT,
                    "keyword": KEYWORD_FUSION_WEIGHT,
                    "dual_recall_bonus": DUAL_RECALL_BONUS,
                },
                "dual_recall_bonus": both_bonus,
                "combined_score": item["combined_score"],
                "fusion_score": item["fusion_score"],
            }
        )
        output.append(item)
    output.sort(key=lambda item: float(item.get("combined_score") or 0.0), reverse=True)  # 按综合分降序
    for rank, item in enumerate(output, start=1):
        item["fusion_rank"] = rank  # 记录融合后排名
        item["retrieval"]["fusion_rank"] = rank
    return output


def _normalised_fingerprint(text: str) -> str:
    # 把文本归一（去非字母数字 / 中文 + 转小写）后取 SHA256，作为「内容指纹」用于去重。
    normalised = re.sub(r"[^a-z0-9_\-\u4e00-\u9fff]+", "", str(text or "").lower())
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest() if normalised else ""


def _candidate_term_set(item: dict[str, Any]) -> set[str]:
    # 取一条候选的分词集合，用于后面的 Jaccard 近似重复判定。
    text = str(item.get("_raw_content") or item.get("snippet") or "")
    return set(_tokens(text))


def _same_duplicate_scope(left: dict[str, Any], right: dict[str, Any]) -> bool:
    # 判断两条候选是否属于「同一去重范围」：同 parent 就比 parent；否则比来源类型 + 来源 id。
    # 为什么：去重只在「同一篇文档 / 同一来源」内部做，跨文档的内容相近不应被误删。
    left_meta = left.get("metadata") or {}
    right_meta = right.get("metadata") or {}
    left_parent = str(left_meta.get("parent_id") or "")
    right_parent = str(right_meta.get("parent_id") or "")
    if left_parent and right_parent:
        return left_parent == right_parent
    return (
        str(left.get("source_type") or "") == str(right.get("source_type") or "")
        and str(left.get("source_id") or "") == str(right.get("source_id") or "")
    )


def _deduplicate_candidates(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    # 去重：先按「指纹完全一致」精确去重，再按「Jaccard 相似度 >=0.9」近似去重。
    # 类比：像论文查重，完全相同的直接判重；几乎一样的（90% 词重合）也算重复，保留先到的那条。
    kept: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    fingerprints: dict[str, dict[str, Any]] = {}
    term_sets: dict[str, set[str]] = {}
    for item in items:
        item_id = str(item.get("id") or "")
        raw_text = str(item.get("_raw_content") or item.get("snippet") or "")
        fingerprint = _normalised_fingerprint(raw_text)
        # 指纹命中且在同一去重范围 → 视为精确重复
        duplicate_of: dict[str, Any] | None = fingerprints.get(fingerprint) if fingerprint else None
        if duplicate_of is not None and not _same_duplicate_scope(item, duplicate_of):
            duplicate_of = None  # 不在同一范围，不算重复
        if duplicate_of is None:
            # 没精确重复，再做近似重复比对（Jaccard）
            current_terms = _candidate_term_set(item)
            for existing in kept:
                if not _same_duplicate_scope(item, existing):
                    continue
                existing_id = str(existing.get("id") or "")
                existing_terms = term_sets.get(existing_id) or _candidate_term_set(existing)
                if not current_terms or not existing_terms:
                    continue
                union = current_terms | existing_terms
                similarity = len(current_terms & existing_terms) / max(len(union), 1)
                if similarity >= NEAR_DUPLICATE_JACCARD:
                    duplicate_of = existing
                    break
            term_sets[item_id] = current_terms
        if duplicate_of is not None:
            # 被判定为重复：记录到被移除列表，并在被保留的那条上登记「删掉了谁」
            duplicate_id = str(duplicate_of.get("id") or "")
            duplicate_trace = duplicate_of.setdefault("retrieval", {})
            duplicate_trace.setdefault("duplicates_removed", []).append(item_id)
            removed.append({"id": item_id, "duplicate_of": duplicate_id})
            continue
        kept.append(item)
        if fingerprint:
            fingerprints[fingerprint] = item
    return kept, removed


def _limit_parent_occupancy(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    # 控制「同一父片段」的子块数量（最多 MAX_CHUNKS_PER_PARENT 块），防止一个大文档的碎片占满结果。
    counts: Counter[str] = Counter()
    kept: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    for item in items:
        metadata = item.get("metadata") or {}
        parent_id = str(metadata.get("parent_id") or "")
        if parent_id and counts[parent_id] >= MAX_CHUNKS_PER_PARENT:
            removed.append({"id": str(item.get("id") or ""), "parent_id": parent_id})
            continue  # 超出限额就丢弃
        kept.append(item)
        if parent_id:
            counts[parent_id] += 1
    return kept, removed


def _same_version(left: dict[str, Any], right: dict[str, Any]) -> bool:
    # 判断两条元数据是否「同一个版本」：branch / commit_sha / version / checksum / 知识文档 id 都不能冲突。
    # 为什么：扩展上下文时只拼同一版本的同文档碎片，绝不允许把旧版和新版拼在一起。
    for key in ("branch", "commit_sha", "version", "checksum", "knowledge_document_id"):
        left_value = left.get(key)
        right_value = right.get(key)
        if left_value is not None and right_value is not None and left_value != right_value:
            return False
    return True


def expand_parent_context(
    db: Session,
    repo_id: str | uuid.UUID,
    items: list[dict[str, Any]],
    *,
    neighbor_window: int = 1,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    # 「父上下文扩展」：对每条命中的碎片，把它的「前后相邻碎片」一起拼进来，避免只给半截内容。
    # 类比：查字典查到某一页，顺便把上下几页也给你，信息才完整。
    expanded: list[dict[str, Any]] = []
    expanded_items = 0
    expanded_chunks = 0
    for item in items:
        metadata = dict(item.get("metadata") or {})
        parent_id = str(metadata.get("parent_id") or "")
        child_index = metadata.get("child_index")
        source_id = item.get("source_id")
        source_type = str(item.get("source_type") or "")
        retrieval = dict(item.get("retrieval") or {})
        if not parent_id or child_index is None or not source_id or not source_type:
            # 缺关键字段，无法扩展，原样保留并标记未扩展
            retrieval["parent_expansion"] = {"expanded": False, "reason": "parent metadata unavailable"}
            expanded.append({**item, "retrieval": retrieval})
            continue
        try:
            source_uuid = _as_uuid(source_id)
        except (TypeError, ValueError):
            retrieval["parent_expansion"] = {"expanded": False, "reason": "invalid source id"}
            expanded.append({**item, "retrieval": retrieval})
            continue
        # 查出同一 source_id 下的所有兄弟碎片
        siblings = (
            db.query(Document)
            .filter(
                Document.repo_id == _as_uuid(repo_id),
                Document.source_type == source_type,
                Document.source_id == source_uuid,
            )
            .all()
        )
        selected: list[Document] = []
        current_index = int(child_index)
        for sibling in siblings:
            sibling_meta = sibling.meta or {}
            if str(sibling_meta.get("parent_id") or "") != parent_id:
                continue  # 不是同一父片段
            if not _same_version(metadata, sibling_meta):
                continue  # 版本不同，跳过
            sibling_index = int(sibling_meta.get("child_index") or 0)
            # 只取相邻窗口内（±neighbor_window）的碎片
            if abs(sibling_index - current_index) <= max(0, neighbor_window):
                selected.append(sibling)
        selected.sort(key=lambda row: int((row.meta or {}).get("child_index") or 0))
        if not selected:
            retrieval["parent_expansion"] = {"expanded": False, "reason": "no matching siblings"}
            expanded.append({**item, "retrieval": retrieval})
            continue
        parts: list[str] = []
        seen_parts: set[str] = set()
        expanded_ids: list[str] = []
        for sibling in selected:
            content = str(sibling.content or "").strip()
            fingerprint = _normalised_fingerprint(content)
            if not content or fingerprint in seen_parts:
                continue  # 空内容或已出现过的就跳过
            seen_parts.add(fingerprint)
            parts.append(content)
            expanded_ids.append(str(sibling.id))
        if not parts:
            retrieval["parent_expansion"] = {"expanded": False, "reason": "empty sibling content"}
            expanded.append({**item, "retrieval": retrieval})
            continue
        # 是否真的扩展到了更多内容（超过原始那条）
        parent_expanded = len(expanded_ids) > 1 or str(item.get("id") or "") not in expanded_ids
        retrieval["parent_expansion"] = {
            "expanded": parent_expanded,
            "parent_id": parent_id,
            "chunk_ids": expanded_ids,
            "neighbor_window": max(0, neighbor_window),
        }
        next_metadata = {**metadata, "expanded_chunk_ids": expanded_ids}
        expanded.append(
            {
                **item,
                "metadata": next_metadata,
                "_raw_content": "\n\n".join(parts),  # 把相邻碎片拼成完整内容
                "parent_expanded": parent_expanded,
                "retrieval": retrieval,
            }
        )
        if parent_expanded:
            expanded_items += 1
            expanded_chunks += max(0, len(expanded_ids) - 1)
    return expanded, {
        "name": "parent_expansion",
        "input_count": len(items),
        "output_count": len(expanded),
        "expanded_items": expanded_items,
        "additional_chunks": expanded_chunks,
    }


def search_similar_documents(
    db: Session,
    repo_id: str | uuid.UUID,
    query: str,
    source_type: str | None = None,
    limit: int = 5,
    metadata_filters: dict | None = None,
    query_embedding: list[float] | None = None,
    retrieval_method: str = "hybrid",
    apply_rerank: bool = True,
    apply_compression: bool = True,
    source_types: list[str] | None = None,
    pipeline_trace: dict[str, Any] | None = None,
) -> list[dict]:
    # 检索主入口：串联「召回 → 融合 → 去重 → 聚合 → 重排 → 压缩」整条混合检索流水线。
    # 在 RAG 流水线里的位置：问答前最关键的一步——从海量文档里挑出最相关的几段交给 LLM。
    # 输入：问题、仓库、来源类型 / 过滤条件 / 检索模式 / 是否重排与压缩。
    # 输出：排好序、已去重、已上下文扩展、并（可选）压缩过的候选列表，外加可观测的 pipeline_trace。
    requested_types = normalize_source_types([source_type, *(source_types or [])]) or sorted(RAG_SOURCE_TYPES)
    # 只保留「可检索」的来源类型（排除 direct 类）
    searchable_types = list(dict.fromkeys(item for item in requested_types if is_searchable_source(item)))
    if not searchable_types:
        return []  # 没有可检索类型，直接空

    # 召回阶段多拉一些（limit 的倍数），给后续融合 / 重排留余量
    fetch_limit = min(max(limit * 8, 40), 200)
    method = retrieval_method.strip().lower()
    vector_types = searchable_types
    vector_candidates: list[dict[str, Any]] = []
    keyword_candidates: list[dict[str, Any]] = []
    stages: list[dict[str, Any]] = []
    vector_status = "skipped"
    # 1) 向量召回（仅 vector / hybrid 模式）
    if method in {"vector", "hybrid"} and vector_types:
        try:
            vector_candidates = _milvus_vector_candidates(
                repo_id,
                query,
                vector_types,
                fetch_limit,
                metadata_filters,
                query_embedding=query_embedding,
            )
            vector_status = "success"
        except MilvusUnavailableError:
            vector_status = "unavailable"
            if method == "vector":
                raise  # 纯向量模式下向量库挂了就没法回答，直接抛
    stages.append(
        {
            "name": "vector_recall",
            "status": vector_status,
            "candidate_count": len(vector_candidates),
            "backend": "milvus",
        }
    )
    # 2) 关键词召回（仅 fulltext / hybrid 模式）
    keyword_types = searchable_types if method in {"fulltext", "hybrid"} else []
    if keyword_types:
        keyword_candidates = _keyword_candidates(
            db,
            repo_id,
            query,
            keyword_types,
            fetch_limit,
            metadata_filters,
        )
    stages.append(
        {
            "name": "keyword_recall",
            "status": "success" if keyword_types else "skipped",
            "candidate_count": len(keyword_candidates),
            "backend": "bm25",
        }
    )
    # 3) 按模式选择结果：纯向量 / 纯关键词 / 混合融合
    if method == "vector":
        merged = vector_candidates
    elif method == "fulltext":
        merged = keyword_candidates
    else:
        merged = _merge_candidates(vector_candidates, keyword_candidates)
    stages.append(
        {
            "name": "weighted_fusion" if method == "hybrid" else "single_retriever_selection",
            "method": "weighted" if method == "hybrid" else method,
            "candidate_count": len(merged),
            "weights": {
                "vector": VECTOR_FUSION_WEIGHT,
                "keyword": KEYWORD_FUSION_WEIGHT,
                "dual_recall_bonus": DUAL_RECALL_BONUS,
            },
        }
    )
    # 4) 去重 + 父片段聚合（控制单文档占比）
    deduplicated, duplicates_removed = _deduplicate_candidates(merged)
    aggregated, parent_overflow_removed = _limit_parent_occupancy(deduplicated)
    stages.append(
        {
            "name": "deduplication_and_parent_aggregation",
            "input_count": len(merged),
            "output_count": len(aggregated),
            "duplicates_removed": duplicates_removed,
            "parent_overflow_removed": parent_overflow_removed,
        }
    )
    # 5) 重排（默认开启；关掉就直接按融合分截断）
    ranked = rerank_documents(query, aggregated, limit) if apply_rerank else aggregated[:limit]
    if apply_rerank:
        stages.append(
            {
                "name": "rerank",
                "input_count": len(aggregated),
                "output_count": len(ranked),
                "mode": ranked[0].get("retrieval", {}).get("rerank_mode") if ranked else None,
            }
        )
    if pipeline_trace is not None:
        # 把每一步的阶段信息回填到调用方传入的 trace 字典，便于前端 / 调试查看
        pipeline_trace.update(
            {
                "retrieval_method": method,
                "fusion_method": "weighted" if method == "hybrid" else method,
                "stages": stages,
            }
        )
    if not apply_compression:
        return ranked  # 不压缩就直接返回
    compressed = compress_retrieval_results(query, ranked, max_tokens_per_item=260)
    if pipeline_trace is not None:
        pipeline_trace["stages"].append(
            {
                "name": "context_compression",
                "input_count": len(ranked),
                "output_count": len(compressed),
            }
        )
    return compressed
