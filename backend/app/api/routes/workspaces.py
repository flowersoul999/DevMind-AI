"""DevMind AI module."""

# workspaces.py —— 工作区（Workspace，多仓库集合）路由层
#
# 业务域：工作区。一个 Workspace 把多个仓库「打包」成一个项目视图，
# 方便跨仓库生成统一报表。本文件提供：创建工作区、列出工作区、
# 以及「多仓库研发周报」（把多个仓库的数据合并成一份报告）。
#
# 暴露端点：
#   POST /api/workspaces          —— 新建工作区（需 settings:manage 权限）
#   GET  /api/workspaces          —— 列出我的工作区
#   POST /api/workspaces/multi-repo-report —— 生成多仓库合并周报（需 agent:run 权限）
#
# 依赖 service：app.services.permissions（权限校验）、app.services.rag.vector_store.add_document（存周报）
from datetime import datetime, time, timezone  # 日期时间
from uuid import UUID  # UUID 类型

from fastapi import APIRouter, Depends, HTTPException  # 路由、依赖、异常
from sqlalchemy.orm import Session  # 数据库会话

from app.db.models import Issue, PullRequest, Repository, User, WorkflowRun, Workspace  # 用到的表
from app.db.session import get_db  # 会话依赖
from app.schemas.workspaces import MultiRepoReportRequest, MultiRepoReportResponse, WorkspaceCreateRequest, WorkspaceResponse  # 模型
from app.services.permissions import require_permission, write_audit_log  # 权限校验 + 审计日志
from app.services.rag.vector_store import add_document  # 把周报写进知识库

router = APIRouter()  # 路由器


def _bounds(payload: MultiRepoReportRequest) -> tuple[datetime, datetime]:
    # 把请求里的起止日期扩成当天完整时间区间
    return (
        datetime.combine(payload.start_date, time.min, tzinfo=timezone.utc),
        datetime.combine(payload.end_date, time.max, tzinfo=timezone.utc),
    )


def _normalize(value: datetime | None) -> datetime | None:
    # 统一时区：空值返回 None，无时区补 UTC
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _in_range(value: datetime | None, start_at: datetime, end_at: datetime) -> bool:
    # 判断时间是否落在区间内
    normalized = _normalize(value)
    return normalized is not None and start_at <= normalized <= end_at


def _repo_ids_from_payload(db: Session, payload: MultiRepoReportRequest) -> list[UUID]:
    # 从请求里收集要去查的仓库 ID：可能直接给 repo_ids，也可能给 workspace_id（取其下所有仓库）
    repo_ids = list(payload.repo_ids)
    if payload.workspace_id:
        # 如果给了工作区 ID，把该工作区绑定的仓库也加进来
        workspace = db.get(Workspace, payload.workspace_id)
        if workspace is None:
            raise HTTPException(status_code=404, detail="未找到 workspace")
        repo_ids.extend(UUID(str(item)) for item in (workspace.repo_ids or []))
    seen: set[UUID] = set()  # 用集合去重（同一个仓库别重复统计）
    output = []
    for repo_id in repo_ids:
        if repo_id not in seen:
            seen.add(repo_id)
            output.append(repo_id)
    return output


@router.post("", response_model=WorkspaceResponse)  # POST 新建工作区
async def create_workspace(
    payload: WorkspaceCreateRequest,  # 含 name、description、repo_ids
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("settings:manage")),  # 需要「设置管理」权限；user 是当前用户
) -> Workspace:
    # 校验请求里的 repo_ids 在数据库里都存在，不存在的报 404
    known_repo_ids = {repo.id for repo in db.query(Repository).filter(Repository.id.in_(payload.repo_ids)).all()} if payload.repo_ids else set()
    missing = [str(repo_id) for repo_id in payload.repo_ids if repo_id not in known_repo_ids]
    if missing:
        raise HTTPException(status_code=404, detail=f"仓库不存在：{', '.join(missing)}")
    workspace = Workspace(
        name=payload.name,
        description=payload.description,
        repo_ids=[str(repo_id) for repo_id in payload.repo_ids],  # 仓库 ID 以字符串列表存
        created_by=user.id,  # 记录创建人
    )
    db.add(workspace)  # 加入会话
    write_audit_log(db, user=user, action="workspace.create", request_json=payload.model_dump(mode="json"))  # 写审计日志
    db.commit()  # 提交
    db.refresh(workspace)  # 刷新拿 id
    return workspace


@router.get("", response_model=list[WorkspaceResponse])  # GET 列出所有工作区
async def list_workspaces(
    db: Session = Depends(get_db),
    _user: User = Depends(require_permission("repo:read")),  # 需 repo 读权限（下划线前缀表示本函数没用到这个变量）
) -> list[Workspace]:
    # 按更新时间倒序返回全部工作区
    return db.query(Workspace).order_by(Workspace.updated_at.desc()).all()


