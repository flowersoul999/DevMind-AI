"""
tests/test_workflow_orchestrator.py —— 测试「多 Agent 工作流编排」（Planner / Orchestrator / Observer）
============================================================================

这是项目最复杂的链路之一：用户一句话 → Planner 拆解成多个跨域任务（issue/pr/ci/rag）→
Orchestrator 并发执行、处理依赖/超时/失败跳过 → Observer 汇总成结论。本测试覆盖：

  - Planner 能为「能否合并」生成跨域任务图（issue_analysis / pr_review / ci_debug / repo_health）；
  - 明确指定了 #142/#86/#9001 时，即使后来有更新的记录，也锁定原目标（不偷偷换成最新）；
  - 缺目标 / 目标属于别的仓库 → 明确报错，绝不「回退用最新」；
  - Orchestrator 并发执行就绪任务（总耗时远小于各任务之和）；
  - 证据不足时自动 replan 加一轮 RAG；确认过的 spec 不再 replan；
  - 卡死任务超时、依赖失败则下游 skipped；
  - Observer 在需要时标记「缺少安全审查」；
  - 端到端：聊天工具跑完整工作流并落库（AgentWorkflowRun / AgentTaskRun）。

全程内存 SQLite + monkeypatch 假 Agent，不依赖真实 GitHub/LLM。."""

import asyncio  # 异步并发
import time  # 计时（验证并行确实省时）
from datetime import datetime, timedelta, timezone  # 时间

import pytest  # 测试框架
from sqlalchemy import create_engine  # 建引擎
from sqlalchemy.orm import Session  # 会话

# 被测的聊天路由函数/异常
from app.api.routes.chat import WorkflowTargetResolutionError, _build_conversation_tools, _build_engineering_workflow_runtime
from app.core.config import settings  # 配置（mock 掉密钥）
from app.db.models import AgentTaskRun, AgentWorkflowRun, Conversation, Issue, PRFile, PullRequest, Repository, WorkflowRun  # 表
from app.db.session import Base  # 建表元数据
from app.schemas.workflow import WorkflowClaim, WorkflowSpec, WorkflowTask, WorkflowTaskResult  # 工作流数据结构
from app.services.agents.observer_agent import ObserverAgent  # 观察者 Agent
from app.services.agents.planner_agent import PlannerAgent  # 规划者 Agent
from app.services.agents.pr_review_agent import PRReviewAgent  # PR 审查 Agent
from app.services.agents.workflow_orchestrator import WorkflowOrchestrator  # 编排器
from app.services.skills import get_skill_registry  # 技能注册表


@pytest.mark.asyncio
async def test_planner_builds_cross_domain_workflow_for_merge_readiness() -> None:
    # 测试：Planner 对「能不能合并」生成跨 Issue/PR/CI 的任务图
    result = await PlannerAgent().run(
        {"message": "这个 PR 能不能合并？请做多 agent 协作 review，检查 Issue、PR 和 CI 风险。"},
        {
            "repo_id": "repo-1",
            "has_issue": True, "issue_id": "issue-1",
            "has_pr": True, "pr_id": "pr-1",
            "has_failed_ci": True, "workflow_run_id": "run-1",
        },
    )

    task_types = [task["task_type"] for task in result["tasks"]]
    assert "repo_health" in task_types  # 有仓库健康度任务
    assert "issue_analysis" in task_types  # 有 issue 分析
    assert "pr_review" in task_types  # 有 PR 审查
    assert "ci_debug" in task_types  # 有 CI 诊断
    assert result["acceptance_criteria"]  # 验收标准非空


