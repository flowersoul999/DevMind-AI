"""DevMind AI module."""

# knowledge.py —— 知识库 / 记忆（Knowledge & Memory）路由层
#
# 业务域：项目知识库与长期记忆。用户可上传文档、写记忆笔记、维护团队成员画像，
# 系统会把这些内容切片、向量化存进知识库，并构建「知识图谱（实体-关系）」。
# 还提供记忆候选的审批、记忆检索、知识图谱查询等能力。
#
# 暴露端点（均带 /{repo_id} 前缀）：
#   GET  /{repo_id}/knowledge                     —— 列出知识条目
#   GET  /{repo_id}/memory/search                  —— 手动检索记忆
#   GET  /{repo_id}/memory/recall-events           —— 查看记忆召回事件
#   GET  /{repo_id}/memory/status                  —— 记忆状态
#   GET  /{repo_id}/memory/candidates              —— 记忆候选列表
#   POST /{repo_id}/memory/candidates/{id}/approve —— 批准一条记忆候选
#   POST /{repo_id}/memory/candidates/{id}/reject  —— 拒绝一条记忆候选
#   POST /{repo_id}/knowledge/notes                —— 新建记忆笔记
#   POST /{repo_id}/knowledge/upload               —— 上传知识文件（切片+向量化）
#   GET  /{repo_id}/knowledge/graph                —— 查知识图谱
#   POST /{repo_id}/knowledge/graph/rebuild        —— 重建知识图谱
#   DELETE /{repo_id}/knowledge/{source_id}        —— 删除知识条目
#   GET/POST /{repo_id}/team-members               —— 团队成员列表/新增
#   DELETE /{repo_id}/team-members/{member_id}     —— 删除成员
#
# 依赖 service：app.services.knowledge_graph（图谱）、app.services.chat_memory（记忆）、
#              app.services.memory_hub（候选/召回）、app.services.rag.*（切片/向量化/检索）
import asyncio  # 异步（文件读取/向量化放到线程里跑，不阻塞）
import hashlib  # 哈希（去重：用内容 sha256 判断是否重复上传）
import re  # 正则（清洗文件名）
import uuid  # 生成 ID
from datetime import datetime, timezone  # 时间
from pathlib import Path  # 路径处理
from typing import Any  # 任意类型

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile  # 路由、依赖、文件上传、异常、查询参数
from sqlalchemy.orm import Session  # 数据库会话

from app.core.config import settings  # 配置（上传目录、向量维度等）
from app.db.models import Document, Repository  # 表模型
from app.db.session import get_db  # 会话依赖
from app.schemas.knowledge import (  # 大量响应/请求模型
    KnowledgeGraphRebuildResponse,
    KnowledgeGraphResponse,
    KnowledgeItemResponse,
    KnowledgeUploadResponse,
    MemoryCandidateActionResponse,
    MemoryCandidateResponse,
    MemorySearchResponse,
    MemoryStatusResponse,
    MemoryNoteRequest,
    RecallEventResponse,
    TeamMemberRequest,
    TeamMemberResponse,
)
from app.services.knowledge_graph import knowledge_graph_payload, rebuild_knowledge_graph  # 知识图谱
from app.services.chat_memory import EvidenceStore  # 证据/记忆检索
from app.services.memory_hub import (  # 记忆候选与召回
    approve_memory_candidate,
    list_memory_candidates,
    list_recall_events,
    memory_status,
    record_recall_event,
    reject_memory_candidate,
)
from app.services.rag.chunking import chunk_document  # 文档切片
from app.services.rag.document_extraction import MAX_DOCUMENT_BYTES, extract_document_text  # 提取文本/最大字节
from app.services.rag.document_processing import SUPPORTED_KNOWLEDGE_SUFFIXES  # 支持的文件后缀
from app.services.rag.embeddings import embed_texts, embedding_provider_name  # 向量化
from app.services.rag.vector_store import DocumentPayload, add_document, add_documents, delete_document_group, stable_source_id  # 向量库写入/删除

router = APIRouter()  # 路由器

# 用户可管理的知识来源类型（笔记、文件、成员、周报、项目说明等）
USER_KNOWLEDGE_SOURCE_TYPES = [
    "memory_note",
    "knowledge_file",
    "team_member",
    "weekly_report",
    "project_overview",
    "project_doc",
    "project_manifest",
]
EMBEDDING_BATCH_SIZE = 64  # 向量化时每批处理 64 段，避免一次塞太多


