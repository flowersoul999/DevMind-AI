"""DevMind AI module."""

# action_drafts.py —— 动作草稿（Action Draft，待确认的安全操作）路由层
#
# 业务域：安全门禁。当 Agent 想「写回 GitHub」（评论、建 Issue、打标签、关 Issue、
# 发报告）时，不直接执行，而是先生成一份「草稿」，等人工在界面上确认才真正执行。
# 这避免 AI 乱改线上仓库。本文件还提供审计日志查询。
#
# 暴露端点：
#   POST /api/action-drafts            —— 创建动作草稿
#   GET  /api/action-drafts            —— 列出草稿
#   GET  /api/action-drafts/audit-logs —— 列出审计日志
#   GET  /api/action-drafts/{draft_id} —— 查看草稿
#   POST /api/action-drafts/{draft_id}/cancel —— 取消草稿
#   POST /api/action-drafts/{draft_id}/confirm —— 确认执行草稿（真正调 GitHub）
#
# 依赖 service：app.services.github.client.GitHubClient（执行写回）、
#              app.services.permissions（权限校验 + 审计日志）
from datetime import datetime, timezone  # 时间
from uuid import UUID  # UUID

from fastapi import APIRouter, Depends, HTTPException, Query  # 路由、依赖、异常、查询参数
from sqlalchemy.orm import Session  # 数据库会话

from app.core.config import settings  # 全局配置（含加密密钥）
from app.core.security import decrypt_token  # 解密仓库的 GitHub token
from app.db.models import ActionDraft, AuditLog, Issue, PullRequest, Repository, User  # 表模型
from app.db.session import get_db  # 会话依赖
from app.schemas.action_drafts import ActionDraftConfirmResponse, ActionDraftCreateRequest, ActionDraftResponse, AuditLogResponse  # 模型
from app.services.github.client import GitHubClient  # 真正调 GitHub API 的客户端
from app.services.permissions import require_permission, write_audit_log  # 权限 + 审计

router = APIRouter()  # 路由器


def _target_number(db: Session, draft: ActionDraft) -> int | None:
    # 从草稿里找出「目标 Issue/PR 的编号（#号后面的数字）」，用于评论/关闭等写回
    meta = draft.meta or {}
    raw = meta.get("target_number") or meta.get("issue_number") or meta.get("pr_number")  # 优先读 meta
    if raw is not None:
        try:
            return int(raw)  # 能转成整数就用它
        except (TypeError, ValueError):
            return None
    if draft.target_id and draft.target_type == "issue":
        # 没有编号但有关联 issue ID，就去查这个 issue 的 number
        issue = db.get(Issue, draft.target_id)
        return issue.number if issue else None
    if draft.target_id and draft.target_type in {"pull_request", "pr"}:
        pr = db.get(PullRequest, draft.target_id)
        return pr.number if pr else None
    return None


async def _execute_github_draft(db: Session, draft: ActionDraft, repo: Repository) -> dict:
    # 真正把草稿执行到 GitHub 上；按草稿类型走不同分支
    token = decrypt_token(repo.github_token_encrypted, settings.token_encryption_key) if repo.github_token_encrypted else None  # 解密 token
    client = GitHubClient(token=token, base_url=repo.api_base_url)  # 构造 GitHub 客户端
    draft_type = draft.draft_type.lower()  # 草稿类型小写化便于比较
    issue_number = _target_number(db, draft)  # 目标编号
    if draft_type in {"issue_comment", "pr_comment", "comment"}:
        # 评论类：缺编号无法评论
        if issue_number is None:
            raise RuntimeError("评论草稿缺少 issue/pr number")
        return await client.create_issue_comment(repo.owner, repo.name, issue_number, draft.content)  # 调 GitHub 发评论
    if draft_type == "create_issue":
        # 新建 Issue：标题取草稿标题，没有就用正文首行
        title = draft.title or (draft.content.splitlines()[0][:120] if draft.content else "DevMind AI Issue")
        return await client.create_issue(repo.owner, repo.name, title, draft.content)
    if draft_type == "close_issue":
        # 关 Issue：缺编号报错
        if issue_number is None:
            raise RuntimeError("关闭 Issue 草稿缺少 issue number")
        return await client.close_issue(repo.owner, repo.name, issue_number)
    if draft_type in {"label", "add_label", "add_labels"}:
        # 打标签：编号必须存在，标签从 meta 或标题取
        if issue_number is None:
            raise RuntimeError("Label 草稿缺少 issue/pr number")
        labels = draft.meta.get("labels") if draft.meta else None
        if not isinstance(labels, list) or not labels:
            labels = [draft.title] if draft.title else []  # 没配标签就退用标题当标签
        if not labels:
            raise RuntimeError("Label 草稿缺少 labels")
        return await client.add_issue_labels(repo.owner, repo.name, issue_number, [str(item) for item in labels])
    if draft_type == "send_report":
        # 发报告：目前没接外部渠道，只标记已记录
        return {"status": "recorded", "message": "报告草稿已在 DevFlow AI 中标记为已发送，未接入外部消息渠道。"}
    raise RuntimeError(f"暂不支持执行草稿类型：{draft.draft_type}")  # 其它类型不支持


