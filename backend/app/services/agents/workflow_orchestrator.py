"""DevMind AI module."""

# 允许类型注解里直接使用类本身（前向引用），兼容老版本 Python 注解写法
from __future__ import annotations

# asyncio：Python 内置的「异步编程」库，用来并发运行多个协程（这里用于并行跑多个任务）
import asyncio
# time：时间相关工具，这里用来统计整个工作流耗时
import time
# 类型工具：Awaitable 表示「可 await 的对象」（协程），Callable 表示「可调用的函数/方法」；用于精确标注「任务执行器」的类型
from collections.abc import Awaitable, Callable
# datetime / timezone：处理带时区的时间戳（记录每个任务开始/结束时间）
from datetime import datetime, timezone
# Any：任意类型占位
from typing import Any

# 导入全局配置（例如任务超时时间等）
from app.core.config import settings
# 导入工作流数据模型：观察结论、最终报告、规格、任务、任务结果
from app.schemas.workflow import WorkflowObservation, WorkflowReport, WorkflowSpec, WorkflowTask, WorkflowTaskResult
# 导入 Agent 基类
from app.services.agents.base import BaseAgent
# 导入三个协作组件：观察员、规划器、综合器
from app.services.agents.observer_agent import ObserverAgent
from app.services.agents.planner_agent import PlannerAgent
from app.services.agents.synthesis_agent import SynthesisAgent


# 模块说明（给零基础同事看的）：
# WorkflowOrchestrator 是「多 Agent 工作流的总指挥 / 包工头」。
# 它把 Planner（编剧）、各专项 Agent（工人）、Observer（质检）、Synthesis（总结汇报）串成一条流水线。
# 输入：用户目标 message + 仓库上下文 context。
# 输出：一份 WorkflowReport（最终工程决策报告，含每个任务结果、置信度、引用、耗时指标等）。
# 被谁调用：通常是上层 API 路由或 ChatAgent 的 workflow.run_engineering_review 工具。
# 典型调用链：run() → Planner 出计划 → 按依赖图并行跑任务 → Observer 检查 →（必要时 replan）→ Synthesis 综合成报告。
# 为什么这样写：用「依赖图 + 并行批处理」而不是顺序执行，是因为各专项 Agent 互不依赖时可以同时跑，省时间；
#              用「replan 循环」是因为第一轮常常证据不足，允许补任务后再综合，结论更靠谱。

# 类型别名：定义一个「任务执行器」的函数签名。
# 它接收（某个任务对象、整份规格、上下文），返回一个协程，最终结果是 WorkflowTaskResult 或字典。
# 为什么定义这个：orchestrator 本身不实现具体任务逻辑，而是通过这个「执行器映射表」去调用真正的专项 Agent，
# 这样编排层与具体业务解耦（要换/加一个新 Agent，只需登记一个执行器即可）。
WorkflowTaskRunner = Callable[[WorkflowTask, WorkflowSpec, dict[str, Any]], Awaitable[WorkflowTaskResult | dict[str, Any]]]


