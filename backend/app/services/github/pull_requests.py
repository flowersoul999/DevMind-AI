"""
services/github/pull_requests.py —— 拉取 GitHub PR（拉取请求）相关数据的「取数函数」
============================================================================

PR 就是开发者说「我改好的代码想合进主分支，请 review」，Pull Request 直译
「拉取请求」。DevMind AI 的 PR 审查 Agent 要靠这里拉来的数据工作。

本文件提供 3 个函数：
  - list_pull_requests               ：列出某仓库的 PR（含标题、状态、作者）
  - list_pull_request_files          ：列出某个 PR 改了哪些文件（审查要看 diff）
  - list_pull_request_review_comments：列出某 PR 已有的 review 评论
"""

from app.services.github.client import GitHubClient  # 引入底层客户端


async def list_pull_requests(owner: str, repo: str, token: str | None = None, limit: int = 30, base_url: str | None = None) -> list[dict]:
    client = GitHubClient(token, base_url)  # 建客户端
    # GitHub 的 PR 列表接口路径是 /pulls；state="all" 拉取打开和已合并的
    return await client.get(f"/repos/{owner}/{repo}/pulls", {"state": "all", "per_page": limit})


async def list_pull_request_files(owner: str, repo: str, number: int, token: str | None = None, base_url: str | None = None) -> list[dict]:
    # number 是 PR 的编号（不是 id，是页面上看到的 #123 那个数字）
    client = GitHubClient(token, base_url)
    # /pulls/{number}/files 返回这个 PR 改动的所有文件（文件名、增删行数、patch 文本）
    return await client.get(f"/repos/{owner}/{repo}/pulls/{number}/files", {"per_page": 100})


async def list_pull_request_review_comments(owner: str, repo: str, number: int, token: str | None = None, base_url: str | None = None) -> list[dict]:
    client = GitHubClient(token, base_url)
    # /pulls/{number}/comments 返回别人已经写的行内 review 评论（在某行代码旁的批注）
    return await client.get(f"/repos/{owner}/{repo}/pulls/{number}/comments", {"per_page": 100})