@router.post("", response_model=ActionDraftResponse)  # POST 创建草稿
async def create_action_draft(
    payload: ActionDraftCreateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("draft:create")),  # 需 draft:create 权限
) -> ActionDraft:
    repo = db.get(Repository, payload.repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="未找到仓库")
    draft = ActionDraft(
        repo_id=payload.repo_id,
        draft_type=payload.draft_type,
        target_type=payload.target_type,
        target_id=payload.target_id,
        title=payload.title,
        content=payload.content,
        risk_level=payload.risk_level,  # 风险等级（high/medium/low）
        created_by=user.id,  # 谁创建的
        meta=payload.metadata,  # 额外信息（编号、标签等）
    )
    db.add(draft)  # 加入会话
    write_audit_log(  # 记一条审计：谁、在哪仓库、创建了什么草稿
        db,
        user=user,
        repo_id=payload.repo_id,
        action="action_draft.create",
        target_type=payload.target_type,
        target_id=str(payload.target_id) if payload.target_id else None,
        request_json=payload.model_dump(mode="json"),
    )
    db.commit()
    db.refresh(draft)
    return draft


@router.get("", response_model=list[ActionDraftResponse])  # GET 列出草稿
async def list_action_drafts(
    repo_id: UUID | None = Query(default=None),  # 可选按仓库过滤
    status: str | None = Query(default=None),  # 可选按状态（pending_confirmation/executed...）
    db: Session = Depends(get_db),
    _user: User = Depends(require_permission("repo:read")),  # 需读权限
) -> list[ActionDraft]:
    query = db.query(ActionDraft)
    if repo_id:
        query = query.filter(ActionDraft.repo_id == repo_id)
    if status:
        query = query.filter(ActionDraft.status == status)
    return query.order_by(ActionDraft.created_at.desc()).limit(100).all()  # 最多 100 条


@router.get("/audit-logs", response_model=list[AuditLogResponse])  # GET 审计日志
async def list_audit_logs(
    repo_id: UUID | None = Query(default=None),
    db: Session = Depends(get_db),
    _user: User = Depends(require_permission("settings:manage")),  # 需管理权限（审计较敏感）
) -> list[AuditLog]:
    query = db.query(AuditLog)
    if repo_id:
        query = query.filter(AuditLog.repo_id == repo_id)
    return query.order_by(AuditLog.created_at.desc()).limit(100).all()


@router.get("/{draft_id}", response_model=ActionDraftResponse)  # GET 查看单个草稿
async def get_action_draft(
    draft_id: UUID,
    db: Session = Depends(get_db),
    _user: User = Depends(require_permission("repo:read")),
) -> ActionDraft:
    draft = db.get(ActionDraft, draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="未找到安全草稿")
    return draft


@router.post("/{draft_id}/cancel", response_model=ActionDraftResponse)  # POST 取消草稿
async def cancel_action_draft(
    draft_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("draft:create")),  # 需创建权限
) -> ActionDraft:
    draft = db.get(ActionDraft, draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="未找到安全草稿")
    if draft.status != "pending_confirmation":
        # 只有「待确认」的草稿才能取消，已执行/已失败的不能
        raise HTTPException(status_code=400, detail="只有待确认草稿可以取消")
    draft.status = "cancelled"  # 改成已取消
    write_audit_log(db, user=user, repo_id=draft.repo_id, action="action_draft.cancel", target_type=draft.target_type, target_id=str(draft.id))
    db.commit()
    db.refresh(draft)
    return draft


@router.post("/{draft_id}/confirm", response_model=ActionDraftConfirmResponse)  # POST 确认执行草稿
async def confirm_action_draft(
    draft_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("draft:approve")),  # 需「批准」权限（比创建更高）
) -> dict:
    draft = db.get(ActionDraft, draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="未找到安全草稿")
    if draft.status != "pending_confirmation":
        raise HTTPException(status_code=400, detail="只有待确认草稿可以执行")
    repo = db.get(Repository, draft.repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="未找到仓库")
    try:
        # 真正执行写回 GitHub
        result = await _execute_github_draft(db, draft, repo)
        draft.status = "executed"  # 标记已执行
        draft.confirmed_by = user.id  # 谁确认的
        draft.executed_at = datetime.now(timezone.utc)  # 执行时间
        draft.execution_result = result  # 存 GitHub 返回
        audit = write_audit_log(  # 记审计（成功）
            db,
            user=user,
            repo_id=draft.repo_id,
            action=f"action_draft.confirm.{draft.draft_type}",
            target_type=draft.target_type,
            target_id=str(draft.target_id or draft.id),
            status="success",
            request_json={"draft_id": str(draft.id), "draft_type": draft.draft_type},
            result_json=result if isinstance(result, dict) else {"result": result},
        )
    except Exception as exc:
        # 执行失败也要记录，方便排查
        draft.status = "failed"
        draft.confirmed_by = user.id
        draft.executed_at = datetime.now(timezone.utc)
        draft.error_message = str(exc)  # 存错误信息
        audit = write_audit_log(
            db,
            user=user,
            repo_id=draft.repo_id,
            action=f"action_draft.confirm.{draft.draft_type}",
            target_type=draft.target_type,
            target_id=str(draft.target_id or draft.id),
            status="failed",
            request_json={"draft_id": str(draft.id), "draft_type": draft.draft_type},
            result_json={"error": str(exc)},
        )
    db.commit()
    db.refresh(draft)
    db.refresh(audit)
    # 返回草稿本身 + 审计记录 ID
    return {"draft": draft, "audit_id": audit.id}