def _repo_or_404(db: Session, repo_id: uuid.UUID) -> Repository:
    # 复用工具：仓库不存在返回 404
    repo = db.get(Repository, repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="未找到仓库")
    return repo


def _clip(text: str, limit: int = 360) -> str:
    # 把文本截断到 limit 字，用于列表里的「内容预览」
    text = (text or "").strip().replace("\r\n", "\n")
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."


def _safe_filename(filename: str) -> str:
    # 清洗文件名：只保留字母数字和少数符号，防止路径穿越等安全问题
    value = Path(filename or "upload.txt").name
    return re.sub(r"[^a-zA-Z0-9._\-\u4e00-\u9fff]+", "_", value).strip("._") or "upload.txt"


def _upload_root(repo_id: uuid.UUID) -> Path:
    # 计算该仓库的上传目录，不存在就创建
    root = Path(settings.upload_dir)
    if not root.is_absolute():
        root = Path.cwd() / root  # 相对路径就拼到当前工作目录
    path = root / str(repo_id)
    path.mkdir(parents=True, exist_ok=True)  # 递归建目录
    return path


def _extract_upload_text(filename: str, data: bytes) -> str:
    # 调用文档提取服务把二进制转成纯文本；失败返回空串
    try:
        return extract_document_text(filename, data)
    except Exception:
        return ""


def _embed_default_in_batches(texts: list[str]) -> list[list[float]]:
    # 批量向量化：按 EMBEDDING_BATCH_SIZE 切分，逐批调用 embed_texts
    vectors: list[list[float]] = []
    for start in range(0, len(texts), EMBEDDING_BATCH_SIZE):
        vectors.extend(
            embed_texts(
                texts[start : start + EMBEDDING_BATCH_SIZE],
                dimensions=settings.embedding_dimensions,  # 用配置的维度
            )
        )
    return vectors


def _group_documents(documents: list[Document]) -> list[KnowledgeItemResponse]:
    # 把一个知识来源拆成的多个切片（chunk）合并成「一个知识条目」对外展示
    groups: dict[tuple[str, uuid.UUID], list[Document]] = {}
    for doc in documents:
        # 以 (来源类型, 来源ID) 分组
        groups.setdefault((doc.source_type, doc.source_id), []).append(doc)

    items: list[KnowledgeItemResponse] = []
    for (source_type, source_id), docs in groups.items():
        docs.sort(key=lambda item: ((item.meta or {}).get("chunk_index", 0), item.created_at))  # 按切片顺序排
        first = docs[0]
        content = "\n".join(doc.content for doc in docs[:2])  # 预览取前两段
        metadata = dict(first.meta or {})
        metadata.setdefault("source_type", source_type)
        items.append(
            KnowledgeItemResponse(
                id=source_id,
                document_ids=[doc.id for doc in docs],  # 下面所有切片 ID
                source_type=source_type,
                title=metadata.get("display_title") or first.title,
                content_preview=_clip(content),  # 内容预览
                metadata=metadata,
                chunk_count=len(docs),  # 共几片
                created_at=first.created_at,
            )
        )
    items.sort(key=lambda item: item.created_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)  # 按时间倒序
    return items


def _team_from_document(doc: Document) -> TeamMemberResponse:
    # 把 team_member 类型的文档转成「成员」对外模型
    meta = doc.meta or {}
    return TeamMemberResponse(
        id=doc.source_id,
        name=str(meta.get("name") or doc.title.replace("团队成员：", "").replace("Team member: ", "")),
        role=str(meta.get("role") or ""),
        strengths=str(meta.get("strengths") or ""),
        techStack=str(meta.get("tech_stack") or meta.get("techStack") or ""),
        created_at=doc.created_at,
    )


@router.get("/{repo_id}/knowledge", response_model=list[KnowledgeItemResponse])  # GET 知识列表
async def list_knowledge(repo_id: uuid.UUID, db: Session = Depends(get_db)) -> list[KnowledgeItemResponse]:
    _repo_or_404(db, repo_id)
    docs = (
        db.query(Document)
        .filter(Document.repo_id == repo_id, Document.source_type.in_(USER_KNOWLEDGE_SOURCE_TYPES))
        .order_by(Document.created_at.desc())
        .all()
    )
    return _group_documents(docs)  # 把切片合并成条目


