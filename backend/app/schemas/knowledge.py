"""
knowledge.py —— 知识库（Knowledge）相关接口的数据契约（Schema 层）

本文件服务于「团队知识沉淀/检索」一系列接口，覆盖：
- 团队记忆笔记（MemoryNoteRequest/Response）、团队成员（TeamMember*）；
- 知识条目与上传（KnowledgeItemResponse / KnowledgeUploadResponse）；
- 知识图谱（KnowledgeGraph* / KnowledgeGraphRebuildResponse）：把知识间关系画成节点+连线；
- 召回事件、记忆搜索/状态/候选（RecallEventResponse / MemorySearchResponse / MemoryStatusResponse / MemoryCandidate*）。

成员相关模型用 populate_by_name 兼容「techStack」(前端驼峰) 与「tech_stack」(后端蛇形) 两种命名。."""

from datetime import datetime  # datetime：时间戳
from typing import Any  # Any：表示任意类型（这里用于自由结构的 dict 值）
from uuid import UUID  # UUID：唯一 ID

from pydantic import BaseModel, Field  # BaseModel：基类；Field：字段约束/别名


class MemoryNoteRequest(BaseModel):
    """MemoryNoteRequest."""
    # 新建一条团队记忆笔记的请求体
    title: str = ""  # 笔记标题，默认空字符串
    content: str = ""  # 笔记正文，默认空


class TeamMemberRequest(BaseModel):
    """TeamMemberRequest."""
    # 新增/更新一名团队成员的请求体
    name: str  # 姓名，必填
    role: str = ""  # 角色（如后端/前端），默认空
    strengths: str = ""  # 擅长领域，默认空
    tech_stack: str = Field(default="", alias="techStack")  # 技术栈；alias 让前端可用 techStack 传入

    model_config = {"populate_by_name": True}  # 为什么：同时允许用 tech_stack 和 techStack 两种名字赋值，兼容前后端命名差异


class TeamMemberResponse(BaseModel):
    """TeamMemberResponse."""
    # 团队成员的响应结构（从数据库读出后返回）
    id: UUID  # 成员记录 ID，必填
    name: str  # 姓名，必填
    role: str = ""  # 角色，默认空
    strengths: str = ""  # 擅长领域，默认空
    tech_stack: str = Field(default="", alias="techStack")  # 技术栈；同时兼容 techStack 命名
    created_at: datetime | None = None  # 创建时间；可空

    model_config = {"populate_by_name": True}  # 为什么：同上，兼容两种字段名


class KnowledgeItemResponse(BaseModel):
    """KnowledgeItemResponse."""
    # 一条知识条目的响应结构
    id: UUID  # 知识条目 ID，必填
    document_ids: list[UUID]  # 它背后对应的文档 ID 列表，必填
    source_type: str  # 来源类型（如 knowledge_file），必填
    title: str  # 标题，必填
    content_preview: str  # 内容预览（截断后的文本），必填
    metadata: dict[str, Any] = Field(default_factory=dict)  # 任意附加元数据，默认空字典
    chunk_count: int  # 被切分成的片段数量，必填
    created_at: datetime | None = None  # 创建时间；可空


class KnowledgeUploadResponse(BaseModel):
    """KnowledgeUploadResponse."""
    # 上传一份知识文档后的响应
    id: UUID  # 新建文档 ID，必填
    filename: str  # 文件名，必填
    chunks: int  # 切分出的片段数，必填
    indexed: bool  # 是否已成功建索引，必填
    duplicate: bool = False  # 是否为重复上传；默认 False（不是重复）


class KnowledgeGraphNodeResponse(BaseModel):
    """KnowledgeGraphNodeResponse."""
    # 知识图谱中的一个「节点」（某个实体，如文档/人/概念）
    id: str  # 节点 ID，必填
    type: str  # 节点类型，必填
    title: str  # 节点标题，必填
    subtitle: str = ""  # 副标题，默认空
    metadata: dict[str, Any] = Field(default_factory=dict)  # 附加元数据，默认空
    highlighted: bool = False  # 是否被高亮（搜索命中时），默认不亮


class KnowledgeGraphEdgeResponse(BaseModel):
    """KnowledgeGraphEdgeResponse."""
    # 知识图谱中的一条「连线」（两个节点之间的关系）
    id: str  # 连线 ID，必填
    from_type: str  # 起点节点类型，必填
    from_id: str  # 起点节点 ID，必填
    to_type: str  # 终点节点类型，必填
    to_id: str  # 终点节点 ID，必填
    relation: str  # 关系描述（如 references/owns），必填
    confidence: float = 1.0  # 关系置信度 0~1，默认 1.0（完全确定）
    source: str = "explicit"  # 关系来源（explicit 显式 / inferred 推断），默认显式
    metadata: dict[str, Any] = Field(default_factory=dict)  # 附加元数据，默认空


