"""记忆中心（MemoryHub）服务（memory_hub.py）。

本模块是"记忆"这一块的"收纳与统计"中枢：一方面记录"记忆/证据被 Agent 召回使用了多少次"
（RecallEvent，用于评估哪些记忆有用）；另一方面把每轮对话提炼出的"记忆候选"（决策/事实/任务/偏好等）
存成待审核条目，由人工或流程"批准/拒绝"，批准后写进向量库变成正式记忆文档。

关键函数：
- record_recall_event：记一次召回事件（记忆/证据被检索并使用了）。
- memory_status：汇总一个仓库的记忆健康度（索引状态、文档数、证据数、候选数、向量库状态）。
- capture_memory_candidates：把一轮对话的记忆更新拆成"候选条目"落库（待审核）。
- list_memory_candidates / approve_memory_candidate / reject_memory_candidate：候选的查询与审批。

与其他模块的关系：被 chat_memory.py（SessionSealer、MemoryTools）调用；写数据用 rag.vector_store.add_document。."""


# 标准库：uuid、datetime+timezone、typing.Any
import uuid
from datetime import datetime, timezone
from typing import Any

# SQLAlchemy：func 提供 count 等聚合函数
from sqlalchemy import func
from sqlalchemy.orm import Session

# 数据表模型
from app.db.models import (
    Document,         # 向量库文档（记忆批准后写入这里）
    EvidenceItem,     # 证据
    MemoryCandidate,  # 记忆候选（待审核）
    ProjectIndexState,  # 项目索引状态
    RecallEvent,      # 召回事件
    Repository,       # 仓库
)
# 向量库：add_document 把一条内容加进向量索引；milvus_runtime_status 取向量库运行状态
from app.services.rag.vector_store import add_document, milvus_runtime_status


def record_recall_event(
    db: Session,
    *,
    repo_id: uuid.UUID | None,
    conversation_id: uuid.UUID | None,
    tool_name: str,
    query: str,
    results: list[dict[str, Any]] | None = None,
    citations: list[dict[str, Any]] | None = None,
    scope: str = "project",
    mode: str = "hybrid",
    status: str = "success",
    metadata: dict[str, Any] | None = None,
) -> RecallEvent:
    # 记录一次"召回事件"：某个工具（如搜索证据）用某个查询召回了哪些结果。
    # 为什么：记下这些能统计"哪些记忆/证据真的被用到了"，用来评估记忆质量和做调试。
    safe_results = [_recall_result_payload(item) for item in (results or [])[:12]]  # 结果最多存 12 条，避免过大
    event = RecallEvent(
        repo_id=repo_id,
        conversation_id=conversation_id,
        tool_name=tool_name,  # 哪个工具触发的（如 devflow_search_evidence）
        query=query.strip(),
        scope=scope,          # 作用域：会话级 or 项目级
        mode=mode,            # 检索模式：hybrid 等
        status=status,        # 成功/失败
        result_count=len(results or []),
        results_json=safe_results,
        citations_json=(citations or [])[:12],  # 引用同样限 12
        meta=metadata or {},
    )
    db.add(event)
    db.flush()
    return event


