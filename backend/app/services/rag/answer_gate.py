"""DevMind AI module."""

import re
from typing import Any


# 这些中文词被认为「指代不清」：光看词本身不知道对象是谁。
# 比如用户问「这个怎么办」「那个禁止吗」，系统无法定位到具体文件 / 版本 / 错误，需要追问。
AMBIGUOUS_MARKERS = ("这个", "那个", "它", "这段", "这里", "怎么办", "怎么处理")
# 冲突极性表：某些主题存在「正反两面」的关键词。
# 每个元组 = (冲突主题, 正面关键词, 反面关键词)，用于检测高排名证据是否互相矛盾。
CONFLICT_POLARITIES = (
    ("permission", ("允许", "可以", "enabled", "enable"), ("禁止", "不允许", "不能", "disabled", "disable")),  # 权限类：允许 vs 禁止
    ("outcome", ("成功", "通过", "success", "passed"), ("失败", "未通过", "failure", "failed")),               # 结果类：成功 vs 失败
    ("requirement", ("必须", "需要", "required"), ("可选", "无需", "optional")),                                 # 要求类：必须 vs 可选
)


def _normalise(text: str) -> str:
    # 把文本转小写、折叠所有空白成一个空格，便于后面做关键词 / 子串匹配。
    return " ".join(str(text or "").lower().split())


def _evidence_text(item: dict[str, Any]) -> str:
    # 从一条检索结果里抽出「能代表它的文字」拼成一个串，用于冲突 / 信号检测。
    metadata = item.get("metadata") or {}
    return _normalise(
        "\n".join(
            [
                str(item.get("title") or ""),                                  # 标题
                str(item.get("snippet") or item.get("_raw_content") or ""),     # 摘要或原文
                str(metadata.get("path") or ""),                               # 文件路径
                str(metadata.get("symbol") or ""),                             # 代码符号名
                str(metadata.get("branch") or metadata.get("base_branch") or ""),  # 分支名
            ]
        )
    )


def _strong_query_signals(query: str) -> list[str]:
    # 从用户问题里提取「强信号」：能唯一定位对象的词，比如文件路径、类名、错误名、Issue 编号。
    # 类比：就像警察办案需要「具体线索」，光说「它」没用，得有「LoginError」「#123」「auth.py」这种硬信息。
    candidates = re.findall(r"[A-Za-z][A-Za-z0-9_./\-]*|#\d+|\b\d{3,}\b", query)
    signals: list[str] = []
    for candidate in candidates:
        # 含有分隔符、含有数字、或以 Error/Exception 结尾的候选，才视为「强信号」
        if (
            any(marker in candidate for marker in ("_", ".", "/", "-"))
            or any(char.isdigit() for char in candidate)
            or candidate.endswith(("Error", "Exception"))
        ):
            signals.append(candidate.lower())
    return list(dict.fromkeys(signals))  # 去重保序


