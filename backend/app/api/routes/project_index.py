"""DevMind AI module."""

# project_index.py —— 项目记忆索引（Project Index / Memory Index）路由层
#
# 业务域：项目级记忆索引。DevFlow 会把仓库的代码、文档整理成「记忆索引」，
# 让 Agent 能快速回忆项目背景。本文件提供：查看索引状态、触发重新扫描、
# 以及「暂时不扫描」（snooze 打盹）三个操作。
#
# 暴露端点：
#   GET    /api/{repo_id}/memory-index/state     —— 查看索引状态（是否已建好/建索引中）
#   POST   /api/{repo_id}/memory-index/scan     —— 触发后台重新扫描（202 已接受）
#   POST   /api/{repo_id}/memory-index/snooze   —— 暂停自动索引一段时间（天数）
#
# 依赖 service：app.services.project_indexing（索引的建/扫/暂停逻辑）
import uuid  # 处理 UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException  # 路由、后台任务、依赖注入、异常
from sqlalchemy.orm import Session  # 数据库会话

from app.db.models import Repository  # 仓库表
from app.db.session import get_db  # 数据库会话依赖
from app.schemas.project_index import (  # 本项目用到的请求/响应模型
    ProjectIndexScanResponse,
    ProjectIndexSnoozeRequest,
    ProjectIndexSnoozeResponse,
    ProjectIndexStateResponse,
)
from app.services.project_indexing import (  # 索引相关 service 函数
    get_project_index_state,
    mark_project_index_building,
    scan_project_index_by_id,
    snooze_project_index,
)

router = APIRouter()  # 路由器


def _repo_or_404(db: Session, repo_id: uuid.UUID) -> Repository:
    # 复用工具：确认仓库存在，不存在返回 404
    repo = db.get(Repository, repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="未找到仓库")
    return repo


@router.get("/{repo_id}/memory-index/state", response_model=ProjectIndexStateResponse)  # GET 查索引状态
async def get_memory_index_state(repo_id: uuid.UUID, db: Session = Depends(get_db)) -> dict:
    repo = _repo_or_404(db, repo_id)  # 确认仓库
    # 调 service 拿索引状态（比如：是否已建好、上次建的时间），直接返回字典
    return get_project_index_state(db, repo)


@router.post("/{repo_id}/memory-index/scan", response_model=ProjectIndexScanResponse, status_code=202)  # POST 触发扫描，202=已接受（后台跑）
async def start_memory_index_scan(
    repo_id: uuid.UUID,
    background_tasks: BackgroundTasks,  # FastAPI 后台任务：让耗时扫描不阻塞当前 HTTP 响应
    db: Session = Depends(get_db),
) -> ProjectIndexScanResponse:
    repo = _repo_or_404(db, repo_id)  # 确认仓库
    mark_project_index_building(db, repo)  # 先把状态标成「building 建索引中」，避免重复触发
    background_tasks.add_task(scan_project_index_by_id, repo.id)  # 把真正耗时的扫描丢进后台线程/进程
    # 立刻返回「已开始」的响应，扫描继续在后台跑
    return ProjectIndexScanResponse(repo_id=repo.id, started=True, status="building")


@router.post("/{repo_id}/memory-index/snooze", response_model=ProjectIndexSnoozeResponse)  # POST 暂停自动索引
async def snooze_memory_index(
    repo_id: uuid.UUID,
    payload: ProjectIndexSnoozeRequest | None = None,  # 可选：要暂停多少天（默认 7）
    db: Session = Depends(get_db),
) -> ProjectIndexSnoozeResponse:
    repo = _repo_or_404(db, repo_id)  # 确认仓库
    # 调 service 把索引「打盹」；没传天数就默认 7 天（类比：告诉系统「这几天别烦我」）
    row = snooze_project_index(db, repo, days=(payload.days if payload else 7))
    # 返回暂停到何时（snoozed_until）
    return ProjectIndexSnoozeResponse(repo_id=repo.id, snoozed_until=row.snoozed_until)
