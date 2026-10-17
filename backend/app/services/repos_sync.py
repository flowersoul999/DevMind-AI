"""
services/repos_sync.py —— 把 GitHub 上的 Issue / PR / CI 数据「搬」进本地数据库
============================================================================

这是「仓库同步」的核心实现。用户在界面点「同步」或后台定时任务触发时，就走到这里。
它做的事很单纯：调 GitHub 接口拉数据 → 和本地库里已有的记录比对 → 有则更新、无则新建
→ 最后 commit 落库。支持三类数据：Issue、PullRequest（含改动文件 + review 评论）、
WorkflowRun（CI 运行，失败时还顺带把日志拉下来）。

为什么叫「sync（同步）」而不是「import」：它是增量的——已存在的记录不会重复建，
只是刷新字段，所以同一仓库反复同步是安全的。

注意：本文件只负责「结构化数据入库」，**不**做向量化/RAG（那是另一套流程），
避免一次同步又重算 embedding 拖慢速度。."""

from datetime import datetime, timezone  # 处理时间；timezone 用于生成带时区的「现在」
from uuid import UUID  # 仓库 id 是 UUID 类型

import httpx  # HTTP 异常类型
from fastapi import HTTPException  # 抛 HTTP 错误
from sqlalchemy.orm import Session  # 数据库会话

from app.core.config import settings  # 全局配置（token 加密密钥）
from app.core.security import decrypt_token  # 解密存在库里的 GitHub token
from app.db.models import Issue, PRFile, PRReviewComment, PullRequest, Repository, WorkflowRun  # 相关表
from app.schemas.repos import RepoSyncRequest, RepoSyncResponse  # 请求/响应结构
from app.services.github.actions import get_workflow_run_logs, list_workflow_run_jobs, list_workflow_runs  # CI 取数
from app.services.github.issues import list_issues  # issue 取数
from app.services.github.pull_requests import list_pull_request_files, list_pull_request_review_comments, list_pull_requests  # PR 取数


def parse_github_datetime(value: str | None) -> datetime | None:
    # GitHub 返回的时间像 "2025-01-01T12:00:00Z"，Z 表示 UTC 零时区
    if not value:
        return None  # 空值直接返回 None
    # replace("Z","+00:00") 把 Z 换成 Python 能解析的时区写法，再转成 datetime 对象
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def login_of(user: dict | None) -> str | None:
    # GitHub 的用户对象里登录名在 "login" 字段；这个函数安全地取出它（user 可能是 None）
    return user.get("login") if user else None


