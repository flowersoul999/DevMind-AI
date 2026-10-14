"""
project_index.py —— 项目索引（Project Index）接口的数据契约（Schema 层）

本文件服务于「仓库知识索引的构建/状态/暂停」接口：
- ProjectIndexStateResponse：当前索引构建的状态（进行中/完成、已索引多少文档等）；
- ProjectIndexScanResponse：触发一次重新扫描后的响应；
- ProjectIndexSnoozeRequest / Response：把索引调度「暂停」若干天（免打扰）。

索引就是把仓库文档切分、向量化后建库，供 RAG 检索；暂停用于避免频繁重建打扰开发。."""

from datetime import datetime  # datetime：时间戳
from typing import Any  # Any：任意类型，用于自由的 dict 值
from uuid import UUID  # UUID：唯一 ID

from pydantic import BaseModel, Field  # BaseModel：基类；Field：字段约束


class ProjectIndexStateResponse(BaseModel):
    """ProjectIndexStateResponse."""
    # 索引状态的响应结构
    repo_id: UUID  # 仓库 ID，必填
    status: str  # 索引状态（building/ready/error 等），必填
    fingerprint: str = ""  # 内容指纹（用于判断仓库是否变化），默认空
    docs_indexed: int = 0  # 已索引文档数，默认 0
    docs_total: int = 0  # 文档总数，默认 0
    error_message: str | None = None  # 若失败，错误信息；可空
    summary_json: dict[str, Any] | None = None  # 索引摘要（任意结构）；可空
    snoozed_until: datetime | None = None  # 暂停到的截止时间；可空（没暂停则为 None）
    last_scan_at: datetime | None = None  # 上次扫描时间；可空


class ProjectIndexScanResponse(BaseModel):
    """ProjectIndexScanResponse."""
    # 触发重新扫描后的响应
    repo_id: UUID  # 仓库 ID，必填
    started: bool  # 是否已开始扫描，必填
    status: str = "building"  # 当前状态，默认 "building"（构建中）


class ProjectIndexSnoozeRequest(BaseModel):
    """ProjectIndexSnoozeRequest."""
    # 请求「暂停索引 N 天」的请求体
    days: int = Field(default=7, ge=1, le=30)  # 暂停天数；默认 7 天，限定 1~30 天（ge=1, le=30）


class ProjectIndexSnoozeResponse(BaseModel):
    """ProjectIndexSnoozeResponse."""
    # 暂停设置成功后的响应
    repo_id: UUID  # 仓库 ID，必填
    snoozed_until: datetime  # 暂停到的具体截止时间，必填
