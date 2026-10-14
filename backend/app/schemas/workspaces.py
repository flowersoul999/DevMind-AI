"""
workspaces.py —— 工作区（Workspace）与多仓库报告接口的数据契约（Schema 层）

本文件服务于「管理工作区」「跨多个仓库生成汇总报告」接口：
- WorkspaceCreateRequest / WorkspaceResponse：创建一个工作区（聚合多个仓库）、查看工作区信息；
- MultiRepoReportRequest / MultiRepoReportResponse：给定若干仓库与时间范围，生成跨仓库汇总报告。

工作区是「把多个仓库当成一个项目来看」的容器，便于做跨仓库统计与周报。."""

from datetime import date, datetime  # date：仅日期；datetime：日期+时间
from uuid import UUID  # UUID：唯一 ID

from pydantic import BaseModel, Field  # BaseModel：基类；Field：字段约束


class WorkspaceCreateRequest(BaseModel):
    """WorkspaceCreateRequest."""
    # 创建工作区的请求体
    name: str  # 工作区名称，必填
    description: str | None = None  # 描述；可空
    repo_ids: list[UUID] = Field(default_factory=list)  # 纳入该工作区的仓库 ID 列表，默认空


class WorkspaceResponse(BaseModel):
    """WorkspaceResponse."""
    # 工作区信息响应
    id: UUID  # 工作区 ID，必填
    name: str  # 名称，必填
    description: str | None = None  # 描述；可空
    repo_ids: list[str] = Field(default_factory=list)  # 关联仓库 ID 列表（这里以字符串存储），默认空
    created_at: datetime  # 创建时间，必填
    updated_at: datetime | None = None  # 更新时间；可空

    model_config = {"from_attributes": True}  # 为什么：允许用 ORM 对象直接构造


class MultiRepoReportRequest(BaseModel):
    """MultiRepoReportRequest."""
    # 跨仓库汇总报告的请求体
    workspace_id: UUID | None = None  # 指定工作区；可空（也可直接给 repo_ids）
    repo_ids: list[UUID] = Field(default_factory=list)  # 直接指定仓库 ID 列表，默认空
    start_date: date  # 统计开始日期，必填
    end_date: date  # 统计结束日期，必填


class MultiRepoReportResponse(BaseModel):
    """MultiRepoReportResponse."""
    # 跨仓库报告响应
    report_markdown: str  # 报告内容（Markdown），必填
    metrics: dict  # 汇总指标（任意结构），必填
    repo_summaries: list[dict]  # 各仓库的小结列表，必填
