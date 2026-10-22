"""
tests/test_context_compression.py —— 测试「上下文压缩」模块
============================================================================

本测试文件验证 context_compression.py（和 chat_memory.py 的部分逻辑）：当聊天历史太长、
快要超出模型上下文窗口时，系统会「压缩旧内容、保留最近对话」。这里用一组单元测试
确保各种压缩策略都按预期工作，不丢关键信息。

测试思路：构造假消息 → 调用压缩函数 → 用 assert 检查「结果里该留的留了、该省的了、
关键事实（路径/错误/待办）没丢」。全部用内存里的假数据，不连数据库。."""

import uuid  # 生成随机 id（造测试用的消息行）
from types import SimpleNamespace  # 快速造一个「只有指定属性的对象」，代替真实 ORM 行

from app.services.chat_memory import ProgressiveContextManager  # 渐进式上下文管理器
from app.services.context_compression import (  # 被测的压缩函数们
    MICROCOMPACT_MARKER,  # 微压缩标记字符串
    adjust_keep_start_to_preserve_api_invariants,  # 调整保留起点，保证「工具调用-结果」成对
    calculate_context_pressure,  # 算上下文压力（还剩多少 token）
    compress_messages,  # 整体压缩消息列表
    compress_retrieval_results,  # 压缩检索结果
    ensure_compaction_summary_preserves_key_facts,  # 确保压缩摘要保留关键事实
    estimate_tokens,  # 估算 token 数
    fallback_memory_update,  # 兜底的记忆更新
    messages_after_compact_boundary,  # 取最近一次压缩边界之后的消息
    merge_memory,  # 合并记忆（结构化字段）
    microcompact_messages,  # 微压缩（只压旧的可再生工具上下文）
)


def test_compress_messages_keeps_recent_turns_under_budget() -> None:
    # 测试：整体压缩要保留最近的对话，且在 token 预算内
    # 造 3 条消息：前两条是超长的「旧内容」（乘 200 让它们很长），最后一条是最近的问题
    messages = [
        {"role": "user", "content": "old setup note " * 200},
        {"role": "assistant", "content": "old answer " * 200},
        {"role": "user", "content": "What did we decide about auth middleware?"},
    ]

    compressed, stats = compress_messages(messages, max_tokens=120)  # 预算只有 120 token

    # 压缩后最后一条（最近的问题）必须原样保留
    assert compressed[-1]["content"] == "What did we decide about auth middleware?"
    assert stats["kept_messages"] < stats["input_messages"]  # 保留的比输入少（确实压缩了）
    assert stats["omitted_messages"] > 0  # 有被省略的消息
    assert stats["exact_suffix_messages"] >= 1  # 至少保留了最近若干条原样消息（后缀）
    # 压缩后总 token 不超过 140（略大于预算，允许一定余量）
    assert estimate_tokens("\n".join(item["content"] for item in compressed)) <= 140


def test_retrieval_compression_prefers_query_relevant_sentence() -> None:
    # 测试：检索结果压缩时，优先保留和问题相关的那句话
    results = compress_retrieval_results(
        "auth token refresh middleware",  # 用户查询
        [
            {
                "id": "auth",
                "title": "auth docs",
                "source_type": "workspace_file",
                "source_id": "auth",
                "score": 1.0,
                # _raw_content 前面是无关的 billing 内容×100，最后一句才是相关的 auth 说明
                "_raw_content": "billing copy. " * 100 + "The auth middleware refreshes expired tokens before validating the session.",
            }
        ],
        max_tokens_per_item=40,  # 每条最多 40 token
    )

    assert "auth middleware refreshes expired tokens" in results[0]["snippet"]  # 相关句被保留在片段里
    # 压缩后的 token 数应小于原始
    assert results[0]["compression"]["compressed_estimated_tokens"] < results[0]["compression"]["original_estimated_tokens"]
    assert "_raw_content" not in results[0]  # 原始大文本不应再出现在结果（已提炼成 snippet）


