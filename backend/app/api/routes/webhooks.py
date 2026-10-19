"""DevMind AI module."""

# webhooks.py —— GitHub Webhook（外部事件回调）路由层
#
# 业务域：与 GitHub 的实时联动。当仓库发生 issue/PR/CI 等事件时，GitHub 会主动向
# 本服务「打一拳」（POST 一个 JSON），这就是 webhook。本文件负责：
#   1) 校验这拳确实来自 GitHub（用 HMAC 签名防伪造）
#   2) 根据事件类型，把「重新同步该仓库」丢进后台任务
#
# 暴露端点：
#   POST /api/webhooks/github —— 接收 GitHub 推送的事件（相对 router 前缀）
#
# 依赖 service：app.services.repos_sync.sync_repository_by_id（按仓库 ID 重新同步）
import hashlib  # 标准库：哈希算法（用来校验签名）
import hmac  # 标准库：HMAC 签名校验（确认请求是 GitHub 发的）

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request  # 路由、后台任务、请求头、异常、原始请求
from sqlalchemy.orm import Session  # 数据库会话

from app.core.config import settings  # 全局配置（含 webhook 密钥）
from app.db.models import Repository  # 仓库表
from app.db.session import SessionLocal  # 独立开一个数据库会话（webhook 不走 Depends 的会话）
from app.schemas.repos import RepoSyncRequest  # 同步请求模型
from app.services.repos_sync import sync_repository_by_id  # 按仓库 ID 同步数据

router = APIRouter()  # 路由器


def verify_signature(body: bytes, signature: str | None) -> None:
    # 校验 GitHub 推送的签名，确认请求不是别人伪造的（类比：查快递员工牌）
    if not settings.github_webhook_secret:
        return  # 没配密钥就不校验（开发/演示环境常见）
    if not signature or not signature.startswith("sha256="):
        # GitHub 的签名必须以 "sha256=" 开头，没有就拒绝
        raise HTTPException(status_code=401, detail="缺少 GitHub webhook 签名")
    # 用密钥对请求体做 HMAC-SHA256，得到「我们算出的正确签名」
    expected = hmac.new(settings.github_webhook_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    actual = signature.removeprefix("sha256=")  # 去掉前端 "sha256=" 前缀，拿到 GitHub 给的签名
    if not hmac.compare_digest(expected, actual):
        # 用安全的字符串比较函数比对；不一致说明签名对不上，拒绝
        raise HTTPException(status_code=401, detail="GitHub webhook 签名无效")


def find_repo_by_full_name(full_name: str) -> Repository | None:
    # 按 "owner/repo" 这种全名在数据库里找仓库（GitHub 事件里给的是全名）
    db: Session = SessionLocal()  # 自己开一个会话（注意：这里没用 Depends，因为是内部调用）
    try:
        # 查到就返回，查不到返回 None
        return db.query(Repository).filter(Repository.full_name == full_name).one_or_none()
    finally:
        db.close()  # 不管成功失败，最后都要关闭会话，避免连接泄漏


@router.post("/github")  # POST /api/webhooks/github
async def github_webhook(
    request: Request,  # 原始请求对象（需要读原始 body 来校验签名）
    background_tasks: BackgroundTasks,  # 后台任务：同步数据放后台
    x_github_event: str | None = Header(default=None),  # 请求头里的事件类型（issue/pr/workflow_run...）
    x_hub_signature_256: str | None = Header(default=None),  # 请求头里的 HMAC 签名
) -> dict:
    body = await request.body()  # 读原始请求体字节（校验签名要用原始内容）
    verify_signature(body, x_hub_signature_256)  # 先验签名，不对直接 401
    payload = await request.json()  # 把 JSON 解析成字典
    repository = payload.get("repository") or {}  # GitHub 事件体里的仓库信息
    full_name = repository.get("full_name")  # 例如 "octocat/Hello-World"
    if not full_name:
        # 没带仓库名，没法处理，礼貌返回「不接收」
        return {"accepted": False, "reason": "payload.repository.full_name is missing"}

    repo = find_repo_by_full_name(full_name)  # 在本地库找这个仓库
    if repo is None:
        # 本地没连过这个仓库，忽略事件
        return {"accepted": False, "reason": f"{full_name} is not connected"}

    if x_github_event in {"issues", "pull_request", "workflow_run", "push", "check_suite", "check_run"}:
        # 这些是「数据可能变了」的事件，需要重新同步
        background_tasks.add_task(sync_repository_by_id, repo.id, RepoSyncRequest(limit=settings.auto_sync_limit))
        # 返回「已接受，已安排同步」
        return {"accepted": True, "event": x_github_event, "repo_id": str(repo.id), "action": "sync_scheduled"}

    # 其它事件（比如 star）不关心，只记录收到了
    return {"accepted": True, "event": x_github_event, "repo_id": str(repo.id), "action": "ignored"}
