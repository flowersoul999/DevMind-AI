"""
action_drafts.py —— 动作草稿（Action Draft）与审计日志接口的数据契约（Schema 层）

本文件服务于「AI 生成待执行动作草稿 + 人工确认执行 + 留痕」接口：
- ActionDraftCreateRequest / ActionDraftResponse：创建一条动作草稿（如给 Issue 写评论）、查看草稿状态；
- ActionDraftConfirmResponse：确认执行后返回「草稿 + 审计记录 ID」；
- AuditLogResponse：所有敏感动作的操作审计日志。

核心思路：AI 先生成「草稿」，经人工确认才真正执行（安全护栏），每次操作都写入审计日志便于追责。."""

from datetime import datetime  # datetime：时间戳
from uuid import UUID  # UUID：唯一 ID

from pydantic import BaseModel, Field  # BaseModel：基类；Field：字段约束/别名


class ActionDraftCreateRequest(BaseModel):
    """ActionDraftCreateRequest."""
    # 创建一条动作草稿的请求体
    repo_id: UUID  # 仓库 ID，必填
    draft_type: str = Field(default="issue_comment")  # 草稿类型，默认 "issue_comment"（给 Issue 写评论）
    target_type: str = Field(default="issue")  # 作用对象类型，默认 "issue"
    target_id: UUID | None = None  # 作用对象 ID（如某条 Issue）；可空
    title: str = ""  # 草稿标题，默认空
    content: str  # 草稿正文（真正要发的内容），必填
    risk_level: str = "medium"  # 风险等级，默认 "medium"（中）
    metadata: dict = Field(default_factory=dict)  # 附加元数据，默认空字典


class ActionDraftResponse(BaseModel):
    """ActionDraftResponse."""
    # 动作草稿的响应结构
    id: UUID  # 草稿 ID，必填
    repo_id: UUID  # 仓库 ID，必填
    draft_type: str  # 草稿类型，必填
    target_type: str  # 作用对象类型，必填
    target_id: UUID | None = None  # 作用对象 ID；可空
    title: str  # 标题，必填
    content: str  # 正文，必填
    risk_level: str  # 风险等级，必填
    status: str  # 状态（pending/approved/executed/rejected 等），必填
    execution_result: dict | None = None  # 执行结果（若已执行）；可空
    error_message: str | None = None  # 执行错误信息；可空
    metadata: dict = Field(default_factory=dict, alias="meta")  # 附加元数据；alias 兼容前端用 "meta" 传入
    created_at: datetime  # 创建时间，必填
    updated_at: datetime | None = None  # 更新时间；可空

    model_config = {"from_attributes": True, "populate_by_name": True}  # 为什么：from_attributes 支持 ORM；populate_by_name 兼容 meta/metadata 两种命名


class ActionDraftConfirmResponse(BaseModel):
    """ActionDraftConfirmResponse."""
    # 确认执行后的响应
    draft: ActionDraftResponse  # 被确认的草稿对象，必填
    audit_id: UUID  # 本次操作产生的审计记录 ID，必填


class AuditLogResponse(BaseModel):
    """AuditLogResponse."""
    # 一条审计日志的响应（记录谁在何时对什么做了什么）
    id: UUID  # 日志 ID，必填
    user_id: UUID | None = None  # 操作用户 ID；可空
    repo_id: UUID | None = None  # 仓库 ID；可空
    action: str  # 动作类型，必填
    target_type: str | None = None  # 目标类型；可空
    target_id: str | None = None  # 目标 ID；可空
    status: str  # 执行状态，必填
    request_json: dict  # 请求原文（JSON），必填
    result_json: dict  # 结果原文（JSON），必填
    created_at: datetime  # 创建时间，必填

    model_config = {"from_attributes": True}  # 为什么：允许用 ORM 对象直接构造