def list_recall_events(
    db: Session,
    *,
    repo_id: uuid.UUID,
    conversation_id: uuid.UUID | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    # 列出最近的召回事件（默认按时间倒序，最多 100 条）。可按会话过滤。
    query = db.query(RecallEvent).filter(RecallEvent.repo_id == repo_id)
    if conversation_id:
        query = query.filter(RecallEvent.conversation_id == conversation_id)
    rows = query.order_by(RecallEvent.created_at.desc()).limit(min(max(limit, 1), 100)).all()  # 限制条数范围 [1,100]
    return [_recall_event_payload(row) for row in rows]


def memory_status(db: Session, repo: Repository) -> dict[str, Any]:
    # 汇总一个仓库的"记忆健康度"快照：索引状态、文档/证据/候选数量、最近召回、向量库状态。
    # 这是给前端"记忆中心"页面展示用的总览数据。
    index_state = db.query(ProjectIndexState).filter(ProjectIndexState.repo_id == repo.id).one_or_none()
    source_counts = dict(
        # 按来源类型统计文档数（group_by + count）
        db.query(Document.source_type, func.count(Document.id))
        .filter(Document.repo_id == repo.id)
        .group_by(Document.source_type)
        .all()
    )
    evidence_counts = dict(
        # 按来源类型统计证据数
        db.query(EvidenceItem.source_type, func.count(EvidenceItem.id))
        .filter(EvidenceItem.repo_id == repo.id)
        .group_by(EvidenceItem.source_type)
        .all()
    )
    candidate_counts = dict(
        # 按状态统计记忆候选数（pending/approved/rejected）
        db.query(MemoryCandidate.status, func.count(MemoryCandidate.id))
        .filter(MemoryCandidate.repo_id == repo.id)
        .group_by(MemoryCandidate.status)
        .all()
    )
    latest_recall = (
        # 最近一次召回事件（看"记忆最近有没有被用"）
        db.query(RecallEvent)
        .filter(RecallEvent.repo_id == repo.id)
        .order_by(RecallEvent.created_at.desc())
        .first()
    )
    return {
        "repo_id": repo.id,
        "index": {
            # 项目索引状态（没有就标 missing）
            "status": index_state.status if index_state else "missing",
            "docs_indexed": index_state.docs_indexed if index_state else 0,
            "docs_total": index_state.docs_total if index_state else 0,
            "last_scan_at": index_state.last_scan_at.isoformat() if index_state and index_state.last_scan_at else None,
            "error_message": index_state.error_message if index_state else None,
            "summary_json": index_state.summary_json if index_state else None,
        },
        "documents": {
            "total": sum(int(value) for value in source_counts.values()),
            "by_source_type": source_counts,
        },
        "evidence": {
            "total": sum(int(value) for value in evidence_counts.values()),
            "by_source_type": evidence_counts,
        },
        "recall": {
            "total": db.query(RecallEvent).filter(RecallEvent.repo_id == repo.id).count(),
            "latest_at": latest_recall.created_at.isoformat() if latest_recall and latest_recall.created_at else None,
        },
        "candidates": {
            "total": sum(int(value) for value in candidate_counts.values()),
            "by_status": candidate_counts,
            "pending": int(candidate_counts.get("pending") or 0),  # 待审核数量（前端常关注）
        },
        "vector": milvus_runtime_status(),  # 向量库当前运行状态
    }


def capture_memory_candidates(
    db: Session,
    *,
    repo_id: uuid.UUID | None,
    conversation_id: uuid.UUID | None,
    session_id: uuid.UUID | None,
    memory_update: dict[str, Any],
) -> list[MemoryCandidate]:
    # 把一轮对话的"记忆更新"拆成候选条目落库，供后续人工/自动审核。
    # 为什么：记忆不该自动无条件写入正式库，先存成 pending 候选，审过再变成正式记忆，更安全可控。
    if repo_id is None:
        return []  # 没有仓库就什么都不存（记忆是挂在仓库下的）
    specs = [
        # 把记忆更新里的各类字段映射成 (种类, 内容列表)
        ("decision", memory_update.get("decisions") or []),
        ("fact", memory_update.get("facts") or []),
        ("task", memory_update.get("tasks") or []),
        ("preference", memory_update.get("user_preferences") or []),
        ("repo_context", memory_update.get("repo_context") or []),
    ]
    created: list[MemoryCandidate] = []
    for kind, values in specs:
        for value in values:
            content = str(value or "").strip()
            if not content or _candidate_exists(db, repo_id, kind, content):
                continue  # 空内容或已存在同款候选就跳过（去重）
            candidate = MemoryCandidate(
                repo_id=repo_id,
                conversation_id=conversation_id,
                session_id=session_id,
                kind=kind,  # 候选种类
                title=_candidate_title(kind, content),  # 自动生成标题
                content=content,
                status="pending",  # 初始状态：待审核
                source="session_sealer",  # 来源：会话封口器
                meta={"memory_update_key": kind, "captured_at": datetime.now(timezone.utc).isoformat()},
            )
            db.add(candidate)
            created.append(candidate)
    if created:
        db.flush()  # 有新建才刷库
    return created


def list_memory_candidates(
    db: Session,
    *,
    repo_id: uuid.UUID,
    status: str | None = "pending",
    limit: int = 50,
) -> list[dict[str, Any]]:
    # 列出记忆候选。status 默认 "pending"（待审核）；传 "all" 看全部。条数范围 [1,100]。
    query = db.query(MemoryCandidate).filter(MemoryCandidate.repo_id == repo_id)
    if status and status != "all":
        query = query.filter(MemoryCandidate.status == status)
    rows = query.order_by(MemoryCandidate.created_at.desc()).limit(min(max(limit, 1), 100)).all()
    return [_candidate_payload(row) for row in rows]


def approve_memory_candidate(db: Session, repo: Repository, candidate_id: uuid.UUID) -> tuple[MemoryCandidate, Document | None]:
    # 批准一个记忆候选：把它写进向量库变成正式"记忆文档"，并把候选状态置为 approved。
    candidate = _candidate_or_none(db, repo.id, candidate_id)
    if candidate is None:
        raise ValueError("candidate_not_found")  # 找不到就抛错（调用方要处理）
    if candidate.status == "approved":
        return candidate, None  # 已批准过：直接返回，避免重复写入
    doc = add_document(
        # 加进向量库（commit=False：由调用方统一提交事务）
        db,
        repo.id,
        "memory_note",  # 来源类型：记忆笔记
        uuid.uuid4(),    # 生成一个 source_id
        candidate.title,
        candidate.content,
        {
            "display_title": candidate.title,
            "candidate_id": str(candidate.id),
            "candidate_kind": candidate.kind,
            "source": "approved_memory_candidate",
            **(candidate.meta or {}),  # 把候选的原 meta 一并带过去
        },
        commit=False,
    )
    candidate.status = "approved"  # 候选变成已批准
    candidate.reviewed_at = datetime.now(timezone.utc)  # 记录审核时间
    candidate.meta = {**(candidate.meta or {}), "approved_document_id": str(doc.id)}  # 记下对应的文档 ID
    db.flush()
    return candidate, doc


def reject_memory_candidate(db: Session, repo: Repository, candidate_id: uuid.UUID) -> MemoryCandidate:
    # 拒绝一个记忆候选：状态置为 rejected（不写入向量库）。软处理，保留记录可追溯。
    candidate = _candidate_or_none(db, repo.id, candidate_id)
    if candidate is None:
        raise ValueError("candidate_not_found")
    candidate.status = "rejected"
    candidate.reviewed_at = datetime.now(timezone.utc)
    db.flush()
    return candidate


def _candidate_or_none(db: Session, repo_id: uuid.UUID, candidate_id: uuid.UUID) -> MemoryCandidate | None:
    # 内部：按仓库+ID 查一个候选，没有返回 None（避免到处写重复查询）
    return (
        db.query(MemoryCandidate)
        .filter(MemoryCandidate.repo_id == repo_id, MemoryCandidate.id == candidate_id)
        .one_or_none()
    )


def _candidate_exists(db: Session, repo_id: uuid.UUID, kind: str, content: str) -> bool:
    # 内部：判断"同仓库+同种类+同内容+未处理(pending/approved)"的候选是否已存在，用于去重。
    return (
        db.query(MemoryCandidate.id)
        .filter(
            MemoryCandidate.repo_id == repo_id,
            MemoryCandidate.kind == kind,
            MemoryCandidate.content == content,
            MemoryCandidate.status.in_(["pending", "approved"]),
        )
        .first()
        is not None  # first() 有结果说明存在
    )


def _candidate_title(kind: str, content: str) -> str:
    # 内部：根据种类生成中文标题前缀（如"决策：…"），内容截断到 80 字。
    labels = {
        "decision": "决策",
        "fact": "事实",
        "task": "任务",
        "preference": "偏好",
        "repo_context": "项目上下文",
    }
    return f"{labels.get(kind, kind)}：{_clip(content, 80)}"


def _recall_result_payload(item: dict[str, Any]) -> dict[str, Any]:
    # 内部：把一个召回结果压缩成可存储的字典（片段截 260 字）
    return {
        "title": str(item.get("title") or ""),
        "source_type": str(item.get("source_type") or ""),
        "source_id": str(item.get("source_id") or ""),
        "score": item.get("score"),
        "snippet": _clip(str(item.get("snippet") or ""), 260),
        "metadata": item.get("metadata") or {},
        "retrieval": item.get("retrieval") or {},
    }


def _recall_event_payload(row: RecallEvent) -> dict[str, Any]:
    # 内部：把数据库召回事件行转成 API 友好的字典
    return {
        "id": row.id,
        "repo_id": row.repo_id,
        "conversation_id": row.conversation_id,
        "tool_name": row.tool_name,
        "query": row.query,
        "scope": row.scope,
        "mode": row.mode,
        "status": row.status,
        "result_count": row.result_count,
        "results": row.results_json or [],
        "citations": row.citations_json or [],
        "metadata": row.meta or {},
        "created_at": row.created_at,
    }


def _candidate_payload(row: MemoryCandidate) -> dict[str, Any]:
    # 内部：把记忆候选行转成 API 友好的字典
    return {
        "id": row.id,
        "repo_id": row.repo_id,
        "conversation_id": row.conversation_id,
        "session_id": row.session_id,
        "kind": row.kind,
        "title": row.title,
        "content": row.content,
        "status": row.status,
        "source": row.source,
        "metadata": row.meta or {},
        "created_at": row.created_at,
        "reviewed_at": row.reviewed_at,
    }


def _clip(text: str, limit: int) -> str:
    # 内部：把文本按"词"截断到 limit 字，超出加"..."（和别处的 _clip 思路一致，但用单空格归一化）
    cleaned = " ".join((text or "").split())  # 多个空白压成单个空格
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1].rstrip() + "..."