@router.post("/multi-repo-report", response_model=MultiRepoReportResponse)  # POST 多仓库合并周报
async def multi_repo_report(
    payload: MultiRepoReportRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("agent:run")),  # 需要「运行 Agent」权限
) -> dict:
    repo_ids = _repo_ids_from_payload(db, payload)  # 收集去重后的仓库 ID
    if not repo_ids:
        raise HTTPException(status_code=400, detail="至少选择一个仓库")  # 一个都没选就报错
    start_at, end_at = _bounds(payload)  # 时间区间
    repos = db.query(Repository).filter(Repository.id.in_(repo_ids)).all()  # 查出这些仓库对象
    summaries = []  # 每个仓库的明细汇总
    totals = {"open_issues": 0, "open_prs": 0, "merged_prs": 0, "failed_ci": 0, "repos": len(repos)}  # 合计
    for repo in repos:
        # 分别拉这个仓库的 issue/pr/ci，并筛出区间内的
        issues = db.query(Issue).filter(Issue.repo_id == repo.id).all()
        prs = db.query(PullRequest).filter(PullRequest.repo_id == repo.id).all()
        runs = db.query(WorkflowRun).filter(WorkflowRun.repo_id == repo.id).all()
        ranged_issues = [item for item in issues if any(_in_range(value, start_at, end_at) for value in [item.created_at, item.updated_at, item.closed_at])]
        ranged_prs = [item for item in prs if any(_in_range(value, start_at, end_at) for value in [item.created_at, item.updated_at, item.merged_at])]
        ranged_runs = [item for item in runs if any(_in_range(value, start_at, end_at) for value in [item.created_at, item.updated_at])]
        open_issues = len([item for item in issues if item.state == "open"])
        open_prs = len([item for item in prs if item.state == "open"])
        merged_prs = len([item for item in ranged_prs if item.merged_at])
        failed_ci = len([item for item in ranged_runs if item.conclusion == "failure"])
        # 把本仓库各项累加进总计
        totals["open_issues"] += open_issues
        totals["open_prs"] += open_prs
        totals["merged_prs"] += merged_prs
        totals["failed_ci"] += failed_ci
        # 风险分级：有失败 CI 或 open PR>5 为 high；open issue>5 为 medium；否则 low
        summaries.append(
            {
                "repo_id": str(repo.id),
                "repo_name": repo.full_name,
                "range_issues": len(ranged_issues),
                "range_prs": len(ranged_prs),
                "open_issues": open_issues,
                "open_prs": open_prs,
                "merged_prs": merged_prs,
                "failed_ci": failed_ci,
                "risk_level": "high" if failed_ci or open_prs > 5 else "medium" if open_issues > 5 else "low",
            }
        )
    risk_repos = [item for item in summaries if item["risk_level"] == "high"]  # 高风险仓库列表
    # 拼一份 markdown 报告
    lines = [
        f"# 多仓库研发周报 {payload.start_date} - {payload.end_date}",
        "",
        f"- 覆盖仓库：{len(repos)} 个",
        f"- Open Issues：{totals['open_issues']}",
        f"- Open PRs：{totals['open_prs']}",
        f"- 本周期 merged PRs：{totals['merged_prs']}",
        f"- 失败 CI：{totals['failed_ci']}",
        "",
        "## 仓库明细",
    ]
    for item in summaries:
        lines.append(
            f"- {item['repo_name']}：open issue {item['open_issues']}，open PR {item['open_prs']}，merged {item['merged_prs']}，失败 CI {item['failed_ci']}，风险 {item['risk_level']}"
        )
    lines.extend(["", "## 重点关注", *(f"- {item['repo_name']} 需要优先处理失败 CI 或 PR backlog。" for item in risk_repos)])
    if not risk_repos:
        lines.append("- 暂无高风险仓库，建议继续关注 PR 停留时间和评审积压。")
    report_markdown = "\n".join(lines) + "\n"  # 拼成完整 markdown 文本
    for repo in repos:
        # 把这份合并周报存进每个仓库的知识库（commit=False，最后统一提交）
        add_document(
            db,
            repo.id,
            "weekly_report",
            None,
            f"多仓库研发周报 {payload.start_date} - {payload.end_date}",
            report_markdown,
            {
                "display_title": f"多仓库研发周报 {payload.start_date} - {payload.end_date}",
                "start_date": str(payload.start_date),
                "end_date": str(payload.end_date),
                "workspace_id": str(payload.workspace_id) if payload.workspace_id else None,
                "repo_count": len(repos),
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
            commit=False,
        )
    # 写审计日志
    write_audit_log(db, user=user, action="workspace.multi_repo_report", request_json=payload.model_dump(mode="json"), result_json=totals)
    db.commit()  # 统一提交
    # 返回报告正文、合计指标、各仓库明细
    return {"report_markdown": report_markdown, "metrics": totals, "repo_summaries": summaries}
