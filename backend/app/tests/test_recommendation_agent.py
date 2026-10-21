"""DevMind AI module."""

# 导入 pytest 测试框架。
import pytest

# 导入真实的"建议智能体" RecommendationAgent：它根据仓库的 issue / PR / CI 状态，给开发者推荐接下来该做什么。
from app.services.agents.recommendation_agent import RecommendationAgent


@pytest.mark.asyncio
async def test_recommendation_agent_avoids_excluded_prompts() -> None:
    # 创建一个 RecommendationAgent 实例（这里没 mock，跑的是它内置的确定性规则逻辑）。
    agent = RecommendationAgent()
    # 构造一份"仓库快照"：有 1 个 issue、1 个 PR、1 条失败的 CI，limit 表示最多给 5 条建议。
    payload = {
        "issues": [{"number": 12, "title": "Login returns 500", "state": "open"}],
        "pull_requests": [{"number": 8, "title": "Refactor auth middleware", "state": "open"}],
        "workflow_runs": [{"name": "backend-tests", "status": "completed", "conclusion": "failure"}],
        "limit": 5,
    }

    # 第一次运行，拿到第一批建议列表。
    first = await agent.run(payload, {})
    # 第二次运行时，把第一次给的所有建议放进 exclude（排除列表），看 Agent 会不会"换个说法/换条建议"。
    # 为什么：验证 Agent 的"去重"能力——用户已经看过第一批了，不该再被重复推荐同样的内容。
    second = await agent.run({**payload, "exclude": first["suggestions"]}, {})

    # 断言：第一批建议数量在 4~5 条之间（说明仓库有料可推，且没超过 limit）。
    assert 4 <= len(first["suggestions"]) <= 5
    # 断言：第二批建议数量也在 4~5 条之间（说明排除后仍有足够候选，没有因为排除就饿死）。
    assert 4 <= len(second["suggestions"]) <= 5
    # 断言：两批建议完全不重叠（isdisjoint = 没有交集）。这是核心断言：排除机制真的生效了。
    assert set(first["suggestions"]).isdisjoint(second["suggestions"])


@pytest.mark.asyncio
async def test_recommendation_agent_returns_contextual_prompts_for_empty_repo() -> None:
    # 喂一个"空仓库"：没有 issue、PR、CI 记录，看 Agent 怎么兜底。
    result = await RecommendationAgent().run({"issues": [], "pull_requests": [], "workflow_runs": [], "limit": 5}, {})

    # 断言：空仓库也要给出 4~5 条"通用建议"（比如学习类、整理类），而不是返回空列表。
    assert 4 <= len(result["suggestions"]) <= 5
    # 断言：这些通用建议里不能出现"失败""PR #""Issue #"——因为仓库里根本没有这些具体事件，提到它们就是胡说八道。
    # 为什么：空仓库给不出针对具体事件的建议，Agent 必须退回到通用引导，而不是编造不存在的 PR/Issue。
    assert all("失败" not in prompt and "PR #" not in prompt and "Issue #" not in prompt for prompt in result["suggestions"])


@pytest.mark.asyncio
async def test_recommendation_agent_cycles_when_candidates_are_exhausted() -> None:
    # 同样用空仓库，连续三轮都排除掉之前给过的建议。
    agent = RecommendationAgent()
    payload = {"issues": [], "pull_requests": [], "workflow_runs": [], "limit": 5}
    first = await agent.run(payload, {})
    # 第二轮：排除第一轮的全部建议。
    second = await agent.run({**payload, "exclude": first["suggestions"]}, {})
    # 第三轮：把前两轮的建议都排除掉。
    third = await agent.run({**payload, "exclude": [*first["suggestions"], *second["suggestions"]]}, {})

    # 断言：三轮都还能给出非空建议。
    # 为什么：候选用尽时 Agent 应该"循环复用"通用建议（而不是返回空或报错），保证用户始终有东西看。
    assert first["suggestions"]
    assert second["suggestions"]
    assert third["suggestions"]


@pytest.mark.asyncio
async def test_recommendation_agent_does_not_suggest_missing_pr_or_failed_ci() -> None:
    # 造一个场景：只有 1 个 issue，但没有 PR、也没有 CI 记录。
    result = await RecommendationAgent().run(
        {
            "issues": [{"number": 3, "title": "Feature request", "state": "open"}],
            "pull_requests": [],
            "workflow_runs": [],
            "limit": 5,
        },
        {},
    )

    # 断言：既然仓库里没有 PR、也没有失败 CI，建议里就不能出现"PR"或"CI 为什么失败"这种话。
    # 为什么：验证 Agent 不会"无中生有"——它只能基于真实存在的事件给建议，避免误导用户。
    assert all("PR" not in prompt and "CI 为什么失败" not in prompt for prompt in result["suggestions"])