@pytest.mark.asyncio
async def test_confirmed_workflow_keeps_explicit_issue_pr_and_ci_claims_after_newer_records_arrive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 测试：用户明确点名 #142/#86/#9001 后，即使数据库里又来了更新的记录，也锁定原目标
    monkeypatch.setattr(settings, "llm_api_key", None)  # 无密钥，走规则兜底
    captured_pr_input: dict[str, object] = {}  # 用来捕获 PR 审查 Agent 收到的输入

    async def capture_pr_run(self, input_data, context):
        """capture_pr_run."""
        # 假 PR 审查：把收到的关键输入记下来，返回假结果
        captured_pr_input.update(
            {
                "number": input_data["number"],
                "ci_summary": context["ci_summary"],
                "skill_name": context["skill_runtime"]["skill_name"],
                "skill_instructions": context["skill_instructions"],
            }
        )
        return {"merge_recommendation": "暂缓合并", "risk_points": [], "blocking_issues": [], "test_suggestions": [], "confidence": 0.8}

    monkeypatch.setattr(PRReviewAgent, "run", capture_pr_run)  # 替换 PR 审查 Agent 的实现
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        repo = Repository(owner="acme", name="targets", full_name="acme/targets")
        db.add(repo)
        db.flush()
        # 用户请求的 issue/pr/run
        requested_issue = Issue(repo_id=repo.id, number=142, title="Requested issue", body="Repro steps and user impact are documented.", labels=["bug"], state="open", created_at=now)
        requested_pr = PullRequest(repo_id=repo.id, number=86, title="Requested PR", body="Fixes the requested issue.", state="open", author="alice", created_at=now)
        requested_run = WorkflowRun(repo_id=repo.id, github_run_id=9001, name="requested-ci", status="completed", conclusion="failure", jobs=[{"name": "pytest", "conclusion": "failure"}], logs_text="requested test failed", created_at=now)
        db.add_all([requested_issue, requested_pr, requested_run])
        db.flush()
        db.add(PRFile(pr_id=requested_pr.id, filename="backend/app/target.py", status="modified", additions=3, deletions=1, patch="target fix"))
        db.commit()

        message = "请用多 Agent 检查 Issue #142、PR #86 和 CI #9001，判断能不能合并。"
        skill_context = {"_skill_registry": get_skill_registry()}
        # 第一步：规划
        planning_context, _ = _build_engineering_workflow_runtime(repo, db, message, skill_context)
        spec = WorkflowSpec.model_validate(await PlannerAgent().run({"message": message}, planning_context))
        claims = {task.task_type: task.claim.target_id for task in spec.tasks}  # 各任务锁定的目标 id
        assert claims["issue_analysis"] == str(requested_issue.id)
        assert claims["pr_review"] == str(requested_pr.id)
        assert claims["ci_debug"] == str(requested_run.id)
        assert "Issue #142" in next(task.objective for task in spec.tasks if task.task_type == "issue_analysis")
        assert "PR #86" in next(task.objective for task in spec.tasks if task.task_type == "pr_review")

        # 模拟「之后来了更新的记录」
        newer_at = now + timedelta(hours=1)
        newer_issue = Issue(repo_id=repo.id, number=999, title="Newer issue", state="open", created_at=newer_at)
        newer_pr = PullRequest(repo_id=repo.id, number=999, title="Newer PR", state="open", created_at=newer_at)
        newer_run = WorkflowRun(repo_id=repo.id, github_run_id=9999, name="newer-ci", status="completed", conclusion="failure", jobs=[], created_at=newer_at)
        db.add_all([newer_issue, newer_pr, newer_run])
        db.commit()

        # 确认执行计划时，执行上下文会取到「最新」记录——但 spec 里锁定的 claim 仍是原目标
        execution_context, runners = _build_engineering_workflow_runtime(repo, db, "确认执行这个计划", skill_context)
        assert execution_context["issue_id"] == str(newer_issue.id)  # 执行上下文用最新 issue（用于补全信息）
        assert execution_context["pr_id"] == str(newer_pr.id)
        assert execution_context["workflow_run_id"] == str(newer_run.id)

        # 实际跑 spec：任务仍应作用于 spec 锁定的原 claim（#142/#86/#9001）
        report = await WorkflowOrchestrator(runners).run_spec(spec, execution_context)
        summaries = {result["task_type"]: result["summary"] for result in report["task_results"]}
        assert summaries["issue_analysis"].startswith("Issue #142:")  # 用的还是 #142
        assert summaries["pr_review"].startswith("PR #86:")
        assert summaries["ci_debug"].startswith("CI requested-ci:")
        assert captured_pr_input["number"] == 86  # PR 审查作用于 #86
        assert captured_pr_input["ci_summary"] == {"failed_runs": 2, "latest_failed_run": "requested-ci"}
        assert captured_pr_input["skill_name"] == "pr-review"  # 用的是 pr-review 技能
        assert "P1/P2" in str(captured_pr_input["skill_instructions"])  # 技能指令含 P1/P2 分级
        pr_output = next(result["output"] for result in report["task_results"] if result["task_type"] == "pr_review")
        assert pr_output["_skill_runtime"]["validation"] == "entrypoint_completed"  # 入口校验通过