@router.get("/{repo_id}/memory/search", response_model=MemorySearchResponse)  # GET 手动检索记忆
async def search_memory(
    repo_id: uuid.UUID,
    q: str = Query(default=""),  # 搜索词
    limit: int = Query(default=8, ge=1, le=30),  # 1~30 条
    db: Session = Depends(get_db),
) -> MemorySearchResponse:
    repo = _repo_or_404(db, repo_id)
    query = q.strip()
    # 用 EvidenceStore 检索记忆（query 为空就返回空列表）
    results = EvidenceStore(db).search(repo.id, None, query, limit=limit) if query else []
    # 记一条「召回事件」，方便以后审计「用户搜了什么、召回了什么」
    record_recall_event(
        db,
        repo_id=repo.id,
        conversation_id=None,
        tool_name="manual_memory_search",
        query=query,
        results=results,
        citations=[{"type": item["source_type"], "id": item["source_id"], "title": item["title"]} for item in results],
        scope="project",
        mode="hybrid",  # 混合检索（向量+关键词）
        metadata={"limit": limit, "surface": "workspace_memory"},
    )
    db.commit()
    return MemorySearchResponse(query=query, results=results, result_count=len(results))


@router.get("/{repo_id}/memory/recall-events", response_model=list[RecallEventResponse])  # GET 召回事件
async def get_recall_events(
    repo_id: uuid.UUID,
    conversation_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=30, ge=1, le=100),
    db: Session = Depends(get_db),
) -> list[RecallEventResponse]:
    _repo_or_404(db, repo_id)
    # 列出历史召回事件（哪些检索发生了）
    return [RecallEventResponse.model_validate(item) for item in list_recall_events(db, repo_id=repo_id, conversation_id=conversation_id, limit=limit)]


@router.get("/{repo_id}/memory/status", response_model=MemoryStatusResponse)  # GET 记忆状态
async def get_memory_status(repo_id: uuid.UUID, db: Session = Depends(get_db)) -> MemoryStatusResponse:
    repo = _repo_or_404(db, repo_id)
    return MemoryStatusResponse.model_validate(memory_status(db, repo))  # 记忆库统计状态


@router.get("/{repo_id}/memory/candidates", response_model=list[MemoryCandidateResponse])  # GET 记忆候选
async def get_memory_candidates(
    repo_id: uuid.UUID,
    status: str | None = Query(default="pending"),  # 默认只看待审批
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
) -> list[MemoryCandidateResponse]:
    _repo_or_404(db, repo_id)
    # 列出待人工确认的记忆候选（Agent 自动发现、但还没正式入库的内容）
    return [MemoryCandidateResponse.model_validate(item) for item in list_memory_candidates(db, repo_id=repo_id, status=status, limit=limit)]


@router.post("/{repo_id}/memory/candidates/{candidate_id}/approve", response_model=MemoryCandidateActionResponse)  # POST 批准候选
async def approve_candidate(repo_id: uuid.UUID, candidate_id: uuid.UUID, db: Session = Depends(get_db)) -> MemoryCandidateActionResponse:
    repo = _repo_or_404(db, repo_id)
    try:
        candidate, doc = approve_memory_candidate(db, repo, candidate_id)  # 正式入库
    except ValueError:
        raise HTTPException(status_code=404, detail="candidate_not_found") from None  # 找不到候选
    EvidenceStore(db).rebuild_repo(repo)  # 重建记忆索引
    rebuild_knowledge_graph(db, repo)  # 重建知识图谱
    db.commit()
    return MemoryCandidateActionResponse(id=candidate.id, status=candidate.status, document_id=doc.id if doc else None)


@router.post("/{repo_id}/memory/candidates/{candidate_id}/reject", response_model=MemoryCandidateActionResponse)  # POST 拒绝候选
async def reject_candidate(repo_id: uuid.UUID, candidate_id: uuid.UUID, db: Session = Depends(get_db)) -> MemoryCandidateActionResponse:
    repo = _repo_or_404(db, repo_id)
    try:
        candidate = reject_memory_candidate(db, repo, candidate_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="candidate_not_found") from None
    db.commit()
    return MemoryCandidateActionResponse(id=candidate.id, status=candidate.status)


@router.post("/{repo_id}/knowledge/notes", response_model=KnowledgeItemResponse)  # POST 新建记忆笔记
async def create_memory_note(
    repo_id: uuid.UUID,
    payload: MemoryNoteRequest,
    db: Session = Depends(get_db),
) -> KnowledgeItemResponse:
    _repo_or_404(db, repo_id)
    content = payload.content.strip()
    title = payload.title.strip() or "未命名记忆"  # 没标题给默认
    if not content and not title:
        raise HTTPException(status_code=400, detail="记忆标题或内容至少需要填写一项")
    source_id = uuid.uuid4()  # 生成来源 ID
    # 作为一条 memory_note 写入知识库
    doc = add_document(
        db,
        repo_id,
        "memory_note",
        source_id,
        title,
        content or title,
        {"display_title": title, "created_by": "user"},
    )
    rebuild_knowledge_graph(db, _repo_or_404(db, repo_id))  # 笔记也算图谱节点，重建
    return _group_documents([doc])[0]  # 返回刚建的那条


