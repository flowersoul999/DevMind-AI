"""DevMind AI module."""

# 允许类型注解里直接使用类本身，兼容老版本 Python 注解写法
from __future__ import annotations

# Any：任意类型占位
from typing import Any

# 导入工作流数据模型：观察发现项、观察结论、规格、任务结果
from app.schemas.workflow import WorkflowFinding, WorkflowObservation, WorkflowSpec, WorkflowTaskResult
# 导入 Agent 基类
from app.services.agents.base import BaseAgent


# 模块说明（给零基础同事看的）：
# ObserverAgent 是「多 Agent 工作流里的质检员 / 审计员」。
# 它不参与具体分析，而是在所有专项 Agent 跑完后，统一检查它们产出的结果，
# 找出：谁执行失败了、谁没给证据、谁置信度太低、PR 和 CI 是否冲突、要不要人工确认等。
# 输入：WorkflowSpec（计划）+ 各任务结果列表。
# 输出：一份 WorkflowObservation（观察结论），包含一串 finding（问题清单）和一个总体置信度评分。
# 被谁调用：WorkflowOrchestrator 在每轮跑完任务后都会调用它。
# 典型调用链：各 Agent 出结果 → ObserverAgent.run() 检查并打分 → Orchestrator 据此决定是否重规划 / 让 Synthesis 总结。
# 为什么这样写：把「质量把关」单独抽成一个 Agent，而不是让每个专岗自己说「我靠谱」，
#               就像考试后由独立老师复核答卷，避免既当运动员又当裁判。
class ObserverAgent(BaseAgent):
    """ObserverAgent."""
    name = "observer_agent"
    description = "审查工作流任务输出中的冲突、证据缺口、低置信度和人工确认门槛。"
    available_tools = ["inspect_workflow_outputs"]

    # 主入口：检查所有任务结果，产出观察结论
    async def run(self, input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        # 把规格字典还原成对象
        spec = WorkflowSpec.model_validate(input_data["spec"])
        # 把每个任务结果字典还原成对象
        results = [WorkflowTaskResult.model_validate(item) for item in input_data.get("task_results") or []]
        # findings：用来收集所有发现的问题，初始为空
        findings: list[WorkflowFinding] = []

        # 依次跑各类检查，把发现的问题追加到 findings
        findings.extend(self._status_findings(results))      # 检查任务执行状态（失败/跳过）
        findings.extend(self._evidence_findings(results))    # 检查是否有证据缺口
        findings.extend(self._confidence_findings(results))  # 检查置信度是否过低
        findings.extend(self._merge_gate_findings(results))  # 检查 PR/CI 合并门禁冲突
        findings.extend(self._ownership_findings(results, context))  # 检查 Issue 负责人是否合理
        findings.extend(self._safety_findings(results, spec))       # 检查安全审查是否到位

        # 计算总体置信度：
        # 先取所有「成功任务」的置信度求平均，作为基础分（没有就给 0.55 的中性值）
        confidence_values = [item.confidence for item in results if item.confidence is not None and item.status == "success"]
        base_confidence = sum(confidence_values) / len(confidence_values) if confidence_values else 0.55
        # 每个 blocker（阻塞项）扣 0.2，每个 warning（警告）扣 0.05
        blocker_penalty = 0.2 * len([item for item in findings if item.severity == "blocker"])
        warning_penalty = 0.05 * len([item for item in findings if item.severity == "warning"])
        # 最终置信度夹在 [0.1, 0.95] 之间，并保留两位小数
        overall_confidence = round(max(0.1, min(0.95, base_confidence - blocker_penalty - warning_penalty)), 2)

        # 组装观察结论对象：
        #   是否需要人工复核 = 规格本身要求人工确认，或发现了任何 blocker
        observation = WorkflowObservation(
            findings=findings,
            overall_confidence=overall_confidence,
            human_review_required=spec.requires_human_confirmation or any(item.severity == "blocker" for item in findings),
            summary=self._summary(findings, overall_confidence),
        )
        return observation.model_dump(mode="json")

    # 检查任务执行状态：error 记 blocker，skipped 记 warning
    def _status_findings(self, results: list[WorkflowTaskResult]) -> list[WorkflowFinding]:
        findings = []
        for result in results:
            if result.status == "error":
                findings.append(
                    WorkflowFinding(
                        finding_type="task_error",
                        severity="blocker",  # 失败是严重阻塞
                        message=f"{result.agent_name} 执行失败：{result.error or '未知错误'}",
                        task_ids=[result.task_id],
                        recommendation="先修复任务失败，或用更窄范围重新运行，再信任综合结论。",
                    )
                )
            elif result.status == "skipped":
                findings.append(
                    WorkflowFinding(
                        finding_type="task_skipped",
                        severity="warning",  # 跳过是警告（可能只是依赖失败，不一定致命）
                        message=f"{result.agent_name} 被跳过，因此工作流覆盖不完整。",
                        task_ids=[result.task_id],
                        recommendation="如果该领域会影响决策，请先连接或同步缺失的仓库信号。",
                    )
                )
        return findings

    # 检查证据缺口：成功任务但没给任何证据/引用（repo_health 和 safety_review 豁免，因为它们性质不同）
    def _evidence_findings(self, results: list[WorkflowTaskResult]) -> list[WorkflowFinding]:
        findings = []
        for result in results:
            # 跳过非成功任务，以及 repo_health / safety_review 这两类不强制证据的任务
            if result.status != "success" or result.task_type in {"repo_health", "safety_review"}:
                continue
            # 既没证据条数也没引用 → 记为证据缺口警告
            if result.evidence_count <= 0 and not result.citations:
                findings.append(
                    WorkflowFinding(
                        finding_type="evidence_gap",
                        severity="warning",
                        message=f"{result.agent_name} 没有返回明确证据或引用。",
                        task_ids=[result.task_id],
                        recommendation="在做高风险决策前，优先同步相关 Issue、PR 文件、CI 日志，并实时搜索当前代码。",
                    )
                )
        return findings

    # 检查置信度：成功但置信度低于 0.5 → 警告
    def _confidence_findings(self, results: list[WorkflowTaskResult]) -> list[WorkflowFinding]:
        findings = []
        for result in results:
            if result.status == "success" and result.confidence is not None and result.confidence < 0.5:
                findings.append(
                    WorkflowFinding(
                        finding_type="low_confidence",
                        severity="warning",
                        message=f"{result.agent_name} 的置信度偏低（{result.confidence:.2f}）。",
                        task_ids=[result.task_id],
                        recommendation="行动前请补充更多上下文，或检查底层证据。",
                    )
                )
        return findings

    # 检查「合并门禁」：CI 失败是否阻塞合并、PR 是否有阻塞项，以及最关键的「PR 想合但 CI 说不行」的跨 Agent 冲突
    def _merge_gate_findings(self, results: list[WorkflowTaskResult]) -> list[WorkflowFinding]:
        findings = []
        # 找出成功执行的 PR 审查结果
        pr_result = self._by_type(results, "pr_review")
        # 找出成功执行的 CI 调试结果
        ci_result = self._by_type(results, "ci_debug")
        # 情况一：CI 标记为「阻塞合并」→ blocker
        if ci_result and bool(ci_result.output.get("is_merge_blocking")):
            findings.append(
                WorkflowFinding(
                    finding_type="ci_merge_gate",
                    severity="blocker",
                    message="CI Agent 将最新失败 workflow 标记为阻塞合并。",
                    task_ids=[ci_result.task_id],
                    recommendation="批准合并或发布前，请先修复或解释 CI 失败。",
                )
            )
        # 情况二：PR 审查自己报了阻塞问题 → blocker
        if pr_result and pr_result.output.get("blocking_issues"):
            findings.append(
                WorkflowFinding(
                    finding_type="pr_blocking_issues",
                    severity="blocker",
                    message="PR 审查发现了阻塞问题。",
                    task_ids=[pr_result.task_id],
                    recommendation="解决 PR 阻塞问题后重新运行工作流。",
                )
            )
        # 情况三（最关键）：CI 阻塞合并，但 PR Agent 却建议「合并」→ 跨 Agent 冲突（blocker）
        # 为什么单独拎出来：这就像两个同事一个说「能发车」一个说「轨道断了」，必须有人拍板，
        # 这里规定「CI 门禁优先于 PR 合并建议」，避免带着故障上线。
        if pr_result and ci_result and bool(ci_result.output.get("is_merge_blocking")):
            if self._looks_like_merge_approval(str(pr_result.output.get("merge_recommendation") or "")):
                findings.append(
                    WorkflowFinding(
                        finding_type="cross_agent_conflict",
                        severity="blocker",
                        message="PR Agent 倾向合并，但 CI Agent 报告了阻塞合并的失败。",
                        task_ids=[pr_result.task_id, ci_result.task_id],
                        recommendation="在失败解决前，让 CI 门禁优先于 PR 合并建议。",
                    )
                )
        return findings

    # 检查 Issue 负责人是否合理：负责人为空、是「待分配」，或不在团队名单里 → 警告
    def _ownership_findings(self, results: list[WorkflowTaskResult], context: dict[str, Any]) -> list[WorkflowFinding]:
        # 先找 Issue 分析结果
        issue_result = self._by_type(results, "issue_analysis")
        if not issue_result:
            return []
        # 取出建议的负责人（小写、去空白）
        owner = str(issue_result.output.get("suggested_owner") or "").strip().lower()
        if not owner:
            return []
        # 团队名单：收集所有成员名字（小写）
        team_members = context.get("team_members") or []
        known_names = {
            str(member.get("name") or "").strip().lower()
            for member in team_members
            if isinstance(member, dict) and str(member.get("name") or "").strip()
        }
        # 「未分配」标记集合
        unassigned_markers = {"unassigned", "待分配"}
        # 如果负责人是未分配，或者团队已配置但它不在名单里 → 警告（让人工确认）
        if owner in unassigned_markers or (known_names and owner not in known_names):
            return [
                WorkflowFinding(
                    finding_type="owner_needs_review",
                    severity="warning",
                    message="Issue 负责人建议为空，或不在已配置团队成员中。",
                    task_ids=[issue_result.task_id],
                    recommendation="依赖自动负责人分配前，请先更新团队成员画像。",
                )
            ]
        # 如果团队根本没配置任何成员 → 提示先加成员画像
        if not known_names:
            return [
                WorkflowFinding(
                    finding_type="team_profile_missing",
                    severity="warning",
                    message="当前还没有配置用于负责人路由的 DevFlow 团队成员。",
                    task_ids=[issue_result.task_id],
                    recommendation="请添加团队成员画像，让负责人推荐更可解释。",
                )
            ]
        return []

    # 检查安全审查是否到位：
    # 1) 规格要求人工确认但没跑 Safety Agent → blocker
    # 2) Safety Agent 说需要人工确认 → blocker（只给草稿，不真执行外部动作）
    def _safety_findings(self, results: list[WorkflowTaskResult], spec: WorkflowSpec) -> list[WorkflowFinding]:
        safety_result = self._by_type(results, "safety_review")
        if not safety_result and spec.requires_human_confirmation:
            return [
                WorkflowFinding(
                    finding_type="missing_safety_review",
                    severity="blocker",
                    message="工作流需要人工确认，但没有运行 SafetyAgent 任务。",
                    recommendation="在起草评论、标签、创建/关闭 Issue 或外部发送前，请先运行 SafetyAgent。",
                )
            ]
        if safety_result and safety_result.output.get("requires_confirmation"):
            return [
                WorkflowFinding(
                    finding_type="human_confirmation_required",
                    severity="blocker",
                    message="SafetyAgent 要求用户对本次写入类操作进行人工确认。",
                    task_ids=[safety_result.task_id],
                    recommendation="只返回草稿；MVP 模式下不要执行外部动作。",
                )
            ]
        return []

    # 工具函数：从结果列表中找「某类型且成功」的第一个结果（找不到返回 None）
    def _by_type(self, results: list[WorkflowTaskResult], task_type: str) -> WorkflowTaskResult | None:
        return next((item for item in results if item.task_type == task_type and item.status == "success"), None)

    # 判断一个文本是否「看起来像批准合并」：包含 approve/merge/建议合并 等词，且不含 after/暂缓/修复后 等限定词
    def _looks_like_merge_approval(self, value: str) -> bool:
        lowered = value.lower()
        return any(token in lowered for token in ["approve", "merge", "建议合并", "建议合入"]) and not any(
            token in lowered for token in ["after", "blocked", "暂缓", "修复后", "修改后"]
        )

    # 根据发现列表和置信度生成一句人话总结
    def _summary(self, findings: list[WorkflowFinding], confidence: float) -> str:
        blockers = len([item for item in findings if item.severity == "blocker"])
        warnings = len([item for item in findings if item.severity == "warning"])
        if blockers:
            return f"Observer 发现 {blockers} 个阻塞项和 {warnings} 个警告；置信度 {confidence:.2f}。"
        if warnings:
            return f"Observer 发现 {warnings} 个警告；置信度 {confidence:.2f}。"
        return f"Observer 没有发现跨 Agent 阻塞项；置信度 {confidence:.2f}。"