def test_workflow_rejects_missing_explicit_target_instead_of_using_latest() -> None:
    # 测试：用户点名了不存在的 PR #404 → 报错，绝不「回退用最新」
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        repo = Repository(owner="acme", name="missing", full_name="acme/missing")
        db.add(repo)
        db.flush()
        db.add(PullRequest(repo_id=repo.id, number=1, title="Only PR", state="open"))
        db.commit()

        # 期望抛出 WorkflowTargetResolutionError，且提示「不会回退」
        with pytest.raises(WorkflowTargetResolutionError, match=r"PR #404.*不会回退"):
            _build_engineering_workflow_runtime(repo, db, "请审查 PR #404", {})


@pytest.mark.asyncio
async def test_workflow_rejects_claim_target_from_another_repository() -> None:
    # 测试：任务 claim 指向「别的仓库」的 issue → 报错（防越权/错乱）
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        repo = Repository(owner="acme", name="one", full_name="acme/one")
        other_repo = Repository(owner="acme", name="two", full_name="acme/two")
        db.add_all([repo, other_repo])
        db.flush()
        local_issue = Issue(repo_id=repo.id, number=1, title="Local issue", state="open")
        foreign_issue = Issue(repo_id=other_repo.id, number=142, title="Foreign issue", state="open")  # 属于 another repo
        db.add_all([local_issue, foreign_issue])
        db.commit()

        workflow_context, runners = _build_engineering_workflow_runtime(repo, db, "检查 Issue", {})
        task = WorkflowTask(
            id="issue_analysis", agent_name="issue_analyst_agent", task_type="issue_analysis",
            objective="Analyze a tampered claim",
            claim=WorkflowClaim(target_type="issue", target_id=str(foreign_issue.id)),  # 指向别的仓库
        )
        spec = WorkflowSpec(goal="tampered", trigger_message="tampered", tasks=[task])

        # 期望报「不属于仓库 acme/one」
        with pytest.raises(WorkflowTargetResolutionError, match="不属于仓库 acme/one"):
            await runners["issue_analysis"](task, spec, workflow_context)


