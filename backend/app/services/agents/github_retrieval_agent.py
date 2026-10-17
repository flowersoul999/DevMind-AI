"""
services/agents/github_retrieval_agent.py —— 统一「取 GitHub 原始数据」的检索 Agent
============================================================================

这是所有分析 Agent 的「数据供货商」。它自己不思考、不分析，只负责按指令把 GitHub
上的原始数据拉回来，交给上游（比如 Planner 编排 / Issue 分诊 / CI 诊断）去用。

它继承自 BaseAgent，并通过 available_tools 声明自己「会哪些取数动作」。run()
方法根据传入的 task 类型，调用不同的取数函数：
  - issues              ：拉 issue 列表
  - pull_requests       ：拉 PR 列表
  - pull_request_detail ：拉某 PR 的文件改动 + review 评论
  - workflow_runs       ：拉 CI 流水线运行列表
  - workflow_run_failure：拉某次失败运行的 jobs + 日志（CI 诊断要用）
  - 其它 / 不指定        ：默认拉「仓库全景」（issues + PR + workflow_runs）

为什么独立成一个 Agent 而不是在各 Agent 里直接调 GitHub？为了「取数」逻辑集中、
可复用、可被测；上层只说「我要什么」，不用关心 GitHub API 细节。."""

from typing import Any  # 任意类型

from app.services.agents.base import BaseAgent  # Agent 基类，提供通用接口
from app.services.github.actions import get_workflow_run_logs, list_workflow_run_jobs, list_workflow_runs  # CI 取数
from app.services.github.issues import list_issues  # issue 取数
from app.services.github.pull_requests import list_pull_request_files, list_pull_request_review_comments, list_pull_requests  # PR 取数


class GitHubRetrievalAgent(BaseAgent):
    """GitHubRetrievalAgent."""
    name = "github_retrieval_agent"  # Agent 的名字，用于登记和日志
    description = "检索 GitHub Issue、PR、diff、Review 评论、workflow run 和失败日志。"  # 一句话能力说明
    # 声明本 Agent 对外暴露的「工具名」列表（和上面的 import 对应）
    available_tools = [
        "list_issues",
        "list_pull_requests",
        "list_pull_request_files",
        "list_pull_request_review_comments",
        "list_workflow_runs",
        "list_workflow_run_jobs",
        "get_workflow_run_logs",
    ]

    async def run(self, input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        # input_data 是上游给的指令；context 是共享上下文（这里没用到，但基类签名要求）
        owner = input_data["owner"]  # 仓库所有者（必填）
        repo = input_data["repo"]  # 仓库名（必填）
        token = input_data.get("token")  # 访问令牌（可选）
        base_url = input_data.get("base_url")  # 自建 GitHub 地址（可选）
        limit = int(input_data.get("limit", 30))  # 拉取条数，默认 30；int() 把字符串转成整数
        task = input_data.get("task", "repo_snapshot")  # 要做什么任务，默认「仓库全景」

        if task == "issues":
            # 只拉 issue 列表，返回 {"issues": [...]}
            return {"issues": await list_issues(owner, repo, token=token, limit=limit, base_url=base_url)}
        if task == "pull_requests":
            return {"pull_requests": await list_pull_requests(owner, repo, token=token, limit=limit, base_url=base_url)}
        if task == "pull_request_detail":
            # PR 详情：同时拉「改了哪些文件」和「已有的 review 评论」，一起返回
            number = int(input_data["number"])  # PR 编号（必填）
            files = await list_pull_request_files(owner, repo, number, token=token, base_url=base_url)
            comments = await list_pull_request_review_comments(owner, repo, number, token=token, base_url=base_url)
            return {"files": files, "review_comments": comments}
        if task == "workflow_runs":
            return {"workflow_runs": await list_workflow_runs(owner, repo, token=token, limit=limit, base_url=base_url)}
        if task == "workflow_run_failure":
            # CI 失败诊断专用：拉「这次运行有哪些 job」和「完整日志文本」
            run_id = int(input_data["run_id"])  # 运行编号（必填）
            jobs = await list_workflow_run_jobs(owner, repo, run_id, token=token, base_url=base_url)
            logs_text = await get_workflow_run_logs(owner, repo, run_id, token=token, base_url=base_url)
            return {"jobs": jobs, "logs_text": logs_text}

        # 默认分支：没指定具体 task 时，拉「仓库全景」——issues + PR + workflow 三件套，方便总览
        issues = await list_issues(owner, repo, token=token, limit=limit, base_url=base_url)
        pull_requests = await list_pull_requests(owner, repo, token=token, limit=limit, base_url=base_url)
        workflow_runs = await list_workflow_runs(owner, repo, token=token, limit=limit, base_url=base_url)
        return {"issues": issues, "pull_requests": pull_requests, "workflow_runs": workflow_runs}
