"""DevMind AI module."""

# feedback.py —— 对话反馈（Chat Feedback）与执行轨迹路由层
#
# 业务域：用户对 Agent 回答的「点赞/点踩」反馈，以及排查一次回答是怎么产生的。
# 点踩（unhelpful）会触发通知，方便人工跟进；还能查看某次执行的完整 trace。
#
# 暴露端点：
#   GET  /api/feedback/metrics      —— 反馈统计指标（覆盖率、好评率等）
#   GET  /api/feedback/trace/{run_id} —— 查看某次 Agent 执行的完整轨迹
#   GET  /api/feedback              —— 列出反馈记录
#   POST /api/feedback              —— 提交一条反馈（赞/踩）
#   PATCH /api/feedback/{feedback_id} —— 审核一条反馈（标记已处理/忽略）
#
# 依赖 service：app.services.feedback_notifications（点踩通知）、app.services.permissions.ensure_demo_user（演示用户）
import uuid  # UUID 处理
from datetime import datetime, timezone  # 时间
from typing import Any  # 任意类型标注

from fastapi import APIRouter, Depends, HTTPException, Query  # 路由、依赖、异常、查询参数
from sqlalchemy.orm import Session  # 数据库会话

from app.db.models import AgentRun, ChatFeedback, ChatMessage, ChatSession, Conversation, Repository  # 表模型
from app.db.session import get_db  # 会话依赖
from app.schemas.feedback import ChatFeedbackCreate, ChatFeedbackResponse, ChatFeedbackReview  # 请求/响应模型
from app.services.feedback_notifications import send_negative_feedback_notification  # 点踩通知
from app.services.permissions import ensure_demo_user  # 拿到当前演示用户

router = APIRouter()  # 路由器


def _run_id_from_message(message: ChatMessage) -> uuid.UUID | None:
    # 从一条消息的元数据里取它对应的 AgentRun（执行记录）ID
    raw_run_id = (message.meta or {}).get("run_id")
    if not raw_run_id:
        return None  # 没有就返回 None
    try:
        return uuid.UUID(str(raw_run_id))  # 尝试转成 UUID，非法就进入 except
    except (TypeError, ValueError):
        return None


def _message_or_404(db: Session, payload: ChatFeedbackCreate) -> ChatMessage:
    # 根据反馈请求里的「助手消息 ID」，找到那条助手回答；找不到或不是 assistant 就 404
    message = (
        db.query(ChatMessage)
        .filter(
            ChatMessage.id == payload.assistant_message_id,  # 消息 ID 要匹配
            ChatMessage.repo_id == payload.repo_id,  # 同仓库
            ChatMessage.conversation_id == payload.conversation_id,  # 同会话
            ChatMessage.role == "assistant",  # 必须是助手发的
        )
        .one_or_none()
    )
    if message is None:
        raise HTTPException(status_code=404, detail="未找到对应的 Agent 回答")
    return message


def _serialize_message(message: ChatMessage | None) -> dict[str, Any] | None:
    # 把一条消息整理成对外可返回的字典（去掉内部字段）
    if message is None:
        return None
    return {
        "id": str(message.id),
        "role": message.role,
        "content": message.content,
        "route": message.route,
        "tool_calls": message.tool_calls or [],  # 调用过的工具
        "metadata": message.meta or {},  # 元数据
        "created_at": message.created_at.isoformat() if message.created_at else None,
    }


