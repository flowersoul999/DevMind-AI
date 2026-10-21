"""DevMind AI module."""

# pytest 测试框架。
import pytest

# 导入真实的 Issue 智能体：它负责给一个 GitHub Issue 分类、定优先级、找重复、指派负责人。
from app.services.agents.issue_agent import IssueAgent


# 假 LLM（Noop）：模型啥也不返回。用来验证 Agent 不依赖大模型时的"规则兜底"是否可靠。
class NoopLLM:
    async def chat_json(self, system_prompt, user_payload):
        """chat_json."""
        """chat_json."""
        return {}


# 假 LLM（InventedOwnerLLM）：模型"一本正经地胡说"——它编了一个根本不在团队里的 owner"布偶猫"。
# 为什么：专门用来测试 Agent 的"防编造"能力，模型推荐的人必须真在团队列表里，否则要被否决。
class InventedOwnerLLM:
    async def chat_json(self, system_prompt, user_payload):
        return {"suggested_owner": "布偶猫", "owner_reason": "仓库文档里提到了布偶猫。"}


@pytest.mark.asyncio
async def test_issue_agent_fallback_classifies_bug() -> None:
    # 模型不说话时，Agent 靠标题/正文/标签自己分类。
    result = await IssueAgent(llm=NoopLLM()).run({"title": "API returns 500", "body": "Login fails", "labels": ["bug"]}, {})
    # 断言：标签是 bug，分类应为 "Bug"。
    assert result["category"] == "Bug"
    # 断言：信息不足无法闭环，结论是"等待澄清"。
    assert result["conclusion"] == "等待澄清"
    # 断言：API 500 属于线上故障，优先级 P1（最高档）。
    assert result["priority"] == "P1"
    # 断言：给出了 checklist（处理清单），不是空。
    assert result["checklist"]


@pytest.mark.asyncio
async def test_issue_agent_recommends_merge_for_strong_duplicate() -> None:
    # 给一个 issue，并提供一条"疑似重复"的候选（相似度 0.91，很高）。
    result = await IssueAgent(llm=NoopLLM()).run(
        {
            "title": "Login API returns 500 when token expires",
            "body": "Steps: expire JWT, call /login, expected 401 but actual 500. Impact: users cannot renew sessions.",
            "labels": ["bug"],
        },
        {"duplicate_candidates": [{"issue_id": "issue-1", "title": "JWT expiry returns 500", "score": 0.91}]},
    )

    # 断言：相似度足够高，结论建议"合并到已有 Issue"（去重）。
    assert result["conclusion"] == "合并到已有 Issue"
    # 断言：重复候选的相似度分数被正确保留（0.91）。
    assert result["duplicate_candidates"][0]["score"] == 0.91
    # 断言：证据里有一条来源类型是 similar_issue（相似 issue），说明推理有据可查。
    assert any(item["source_type"] == "similar_issue" for item in result["evidence"])


@pytest.mark.asyncio
async def test_issue_agent_dedupes_repeated_evidence() -> None:
    # 造一份证据（来自工作区代码文件）。
    evidence = {
        "source_type": "workspace_file",
        "title": "backend/app/auth/middleware.py#refresh",
        "snippet": "Refresh expired tokens before validating the session.",
        "source_id": "code-1",
        "score": 0.8,
    }
    # 故意把同一份证据同时放进 code_references 和 conversation_evidence（模拟两个渠道都提到了同一处代码）。
    result = await IssueAgent(llm=NoopLLM()).run(
        {
            "title": "Expired token refresh fails",
            "body": "Steps: expire JWT, call API, expected refresh but actual 401. Impact: users lose sessions.",
            "labels": ["bug"],
        },
        {"code_references": [evidence], "conversation_evidence": [evidence]},
    )

    # 找出 evidence 里来源是 workspace_file 且 reference 为 code-1 的条目。
    matching = [
        item
        for item in result["evidence"]
        if item["source_type"] == "workspace_file" and item["reference"] == "code-1"
    ]
    # 断言：去重后只剩 1 条——同一处代码证据没有被重复罗列两次。
    assert len(matching) == 1


@pytest.mark.asyncio
async def test_issue_agent_matches_desktop_issue_to_electron_owner() -> None:
    # 一个"桌面化"需求 issue，团队里只有李勇宏擅长 electron。
    result = await IssueAgent(llm=NoopLLM()).run(
        {
            "title": "Feature: 增加桌面化能力",
            "body": "### What problem does this solve?\n可以把这个项目桌面化\n\n### Proposed solution\n解决需要打开网页才能使用的问题",
            "labels": [],
        },
        {
            "team_members": [
                {
                    "name": "李勇宏",
                    "role": "前端工程师",
                    "strengths": "擅长electron、js",
                    "techStack": "js, python",
                }
            ]
        },
    )

    # 断言：分类为 Feature（功能需求）。
    assert result["category"] == "Feature"
    # 断言：负责人被正确匹配到"李勇宏"——因为他的技能树里正好有 electron，和"桌面化"主题吻合。
    assert result["suggested_owner"] == "李勇宏"
    # 断言：指派理由里点出了"桌面化/electron"的关联，说明匹配是有依据的。
    assert "桌面化/electron" in result["owner_reason"]


@pytest.mark.asyncio
async def test_issue_agent_does_not_assign_person_when_team_is_empty() -> None:
    # 同样是桌面化需求，但这次团队列表为空（team_members 没给），只有项目文档提到了"布偶猫"。
    result = await IssueAgent(llm=NoopLLM()).run(
        {
            "title": "Feature: 增加桌面化能力",
            "body": "希望把项目桌面化，支持 Electron 和窗口管理。",
            "labels": [],
        },
        {"project_documents": [{"title": "README", "snippet": "项目文档里提到了布偶猫 / opus 这样的协作角色。"}]},
    )

    # 断言：分类为 Feature。
    assert result["category"] == "Feature"
    # 断言：没有可指派的人，owner 回退为"待分配"，而不是从文档里随便抓个"布偶猫"当负责人。
    assert result["suggested_owner"] == "待分配"
    # 断言：指派理由说明"还没有添加团队成员"——诚实告知原因。
    assert "还没有添加团队成员" in result["owner_reason"]


@pytest.mark.asyncio
async def test_issue_agent_rejects_llm_owner_not_in_team_members() -> None:
    # 这次让会"胡说"的 InventedOwnerLLM 上场，它坚持推荐"布偶猫"，且团队为空。
    result = await IssueAgent(llm=InventedOwnerLLM()).run(
        {
            "title": "Feature: 增加桌面化能力",
            "body": "希望把项目桌面化，支持 Electron 和窗口管理。",
            "labels": [],
        },
        {"team_members": []},
    )

    # 断言：尽管模型强烈推荐"布偶猫"，但因为它不在团队成员列表里，Agent 仍然判为"待分配"。
    assert result["suggested_owner"] == "待分配"
    # 断言：理由里也没有出现"布偶猫"——模型编造的人名被彻底排除，绝不让假 owner 混进结果。
    assert "布偶猫" not in result["owner_reason"]