@router.post("/{repo_id}/knowledge/upload", response_model=KnowledgeUploadResponse)  # POST 上传知识文件
async def upload_knowledge_file(
    repo_id: uuid.UUID,
    file: UploadFile = File(...),  # File(...) 表示必传的文件
    db: Session = Depends(get_db),
) -> KnowledgeUploadResponse:
    repo = _repo_or_404(db, repo_id)
    data = await file.read(MAX_DOCUMENT_BYTES + 1)  # 多读 1 字节用来判断是否超限
    if not data:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    if len(data) > MAX_DOCUMENT_BYTES:
        raise HTTPException(status_code=400, detail="Uploaded file is too large; max size is 12 MB")

    filename = _safe_filename(file.filename or "upload.txt")
    suffix = Path(filename).suffix.lower()  # 文件后缀
    if suffix not in SUPPORTED_KNOWLEDGE_SUFFIXES:
        # 不支持的类型返回 415（媒体类型不支持）
        supported = ", ".join(sorted(SUPPORTED_KNOWLEDGE_SUFFIXES))
        raise HTTPException(status_code=415, detail=f"Unsupported file type. Supported: {supported}")
    checksum = hashlib.sha256(data).hexdigest()  # 内容指纹
    # 查是否已上传过同样的文件（去重）
    existing = (
        db.query(Document)
        .filter(Document.repo_id == repo_id, Document.source_type == "knowledge_file")
        .order_by(Document.created_at.asc())
        .all()
    )
    duplicate_docs = [doc for doc in existing if (doc.meta or {}).get("checksum") == checksum]
    if duplicate_docs:
        # 重复就直接返回已有记录，不再处理
        return KnowledgeUploadResponse(
            id=duplicate_docs[0].source_id,
            filename=str((duplicate_docs[0].meta or {}).get("filename") or filename),
            chunks=len(duplicate_docs),
            indexed=True,
            duplicate=True,
        )
    # 在线程里提取文本（不阻塞事件循环）
    text = await asyncio.to_thread(_extract_upload_text, filename, data)
    if not text.strip():
        raise HTTPException(status_code=400, detail="Could not extract text from this file")

    source_id = uuid.uuid4()
    stored_path = _upload_root(repo_id) / f"{source_id}-{filename}"  # 落盘路径
    # 文档切片（结构感知，1800 字一片、重叠 180）
    chunks = await asyncio.to_thread(chunk_document, text, filename, 1800, 180)
    if not chunks:
        raise HTTPException(status_code=400, detail="Document did not produce indexable chunks")
    # 构造每段要写入向量库的数据
    payloads = [
        DocumentPayload(
            source_type="knowledge_file",
            source_id=source_id,
            title=f"{filename}#{index + 1}",
            content=str(chunk["content"]),
            metadata={
                **{key: value for key, value in chunk.items() if key != "content" and value is not None},
                "display_title": filename,
                "filename": filename,
                "stored_path": str(stored_path),
                "content_type": file.content_type,
                "checksum": checksum,
                "file_size": len(data),
                "chunk_index": index,
                "chunk_chars": len(str(chunk["content"])),
                "total_chunks": len(chunks),
                "chunk_strategy": "structure_aware_v1",
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
                "embedding_provider": embedding_provider_name(),
                "embedding_model": settings.embedding_model,
                "embedding_dimensions": settings.embedding_dimensions,
            },
        )
        for index, chunk in enumerate(chunks)
    ]
    embedding_inputs = [f"{payload.title}\n{payload.content}" for payload in payloads]  # 向量化输入
    vectors = await asyncio.to_thread(_embed_default_in_batches, embedding_inputs)  # 批量向量化
    try:
        await asyncio.to_thread(stored_path.write_bytes, data)  # 先把原始文件存盘
        docs = add_documents(db, repo_id, payloads, commit=False, vectors=vectors)  # 写入切片+向量
        rebuild_knowledge_graph(db, repo)  # 重建图谱
    except Exception:
        db.rollback()  # 失败回滚
        try:
            stored_path.unlink(missing_ok=True)  # 删掉已落盘的文件
        except OSError:
            pass
        raise
    return KnowledgeUploadResponse(id=source_id, filename=filename, chunks=len(docs), indexed=bool(docs))


