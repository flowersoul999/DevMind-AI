"""DevMind AI module."""

# pytest 测试框架。
import pytest

# 导入真实的 PR 评审智能体（PRReviewAgent），它会分析一个 Pull Request 并给出评审意见和合入建议。
from app.services.agents.pr_review_agent import PRReviewAgent


# 这是一个"假 LLM"（桩对象 / stub）。它实现 PRReviewAgent 需要的接口 chat_json，但永远返回空字典 {}。
# 为什么：测试前缀叫 Noop（no operation，啥也不干）。当 LLM 不给任何意见时，我们想验证 Agent 自身的"兜底规则"是否足够稳——
#       不依赖模型也能发出关键评审发现。这比调真实大模型便宜、可控、可复现。
class NoopLLM:
    async def chat_json(self, system_prompt, user_payload):
        """chat_json."""
        """chat_json."""
        return {}


# 另一个"假 LLM"：它假装是个过于自信的模型，LLM 自己就给出了合入建议 + 一条 blocking（阻塞）发现。
# 为什么：用这个 mock 模拟"模型返回了危险信号"的场景，验证 Agent 会不会尊重模型的 blocking 判断、把合入建议降级。
class OverconfidentLLM:
    async def chat_json(self, system_prompt, user_payload):
        return {
            "merge_recommendation": "建议合入",
            "review_findings": [
                {
                    "severity": "P2",
                    "title": "权限边界缺少验证",
                    "evidence": "diff 修改了权限判断，但没有对应测试证据。",
                    "required_action": "补充权限边界测试或人工验证记录。",
                    "blocking": True,
                }
            ],
            "blocking_issues": [],
        }


@pytest.mark.asyncio
async def test_pr_review_fallback_emits_blocking_findings() -> None:
    # 把 NoopLLM 注入 Agent：模型啥也没说。看 Agent 自己的规则能不能兜底。
    result = await PRReviewAgent(llm=NoopLLM()).run(
        {
            # PR 基本信息：编号、状态、作者、标题。
            "number": 12,
            "state": "open",
            "author": "alice",
            "title": "Update auth middleware",
            "body": "Refactor permission checks.",
            # 改动的文件：auth 中间件，一次加了 320 行——属于敏感的高风险改动。
            "files": [
                {
                    "filename": "backend/app/auth/middleware.py",
                    "additions": 320,
                    "deletions": 12,
                    "patch": "permission change",
                }
            ],
            # 评审评论里有人在提醒：别把 token 处理日志打印出来（安全关注点）。
            "comments": [{"body": "Please verify token handling is not logged."}],
        },
        # 额外上下文：CI 有 1 条失败记录。
        {"ci_summary": {"failed_runs": 1}},
    )

    # 把所有评审发现的 severity（严重程度）收集成集合。
    severities = {item["severity"] for item in result["review_findings"]}
    # 断言：严重程度集合至少包含 P1、P2——说明即便模型没说话，Agent 仍按规则发出了高危（P1）和中危（P2）发现。
    assert {"P1", "P2"} <= severities
    # 断言：合入建议被定为"暂缓"——高风险的权限改动 + CI 失败，绝不能建议直接合入。
    assert result["merge_recommendation"] == "暂缓"
    # 断言：确实产生了 blocking_issues（阻塞项），说明 Agent 拦住了这个 PR。
    assert result["blocking_issues"]


@pytest.mark.asyncio
async def test_pr_review_downgrades_merge_when_llm_returns_blocking_finding() -> None:
    # 这次注入 OverconfidentLLM：模型自己说"建议合入"，但同时给出了一条 blocking 发现。
    result = await PRReviewAgent(llm=OverconfidentLLM()).run(
        {
            "number": 13,
            "state": "open",
            "author": "alice",
            "title": "Update permission UI",
            "body": "Ready to merge.",
            "files": [
                {
                    "filename": "frontend/app/page.tsx",
                    "additions": 20,
                    "deletions": 3,
                    "patch": "permission UI change",
                }
            ],
            "comments": [],
        },
        {},
    )

    # 断言：合入建议被降级为"修改后合入"，而不是模型说的"建议合入"。
    # 为什么：模型说能合，但它自己又标了 blocking 发现——Agent 必须遵守"有阻塞项就不能直接合入"的硬规则，不被模型带偏。
    assert result["merge_recommendation"] == "修改后合入"
    # 断言：阻塞项里出现了模型给的"权限边界缺少验证"这条（说明 Agent 采信并保留了模型的 blocking 发现）。
    assert any("权限边界缺少验证" in item for item in result["blocking_issues"])
    # 断言：第一条评审发现的 severity 是 P2（与模型返回一致，没有被错误改写）。
    assert result["review_findings"][0]["severity"] == "P2"
