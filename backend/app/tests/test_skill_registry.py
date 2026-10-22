"""DevMind AI module."""

from typing import Any

import pytest
from langchain_core.messages import AIMessage

from app.services.agents.chat_agent import ChatAgent
from app.services.mcp_client import StdioMcpClient
from app.services.skills import SkillRegistry, SkillRegistryError, get_skill_registry


class IssueSkillModel:
    def __init__(self) -> None:
        self.calls = 0
        self.tool_descriptions: list[str] = []

    def bind_tools(self, tools: list[Any]) -> "IssueSkillModel":
        self.tool_descriptions = [str(tool.description) for tool in tools]
        return self

    async def ainvoke(self, messages: list[Any]) -> AIMessage:
        self.calls += 1
        if self.calls == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {"name": "issue_agent__analyze_issue", "args": {"message": "triage the latest issue"}, "id": "call_issue"},
                ],
            )
        return AIMessage(content="Issue triage completed.")


class MemoryPrefetchModel:
    def __init__(self) -> None:
        self.calls = 0

    async def ainvoke(self, messages: list[Any]) -> AIMessage:
        self.calls += 1
        return AIMessage(content="根据 MCP 返回的历史证据，Access Token 的有效期为 15 分钟。")


class SkillRuntimeModel:
    def __init__(self, tool_name: str | None = None) -> None:
        self.calls = 0
        self.tool_name = tool_name
        self.prompts: list[str] = []

    def bind_tools(self, tools: list[Any]) -> "SkillRuntimeModel":
        return self

    async def ainvoke(self, messages: list[Any]) -> AIMessage:
        self.calls += 1
        self.prompts.append("\n".join(str(getattr(message, "content", "")) for message in messages))
        if self.calls == 1 and self.tool_name:
            return AIMessage(content="", tool_calls=[{"name": self.tool_name, "args": {}, "id": "skill_call"}])
        return AIMessage(content="Skill runtime completed.")


def test_skill_registry_loads_builtin_skills() -> None:
    registry = get_skill_registry()

    names = {skill.manifest.name for skill in registry.list_skills()}
    assert {
        "issue-triage",
        "pr-review",
        "ci-debug",
        "weekly-report",
        "safety-draft",
        "project-investigation",
    } <= names
    assert registry.skill_for_entrypoint("pr_review_agent.analyze_pr").manifest.name == "pr-review"
    assert registry.skill_for_entrypoint("devflow_search_evidence").manifest.name == "project-investigation"
    assert registry.get_skill("ci-debug").manifest.workflow_steps[0] == "collect_failed_jobs_and_logs"
    assert "ci-debug" in registry.prompt_context()


def test_skill_registry_selects_and_loads_full_instructions() -> None:
    registry = get_skill_registry()

    activations = registry.activate("请审查 PR #86 有没有 P1/P2 风险")

    assert [item.skill_name for item in activations] == ["pr-review"]
    assert activations[0].activation_mode == "automatic"
    assert "不能用「looks good」" in activations[0].instructions
    assert activations[0].instruction_digest
    assert activations[0].resources[0]["path"] == "references/risk-checklist.md"
    assert "接口与数据" in activations[0].resources[0]["content"]


def test_skill_registry_explicit_selection_and_unknown_skill() -> None:
    registry = get_skill_registry()

    activation = registry.activate("请处理这个任务", requested_name="CI 排障")
    assert [item.skill_name for item in activation] == ["ci-debug"]
    assert activation[0].activation_mode == "explicit"

    with pytest.raises(SkillRegistryError, match="not-installed"):
        registry.activate("请处理这个任务", requested_name="not-installed")


def test_skill_registry_loads_only_referenced_safe_resources(tmp_path) -> None:
    skill_dir = tmp_path / "resource-skill"
    references = skill_dir / "references"
    references.mkdir(parents=True)
    (references / "checklist.md").write_text("# Runtime checklist\nVerify evidence.", encoding="utf-8")
    (skill_dir / "SKILL.md").write_text(
        """---
name: resource-skill
title: 资源技能
version: 0.1.0
description: 测试按需资源。
entrypoint: resource.tool
tools:
  - resource.tool
triggers:
  - 资源测试
workflow_steps:
  - verify
---

# 资源技能

需要时读取 `references/checklist.md`。.""",
        encoding="utf-8",
    )
    registry = SkillRegistry(tmp_path)

    activation = registry.activate("资源测试")[0]

    assert activation.resources[0]["path"] == "references/checklist.md"
    assert "Verify evidence" in activation.resources[0]["content"]


