"""DevMind AI module."""

import json
import logging
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import Document
from app.services.rag.embeddings import embed_text, embed_texts, embedding_contract, embedding_provider_name
from app.services.rag.source_policy import policy_metadata, should_vectorize

logger = logging.getLogger(__name__)


class MilvusUnavailableError(RuntimeError):
    """MilvusUnavailableError."""
    # 自定义异常：向量库（Milvus）不可用 / 未启用时抛出，让上层能据此走降级逻辑。
    pass


try:  # pymilvus 在测试和本地演示模式中是可选依赖。
    # 为什么：有些环境跑 demo 不装 Milvus，用 try/except 把「缺依赖」变成可控降级，而不是一启动就崩。
    from pymilvus import DataType, MilvusClient
except Exception:  # pragma: no cover - 依赖缺失时会走到这里。
    DataType = None  # type: ignore[assignment]
    MilvusClient = None  # type: ignore[assignment]


@dataclass
class DocumentPayload:
    # 写库用的「半成品」：先把要存的内容打包成这个结构，再由 add_documents 转成真正的 ORM 对象。
    source_type: str
    title: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    source_id: uuid.UUID | str | None = None


def _as_uuid(value: uuid.UUID | str | None) -> uuid.UUID:
    # 把字符串 / None 统一成 uuid.UUID；None 会随机生成一个新的。
    if value is None:
        return uuid.uuid4()
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def stable_source_id(repo_id: uuid.UUID | str, source_type: str, key: str) -> uuid.UUID:
    # 生成「稳定的」来源 id：同样输入永远得到同样 uuid（用 uuid5 + 固定命名空间）。
    # 为什么：让同一来源（如某个 Issue）在多次同步时拿到同一个 id，方便增量替换而非重复插入。
    return uuid.uuid5(uuid.NAMESPACE_URL, f"devflow:{repo_id}:{source_type}:{key}")


def _escape_filter_value(value: str) -> str:
    # 转义 Milvus 过滤表达式里的特殊字符（反斜杠、双引号），防止注入 / 语法错误。
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _clip_utf8(value: str, max_bytes: int) -> str:
    # 按 UTF-8 字节数裁剪字符串：Milvus 字段有最大长度限制，超了会写不进去。
    # 按字节裁而不是按字符，是因为中文一个字占 3 字节，按字符裁仍可能超字节上限。
    data = value.encode("utf-8")
    if len(data) <= max_bytes:
        return value
    return data[:max_bytes].decode("utf-8", errors="ignore")  # 解码允许忽略半截字符


def _json_dumps(value: dict[str, Any], max_chars: int = 4096) -> str:
    # 把元数据字典序列化成 JSON 字符串，并按最大字符数裁剪，确保能塞进 Milvus 的 VARCHAR 字段。
    text = json.dumps(value or {}, ensure_ascii=False, default=str)
    return _clip_utf8(text, max_chars)


