"""
issues.py —— Issue（问题/工单）查询与 Issue 分析的数据契约（Schema 层）

本文件服务于「查看仓库 Issue 列表/详情」和「对 Issue 做智能分析」两个接口：
- IssueResponse：把一条 GitHub Issue 的基本信息返回前端（标题、状态、标签、指派人等）；
- IssueAnalysisResponse：把一次 Issue 分析的结果（IssueAnalysisOutput）打包返回。

与 analysis.py 是配套关系：analysis.py 定义「分析出来的内容长啥样」，本文件定义
「Issue 本身长啥样」以及「分析接口返回什么」。."""

from datetime import datetime  # datetime：时间戳
from uuid import UUID  # UUID：唯一 ID

from pydantic import BaseModel  # BaseModel：模型基类

from app.schemas.analysis import IssueAnalysisOutput  # 复用 analysis.py 里定义好的 Issue 分析输出结构


class IssueResponse(BaseModel):
    """IssueResponse."""
    # 一条 Issue 的响应结构
    id: UUID  # Issue 记录 ID，必填
    repo_id: UUID  # 所属仓库 ID，必填
    number: int  # Issue 在 GitHub 上的编号（#1、#2…），必填
    title: str  # 标题，必填
    body: str | None = None  # 正文内容；可空（有的 Issue 没正文）
    state: str  # 状态（open/closed 等），必填
    labels: list[str] = []  # 标签列表（字符串），默认空
    author: str | None = None  # 作者用户名；可空
    assignees: list[str] = []  # 指派人列表，默认空
    created_at: datetime | None = None  # 创建时间；可空
    updated_at: datetime | None = None  # 更新时间；可空

    model_config = {"from_attributes": True}  # 为什么：允许用 ORM 对象直接构造，便于数据库行→JSON


class IssueAnalysisResponse(BaseModel):
    """IssueAnalysisResponse."""
    # Issue 分析结果的响应结构
    analysis_id: UUID | str  # 本次分析记录 ID（UUID 或字符串），必填
    result: IssueAnalysisOutput  # 具体分析内容，复用 analysis.py 的 IssueAnalysisOutput，必填