@pytest.mark.asyncio
async def test_orchestrator_runs_ready_tasks_in_parallel_and_observer_blocks_merge() -> None:
    # 测试：Orchestrator 并发跑就绪任务，且 Observer 判定「CI 失败 → 暂缓合并」
    class StaticPlanner:
        # 假的 Planner：固定返回 pr_review + ci_debug 两个任务
        async def run(self, input_data, context):
            """run."""
            """run."""
            """run."""
            """run."""
            """run."""
            return WorkflowSpec(
                goal="Check merge readiness", trigger_message="merge?",
                tasks=[
                    WorkflowTask(id="pr_review", agent_name="pr_review_agent", task_type="pr_review", objective="Review PR", claim=WorkflowClaim(target_type="pull_request", target_id="pr-1")),
                    WorkflowTask(id="ci_debug", agent_name="ci_debug_agent", task_type="ci_debug", objective="Review CI", claim=WorkflowClaim(target_type="workflow_run", target_id="run-1")),
                ],
            ).model_dump(mode="json")

    async def run_pr(task, spec, context):
        """run_pr."""
        """run_pr."""
        await asyncio.sleep(0.12)  # 模拟耗时 0.12s
        return WorkflowTaskResult(task_id=task.id, agent_name=task.agent_name, task_type=task.task_type, status="success", summary="PR looks mergeable.", output={"merge_recommendation": "建议合并", "blocking_issues": []}, evidence_count=1, confidence=0.8)

    async def run_ci(task, spec, context):
        """run_ci."""
        await asyncio.sleep(0.12)  # 也耗 0.12s
        return WorkflowTaskResult(task_id=task.id, agent_name=task.agent_name, task_type=task.task_type, status="success", summary="CI is failing.", output={"is_merge_blocking": True, "failure_type": "test", "debug_steps": ["Fix failing tests"]}, evidence_count=1, confidence=0.8)

    started = time.perf_counter()
    report = await WorkflowOrchestrator(
        {"pr_review": run_pr, "ci_debug": run_ci},
        planner=StaticPlanner(),  # type: ignore[arg-type]
    ).run({"message": "merge?"}, {})
    duration = time.perf_counter() - started

    # 两个任务各 0.12s，若串行应 >0.24s；并行应 <0.22s，证明确实并发
    assert duration < 0.22
    assert report["metrics"]["success_count"] == 2
    assert report["metrics"]["replan_count"] == 0
    finding_types = [item["finding_type"] for item in report["observation"]["findings"]]
    assert "ci_merge_gate" in finding_types  # Observer 给出「CI 合并门」发现
    assert "cross_agent_conflict" in finding_types  # 跨 Agent 冲突发现
    assert "暂缓合并" in report["final_answer"]  # 最终结论因 CI 失败而暂缓


@pytest.mark.asyncio
async def test_orchestrator_replans_with_rag_when_evidence_is_missing() -> None:
    # 测试：PR 审查「没有证据」时，Orchestrator 自动加一轮 RAG 检索补证据（replan 一次）
    class StaticPlanner(PlannerAgent):
        """StaticPlanner."""
        async def run(self, input_data, context):
            return WorkflowSpec(
                goal="Check merge readiness", trigger_message="merge?",
                tasks=[WorkflowTask(id="pr_review", agent_name="pr_review_agent", task_type="pr_review", objective="Review PR", claim=WorkflowClaim(target_type="pull_request", target_id="pr-1"))],
            ).model_dump(mode="json")

    async def run_pr(task, spec, context):
        return WorkflowTaskResult(task_id=task.id, agent_name=task.agent_name, task_type=task.task_type, status="success", summary="PR looks mergeable, but no concrete evidence was attached.", output={"merge_recommendation": "approve"}, evidence_count=0, confidence=0.72)  # 证据数 0

    async def run_rag(task, spec, context):
        """run_rag."""
        return WorkflowTaskResult(task_id=task.id, agent_name=task.agent_name, task_type=task.task_type, status="success", summary="Recovered related historical context.", output={"answer": "Found similar PR history."}, evidence_count=1, confidence=0.68, citations=[{"type": "pull_request", "id": "pr-0", "title": "Similar PR"}])

    report = await WorkflowOrchestrator(
        {"pr_review": run_pr, "rag_search": run_rag},
        planner=StaticPlanner(),
    ).run({"message": "merge?"}, {"repo_id": "repo-1"})

    assert report["metrics"]["replan_count"] == 1  # 重规划了 1 次
    assert report["metrics"]["execution_rounds"] == 2  # 执行了 2 轮
    assert [item["task_id"] for item in report["task_results"]] == ["pr_review", "rag_search"]  # 第二轮加了 rag_search
    assert "rag_search" in [task["task_type"] for task in report["spec"]["tasks"]]  # spec 里也多了 rag_search
    assert any(event["type"] == "workflow_replan" for event in report["agent_events"])  # 记录了 replan 事件