@router.get("/metrics")  # GET 反馈统计
async def feedback_metrics(
    repo_id: uuid.UUID = Query(...),  # Query(...) 表示必填查询参数（?repo_id=xxx）
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    rows = db.query(ChatFeedback).filter(ChatFeedback.repo_id == repo_id).all()  # 该仓库全部反馈
    assistant_messages = db.query(ChatMessage).filter(ChatMessage.repo_id == repo_id, ChatMessage.role == "assistant").count()  # 助手消息总数
    helpful = sum(row.rating == "helpful" for row in rows)  # 赞的数量
    unhelpful = sum(row.rating == "unhelpful" for row in rows)  # 踩的数量
    rated = len(rows)  # 已评价总数
    reasons: dict[str, int] = {}  # 踩的原因统计
    for row in rows:
        if row.reason:
            reasons[row.reason] = reasons.get(row.reason, 0) + 1
    return {
        "repo_id": str(repo_id),
        "assistant_messages": assistant_messages,
        "rated_messages": rated,
        # 反馈覆盖率 = 已评价 / 助手消息数
        "feedback_coverage": round(rated / assistant_messages, 4) if assistant_messages else 0.0,
        "helpful": helpful,
        "unhelpful": unhelpful,
        "helpful_rate": round(helpful / rated, 4) if rated else 0.0,  # 好评率
        "unhelpful_rate": round(unhelpful / rated, 4) if rated else 0.0,  # 差评率
        # 差评占所有助手消息的比例（更能反映「问题有多普遍」）
        "negative_feedback_rate": round(unhelpful / assistant_messages, 4) if assistant_messages else 0.0,
        # 还没处理完的踩（open / in_review）
        "open_reviews": sum(row.rating == "unhelpful" and row.review_status in {"open", "in_review"} for row in rows),
        "reason_counts": reasons,
    }


@router.get("/trace/{run_id}")  # GET 某次执行的完整轨迹
async def feedback_trace(run_id: uuid.UUID, db: Session = Depends(get_db)) -> dict[str, Any]:
    run = db.get(AgentRun, run_id)  # 执行记录
    feedback = db.query(ChatFeedback).filter(ChatFeedback.run_id == run_id).one_or_none()  # 对应反馈
    if run is None and feedback is None:
        # 执行和反馈都没有，说明这个 run_id 无效
        raise HTTPException(status_code=404, detail="未找到对应的执行轨迹")

    # 找这次执行对应的「助手消息」
    assistant_message = db.get(ChatMessage, feedback.assistant_message_id) if feedback else None
    if assistant_message is None and run is not None:
        # 没有直接关联时，从会话里按 run_id 反查助手消息
        candidates = (
            db.query(ChatMessage)
            .filter(ChatMessage.conversation_id == run.conversation_id, ChatMessage.role == "assistant")
            .order_by(ChatMessage.created_at.desc())
            .all()
        )
        assistant_message = next((item for item in candidates if _run_id_from_message(item) == run_id), None)

    # 通过会话去还原这次对话的用户问题 / 助手回答 / 执行细节
    session = (
        db.query(ChatSession).filter(ChatSession.assistant_message_id == assistant_message.id).one_or_none()
        if assistant_message
        else None
    )
    user_message = db.get(ChatMessage, session.user_message_id) if session and session.user_message_id else None
    repo_id = feedback.repo_id if feedback else run.repo_id if run else None  # 优先从反馈取仓库
    conversation_id = feedback.conversation_id if feedback else run.conversation_id if run else None
    repo = db.get(Repository, repo_id) if repo_id else None
    conversation = db.get(Conversation, conversation_id) if conversation_id else None
    transcript = session.transcript_json if session else {}  # 会话内部记录的事件

    return {
        "trace_id": str(run_id),
        "repository": {"id": str(repo.id), "full_name": repo.full_name} if repo else None,
        "conversation": {"id": str(conversation.id), "title": conversation.title} if conversation else None,
        "feedback": ChatFeedbackResponse.model_validate(feedback).model_dump(mode="json") if feedback else None,
        "run": {
            "id": str(run.id),
            "status": run.status,
            "route": run.route,
            "user_message": run.user_message,
            "final_answer": run.final_answer,
            "tool_calls": run.tool_calls or [],
            "created_at": run.created_at.isoformat() if run.created_at else None,
        }
        if run
        else None,
        "messages": {
            "user": _serialize_message(user_message),  # 用户原话
            "assistant": _serialize_message(assistant_message),  # 助手回答
        },
        "session": {
            "id": str(session.id),
            "status": session.status,
            "digest": session.digest,
            "agent_events": transcript.get("agent_events", []),  # Agent 内部事件
            "tool_use_results": transcript.get("tool_use_results", []),  # 工具调用结果
            "memory_update": transcript.get("memory_update", {}),  # 记忆更新
            "sealed_at": session.sealed_at.isoformat() if session.sealed_at else None,
        }
        if session
        else None,
    }


@router.get("", response_model=list[ChatFeedbackResponse])  # GET 反馈列表
async def list_feedback(
    repo_id: uuid.UUID = Query(...),  # 必填仓库参数
    conversation_id: uuid.UUID | None = Query(default=None),  # 可选按会话过滤
    limit: int = Query(default=200, ge=1, le=500),  # 每页最多 200，范围 1~500
    db: Session = Depends(get_db),
) -> list[ChatFeedback]:
    query = db.query(ChatFeedback).filter(ChatFeedback.repo_id == repo_id)  # 该仓库反馈
    if conversation_id:
        query = query.filter(ChatFeedback.conversation_id == conversation_id)  # 再按会话过滤
    return query.order_by(ChatFeedback.created_at.desc()).limit(limit).all()  # 倒序取前 limit 条


@router.post("", response_model=ChatFeedbackResponse)  # POST 提交反馈
async def create_feedback(payload: ChatFeedbackCreate, db: Session = Depends(get_db)) -> ChatFeedback:
    repo = db.get(Repository, payload.repo_id)
    conversation = db.get(Conversation, payload.conversation_id)
    if repo is None or conversation is None or conversation.repo_id != repo.id:
        # 仓库或会话不存在、或会话不属于该仓库，都拒绝
        raise HTTPException(status_code=404, detail="未找到对应的项目会话")
    message = _message_or_404(db, payload)  # 找到那条被评价的助手消息
    run_id = _run_id_from_message(message)  # 取它对应的执行 ID
    run = db.get(AgentRun, run_id) if run_id else None
    reporter = ensure_demo_user(db)  # 当前用户（演示模式下一个默认用户）

    # 同一条助手消息可能已有反馈，查一下避免重复创建
    feedback = db.query(ChatFeedback).filter(ChatFeedback.assistant_message_id == message.id).one_or_none()
    # 是否要发通知：本次是踩，且之前没踩过 / 没通知过
    notify = payload.rating == "unhelpful" and (feedback is None or feedback.rating != "unhelpful" or feedback.notified_at is None)
    if feedback is None:
        # 没有就新建一条反馈记录
        feedback = ChatFeedback(
            repo_id=repo.id,
            conversation_id=conversation.id,
            assistant_message_id=message.id,
            run_id=run_id,
            user_id=reporter.id,
            rating=payload.rating,  # helpful / unhelpful
        )
        db.add(feedback)

    previous_rating = feedback.rating  # 记录旧评分，判断状态要不要变
    feedback.rating = payload.rating
    feedback.reason = payload.reason if payload.rating == "unhelpful" else None  # 只有踩才记录原因
    feedback.comment = (payload.comment or "").strip() or None
    feedback.run_id = run_id
    feedback.user_id = reporter.id
    if payload.rating == "unhelpful":
        # 踩：如果之前不是踩，重新打开待审核；需要通知则标记 pending
        if previous_rating != "unhelpful":
            feedback.review_status = "open"
            feedback.review_note = None
            feedback.reviewed_at = None
        if notify:
            feedback.notification_status = "pending"
            feedback.notification_error = None
    else:
        # 赞：直接标记已解决，不通知
        feedback.review_status = "resolved"
        feedback.review_note = None
        feedback.reviewed_at = datetime.now(timezone.utc)
        feedback.notification_status = "not_applicable"
        feedback.notification_error = None
    db.commit()  # 先提交主记录
    db.refresh(feedback)

    if notify:
        # 发点踩通知（比如通知负责人跟进）；返回通知状态写回
        notification_status, notification_error = await send_negative_feedback_notification(
            feedback=feedback,
            repo=repo,
            message=message,
            run=run,
            reporter=reporter,
        )
        feedback.notification_status = notification_status
        feedback.notification_error = notification_error
        feedback.notified_at = datetime.now(timezone.utc) if notification_status == "sent" else None
        db.commit()  # 再提交通知结果
        db.refresh(feedback)
    return feedback


@router.patch("/{feedback_id}", response_model=ChatFeedbackResponse)  # PATCH 审核反馈（部分更新）
async def review_feedback(
    feedback_id: uuid.UUID,
    payload: ChatFeedbackReview,  # 含 review_status、review_note
    db: Session = Depends(get_db),
) -> ChatFeedback:
    feedback = db.get(ChatFeedback, feedback_id)
    if feedback is None:
        raise HTTPException(status_code=404, detail="未找到反馈记录")
    feedback.review_status = payload.review_status  # 改成「已处理/已忽略」等
    feedback.review_note = (payload.review_note or "").strip() or None
    # 标记为 resolved/dismissed 时记录处理时间
    feedback.reviewed_at = datetime.now(timezone.utc) if payload.review_status in {"resolved", "dismissed"} else None
    db.commit()
    db.refresh(feedback)
    return feedback
