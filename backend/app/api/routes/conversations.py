"""DevMind AI module."""

# conversations.py —— 会话（Conversation）管理路由层
#
# 业务域：对话/会话。前端和 Agent 的每一轮聊天都挂在一个「会话」下，
# 会话相当于一个聊天窗口。本文件负责列出、创建、删除某个仓库下的会话。
#
# 暴露端点：
#   GET    /api/{repo_id}/conversations        —— 列出该仓库的所有会话
#   POST   /api/{repo_id}/conversations        —— 新建一个会话
#   DELETE /api/{repo_id}/conversations/{conversation_id} —— 删除会话，并返回剩余会话列表
#
# 依赖 service：app.services.conversations（会话的增删查逻辑）
import uuid  # Python 标准库：生成/处理 UUID（全局唯一 ID）
from typing import Any  # 类型标注用：表示任意类型

from fastapi import APIRouter, Depends, HTTPException  # 路由、依赖注入、HTTP 异常
from sqlalchemy.orm import Session  # 数据库会话类型

from app.db.models import Repository  # 仓库表模型
from app.db.session import get_db  # 获取数据库会话的依赖
from app.schemas.conversations import ConversationCreateRequest, ConversationResponse  # 请求/响应模型
from app.services.conversations import create_conversation, delete_conversation, list_conversations  # 会话业务逻辑

router = APIRouter()  # 本模块路由器


def _repo_or_404(db: Session, repo_id: uuid.UUID) -> Repository:
    # 小工具：按 repo_id 查仓库，查不到就抛 404；很多端点都会复用它
    repo = db.get(Repository, repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="未找到仓库")
    return repo


@router.get("/{repo_id}/conversations", response_model=list[ConversationResponse])  # GET，路径带 repo_id；返回会话列表
async def get_conversations(repo_id: uuid.UUID, db: Session = Depends(get_db)) -> list[Any]:
    # repo_id 是路径参数（写在网址里的）；db 由 Depends 自动提供
    _repo_or_404(db, repo_id)  # 先确认仓库存在，不存在会直接 404
    conversations = list_conversations(db, repo_id)  # 调 service 拿该仓库的会话列表
    db.commit()  # 提交可能产生的数据库改动（列表查询通常无改动，但保持和创建一致）
    return conversations  # 返回查到的会话


@router.post("/{repo_id}/conversations", response_model=ConversationResponse)  # POST，新建会话；返回新建的会话
async def add_conversation(
    repo_id: uuid.UUID,
    payload: ConversationCreateRequest | None = None,  # 请求体可选（可只传 repo_id，标题默认空）
    db: Session = Depends(get_db),
) -> Any:
    _repo_or_404(db, repo_id)  # 确认仓库存在
    # 创建会话；标题取 payload.title，没传就是 None
    conversation = create_conversation(db, repo_id, payload.title if payload else None)
    db.commit()  # 提交新建
    db.refresh(conversation)  # 刷新，拿到数据库生成的 id、时间等字段
    return conversation  # 返回新建好的会话


@router.delete("/{repo_id}/conversations/{conversation_id}")  # DELETE，删除指定会话；无固定响应模型，返回字典
async def remove_conversation(
    repo_id: uuid.UUID,
    conversation_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _repo_or_404(db, repo_id)  # 确认仓库存在
    replacement = delete_conversation(db, repo_id, conversation_id)  # 删除会话，service 会返回「当前应该激活的会话」
    conversations = list_conversations(db, repo_id)  # 重新列出剩下的会话
    db.commit()  # 提交删除
    # 返回被删 ID、新的激活会话 ID、以及剩余会话的精简列表
    return {
        "deleted": str(conversation_id),  # 被删除的会话 ID（转成字符串，方便前端用）
        "active_conversation_id": str(replacement.id),  # 删除后要切换到哪个会话
        "conversations": [
            {
                "id": str(item.id),
                "repo_id": str(item.repo_id),
                "title": item.title,
                "status": item.status,
                "message_count": item.message_count,  # 该会话有多少条消息
                "created_at": item.created_at.isoformat() if item.created_at else None,  # 时间转 ISO 字符串
                "updated_at": item.updated_at.isoformat() if item.updated_at else None,
            }
            for item in conversations
        ],
    }
