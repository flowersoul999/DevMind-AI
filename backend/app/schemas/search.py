"""
search.py —— 全局搜索（Search）接口的数据契约（Schema 层）

本文件服务于「跨来源统一搜索」接口：
- SearchRequest：给定仓库 + 关键词 + 一堆可选过滤条件（来源类型、标签、路径、模块、状态、日期）；
- SearchResponse：返回命中的结果列表（结构自由，因为不同类型来源字段差异大）。

这里的结果用 list[dict]（任意键值对列表），因为 Issue/PR/文档的字段长得不一样，统一成字典最灵活。."""

from uuid import UUID  # UUID：唯一 ID

from pydantic import BaseModel, Field  # BaseModel：基类；Field：字段约束


class SearchRequest(BaseModel):
    """SearchRequest."""
    # 发起搜索的请求体
    repo_id: UUID  # 仓库 ID，必填
    query: str  # 搜索关键词，必填
    source_type: str | None = None  # 限定来源类型（issue/pr/document…）；可空
    label: str | None = None  # 限定标签；可空
    path: str | None = None  # 限定文件路径；可空
    module: str | None = None  # 限定模块；可空
    state: str | None = None  # 限定状态（open/closed）；可空
    start_date: str | None = None  # 起始日期（字符串）；可空
    end_date: str | None = None  # 结束日期（字符串）；可空
    limit: int = Field(default=5, ge=1, le=20)  # 返回条数上限；默认 5，限定 1~20


class SearchResponse(BaseModel):
    """SearchResponse."""
    # 搜索响应
    results: list[dict]  # 命中结果列表（每项是任意结构的字典），必填
