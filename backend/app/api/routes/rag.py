"""DevMind AI module."""

# rag.py —— RAG 知识库（检索增强生成）路由层
#
# 业务域：底层知识库管理。和 knowledge.py（偏用户记忆）不同，这里是「知识库引擎」本身：
# 模型目录、仓库级知识库配置、文档上传/切片/重试/删除、检索测试、状态查询、
# 语义搜索、问答（ask）、重建索引。
#
# 暴露端点：
#   GET  /api/rag/models                       —— 可用 embedding/生成模型目录
#   GET  /api/rag/repositories                 —— 列出有知识库的仓库
#   GET  /api/rag/repositories/{repo_id}      —— 单个仓库知识库详情
#   PATCH /api/rag/repositories/{repo_id}/config —— 更新知识库配置（切片/向量参数）
#   GET  /api/rag/repositories/{repo_id}/documents —— 文档列表
#   POST /api/rag/repositories/{repo_id}/documents —— 上传文档（后台切片）
#   GET  /api/rag/repositories/{repo_id}/documents/{id}/chunks —— 某文档的切片
#   POST /api/rag/repositories/{repo_id}/documents/{id}/retry —— 重试处理失败文档
#   DELETE /api/rag/repositories/{repo_id}/documents/{id} —— 删除文档
#   POST /api/rag/repositories/{repo_id}/retrieval-tests —— 跑一次检索测试
#   GET  /api/rag/repositories/{repo_id}/retrieval-tests —— 检索测试历史
#   GET  /api/rag/{repo_id}/status             —— 知识库状态
#   POST /api/rag/{repo_id}/search             —— 语义搜索
#   POST /api/rag/{repo_id}/ask                —— 直接问答
#   POST /api/rag/{repo_id}/reindex            —— 重建索引
#
# 依赖 service：app.services.rag.*（文档提取/切片/向量化/问答/检索）
import asyncio  # 异步（文件写入放线程）
import hashlib  # 哈希（去重）
import re  # 正则（清洗文件名）
import uuid  # UUID
from pathlib import Path  # 路径
from typing import Any  # 任意类型

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, UploadFile  # 路由、后台、文件、异常、查询参数
from sqlalchemy import func  # SQL 聚合函数（count/sum）
from sqlalchemy.orm import Session  # 会话

from app.core.config import settings  # 配置
from app.db.models import (
    Document,
    KnowledgeSourceDocument,
    Repository,
    RetrievalTestRun,
)
from app.db.session import get_db  # 会话依赖
from app.schemas.rag import (  # 大量 RAG 相关模型
    KnowledgeBaseConfigUpdate,
    KnowledgeBaseResponse,
    KnowledgeChunkResponse,
    KnowledgeDocumentResponse,
    ModelCatalogResponse,
    RAGAnswerResponse,
    RAGQueryRequest,
    RAGReindexResponse,
    RAGSearchResponse,
    RetrievalTestHistoryResponse,
    RetrievalTestRequest,
    RetrievalTestResponse,
)
from app.services.rag.document_extraction import MAX_DOCUMENT_BYTES  # 最大上传字节
from app.services.rag.document_processing import SUPPORTED_KNOWLEDGE_SUFFIXES, process_knowledge_document  # 支持后缀、处理文档
from app.services.rag.embeddings import validate_embedding_contract  # 校验向量化参数
from app.services.rag.knowledge_base import (  # 知识库配置/状态
    config_dict,
    get_or_create_config,
    model_catalog,
    record_retrieval_test,
    retrieve_knowledge,
    runtime_status,
)
from app.services.rag.qa import answer_question, build_sources, rag_status, reindex_documents  # 问答/检索/状态/重建
from app.services.rag.vector_store import delete_document_group  # 删文档分组

router = APIRouter()  # 路由器


def _repo_or_404(db: Session, repo_id: uuid.UUID) -> Repository:
    # 复用工具：仓库不存在（或本身是 knowledge_base 类型）返回 404
    repo = db.get(Repository, repo_id)
    if repo is None or repo.provider == "knowledge_base":
        raise HTTPException(status_code=404, detail="未找到仓库")
    return repo


def _safe_filename(filename: str) -> str:
    # 清洗文件名，防路径穿越
    value = Path(filename or "upload.txt").name
    return re.sub(r"[^a-zA-Z0-9._\-\u4e00-\u9fff]+", "_", value).strip("._") or "upload.txt"


