"""DevMind AI module."""

# 允许在类型注解里直接使用类型本身（例如函数参数标注 WorkflowSpec 而无需引号），主要是为兼容老版本 Python 注解写法
from __future__ import annotations

# Any：任意类型占位符
from typing import Any

# 从工作流相关的数据模型里导入一堆结构定义：
#   WorkflowClaim：单个任务「允许查看/使用的证据来源范围」声明（防止 Agent 越权乱翻数据）
#   WorkflowObservation：Observer 检查后的观察结论
#   WorkflowSpec：一次完整工作流的「规格说明书」（目标、任务列表、验收标准等）
#   WorkflowTask：工作流里的单个子任务
#   WorkflowTaskResult：单个子任务的执行结果
from app.schemas.workflow import WorkflowClaim, WorkflowObservation, WorkflowSpec, WorkflowTask, WorkflowTaskResult
# 导入 Agent 公共基类，PlannerAgent 要继承它
from app.services.agents.base import BaseAgent


# 模块说明（给零基础同事看的）：
# PlannerAgent 是「多 Agent 工作流的总导演 / 编剧」。
# 它不真正去分析 Issue、审查 PR，而是根据用户的一句话目标（message）和当前仓库状态（context），
# 决定「这次应该派哪些专项 Agent 上场、谁先谁后、各自能看什么数据」。
# 输入：用户问题 message + 仓库上下文 context（是否有关联 Issue/PR/CI、团队成员等）。
# 输出：一份 WorkflowSpec（工作流规格），里面是一张任务清单（例如先跑 repo_health，再跑 pr_review）。
# 被谁调用：WorkflowOrchestrator.run() 一开场就会调用它来生成计划；
#           当 Observer 发现证据不足、缺安全审查时，orchestrator 还会调用它的 replan() 重新规划。
# 典型调用链：用户输入 → Orchestrator → PlannerAgent.run() 产出 Spec → Orchestrator 按 Spec 调度各专项 Agent。
class PlannerAgent(BaseAgent):
    """PlannerAgent."""
    # Agent 的唯一名字
    name = "planner_agent"
    # 一句话职责说明
    description = "根据用户目标和可用仓库信号，构建有边界的工程工作流规格。"
    # 声明它能用的工具（这里主要是「创建工作流规格」）
    available_tools = ["create_workflow_spec"]

    # 主入口：根据输入和上下文，生成一份工作流规格（WorkflowSpec）
    async def run(self, input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        # 从输入里取出用户消息：优先取 message，其次取 goal，都没有就空字符串；strip() 去掉首尾空白
        message = str(input_data.get("message") or input_data.get("goal") or "").strip()
        # 把消息转小写，方便后面做「关键词命中」判断（大小写不敏感匹配）
        lowered = message.lower()
        # 用来收集本次要派发的任务列表，初始为空
        tasks: list[WorkflowTask] = []
        # 根据 context 里有没有 issue_number，生成给人类看的「Issue 标签」文案（例如 "Issue #123" 或 "当前 Issue"）
        issue_label = f"Issue #{context['issue_number']}" if context.get("issue_number") is not None else "当前 Issue"
        # 同理生成 PR 标签文案
        pr_label = f"PR #{context['pr_number']}" if context.get("pr_number") is not None else "当前 PR"
        # 同理生成 CI/Workflow 运行标签文案（失败流水线相关）
        ci_label = (
            f"CI/Workflow Run #{context['workflow_run_number']}"
            if context.get("workflow_run_number") is not None
            else "最新失败的 CI 运行"
        )
        # 判断是否「存在一次 CI 运行」：优先看 has_ci_run，否则退而看 has_failed_ci 这个兼容字段
        has_ci_run = context.get("has_ci_run", context.get("has_failed_ci"))

        # 如果仓库存在（默认视为存在），第一个任务永远是「仓库健康扫描」（repo_health）。
        # 为什么：让所有专项 Agent 都能先拿到一份统一的仓库基线快照，后面的结论才有共同证据基础，避免各说各话。
        if context.get("has_repo", True):
            tasks.append(
                # 组装一个 WorkflowTask（子任务）对象
                WorkflowTask(
                    id="repo_health",  # 任务 id，后续任务可以用它做依赖
                    agent_name="repo_health_agent",  # 负责执行该任务的 Agent 名字
                    task_type="repo_health",  # 任务类型，orchestrator 据此找到对应的执行器
                    objective="在专用分析前捕获仓库基线。",  # 这个任务的目标描述
                    claim=WorkflowClaim(
                        target_type="repository",  # 证据目标类型：整个仓库
                        target_id=context.get("repo_id"),  # 目标 id
                        allowed_sources=["issues", "pull_requests", "workflow_runs"],  # 允许它看的数据源
                        reason="共享仓库快照可以让后续 Agent 结论有证据支撑。",  # 为什么要这个权限
                    ),
                )
            )

        # 判断用户问题是不是「宽泛的工程类问题」（例如「整体工程状态怎么样」）。
        # 宽泛问题通常需要把能看的专项都跑一遍。
        broad = self._is_broad_engineering_question(lowered)
        # 如果用户提到了 Issue，或者这是个宽泛问题，并且仓库里确实有关联 Issue，就加一个 Issue 分析任务
        if context.get("has_issue") and (broad or self._mentions_issue(lowered)):
            tasks.append(
                WorkflowTask(
                    id="issue_analysis",
                    agent_name="issue_analyst_agent",
                    task_type="issue_analysis",
                    # 目标文案里嵌入上面生成的 issue_label，告诉分析师具体分析哪个 Issue
                    objective=f"分析 {issue_label} 的分诊结论、优先级、负责人、证据和下一步动作。",
                    # 如果已经有 repo_health 任务，就声明「本任务依赖它先完成」（先有基线再分析）
                    dependencies=["repo_health"] if self._has_task(tasks, "repo_health") else [],
                    claim=WorkflowClaim(
                        target_type="issue",
                        target_id=context.get("issue_id"),
                        # 允许看 Issue 本体、相似 Issue、代码、文档、团队成员等
                        allowed_sources=["issue", "similar_issues", "code", "documents", "team_members"],
                        reason="判断负责人和优先级需要明确 Issue 范围。",
                    ),
                    # 透传给 Issue Agent 的关键入参：Issue 编号
                    input_hint={"issue_number": context.get("issue_number")},
                )
            )

        # 类似上面：提到 PR 或宽泛问题 + 仓库有关联 PR，就加 PR 审查任务
        if context.get("has_pr") and (broad or self._mentions_pr(lowered)):
            tasks.append(
                WorkflowTask(
                    id="pr_review",
                    agent_name="pr_review_agent",
                    task_type="pr_review",
                    objective=f"审查 {pr_label} diff、风险、阻塞问题和测试建议。",
                    dependencies=["repo_health"] if self._has_task(tasks, "repo_health") else [],
                    claim=WorkflowClaim(
                        target_type="pull_request",
                        target_id=context.get("pr_id"),
                        allowed_sources=["pull_request", "diff", "review_comments", "ci_summary", "documents"],
                        reason="合并就绪度和风险判断需要明确 PR 范围。",
                    ),
                    input_hint={"pr_number": context.get("pr_number")},
                    # 如果用户明确提到「合并」，把这个任务标记为关键（critical），因为它影响是否放行
                    critical=self._mentions_merge(lowered),
                )
            )

        # 类似：提到 CI / 合并，或宽泛问题 + 有失败 CI，就加 CI 调试任务（且一律标记为关键，因为 CI 失败常阻塞发布）
        if has_ci_run and (broad or self._mentions_ci(lowered) or self._mentions_merge(lowered)):
            tasks.append(
                WorkflowTask(
                    id="ci_debug",
                    agent_name="ci_debug_agent",
                    task_type="ci_debug",
                    objective=f"分析 {ci_label}，并判断是否阻塞合并或发布。",
                    dependencies=["repo_health"] if self._has_task(tasks, "repo_health") else [],
                    claim=WorkflowClaim(
                        target_type="workflow_run",
                        target_id=context.get("workflow_run_id"),
                        allowed_sources=["workflow_run", "jobs", "logs", "recent_prs", "code"],
                        reason="任何合并或就绪度结论都需要先看 CI 失败上下文。",
                    ),
                    input_hint={"workflow_run_number": context.get("workflow_run_number")},
                    critical=True,
                )
            )

        # 如果用户开启 RAG 检索任务，或问题里提到历史/相似/知识库，就加一个「历史证据检索」任务
        if context.get("enable_rag_task") or self._mentions_history(lowered):
            tasks.append(
                WorkflowTask(
                    id="rag_search",
                    agent_name="rag_context_agent",
                    task_type="rag_search",
                    objective="检索与目标相关的历史 Issue、PR、失败 CI 日志、项目文档和已批准知识。",
                    dependencies=["repo_health"] if self._has_task(tasks, "repo_health") else [],
                    claim=WorkflowClaim(
                        target_type="knowledge",
                        target_id=context.get("repo_id"),
                        allowed_sources=["rag_documents", "approved_memory", "uploaded_knowledge"],
                        reason="历史证据可以避免重复分析和遗漏回归风险。",
                    ),
                )
            )

        # 如果 context 要求做安全审查（requires_safety），必须加一个 Safety Agent 任务，专门判断「写操作风险」
        if context.get("requires_safety"):
            tasks.append(
                WorkflowTask(
                    id="safety_review",
                    agent_name="safety_agent",
                    task_type="safety_review",
                    objective="判断写入或外部发送风险，并生成安全草稿而不是执行副作用。",
                    claim=WorkflowClaim(
                        target_type="external_action",
                        target_id=context.get("repo_id"),
                        allowed_sources=["user_request", "repository_snapshot"],
                        write_intent=True,  # 标记这是一个「写意图」操作（可能改外部系统）
                        reason="写入类操作必须经过人工确认。",
                    ),
                    critical=True,
                )
            )

        # 兜底分支：如果到现在只生成了「repo_health」一个任务（说明用户没明确提到任何专项），
        # 就根据优先级（PR > CI > Issue）再补一个最强的工程信号任务，避免工作流「白跑一场」。
        if len(tasks) == 1 and tasks[0].task_type == "repo_health":
            if context.get("has_pr"):
                tasks.append(self._fallback_pr_task(context))
            elif has_ci_run:
                tasks.append(self._fallback_ci_task(context))
            elif context.get("has_issue"):
                tasks.append(self._fallback_issue_task(context))

        # 把收集到的任务封装成一份完整的工作流规格 WorkflowSpec
        spec = WorkflowSpec(
            goal=message or "运行一次工程协作工作流。",  # 目标：用户原话，没有就给默认值
            trigger_message=message,  # 触发本次工作流的消息
            # 验收标准：白纸黑字写清楚「这份报告必须长什么样、不能做什么」。
            # 为什么：相当于给整条流水线定规矩，防止某个 Agent 越界或偷懒（例如 PR 没解决 P1 就别建议合入）。
            acceptance_criteria=[
                "所有被选中的 Agent 都必须停留在声明的 claim 范围内。",
                "每个专用结果都应包含证据、置信度，或在证据不足时说明明确原因。",
                "Observer 必须在最终综合前标出跨 Agent 冲突。",
                "最终综合必须保留 What、Why、Tradeoff、Open Questions、Next Action。",
                "PR 审查必须按 P1/P2/P3 分级；P1/P2 未解决前不能建议直接合入。",
                "外部写操作必须返回草稿，并要求人工确认。",
            ],
            tasks=tasks,  # 前面组装好的任务清单
            # 是否需要人工确认：只要原本就要求安全审查，就置为 True
            requires_human_confirmation=bool(context.get("requires_safety")),
        )
        # model_dump(mode="json")：把 Pydantic 模型转成普通 Python 字典（且值都是 JSON 友好的类型），方便序列化返回
        return spec.model_dump(mode="json")

    # 重新规划（replan）：当第一轮跑完、Observer 发现问题（缺证据/缺安全检查等）时，在原计划基础上「补任务」。
    # 为什么：与其一次性把计划做死，不如允许「边跑边补」，更接近真实团队协作——先干起来，发现缺啥再补啥。
    async def replan(self, input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        # 把上一轮的 spec（规格）反序列化成 WorkflowSpec 对象
        previous_spec = WorkflowSpec.model_validate(input_data["spec"])
        # 把上一轮各任务的执行结果列表，逐个还原成 WorkflowTaskResult 对象
        results = [WorkflowTaskResult.model_validate(item) for item in input_data.get("task_results") or []]
        # 把 Observer 的观察结论还原成 WorkflowObservation 对象
        observation = WorkflowObservation.model_validate(input_data["observation"])
        # 深拷贝旧任务列表，准备在其基础上增删（不破坏原对象）
        tasks = [task.model_copy(deep=True) for task in previous_spec.tasks]
        # 收集现有任务类型集合，避免重复添加同类任务
        task_types = {task.task_type for task in tasks}
        # 收集「成功」的任务 id 集合，用于后面判断「某依赖是否已完成」
        successful_task_ids = {result.task_id for result in results if result.status == "success"}
        # 收集 Observer 发现的问题类型集合（例如 "task_error"、"missing_safety_review"）
        finding_types = {finding.finding_type for finding in observation.findings}

        # 情况一：Observer 发现「缺少安全审查」，且当前计划里没有 safety_review 任务 → 补一个安全审查任务
        if "missing_safety_review" in finding_types and "safety_review" not in task_types:
            tasks.append(
                WorkflowTask(
                    # 用唯一 id 生成，防止和已有任务 id 撞车
                    id=self._unique_task_id(tasks, "safety_review"),
                    agent_name="safety_agent",
                    task_type="safety_review",
                    objective="Review write or external-send risk before producing a final recommendation.",
                    # 如果 repo_health 成功了，就把它作为依赖；否则不依赖（避免被卡住）
                    dependencies=self._dependency_if_successful(successful_task_ids, "repo_health"),
                    claim=WorkflowClaim(
                        target_type="external_action",
                        target_id=context.get("repo_id"),
                        allowed_sources=["user_request", "repository_snapshot"],
                        write_intent=True,
                        reason="Replan added a missing safety gate after Observer flagged it.",
                    ),
                    critical=True,
                )
            )

        # 情况二：Observer 发现需要「补证据」（错误/跳过/证据缺口/低置信度等），且没有 rag_search 任务 → 补一个历史检索任务
        needs_context_recovery = bool(
            finding_types
            & {  # 集合交集：finding_types 里只要命中下面任一类，就视为需要补证据
                "task_error",
                "task_skipped",
                "evidence_gap",
                "low_confidence",
                "owner_needs_review",
                "team_profile_missing",
            }
        )
        if needs_context_recovery and "rag_search" not in task_types:
            tasks.append(
                WorkflowTask(
                    id=self._unique_task_id(tasks, "rag_search"),
                    agent_name="rag_context_agent",
                    task_type="rag_search",
                    objective="Recover missing evidence by searching historical issues, pull requests, failed CI logs, project documents, and approved knowledge.",
                    dependencies=self._dependency_if_successful(successful_task_ids, "repo_health"),
                    claim=WorkflowClaim(
                        target_type="knowledge",
                        target_id=context.get("repo_id"),
                        allowed_sources=["rag_documents", "approved_memory", "uploaded_knowledge"],
                        reason="Replan added historical context because Observer found weak evidence or confidence.",
                    ),
                )
            )

        # 情况三：有 CI 运行、当前计划没有 ci_debug、且根据内容判断确实需要看 CI → 补 CI 调试任务
        if context.get("has_ci_run", context.get("has_failed_ci")) and "ci_debug" not in task_types and self._replan_needs_ci(previous_spec, results, observation):
            tasks.append(
                WorkflowTask(
                    id=self._unique_task_id(tasks, "ci_debug"),
                    agent_name="ci_debug_agent",
                    task_type="ci_debug",
                    objective="Check the latest failed CI run before final merge or release guidance.",
                    dependencies=self._dependency_if_successful(successful_task_ids, "repo_health"),
                    claim=WorkflowClaim(
                        target_type="workflow_run",
                        target_id=context.get("workflow_run_id"),
                        allowed_sources=["workflow_run", "jobs", "logs", "recent_prs", "code"],
                        reason="Replan added CI debugging because the current decision may depend on failed automation.",
                    ),
                    input_hint={"workflow_run_number": context.get("workflow_run_number")},
                    critical=True,
                )
            )

        # 在旧 spec 基础上生成新的 spec，但保持 workflow_id 等关键字段不变
        replanned = previous_spec.model_copy(deep=True)
        replanned.tasks = tasks
        return replanned.model_dump(mode="json")

    # 兜底：当只有一个 repo_health 任务且仓库有关联 Issue 时，补一个 Issue 分析任务
    def _fallback_issue_task(self, context: dict[str, Any]) -> WorkflowTask:
        label = f"Issue #{context['issue_number']}" if context.get("issue_number") is not None else "最新 Issue"
        return WorkflowTask(
            id="issue_analysis",
            agent_name="issue_analyst_agent",
            task_type="issue_analysis",
            objective=f"分析 {label}，因为它是当前最强的可用工程信号。",
            dependencies=["repo_health"],  # 兜底任务也依赖基线先完成
            claim=WorkflowClaim(target_type="issue", target_id=context.get("issue_id"), allowed_sources=["issue", "team_members"]),
            input_hint={"issue_number": context.get("issue_number")},
        )

    # 兜底：仓库有关联 PR 时，补一个 PR 审查任务
    def _fallback_pr_task(self, context: dict[str, Any]) -> WorkflowTask:
        label = f"PR #{context['pr_number']}" if context.get("pr_number") is not None else "最新 PR"
        return WorkflowTask(
            id="pr_review",
            agent_name="pr_review_agent",
            task_type="pr_review",
            objective=f"审查 {label}，因为它是当前最强的可用工程信号。",
            dependencies=["repo_health"],
            claim=WorkflowClaim(target_type="pull_request", target_id=context.get("pr_id"), allowed_sources=["pull_request", "diff", "ci_summary"]),
            input_hint={"pr_number": context.get("pr_number")},
        )

    # 兜底：有失败 CI 时，补一个 CI 调试任务（标记关键）
    def _fallback_ci_task(self, context: dict[str, Any]) -> WorkflowTask:
        label = (
            f"CI/Workflow Run #{context['workflow_run_number']}"
            if context.get("workflow_run_number") is not None
            else "最新失败的 CI 运行"
        )
        return WorkflowTask(
            id="ci_debug",
            agent_name="ci_debug_agent",
            task_type="ci_debug",
            objective=f"分析 {label}，因为它是当前最强的可用工程信号。",
            dependencies=["repo_health"],
            claim=WorkflowClaim(target_type="workflow_run", target_id=context.get("workflow_run_id"), allowed_sources=["workflow_run", "jobs", "logs"]),
            input_hint={"workflow_run_number": context.get("workflow_run_number")},
            critical=True,
        )

    # 工具函数：判断任务列表里是否已经存在某个 id 的任务
    def _has_task(self, tasks: list[WorkflowTask], task_id: str) -> bool:
        # 只要有一个任务的 id 等于 task_id 就返回 True
        return any(task.id == task_id for task in tasks)

    # 工具函数：生成一个「不重复」的任务 id。如果 base 已被占用，就依次尝试 base_2、base_3……
    def _unique_task_id(self, tasks: list[WorkflowTask], base: str) -> str:
        existing = {task.id for task in tasks}  # 已有 id 集合
        if base not in existing:
            return base  # 没占用就直接用
        index = 2
        while f"{base}_{index}" in existing:  # 一直找到第一个没被占用的后缀
            index += 1
        return f"{base}_{index}"

    # 工具函数：如果依赖任务（task_id）执行成功过，就返回 [task_id] 作为依赖列表，否则返回空列表
    # 为什么：避免因为某个依赖失败而把后续补的任务也卡死（依赖没成功就不强求依赖）
    def _dependency_if_successful(self, successful_task_ids: set[str], task_id: str) -> list[str]:
        return [task_id] if task_id in successful_task_ids else []

    # 判断重新规划时是否真的需要补 CI 任务：把目标、结果摘要、观察发现拼成一段文字，再看是否提到 CI/合并
    def _replan_needs_ci(
        self,
        spec: WorkflowSpec,
        results: list[WorkflowTaskResult],
        observation: WorkflowObservation,
    ) -> bool:
        text = " ".join(
            [
                spec.goal,
                spec.trigger_message,
                *(result.summary for result in results),  # 展开所有结果摘要
                *(finding.message for finding in observation.findings),  # 展开所有发现消息
            ]
        ).lower()
        return self._mentions_ci(text) or self._mentions_merge(text)

    # 关键词命中：问题里是否提到 Issue 相关词
    def _mentions_issue(self, text: str) -> bool:
        return any(token in text for token in ["issue", "bug", "需求", "问题", "负责人", "分配", "优先级"])

    # 关键词命中：是否提到 PR 相关词
    def _mentions_pr(self, text: str) -> bool:
        return any(token in text for token in ["pr", "pull request", "diff", "review", "合并", "代码审查", "风险"])

    # 关键词命中：是否提到 CI 相关词
    def _mentions_ci(self, text: str) -> bool:
        return any(token in text for token in ["ci", "workflow", "action", "build", "test", "失败", "流水线", "构建"])

    # 关键词命中：是否提到「合并 / 发布」相关词
    def _mentions_merge(self, text: str) -> bool:
        return any(token in text for token in ["merge", "合并", "能不能上", "能不能发", "readiness", "release"])

    # 关键词命中：是否提到「历史 / 知识库 / 记忆」相关词
    def _mentions_history(self, text: str) -> bool:
        return any(token in text for token in ["history", "similar", "rag", "knowledge", "历史", "相似", "知识库", "记忆"])

    # 判断用户是不是在问一个「宽泛的工程问题」（多 Agent 协作、工程状态、阻塞、风险等）
    def _is_broad_engineering_question(self, text: str) -> bool:
        return any(
            token in text
            for token in [
                "multi-agent",
                "workflow",
                "协作",
                "工程状态",
                "阻塞",
                "风险",
                "优先处理",
                "下一步",
                "整体",
                "全局",
                "当前仓库",
                "能不能合并",
            ]
        )
