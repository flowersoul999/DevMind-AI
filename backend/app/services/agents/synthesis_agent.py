"""DevMind AI module."""

# 允许类型注解里直接使用类本身，兼容老版本 Python 注解写法
from __future__ import annotations

# Any：任意类型占位
from typing import Any

# 导入工作流数据模型：观察结论、规格、任务结果
from app.schemas.workflow import WorkflowObservation, WorkflowSpec, WorkflowTaskResult
# 导入 Agent 基类
from app.services.agents.base import BaseAgent


# 模块说明（给零基础同事看的）：
# SynthesisAgent 是「多 Agent 工作流里的总结汇报人 / 秘书」。
# 它把前面各专项 Agent 的分析结果 + Observer 的质检结论，综合成一份人能直接读的「工程决策备忘录」。
# 输入：WorkflowSpec（计划）+ 各任务结果 + WorkflowObservation（观察结论）。
# 输出：一个字典 {"answer": "……"}，answer 是一大段 Markdown 文字，包含目标、任务图、各 Agent 摘要、
#       Observer 检查、决策依据、下一步建议等。
# 被谁调用：WorkflowOrchestrator 在跑完所有任务、做完观察后，最后调用它出最终报告正文。
# 典型调用链：各 Agent 结果 + Observer 结论 → SynthesisAgent.run() → 生成 answer → 塞进 WorkflowReport。
# 为什么这样写：让「写总结」也独立成一个 Agent，保证最终输出格式统一、结构完整，
#               不会因为某个专岗只吐了一句话就丢了 What/Why/风险/下一步这些关键信息。
class SynthesisAgent(BaseAgent):
    """SynthesisAgent."""
    name = "synthesis_agent"
    description = "将多 Agent 工作流综合成一份工程决策备忘录。"
    available_tools = ["synthesize_workflow_report"]

    # 主入口：综合出最终回答文字
    async def run(self, input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        # 规格字典 → 对象
        spec = WorkflowSpec.model_validate(input_data["spec"])
        # 结果列表字典 → 对象列表
        results = [WorkflowTaskResult.model_validate(item) for item in input_data.get("task_results") or []]
        # 观察结论字典 → 对象
        observation = WorkflowObservation.model_validate(input_data["observation"])
        # 调用内部方法拼装出 answer 文本
        answer = self._build_answer(spec, results, observation)
        return {"answer": answer}

    # 真正拼装 Markdown 总结的核心方法
    def _build_answer(self, spec: WorkflowSpec, results: list[WorkflowTaskResult], observation: WorkflowObservation) -> str:
        # 把观察里的 blocker 和 warning 找出来，供后面判断决策
        blockers = [item for item in observation.findings if item.severity == "blocker"]
        warnings = [item for item in observation.findings if item.severity == "warning"]
        # 先算一句「总体判断」决策
        decision = self._decision(blockers, warnings, results)

        # 用列表逐行收集 Markdown 文本（最后用换行连接）
        lines = [
            "## 多 Agent 协作结论",
            "",
            f"目标：{spec.goal}",
            f"总体判断：{decision}",
            f"Observer 置信度：{observation.overall_confidence:.2f}",
            "",
            "## 协作任务图",
        ]
        # 列出这次计划里的每个任务（谁负责、目标、依赖）
        for task in spec.tasks:
            deps = f"，依赖：{', '.join(task.dependencies)}" if task.dependencies else ""
            lines.append(f"- {task.id}: {task.agent_name} 负责 {task.objective}{deps}")

        # 列出每个 Agent 的输出摘要
        lines.extend(["", "## Agent 输出摘要"])
        for result in results:
            lines.append(f"- {result.agent_name} [{result.status}]: {result.summary or '无摘要。'}")

        # 列出 Observer 的检查发现；没有就说明无冲突
        lines.extend(["", "## Observer 检查"])
        if observation.findings:
            for finding in observation.findings:
                lines.append(f"- {finding.severity.upper()}: {finding.message} 建议：{finding.recommendation}")
        else:
            lines.append("- 未发现跨 Agent 结论冲突。")

        # 列出决策依据与待确认项
        lines.extend(["", "## 决策依据与待确认项"])
        lines.extend(f"- {item}" for item in self._decision_basis(results, observation))

        # 计算并列出下一步建议
        next_steps = self._next_steps(blockers, warnings, results)
        lines.extend(["", "## 下一步"])
        lines.extend(f"- {item}" for item in next_steps)
        # 用换行连接并去掉首尾空白
        return "\n".join(lines).strip()

    # 根据 blockers / warnings / PR 结果，给出一句总体决策判断
    def _decision(
        self,
        blockers: list[Any],
        warnings: list[Any],
        results: list[WorkflowTaskResult],
    ) -> str:
        # 有阻塞项时，按阻塞类型细分决策
        if blockers:
            # CI/跨 Agent 冲突 → 暂缓合并或发布
            if any(item.finding_type in {"ci_merge_gate", "cross_agent_conflict"} for item in blockers):
                return "暂缓合并或发布，先处理 CI/跨 Agent 冲突。"
            # 需要人工确认 → 只给草稿
            if any(item.finding_type == "human_confirmation_required" for item in blockers):
                return "只生成草稿，等待人工确认后再执行外部写操作。"
            return "存在阻塞项，当前不建议直接推进。"
        # 没有阻塞项时，看 PR 审查有没有给出合并建议
        pr_result = self._by_type(results, "pr_review")
        if pr_result and pr_result.output.get("merge_recommendation"):
            return f"按 PR Agent 建议推进：{pr_result.output.get('merge_recommendation')}。"
        # 有警告但能推进
        if warnings:
            return "可以继续推进，但需要补齐 Observer 标出的证据或团队信息。"
        return "可以继续推进，未发现明显跨域阻塞。"

    # 计算「下一步」清单：从 CI/PR/Issue 结果里提取 debug 步骤、阻塞项、测试建议、行动项，再加上观察建议
    def _next_steps(
        self,
        blockers: list[Any],
        warnings: list[Any],
        results: list[WorkflowTaskResult],
    ) -> list[str]:
        steps: list[str] = []
        ci_result = self._by_type(results, "ci_debug")
        pr_result = self._by_type(results, "pr_review")
        issue_result = self._by_type(results, "issue_analysis")

        # 如果 CI 阻塞合并，取前 3 条 debug 步骤，没有就给默认提示
        if ci_result and ci_result.output.get("is_merge_blocking"):
            debug_steps = [str(item) for item in ci_result.output.get("debug_steps") or [] if str(item).strip()]
            steps.extend(debug_steps[:3] or ["先定位并修复最新失败 CI。"])
        # 如果有 PR 结果，补充阻塞问题和测试建议（各取前 2 条）
        if pr_result:
            blocking = [str(item) for item in pr_result.output.get("blocking_issues") or [] if str(item).strip()]
            tests = [str(item) for item in pr_result.output.get("test_suggestions") or [] if str(item).strip()]
            steps.extend(blocking[:2])
            steps.extend(tests[:2])
        # 如果有 Issue 结果，补充它的 action_items / checklist
        if issue_result:
            actions = [str(item) for item in issue_result.output.get("checklist") or issue_result.output.get("action_items") or [] if str(item).strip()]
            steps.extend(actions[:2])
        # 把每个 blocker / warning 的「建议」也作为下一步
        for finding in [*blockers, *warnings]:
            if finding.recommendation:
                steps.append(str(finding.recommendation))
        # 如果什么都没生成，给一个兜底建议
        if not steps:
            steps.append("把本次 workflow 结论回填到 PR/Issue 描述或团队同步中。")
        # 去重（保持顺序）并最多取 6 条
        return list(dict.fromkeys(steps))[:6]

    # 生成「决策依据」清单：每个 Agent 的结论 + 证据 + 置信度，再加上待确认项
    def _decision_basis(self, results: list[WorkflowTaskResult], observation: WorkflowObservation) -> list[str]:
        basis: list[str] = []
        for result in results:
            # 拼出「置信度」「证据条数」的可读后缀（没有就不加）
            confidence = f"，置信度 {result.confidence:.2f}" if result.confidence is not None else ""
            evidence = f"，证据 {result.evidence_count} 条" if result.evidence_count else ""
            basis.append(f"{result.agent_name}：{result.summary or '无摘要'}{evidence}{confidence}。")
        if observation.findings:
            # 有发现就把每条作为「待确认」列出
            for finding in observation.findings:
                basis.append(f"待确认：{finding.message} 处理建议：{finding.recommendation}")
        else:
            # 没有发现也提示：最终合入/发送仍需人工按团队流程确认
            basis.append("待确认：最终合入、评论或外部发送仍需要人工按团队流程确认。")
        return basis[:8]

    # 工具函数：从结果列表中找「某类型且成功」的第一个结果
    def _by_type(self, results: list[WorkflowTaskResult], task_type: str) -> WorkflowTaskResult | None:
        return next((item for item in results if item.task_type == task_type and item.status == "success"), None)