def _detect_conflict(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    # 检测高排名证据之间是否存在「相反结论」的冲突。
    # 只看分数 >= 0.6 的高置信证据；低于这个分数的噪声不参与冲突判断。
    high_confidence = [item for item in items if float(item.get("score") or 0.0) >= 0.6]
    if len(high_confidence) < 2:
        return None  # 证据不足两条，谈不上互相冲突
    # 把每条高置信证据的 id 和文字准备好
    texts = [(str(item.get("id") or item.get("source_id") or ""), _evidence_text(item)) for item in high_confidence]
    for conflict_type, positive_markers, negative_markers in CONFLICT_POLARITIES:
        # 找出文本里出现「反面关键词」的证据
        negative_ids = [item_id for item_id, text in texts if any(marker in text for marker in negative_markers)]
        # 找出文本里出现「正面关键词」且没出现反面关键词的证据（纯正面）
        positive_ids = [
            item_id
            for item_id, text in texts
            if any(marker in text for marker in positive_markers)
            and not any(marker in text for marker in negative_markers)
        ]
        # 如果同时有纯正面和纯反面的证据，且不是同一批，就判定为冲突
        if positive_ids and negative_ids and set(positive_ids) != set(negative_ids):
            return {
                "type": conflict_type,                       # 冲突主题（如 permission）
                "positive_evidence_ids": positive_ids,        # 支持「正面」结论的证据
                "negative_evidence_ids": negative_ids,        # 支持「反面」结论的证据
            }
    return None


def _is_ambiguous_query(query: str, items: list[dict[str, Any]]) -> bool:
    # 判断问题是否「指代不清、缺少可定位对象」。
    compact = re.sub(r"\s+", "", query)  # 去掉空格后判断更简单
    if not any(marker in compact for marker in AMBIGUOUS_MARKERS):
        return False  # 问题里根本没有指代代词，不算模糊
    # 如果命中了指代词，再看：涉及的来源不止一个，或问题极短（<=8 字），就认为需要追问
    source_ids = {str(item.get("source_id") or "") for item in items if item.get("source_id")}
    return len(source_ids) > 1 or len(compact) <= 8


def evaluate_answer_gate(
    query: str,
    items: list[dict[str, Any]],
    *,
    score_threshold: float = 0.0,
    threshold_enabled: bool = True,
) -> dict[str, Any]:
    # 「答案闸门」：在真正交给 LLM 生成答案之前，先判断「现在该不该回答 / 能不能回答」。
    # 它像一个安检门，决定放行（answer）、退回追问（ask_clarification）、提示冲突（conflict）或资料不足（insufficient_evidence）。
    # 为什么：防止 LLM 在证据不足、问题模糊、证据自相矛盾时硬编答案，从源头降低幻觉。
    # 输入：用户问题、检索到的证据列表、分数门槛配置。
    # 输出：一个决策字典，含 decision（决策）、reason（原因）等。
    # 先算出当前最高证据分数，作为后续判断的基线
    top_score = max((float(item.get("score") or 0.0) for item in items), default=0.0)
    base = {
        "evidence_count": len(items),                                                       # 拿到了几条证据
        "top_score": round(top_score, 4),                                                   # 最高分
        "score_threshold": round(float(score_threshold), 4) if threshold_enabled else None,  # 生效的门槛
    }
    if not items:
        # 一条证据都没有，肯定没法回答
        return {**base, "decision": "insufficient_evidence", "reason": "没有检索到满足条件的证据"}
    if threshold_enabled and top_score < score_threshold:
        # 即使有证据，但最高分都没达到门槛，说明相关性太弱，也不回答
        return {**base, "decision": "insufficient_evidence", "reason": "最高证据分数未达到回答门槛"}

    # 提取问题里的强信号，并检查这些关键名称是否真的出现在证据中
    signals = _strong_query_signals(query)
    evidence_text = "\n".join(_evidence_text(item) for item in items)
    # 找出「问题里有、但证据里完全没出现」的信号
    missing_signals = [signal for signal in signals if signal not in evidence_text]
    if signals and len(missing_signals) == len(signals):
        # 所有关键名称在证据里都查无此人，说明检索到的不是用户想问的东西
        return {
            **base,
            "decision": "insufficient_evidence",
            "reason": "查询中的关键名称没有出现在候选证据中",
            "required_signals": signals,        # 用户问题里需要的关键名称
            "missing_signals": missing_signals,  # 证据里缺失的
        }

    # 检查高排名证据之间是否互相矛盾
    conflict = _detect_conflict(items)
    if conflict:
        # 证据打架，不能给出确定结论，先把矛盾亮出来让用户去核对
        return {**base, "decision": "conflict", "reason": "高排名证据给出了相反结论", "conflict": conflict}

    if _is_ambiguous_query(query, items):
        # 问题指代不清，反问用户要更明确的对象 / 范围
        return {
            **base,
            "decision": "ask_clarification",
            "reason": "问题缺少可定位的对象或范围",
            "suggested_fields": ["文件或模块", "错误名称", "分支或版本"],  # 建议用户补充什么
        }

    # 一路过关，证据充分、关键名称可核对、无冲突、问题明确 → 放行回答
    return {
        **base,
        "decision": "answer",
        "reason": "证据达到分数门槛，关键名称可以在候选中核对",
        "matched_signals": [signal for signal in signals if signal in evidence_text],  # 证据中确实命中的关键名称
    }


def gate_response_message(gate: dict[str, Any]) -> str:
    # 把闸门的决策翻译成一句给用户看的「温柔提示」，而不是直接把内部 reason 甩出去。
    decision = str(gate.get("decision") or "insufficient_evidence")
    if decision == "ask_clarification":
        return "当前问题的对象或范围不够明确。请补充文件、模块、错误名称、分支或版本后再试。"
    if decision == "conflict":
        return "当前知识库中的高排名证据存在冲突，暂时无法给出确定结论。请先核对对应来源或版本。"
    # 默认兜底：资料不足
    return "当前知识库资料不足，无法确认。请补充相关资料、检查检索范围，或换一种更具体的问法。"
