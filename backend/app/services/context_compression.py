"""上下文压缩与 token 预算管理（context_compression.py）。

本模块专门解决"聊天上下文越来越长、快要超出模型窗口"的问题，是记忆系统的算法核心。
它不碰数据库，纯做文本/token 计算，被 chat_memory.py 和 agents 调用。类比：聊天记录太长时，
把旧内容总结成几句话，只保留最近的关键对话，腾出空间给新消息。

关键能力：
- token 估算（estimate_tokens）：粗略但够用地估算一段文本占多少 token（中文按字、英文按词）。
- 模型上下文窗口查询（model_context_window_tokens 等）：按模型名查它"能装多少字"。
- 上下文压力分级（calculate_context_pressure）：把当前用量分成 normal/warning/auto_compact/aggressive/manual_path。
- 消息压缩（compress_messages / microcompact_messages）：保留精确后缀、截断中间、把可重算的工具结果清掉。
- 压缩摘要生成（build_compaction_summary_payload / format_compaction_summary / fallback_compaction_summary）：
  把一段消息交给 LLM 或兜底算法总结。
- 记忆合并与渲染（merge_memory / render_structured_memory / structured_memory_from_model）：维护结构化记忆。

与其他模块的关系：被 services/chat_memory.py 大量引用；提示词常量也被 chat_memory.py 导入。."""


# 标准库：json（序列化）、re（正则）、dataclass（数据结构）、typing（类型标注）
import json
import re
from dataclasses import dataclass
from typing import Any, Literal  # Literal：限定取值只能是列举出的几个字符串

# 项目配置（各种窗口大小、阈值、比例都从这里读）
from app.core.config import settings


# 分词正则：一个中文 = 1 token；一个英文/数字词 = 1 token；其它单字符 = 1 token
TOKEN_PATTERN = re.compile(r"[\u4e00-\u9fff]|[a-zA-Z0-9_\-]+|[^\s]")
# 取"词"的正则（中英文词），用于关键词匹配
WORD_PATTERN = re.compile(r"[a-zA-Z0-9_\-\u4e00-\u9fff]+")
# 句子切分正则：在句号/问号/换行等"句尾符号"后面断句（中英文标点都覆盖）
SENTENCE_SPLIT_PATTERN = re.compile(r"(?<=[.!?\n\u3002\uff01\uff1f])\s+")

# 各模型的最大"上下文窗口"（能塞进提示词的总 token 数）。为什么用字典：
# 不同模型窗口差异巨大（从 12.8 万到 100 万），需要按名字查。
MODEL_CONTEXT_WINDOWS = {
    "deepseek-v4-pro": 1_000_000,   # 下划线 1_000_000 只是可读性写法，等于 1000000
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4.1": 1_000_000,
    "gpt-4.1-mini": 1_000_000,
    "gpt-4.1-nano": 1_000_000,
    "gpt-5": 400_000,
    "gpt-5-mini": 400_000,
    "gpt-5-nano": 400_000,
    "o1": 200_000,
    "o1-mini": 128_000,
    "o3": 200_000,
    "o3-mini": 200_000,
    "o4-mini": 200_000,
    "claude-3-5-sonnet": 200_000,
    "claude-3-7-sonnet": 200_000,
    "claude-sonnet-4": 200_000,
    "claude-opus-4": 200_000,
}

# 各模型的"输出窗口"（模型一次最多能吐多少 token 的回答）
MODEL_OUTPUT_WINDOWS = {
    "deepseek-v4-pro": 384_000,
    "gpt-4o": 16_384,
    "gpt-4o-mini": 16_384,
    "gpt-4.1": 32_768,
    "gpt-4.1-mini": 32_768,
    "gpt-4.1-nano": 32_768,
    "gpt-5": 128_000,
    "gpt-5-mini": 128_000,
    "gpt-5-nano": 128_000,
    "o1": 100_000,
    "o1-mini": 65_536,
    "o3": 100_000,
    "o3-mini": 100_000,
    "o4-mini": 100_000,
    "claude-3-5-sonnet": 8_192,
    "claude-3-7-sonnet": 64_000,
    "claude-sonnet-4": 64_000,
    "claude-opus-4": 64_000,
}

# 可被"压缩掉"的工具提示名单：这些工具的输出（读文件/搜代码/CI 日志等）理论上能重新跑出来，
# 所以压缩时可以只留一句"可重算"，而不必保留完整输出。
COMPACTABLE_TOOL_HINTS = (
    "workspace.read_file",
    "workspace.search_code",
    "workspace.list_files",
    "rag.search_similar_documents",
    "devflow_search_evidence",
    "repo_health.generate_summary",
    "ci_debug_agent.analyze_workflow_run",
    "read",
    "grep",
    "search",
    "shell",
    "web_fetch",
    "webfetch",
)

# 压缩边界消息在 meta 里的子类型标识（统一常量，避免各处写错字符串）
COMPACT_BOUNDARY_SUBTYPE = "compact_boundary"
# 微压缩后替换工具输出的标记文本
MICROCOMPACT_MARKER = "[Old regenerable tool/context result cleared by microcompact]"


# 压力阶段类型：只能是这 5 个值之一（Literal 做编译期约束，写错值 IDE 会报错）
PressureStage = Literal["normal", "warning", "auto_compact", "aggressive", "manual_path"]


