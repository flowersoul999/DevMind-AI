"""
feedback.py —— 对话反馈（Feedback）接口的数据契约（Schema 层）

本文件服务于「用户对 AI 回答打分/反馈」与「人工复核反馈」接口：
- ChatFeedbackCreate：用户提交一条反馈（有用/没用 + 原因 + 备注）；
- ChatFeedbackReview：管理员复核一条反馈的状态；
- ChatFeedbackResponse：反馈记录的完整返回（含通知状态、复核时间等）。

核心机制：用 Literal 把可选值写死（如下拉框）；用 model_validator 做「跨字段校验」
（例如选了「有帮助」就不该再填负反馈原因）。."""

from datetime import datetime  # datetime：时间戳
from typing import Literal  # Literal：限定字段只能取固定字符串
from uuid import UUID  # UUID：唯一 ID

from pydantic import BaseModel, ConfigDict, Field, model_validator  # ConfigDict：模型配置；model_validator：自定义校验器


# 评分取值：只有「helpful（有帮助）」「unhelpful（没帮助）」两种
FeedbackRating = Literal["helpful", "unhelpful"]
# 负反馈原因：限几种固定类型，方便后端统计
FeedbackReason = Literal[
    "inaccurate",   # 不准确
    "not_relevant",  # 不相关
    "missing_context",  # 缺少上下文
    "unreliable_citation",  # 引用不可靠
    "tool_error",  # 工具调用出错
    "other",  # 其他
]
# 复核状态：open 待处理 / in_review 复核中 / resolved 已解决 / dismissed 已忽略
ReviewStatus = Literal["open", "in_review", "resolved", "dismissed"]


class ChatFeedbackCreate(BaseModel):
    """ChatFeedbackCreate."""
    # 用户提交反馈的请求体
    repo_id: UUID  # 仓库 ID，必填
    conversation_id: UUID  # 会话 ID，必填
    assistant_message_id: UUID  # 被评价的 AI 消息 ID，必填
    rating: FeedbackRating  # 评分（helpful/unhelpful），必填
    reason: FeedbackReason | None = None  # 负反馈原因；可空（给好评时不需要）
    comment: str | None = Field(default=None, max_length=1000)  # 自由备注；可空，最多 1000 字（max_length 约束）

    @model_validator(mode="after")
    def validate_reason(self) -> "ChatFeedbackCreate":
        # 为什么：跨字段业务规则——选了「helpful」却还填负反馈原因，属于自相矛盾，直接报错拦截
        if self.rating == "helpful" and self.reason is not None:
            raise ValueError("有帮助反馈不需要填写负反馈原因")
        return self  # 校验通过，把对象原样返回


class ChatFeedbackReview(BaseModel):
    """ChatFeedbackReview."""
    # 管理员复核反馈的请求体
    review_status: ReviewStatus  # 复核后设置的状态，必填
    review_note: str | None = Field(default=None, max_length=2000)  # 复核备注；可空，最多 2000 字


class ChatFeedbackResponse(BaseModel):
    """ChatFeedbackResponse."""
    # 反馈记录的完整响应（通常从数据库读出后返回）
    model_config = ConfigDict(from_attributes=True)  # 为什么：允许直接用 ORM 对象构造，便于数据库行→JSON

    id: UUID  # 反馈记录 ID，必填
    repo_id: UUID  # 仓库 ID，必填
    conversation_id: UUID  # 会话 ID，必填
    assistant_message_id: UUID  # AI 消息 ID，必填
    run_id: UUID | None = None  # 对应的一次运行 ID；可空
    rating: FeedbackRating  # 评分，必填
    reason: FeedbackReason | None = None  # 负反馈原因；可空
    comment: str | None = None  # 备注；可空
    review_status: ReviewStatus  # 当前复核状态，必填
    review_note: str | None = None  # 复核备注；可空
    notification_status: str  # 通知发送状态（如 sent/failed），必填
    notification_error: str | None = None  # 通知失败原因；可空
    notified_at: datetime | None = None  # 通知发出时间；可空
    reviewed_at: datetime | None = None  # 复核时间；可空
    created_at: datetime  # 创建时间，必填
    updated_at: datetime  # 更新时间，必填