class MilvusVectorStore:
    def __init__(self) -> None:
        if MilvusClient is None or DataType is None:
            raise RuntimeError("pymilvus is not installed")  # 依赖没装就明确报错
        self.collection_name = settings.milvus_collection
        self.dimensions = settings.embedding_dimensions
        # 连接 Milvus，可能抛连接异常（被上层包装成 MilvusUnavailableError）
        self.client = MilvusClient(uri=settings.milvus_uri, token=settings.milvus_token or "")
        self._ensure_collection()  # 启动时保证集合存在 / 维度一致

    def _ensure_collection(self) -> None:
        # 确保向量集合存在且维度正确。已有集合就校验维度；没有就新建带 schema 的集合。
        if self.client.has_collection(self.collection_name):
            description = self.client.describe_collection(self.collection_name)
            actual_dimensions = _collection_dimensions(description)
            if actual_dimensions != self.dimensions:
                raise RuntimeError(
                    f"Milvus collection '{self.collection_name}' has dimension {actual_dimensions}; "
                    f"the configured embedding contract requires {self.dimensions}. "
                    "Use a new collection name and rebuild the index."
                )
            return

        # 新建集合的字段定义：主键 + 各种过滤字段 + 内容 + 元数据 + 向量
        schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=True)
        schema.add_field(field_name="pk", datatype=DataType.VARCHAR, is_primary=True, max_length=64)
        schema.add_field(field_name="repo_id", datatype=DataType.VARCHAR, max_length=64)
        schema.add_field(field_name="document_id", datatype=DataType.VARCHAR, max_length=64)
        schema.add_field(field_name="source_type", datatype=DataType.VARCHAR, max_length=80)
        schema.add_field(field_name="source_id", datatype=DataType.VARCHAR, max_length=64)
        schema.add_field(field_name="title", datatype=DataType.VARCHAR, max_length=500)
        schema.add_field(field_name="content", datatype=DataType.VARCHAR, max_length=8192)
        schema.add_field(field_name="metadata_json", datatype=DataType.VARCHAR, max_length=4096)
        # 核心：向量字段，维度必须和 embedding 配置一致
        schema.add_field(field_name="embedding", datatype=DataType.FLOAT_VECTOR, dim=self.dimensions)

        # 建索引：向量用 AUTOINDEX + 余弦相似度（COSINE）度量语义接近程度
        index_params = self.client.prepare_index_params()
        index_params.add_index(field_name="embedding", index_type="AUTOINDEX", metric_type="COSINE")
        self.client.create_collection(
            collection_name=self.collection_name,
            schema=schema,
            index_params=index_params,
        )

    def upsert_documents(self, documents: list[Document], vectors: list[list[float]]) -> None:
        # 把一批文档「插入或更新」进 Milvus。Milvus 的 upsert 按主键去重：主键相同即覆盖。
        if len(documents) != len(vectors):
            raise ValueError("document/vector count mismatch")  # 文档和向量必须一一对应
        rows = []
        for doc, vector in zip(documents, vectors):
            if not should_vectorize(doc.source_type):
                continue  # 不该向量化的来源跳过（直接检索类不进向量库）
            if len(vector) != self.dimensions:
                raise ValueError(
                    f"document {doc.id} has {len(vector)} embedding dimensions; expected {self.dimensions}"
                )
            rows.append(
                {
                    "pk": str(doc.id),                                          # 主键 = 文档 id
                    "repo_id": str(doc.repo_id),
                    "document_id": str(doc.id),
                    "source_type": doc.source_type,
                    "source_id": str(doc.source_id),
                    "title": _clip_utf8(doc.title, 500),                        # 标题裁到 500 字节
                    "content": _clip_utf8(doc.content or "", 8192),             # 正文裁到 8192 字节
                    "metadata_json": _json_dumps(doc.meta or {}),               # 元数据 JSON 化
                    "embedding": vector,                                        # 向量本体
                }
            )
        if rows:
            # 真正写入；update 语义保证重复主键会被覆盖，便于重索引
            self.client.upsert(collection_name=self.collection_name, data=rows)

    def delete_documents(
        self,
        *,
        repo_id: uuid.UUID | str | None = None,
        source_types: list[str] | None = None,
        source_id: uuid.UUID | str | None = None,
        document_ids: list[uuid.UUID | str] | None = None,
    ) -> None:
        # 按条件从 Milvus 删除文档：用过滤表达式（expr）描述「删哪些」。
        # 类比：像 SQL 的 DELETE WHERE，不过是 Milvus 自己的表达式语法。
        expressions: list[str] = []
        if document_ids:
            ids = ", ".join(f'"{_escape_filter_value(str(item))}"' for item in document_ids)
            expressions.append(f"document_id in [{ids}]")  # 按主键集合删
        if repo_id is not None:
            expressions.append(f'repo_id == "{_escape_filter_value(str(repo_id))}"')  # 按仓库删
        if source_types:
            values = ", ".join(f'"{_escape_filter_value(item)}"' for item in source_types)
            expressions.append(f"source_type in [{values}]")  # 按来源类型集合删
        if source_id is not None:
            expressions.append(f'source_id == "{_escape_filter_value(str(source_id))}"')
        if expressions:
            # 多个条件用 and 连接，组成完整过滤表达式
            self.client.delete(collection_name=self.collection_name, filter=" and ".join(expressions))

    def search(
        self,
        *,
        repo_id: uuid.UUID | str,
        query_embedding: list[float],
        source_type: str | None = None,
        source_types: list[str] | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        # 向量检索：给定「查询向量」，在指定仓库（及来源类型）里找最相似的 limit 条。
        # 为什么：这是 RAG「语义召回」的核心——把问题向量和所有文档向量比余弦相似度，取最近的。
        expressions = [f'repo_id == "{_escape_filter_value(str(repo_id))}"']  # 先锁定仓库
        if source_type:
            expressions.append(f'source_type == "{_escape_filter_value(source_type)}"')
        elif source_types:
            values = ", ".join(f'"{_escape_filter_value(item)}"' for item in source_types)
            expressions.append(f"source_type in [{values}]")
        results = self.client.search(
            collection_name=self.collection_name,
            data=[query_embedding],  # 要查的向量（单次一条）
            filter=" and ".join(expressions),
            limit=limit,
            output_fields=["document_id", "source_type", "source_id", "title", "content", "metadata_json"],
        )
        hits = results[0] if results else []  # Milvus 返回的是「每查询一个结果列表」，这里取第一条查询的结果
        output: list[dict[str, Any]] = []
        for hit in hits:
            entity = hit.get("entity") or {}
            metadata_text = entity.get("metadata_json") or "{}"
            try:
                metadata = json.loads(metadata_text)  # 把元数据 JSON 解析回字典
            except json.JSONDecodeError:
                metadata = {}
            output.append(
                {
                    "id": str(entity.get("document_id") or hit.get("id")),
                    "title": str(entity.get("title") or ""),
                    "source_type": str(entity.get("source_type") or ""),
                    "source_id": str(entity.get("source_id") or ""),
                    "score": round(float(hit.get("distance") or 0.0), 4),  # 距离转成相似度分数
                    "metadata": metadata,
                    "snippet": str(entity.get("content") or "")[:260],     # 摘要：正文前 260 字
                    "_raw_content": str(entity.get("content") or ""),      # 完整正文（内部用）
                }
            )
        return output


def _collection_dimensions(description: dict[str, Any]) -> int:
    # 从集合描述里读出 embedding 字段的维度。
    for schema_field in description.get("fields") or []:
        if schema_field.get("name") != "embedding":
            continue
        params = schema_field.get("params") or schema_field.get("type_params") or {}
        value = params.get("dim") or schema_field.get("dim")
        if value is not None:
            return int(value)
    raise RuntimeError("Milvus collection does not expose an embedding dimension")


@lru_cache(maxsize=1)
def _milvus_store() -> MilvusVectorStore:
    # 缓存单个 MilvusVectorStore 实例（连接较重，全局复用）。
    if not settings.milvus_enabled:
        raise MilvusUnavailableError("Milvus is disabled; vector and hybrid RAG are unavailable")
    try:
        return MilvusVectorStore()
    except Exception as exc:  # pragma: no cover - 依赖本地基础设施。
        raise MilvusUnavailableError(f"Milvus is unavailable: {exc}") from exc


def reset_milvus_store_cache() -> None:
    # 清空上面的缓存，迫使下次重新连接（配置变更 / 测试后重置用）。
    _milvus_store.cache_clear()


def milvus_runtime_status() -> dict[str, Any]:
    # 汇报向量库运行时状态：可用还是降级，以及当前 embedding 合约。
    try:
        store = _milvus_store()
    except MilvusUnavailableError as exc:
        return {
            "backend": "milvus",
            "status": "degraded",   # 降级（不可用）
            "available": False,
            "error": str(exc),
            "collection": settings.milvus_collection,
            "embedding": embedding_contract(),
        }
    return {
        "backend": "milvus",
        "status": "ready",
        "available": True,
        "error": None,
        "collection": store.collection_name,
        "embedding": embedding_contract(),
    }


def upsert_milvus_documents(documents: list[Document], vectors: list[list[float]]) -> bool:
    # 对外的「写向量」入口：先做维度校验，再过滤掉不该向量化的，最后交给 store 写入。
    if len(documents) != len(vectors):
        raise ValueError("document/vector count mismatch")
    for vector in vectors:
        if len(vector) != settings.embedding_dimensions:
            raise ValueError(
                f"embedding has {len(vector)} dimensions; expected {settings.embedding_dimensions}. "
                "Refusing to truncate or pad vectors."  # 绝不允许改维度
            )
    # 只保留 should_vectorize 为 True 的文档 + 对应向量
    pairs = [(doc, vector) for doc, vector in zip(documents, vectors) if should_vectorize(doc.source_type)]
    eligible_documents = [item[0] for item in pairs]
    eligible_vectors = [item[1] for item in pairs]
    if not eligible_documents:
        return True  # 没有需要写的，算成功
    store = _milvus_store()
    try:
        store.upsert_documents(eligible_documents, eligible_vectors)
    except Exception as exc:  # pragma: no cover - 依赖本地基础设施。
        raise MilvusUnavailableError("failed to write documents to Milvus") from exc
    return True


def delete_milvus_documents(
    *,
    repo_id: uuid.UUID | str | None = None,
    source_types: list[str] | None = None,
    source_id: uuid.UUID | str | None = None,
    document_ids: list[uuid.UUID | str] | None = None,
    strict: bool = True,
) -> bool:
    # 对外的「删向量」入口：strict=True 时出错就抛，False 时只告警、返回 False（降级友好）。
    try:
        store = _milvus_store()
    except MilvusUnavailableError:
        if strict:
            raise
        return False
    try:
        store.delete_documents(repo_id=repo_id, source_types=source_types, source_id=source_id, document_ids=document_ids)
    except Exception as exc:  # pragma: no cover - 依赖本地基础设施。
        if not strict:
            logger.warning("从 Milvus 删除文档失败：%s", exc)
            return False
        raise MilvusUnavailableError("failed to delete documents from Milvus") from exc
    return True


def search_milvus_documents(
    repo_id: str | uuid.UUID,
    query: str,
    source_type: str | None = None,
    limit: int = 20,
    query_embedding: list[float] | None = None,
    source_types: list[str] | None = None,
) -> list[dict[str, Any]]:
    # 对外的「向量检索」入口：没传查询向量就现场用配置 provider 把 query 文本编码成向量。
    store = _milvus_store()
    try:
        vector = query_embedding or embed_text(query, settings.embedding_dimensions)
        return store.search(
            repo_id=repo_id,
            query_embedding=vector,
            source_type=source_type,
            source_types=source_types,
            limit=limit,
        )
    except Exception as exc:  # pragma: no cover - 依赖本地基础设施。
        raise MilvusUnavailableError("failed to search documents in Milvus") from exc


def add_document(
    db: Session,
    repo_id: str | uuid.UUID,
    source_type: str,
    source_id: str | uuid.UUID | None,
    title: str,
    content: str,
    metadata: dict[str, Any] | None = None,
    *,
    commit: bool = True,
) -> Document:
    # 单条写入：落 Postgres（Document 表）并（若启用）同步写 Milvus。
    if source_type == "code_file":
        raise ValueError("源码必须通过工作区实时搜索，不能保存为 RAG Document")  # 源码不走 RAG 库
    vectorized = should_vectorize(source_type)
    vectors = [embed_text(f"{title}\n{content}", settings.embedding_dimensions)] if vectorized else []
    embedding_metadata = (
        {
            "embedding_provider": embedding_provider_name(),
            "embedding_model": settings.embedding_model,
            "embedding_dimensions": settings.embedding_dimensions,
        }
        if vectorized
        else {}
    )
    doc = Document(
        repo_id=_as_uuid(repo_id),
        source_type=source_type,
        source_id=_as_uuid(source_id),
        title=title[:500],
        content=content,
        meta={
            **(metadata or {}),
            **policy_metadata(source_type),   # 附带检索策略元数据
            **embedding_metadata,
        },
    )
    db.add(doc)
    db.flush()  # 先刷出拿到主键 id，方便下面写向量
    if vectorized and settings.milvus_enabled:
        upsert_milvus_documents([doc], vectors)  # 同步写向量库
    if commit:
        db.commit()
        db.refresh(doc)
    return doc


def add_documents(
    db: Session,
    repo_id: str | uuid.UUID,
    payloads: list[DocumentPayload],
    *,
    commit: bool = True,
    vectors: list[list[float]] | None = None,
    embedding_info: dict[str, Any] | None = None,
    generate_embeddings: bool = True,
    sync_vector_store: bool = True,
) -> list[Document]:
    # 批量写入：把一批 DocumentPayload 转成真正的 Document 并落库 +（可选）写向量库。
    # 是建库时最核心的写入口；支持「外部已给向量」或「内部现场生成」。
    clean_payloads = [payload for payload in payloads if payload.content.strip()]  # 丢掉空内容
    if any(payload.source_type == "code_file" for payload in clean_payloads):
        raise ValueError("源码必须通过工作区实时搜索，不能保存为 RAG Document")
    if vectors is not None and len(vectors) != len(clean_payloads):
        raise ValueError("embedding count does not match document payload count")

    embeddings: list[list[float] | None] = [None] * len(clean_payloads)  # 先占位 None
    # 找出哪些 payload 需要向量化
    vector_indexes = [
        index for index, payload in enumerate(clean_payloads) if should_vectorize(payload.source_type)
    ]
    if vectors is not None:
        # 外部已给向量：直接对号入座
        for index in vector_indexes:
            embeddings[index] = vectors[index]
    elif vector_indexes and generate_embeddings:
        # 没给向量但要生成：只对需要向量化的那批统一编码，省算力
        generated = embed_texts(
            [f"{clean_payloads[index].title}\n{clean_payloads[index].content}" for index in vector_indexes],
            settings.embedding_dimensions,
        )
        if len(generated) != len(vector_indexes):
            raise ValueError("embedding count does not match vectorized document payload count")
        for index, embedding in zip(vector_indexes, generated):
            embeddings[index] = embedding
    if sync_vector_store and any(embeddings[index] is None for index in vector_indexes):
        raise ValueError("RAG documents require embeddings before Milvus synchronization")  # 要同步向量库就必须先有向量

    # 逐个构造 ORM 对象
    docs: list[Document] = []
    for payload, embedding in zip(clean_payloads, embeddings):
        vectorized = embedding is not None
        embedding_metadata = (
            {
                "embedding_provider": embedding_provider_name(),
                "embedding_model": settings.embedding_model,
                "embedding_dimensions": settings.embedding_dimensions,
                **(embedding_info or {}),
            }
            if vectorized
            else {}
        )
        docs.append(
            Document(
                repo_id=_as_uuid(repo_id),
                source_type=payload.source_type,
                source_id=_as_uuid(payload.source_id),
                title=payload.title[:500],
                content=payload.content,
                meta={
                    **(payload.metadata or {}),
                    **policy_metadata(payload.source_type),
                    **embedding_metadata,
                },
            )
        )
    if not docs:
        return []
    db.add_all(docs)
    db.flush()  # 刷出主键
    if sync_vector_store and settings.milvus_enabled:
        # 只把需要向量化的文档 + 向量交给 Milvus
        vector_documents = [docs[index] for index in vector_indexes]
        vector_values = [embeddings[index] for index in vector_indexes]
        upsert_milvus_documents(
            vector_documents,
            [vector for vector in vector_values if vector is not None],
        )
    if commit:
        db.commit()
        for doc in docs:
            db.refresh(doc)
    return docs


def replace_documents(
    db: Session,
    repo_id: str | uuid.UUID,
    source_types: list[str],
    payloads: list[DocumentPayload],
    *,
    commit: bool = True,
    vectors: list[list[float]] | None = None,
    embedding_info: dict[str, Any] | None = None,
    generate_embeddings: bool = True,
    sync_vector_store: bool = True,
) -> list[Document]:
    # 「替换式」写入：先把该仓库指定来源类型的旧文档从 Postgres + Milvus 清掉，再写新的。
    # 为什么：同步索引本质是「全量覆盖」，先删后写能保证不残留旧数据。
    repo_uuid = _as_uuid(repo_id)
    db.query(Document).filter(Document.repo_id == repo_uuid, Document.source_type.in_(source_types)).delete(
        synchronize_session=False  # 不同步会话里已加载对象的状态，提升删除效率
    )
    db.flush()
    if sync_vector_store and settings.milvus_enabled:
        delete_milvus_documents(repo_id=repo_uuid, source_types=source_types)
    docs = add_documents(
        db,
        repo_uuid,
        payloads,
        commit=False,  # 由本函数统一提交
        vectors=vectors,
        embedding_info=embedding_info,
        generate_embeddings=generate_embeddings,
        sync_vector_store=sync_vector_store,
    )
    if commit:
        db.commit()
    return docs


def replace_documents_by_metadata(
    db: Session,
    repo_id: str | uuid.UUID,
    source_type: str,
    metadata_filters: dict[str, Any],
    payloads: list[DocumentPayload],
    *,
    commit: bool = True,
) -> list[Document]:
    # 按「元数据条件」替换：只删掉元数据匹配的旧文档，再写入新的。粒度比 replace_documents 更细。
    repo_uuid = _as_uuid(repo_id)
    rows = db.query(Document).filter(Document.repo_id == repo_uuid, Document.source_type == source_type).all()
    # 找出元数据全部匹配过滤条件的旧文档 id
    document_ids = [
        row.id
        for row in rows
        if all((row.meta or {}).get(key) == value for key, value in metadata_filters.items())
    ]
    if document_ids:
        db.query(Document).filter(Document.id.in_(document_ids)).delete(synchronize_session=False)
        if should_vectorize(source_type) and settings.milvus_enabled:
            delete_milvus_documents(repo_id=repo_uuid, document_ids=document_ids)
    db.flush()
    docs = add_documents(db, repo_uuid, payloads, commit=False)
    if commit:
        db.commit()
    return docs


def delete_document_group(
    db: Session,
    repo_id: str | uuid.UUID,
    source_id: str | uuid.UUID,
    source_type: str | None = None,
    *,
    commit: bool = True,
) -> int:
    # 删除「同一来源 id 下的一组文档」（如某个文件、某个 Issue 的全部碎片）。
    repo_uuid = _as_uuid(repo_id)
    source_uuid = _as_uuid(source_id)
    query = db.query(Document).filter(Document.repo_id == repo_uuid, Document.source_id == source_uuid)
    if source_type:
        query = query.filter(Document.source_type == source_type)
    document_ids = [row.id for row in query.all()]
    count = query.delete(synchronize_session=False)  # 返回删除条数
    # 同步从 Milvus 删对应向量（若该来源需要向量化）
    if settings.milvus_enabled and (source_type is None or should_vectorize(source_type)):
        delete_milvus_documents(repo_id=repo_uuid, source_id=source_uuid, document_ids=document_ids)
    if commit:
        db.commit()
    return count