def test_retrieval_compression_respects_global_token_budget() -> None:
    # 测试：多条检索结果要服从全局 token 总预算
    results = compress_retrieval_results(
        "auth token",
        [
            {
                "id": f"doc-{index}",
                "score": 1.0 - index * 0.1,  # 第 0 条分最高，后面递减
                "_raw_content": ("auth token refresh evidence. " * 80),
            }
            for index in range(5)  # 造 5 条
        ],
        max_tokens_per_item=40,
        max_total_tokens=75,  # 全局最多 75 token
    )

    assert sum(item["compression"]["compressed_estimated_tokens"] for item in results) <= 75  # 总预算内
    assert all(item["retrieval"]["selected_for_context"] is True for item in results)  # 都被选中
    assert len(results) < 5  # 因为预算不够，应该被砍掉一些（少于 5 条）


def test_memory_update_merges_structured_sections() -> None:
    # 测试：把一次对话更新合并进结构化记忆（decisions/tasks/user_preferences 等字段）
    update = fallback_memory_update(
        "I prefer concise answers. Can we keep token refresh in middleware?",  # 用户消息
        "Decision: keep token refresh in middleware. Next step: add tests.",  # 助手回答
        "direct_answer",  # 回答类型
        [],
    )
    merged = merge_memory(
        # 空的初始记忆（各字段都是空列表）
        {"summary": "", "facts": [], "decisions": [], "open_questions": [], "tasks": [], "user_preferences": [], "repo_context": [], "citations": []},
        update,
        update["summary"],
        max_tokens=180,
    )

    assert merged["decisions"]  # 合并后 decisions 非空（识别出了决策）
    assert merged["tasks"]  # tasks 非空（识别出了待办）
    assert merged["user_preferences"]  # user_preferences 非空（识别出了用户偏好）


def test_pressure_uses_effective_window_stages() -> None:
    # 测试：上下文压力计算给出正确的阶段和剩余 token
    pressure = calculate_context_pressure(116_000, model="gpt-4o-mini")  # 已用 116k token

    assert pressure.stage in {"auto_compact", "aggressive", "manual_path"}  # 阶段是这几个之一
    assert pressure.effective_window_tokens > 100_000  # 有效窗口大于 10 万
    assert pressure.remaining_tokens >= 0  # 剩余 token 非负


def test_microcompact_clears_old_regenerable_tool_context() -> None:
    # 测试：微压缩能把「旧的、可重新生成的工具结果」压成摘要，但保留相关句
    messages = [
        {
            "role": "tool_result",  # 一条工具返回结果
            "content": "irrelevant setup. " * 300 + "The auth middleware refreshes expired tokens before validation.",
            "route": "workspace.read_file",
            "tool_calls": [{"tool_name": "workspace.read_file", "arguments": {}, "status": "success"}],
        },
        {"role": "user", "content": "What did we decide about auth middleware?"},
        {"role": "assistant", "content": "Keep token refresh in middleware."},
    ]

    compacted, stats = microcompact_messages(messages, "auth middleware", keep_recent=2, max_excerpt_tokens=50)

    assert MICROCOMPACT_MARKER in compacted[0]["content"]  # 第一条被打了微压缩标记
    assert "auth middleware refreshes expired tokens" in compacted[0]["content"]  # 相关句保留
    assert compacted[-1]["content"] == "Keep token refresh in middleware."  # 最近一条原样保留
    assert stats["compacted_messages"] == 1  # 只有 1 条被压缩
    assert stats["estimated_tokens_saved"] > 0  # 确实省了 token