def test_skill_registry_rejects_resource_outside_skill_directory(tmp_path) -> None:
    skill_dir = tmp_path / "unsafe-skill"
    skill_dir.mkdir()
    (tmp_path / "secret.md").write_text("secret", encoding="utf-8")
    (skill_dir / "SKILL.md").write_text(
        """---
name: unsafe-skill
title: 越界资源技能
version: 0.1.0
description: 测试目录约束。
entrypoint: unsafe.tool
tools:
  - unsafe.tool
triggers:
  - 越界测试
---

读取 `references/../../secret.md`。.""",
        encoding="utf-8",
    )
    registry = SkillRegistry(tmp_path)

    with pytest.raises(SkillRegistryError, match="目录外资源"):
        registry.activate("越界测试")


def test_skill_registry_annotates_entrypoint_tool_calls() -> None:
    registry = get_skill_registry()

    annotated = registry.annotate_tool_call(
        {"tool_name": "safety_agent.classify_action_risk", "arguments": {"action": "comment"}, "status": "success"}
    )

    assert annotated["skill_name"] == "safety-draft"
    assert annotated["skill_version"] == "0.1.0"
    assert "require_confirmation" in annotated["skill_steps"]


@pytest.mark.asyncio
async def test_stdio_mcp_client_inspects_real_server_and_tools() -> None:
    inspection = await StdioMcpClient().inspect_server()

    assert inspection["initialize"]["serverInfo"] == {"name": "devflow-memory-mcp", "version": "0.1.0"}
    assert inspection["initialize"]["protocolVersion"] == "2024-11-05"
    assert {tool["name"] for tool in inspection["tools"]} == {
        "devflow_search_evidence",
        "devflow_get_thread_context",
        "devflow_read_session_events",
    }


