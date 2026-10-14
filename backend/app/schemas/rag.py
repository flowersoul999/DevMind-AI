"""
rag.py —— RAG（检索增强生成）相关接口的数据契约（Schema 层）

本文件服务于知识库配置、RAG 检索/问答、检索效果测试、状态查询等接口：
- KnowledgeBaseConfigUpdate / KnowledgeBaseResponse：知识库配置与信息；
- KnowledgeDocumentResponse / KnowledgeChunkResponse：已索引文档与片段；
- RAGQueryRequest / RAGSearchResponse / RAGSourceResponse：发起检索与返回命中来源；
- RAGAnswerResponse：基于检索结果让模型生成的回答；
- RetrievalTest*：检索效果「沙盒测试」；
- ModelCatalogResponse / RAGStatusResponse / RAGReindexResponse：模型目录、状态、重建索引。

Field(ge=…, le=…) 等约束用来把「embedding 维度、top_k、阈值」等参数锁在安全区间内，避免越界报错。."""

from datetime import datetime  # datetime：时间戳
from typing import Any, Literal  # Any：任意类型；Literal：限定固定取值
from uuid import UUID  # UUID：唯一 ID

from pydantic import BaseModel, Field  # BaseModel：基类；Field：字段约束


class KnowledgeBaseConfigUpdate(BaseModel):
    """KnowledgeBaseConfigUpdate."""
    # 更新知识库配置（所有字段都可空：只传想改的，不传的保持不变）
    embedding_provider: str | None = None  # 向量化模型供应商；可空
    embedding_model: str | None = None  # 向量化模型名；可空
    embedding_dimensions: int | None = Field(default=None, ge=64, le=4096)  # 向量维度；限定 64~4096
    retrieval_method: Literal["vector", "fulltext", "hybrid"] | None = None  # 检索方式：向量/全文/混合；可空
    rerank_enabled: bool | None = None  # 是否开启重排；可空
    rerank_provider: str | None = None  # 重排模型供应商；可空
    rerank_model: str | None = None  # 重排模型名；可空
    top_k: int | None = Field(default=None, ge=1, le=20)  # 召回条数；限定 1~20
    score_threshold_enabled: bool | None = None  # 是否启用分数门槛；可空
    score_threshold: float | None = Field(default=None, ge=0, le=1)  # 分数门槛 0~1；可空
    chunk_size: int | None = Field(default=None, ge=200, le=4000)  # 切片大小（字符数）；限定 200~4000
    chunk_overlap: int | None = Field(default=None, ge=0, le=1000)  # 切片重叠（字符数）；限定 0~1000


class KnowledgeBaseResponse(BaseModel):
    """KnowledgeBaseResponse."""
    # 知识库信息响应
    id: UUID  # 知识库 ID，必填
    name: str  # 名称，必填
    full_name: str  # 全名（如 owner/repo），必填
    description: str = ""  # 描述，默认空
    provider: str = "github"  # 来源供应商，默认 github
    document_count: int = 0  # 文档数，默认 0
    chunk_count: int = 0  # 片段数，默认 0
    character_count: int = 0  # 字符总数，默认 0
    config: dict[str, Any] = Field(default_factory=dict)  # 当前配置，默认空字典
    runtime: dict[str, Any] = Field(default_factory=dict)  # 运行时状态，默认空
    created_at: datetime | None = None  # 创建时间；可空
    updated_at: datetime | None = None  # 更新时间；可空


class KnowledgeDocumentResponse(BaseModel):
    """KnowledgeDocumentResponse."""
    # 一份已索引文档的响应
    id: UUID  # 文档 ID，必填
    repo_id: UUID  # 仓库 ID，必填
    name: str  # 文件名，必填
    source_type: str  # 来源类型，必填
    status: str  # 状态（pending/indexed/failed 等），必填
    content_type: str | None = None  # 内容类型（MIME）；可空
    file_size: int = 0  # 文件大小（字节），默认 0
    character_count: int = 0  # 字符数，默认 0
    chunk_count: int = 0  # 片段数，默认 0
    error_message: str | None = None  # 失败原因；可空
    created_at: datetime | None = None  # 创建时间；可空
    updated_at: datetime | None = None  # 更新时间；可空
    completed_at: datetime | None = None  # 完成时间；可空


class KnowledgeChunkResponse(BaseModel):
    """KnowledgeChunkResponse."""
    # 文档中一个片段（chunk）的响应
    id: UUID  # 片段 ID，必填
    position: int  # 在文档中的序号，必填
    title: str  # 片段标题，必填
    content: str  # 片段内容，必填
    character_count: int  # 字符数，必填
    enabled: bool = True  # 是否启用（可被检索）；默认启用


class RAGQueryRequest(BaseModel):
    """RAGQueryRequest."""
    # 发起一次 RAG 检索的请求体
    question: str = Field(min_length=1, max_length=4000)  # 检索问句；至少 1 字、最多 4000 字
    top_k: int = Field(default=5, ge=1, le=10)  # 召回条数；默认 5，限定 1~10
    source_types: list[str] = Field(default_factory=list, max_length=20)  # 限定来源类型列表；默认空，最多 20 个
    metadata_filters: dict[str, Any] = Field(default_factory=dict)  # 按元数据过滤，默认空


