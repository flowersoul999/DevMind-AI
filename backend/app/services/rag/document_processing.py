"""DevMind AI module."""

import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.db.models import KnowledgeSourceDocument
from app.db.session import SessionLocal
from app.services.rag.chunking import chunk_document
from app.services.rag.document_extraction import MAX_DOCUMENT_BYTES, extract_document_text
from app.services.rag.embeddings import embed_knowledge_texts
from app.services.rag.knowledge_base import get_or_create_config
from app.services.rag.vector_store import DocumentPayload, add_documents, delete_document_group

# 知识库支持的文件后缀（上传文件类型白名单）。
SUPPORTED_KNOWLEDGE_SUFFIXES = {".txt", ".md", ".markdown", ".mdx", ".pdf", ".docx", ".json", ".csv", ".log"}
# embedding 批大小：一次最多给 64 段文本生成向量，避免单次请求过大。
EMBEDDING_BATCH_SIZE = 64


def extract_text(filename: str, data: bytes) -> str:
    # 透传：直接调用抽取模块的入口。这里包一层方便后续扩展（如加缓存、加日志）。
    return extract_document_text(filename, data)


def _embed_in_batches(
    texts: list[str],
    *,
    provider: str,
    model: str,
    dimensions: int,
) -> list[list[float]]:
    # 把文本列表按 EMBEDDING_BATCH_SIZE 分批调 embedding，再拼回完整向量列表。
    # 为什么：一次性发几千段可能超时或超出接口上限；分批既稳又能均摊负载。
    vectors: list[list[float]] = []
    for start in range(0, len(texts), EMBEDDING_BATCH_SIZE):
        vectors.extend(
            embed_knowledge_texts(
                texts[start : start + EMBEDDING_BATCH_SIZE],
                provider=provider,
                model=model,
                dimensions=dimensions,
            )
        )
    return vectors


def _set_status(document_id: uuid.UUID, status: str, *, error: str | None = None) -> None:
    # 把「知识库文档」的处理状态写回数据库（parsing / splitting / embedding / completed / failed）。
    # 用独立会话，确保状态变更即使后续步骤失败也能落库，方便前端展示进度。
    db = SessionLocal()
    try:
        document = db.get(KnowledgeSourceDocument, document_id)
        if document is None:
            return
        document.status = status
        document.error_message = error
        document.updated_at = datetime.now(timezone.utc)
        db.commit()
    finally:
        db.close()


def process_knowledge_document(document_id: uuid.UUID) -> None:
    # 处理「一个用户上传的知识库文件」的完整流水线：读文件 → 抽文本 → 切块 → 向量化 → 写库 + 写向量库。
    # 在 RAG 流水线里的位置：知识库「建库 / 增量」的核心执行单元，被后台任务调用。
    # 输入：知识库文档记录 id（文件已在磁盘 stored_path）。
    # 输出：无（副作用是把文档落进 Postgres + Milvus，并更新状态）。
    _set_status(document_id, "parsing")  # 先标记「解析中」
    db = SessionLocal()
    try:
        source = db.get(KnowledgeSourceDocument, document_id)
        if source is None:
            return  # 记录没了，放弃
        # 只读文件内容，且多读 1 字节用来判断是否超限
        with Path(source.stored_path).open("rb") as handle:
            data = handle.read(MAX_DOCUMENT_BYTES + 1)
        if len(data) > MAX_DOCUMENT_BYTES:
            raise ValueError(f"knowledge file exceeds the {MAX_DOCUMENT_BYTES} byte limit")
        text = extract_text(source.name, data)  # 抽文本
        if not text.strip():
            raise ValueError("文档中没有可索引的文本")

        # 标记「切块中」，并记录字符数
        source.status = "splitting"
        source.character_count = len(text)
        db.commit()
        config = get_or_create_config(db, source.repo_id)  # 拿该仓库的切块 / embedding 配置
        chunks = chunk_document(
            text,
            source.name,
            max_chars=config.chunk_size,    # 每片最大字符（来自配置）
            overlap=config.chunk_overlap,   # 片间重叠（来自配置）
        )
        if not chunks:
            raise ValueError("文档切分后没有有效片段")

        # 标记「向量化中」，记录碎片数
        source.status = "embedding"
        source.chunk_count = len(chunks)
        db.commit()
        # 给每个碎片构造一个「标题」：来源名 + 章节/页码 + 子序号，方便检索结果可读、可溯源
        titles = []
        for index, chunk in enumerate(chunks):
            label = chunk.get("section_title")
            if not label and chunk.get("page"):
                label = f"Page {chunk['page']}"
            label = str(label or f"Chunk {index + 1}")
            child_suffix = f" ({int(chunk.get('child_index') or 0) + 1})" if chunk.get("child_index") else ""
            titles.append(f"{source.name} · {label}{child_suffix}")
        contents = [str(chunk["content"]) for chunk in chunks]  # 碎片正文
        vectors = _embed_in_batches(
            # 把「标题 + 正文」一起编码：标题里的章节信息能提升向量语义质量
            [f"{title}\n{content}" for title, content in zip(titles, contents)],
            provider=config.embedding_provider,
            model=config.embedding_model,
            dimensions=config.embedding_dimensions,
        )
        # 组装每条碎片的「写库半成品」，元数据里塞满来源、行号、切片策略等可追溯信息
        payloads = [
            DocumentPayload(
                source_type="knowledge_file",
                source_id=source.id,
                title=title,
                content=content,
                metadata={
                    # 把碎片的其它字段（除 content、及空值外）都并入元数据
                    **{key: value for key, value in chunk.items() if key != "content" and value is not None},
                    "display_title": source.name,
                    "filename": source.name,
                    "knowledge_document_id": str(source.id),
                    "checksum": source.checksum,
                    "file_size": source.file_size,
                    "content_type": source.content_type,
                    "chunk_index": index,
                    "chunk_chars": len(content),
                    "total_chunks": len(chunks),
                    "chunk_strategy": "structure_aware_v1",  # 标记用的是「结构感知」切分策略
                },
            )
            for index, (title, content, chunk) in enumerate(zip(titles, contents, chunks))
        ]
        # 先删掉该文件旧的碎片（重新上传 / 重处理时避免重复），再写入新的
        delete_document_group(db, source.repo_id, source.id, source_type="knowledge_file", commit=False)
        add_documents(
            db,
            source.repo_id,
            payloads,
            commit=False,                # 统一在末尾提交
            vectors=vectors,
            embedding_info={
                "embedding_provider": config.embedding_provider,
                "embedding_model": config.embedding_model,
                "embedding_dimensions": config.embedding_dimensions,
            },
        )
        # 全部成功：标记完成，清空错误，记录完成时间
        source.status = "completed"
        source.error_message = None
        source.completed_at = datetime.now(timezone.utc)
        source.updated_at = source.completed_at
        db.commit()
    except Exception as exc:
        # 任何一步出错：回滚，并把状态置为 failed、记录错误（截断到 1000 字）
        db.rollback()
        source = db.get(KnowledgeSourceDocument, document_id)
        if source is not None:
            source.status = "failed"
            source.error_message = str(exc)[:1000]
            source.updated_at = datetime.now(timezone.utc)
            db.commit()
    finally:
        db.close()  # 无论如何都关掉会话