@pytest.mark.asyncio
async def test_orchestrator_runs_confirmed_spec_without_replanning() -> None:
    # 测试：已经「确认过」的 spec 直接执行，绝不再调 Planner（用一个会炸的 Planner 验证）
    class FailingPlanner:
        async def run(self, input_data, context):
            raise AssertionError("confirmed specs must not be replanned")  # 若被调用就报错

    async def run_repo_health(task, spec, context):
        """run_repo_health."""
        return WorkflowTaskResult(task_id=task.id, agent_name=task.agent_name, task_type=task.task_type, status="success", summary="Repo baseline collected.", evidence_count=1, confidence=0.8)

    spec = WorkflowSpec(
        goal="Execute a confirmed plan", trigger_message="plan",
        tasks=[WorkflowTask(id="repo_health", agent_name="repo_health_agent", task_type="repo_health", objective="Collect repo baseline", claim=WorkflowClaim(target_type="repository", target_id="repo-1"))],
    )

    report = await WorkflowOrchestrator(
        {"repo_health": run_repo_health},
        planner=FailingPlanner(),  # type: ignore[arg-type]
    ).run_spec(spec.model_dump(mode="json"), {})  # 用 run_spec（确认执行路径），不会触发 Planner

    assert report["spec"]["workflow_id"] == spec.workflow_id
    assert report["metrics"]["task_count"] == 1
    assert report["task_results"][0]["task_id"] == "repo_health"


@pytest.mark.asyncio
async def test_orchestrator_times_out_stuck_task() -> None:
    # 测试：任务卡死（超过超时阈值）被标记为 error，而不是无限等
    class StaticPlanner:
        async def run(self, input_data, context):
            return WorkflowSpec(goal="Check stuck worker", trigger_message="run", tasks=[WorkflowTask(id="slow", agent_name="slow_agent", task_type="slow_task", objective="Never finish quickly", claim=WorkflowClaim(target_type="repository", target_id="repo-1"))]).model_dump(mode="json")

    async def run_slow(task, spec, context):
        """run_slow."""
        await asyncio.sleep(1)  # 故意睡 1 秒
        return WorkflowTaskResult(task_id=task.id, agent_name=task.agent_name, task_type=task.task_type, status="success", summary="too late")

    # 超时设成 0.01 秒，且禁止 replan（避免超时触发重规划掩盖测试意图）
    report = await WorkflowOrchestrator(
        {"slow_task": run_slow},
        planner=StaticPlanner(),  # type: ignore[arg-type]
    ).run({"message": "run"}, {"workflow_task_timeout_seconds": 0.01, "disable_replan": True})

    result = report["task_results"][0]
    assert result["status"] == "error"  # 状态是错误
    assert result["output"]["error_kind"] == "timeout"  # 错误类型是超时


@pytest.mark.asyncio
async def test_orchestrator_marks_dependents_skipped_when_dependency_fails() -> None:
    # 测试：依赖的任务失败，下游依赖它的任务被标记为 skipped（不执行）
    class StaticPlanner:
        async def run(self, input_data, context):
            return WorkflowSpec(goal="Check dependency propagation", trigger_message="run", tasks=[
                WorkflowTask(id="root", agent_name="root_agent", task_type="root_task", objective="Fail first", claim=WorkflowClaim(target_type="repository", target_id="repo-1")),
                WorkflowTask(id="child", agent_name="child_agent", task_type="child_task", objective="Requires root", dependencies=["root"], claim=WorkflowClaim(target_type="repository", target_id="repo-1")),  # 依赖 root
            ]).model_dump(mode="json")

    async def run_root(task, spec, context):
        """run_root."""
        return WorkflowTaskResult(task_id=task.id, agent_name=task.agent_name, task_type=task.task_type, status="error", summary="root failed", error="boom")  # 根任务失败

    async def run_child(task, spec, context):  # pragma: no cover - should not execute
        """run_child."""
        raise AssertionError("dependent task should be skipped")  # 若被执行就报错（证明不该执行）

    report = await WorkflowOrchestrator(
        {"root_task": run_root, "child_task": run_child},
        planner=StaticPlanner(),  # type: ignore[arg-type]
    ).run({"message": "run"}, {"disable_replan": True})

    statuses = {result["task_id"]: result for result in report["task_results"]}
    assert statuses["root"]["status"] == "error"  # 根任务失败
    assert statuses["child"]["status"] == "skipped"  # 子任务被跳过
    assert statuses["child"]["output"]["error_kind"] == "dependency_failed"  # 跳过原因：依赖失败