class RAGSourceResponse(BaseModel):
    """RAGSourceResponse."""
    # 一个检索命中来源的响应结构
    citation: int  # 引用序号（前端展示用 [1][2]），必填
    document_id: str  # 所属文档 ID，必填
    source_id: str  # 来源 ID，必填
    source_type: str  # 来源类型，必填
    title: str  # 标题，必填
    snippet: str  # 命中片段文本，必填
    score: float  # 相关度分数，必填
    metadata: dict[str, Any] = Field(default_factory=dict)  # 附加元数据，默认空
    retrieval: dict[str, Any] = Field(default_factory=dict)  # 检索明细（用了哪种方式/阈值），默认空


class RAGSearchResponse(BaseModel):
    """RAGSearchResponse."""
    # 一次 RAG 检索的响应
    query: str  # 原始问句，必填
    results: list[RAGSourceResponse]  # 命中来源列表，必填
    result_count: int  # 命中条数，必填
    took_ms: int  # 耗时（毫秒），必填
    retrieval_config: dict[str, Any] = Field(default_factory=dict)  # 本次使用的检索配置，默认空


class RetrievalTestRequest(BaseModel):
    """RetrievalTestRequest."""
    # 检索「沙盒测试」请求（不改变线上配置，只试算效果）
    query: str = Field(min_length=1, max_length=250)  # 测试问句；1~250 字
    top_k: int | None = Field(default=None, ge=1, le=20)  # 召回条数；可空，1~20
    retrieval_method: Literal["vector", "fulltext", "hybrid"] | None = None  # 检索方式；可空
    rerank_enabled: bool | None = None  # 是否重排；可空
    score_threshold_enabled: bool | None = None  # 是否启用分数门槛；可空
    score_threshold: float | None = Field(default=None, ge=0, le=1)  # 分数门槛 0~1；可空
    metadata_filters: dict[str, Any] = Field(default_factory=dict)  # 元数据过滤，默认空


class RetrievalTestResponse(BaseModel):
    """RetrievalTestResponse."""
    # 检索测试的响应
    id: UUID  # 测试记录 ID，必填
    query: str  # 问句，必填
    results: list[RAGSourceResponse]  # 命中来源，必填
    result_count: int  # 命中条数，必填
    took_ms: int  # 耗时，必填
    retrieval_config: dict[str, Any] = Field(default_factory=dict)  # 使用的配置，默认空
    created_at: datetime | None = None  # 创建时间；可空


class RetrievalTestHistoryResponse(BaseModel):
    """RetrievalTestHistoryResponse."""
    # 检索测试历史的单条摘要
    id: UUID  # 测试记录 ID，必填
    query: str  # 问句，必填
    result_count: int  # 命中条数，必填
    took_ms: int  # 耗时，必填
    created_at: datetime | None = None  # 创建时间；可空


class ModelCatalogResponse(BaseModel):
    """ModelCatalogResponse."""
    # 可用模型目录（供前端下拉选择）
    embedding_models: list[dict[str, Any]]  # 向量化模型列表，必填
    rerank_models: list[dict[str, Any]]  # 重排模型列表，必填
    deepseek: dict[str, Any]  # 对话模型（deepseek）信息，必填


class RAGAnswerResponse(BaseModel):
    """RAGAnswerResponse."""
    # 基于检索结果生成的回答响应
    question: str  # 原始问句，必填
    answer: str  # 生成的回答，必填
    sources: list[RAGSourceResponse]  # 引用来源，必填
    retrieval_count: int  # 参与生成的检索条数，必填
    generation_mode: Literal["llm", "extractive", "no_evidence"]  # 生成方式：大模型/抽取式/无证据，必填
    model: str | None = None  # 使用的生成模型；可空
    answer_gate: dict[str, Any] = Field(default_factory=dict)  # 回答质量门禁结果，默认空
    retrieval_config: dict[str, Any] = Field(default_factory=dict)  # 检索配置，默认空
    took_ms: int  # 耗时（毫秒），必填


class RAGStatusResponse(BaseModel):
    """RAGStatusResponse."""
    # RAG 系统状态响应
    repo_id: UUID  # 仓库 ID，必填
    document_count: int  # 文档数，必填
    source_count: int  # 来源数，必填
    source_types: dict[str, int] = Field(default_factory=dict)  # 各来源类型的数量，默认空
    embedding: dict[str, Any] = Field(default_factory=dict)  # 向量化状态，默认空
    vector_store: dict[str, Any] = Field(default_factory=dict)  # 向量库状态，默认空
    generation: dict[str, Any] = Field(default_factory=dict)  # 生成状态，默认空
    supported_files: list[str] = Field(default_factory=list)  # 支持的文档后缀列表，默认空


class RAGReindexResponse(BaseModel):
    """RAGReindexResponse."""
    # 重建索引后的响应
    repo_id: UUID  # 仓库 ID，必填
    indexed_documents: int  # 已重建索引的文档数，必填
    embedding_provider: str  # 向量化供应商，必填
    embedding_model: str  # 向量化模型，必填