def estimate_tokens(text: str | None) -> int:
    # 估算 token 数：空文本返回 0；中文每字 1 token；英文词按 (长度+3)//4 估算（约 4 字符=1 token）；其它字符 1 token
    if not text:
        return 0
    total = 0
    for token in TOKEN_PATTERN.findall(text):
        if re.fullmatch(r"[\u4e00-\u9fff]", token):
            total += 1  # 中文
        elif re.fullmatch(r"[a-zA-Z0-9_\-]+", token):
            total += max(1, (len(token) + 3) // 4)  # 英文/数字词
        else:
            total += 1  # 标点等
    return total


def is_context_overflow_error(exc: Exception) -> bool:
    """识别各家大模型网关返回的"上下文溢出"错误。

    各 OpenAI 兼容网关没有统一的异常类型，所以这里只靠一串错误信息关键词来判定，
    并且只允许调用方重试一次（避免无限重试）。."""
    message = str(exc or "").lower()  # 异常信息转小写，便于不区分大小写匹配
    markers = (
        "context_length_exceeded",
        "maximum context length",
        "prompt is too long",
        "prompt too long",
        "context window",
        "too many tokens",
        "request too large",
    )
    # 只要错误信息里出现任一关键词，就认为是上下文溢出
    return any(marker in message for marker in markers)


def _normalize_model_name(model: str | None) -> str:
    # 把模型名规范成小写、去掉路径前缀和日期后缀，方便查表。
    # 为什么：模型名可能是 "openai/gpt-4o-2024-08-06" 这种带供应商和日期的形式，查表前要归一化。
    value = str(model or settings.llm_model or "").strip().lower()
    if not value:
        return "unknown"
    value = value.split("/")[-1]  # 只取最后一个 "/" 之后的部分（去掉供应商）
    value = re.sub(r"-(20\d{2}[-_]\d{2}[-_]\d{2}|\d{4}[-_]\d{2}[-_]\d{2})$", "", value)  # 去掉末尾日期
    return value


def _model_lookup(model: str | None, table: dict[str, int]) -> int | None:
    # 在模型窗口表里查值：先精确匹配，再按"前缀"匹配（如 "gpt-4o-xxx" 命中 "gpt-4o"）。
    normalized = _normalize_model_name(model)
    if normalized in table:
        return table[normalized]  # 精确命中
    for key, value in table.items():
        if normalized.startswith(key):
            return value  # 前缀命中
    return None  # 查不到返回 None


def model_context_window_tokens(model: str | None = None) -> int:
    # 返回模型"上下文窗口"的 token 数（优先用配置，其次查表，最后兜底算）。
    if settings.context_model_window_tokens:
        return max(4096, settings.context_model_window_tokens)  # 配置直接给了就用，且不小于 4096
    known = _model_lookup(model, MODEL_CONTEXT_WINDOWS)
    if known:
        return known  # 查表命中
    # 都没命中：用"最大输入 token + 预留输出 token"兜底
    return max(4096, settings.context_max_input_tokens + settings.context_reserved_response_tokens)


def model_output_window_tokens(model: str | None = None) -> int:
    # 返回模型"输出窗口"的 token 数（优先查表，没命中用配置或 512 兜底）
    known = _model_lookup(model, MODEL_OUTPUT_WINDOWS)
    if known:
        return known
    return max(512, settings.context_reserved_response_tokens)


def effective_context_window_tokens(model: str | None = None, *, for_compaction: bool = False) -> int:
    # 计算"实际可用上下文窗口"：总窗口减去给"模型回答"预留的空间。
    # for_compaction=True 时用压缩专用预留值，否则用普通预留值。
    model_window = model_context_window_tokens(model)
    if for_compaction:
        # 压缩场景：预留值取"输出窗口"和"压缩预留输出"的较小者，但不低于 512
        reserved = min(model_output_window_tokens(model), max(512, settings.context_compact_reserved_output_tokens))
    else:
        reserved = max(512, min(settings.context_reserved_response_tokens, model_window // 2))  # 普通：预留一半左右
    dynamic_window = max(4096, model_window - reserved)  # 总窗口减预留
    if settings.context_dynamic_model_window_enabled:
        # 开启动态窗口：取"配置上限"和"动态窗口"的较大者
        return max(settings.context_max_input_tokens, dynamic_window)
    # 否则限制在配置上限内
    return max(4096, min(settings.context_max_input_tokens, dynamic_window))


@dataclass(frozen=True)  # frozen=True：这个数据结构一旦创建就不可改（像只读记录）
class ContextPressure:
    # 上下文压力快照：描述当前用量处于哪个阶段、还剩多少 token 等。
    stage: PressureStage
    token_usage: int
    effective_window_tokens: int
    remaining_tokens: int
    warning_threshold: int
    auto_compact_threshold: int
    aggressive_threshold: int
    manual_threshold: int
    utilization: float  # 利用率（已用/总窗口），0~1

    @property
    def should_warn(self) -> bool:
        # 是不是该警告了：只要不是 normal 就该警告
        return self.stage != "normal"

    @property
    def should_auto_compact(self) -> bool:
        # 是不是该自动压缩：处于三个压缩阶段之一就触发
        return self.stage in {"auto_compact", "aggressive", "manual_path"}

    def as_dict(self) -> dict[str, Any]:
        # 转成普通字典，方便序列化（存数据库/返回 API）
        return {
            "stage": self.stage,
            "token_usage": self.token_usage,
            "effective_window_tokens": self.effective_window_tokens,
            "remaining_tokens": self.remaining_tokens,
            "warning_threshold": self.warning_threshold,
            "auto_compact_threshold": self.auto_compact_threshold,
            "aggressive_threshold": self.aggressive_threshold,
            "manual_threshold": self.manual_threshold,
            "utilization": self.utilization,
        }


def _thresholds_for_window(window: int) -> tuple[int, int, int, int]:
    # 根据窗口大小算出 4 个阈值（warning / auto / aggressive / manual）。阈值 = 窗口 - 缓冲。
    # 缓冲随窗口比例变化，但都有下限（避免窗口很小时缓冲过大）。
    warning_buffer = min(settings.context_warning_buffer_tokens, max(512, window // 4))
    auto_buffer = min(settings.context_auto_compact_buffer_tokens, max(512, window // 5))
    aggressive_buffer = min(settings.context_aggressive_buffer_tokens, max(512, window // 8))
    manual_buffer = min(settings.context_manual_buffer_tokens, max(256, window // 16))
    auto_threshold = max(1024, window - auto_buffer)        # 到这个用量就该自动压缩
    warning_threshold = max(512, min(auto_threshold - 1, auto_threshold - warning_buffer))  # 略低于 auto
    aggressive_threshold = max(auto_threshold + 1, window - aggressive_buffer)   # 更激进
    manual_threshold = max(aggressive_threshold + 1, window - manual_buffer)    # 最猛，需人工介入
    return warning_threshold, auto_threshold, aggressive_threshold, manual_threshold


def calculate_context_pressure(token_usage: int, model: str | None = None) -> ContextPressure:
    # 核心：根据"已用 token 数"算出当前处于哪个压力阶段（normal→warning→auto_compact→aggressive→manual_path）。
    effective_window = effective_context_window_tokens(model, for_compaction=True)
    warning_threshold, auto_threshold, aggressive_threshold, manual_threshold = _thresholds_for_window(effective_window)
    if token_usage >= manual_threshold:
        stage: PressureStage = "manual_path"  # 用量最大，需手动处理
    elif token_usage >= aggressive_threshold:
        stage = "aggressive"
    elif token_usage >= auto_threshold:
        stage = "auto_compact"  # 触发自动压缩
    elif token_usage >= warning_threshold:
        stage = "warning"
    else:
        stage = "normal"  # 正常，还很充裕
    return ContextPressure(
        stage=stage,
        token_usage=token_usage,
        effective_window_tokens=effective_window,
        remaining_tokens=max(0, effective_window - token_usage),  # 剩余 token（不为负）
        warning_threshold=warning_threshold,
        auto_compact_threshold=auto_threshold,
        aggressive_threshold=aggressive_threshold,
        manual_threshold=manual_threshold,
        utilization=round(token_usage / effective_window, 4) if effective_window else 0.0,  # 利用率保留 4 位小数
    )


def is_compact_boundary_message(message: dict[str, Any]) -> bool:
    # 判断一条消息是不是"压缩边界"：角色必须是 system，且 meta.subtype 等于标志常量
    meta = message.get("meta") or message.get("metadata") or {}
    return str(message.get("role") or "") == "system" and meta.get("subtype") == COMPACT_BOUNDARY_SUBTYPE


def find_last_compact_boundary_index(messages: list[dict[str, Any]]) -> int:
    # 从后往前找最近一条压缩边界的下标；没有就返回 -1
    for index in range(len(messages) - 1, -1, -1):
        if is_compact_boundary_message(messages[index]):
            return index
    return -1


def messages_after_compact_boundary(messages: list[dict[str, Any]], *, include_boundary: bool = True) -> list[dict[str, Any]]:
    # 返回"压缩边界之后"的消息（也就是尚未被摘要压缩的"活跃段"）。
    # include_boundary=True 时把边界那条本身也算进去。
    boundary_index = find_last_compact_boundary_index(messages)
    if boundary_index < 0:
        return messages  # 没有边界：整段都是活跃段
    start = boundary_index if include_boundary else boundary_index + 1
    return messages[start:]


def _message_meta(message: dict[str, Any]) -> dict[str, Any]:
    # 统一取消息的 meta（兼容 meta 和 metadata 两种键名），并保证返回的是 dict
    meta = message.get("meta") or message.get("metadata") or {}
    return meta if isinstance(meta, dict) else {}


def _message_provider_id(message: dict[str, Any]) -> str | None:
    # 取消息在模型供应商侧的 ID（可能是 message_id / provider_message_id，或藏在 meta 里），用于保持对话连续性
    meta = _message_meta(message)
    value = (
        message.get("message_id")
        or message.get("provider_message_id")
        or meta.get("message_id")
        or meta.get("provider_message_id")
    )
    return str(value) if value else None


def _content_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    # 取消息里"结构化内容块"列表（content 是 list 时才有；每段是 dict）
    content = message.get("content")
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    return []


def _tool_use_ids(message: dict[str, Any]) -> set[str]:
    # 收集一条消息里所有"工具调用"的 ID（assistant 发起工具时带的 id），用于配对工具结果。
    ids: set[str] = set()
    for call in message.get("tool_calls") or []:
        if isinstance(call, dict):
            value = (
                call.get("id")
                or call.get("tool_call_id")
                or call.get("tool_use_id")
                or call.get("call_id")
            )
            if value:
                ids.add(str(value))
    for block in _content_blocks(message):
        if block.get("type") == "tool_use" and block.get("id"):
            ids.add(str(block["id"]))  # 内容块里也可能是 tool_use
    meta = _message_meta(message)
    for key in ("tool_call_id", "tool_use_id"):
        if meta.get(key) and str(message.get("role") or "") in {"assistant", "tool_use"}:
            ids.add(str(meta[key]))
    return ids


def _tool_result_ids(message: dict[str, Any]) -> set[str]:
    # 收集一条消息里所有"工具结果"对应的 ID（tool 结果用来呼应前面的 tool_use）
    ids: set[str] = set()
    role = str(message.get("role") or "")
    for key in ("tool_call_id", "tool_use_id"):
        value = message.get(key)
        if value:
            ids.add(str(value))
    for block in _content_blocks(message):
        if block.get("type") == "tool_result" and block.get("tool_use_id"):
            ids.add(str(block["tool_use_id"]))
    meta = _message_meta(message)
    for key in ("tool_call_id", "tool_use_id"):
        if meta.get(key) and role in {"tool", "tool_result", "user"}:
            ids.add(str(meta[key]))
    return ids


def adjust_keep_start_to_preserve_api_invariants(messages: list[dict[str, Any]], start_index: int) -> int:
    """在切片保留后缀时，把切点向左挪，避免把"工具结果"变成孤儿（找不到对应的工具调用）。."""
    if start_index <= 0 or start_index >= len(messages):
        return max(0, min(start_index, len(messages)))  # 边界情况直接返回合法下标

    adjusted = start_index
    kept_results: set[str] = set()  # 保留段里"工具结果"的 ID
    kept_uses: set[str] = set()     # 保留段里"工具调用"的 ID
    for message in messages[adjusted:]:
        kept_results.update(_tool_result_ids(message))
        kept_uses.update(_tool_use_ids(message))

    missing_uses = kept_results - kept_uses  # 有结果但没对应调用 = 孤儿，得把调用也保留进来
    if missing_uses:
        # 从切点往前找，把"发起这些工具的 assistant 消息"一并纳入保留段
        for index in range(adjusted - 1, -1, -1):
            uses = _tool_use_ids(messages[index])
            if uses & missing_uses:
                adjusted = index  # 把切点左移
                missing_uses -= uses
                if not missing_uses:
                    break

    # 还要保证：保留段开头不能是"半截 assistant 消息"（保持对话从 user 起头的 API 约束）
    kept_provider_ids = {
        provider_id
        for message in messages[adjusted:]
        if str(message.get("role") or "") == "assistant"
        for provider_id in [_message_provider_id(message)]
        if provider_id
    }
    while adjusted > 0:
        previous = messages[adjusted - 1]
        if str(previous.get("role") or "") != "assistant":
            break  # 前一条不是 assistant，停止
        previous_provider_id = _message_provider_id(previous)
        if not previous_provider_id or previous_provider_id not in kept_provider_ids:
            break
        adjusted -= 1  # 把这条 assistant 也纳入保留段

    return adjusted


def _words(text: str) -> list[str]:
    # 把文本按词拆成小写列表（用于关键词匹配，复用 WORD_PATTERN）
    return WORD_PATTERN.findall((text or "").lower())


def _unique_preserve_order(items: list[str], limit: int = 12) -> list[str]:
    # 去重并保持顺序，最多留 limit 条。先归一化空白再比较，避免"相同内容不同空格"被当成两条。
    seen: set[str] = set()
    output: list[str] = []
    for item in items:
        clean = " ".join(str(item or "").strip().split())
        if not clean:
            continue
        key = clean.lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(clean)
        if len(output) >= limit:
            break
    return output


def clip_to_token_budget(text: str, max_tokens: int, suffix: str = "...") -> str:
    # 把文本截到不超过 max_tokens，超了就加 suffix（默认"..."）。先按字符粗切，再按 token 精修。
    clean = str(text or "").strip()
    if max_tokens <= 0 or not clean:
        return ""
    if estimate_tokens(clean) <= max_tokens:
        return clean  # 没超预算直接返回
    approx_chars = max(32, max_tokens * 4)  # 先按 1 token≈4 字符粗切
    clipped = clean[:approx_chars].rstrip()
    while clipped and estimate_tokens(clipped + suffix) > max_tokens:
        # 逐次回退 24 字符，直到加上 suffix 也不超预算
        clipped = clipped[: max(0, len(clipped) - 24)].rstrip()
    return f"{clipped}{suffix}" if clipped else ""


@dataclass(frozen=True)
class ContextBudget:
    # 上下文预算：把一个会话窗口按区块切成 system/memory/evidence/recent 等预算。
    max_input_tokens: int
    reserved_response_tokens: int
    system_tokens: int
    memory_tokens: int
    evidence_tokens: int
    recent_tokens: int
    tool_observation_tokens: int

    @property
    def available_prompt_tokens(self) -> int:
        # 除去给回答预留的空间，真正能塞提示词的部分
        return max(512, self.max_input_tokens - self.reserved_response_tokens)


def default_context_budget(model: str | None = None) -> ContextBudget:
    # 计算一份默认预算：先定总输入上限，再给回答预留，再把剩余按比例分给各区块。
    if settings.context_dynamic_model_window_enabled:
        max_input = max(settings.context_max_input_tokens, model_context_window_tokens(model))
    else:
        max_input = max(4096, settings.context_max_input_tokens)
    reserved = max(512, min(settings.context_reserved_response_tokens, max_input // 2))  # 预留给回答（至少 512）
    available = max_input - reserved  # 可分配给各区块的总量
    return ContextBudget(
        max_input_tokens=max_input,
        reserved_response_tokens=reserved,
        # 各区块按配置比例（context_system_ratio 等）切分，且都有 512 下限
        system_tokens=max(512, int(available * settings.context_system_ratio)),
        memory_tokens=max(512, int(available * settings.context_memory_ratio)),
        evidence_tokens=max(512, int(available * settings.context_evidence_ratio)),
        recent_tokens=max(512, int(available * settings.context_recent_ratio)),
        tool_observation_tokens=max(512, int(available * settings.context_tool_ratio)),
    )


def compress_messages(messages: list[dict[str, Any]], max_tokens: int) -> tuple[list[dict[str, str]], dict[str, Any]]:
    # 压缩历史消息：从"最新"往"最旧"保留，直到预算用满；中间的消息被截断（精确后缀保留）。
    # 为什么从新往旧：最近的对话对当前回答最重要，旧的可以牺牲。
    # 返回：(保留下来的消息列表, 统计信息)
    kept_reversed: list[dict[str, str]] = []  # 倒序收集（后面再翻回来）
    used = 0            # 已用 token
    truncated_messages = 0   # 被截断（非精确）的条数
    exact_suffix_messages = 0  # 完整保留的条数
    exact_suffix_open = True     # 是否还在"连续完整"区段
    for message in reversed(messages):  # 从最新开始
        role = str(message.get("role") or "")
        content = str(message.get("content") or "")
        if role not in {"user", "assistant"} or not content.strip():
            continue  # 跳过非对话角色或空消息
        per_message_budget = max(96, min(900, max_tokens // 3))  # 每条消息单独预算（总预算的 1/3，限在 96~900）
        clipped = clip_to_token_budget(content, per_message_budget)  # 单条截断
        cost = estimate_tokens(role) + estimate_tokens(clipped)
        if kept_reversed and used + cost > max_tokens:
            break  # 再加就超预算，停止（保留最新那些）
        kept_reversed.append({"role": role, "content": clipped})
        if clipped == content.strip():
            if exact_suffix_open:
                exact_suffix_messages += 1  # 完整保留
        else:
            truncated_messages += 1      # 被截断
            exact_suffix_open = False    # 一旦截断，后面不再算"连续完整"
        used += cost
        if used >= max_tokens:
            break
    kept = list(reversed(kept_reversed))  # 翻回正序（旧→新）
    total_eligible_messages = sum(
        # 统计"有资格"进历史的消息总数（用于算 omitted）
        1
        for message in messages
        if str(message.get("role") or "") in {"user", "assistant"}
        and str(message.get("content") or "").strip()
    )
    return kept, {
        "input_messages": len(messages),
        "eligible_messages": total_eligible_messages,
        "kept_messages": len(kept),
        "omitted_messages": max(0, total_eligible_messages - len(kept)),  # 被整体丢掉的条数
        "truncated_messages": truncated_messages,
        "exact_suffix_messages": exact_suffix_messages,  # 完整保留的（保真）条数
        "estimated_tokens": used,
        "budget_tokens": max_tokens,
    }


def _tool_names(message: dict[str, Any]) -> list[str]:
    # 从一条消息里取出所有工具名（来自 route 或 tool_calls），用于判断"是不是可重算工具"
    names: list[str] = []
    route = str(message.get("route") or "").strip()
    if route:
        names.append(route)
    for call in message.get("tool_calls") or []:
        if isinstance(call, dict):
            name = str(call.get("tool_name") or "").strip()
            if name:
                names.append(name)
    return names


def is_regenerable_context_message(message: dict[str, Any]) -> bool:
    # 判断一条消息是否"可重算的上下文"（工具/工具结果，且工具在 COMPACTABLE 名单里）。
    role = str(message.get("role") or "")
    meta = _message_meta(message)
    if role not in {"tool", "tool_result"} and meta.get("subtype") != "tool_result":
        return False  # 不是工具相关消息，直接否
    if meta.get("regenerable") is False:
        return False  # 显式标记为不可重算，跳过
    haystack = " ".join(_tool_names(message)).lower()
    if not haystack:
        return False
    # 工具名命中"可压缩工具名单"任一即算可重算
    return any(hint in haystack for hint in COMPACTABLE_TOOL_HINTS)


def microcompact_messages(
    messages: list[dict[str, Any]],
    query: str,
    *,
    keep_recent: int | None = None,
    max_excerpt_tokens: int = 90,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    # 微压缩：比整段压缩轻量。把"保留段之外、且可重算"的工具结果替换成一行提示（省很多 token）。
    # 类比：不把整段历史总结，只是把"能重新跑出来的工具输出"删掉，留句"需要可重跑"。
    keep = keep_recent if keep_recent is not None else settings.context_microcompact_keep_recent_messages
    keep_start = adjust_keep_start_to_preserve_api_invariants(
        # 算保留段的起点（保证对话对完整）
        messages,
        max(0, len(messages) - max(0, keep)),
    )
    output: list[dict[str, Any]] = []
    compacted = 0       # 被压缩条数
    tokens_before = 0   # 压缩前总 token
    tokens_after = 0    # 压缩后总 token
    compacted_tools: list[str] = []  # 涉及到的工具名

    for index, message in enumerate(messages):
        content = str(message.get("content") or "")
        before = estimate_tokens(content)
        tokens_before += before
        next_message = dict(message)  # 复制一份（下面可能改它的 content）
        if (
            index < keep_start  # 在保留段之外
            and is_regenerable_context_message(message)  # 且可重算
            and before > max_excerpt_tokens * 2  # 且够长（短的不值得压缩）
        ):
            names = _tool_names(message)
            excerpt = extract_relevant_snippet(query, content, max_tokens=max_excerpt_tokens)  # 抽关键片段留着
            tool_label = ", ".join(_unique_preserve_order(names, 4)) or "tool output"
            # 把长工具输出替换成"可重算"提示 + 来源 + 关键片段
            compacted_content = f"{MICROCOMPACT_MARKER}\nSource: {tool_label}\nRecoverable: rerun the corresponding workspace, RAG, or GitHub tool if exact details are needed."
            if excerpt:
                compacted_content += f"\nKey excerpt: {excerpt}"
            next_message["content"] = compacted_content
            compacted += 1
            compacted_tools.extend(names)
        tokens_after += estimate_tokens(str(next_message.get("content") or ""))
        output.append(next_message)

    return output, {
        "mode": "microcompact_regenerable_context",
        "input_messages": len(messages),
        "compacted_messages": compacted,
        "tokens_before": tokens_before,
        "tokens_after": tokens_after,
        "estimated_tokens_saved": max(0, tokens_before - tokens_after),  # 省下的 token
        "kept_recent_messages": max(0, min(keep, len(messages))),
        "compacted_tools": _unique_preserve_order(compacted_tools, 12),
    }


def extract_relevant_snippet(query: str, text: str, max_tokens: int = 220) -> str:
    # 从长文本里抽取"和查询最相关"的片段（按句子打分：命中的词越多分越高）。用于证据/工具结果截取。
    clean = " ".join(str(text or "").replace("\r", "\n").split())  # 归一化空白
    if not clean:
        return ""
    terms = set(_words(query))  # 查询词集合
    if not terms:
        return clip_to_token_budget(clean, max_tokens)  # 没查询词就直接截

    sentences = [part.strip() for part in SENTENCE_SPLIT_PATTERN.split(clean) if part.strip()]  # 按句切
    if not sentences:
        sentences = [clean]

    scored: list[tuple[float, int, str]] = []  # (分数, 原顺序, 句子)
    for index, sentence in enumerate(sentences):
        lower = sentence.lower()
        matched = [term for term in terms if term and term in lower]
        # 分数 = 命中不同词数*3 + 词出现次数；如果整句包含完整查询再加 4 分
        score = len(set(matched)) * 3.0 + sum(lower.count(term) for term in matched)
        if query.lower().strip() and query.lower().strip() in lower:
            score += 4.0
        scored.append((score, index, sentence))

    positive = [item for item in scored if item[0] > 0]  # 只保留有命中的
    if not positive:
        return clip_to_token_budget(clean, max_tokens)

    positive.sort(key=lambda item: (-item[0], item[1]))  # 先按分数降序，再按原顺序
    selected = sorted(positive[:4], key=lambda item: item[1])  # 取分数最高的 4 句，再按原文顺序拼回
    snippet = " ".join(sentence for _score, _index, sentence in selected)
    return clip_to_token_budget(snippet, max_tokens)


def compress_retrieval_results(
    query: str,
    items: list[dict[str, Any]],
    max_tokens_per_item: int = 260,
    max_total_tokens: int | None = None,
) -> list[dict[str, Any]]:
    # 压缩"检索结果"：每条检索结果只保留和查询相关的片段，并记压缩比。支持总预算上限。
    compressed: list[dict[str, Any]] = []
    total_budget = max(0, int(max_total_tokens)) if max_total_tokens is not None else None  # 可能无上限
    used_tokens = 0
    for item in items:
        if total_budget is not None and used_tokens >= total_budget:
            break  # 总预算用尽
        if (
            total_budget is not None
            and compressed
            and total_budget - used_tokens < min(24, max(1, int(max_tokens_per_item)))
        ):
            break  # 剩下的预算连一条最小片段都不够，停
        item_budget = max(1, int(max_tokens_per_item))
        if total_budget is not None:
            item_budget = min(item_budget, total_budget - used_tokens)  # 本条预算不超过剩余
        raw_text = str(item.get("_raw_content") or item.get("content") or item.get("snippet") or "")
        snippet = extract_relevant_snippet(query, raw_text, max_tokens=item_budget)  # 抽相关片段
        original_tokens = estimate_tokens(raw_text)
        compressed_tokens = estimate_tokens(snippet)
        next_item = {key: value for key, value in item.items() if key not in {"_raw_content", "content"}}  # 去掉原始大文本
        next_item["snippet"] = snippet  # 换成截好的片段
        next_item["compression"] = {
            # 记录压缩情况（便于评估质量）
            "mode": "query_aware_extractive",
            "original_estimated_tokens": original_tokens,
            "compressed_estimated_tokens": compressed_tokens,
            "ratio": round(compressed_tokens / original_tokens, 4) if original_tokens else 1.0,
            "item_budget_tokens": item_budget,
            "global_budget_tokens": total_budget,
            "global_tokens_before_item": used_tokens,
        }
        retrieval = dict(next_item.get("retrieval") or {})
        retrieval["context_compression"] = next_item["compression"]
        retrieval["selected_for_context"] = True
        next_item["retrieval"] = retrieval
        compressed.append(next_item)
        used_tokens += compressed_tokens
    return compressed


# 下面这段是"压缩摘要"的系统提示词（发给 LLM 用的）。注意：这是提示词字符串，内部保持原样、不插注释（硬性要求）。
COMPACT_SUMMARY_SYSTEM_PROMPT = """请在上下文压缩后，为 DevMind AI 创建一份可延续工作的摘要。
目标不是单纯减少 token，而是为软件工程 Agent 保留工作连续性。

请用纯文本返回以下部分：
1. 用户意图和明确要求。
2. 当前工作面：现在正在处理什么；如相关，请包含仓库、文件、函数、命令、测试和 UI 状态。
3. 持久决策和约束。
4. 重要事实、代码引用、错误和修复。
5. 待办任务和下一步，并以最近一次用户请求为依据。

当文件路径、标识符、日期、命令、错误消息和用户偏好很重要时，请保留原文。
不要编造细节。如果某个细节只存在于可以重新生成的工具输出中，请说明它可以通过相关工具恢复。"""

# 路径正则：识别 "src/app.py:42" 这类"文件路径+行号"，压缩时要保留（很重要不能丢）
PATH_LIKE_PATTERN = re.compile(
    r"(?:[A-Za-z]:[\\/])?(?:[\w.@()+\- ]+[\\/])+[\w.@()+\- ]+\.[A-Za-z0-9_]+(?::\d+)?"
)
# 命令正则：识别 pytest/npm/git/docker 等命令关键词
COMMAND_HINT_PATTERN = re.compile(
    r"\b(pytest|npm|pnpm|yarn|git|docker|uvicorn|alembic|python|pip|ruff|mypy|pytest)\b",
    flags=re.IGNORECASE,  # 忽略大小写
)
# 保真提示词：包含这些词的行"很重要"，压缩摘要时必须保留（错误/决定/待办等）
PRESERVE_LINE_HINTS = (
    "error",
    "failed",
    "failure",
    "exception",
    "traceback",
    "decision",
    "decide",
    "agreed",
    "todo",
    "next step",
    "prefer",
    "preference",
    "错误",
    "失败",
    "异常",
    "决定",
    "确定",
    "待办",
    "下一步",
    "偏好",
)


def _must_preserve_candidates(messages: list[dict[str, Any]], limit: int = 18) -> list[str]:
    # 从消息里扫出"必须保留"的候选（文件路径、命令、含保真关键词的行），去重限条数。
    candidates: list[str] = []
    for message in messages:
        content = str(message.get("content") or "")
        if not content:
            continue
        # 提取所有"路径:行号"形式
        candidates.extend(match.group(0).strip() for match in PATH_LIKE_PATTERN.finditer(content))
        for raw_line in re.split(r"[\r\n]+", content):
            line = " ".join(raw_line.strip(" -\t").split())
            if not line:
                continue
            lower = line.lower()
            # 命中命令或任一保真关键词就保留
            if COMMAND_HINT_PATTERN.search(line) or any(hint in lower for hint in PRESERVE_LINE_HINTS):
                candidates.append(clip_to_token_budget(line, 120))
    return _unique_preserve_order(candidates, limit)


def compaction_preservation_report(messages: list[dict[str, Any]], summary: str) -> dict[str, Any]:
    # 检查摘要是否"漏掉"了必须保留的事实。返回 required（该保留的）和 missing（漏掉的）。
    required = _must_preserve_candidates(messages)
    summary_lower = str(summary or "").lower()
    missing = [item for item in required if item.lower() not in summary_lower]  # 在原文有、摘要里没有 = 漏了
    return {
        "required_count": len(required),
        "missing_count": len(missing),
        "required": required,
        "missing": missing,
    }


def ensure_compaction_summary_preserves_key_facts(
    messages: list[dict[str, Any]],
    summary: str,
    max_tokens: int,
) -> tuple[str, dict[str, Any]]:
    # 保真补全：若摘要漏了关键事实，把漏掉的作为"保真补充"附录追加（确保压缩不丢重要信息）。
    report = compaction_preservation_report(messages, summary)
    missing = [str(item) for item in report.get("missing") or []]
    if not missing:
        return clip_to_token_budget(summary, max_tokens), report  # 没漏，直接返回

    appendix = "保真补充：\n" + "\n".join(
        # 把漏掉的项（最多 12 个）列成附录
        f"- {clip_to_token_budget(item, 96)}" for item in missing[:12]
    )
    body_budget = max(160, max_tokens - estimate_tokens(appendix) - 12)  # 给摘要正文留的预算 = 总预算 - 附录
    combined = "\n\n".join([clip_to_token_budget(summary, body_budget), appendix]).strip()  # 正文 + 附录
    combined = clip_to_token_budget(combined, max_tokens)  # 整体再截到总预算
    final_report = compaction_preservation_report(messages, combined)  # 重新检查
    final_report["appended_missing_count"] = len(missing)
    return combined, final_report


def build_compaction_summary_payload(messages: list[dict[str, Any]], max_tokens: int = 6000) -> str:
    # 把要压缩的消息拼成可发给 LLM 的载荷（每行 "角色: 内容"，内容截短）。
    lines: list[str] = []
    for message in messages:
        role = str(message.get("role") or "unknown")
        if role == "system" and is_compact_boundary_message(message):
            role = "compact_boundary"  # 边界消息改名，避免和 system 混淆
        content = clip_to_token_budget(str(message.get("content") or ""), max(160, max_tokens // max(len(messages), 1)))
        if not content:
            continue
        tool_names = _tool_names(message)
        suffix = f" tools={tool_names}" if tool_names else ""  # 附上工具名
        lines.append(f"{role}{suffix}: {content}")
    return clip_to_token_budget("\n\n".join(lines), max_tokens)


def format_compaction_summary(summary: str) -> str:
    # 把 LLM 返回的摘要整理成标准格式：去掉 <analysis>，提取 <summary> 内容，压缩多余空行。
    formatted = str(summary or "").strip()
    formatted = re.sub(r"<analysis>[\s\S]*?</analysis>", "", formatted, flags=re.IGNORECASE).strip()  # 删思考过程
    match = re.search(r"<summary>([\s\S]*?)</summary>", formatted, flags=re.IGNORECASE)
    if match:
        formatted = "摘要：\n" + match.group(1).strip()  # 提取摘要正文
    formatted = re.sub(r"\n{3,}", "\n\n", formatted)  # 多余空行压成两个
    return formatted.strip()


def fallback_compaction_summary(messages: list[dict[str, Any]], max_tokens: int | None = None) -> str:
    # 兜底压缩摘要（LLM 不可用时用）：抽取式——把用户/助手消息里的"意图/决策/待办/事实"挑出来拼成摘要。
    user_messages = [str(item.get("content") or "") for item in messages if item.get("role") == "user"]
    assistant_messages = [str(item.get("content") or "") for item in messages if item.get("role") == "assistant"]
    decisions: list[str] = []
    tasks: list[str] = []
    facts: list[str] = []
    for content in [*user_messages, *assistant_messages]:
        for line in [part.strip(" -\t") for part in content.splitlines() if part.strip()]:
            lower = line.lower()
            # 命中"决定/确定"类词 -> 归入决策
            if any(token in lower for token in ["decision", "decide", "agreed", "\u51b3\u5b9a", "\u786e\u5b9a"]):
                decisions.append(line)
            # 命中"待办/下一步"类词 -> 归入任务
            if any(token in lower for token in ["todo", "next step", "implement", "fix", "\u5f85\u529e", "\u4e0b\u4e00\u6b65"]):
                tasks.append(line)
            # 较长且无太多条时 -> 归为事实
            if len(line) > 20 and len(facts) < 10:
                facts.append(line)
    summary = "\n\n".join(
        [
            "This conversation was compacted. Continue from the preserved recent messages and this summary.",  # 提示：从保留段+摘要继续
            "User intent:\n" + "\n".join(f"- {clip_to_token_budget(item, 120)}" for item in user_messages[-8:]),
            "Assistant context:\n" + "\n".join(f"- {clip_to_token_budget(item, 120)}" for item in assistant_messages[-5:]),
            "Durable decisions:\n" + "\n".join(f"- {item}" for item in _unique_preserve_order(decisions, 12)),
            "Pending tasks:\n" + "\n".join(f"- {item}" for item in _unique_preserve_order(tasks, 12)),
            "Important facts:\n" + "\n".join(f"- {item}" for item in _unique_preserve_order(facts, 12)),
        ]
    )
    return clip_to_token_budget(summary, max_tokens or settings.context_compact_summary_tokens)


def structured_memory_from_model(parsed: dict[str, Any]) -> dict[str, Any]:
    # 把模型返回的（可能不规整的）记忆字典，规整成标准结构：每类字段去重限条数。
    return {
        "summary": str(parsed.get("summary") or "").strip(),
        "facts": _unique_preserve_order([str(item) for item in parsed.get("facts") or []], 16),
        "decisions": _unique_preserve_order([str(item) for item in parsed.get("decisions") or []], 16),
        "open_questions": _unique_preserve_order([str(item) for item in parsed.get("open_questions") or []], 12),
        "tasks": _unique_preserve_order([str(item) for item in parsed.get("tasks") or []], 16),
        "user_preferences": _unique_preserve_order([str(item) for item in parsed.get("user_preferences") or []], 12),
        "repo_context": _unique_preserve_order([str(item) for item in parsed.get("repo_context") or []], 16),
        "citations": [item for item in parsed.get("citations") or [] if isinstance(item, dict)][:16],  # 只保留 dict 型引用，限 16
    }


def fallback_memory_update(user_message: str, assistant_message: str, route: str, tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
    # 兜底记忆更新（LLM 不可用时）：从一问一答里抽取问题/决策/待办/偏好/事实，生成一份结构化记忆。
    joined = "\n".join([user_message or "", assistant_message or ""])
    lines = [line.strip(" -\t") for line in joined.splitlines() if line.strip()]
    decisions: list[str] = []
    open_questions: list[str] = []
    tasks: list[str] = []
    facts: list[str] = []
    preferences: list[str] = []

    for line in lines:
        lower = line.lower()
        # 含问号或"why/how/what"开头 -> 未决问题
        if "?" in line or "\uff1f" in line or lower.startswith(("why ", "how ", "what ")):
            open_questions.append(line)
        # 决定类
        if any(token in lower for token in ["decide", "decision", "agreed", "chosen", "\u786e\u5b9a", "\u51b3\u5b9a"]):
            decisions.append(line)
        # 待办类
        if any(token in lower for token in ["todo", "next step", "action", "fix", "implement", "\u5f85\u529e", "\u4e0b\u4e00\u6b65"]):
            tasks.append(line)
        # 偏好类
        if any(token in lower for token in ["prefer", "preference", "\u559c\u6b22", "\u504f\u597d"]):
            preferences.append(line)
        # 较长事实（限 6 条）
        if len(line) > 20 and len(facts) < 6:
            facts.append(line)

    tools = ", ".join(str(call.get("tool_name") or "未知") for call in tool_calls) or "无"
    summary = (
        f"路由：{route}\n"
        f"用户请求：{clip_to_token_budget(user_message, 90)}\n"
        f"助手回答：{clip_to_token_budget(assistant_message, 140)}\n"
        f"工具：{tools}"
    )
    return structured_memory_from_model(
        {
            "summary": summary,
            "facts": facts,
            "decisions": decisions,
            "open_questions": open_questions,
            "tasks": tasks,
            "user_preferences": preferences,
            "repo_context": [f"使用路由：{route}"] if route else [],
            "citations": [],
        }
    )


def merge_memory(existing: dict[str, Any], update: dict[str, Any], digest: str, max_tokens: int) -> dict[str, Any]:
    # 合并新旧记忆：旧摘要 + 新摘要拼成总摘要（截预算），各列表字段合并去重限条数。
    # 为什么：会话是多轮的，记忆要"滚雪球"累积，而不是每轮只存本轮。
    existing_summary = str(existing.get("summary") or "").strip()
    update_summary = str(update.get("summary") or digest or "").strip()
    summary_parts = [part for part in [existing_summary, update_summary] if part]
    summary = "\n\n".join(summary_parts)
    summary = clip_to_token_budget(summary, max_tokens)
    return {
        "summary": summary,
        "facts": _unique_preserve_order([*(existing.get("facts") or []), *(update.get("facts") or [])], 16),
        "decisions": _unique_preserve_order([*(existing.get("decisions") or []), *(update.get("decisions") or [])], 16),
        "open_questions": _unique_preserve_order([*(existing.get("open_questions") or []), *(update.get("open_questions") or [])], 12),
        "tasks": _unique_preserve_order([*(existing.get("tasks") or []), *(update.get("tasks") or [])], 16),
        "user_preferences": _unique_preserve_order([*(existing.get("user_preferences") or []), *(update.get("user_preferences") or [])], 12),
        "repo_context": _unique_preserve_order([*(existing.get("repo_context") or []), *(update.get("repo_context") or [])], 16),
        "citations": [*(existing.get("citations") or []), *(update.get("citations") or [])][-16:],  # 引用取最近 16 条
    }


def render_structured_memory(memory: Any, max_tokens: int | None = None) -> str:
    # 把结构化记忆对象渲染成可读文本（各区块标题 + 列表），可选截预算。
    sections: list[str] = []
    summary = str(getattr(memory, "summary", "") or "").strip()
    if summary:
        sections.append("摘要：\n" + summary)
    for label, attr in [
        ("事实", "facts"),
        ("决策", "decisions"),
        ("未决问题", "open_questions"),
        ("任务", "tasks"),
        ("用户偏好", "user_preferences"),
        ("仓库上下文", "repo_context"),
    ]:
        values = getattr(memory, attr, None) or []
        if values:
            sections.append(label + ":\n" + "\n".join(f"- {item}" for item in values[:12]))  # 每类最多 12 条
    text = "\n\n".join(sections)
    return clip_to_token_budget(text, max_tokens) if max_tokens else text


def memory_document_text(memory: Any) -> str:
    # 直接拿完整记忆文本（不限 token），供落库/检索用
    return render_structured_memory(memory, max_tokens=None)


def prompt_cache_model_kwargs() -> dict[str, Any]:
    # 返回"提示词缓存"相关参数（部分模型支持把 system 提示词缓存起来省 token/钱）。
    # 没开缓存就返回空字典（调用方按"无缓存"处理）。
    if not settings.prompt_cache_enabled:
        return {}
    kwargs: dict[str, Any] = {}
    if settings.prompt_cache_key:
        kwargs["prompt_cache_key"] = settings.prompt_cache_key
    if settings.prompt_cache_retention:
        kwargs["prompt_cache_retention"] = settings.prompt_cache_retention
    return kwargs


def compression_eval_summary(before_text: str, after_text: str, retrieved: int, kept: int) -> dict[str, Any]:
    # 压缩效果评估：比较压缩前后 token 数，算压缩比和节省量。供监控/评估用。
    before = estimate_tokens(before_text)
    after = estimate_tokens(after_text)
    return {
        "estimated_tokens_before": before,
        "estimated_tokens_after": after,
        "compression_ratio": round(after / before, 4) if before else 1.0,  # 越小压缩越狠
        "retrieved_items": retrieved,
        "kept_items": kept,
        "token_savings": max(0, before - after),
    }


def dumps_compact(value: Any) -> str:
    # 紧凑序列化：ensure_ascii=False 保留中文；separators 去掉多余空格；default=str 兜底不可序列化对象
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