# 总指挥类
class WorkflowOrchestrator(BaseAgent):
    """WorkflowOrchestrator."""
    name = "workflow_orchestrator"
    description = "协调 Planner、专用 Agent、Observer 和 Synthesis 完成工程决策。"
    available_tools = ["run_engineering_workflow"]

    def __init__(
        self,
        task_runners: dict[str, WorkflowTaskRunner],
        planner: PlannerAgent | None = None,
        observer: ObserverAgent | None = None,
        synthesizer: SynthesisAgent | None = None,
    ) -> None:
        # task_runners：一个字典，「任务类型 → 对应的执行器函数」。例如 "pr_review" 对应真正跑 PR 审查的函数
        self.task_runners = task_runners
        # 保存规划器实例；如果没传就新建一个默认的（依赖注入 + 默认值的写法，方便测试时替换）
        self.planner = planner or PlannerAgent()
        # 保存观察员实例（质检）
        self.observer = observer or ObserverAgent()
        # 保存综合器实例（写最终报告）
        self.synthesizer = synthesizer or SynthesisAgent()

    # 主入口：先让 Planner 出计划，再按计划执行并返回报告
    async def run(self, input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        # 调用规划器生成工作流规格（dict 形式）
        spec_data = await self.planner.run(input_data, context)
        # 把规格交给 run_spec 去真正执行并产出报告
        return await self.run_spec(spec_data, context)

    # 执行一份工作流规格（支持「最多重规划几次」的可选参数 allow_replan）
    async def run_spec(
        self,
        spec_data: WorkflowSpec | dict[str, Any],
        context: dict[str, Any],
        *,
        allow_replan: bool = True,
    ) -> dict[str, Any]:
        # 记录开始时间（用高精度计时器，后面算耗时）
        started = time.perf_counter()
        # 如果传入的是字典就反序列化成对象，已经是对象就直接用
        spec = spec_data if isinstance(spec_data, WorkflowSpec) else WorkflowSpec.model_validate(spec_data)
        # current_spec：当前正在执行的规格（重规划后会被替换成新规格）
        current_spec = spec
        # 累积所有轮次的「任务结果」
        task_results: list[WorkflowTaskResult] = []
        # 累积每一轮的「观察结论」
        observations: list[WorkflowObservation] = []
        # 记录每一轮的详情（规格、结果、观察），用于最终生成事件轨迹
        round_records: list[dict[str, Any]] = []
        # 记录每次「重新规划」的事件（用于报告里展示复盘过程）
        replan_events: list[dict[str, Any]] = []
        # 计算最多允许重规划几次（受 context 和 allow_replan 限制）
        max_replans = self._max_replans(context, allow_replan)

        # 主循环：跑 (max_replans + 1) 轮。第 1 轮是原始计划，后面的轮次是重规划后的计划
        for attempt in range(max_replans + 1):
            # 按依赖图执行当前规格里的所有任务，得到本轮结果列表
            round_results = await self._run_task_graph(current_spec, context, previous_results=task_results)
            # 把本轮结果追加到总结果里（之前轮次已成功的任务不会重复跑）
            task_results.extend(round_results)
            # 让 Observer 检查本轮结果，产出观察结论
            observation = await self._observe(current_spec, task_results, context)
            observations.append(observation)
            # 记录本轮快照
            round_records.append({"spec": current_spec, "results": round_results, "observation": observation})

            # 如果已经是最后一轮，跳出循环，不再重规划
            if attempt >= max_replans:
                break

            # 判断是否需要重规划（基于结果和观察结论）。不需要就停
            replan_reason = self._replan_reason(task_results, observation)
            if replan_reason is None:
                break
            # 如果规划器没有 replan 方法（理论上都有），也停止
            if not hasattr(self.planner, "replan"):
                break

            # 调用规划器的 replan，传入旧规格、所有结果、观察结论和原因，得到新规格（dict）
            next_spec_data = await self.planner.replan(
                {
                    "spec": current_spec.model_dump(mode="json"),
                    "task_results": [item.model_dump(mode="json") for item in task_results],
                    "observation": observation.model_dump(mode="json"),
                    "reason": replan_reason,
                    "replan_iteration": attempt + 1,
                },
                context,
            )
            # 把新规格与旧规格做「规范化合并」：保留原始 workflow_id、验收标准等关键字段
            next_spec = self._normalize_replanned_spec(
                original_spec=spec,
                current_spec=current_spec,
                next_spec=WorkflowSpec.model_validate(next_spec_data),
            )
            # 如果新旧任务图完全一样（补的任务集合没变化），再规划也没意义，直接停
            if self._same_task_graph(current_spec, next_spec):
                break

            # 记录这次重规划事件（包含新增了哪些任务）
            replan_events.append(
                {
                    "type": "workflow_replan",
                    "workflow_id": spec.workflow_id,
                    "iteration": attempt + 1,
                    "reason": replan_reason,
                    "from_task_count": len(current_spec.tasks),
                    "to_task_count": len(next_spec.tasks),
                    "added_tasks": [
                        task.id
                        for task in next_spec.tasks
                        if task.id not in {existing.id for existing in current_spec.tasks}
                    ],
                }
            )
            # 把当前规格切换成新规格，进入下一轮循环
            current_spec = next_spec

        # 取最后一轮的观察结论作为最终观察；如果一轮都没跑（理论上不会），给个占位
        observation = observations[-1] if observations else WorkflowObservation(summary="Workflow did not run.")
        # 让综合器把「规格 + 结果 + 观察」综合成最终回答文字（answer）
        synthesis = await self.synthesizer.run(
            {
                "spec": current_spec.model_dump(mode="json"),
                "task_results": [item.model_dump(mode="json") for item in task_results],
                "observation": observation.model_dump(mode="json"),
            },
            context,
        )
        # 组装最终的工作流报告对象
        report = WorkflowReport(
            spec=current_spec,
            task_results=task_results,
            observation=observation,
            final_answer=str(synthesis.get("answer") or ""),  # 综合器给出的结论文字
            # 工具调用记录（每个任务对应一次 workflow.<task_type> 调用，外加重规划调用）
            tool_calls=self._tool_calls(current_spec, task_results, replan_events),
            # 引用/证据汇总（去重后的所有任务引用）
            citations=self._citations(task_results),
            # 各种指标统计：耗时、任务数、成功/失败/跳过数、观察发现数等
            metrics={
                "duration_ms": int((time.perf_counter() - started) * 1000),  # 总耗时（毫秒）
                "task_count": len(task_results),
                "plan_task_count": len(current_spec.tasks),
                "execution_rounds": len(observations),  # 执行了几轮（含重规划）
                "replan_count": len(replan_events),  # 重规划次数
                "success_count": len([item for item in task_results if item.status == "success"]),
                "error_count": len([item for item in task_results if item.status == "error"]),
                "skipped_count": len([item for item in task_results if item.status == "skipped"]),
                "observer_findings": len(observation.findings),
            },
            # 完整事件轨迹（供前端回放/调试用）
            agent_events=self._agent_events(round_records, replan_events),
        )
        # 转成字典返回
        return report.model_dump(mode="json")

    # 调用 Observer 检查，并把返回字典还原成 WorkflowObservation 对象
    async def _observe(
        self,
        spec: WorkflowSpec,
        task_results: list[WorkflowTaskResult],
        context: dict[str, Any],
    ) -> WorkflowObservation:
        observation_data = await self.observer.run(
            {
                "spec": spec.model_dump(mode="json"),
                "task_results": [item.model_dump(mode="json") for item in task_results],
            },
            context,
        )
        return WorkflowObservation.model_validate(observation_data)

    # 按依赖图执行任务：处理「依赖完成才能跑」「依赖失败则跳过」「并行跑无依赖任务」等逻辑
    async def _run_task_graph(
        self,
        spec: WorkflowSpec,
        context: dict[str, Any],
        previous_results: list[WorkflowTaskResult] | None = None,
    ) -> list[WorkflowTaskResult]:
        # 把所有任务放进 pending（待办）字典，key 是任务 id
        pending = {task.id: task for task in spec.tasks}
        # completed：上一轮已成功完成的任务（直接复用，不重跑），key 是任务 id
        completed: dict[str, WorkflowTaskResult] = {
            result.task_id: result
            for result in previous_results or []
            if result.status == "success"
        }
        # blocked_dependencies：上一轮「失败或跳过」的任务，它们会让依赖它们的任务也无法跑
        blocked_dependencies: dict[str, WorkflowTaskResult] = {
            result.task_id: result
            for result in previous_results or []
            if result.status in {"error", "skipped"}
        }
        # 已完成的任务从待办里移除
        for task_id in completed:
            pending.pop(task_id, None)
        # results：本轮新产生的任务结果列表
        results: list[WorkflowTaskResult] = []

        # 主循环：只要还有待办任务就继续
        while pending:
            # 找出「被失败依赖卡住」的任务（它的某个依赖在 blocked_dependencies 里）
            blocked = [
                task
                for task in pending.values()
                if any(dependency in blocked_dependencies for dependency in task.dependencies)
            ]
            # 把这些被卡住的任务标记为 skipped，并记录原因（依赖失败）
            for task in blocked:
                failed_dependencies = [
                    dependency
                    for dependency in task.dependencies
                    if dependency in blocked_dependencies
                ]
                result = self._dependency_blocked_result(task, failed_dependencies)
                blocked_dependencies[task.id] = result
                results.append(result)
                pending.pop(task.id, None)
            # 如果处理完被卡任务后没待办了，结束
            if not pending:
                break

            # 找出「所有依赖都已成功完成」的任务，这些可以准备跑（ready = 就绪）
            ready = [
                task
                for task in pending.values()
                if all(dependency in completed for dependency in task.dependencies)
            ]
            # 如果没有任何就绪任务，说明出现了「依赖环」或「依赖缺失」——无法继续，记录错误并结束
            if not ready:
                for task in pending.values():
                    result = self._error_result(task, "任务依赖存在循环或缺失依赖。", error_kind="dependency_cycle_or_missing")
                    blocked_dependencies[task.id] = result
                    results.append(result)
                break

            # 取前 max_parallel_task 个就绪任务作为一个「批次」，并行执行
            batch = ready[: spec.max_parallel_task]
            # asyncio.gather：并发地同时跑这一批任务，而不是一个个等。为什么：省时间。
            batch_results = await asyncio.gather(
                *(self._run_single_task(task, spec, context) for task in batch),
                return_exceptions=False,  # 如果有任务抛异常，直接向上抛（由上层处理）
            )
            # 把批次结果分别归档：成功进 completed，否则进 blocked_dependencies（可能卡住后续）
            for task, result in zip(batch, batch_results):
                if result.status == "success":
                    completed[task.id] = result
                else:
                    blocked_dependencies[task.id] = result
                results.append(result)
                pending.pop(task.id, None)

        return results

    # 执行单个任务：找对应执行器，加超时保护，记录开始/结束时间，统一处理超时与异常
    async def _run_single_task(
        self,
        task: WorkflowTask,
        spec: WorkflowSpec,
        context: dict[str, Any],
    ) -> WorkflowTaskResult:
        # 记录任务开始时间（UTC 时区）
        started_at = datetime.now(timezone.utc)
        # 根据任务类型找到注册的执行器函数
        runner = self.task_runners.get(task.task_type)
        # 没注册对应执行器 → 直接返回一个 skipped 结果（而不是报错崩溃）
        if runner is None:
            return WorkflowTaskResult(
                task_id=task.id,
                agent_name=task.agent_name,
                task_type=task.task_type,
                status="skipped",
                summary=f"没有为 {task.task_type} 注册任务执行器。",
                started_at=started_at,
                completed_at=datetime.now(timezone.utc),
            )
        try:
            # 用 asyncio.wait_for 给任务加「超时上限」，防止某个 Agent 卡死拖垮整条流水线
            raw_result = await asyncio.wait_for(
                runner(task, spec, context),
                timeout=self._task_timeout_seconds(context),
            )
            # 执行器可能返回字典或对象，统一规范成 WorkflowTaskResult
            result = raw_result if isinstance(raw_result, WorkflowTaskResult) else WorkflowTaskResult.model_validate(raw_result)
            # 补全开始/结束时间（如果执行器没填）
            result.started_at = result.started_at or started_at
            result.completed_at = result.completed_at or datetime.now(timezone.utc)
            return result
        except TimeoutError:
            # 超时：返回一个 error 结果，说明超过多少秒
            return self._error_result(
                task,
                f"{task.agent_name} 执行超过 {self._task_timeout_seconds(context):g} 秒。",
                started_at=started_at,
                error_kind="timeout",
            )
        except Exception as exc:
            # 其他异常：也包成 error 结果，不让单个任务崩溃整个工作流
            return self._error_result(task, str(exc), started_at=started_at, error_kind="task_runtime_error")

    # 构造一个「任务执行失败」的结果对象
    def _error_result(
        self,
        task: WorkflowTask,
        error: str,
        started_at: datetime | None = None,
        *,
        error_kind: str = "task_runtime_error",
    ) -> WorkflowTaskResult:
        return WorkflowTaskResult(
            task_id=task.id,
            agent_name=task.agent_name,
            task_type=task.task_type,
            status="error",
            summary=f"{task.agent_name} 执行失败。",
            output={"error_kind": error_kind},  # 记录错误种类，方便上层分类
            error=error,
            started_at=started_at or datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
        )

    # 构造一个「因依赖失败而被跳过」的结果对象
    def _dependency_blocked_result(self, task: WorkflowTask, failed_dependencies: list[str]) -> WorkflowTaskResult:
        return WorkflowTaskResult(
            task_id=task.id,
            agent_name=task.agent_name,
            task_type=task.task_type,
            status="skipped",
            summary=f"{task.agent_name} 未执行，因为依赖任务未成功：{', '.join(failed_dependencies)}。",
            output={"error_kind": "dependency_failed", "failed_dependencies": failed_dependencies},
            confidence=0.0,  # 没跑，置信度为 0
            completed_at=datetime.now(timezone.utc),
        )

    # 计算单个任务的超时秒数：优先用 context 传的值，否则用配置默认值；并做「至少 0.001 秒」的保护
    def _task_timeout_seconds(self, context: dict[str, Any]) -> float:
        try:
            value = float(context.get("workflow_task_timeout_seconds", settings.workflow_task_timeout_seconds))
        except (TypeError, ValueError):
            value = settings.workflow_task_timeout_seconds
        return max(0.001, value)

    # 计算最多允许重规划几次：默认 1 次，最多 2 次，最少 0 次（allow_replan 为 False 或 disable_replan 时直接 0）
    def _max_replans(self, context: dict[str, Any], allow_replan: bool) -> int:
        if not allow_replan or context.get("disable_replan"):
            return 0
        try:
            requested = int(context.get("max_replans", 1))
        except (TypeError, ValueError):
            requested = 1
        return max(0, min(2, requested))  # 夹紧在 [0, 2]

    # 判断是否需要重规划，并返回原因（或 None 表示不需要）
    def _replan_reason(self, results: list[WorkflowTaskResult], observation: WorkflowObservation) -> str | None:
        # 这些类型的发现属于「可以靠补任务修复」的问题
        fixable_findings = {
            "task_error",
            "task_skipped",
            "evidence_gap",
            "low_confidence",
            "missing_safety_review",
            "owner_needs_review",
            "team_profile_missing",
        }
        # 只要 Observer 标了上述任一问题，就返回对应原因（为什么：补跑相关 Agent 通常能解决）
        for finding in observation.findings:
            if finding.finding_type in fixable_findings:
                return f"{finding.finding_type}: {finding.recommendation or finding.message}"
        # 如果有任务直接 error，也建议重规划
        if any(result.status == "error" for result in results):
            return "task_error: at least one workflow task failed"
        # 如果有成功任务但置信度过低（< 0.45），说明证据不足，建议补
        low_confidence_results = [
            result
            for result in results
            if result.status == "success" and result.confidence is not None and result.confidence < 0.45
        ]
        if low_confidence_results:
            return "low_confidence: one or more successful task results have weak confidence"
        # 如果整体观察置信度也低于阈值，建议重规划
        if results and observation.overall_confidence < 0.45:
            return "low_confidence: observer confidence is below the workflow threshold"
        return None

    # 把「重规划后的新规格」与旧规格对齐：保留原始 id、目标、验收标准、并行度、人工确认标志
    def _normalize_replanned_spec(
        self,
        *,
        original_spec: WorkflowSpec,
        current_spec: WorkflowSpec,
        next_spec: WorkflowSpec,
    ) -> WorkflowSpec:
        normalized = next_spec.model_copy(deep=True)  # 深拷贝，避免改到原对象
        normalized.workflow_id = original_spec.workflow_id  # 保持同一工作流 id 不变
        normalized.goal = normalized.goal or current_spec.goal  # 目标缺失时沿用当前
        normalized.trigger_message = normalized.trigger_message or current_spec.trigger_message
        # 验收标准缺失时，沿用当前规格的（防止重规划后丢掉了规矩）
        if not normalized.acceptance_criteria:
            normalized.acceptance_criteria = list(current_spec.acceptance_criteria)
        normalized.max_parallel_tasks = current_spec.max_parallel_tasks  # 并行度保持一致
        # 人工确认标志：只要旧规格或新规格任一方要求，就保持要求（只严不松）
        normalized.requires_human_confirmation = (
            current_spec.requires_human_confirmation or normalized.requires_human_confirmation
        )
        return normalized

    # 判断两个规格的「任务图」是否完全一致（只看 id、类型、依赖），用来避免无意义的重规划
    def _same_task_graph(self, left: WorkflowSpec, right: WorkflowSpec) -> bool:
        # 定义一个签名函数：把每个任务压成 (id, 类型, 依赖元组)
        def signature(spec: WorkflowSpec) -> list[tuple[str, str, tuple[str, ...]]]:
            return [(task.id, task.task_type, tuple(task.dependencies)) for task in spec.tasks]

        return signature(left) == signature(right)

    # 收集工具调用记录：每个任务结果对应一次 workflow.<类型> 调用；外加每次重规划调用
    def _tool_calls(
        self,
        spec: WorkflowSpec,
        results: list[WorkflowTaskResult],
        replan_events: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        calls = [
            {
                "tool_name": f"workflow.{result.task_type}",
                "arguments": {
                    "workflow_id": spec.workflow_id,
                    "task_id": result.task_id,
                    "agent": result.agent_name,
                },
                "status": result.status,
                "error": result.error,
            }
            for result in results
        ]
        # 把重规划事件也作为一次「replan 工具调用」补充进去
        calls.extend(
            {
                "tool_name": "planner_agent.replan_workflow_spec",
                "arguments": {
                    "workflow_id": spec.workflow_id,
                    "iteration": event.get("iteration"),
                    "reason": event.get("reason"),
                },
                "status": "success",
                "error": None,
            }
            for event in replan_events
        )
        return calls

    # 汇总并去重所有任务产生的「引用/证据」(citations)
    def _citations(self, results: list[WorkflowTaskResult]) -> list[dict[str, Any]]:
        citations: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()  # 用 (type, id, title) 作为去重键
        for result in results:
            for citation in result.citations:
                key = (
                    str(citation.get("type") or ""),
                    str(citation.get("id") or citation.get("source_id") or ""),
                    str(citation.get("title") or ""),
                )
                if key in seen:
                    continue
                seen.add(key)
                citations.append(citation)
        return citations

    # 把多轮执行过程整理成「事件轨迹」，供前端回放或调试（包含每轮规格、任务结果、观察）
    def _agent_events(
        self,
        round_records: list[dict[str, Any]],
        replan_events: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for index, record in enumerate(round_records):
            # 第一轮之后的每一轮，前面都先插入对应的重规划事件
            if index > 0 and index - 1 < len(replan_events):
                events.append(replan_events[index - 1])
            spec = record["spec"]
            # 记录本轮的规格快照
            events.append(
                {
                    "type": "workflow_spec",
                    "workflow_id": spec.workflow_id,
                    "goal": spec.goal,
                    "iteration": index,
                    "is_replan": index > 0,  # 是否重规划轮次
                    "tasks": [
                        {
                            "id": task.id,
                            "agent_name": task.agent_name,
                            "task_type": task.task_type,
                            "dependencies": task.dependencies,
                            "claim": task.claim.model_dump(mode="json"),
                        }
                        for task in spec.tasks
                    ],
                }
            )
            # 记录本轮每个任务的结果（并摘出 skill 运行时信息，如果有）
            for result in record["results"]:
                skill_runtime = result.output.get("_skill_runtime") if isinstance(result.output, dict) else None
                events.append(
                    {
                        "type": "workflow_task_result",
                        "workflow_id": spec.workflow_id,
                        "iteration": index,
                        "task_id": result.task_id,
                        "agent_name": result.agent_name,
                        "task_type": result.task_type,
                        "status": result.status,
                        "summary": result.summary,
                        "confidence": result.confidence,
                        "evidence_count": result.evidence_count,
                        # 只挑选 skill 相关的几个字段附上，避免把整个 output 塞进来
                        **(
                            {
                                key: value
                                for key, value in skill_runtime.items()
                                if key
                                in {
                                    "skill_name",
                                    "skill_title",
                                    "skill_version",
                                    "skill_steps",
                                    "skill_activation_mode",
                                    "skill_activation_reason",
                                    "skill_instruction_digest",
                                    "validation",
                                }
                            }
                            if isinstance(skill_runtime, dict)
                            else {}
                        ),
                    }
                )
            # 记录本轮的观察结论
            observation = record["observation"]
            events.append(
                {
                    "type": "workflow_observer_result",
                    "workflow_id": spec.workflow_id,
                    "iteration": index,
                    "is_final": index == len(round_records) - 1,  # 是否最后一轮
                    "overall_confidence": observation.overall_confidence,
                    "human_review_required": observation.human_review_required,
                    "findings": [finding.model_dump(mode="json") for finding in observation.findings],
                }
            )
        return events