@pytest.mark.asyncio
async def test_chat_agent_executes_explicit_memory_tool_before_model_answer() -> None:
    registry = get_skill_registry()
    model = MemoryPrefetchModel()

    async def evidence_tool(input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        return {
            "route": "memory_search",
            "tool_calls": [],
            "answer": "历史决策：Access Token 固定为 15 分钟，减少泄露后的暴露窗口。",
            "citations": [{"type": "memory", "id": "AUTH-ADR-007", "title": "令牌生命周期评审"}],
        }

    events = [
        event
        async for event in ChatAgent(model=model).run_stream(  # type: ignore[arg-type]
            {"message": "请先调用 devflow_search_evidence 搜索历史证据"},
            {
                "tools": {"devflow_search_evidence": evidence_tool},
                "skill_trace_by_tool": registry.trace_metadata_by_tool(),
                "skill_description_by_tool": registry.tool_description_suffixes(),
            },
        )
    ]

    tool_use = next(event for event in events if event["event"] == "tool_use")
    tool_result = next(event for event in events if event["event"] == "tool_result")
    final = events[-1]["data"]
    assert model.calls == 1
    assert tool_use["data"]["tool_name"] == "devflow_search_evidence"
    assert tool_result["data"]["skill_name"] == "project-investigation"
    assert final["tool_calls"][0]["skill_version"] == "0.1.0"


@pytest.mark.asyncio
async def test_chat_agent_adds_skill_trace_to_tool_events_and_calls() -> None:
    registry = get_skill_registry()
    model = IssueSkillModel()

    async def issue_tool(input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        return {
            "route": "issue_analysis",
            "tool_calls": [{"tool_name": "issue_agent.analyze_issue", "arguments": input_data, "status": "success"}],
            "answer": "Issue category: Bug",
            "citations": [],
        }

    agent = ChatAgent(model=model)  # type: ignore[arg-type]
    events = [
        event
        async for event in agent.run_stream(
            {"message": "triage the latest issue"},
            {
                "tools": {"issue_agent.analyze_issue": issue_tool},
                "registered_skills": registry.prompt_context(),
                "skill_trace_by_tool": registry.trace_metadata_by_tool(),
                "skill_description_by_tool": registry.tool_description_suffixes(),
            },
        )
    ]

    tool_use = next(event for event in events if event["event"] == "tool_use")
    tool_result = next(event for event in events if event["event"] == "tool_result")
    final = [event for event in events if event["event"] == "final"][-1]["data"]

    assert "注册技能：Issue 分诊" in model.tool_descriptions[0]
    assert tool_use["data"]["skill_name"] == "issue-triage"
    assert tool_result["data"]["skill_name"] == "issue-triage"
    assert final["tool_calls"][0]["skill_name"] == "issue-triage"
    assert "produce_action_items" in final["tool_calls"][0]["skill_steps"]


@pytest.mark.asyncio
async def test_chat_agent_auto_loads_full_skill_and_records_validation() -> None:
    registry = get_skill_registry()
    model = SkillRuntimeModel(tool_name="pr_review_agent__analyze_pr")

    async def pr_tool(input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        active = context.get("active_skills") or []
        assert active[0]["skill_name"] == "pr-review"
        assert "不能用「looks good」" in active[0]["instructions"]
        return {
            "route": "pr_review",
            "tool_calls": [{"tool_name": "pr_review_agent.analyze_pr", "arguments": {}, "status": "success"}],
            "answer": "PR reviewed.",
            "citations": [],
        }

    final = await ChatAgent(model=model).run(
        {"message": "请审查 PR #86 有没有风险"},
        {
            "tools": {"pr_review_agent.analyze_pr": pr_tool},
            "_skill_registry": registry,
            "registered_skills": registry.prompt_context(),
            "skill_trace_by_tool": registry.trace_metadata_by_tool(),
            "skill_description_by_tool": registry.tool_description_suffixes(),
        },
    )

    assert any("不能用「looks good」" in prompt for prompt in model.prompts)
    assert any("PR 风险检查清单" in prompt for prompt in model.prompts)
    assert all("优先提取首个关键错误块" not in prompt for prompt in model.prompts)
    activation = next(event for event in final["agent_events"] if event["type"] == "skill_activated")
    validation = next(event for event in final["agent_events"] if event["type"] == "skill_validation")
    assert activation["skill_name"] == "pr-review"
    assert activation["activation_mode"] == "automatic"
    assert validation["status"] == "completed"
    assert validation["entrypoint_called"] is True
    assert final["tool_calls"][0]["skill_activation_mode"] == "automatic"


@pytest.mark.asyncio
async def test_chat_agent_does_not_load_unmatched_skill_body() -> None:
    registry = get_skill_registry()
    model = SkillRuntimeModel()

    final = await ChatAgent(model=model).run(
        {"message": "你好"},
        {
            "tools": {},
            "_skill_registry": registry,
            "registered_skills": registry.prompt_context(),
            "skill_trace_by_tool": registry.trace_metadata_by_tool(),
            "skill_description_by_tool": registry.tool_description_suffixes(),
        },
    )

    assert final["route"] == "direct_answer"
    assert any("本轮没有匹配到需要加载完整正文的 Skill" in prompt for prompt in model.prompts)
    assert all("不能用「looks good」" not in prompt for prompt in model.prompts)
    assert not any(event["type"] == "skill_activated" for event in final["agent_events"])


@pytest.mark.asyncio
async def test_explicit_skill_rejects_tools_outside_its_contract() -> None:
    registry = get_skill_registry()
    model = SkillRuntimeModel(tool_name="issue_agent__analyze_issue")
    issue_called = False

    async def issue_tool(input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        nonlocal issue_called
        issue_called = True
        return {"route": "issue_analysis", "tool_calls": [], "answer": "unexpected", "citations": []}

    async def pr_tool(input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        return {"route": "pr_review", "tool_calls": [], "answer": "unused", "citations": []}

    final = await ChatAgent(model=model).run(
        {"message": "使用 pr-review Skill 审查这个变更"},
        {
            "tools": {
                "issue_agent.analyze_issue": issue_tool,
                "pr_review_agent.analyze_pr": pr_tool,
            },
            "_skill_registry": registry,
            "registered_skills": registry.prompt_context(),
            "skill_trace_by_tool": registry.trace_metadata_by_tool(),
            "skill_description_by_tool": registry.tool_description_suffixes(),
        },
    )

    assert issue_called is False
    assert final["tool_calls"][0]["status"] == "error"
    assert final["tool_calls"][0]["error_kind"] == "permission_denied"
    validation = next(event for event in final["agent_events"] if event["type"] == "skill_validation")
    assert validation["status"] == "violated"
    assert validation["tool_contract_violations"] == ["issue_agent.analyze_issue"]


@pytest.mark.asyncio
async def test_chat_agent_reports_unknown_explicit_skill_without_calling_model() -> None:
    registry = get_skill_registry()
    model = SkillRuntimeModel()

    final = await ChatAgent(model=model).run(
        {"message": "使用 not-installed Skill 处理这个任务"},
        {"tools": {}, "_skill_registry": registry},
    )

    assert final["route"] == "skill_error"
    assert "not-installed" in final["answer"]
    assert model.calls == 0
