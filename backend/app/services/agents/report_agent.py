"""DevMind AI module."""

# Any：任意类型占位
from typing import Any

# 导入 Agent 基类
from app.services.agents.base import BaseAgent


# 模块说明（给零基础同事看的）：
# ReportAgent 是「研发周报生成器」。
# 它接收一个时间范围内的仓库活动数据（Issues、PRs、CI 运行、指标），拼成一份 Markdown 周报：
# 数据概览、本周完成、进行中、风险与阻塞、需要关注的 PR、失败 CI、下周建议。
# 输入：issues / pull_requests / workflow_runs / metrics + 上下文（repo_name、起止日期）。
# 输出：一个字典 {"report_markdown": "……"}，里面是排版好的周报文本。
# 被谁调用：通常由 ChatAgent 的 report_agent.generate_weekly_report 工具，或定时任务触发。
# 典型调用链：定时/手动触发 → 拉取近期仓库活动 → ReportAgent.run() → 生成周报 Markdown。
# 为什么这样写：周报本质是「模板 + 数据填充」，纯字符串拼装即可，不需要 LLM；
#               这样快、稳定、可控，也不会乱编数据。
class ReportAgent(BaseAgent):
    """ReportAgent."""
    name = "report_agent"
    description = "根据仓库活动生成工程周报。"
    available_tools = ["list_issues_by_time_range", "list_prs_by_time_range", "list_failed_workflow_runs"]

    # 主入口：统计各项数据并拼装 Markdown 周报
    async def run(self, input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        # 取出三类活动数据
        issues = input_data.get("issues", [])
        prs = input_data.get("pull_requests", [])
        ci_runs = input_data.get("workflow_runs", [])
        # 指标（可能上游已算好，没有就现场数）
        metrics = input_data.get("metrics", {})
        # 失败的 CI 运行
        failed_ci = [run for run in ci_runs if run.get("conclusion") == "failure"]
        # 已合并的 PR
        merged_prs = [pr for pr in prs if pr.get("merged_at")]
        # 仍 open 的 PR
        open_prs = [pr for pr in prs if pr.get("state") == "open"]
        # 已关闭的 Issue
        closed_issues = [issue for issue in issues if issue.get("state") == "closed"]
        # 仓库名（拿不到就用 repo_id 拼一个）
        repo_name = context.get("repo_name") or f"Repo {context.get('repo_id', '')}".strip()
        # 时间范围文案
        date_range = f"{context.get('start_date', '')} 至 {context.get('end_date', '')}".strip()
        # 合并 PR 编号串（最多 8 个，没有显示「暂无」）
        merged_pr_numbers = ", ".join(f"#{pr.get('number')}" for pr in merged_prs[:8]) or "暂无"
        # 关闭 Issue 编号串
        closed_issue_numbers = ", ".join(f"#{issue.get('number')}" for issue in closed_issues[:8]) or "暂无"
        # 用 f-string 多段拼接出完整 Markdown 周报（\n 是换行，\n\n 是段落空行）
        markdown = (
            f"# {repo_name} 研发周报\n\n"
            f"> 时间范围：{date_range}\n\n"
            "## 数据概览\n"
            f"- Issues：{metrics.get('issues', len(issues))} 个\n"
            f"- PRs：{metrics.get('pull_requests', len(prs))} 个，其中 Merged PR {metrics.get('merged_prs', len(merged_prs))} 个，Open PR {metrics.get('open_prs', len(open_prs))} 个\n"
            f"- 失败 CI：{metrics.get('failed_ci', len(failed_ci))} 次\n\n"
            "## 本周完成\n"
            f"- 合并 PR {len(merged_prs)} 个：{merged_pr_numbers}。\n"
            f"- 关闭 Issue {len(closed_issues)} 个：{closed_issue_numbers}。\n\n"
            "## 进行中\n"
            f"- 当前仍有 {len(open_prs)} 个 Open PR 需要跟进。\n"
            # 列出前 5 个 open PR；没有就提示暂无
            + "\n".join(f"- #{pr.get('number')} {pr.get('title')}" for pr in open_prs[:5])
            + ("\n" if open_prs else "- 暂无 Open PR。\n")
            + "\n"
            "## 风险与阻塞\n"
            f"- 失败 CI {len(failed_ci)} 次，建议优先排查重复失败的 workflow。\n"
            # 没有失败 CI 时补一句说明
            + ("- 本期没有同步到失败 CI。\n" if not failed_ci else "")
            + "\n"
            "## 需要关注的 PR\n"
            # 列出前 5 个 PR
            + "\n".join(f"- #{pr.get('number')} {pr.get('title')}" for pr in prs[:5])
            + "\n\n## 失败 CI\n"
            # 列出前 5 个失败 CI（名字 + 结论）
            + "\n".join(f"- {run.get('name')}: {run.get('conclusion')}" for run in failed_ci[:5])
            + "\n\n## 下周建议\n"
            "- 补充关键模块测试覆盖。\n- 对高风险 PR 提前拆分 Review。\n"
        )
        return {"report_markdown": markdown}