class KnowledgeGraphResponse(BaseModel):
    """KnowledgeGraphResponse."""
    # 一次知识图谱查询的响应
    repo_id: UUID  # 仓库 ID，必填
    center_type: str | None = None  # 中心节点类型（以谁为中心展开）；可空
    center_id: str | None = None  # 中心节点 ID；可空
    depth: int = 1  # 展开层数（几度关系），默认 1 层
    nodes: list[KnowledgeGraphNodeResponse]  # 节点列表，必填
    edges: list[KnowledgeGraphEdgeResponse]  # 连线列表，必填
    stats: dict[str, Any] = Field(default_factory=dict)  # 统计信息，默认空


class KnowledgeGraphRebuildResponse(BaseModel):
    """KnowledgeGraphRebuildResponse."""
    # 重建知识图谱后的响应
    repo_id: UUID  # 仓库 ID，必填
    edges: int  # 重建后连线条数，必填
    nodes: int  # 重建后节点个数，必填


class RecallEventResponse(BaseModel):
    """RecallEventResponse."""
    # 一次「召回」事件的记录（被用于 RAG 的检索动作留痕）
    id: UUID  # 事件 ID，必填
    repo_id: UUID | None = None  # 仓库 ID；可空
    conversation_id: UUID | None = None  # 会话 ID；可空
    tool_name: str  # 触发召回的工具名，必填
    query: str = ""  # 检索关键词，默认空
    scope: str = "project"  # 检索范围，默认 "project"（项目级）
    mode: str = "hybrid"  # 检索模式（hybrid 混合），默认混合
    status: str = "success"  # 状态，默认成功
    result_count: int = 0  # 召回结果条数，默认 0
    results: list[dict[str, Any]] = Field(default_factory=list)  # 召回结果列表，默认空
    citations: list[dict[str, Any]] = Field(default_factory=list)  # 引用列表，默认空
    metadata: dict[str, Any] = Field(default_factory=dict)  # 附加元数据，默认空
    created_at: datetime | None = None  # 创建时间；可空


class MemorySearchResponse(BaseModel):
    """MemorySearchResponse."""
    # 记忆搜索的响应
    query: str  # 搜索关键词，必填
    results: list[dict[str, Any]]  # 命中的记忆条目列表，必填
    result_count: int  # 命中条数，必填


class MemoryStatusResponse(BaseModel):
    """MemoryStatusResponse."""
    # 记忆系统状态的响应（各子系统健康度/统计）
    repo_id: UUID  # 仓库 ID，必填
    index: dict[str, Any] = Field(default_factory=dict)  # 索引状态，默认空
    documents: dict[str, Any] = Field(default_factory=dict)  # 文档统计，默认空
    evidence: dict[str, Any] = Field(default_factory=dict)  # 证据统计，默认空
    recall: dict[str, Any] = Field(default_factory=dict)  # 召回统计，默认空
    candidates: dict[str, Any] = Field(default_factory=dict)  # 候选统计，默认空
    vector: dict[str, Any] = Field(default_factory=dict)  # 向量库统计，默认空


class MemoryCandidateResponse(BaseModel):
    """MemoryCandidateResponse."""
    # 一条「记忆候选」的响应（待审核是否沉淀为正式记忆）
    id: UUID  # 候选 ID，必填
    repo_id: UUID | None = None  # 仓库 ID；可空
    conversation_id: UUID | None = None  # 会话 ID；可空
    session_id: UUID | None = None  # 会话片段 ID；可空
    kind: str  # 候选类型，必填
    title: str  # 标题，必填
    content: str  # 内容，必填
    status: str  # 状态（pending/accepted/rejected 等），必填
    source: str  # 来源，必填
    metadata: dict[str, Any] = Field(default_factory=dict)  # 附加元数据，默认空
    created_at: datetime | None = None  # 创建时间；可空
    reviewed_at: datetime | None = None  # 审核时间；可空


class MemoryCandidateActionResponse(BaseModel):
    """MemoryCandidateActionResponse."""
    # 对一条记忆候选执行「接受/拒绝」等操作后的响应
    id: UUID  # 候选 ID，必填
    status: str  # 操作后的状态，必填
    document_id: UUID | None = None  # 若被采纳，生成的正式文档 ID；可空