@router.get("/{repo_id}/knowledge/graph", response_model=KnowledgeGraphResponse)  # GET 知识图谱
async def get_knowledge_graph(
    repo_id: uuid.UUID,
    center_type: str | None = Query(default=None),  # 以某类节点为中心展开
    center_id: str | None = Query(default=None),
    depth: int = Query(default=1, ge=1, le=2),  # 展开层数 1~2
    db: Session = Depends(get_db),
) -> KnowledgeGraphResponse:
    repo = _repo_or_404(db, repo_id)
    # 返回图谱的节点和边（关系）
    return KnowledgeGraphResponse.model_validate(
        knowledge_graph_payload(db, repo, center_type=center_type, center_id=center_id, depth=depth)
    )


@router.post("/{repo_id}/knowledge/graph/rebuild", response_model=KnowledgeGraphRebuildResponse)  # POST 重建图谱
async def rebuild_knowledge_graph_route(repo_id: uuid.UUID, db: Session = Depends(get_db)) -> KnowledgeGraphRebuildResponse:
    repo = _repo_or_404(db, repo_id)
    result = rebuild_knowledge_graph(db, repo)  # 全量重建
    return KnowledgeGraphRebuildResponse(repo_id=repo.id, edges=result["edges"], nodes=result["nodes"])


@router.delete("/{repo_id}/knowledge/{source_id}")  # DELETE 删除知识条目
async def delete_knowledge_item(
    repo_id: uuid.UUID,
    source_id: uuid.UUID,
    source_type: str | None = Query(default=None),  # 可选按来源类型精确删
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _repo_or_404(db, repo_id)
    count = delete_document_group(db, repo_id, source_id, source_type=source_type)  # 删除该来源全部切片
    rebuild_knowledge_graph(db, _repo_or_404(db, repo_id))  # 重建图谱
    return {"repo_id": str(repo_id), "source_id": str(source_id), "deleted": count}


@router.get("/{repo_id}/team-members", response_model=list[TeamMemberResponse])  # GET 团队成员
async def list_team_members(repo_id: uuid.UUID, db: Session = Depends(get_db)) -> list[TeamMemberResponse]:
    _repo_or_404(db, repo_id)
    docs = (
        db.query(Document)
        .filter(Document.repo_id == repo_id, Document.source_type == "team_member")
        .order_by(Document.created_at.asc())
        .all()
    )
    return [_team_from_document(doc) for doc in docs]


@router.post("/{repo_id}/team-members", response_model=TeamMemberResponse)  # POST 新增成员
async def create_team_member(
    repo_id: uuid.UUID,
    payload: TeamMemberRequest,
    db: Session = Depends(get_db),
) -> TeamMemberResponse:
    _repo_or_404(db, repo_id)
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="团队成员姓名为必填项")
    # 把成员信息拼成文档内容（name/role/strengths/tech_stack 各一行）
    content = "\n".join(
        [
            f"name: {name}",
            f"role: {payload.role.strip()}",
            f"strengths: {payload.strengths.strip()}",
            f"tech_stack: {payload.tech_stack.strip()}",
        ]
    )
    # 用稳定 source_id（同名成员复用同一 ID，避免重复）
    source_id = stable_source_id(repo_id, "team_member", name.lower())
    delete_document_group(db, repo_id, source_id, source_type="team_member", commit=False)  # 先清旧的
    doc = add_document(
        db,
        repo_id,
        "team_member",
        source_id,
        f"团队成员：{name}",
        content,
        {
            "display_title": name,
            "name": name,
            "role": payload.role.strip(),
            "strengths": payload.strengths.strip(),
            "tech_stack": payload.tech_stack.strip(),
        },
    )
    rebuild_knowledge_graph(db, _repo_or_404(db, repo_id))  # 成员也进图谱
    return _team_from_document(doc)


@router.delete("/{repo_id}/team-members/{member_id}")  # DELETE 删除成员
async def delete_team_member(repo_id: uuid.UUID, member_id: uuid.UUID, db: Session = Depends(get_db)) -> dict[str, Any]:
    _repo_or_404(db, repo_id)
    count = delete_document_group(db, repo_id, member_id, source_type="team_member")  # 删除该成员文档
    rebuild_knowledge_graph(db, _repo_or_404(db, repo_id))
    return {"repo_id": str(repo_id), "member_id": str(member_id), "deleted": count}
