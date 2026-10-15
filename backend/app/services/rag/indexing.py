"""DevMind AI module."""

import re

from sqlalchemy.orm import Session, selectinload

from app.db.models import Issue, KnowledgeBaseConfig, PullRequest, Repository, WorkflowRun
from app.services.rag.embeddings import embed_knowledge_texts
from app.services.rag.vector_store import DocumentPayload, replace_documents

# 走 RAG 的、随仓库同步索引的来源类型：Issue / PR / CI 运行记录。
SYNCED_REPO_SOURCE_TYPES = ["issue", "pull_request", "workflow_run"]
CI_LOG_CHUNK_CHARS = 8000     # 单条 CI 日志块最大字符数
CI_LOG_MAX_CHUNKS = 6         # 一条 CI 日志最多切成 6 块（防止超长日志刷屏）


def sanitize_ci_log(text: str) -> str:
    # 清洗 CI 日志里的「秘密信息」：私钥、GitHub Token、AWS Key、JWT、Bearer、add-mask、常见密钥字段全部打码。
    # 为什么：CI 日志极易泄漏密钥，入库前必须脱敏，否则会把凭证写进知识库。
    sanitized = re.sub(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        "[REDACTED_PRIVATE_KEY]",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    sanitized = re.sub(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b", "[REDACTED_GITHUB_TOKEN]", sanitized)
    sanitized = re.sub(r"\bAKIA[A-Z0-9]{16}\b", "[REDACTED_AWS_ACCESS_KEY]", sanitized)
    sanitized = re.sub(
        r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b",
        "[REDACTED_JWT]",
        sanitized,
    )
    sanitized = re.sub(
        r"(?i)\b(authorization\s*:\s*bearer)\s+[^\s,;]+",
        r"\1 [REDACTED]",
        sanitized,
    )
    sanitized = re.sub(r"(?i)(::add-mask::)[^\r\n]+", r"\1[REDACTED]", sanitized)
    return re.sub(
        r"(?i)\b([a-z0-9_]*(?:api[_-]?key|access[_-]?key|access[_-]?token|auth[_-]?token|"
        r"token|secret|password|passwd|private[_-]?key|client[_-]?secret|account[_-]?key)[a-z0-9_]*)"
        r"(\s*[:=]\s*)([^\s,;]+)",
        r"\1\2[REDACTED]",
        sanitized,
    )


def _ci_log_chunks(text: str) -> tuple[list[str], bool]:
    # 把一条 CI 日志按固定长度切块，返回 (块列表, 是否被截断)。
    sanitized = sanitize_ci_log(text.strip())  # 先脱敏
    max_chars = CI_LOG_CHUNK_CHARS * CI_LOG_MAX_CHUNKS  # 总上限 = 单块 × 块数
    bounded = sanitized[:max_chars]  # 超长直接截断
    # 按 CI_LOG_CHUNK_CHARS 大小滑动切分，跳过纯空白块
    chunks = [
        bounded[start : start + CI_LOG_CHUNK_CHARS]
        for start in range(0, len(bounded), CI_LOG_CHUNK_CHARS)
        if bounded[start : start + CI_LOG_CHUNK_CHARS].strip()
    ]
    return chunks, len(sanitized) > max_chars  # 第二个值告诉上层「还有更多日志被砍了」


def _iso(value) -> str | None:
    # 把 datetime 转成 ISO 字符串；空值返回 None，方便写进元数据。
    return value.isoformat() if value else None


def _payload(source_type: str, source_id, title: str, content: str, metadata: dict) -> DocumentPayload | None:
    # 小工具：内容为空就跳过，否则包成一个 DocumentPayload（写库的「半成品」）。
    if not content.strip():
        return None
    return DocumentPayload(
        source_type=source_type,
        source_id=source_id,
        title=title[:500],   # 标题截断到 500 字符
        content=content,
        metadata=metadata,
    )


def index_repository_documents(
    db: Session,
    repo_id,
    *,
    generate_embeddings: bool = True,
    sync_vector_store: bool = True,
) -> int:
    # 把一个仓库的 Issue / PR / 失败的 CI 日志「同步索引」进 RAG。
    # 在流水线里的位置：属于「建库 / 增量同步」阶段——把结构化数据转成可检索的 RAG 文档。
    # 输入：仓库 id、是否生成向量、是否同步向量库。
    # 输出：实际写入的文档数量。
    repo = db.get(Repository, repo_id)
    if repo is None:
        return 0  # 仓库不存在，啥也不做

    payloads: list[DocumentPayload] = []  # 收集所有要写入的「半成品」

    # 1) Issue：标题 + 正文拼成内容，连同编号 / 标签 / 状态 / 时间等元数据
    issues = db.query(Issue).filter(Issue.repo_id == repo_id).all()
    for issue in issues:
        content = "\n".join([issue.title, issue.body or ""])
        item = _payload(
            "issue",
            issue.id,
            issue.title,
            content,
            {
                "number": issue.number,
                "labels": issue.labels or [],
                "state": issue.state,
                "author": issue.author,
                "assignees": issue.assignees or [],
                "created_at": _iso(issue.created_at),
                "updated_at": _iso(issue.updated_at),
                "closed_at": _iso(issue.closed_at),
            },
        )
        if item:
            payloads.append(item)

    # 2) PullRequest：连同标题、正文、行内评审评论一起拼，并带分支 / 文件清单等元数据
    prs = (
        db.query(PullRequest)
        .options(selectinload(PullRequest.files), selectinload(PullRequest.review_comments))  # 预加载关联表，避免 N+1 查询
        .filter(PullRequest.repo_id == repo_id)
        .all()
    )
    for pr in prs:
        # 把每条行内评审评论拼成「路径:行号 作者: 内容」
        comments_text = "\n".join(
            f"{comment.path or ''}:{comment.line or ''} {comment.author or ''}: {comment.body or ''}"
            for comment in pr.review_comments
        )
        content = "\n".join(
            [
                pr.title,
                pr.body or "",
                "review comments:",
                comments_text,
            ]
        )
        item = _payload(
            "pull_request",
            pr.id,
            pr.title,
            content,
            {
                "number": pr.number,
                "state": pr.state,
                "author": pr.author,
                "files": [file.filename for file in pr.files],
                "base_branch": pr.base_branch,
                "head_branch": pr.head_branch,
                "created_at": _iso(pr.created_at),
                "updated_at": _iso(pr.updated_at),
                "merged_at": _iso(pr.merged_at),
            },
        )
        if item:
            payloads.append(item)

    # 3) WorkflowRun：只索引「失败」的 CI 运行（成功的没排查价值），且只索引有日志的
    runs = db.query(WorkflowRun).filter(WorkflowRun.repo_id == repo_id).all()
    for run in runs:
        if run.conclusion != "failure" or not (run.logs_text or "").strip():
            continue  # 只关心失败且带日志的，节省索引空间
        log_chunks, logs_truncated = _ci_log_chunks(run.logs_text or "")
        for chunk_index, log_chunk in enumerate(log_chunks):
            content = "\n".join([run.name, log_chunk])
            item = _payload(
                "workflow_run",
                run.id,
                run.name if len(log_chunks) == 1 else f"{run.name}#{chunk_index + 1}",  # 多块时加序号
                content,
                {
                    "status": run.status,
                    "conclusion": run.conclusion,
                    "html_url": run.html_url,
                    "jobs": [job.get("name") for job in (run.jobs or []) if isinstance(job, dict)],
                    "created_at": _iso(run.created_at),
                    "updated_at": _iso(run.updated_at),
                    "chunk_index": chunk_index,
                    "total_chunks": len(log_chunks),
                    "logs_truncated": logs_truncated,
                    "secrets_redacted": True,  # 标记：已脱敏
                },
            )
            if item:
                payloads.append(item)

    # 在生成向量前，先取一次知识库配置（拿到用哪个模型 / 维度）
    vectors = None
    embedding_info = None
    config = (
        db.query(KnowledgeBaseConfig).filter(KnowledgeBaseConfig.repo_id == repo_id).one_or_none()
        if generate_embeddings
        else None
    )
    if config is not None and payloads:
        # 给每个 payload 的「标题 + 内容」生成向量
        vectors = embed_knowledge_texts(
            [f"{payload.title}\n{payload.content}" for payload in payloads],
            provider=config.embedding_provider,
            model=config.embedding_model,
            dimensions=config.embedding_dimensions,
        )
        embedding_info = {
            "embedding_provider": config.embedding_provider,
            "embedding_model": config.embedding_model,
            "embedding_dimensions": config.embedding_dimensions,
        }

    # 用 replace_documents 先删掉该仓库旧的这三类文档，再整体写入新的（增量同步 = 全量替换）
    docs = replace_documents(
        db,
        repo_id,
        SYNCED_REPO_SOURCE_TYPES,
        payloads,
        commit=False,  # 由上层统一提交
        vectors=vectors,
        embedding_info=embedding_info,
        generate_embeddings=generate_embeddings,
        sync_vector_store=sync_vector_store,
    )
    return len(docs)