def _upload_root(repo_id: uuid.UUID) -> Path:
    # 该仓库上传目录（不存在就建）
    root = Path(settings.upload_dir)
    if not root.is_absolute():
        root = Path.cwd() / root
    path = root / str(repo_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _validate_chunking(chunk_size: int, overlap: int) -> None:
    # 校验切片参数：重叠必须小于切片长度，否则没意义
    if overlap >= chunk_size:
        raise HTTPException(status_code=400, detail="切片重叠必须小于切片长度")


def _document_response(item: KnowledgeSourceDocument) -> KnowledgeDocumentResponse:
    # 把知识源文档转成对外响应模型
    return KnowledgeDocumentResponse(
        id=item.id,
        repo_id=item.repo_id,
        name=item.name,
        source_type=item.source_type,
        status=item.status,  # queued/processing/done/failed
        content_type=item.content_type,
        file_size=item.file_size,
        character_count=item.character_count,
        chunk_count=item.chunk_count,
        error_message=item.error_message,
        created_at=item.created_at,
        updated_at=item.updated_at,
        completed_at=item.completed_at,
    )


def _knowledge_base(db: Session, repo: Repository) -> KnowledgeBaseResponse:
    # 组装一个仓库的知识库完整信息（配置、统计、运行时状态）
    config = get_or_create_config(db, repo.id)
    # 统计文档数和字符数（SQL 聚合）
    document_count, character_count = (
        db.query(func.count(KnowledgeSourceDocument.id), func.coalesce(func.sum(KnowledgeSourceDocument.character_count), 0))
        .filter(KnowledgeSourceDocument.repo_id == repo.id)
        .one()
    )
    chunk_count = db.query(func.count(Document.id)).filter(Document.repo_id == repo.id).scalar() or 0  # 切片总数
    return KnowledgeBaseResponse(
        id=repo.id,
        name=repo.name,
        full_name=repo.full_name,
        description=repo.description or "",
        provider=repo.provider or "github",
        document_count=int(document_count or 0),
        chunk_count=int(chunk_count),
        character_count=int(character_count or 0),
        config=config_dict(config),  # 配置字典
        runtime=runtime_status(config),  # 运行时（就绪/未配置向量库等）
        created_at=repo.created_at,
        updated_at=repo.updated_at,
    )


@router.get("/models", response_model=ModelCatalogResponse)  # GET 模型目录
async def get_model_catalog() -> ModelCatalogResponse:
    # 返回可用模型清单（embedding / 生成模型）
    return ModelCatalogResponse.model_validate(model_catalog())


@router.get("/repositories", response_model=list[KnowledgeBaseResponse])  # GET 知识库仓库列表
async def list_rag_repositories(db: Session = Depends(get_db)) -> list[KnowledgeBaseResponse]:
    repos = (
        db.query(Repository)
        .filter(Repository.provider != "knowledge_base")
        .order_by(Repository.updated_at.desc())
        .all()
    )
    return [_knowledge_base(db, repo) for repo in repos]


@router.get("/repositories/{repo_id}", response_model=KnowledgeBaseResponse)  # GET 单个知识库详情
async def get_rag_repository(repo_id: uuid.UUID, db: Session = Depends(get_db)) -> KnowledgeBaseResponse:
    return _knowledge_base(db, _repo_or_404(db, repo_id))


@router.patch("/repositories/{repo_id}/config", response_model=KnowledgeBaseResponse)  # PATCH 更新配置
async def update_rag_config(
    repo_id: uuid.UUID,
    payload: KnowledgeBaseConfigUpdate,  # 只传要改的字段
    db: Session = Depends(get_db),
) -> KnowledgeBaseResponse:
    repo = _repo_or_404(db, repo_id)
    config = get_or_create_config(db, repo_id, commit=False)
    changes = payload.model_dump(exclude_unset=True)  # 只取前端实际传的字段
    next_size = int(changes.get("chunk_size", config.chunk_size))
    next_overlap = int(changes.get("chunk_overlap", config.chunk_overlap))
    _validate_chunking(next_size, next_overlap)  # 校验切片参数
    try:
        validate_embedding_contract(  # 校验 embedding 提供方/模型/维度是否合法
            str(changes.get("embedding_provider", config.embedding_provider)),
            str(changes.get("embedding_model", config.embedding_model)),
            int(changes.get("embedding_dimensions", config.embedding_dimensions)),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc  # 冲突：参数不兼容
    for key, value in changes.items():
        setattr(config, key, value)  # 逐字段更新
    db.commit()
    db.refresh(repo)
    return _knowledge_base(db, repo)


@router.get("/repositories/{repo_id}/documents", response_model=list[KnowledgeDocumentResponse])  # GET 文档列表
async def list_documents(repo_id: uuid.UUID, db: Session = Depends(get_db)) -> list[KnowledgeDocumentResponse]:
    _repo_or_404(db, repo_id)
    items = (
        db.query(KnowledgeSourceDocument)
        .filter(KnowledgeSourceDocument.repo_id == repo_id)
        .order_by(KnowledgeSourceDocument.created_at.desc())
        .all()
    )
    return [_document_response(item) for item in items]


@router.post("/repositories/{repo_id}/documents", response_model=KnowledgeDocumentResponse, status_code=202)  # POST 上传文档
async def upload_document(
    repo_id: uuid.UUID,
    background_tasks: BackgroundTasks,  # 切片处理放后台
    file: UploadFile = File(...),  # 必传文件
    db: Session = Depends(get_db),
) -> KnowledgeDocumentResponse:
    _repo_or_404(db, repo_id)
    data = await file.read(MAX_DOCUMENT_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="上传文件为空")
    if len(data) > MAX_DOCUMENT_BYTES:
        raise HTTPException(status_code=400, detail="文件不能超过 12 MB")
    filename = _safe_filename(file.filename or "upload.txt")
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_KNOWLEDGE_SUFFIXES:
        raise HTTPException(status_code=415, detail=f"暂不支持 {suffix or '无扩展名'} 文件")
    checksum = hashlib.sha256(data).hexdigest()  # 内容指纹
    # 查是否已存在相同文档（非失败状态），去重
    duplicate = (
        db.query(KnowledgeSourceDocument)
        .filter(
            KnowledgeSourceDocument.repo_id == repo_id,
            KnowledgeSourceDocument.checksum == checksum,
            KnowledgeSourceDocument.status != "failed",
        )
        .order_by(KnowledgeSourceDocument.created_at.desc())
        .first()
    )
    if duplicate is not None:
        return _document_response(duplicate)  # 重复直接返回已有
    # 先建一条 queued 记录，再落盘，再后台处理
    document = KnowledgeSourceDocument(
        repo_id=repo_id,
        name=filename,
        content_type=file.content_type,
        checksum=checksum,
        file_size=len(data),
        stored_path="pending",
        status="queued",
    )
    db.add(document)
    db.flush()  # 先拿到 document.id（用于文件名）
    stored_path = _upload_root(repo_id) / f"{document.id}-{filename}"
    try:
        await asyncio.to_thread(stored_path.write_bytes, data)  # 线程里写文件
        document.stored_path = str(stored_path)
        db.commit()
    except Exception:
        db.rollback()  # 失败回滚
        try:
            await asyncio.to_thread(stored_path.unlink, missing_ok=True)
        except OSError:
            pass
        raise
    db.refresh(document)
    background_tasks.add_task(process_knowledge_document, document.id)  # 后台切片+向量化
    return _document_response(document)


@router.get("/repositories/{repo_id}/documents/{document_id}/chunks", response_model=list[KnowledgeChunkResponse])  # GET 文档切片
async def list_document_chunks(
    repo_id: uuid.UUID,
    document_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> list[KnowledgeChunkResponse]:
    _repo_or_404(db, repo_id)
    rows = (
        db.query(Document)
        .filter(Document.repo_id == repo_id, Document.source_id == document_id)  # source_id 即知识源文档 ID
        .order_by(Document.created_at.asc())
        .all()
    )
    return [
        KnowledgeChunkResponse(
            id=row.id,
            position=int((row.meta or {}).get("chunk_index", index)) + 1,  # 第几片（从 1 计）
            title=row.title,
            content=row.content,
            character_count=len(row.content),
        )
        for index, row in enumerate(rows)
    ]


@router.post("/repositories/{repo_id}/documents/{document_id}/retry", response_model=KnowledgeDocumentResponse)  # POST 重试处理
async def retry_document(
    repo_id: uuid.UUID,
    document_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> KnowledgeDocumentResponse:
    _repo_or_404(db, repo_id)
    document = db.get(KnowledgeSourceDocument, document_id)
    if document is None or document.repo_id != repo_id:
        raise HTTPException(status_code=404, detail="未找到文档")
    document.status = "queued"  # 重置为排队
    document.error_message = None
    db.commit()
    db.refresh(document)
    background_tasks.add_task(process_knowledge_document, document.id)  # 重新处理
    return _document_response(document)


@router.delete("/repositories/{repo_id}/documents/{document_id}")  # DELETE 删除文档
async def delete_document(repo_id: uuid.UUID, document_id: uuid.UUID, db: Session = Depends(get_db)) -> dict[str, Any]:
    _repo_or_404(db, repo_id)
    source = db.get(KnowledgeSourceDocument, document_id)
    if source is None or source.repo_id != repo_id:
        raise HTTPException(status_code=404, detail="未找到文档")
    delete_document_group(db, repo_id, document_id, source_type="knowledge_file", commit=False)  # 删向量切片
    path = Path(source.stored_path)
    if path.exists() and path.is_file():
        path.unlink()  # 删磁盘文件
    db.delete(source)  # 删源文档记录
    db.commit()
    return {"id": str(document_id), "deleted": True}


@router.post("/repositories/{repo_id}/retrieval-tests", response_model=RetrievalTestResponse)  # POST 检索测试
async def run_retrieval_test(
    repo_id: uuid.UUID,
    payload: RetrievalTestRequest,  # 含 query、top_k、检索方法、是否重排等
    db: Session = Depends(get_db),
) -> RetrievalTestResponse:
    _repo_or_404(db, repo_id)
    # 调 retrieve_knowledge 跑一次真实检索，拿到结果、trace、耗时
    items, trace, duration_ms = await retrieve_knowledge(
        db,
        repo_id,
        payload.query.strip(),
        top_k=payload.top_k,
        retrieval_method=payload.retrieval_method,
        rerank_enabled=payload.rerank_enabled,
        score_threshold_enabled=payload.score_threshold_enabled,
        score_threshold=payload.score_threshold,
        metadata_filters=payload.metadata_filters,
    )
    results = build_sources(items)  # 整理成来源格式
    run = record_retrieval_test(db, repo_id, payload.query.strip(), trace, results, duration_ms)  # 记录测试
    return RetrievalTestResponse(
        id=run.id,
        query=run.query,
        results=results,
        result_count=len(results),
        took_ms=duration_ms,
        retrieval_config=trace,  # 实际用了什么检索配置
        created_at=run.created_at,
    )


@router.get("/repositories/{repo_id}/retrieval-tests", response_model=list[RetrievalTestHistoryResponse])  # GET 测试历史
async def list_retrieval_tests(
    repo_id: uuid.UUID,
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> list[RetrievalTestHistoryResponse]:
    _repo_or_404(db, repo_id)
    runs = (
        db.query(RetrievalTestRun)
        .filter(RetrievalTestRun.repo_id == repo_id)
        .order_by(RetrievalTestRun.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        RetrievalTestHistoryResponse(
            id=run.id,
            query=run.query,
            result_count=len(run.results_json or []),
            took_ms=run.duration_ms,
            created_at=run.created_at,
        )
        for run in runs
    ]


@router.get("/{repo_id}/status")  # GET 知识库状态
async def get_rag_status(repo_id: uuid.UUID, db: Session = Depends(get_db)) -> dict[str, Any]:
    _repo_or_404(db, repo_id)
    config = get_or_create_config(db, repo_id)
    # 返回检索状态 + 配置 + 运行时
    return {**rag_status(db, repo_id), "config": config_dict(config), "runtime": runtime_status(config)}


@router.post("/{repo_id}/search", response_model=RAGSearchResponse)  # POST 语义搜索
async def search_rag(repo_id: uuid.UUID, payload: RAGQueryRequest, db: Session = Depends(get_db)) -> RAGSearchResponse:
    _repo_or_404(db, repo_id)
    items, trace, duration_ms = await retrieve_knowledge(
        db,
        repo_id,
        payload.question.strip(),
        top_k=payload.top_k,
        source_types=payload.source_types,
        metadata_filters=payload.metadata_filters,
    )
    results = build_sources(items)
    return RAGSearchResponse(
        query=payload.question.strip(),
        results=results,
        result_count=len(results),
        took_ms=duration_ms,
        retrieval_config=trace,
    )


@router.post("/{repo_id}/ask", response_model=RAGAnswerResponse)  # POST 直接问答
async def ask_rag(repo_id: uuid.UUID, payload: RAGQueryRequest, db: Session = Depends(get_db)) -> RAGAnswerResponse:
    _repo_or_404(db, repo_id)
    # 调问答服务：检索 + LLM 生成答案
    result = await answer_question(
        db,
        repo_id,
        payload.question,
        payload.top_k,
        payload.source_types,
        payload.metadata_filters,
    )
    return RAGAnswerResponse.model_validate(result)


@router.post("/{repo_id}/reindex", response_model=RAGReindexResponse)  # POST 重建索引
async def reindex_rag(repo_id: uuid.UUID, db: Session = Depends(get_db)) -> RAGReindexResponse:
    _repo_or_404(db, repo_id)
    config = get_or_create_config(db, repo_id)
    count = reindex_documents(db, repo_id)  # 重新切片+向量化所有文档
    return RAGReindexResponse(
        repo_id=repo_id,
        indexed_documents=count,
        embedding_provider=config.embedding_provider,
        embedding_model=config.embedding_model,
    )