@pytest.mark.asyncio
async def test_observer_flags_missing_safety_review_when_required() -> None:
    # 测试：当 spec 要求人工确认时，Observer 标记「缺少安全审查」
    spec = WorkflowSpec(goal="Draft a GitHub comment", trigger_message="comment", requires_human_confirmation=True, tasks=[])

    observation = await ObserverAgent().run({"spec": spec.model_dump(mode="json"), "task_results": []}, {})

    assert observation["human_review_required"] is True  # 需要人工确认
    assert observation["findings"][0]["finding_type"] == "missing_safety_review"  # 发现类型正确


@pytest.mark.asyncio
async def test_chat_workflow_tool_persists_workflow_and_task_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    # 测试：端到端——聊天工具跑完整工作流，并把运行记录落库（AgentWorkflowRun / AgentTaskRun）
    monkeypatch.setattr(settings, "llm_api_key", None)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        repo = Repository(owner="acme", name="demo", full_name="acme/demo")
        db.add(repo)
        db.flush()
        conversation = Conversation(repo_id=repo.id, title="Workflow")
        db.add(conversation)
        # 准备 issue / pr / 失败 CI
        issue = Issue(repo_id=repo.id, number=7, title="Login API returns 500", body="Steps: call login. Expected 401, actual 500. Impact: users cannot login.", labels=["bug"], state="open", created_at=now)
        pr = PullRequest(repo_id=repo.id, number=8, title="Fix login error handling", body="Fixes login status handling.", state="open", author="alice", created_at=now)
        db.add_all([issue, pr])
        db.flush()
        db.add(PRFile(pr_id=pr.id, filename="backend/app/auth.py", status="modified", additions=40, deletions=8, patch="handle login"))
        db.add(WorkflowRun(repo_id=repo.id, name="backend-tests", status="completed", conclusion="failure", jobs=[{"name": "pytest", "conclusion": "failure", "steps": [{"name": "Run tests", "conclusion": "failure"}]}], logs_text="pytest tests/test_auth.py FAILED AssertionError", created_at=now))
        db.commit()

        tools = _build_conversation_tools(repo, conversation, db)  # 构建会话可用的工具
        result = await tools["workflow.run_engineering_review"](
            {"message": "这个 PR 能不能合并？请用多 agent 协作检查 Issue、PR 和 CI。"},
            {"team_members": [{"name": "Alice", "role": "Backend", "strengths": "FastAPI auth", "techStack": "Python"}]},
        )

        assert result["route"] == "engineering_workflow"
        assert "多 Agent 协作结论" in result["answer"]  # 答案是协作结论
        tool_names = [item["tool_name"] for item in result["tool_calls"]]
        assert "workflow.pr_review" in tool_names  # 调用了 PR 审查工具
        assert "workflow.ci_debug" in tool_names  # 调用了 CI 诊断工具
        assert db.query(AgentWorkflowRun).count() == 1  # 落库了 1 条工作流运行
        assert db.query(AgentTaskRun).count() >= 3  # 至少 3 条任务运行（issue/pr/ci）
