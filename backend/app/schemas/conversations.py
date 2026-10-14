"""
conversations.py —— 会话（Conversation）接口的数据契约（Schema 层）

本文件服务于「管理聊天会话」接口：
- ConversationCreateRequest：新建一个会话（只需一个可选标题）；
- ConversationResponse：会话的基本信息（所属仓库、消息数、状态、时间）。

会话是用户与 AI 多轮对话的容器，一条会话下挂很多条消息（消息结构见 chat.py）。."""

from datetime import datetime  # datetime：时间戳
from uuid import UUID  # UUID：唯一 ID

from pydantic import BaseModel  # BaseModel：模型基类


class ConversationCreateRequest(BaseModel):
    """ConversationCreateRequest."""
    # 新建会话的请求体（标题可空，后端会自动生成）
    title: str | None = None  # 会话标题；可空


class ConversationResponse(BaseModel):
    """ConversationResponse."""
    # 会话信息响应
    id: UUID  # 会话 ID，必填
    repo_id: UUID  # 所属仓库 ID，必填
    title: str  # 标题，必填
    status: str = "active"  # 状态（active/archived 等），默认 "active"（进行中）
    message_count: int = 0  # 消息条数，默认 0
    created_at: datetime | None = None  # 创建时间；可空
    updated_at: datetime | None = None  # 更新时间；可空

    model_config = {"from_attributes": True}  # 为什么：允许用 ORM 对象直接构造