async def sync_repository(db: Session, repo: Repository, payload: RepoSyncRequest) -> RepoSyncResponse:
    # 主入口：对单个仓库做同步。db=数据库会话，repo=仓库对象，payload=本次同步的选项（同步哪些、拉多少）
    if repo.provider not in {"github", "github_compatible"}:
        # 目前只支持 GitHub 系；其它来源（如 GitLab）还没实现，直接 400 拒绝
        raise HTTPException(status_code=400, detail=f"{repo.provider} sync is not implemented yet")

    synced = {"issues": 0, "pull_requests": 0, "workflow_runs": 0}  # 计数器：记录本次各同步了多少条
    # 如果仓库存了加密 token 就解密出来用（库里只存密文，见 core/security.py）；否则用匿名（公开库可读）
    token = decrypt_token(repo.github_token_encrypted, settings.token_encryption_key) if repo.github_token_encrypted else None
    base_url = repo.api_base_url  # 仓库对应的 GitHub API 地址（支持自建）

    if payload.sync_issues:
        # ---- 第一部分：同步 Issue ----
        try:
            remote_issues = await list_issues(repo.owner, repo.name, token=token, limit=payload.limit, base_url=base_url)
        except httpx.HTTPStatusError as exc:
            # GitHub 返回错误（如 token 无效 401），转成 400 告诉前端
            raise HTTPException(status_code=400, detail=f"GitHub issues sync failed: {exc.response.status_code} {exc.response.text}") from exc
        for item in remote_issues:
            # 用「仓库 id + GitHub issue id」定位本地是否已有这条；one_or_none 取一条或 None
            issue = db.query(Issue).filter(Issue.repo_id == repo.id, Issue.github_issue_id == item.get("id")).one_or_none()
            if issue is None:
                # 本地没有：新建，填必填字段（github_issue_id 是 GitHub 那边的唯一 id）
                issue = Issue(repo_id=repo.id, github_issue_id=item.get("id"), number=item.get("number", 0), title=item.get("title") or "")
                db.add(issue)  # 加入会话（还没提交）
            # 下面这些字段无论新建还是已有都刷新（增量更新）
            issue.number = item.get("number", issue.number)  # 编号；取不到就保留原值（.get 第二参）
            issue.title = item.get("title") or issue.title  # 标题；空则用原值
            issue.body = item.get("body")  # 正文（可能为空）
            issue.state = item.get("state") or "open"  # 状态；默认 open
            # 标签：从 GitHub 的 labels 列表里抽出每个的 name，过滤掉没有 name 的
            issue.labels = [label.get("name") for label in item.get("labels", []) if label.get("name")]
            issue.author = login_of(item.get("user"))  # 作者登录名
            issue.assignees = [login_of(user) for user in item.get("assignees", []) if login_of(user)]  # 指派人列表
            issue.created_at = parse_github_datetime(item.get("created_at"))  # 创建时间
            issue.updated_at = parse_github_datetime(item.get("updated_at"))  # 更新时间
            issue.closed_at = parse_github_datetime(item.get("closed_at"))  # 关闭时间（可能为空）
            synced["issues"] += 1  # 计数 +1

    if payload.sync_pull_requests:
        # ---- 第二部分：同步 PR（含文件改动和 review 评论）----
        try:
            remote_prs = await list_pull_requests(repo.owner, repo.name, token=token, limit=payload.limit, base_url=base_url)
        except httpx.HTTPStatusError as exc:
            raise HTTPException(status_code=400, detail=f"GitHub pull requests sync failed: {exc.response.status_code} {exc.response.text}") from exc
        for item in remote_prs:
            pr = db.query(PullRequest).filter(PullRequest.repo_id == repo.id, PullRequest.github_pr_id == item.get("id")).one_or_none()
            if pr is None:
                pr = PullRequest(repo_id=repo.id, github_pr_id=item.get("id"), number=item.get("number", 0), title=item.get("title") or "")
                db.add(pr)
                db.flush()  # flush 让数据库分配 pr.id，后面存 PRFile/PRReviewComment 要用它做外键
            pr.number = item.get("number", pr.number)
            pr.title = item.get("title") or pr.title
            pr.body = item.get("body")
            pr.state = item.get("state") or "open"
            pr.author = login_of(item.get("user"))
            # base/head 是 PR 的「目标分支 / 来源分支」；(item.get("base") or {}).get("ref") 安全取分支名
            pr.base_branch = (item.get("base") or {}).get("ref")
            pr.head_branch = (item.get("head") or {}).get("ref")
            pr.merged_at = parse_github_datetime(item.get("merged_at"))
            pr.created_at = parse_github_datetime(item.get("created_at"))
            pr.updated_at = parse_github_datetime(item.get("updated_at"))

            # 文件改动：先把本地旧的 PRFile 删掉，再按 GitHub 最新结果重建（PR 文件会变，直接全量覆盖最简单）
            db.query(PRFile).filter(PRFile.pr_id == pr.id).delete()
            try:
                files = await list_pull_request_files(repo.owner, repo.name, pr.number, token=token, base_url=base_url)
            except httpx.HTTPStatusError:
                files = []  # 拉文件失败就当没有，不中断整个同步
            for file_item in files:
                db.add(
                    PRFile(
                        pr_id=pr.id,
                        filename=file_item.get("filename") or "",  # 文件路径
                        status=file_item.get("status") or "modified",  # added/modified/removed
                        additions=file_item.get("additions") or 0,  # 新增行数
                        deletions=file_item.get("deletions") or 0,  # 删除行数
                        patch=file_item.get("patch"),  # diff 文本（可能为空）
                    )
                )
            # review 评论同理：先删旧的，再重建
            db.query(PRReviewComment).filter(PRReviewComment.pr_id == pr.id).delete()
            try:
                review_comments = await list_pull_request_review_comments(repo.owner, repo.name, pr.number, token=token, base_url=base_url)
            except httpx.HTTPStatusError:
                review_comments = []
            for comment_item in review_comments:
                db.add(
                    PRReviewComment(
                        pr_id=pr.id,
                        github_comment_id=comment_item.get("id"),
                        body=comment_item.get("body"),  # 评论内容
                        path=comment_item.get("path"),  # 评论针对的文件
                        line=comment_item.get("line") or comment_item.get("original_line"),  # 行号（原行号兜底）
                        author=login_of(comment_item.get("user")),  # 评论人
                        created_at=parse_github_datetime(comment_item.get("created_at")),
                        updated_at=parse_github_datetime(comment_item.get("updated_at")),
                    )
                )
            synced["pull_requests"] += 1

    if payload.sync_workflow_runs:
        # ---- 第三部分：同步 CI 运行（WorkflowRun）----
        try:
            remote_runs = await list_workflow_runs(repo.owner, repo.name, token=token, limit=payload.limit, base_url=base_url)
        except httpx.HTTPStatusError:
            remote_runs = []  # CI 拉取失败不中断
        for item in remote_runs:
            run = db.query(WorkflowRun).filter(WorkflowRun.repo_id == repo.id, WorkflowRun.github_run_id == item.get("id")).one_or_none()
            if run is None:
                run = WorkflowRun(repo_id=repo.id, github_run_id=item.get("id"), name=item.get("name") or "workflow")
                db.add(run)
            run.name = item.get("name") or run.name
            run.status = item.get("status") or "unknown"  # queued/in_progress/completed
            run.conclusion = item.get("conclusion")  # success/failure/cancelled（可能为 None）
            run.html_url = item.get("html_url")  # GitHub 上这次运行的网页链接
            run.created_at = parse_github_datetime(item.get("created_at"))
            run.updated_at = parse_github_datetime(item.get("updated_at"))
            if run.github_run_id:
                # 进一步拉这次运行的 jobs（编译/测试等步骤），存成 JSON 方便前端展示
                try:
                    jobs = await list_workflow_run_jobs(repo.owner, repo.name, run.github_run_id, token=token, base_url=base_url)
                    # 列表推导：把每个 job 和里面的 step 整理成字典列表
                    run.jobs = [
                        {
                            "id": job.get("id"),
                            "name": job.get("name"),
                            "status": job.get("status"),
                            "conclusion": job.get("conclusion"),
                            "html_url": job.get("html_url"),
                            "started_at": job.get("started_at"),
                            "completed_at": job.get("completed_at"),
                            # 嵌套列表推导：把 job 里的 steps 也整理成精简字典
                            "steps": [
                                {
                                    "name": step.get("name"),
                                    "status": step.get("status"),
                                    "conclusion": step.get("conclusion"),
                                    "number": step.get("number"),
                                }
                                for step in job.get("steps", [])
                            ],
                        }
                        for job in jobs
                    ]
                except httpx.HTTPStatusError:
                    run.jobs = run.jobs or []  # 拉失败就保留原值或空
            if run.conclusion == "failure" and run.github_run_id:
                # 只有「失败的」运行才值得花流量去拉日志（成功的不用），用于 CI 诊断 Agent
                try:
                    run.logs_text = await get_workflow_run_logs(repo.owner, repo.name, run.github_run_id, token=token, base_url=base_url)
                except httpx.HTTPStatusError:
                    run.logs_text = run.logs_text  # 拉失败保留原值
            synced["workflow_runs"] += 1

    repo.last_sync_at = datetime.now(timezone.utc)  # 记下「最后同步时间」（UTC 标准时）
    db.flush()  # 把上面的改动刷进事务
    db.commit()  # 真正提交到数据库（前面的 add/修改在此之前都在内存里）
    # 返回同步结果：仓库 id、状态 completed、各类型同步条数
    return RepoSyncResponse(repo_id=repo.id, status="completed", synced=synced)


async def sync_repository_by_id(repo_id: UUID, payload: RepoSyncRequest) -> RepoSyncResponse | None:
    # 给「只知道仓库 id」的调用方用的便捷入口（后台定时任务 scheduler.py 就调它）
    from app.db.session import SessionLocal  # 在函数内导入，避免顶层循环依赖

    db = SessionLocal()  # 新建一个数据库会话
    try:
        repo = db.get(Repository, repo_id)  # 按主键取仓库
        if repo is None:
            return None  # 仓库不存在就返回 None（调用方自行处理）
        return await sync_repository(db, repo, payload)  # 委托给上面的主函数
    finally:
        db.close()  # 无论成败都关闭会话，释放连接
