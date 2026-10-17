"""
services/github/issues.py —— 拉取 GitHub Issue 列表的「取数函数」
============================================================================

这个文件很薄，只做一件事：从 GitHub 拿到某个仓库的 issue 列表。
它和 services/github/client.py 的关系，就像「点餐员」和「厨房」——这里决定
「要什么菜」，真正的「做菜/发请求」交给 GitHubClient。

为什么要单独一个小文件？
----------------------
issue 是 DevMind AI 的核心分析对象之一（Issue 分诊 Agent 会用到）。把它和
PR、Actions 拆开，结构更清晰，以后想改 issue 的拉取逻辑不影响别的。

一个重要细节：GitHub 的接口里 PR（拉取请求）也会被当成 issue 返回，所以这里
用一行过滤把 PR 排除掉，只留真正的 issue。
"""

from app.services.github.client import GitHubClient  # 引入底层客户端，所有请求经它发出


async def list_issues(owner: str, repo: str, token: str | None = None, limit: int = 30, base_url: str | None = None) -> list[dict]:
    # owner=仓库所有者(用户名或组织名)，repo=仓库名，token=访问令牌，limit=最多拉几条，base_url=支持自建 GitHub
    client = GitHubClient(token, base_url)  # 建客户端；不传 token 就用全局配置
    # 调用 GitHub 的 issues 接口：state="all" 表示打开和关闭的都要；per_page=limit 表示每页几条
    items = await client.get(f"/repos/{owner}/{repo}/issues", {"state": "all", "per_page": limit})
    # 为什么过滤 pull_request：GitHub 把 PR 也混在 issues 列表里返回（PR 本质是一种特殊 issue）。
    # 这里只保留「不是 PR」的条目，确保拿到的是纯 issue。
    return [item for item in items if "pull_request" not in item]
