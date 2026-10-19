"""
services/scheduler.py —— 周期性自动同步仓库的后台循环
============================================================================

项目需要「仓库数据保持新鲜」：GitHub 上的 Issue/PR/CI 一直在变，如果只在用户
手动点「同步」时才更新，分析就会用到旧数据。所以这个文件提供一个「后台常驻循环」：
每隔一段时间，自动把所有已连接的 GitHub 仓库重新同步一遍。

注意：这只是「调度器」（决定什么时候跑、跑哪些仓库），真正的同步逻辑在
repos_sync.py 的 sync_repository_by_id 里。这里只负责「定时触发」。

为什么用无限循环 + sleep 而不是系统 cron？因为本项目是单进程演示，内置一个
异步循环最简单，不依赖操作系统定时任务。开关由 settings.auto_sync_enabled 控制。."""

import asyncio  # 异步睡眠与协程
import logging  # 打日志

from app.core.config import settings  # 全局配置：是否开启自动同步、间隔、每批数量
from app.db.models import Repository  # 仓库表
from app.db.session import SessionLocal  # 创建数据库会话的工厂
from app.schemas.repos import RepoSyncRequest  # 同步请求的数据结构
from app.services.repos_sync import sync_repository_by_id  # 真正执行同步的函数


logger = logging.getLogger(__name__)  # 取本模块的 logger，__name__ 就是 "app.services.scheduler"


async def periodic_repo_sync_loop() -> None:
    # 整个后台循环的入口，被 main.py 启动时调用（作为异步任务常驻）
    await asyncio.sleep(5)  # 启动后先歇 5 秒，等数据库/其他初始化就绪再开始，避免抢跑
    while settings.auto_sync_enabled:  # 只要配置允许自动同步，就一直转
        # 为什么开一个短命 db 会话：只是先查「有哪些仓库要同步」，用完立刻关，不占连接
        db = SessionLocal()
        try:
            # 只挑 GitHub 类（含兼容 GitHub API 的）仓库；provider 是仓库来源类型
            repos = db.query(Repository).filter(Repository.provider.in_(["github", "github_compatible"])).all()
            repo_ids = [repo.id for repo in repos]  # 抽出 id 列表，循环外再用
        finally:
            db.close()  # 无论成功失败都关闭会话

        for repo_id in repo_ids:
            try:
                # 逐个仓库触发同步；limit 控制每次最多拉多少条记录，来自配置
                await sync_repository_by_id(repo_id, RepoSyncRequest(limit=settings.auto_sync_limit))
            except Exception as exc:
                # 为什么这里只 warning 不中断：单个仓库同步失败不应拖垮整个循环，记日志后继续下一个
                logger.warning("Periodic repo sync failed for %s: %s", repo_id, exc)

        # 一轮同步完，睡到下一次；max(..., 30) 保证间隔至少 30 秒，避免配置写 0 导致狂转
        await asyncio.sleep(max(settings.auto_sync_interval_seconds, 30))
