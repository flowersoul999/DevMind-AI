"""
services/github/client.py —— GitHub REST API 的「万能遥控器」
============================================================================

这是整个项目访问 GitHub 的最底层工具。你可以把它想象成一把「万能遥控器」：
上层想要 GitHub 的任何数据（仓库信息、issue、PR、评论、标签…），都通过它
按一个按钮（调用一个方法）来发送 HTTP 请求，它负责把请求拼好、带上身份令牌、
发出去、把返回的 JSON 解析好交给上层。

为什么单独抽一个客户端类？
--------------------------
1. 所有 GitHub 请求都要带几乎一样的请求头（认证 token、API 版本），集中放在
   _headers() 里，避免每个接口都复制粘贴。
2. 统一处理超时、重定向、错误状态码，上层调用更省心。
3. 以后要换 base_url（比如连公司自建的 GitHub Enterprise），只改这里。

对外主要方法：
  - get / get_bytes / post / patch ：四种 HTTP 动作的封装
  - get_repo / create_issue_comment / create_issue / close_issue / add_issue_labels ：常用的业务快捷方法."""

from typing import Any  # Any 表示「任意类型」，用于返回结构不固定的 JSON

import httpx  # 第三方 HTTP 客户端库，比 Python 自带的更现代、支持异步

from app.core.config import settings  # 引入全局配置（里面有 GitHub token 和 API 地址）


class GitHubClient:
    # 为什么用类而不是一堆函数：每个实例可以绑定自己的 token / base_url，调用时不用每次传
    def __init__(self, token: str | None = None, base_url: str | None = None) -> None:
        # 初始化：token 没传就用配置文件里的全局 token；base_url 没传就用配置里的 API 地址
        self.token = token or settings.github_token
        # rstrip("/") 去掉地址末尾多余的斜杠，避免拼路径时出现 "//" 这种错误
        self.base_url = (base_url or settings.github_api_base_url).rstrip("/")

    def _headers(self) -> dict[str, str]:
        # 私有方法（下划线开头）：构造每次请求都要带的 HTTP 头
        # Accept 告诉 GitHub 我们要 json 格式；X-GitHub-Api-Version 锁定 API 版本，防止 GitHub 升级后不兼容
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:  # 只有配置了 token 才加认证头，否则走匿名（公开仓库可读）
            headers["Authorization"] = f"Bearer {self.token}"  # Bearer 是「持票人令牌」的标准写法
        return headers

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        # 发送 GET 请求（查数据用）。params 是 URL 后面的查询参数，比如 ?state=all
        # 为什么用 async with：httpx 的客户端用完要关闭，with 语法保证「用完自动回收」，不会漏连接
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            # timeout=30 表示最多等 30 秒；follow_redirects 表示遇到跳转自动跟过去
            response = await client.get(f"{self.base_url}{path}", headers=self._headers(), params=params)
            response.raise_for_status()  # 如果返回 4xx/5xx 错误码，这里直接抛异常，让上层知道失败了
            return response.json()  # 把 GitHub 返回的 JSON 字符串解析成 Python 字典/列表

    async def get_bytes(self, path: str, params: dict[str, Any] | None = None) -> bytes:
        # 和 get 类似，但返回「原始字节」——用于下载文件/压缩包（JSON 用不到）
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            response = await client.get(f"{self.base_url}{path}", headers=self._headers(), params=params)
            response.raise_for_status()
            return response.content  # .content 是原始字节；.json() 是解析后的对象，这里要前者

    async def post(self, path: str, payload: dict[str, Any]) -> Any:
        # 发送 POST 请求（新建数据用），payload 是请求体（要发给 GitHub 的数据）
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            # json=payload 会让 httpx 自动把字典转成 JSON 文本，并加上 Content-Type: application/json 头
            response = await client.post(f"{self.base_url}{path}", headers=self._headers(), json=payload)
            response.raise_for_status()
            return response.json()

    async def patch(self, path: str, payload: dict[str, Any]) -> Any:
        # 发送 PATCH 请求（局部修改数据用，比如把 issue 标记为关闭）
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.patch(f"{self.base_url}{path}", headers=self._headers(), json=payload)
            response.raise_for_status()
            return response.json()

    async def get_repo(self, owner: str, repo: str) -> dict[str, Any]:
        # 快捷方法：获取某个仓库的基本信息（语言、star 数、默认分支等）
        return await self.get(f"/repos/{owner}/{repo}")

    async def create_issue_comment(self, owner: str, repo: str, issue_number: int, body: str) -> dict[str, Any]:
        # 在某个 issue（或 PR）下发表评论。注意 GitHub 把 PR 也当作一种 issue，所以路径带 issues
        return await self.post(f"/repos/{owner}/{repo}/issues/{issue_number}/comments", {"body": body})

    async def create_issue(self, owner: str, repo: str, title: str, body: str) -> dict[str, Any]:
        # 新建一个 issue，title 是标题，body 是正文
        return await self.post(f"/repos/{owner}/{repo}/issues", {"title": title, "body": body})

    async def close_issue(self, owner: str, repo: str, issue_number: int) -> dict[str, Any]:
        # 关闭一个 issue：PATCH 把 state 改成 "closed"
        return await self.patch(f"/repos/{owner}/{repo}/issues/{issue_number}", {"state": "closed"})

    async def add_issue_labels(self, owner: str, repo: str, issue_number: int, labels: list[str]) -> dict[str, Any]:
        # 给 issue 添加标签（比如 "bug" "priority/high"），labels 是标签名列表
        return await self.post(f"/repos/{owner}/{repo}/issues/{issue_number}/labels", {"labels": labels})