def test_compaction_summary_preserves_paths_errors_and_todos() -> None:
    # 测试：压缩摘要不能丢「文件路径 / 报错 / 待办」这类关键信息
    messages = [
        {
            "role": "user",
            "content": "Decision: keep token refresh in middleware.\nNext step: run pytest backend/app/tests/test_auth.py.",
        },
        {
            "role": "assistant",
            "content": "backend/app/auth.py raised TimeoutError: token refresh failed.",
        },
    ]

    summary, report = ensure_compaction_summary_preserves_key_facts(
        messages,
        "We discussed auth middleware.",  # 初始摘要（故意没写全）
        max_tokens=180,
    )

    assert "backend/app/auth.py" in summary  # 文件路径被补进摘要
    assert "pytest backend/app/tests/test_auth.py" in summary  # 待办命令被补进摘要
    assert "TimeoutError" in summary  # 报错类型被补进摘要
    assert report["appended_missing_count"] >= 3  # 至少补了 3 项缺失事实
    assert report["missing_count"] == 0  # 补完后没有遗漏


def test_messages_after_compact_boundary_uses_latest_boundary() -> None:
    # 测试：取「最近一次压缩边界」之后的消息（更旧的压缩边界忽略）
    messages = [
        {"role": "user", "content": "old"},
        {"role": "system", "content": "first", "meta": {"subtype": "compact_boundary"}},  # 早的边界
        {"role": "assistant", "content": "middle"},
        {"role": "system", "content": "latest", "meta": {"subtype": "compact_boundary"}},  # 最近的边界
        {"role": "user", "content": "current"},
    ]

    sliced = messages_after_compact_boundary(messages)

    # 只保留最近边界（"latest"）及其之后的消息
    assert [item["content"] for item in sliced] == ["latest", "current"]


def test_keep_start_moves_back_to_preserve_tool_use_result_pair() -> None:
    # 测试：保留起点要回退，保证「助手调用工具」和「工具返回结果」成对不割裂
    messages = [
        {
            "role": "assistant",
            "content": "calling read",
            "tool_calls": [{"id": "call_read", "tool_name": "workspace.read_file"}],  # 助手发起了工具调用
        },
        {"role": "tool_result", "content": "file contents", "tool_call_id": "call_read"},  # 对应的工具结果
        {"role": "user", "content": "continue"},
    ]

    # 原本想从索引 1 开始保留，但那样会把「工具调用」和「结果」拆开，所以函数应回退到 0
    assert adjust_keep_start_to_preserve_api_invariants(messages, 1) == 0


def test_formal_compaction_keep_rows_preserves_tool_pair() -> None:
    # 测试：正式压缩的「保留行」逻辑也能保住工具调用-结果对
    manager = ProgressiveContextManager(db=None)  # type: ignore[arg-type]  # 不连库，传 None
    rows = [
        SimpleNamespace(  # 用 SimpleNamespace 造假 ORM 行，省去建表
            id=uuid.uuid4(),
            role="assistant",
            content="calling read",
            route=None,
            tool_calls=[{"id": "call_read"}],
            meta={},
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            role="tool_result",
            content="file contents",
            route=None,
            tool_calls=[],
            meta={"tool_call_id": "call_read"},  # 对应上面的调用
        ),
        SimpleNamespace(id=uuid.uuid4(), role="user", content="continue", route=None, tool_calls=[], meta={}),
    ]

    kept = manager._messages_to_keep(rows, keep_count=2)  # type: ignore[arg-type]

    assert kept == rows  # 因为工具对不能拆，所以全部保留


def test_microcompact_never_rewrites_assistant_final_answer() -> None:
    # 测试：微压缩绝不能改动「助手的最终回答」（那是给用户看的关键内容）
    messages = [
        {
            "role": "assistant",
            "content": "Final answer based on workspace.read_file. " * 300,  # 很长的助手最终回答
            "route": "workspace.read_file",
            "tool_calls": [{"tool_name": "workspace.read_file", "status": "success"}],
        },
        {"role": "user", "content": "Continue."},
    ]

    compacted, stats = microcompact_messages(messages, "continue", keep_recent=1)

    assert compacted[0]["content"] == messages[0]["content"]  # 第一条（最终回答）原样不动
    assert stats["compacted_messages"] == 0  # 没有被压缩（保护好回答）
