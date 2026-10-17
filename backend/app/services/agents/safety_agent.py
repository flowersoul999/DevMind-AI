"""DevMind AI module."""

# Any：任意类型占位
from typing import Any

# 导入分析输出模型：安全检查结果
from app.schemas.analysis import SafetyOutput
# 导入 Agent 基类
from app.services.agents.base import BaseAgent


# 模块说明（给零基础同事看的）：
# SafetyAgent 是「写操作安全守门员」。
# 它不真正执行任何写操作（不评论、不关 Issue、不发包），只判断「用户这次想做的动作是不是有副作用的写操作」。
# 如果是写操作（评论/创建/打标签/关闭/发送等），就要求人工确认，并只返回草稿。
# 输入：一个 action 字符串（想做什么）+ 可选的 draft（草稿内容）。
# 输出：SafetyOutput（requires_confirmation 是否需要确认、risk_level 风险等级、reason 原因、draft 草稿）。
# 被谁调用：WorkflowOrchestrator 在 requires_safety 时派发 safety_review 任务；或 ChatAgent 的 safety_agent.classify_action_risk 工具。
# 典型调用链：用户想「给 Issue 写评论」→ ChatAgent 先调 SafetyAgent → 返回需确认 → 只给草稿，不真发。
# 为什么这样写：把「能不能动外部系统」单独卡一道，是安全底线——
#               MVP 阶段任何写操作都只生成草稿，必须人来点确认，避免 AI 擅自改了 GitHub/发消息。
class SafetyAgent(BaseAgent):
    """SafetyAgent."""
    name = "safety_agent"
    description = "检查某个操作是否需要人工确认。"
    available_tools = ["classify_action_risk"]

    # 主入口：判断 action 是否命中「写/外部发送」关键词，从而决定是否需要人工确认
    async def run(self, input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        # 把动作描述转小写，便于关键词匹配
        action = input_data.get("action", "").lower()
        # 写操作/外部发送类关键词清单（中英文都有）
        risky = any(
            token in action
            for token in [
                "comment",
                "create_issue",
                "label",
                "close_issue",
                "send_report",
                "write",
                "post",
                "send",
                "评论",
                "创建",
                "新建",
                "发给",
                "发送",
                "改标签",
                "关闭",
                "写回",
            ]
        )
        # 返回安全检查结果：
        #   命中 → 需要确认，风险等级 medium，原因说明「MVP 只生成草稿」
        #   未命中 → 不需要确认，风险等级 low，说明是只读分析
        return SafetyOutput(
            requires_confirmation=risky,
            risk_level="medium" if risky else "low",
            reason="MVP 阶段所有外部写操作只生成草稿。" if risky else "只读分析操作不需要人工确认。",
            draft=input_data.get("draft"),
        ).model_dump()
