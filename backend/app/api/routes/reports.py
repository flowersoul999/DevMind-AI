"""DevMind AI module."""

# reports.py —— 研发报表（周报 / 仓库健康度）路由层
#
# 业务域：把仓库的 Issue/PR/CI 数据汇总成「研发周报」和「仓库健康度」评估。
#
# 暴露端点：
#   POST /api/reports/weekly   —— 按时间范围生成研发周报（并用 RAG 存成知识文档）
#   POST /api/reports/repo-health —— 快速给出仓库健康度指标（demo 估算）
#
# 依赖 service：app.services.agents.report_agent.ReportAgent（写周报的 LLM Agent）、
#              app.services.rag.vector_store.add_document（把周报存进知识库）
from datetime import date, datetime, time, timezone  # 日期时间处理

from fastapi import APIRouter, Depends, HTTPException  # 路由、依赖、异常
from sqlalchemy.orm import Session  # 数据库会话

from app.db.models import Issue, PullRequest, Repository, WorkflowRun  # 用到的表模型
from app.db.session import get_db  # 会话依赖
from app.schemas.reports import WeeklyReportRequest, WeeklyReportResponse  # 请求/响应模型
from app.services.agents.report_agent import ReportAgent  # 生成周报的 Agent
from app.services.rag.vector_store import add_document  # 把生成的周报写进向量知识库

router = APIRouter()  # 路由器


def _range_bounds(start_date: date, end_date: date) -> tuple[datetime, datetime]:
    # 把「日期」扩成「当天 00:00 ~ 23:59」的时间区间，方便和带时间的记录比较
    return (
        datetime.combine(start_date, time.min, tzinfo=timezone.utc),  # 起点：当天最早一刻（UTC 时区）
        datetime.combine(end_date, time.max, tzinfo=timezone.utc),  # 终点：当天最晚一刻
    )


def _normalize_dt(value: datetime | None) -> datetime | None:
    # 统一时区：没带时区的时间补上 UTC，便于后面比较大小
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)  # 无时区就当 UTC
    return value  # 已有则原样返回


def _in_range(value: datetime | None, start_at: datetime, end_at: datetime) -> bool:
    # 判断某个时间点是否落在 [start_at, end_at] 区间内
    normalized = _normalize_dt(value)
    return normalized is not None and start_at <= normalized <= end_at


def _issue_in_range(issue: Issue, start_at: datetime, end_at: datetime) -> bool:
    # Issue 的创建/更新/关闭时间，任意一个在区间内就算「这段时间相关的 Issue」
    return any(_in_range(value, start_at, end_at) for value in [issue.created_at, issue.updated_at, issue.closed_at])


def _pr_in_range(pr: PullRequest, start_at: datetime, end_at: datetime) -> bool:
    # PR 的创建/更新/合并时间，任意一个在区间内就算命中
    return any(_in_range(value, start_at, end_at) for value in [pr.created_at, pr.updated_at, pr.merged_at])


@router.post("/weekly", response_model=WeeklyReportResponse)  # POST 生成周报
async def weekly_report(payload: WeeklyReportRequest, db: Session = Depends(get_db)) -> dict:
    # payload 含 repo_id 和起止日期；db 由依赖注入提供
    repo = db.get(Repository, payload.repo_id)  # 查仓库
    if repo is None:
        raise HTTPException(status_code=404, detail="未找到仓库")  # 没有就 404
    start_at, end_at = _range_bounds(payload.start_date, payload.end_date)  # 算出时间区间
    # 拉出该仓库全部 Issue，用列表推导留下区间内相关的
    issues = [
        item
        for item in db.query(Issue).filter(Issue.repo_id == payload.repo_id).all()
        if _issue_in_range(item, start_at, end_at)
    ]
    # 同理筛出区间内的 PR
    prs = [
        item
        for item in db.query(PullRequest).filter(PullRequest.repo_id == payload.repo_id).all()
        if _pr_in_range(item, start_at, end_at)
    ]
    # 同理筛出区间内的 CI 运行（按创建/更新时间）
    runs = [
        item
        for item in db.query(WorkflowRun).filter(WorkflowRun.repo_id == payload.repo_id).all()
        if _in_range(item.created_at, start_at, end_at) or _in_range(item.updated_at, start_at, end_at)
    ]
    merged_prs = [item for item in prs if item.merged_at]  # 已合并的 PR
    open_prs = [item for item in prs if item.state == "open"]  # 仍打开的 PR
    failed_ci = [item for item in runs if item.conclusion == "failure"]  # 失败了的 CI
    # 调 ReportAgent 生成周报；把整理好的数据喂给它
    result = await ReportAgent().run(
        {
            "issues": [{"number": item.number, "title": item.title, "state": item.state, "closed_at": item.closed_at} for item in issues],
            "pull_requests": [
                {"number": item.number, "title": item.title, "state": item.state, "merged_at": item.merged_at}
                for item in prs
            ],
            "workflow_runs": [{"name": item.name, "conclusion": item.conclusion} for item in runs],
            "metrics": {
                "issues": len(issues),  # 区间内 Issue 数
                "pull_requests": len(prs),  # PR 数
                "merged_prs": len(merged_prs),  # 合并数
                "open_prs": len(open_prs),  # 打开数
                "failed_ci": len(failed_ci),  # 失败 CI 数
            },
        },
        {
            "repo_id": str(payload.repo_id),
            "repo_name": repo.full_name,
            "start_date": str(payload.start_date),
            "end_date": str(payload.end_date),
        },
    )
    # 把生成的周报 markdown 存进知识库（source_type="weekly_report"），方便以后检索
    add_document(
        db,
        payload.repo_id,
        "weekly_report",  # 文档类型
        None,  # source_id 为 None（整份周报是一条）
        f"研发周报 {payload.start_date} - {payload.end_date}",  # 标题
        result["report_markdown"],  # 周报正文（markdown）
        {
            "display_title": f"研发周报 {payload.start_date} - {payload.end_date}",
            "start_date": str(payload.start_date),
            "end_date": str(payload.end_date),
            "repo_name": repo.full_name,
            "generated_at": datetime.now(timezone.utc).isoformat(),  # 生成时间
        },
    )
    return result  # 返回 Agent 结果（含周报 markdown 等）


@router.post("/repo-health")  # POST 仓库健康度（无固定响应模型，返回字典）
async def repo_health(payload: WeeklyReportRequest, db: Session = Depends(get_db)) -> dict:
    # 注意：这里复用 WeeklyReportRequest 只是为了拿 repo_id 和日期，本质是「快速体检」
    open_issues = db.query(Issue).filter(Issue.repo_id == payload.repo_id, Issue.state == "open").count()  # 打开的 Issue 数
    open_prs = db.query(PullRequest).filter(PullRequest.repo_id == payload.repo_id, PullRequest.state == "open").count()  # 打开的 PR 数
    failed_ci = db.query(WorkflowRun).filter(WorkflowRun.repo_id == payload.repo_id, WorkflowRun.conclusion == "failure").count()  # 失败 CI 数
    return {
        "summary": "仓库健康度以 demo 指标估算，建议结合近期失败 CI 和高风险 PR 判断。",
        "metrics": {
            "open_issues": open_issues,
            "open_prs": open_prs,
            "failed_ci_runs": failed_ci,
            "merged_prs_7d": 0,  # 占位：7 天合并数暂未实现
        },
        # 风险提示：有失败 CI 就提示要排查
        "risks": ["存在失败 CI，需要排查"] if failed_ci else [],
    }
