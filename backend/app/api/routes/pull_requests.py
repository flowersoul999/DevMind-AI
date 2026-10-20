"""DevMind AI module."""

# pull_requests.py —— PR 合入就绪度审查路由层
#
# 业务域：GitHub Pull Request 的智能审查。前端列出 PR、查看单个 PR，并让 LLM Agent
# 审查 PR（读 diff、查代码/文档、看 CI、看 review comment），给出合入建议。
# 还支持「PR 分支快照」——把 PR 代码拉到本地工作区，方便读真实改动。支持流式返回。
#
# 暴露端点：
#   GET  /api/repos/{repo_id}/pull-requests         —— 列出 PR
#   GET  /api/pull-requests/{pr_id}                —— 查看单个 PR
#   POST /api/pull-requests/{pr_id}/analyze        —— 审查 PR（一次性）
#   POST /api/pull-requests/{pr_id}/analyze/stream —— 流式审查（SSE）
#   POST /api/pull-requests/{pr_id}/snapshot        —— 准备 PR 分支快照
#   POST /api/pull-requests/{pr_id}/worktree        —— 准备 PR 工作区
#   POST /api/repos/{repo_id}/pr-snapshots/cleanup  —— 清理过期 PR 快照
#   POST /api/pull-requests/{pr_id}/review-checklist—— 生成审查清单
#
# 依赖 service：app.services.agents.pr_review_agent.PRReviewAgent、app.services.worktree_manager
import asyncio  # 异步（流式节奏）
import json  # JSON
from collections.abc import AsyncIterator  # 异步迭代器
from uuid import UUID  # UUID

from fastapi import APIRouter, Depends, HTTPException, Query  # 路由、依赖、异常、查询参数
from fastapi.responses import StreamingResponse  # SSE 响应
from sqlalchemy import or_  # SQL 或
from sqlalchemy.orm import Session, selectinload  # 会话、预加载

from app.db.models import AnalysisResult, CodeRelation, CodeSymbol, Conversation, Document, PullRequest, Repository, WorkflowRun  # 表模型
from app.db.session import get_db  # 会话依赖
from app.schemas.pull_requests import PRAnalysisResponse, PullRequestResponse  # 响应模型
from app.services.agents.pr_review_agent import PRReviewAgent  # PR 审查 Agent
from app.services.chat_memory import ContextAssembler  # 上下文拼装
from app.services.code_search import search_repository_code  # 代码检索
from app.services.rag.retrieval import search_similar_documents  # 语义检索
from app.services.worktree_manager import cleanup_pr_snapshots, prepare_pr_worktree  # PR 快照/工作区管理

router = APIRouter()  # 路由器

# 项目文档类型（检索背景用）
PROJECT_DOC_TYPES = {"knowledge_file", "memory_note", "project_doc"}
# 给 LLM 的「公开执行过程」提示词
PUBLIC_REASONING_PROMPT = """
你是 DevMind AI 的 PR 合并就绪度 Agent。请用中文流式输出一段“公开执行过程”，帮助用户理解你如何按计划审查当前 PR。
要求：
- 只输出面向用户的可审计分析摘要，不输出隐藏链路思维或私有推理链。
- 按计划说明你检查了 PR 目标、diff 风险、相关代码/文档、CI、Review 评论，并如何形成合入建议。
- 最终建议只能围绕：建议合入 / 修改后合入 / 暂缓 / 拒绝。
- 不要编造输入中没有的文件、CI、Issue、人员或评论。
- 不要输出最终 JSON。
"""


def _encode_sse(event: str, data: dict) -> str:
    # 编码成 SSE 一行
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _trace_item(kind: str, title: str, content: str, status: str = "done") -> dict:
    # 构造一条分析轨迹事件
    return {"kind": kind, "title": title, "content": content, "status": status}


def _document_evidence(items: list[dict], default_source_type: str) -> list[dict]:
    # 整理检索证据为统一格式
    evidence = []
    for item in items:
        snippet = str(item.get("snippet") or "").strip()
        title = str(item.get("title") or "").strip()
        if not snippet or not title:
            continue
        evidence.append(
            {
                "source_type": item.get("source_type") or default_source_type,
                "title": title,
                "snippet": snippet,
                "source_id": str(item.get("source_id") or item.get("id") or ""),
                "score": item.get("score"),
                "metadata": item.get("metadata") or {},
            }
        )
    return evidence


def _dedupe_evidence(items: list[dict], limit: int) -> list[dict]:
    # 证据去重，最多保留 limit 条
    output = []
    seen = set()
    for item in items:
        key = (item.get("source_type"), item.get("source_id"), item.get("title"))
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
        if len(output) >= limit:
            break
    return output


def _fallback_project_documents(db: Session, repo_id: UUID) -> list[dict]:
    # 语义检索不到时退而取最近项目文档
    rows = (
        db.query(Document)
        .filter(Document.repo_id == repo_id, Document.source_type.in_(PROJECT_DOC_TYPES))
        .order_by(Document.created_at.desc())
        .limit(4)
        .all()
    )
    return [
        {
            "source_type": doc.source_type,
            "title": doc.title,
            "snippet": doc.content[:260],
            "source_id": str(doc.source_id),
            "score": None,
            "metadata": doc.meta or {},
        }
        for doc in rows
    ]


def _conversation_evidence(db: Session, pr: PullRequest, conversation_id: UUID | None, query: str) -> list[dict]:
    # 会话上下文作为证据
    if not conversation_id:
        return []
    conversation = db.get(Conversation, conversation_id)
    if conversation is None or conversation.repo_id != pr.repo_id:
        return []
    repo = db.get(Repository, pr.repo_id)
    assembled = ContextAssembler(db).assemble(repo, conversation, query, limit=8)
    evidence = _document_evidence(assembled.evidence, "conversation")
    if assembled.system_context.strip():
        evidence.append(
            {
                "source_type": "conversation",
                "title": f"会话上下文：{conversation.title}",
                "snippet": assembled.system_context[:320],
                "source_id": str(conversation.id),
                "score": None,
            }
        )
    return evidence[:5]


def _pr_payload(pr: PullRequest) -> dict:
    # 把 PR 整理成喂给 Agent 的精简字典（含文件变更和 review 评论）
    return {
        "id": str(pr.id),
        "number": pr.number,
        "title": pr.title,
        "body": pr.body,
        "state": pr.state,
        "author": pr.author,
        "base_branch": pr.base_branch,  # 目标分支（合入到哪）
        "head_branch": pr.head_branch,  # 来源分支
        "merged_at": pr.merged_at.isoformat() if pr.merged_at else None,
        "files": [
            {
                "filename": file.filename,
                "status": file.status,  # added/modified/deleted
                "additions": file.additions,  # 增行数
                "deletions": file.deletions,  # 删行数
                "patch": file.patch,  # diff 文本
            }
            for file in pr.files
        ],
        "comments": [
            {"body": comment.body, "path": comment.path, "line": comment.line, "author": comment.author}
            for comment in pr.review_comments
        ],
    }


def _pr_review_plan() -> list[str]:
    # PR 审查标准步骤
    return [
        "理解 PR 标题、描述、变更文件和关联上下文。",
        "按模块拆分 diff 风险。",
        "读取相关源代码、测试文件和项目文档。",
        "检查 CI 状态和失败信号。",
        "检索团队约定、历史对话和 review comments。",
        "形成合入建议和 review comments。",
    ]


def _build_pr_analysis_context(db: Session, pr: PullRequest, conversation_id: UUID | None = None) -> tuple[str, dict, dict]:
    # 构建 PR 审查上下文：代码、文档、相似 Issue、会话、CI、团队画像
    repo = db.get(Repository, pr.repo_id)
    file_names = [file.filename for file in pr.files]
    diff_text = "\n".join(f"{file.filename}\n{file.patch or ''}" for file in pr.files[:12])  # 取前 12 个文件 diff
    code_query = "\n".join(filter(None, [pr.title, pr.body or "", " ".join(file_names), diff_text[:5000]]))  # 代码检索词
    review_text = "\n".join(comment.body or "" for comment in pr.review_comments)
    semantic_query = "\n".join(filter(None, [pr.title, pr.body or "", review_text[:3000]]))  # 语义检索词
    code_docs = _document_evidence(
        search_repository_code(repo, code_query, limit=6) if repo else [],
        "code",
    )
    project_docs = _document_evidence(
        search_similar_documents(
            db,
            str(pr.repo_id),
            semantic_query,
            limit=4,
            source_types=sorted(PROJECT_DOC_TYPES),
        ),
        "document",
    )
    if not project_docs:
        project_docs = _fallback_project_documents(db, pr.repo_id)
    related_issues = _document_evidence(
        search_similar_documents(db, str(pr.repo_id), semantic_query, "issue", 4),
        "issue",
    )
    chat_evidence = _conversation_evidence(db, pr, conversation_id, semantic_query)
    recent_runs = (
        db.query(WorkflowRun)
        .filter(WorkflowRun.repo_id == pr.repo_id)
        .order_by(WorkflowRun.created_at.desc())
        .limit(8)
        .all()
    )
    failed_runs = [run for run in recent_runs if run.conclusion == "failure"]  # 最近失败 CI
    team_members = [
        {
            "name": (doc.meta or {}).get("name") or doc.title.replace("团队成员：", "").replace("Team member: ", ""),
            "role": (doc.meta or {}).get("role") or "",
            "strengths": (doc.meta or {}).get("strengths") or "",
            "techStack": (doc.meta or {}).get("tech_stack") or "",
        }
        for doc in db.query(Document).filter(Document.repo_id == pr.repo_id, Document.source_type == "team_member").all()
    ]
    # 统计增减行数
    total_additions = sum(file.additions or 0 for file in pr.files)
    total_deletions = sum(file.deletions or 0 for file in pr.files)
    # 敏感文件检测（auth/security/migration/ci 等目录要更谨慎）
    sensitive_files = [
        name
        for name in file_names
        if any(token in name.lower() for token in ["auth", "security", "migration", "schema", "payment", "permission", "config", "ci", "workflow"])
    ]
    context = {
        "repo_id": str(pr.repo_id),
        "plan": _pr_review_plan(),
        "diff_stats": {
            "files": len(pr.files),
            "additions": total_additions,
            "deletions": total_deletions,
            "sensitive_files": sensitive_files,
        },
        "code_references": code_docs,
        "project_documents": project_docs,
        "related_issues": related_issues,
        "conversation_evidence": chat_evidence,
        "review_comments": _pr_payload(pr)["comments"],
        "ci_summary": {
            "recent_runs": len(recent_runs),
            "failed_runs": len(failed_runs),
            "failed": [
                {"name": run.name, "conclusion": run.conclusion, "logs_text": (run.logs_text or "")[:260]}
                for run in failed_runs[:4]
            ],
        },
        "team_members": team_members,
    }
    counts = {
        "files": len(pr.files),
        "comments": len(pr.review_comments),
        "code": len(code_docs),
        "documents": len(project_docs),
        "issues": len(related_issues),
        "conversation": len(chat_evidence),
        "failed_ci": len(failed_runs),
        "team_members": len(team_members),
    }
    return code_query, context, counts


def _snapshot_summary(snapshot: dict) -> dict:
    # PR 快照摘要
    return {
        "status": snapshot.get("status"),
        "message": snapshot.get("message"),
        "name": snapshot.get("snapshot_name") or "PR 分支快照",
        "branch": snapshot.get("branch"),
        "commit_sha": snapshot.get("commit_sha"),
        "pr_id": snapshot.get("pr_id"),
        "pr_number": snapshot.get("pr_number"),
        "indexed_symbols": snapshot.get("indexed_symbols"),
    }


def _prepare_pr_snapshot_context(db: Session, pr: PullRequest, enabled: bool) -> dict:
    # 准备 PR 分支快照（enabled=False 就跳过）
    if not enabled:
        return {"status": "skipped", "message": "未请求 PR 分支快照。"}
    repo = db.get(Repository, pr.repo_id)
    if repo is None:
        return {"status": "missing_repo", "message": "未找到仓库。"}
    return prepare_pr_worktree(db, repo, pr)  # 真正拉起快照


def _pr_code_graph_impact(db: Session, pr: PullRequest) -> dict:
    # 在代码图谱里算「这个 PR 改了哪些文件、影响哪些符号和关系」
    changed_paths = list(dict.fromkeys(file.filename for file in pr.files if file.filename))  # 去重改动路径
    symbol_query = db.query(CodeSymbol).filter(CodeSymbol.repo_id == pr.repo_id, CodeSymbol.pr_id == pr.id)
    if changed_paths:
        symbol_query = symbol_query.filter(CodeSymbol.path.in_(changed_paths))
    symbols = symbol_query.order_by(CodeSymbol.path.asc(), CodeSymbol.start_line.asc()).limit(30).all()
    symbol_names = [symbol.name for symbol in symbols]
    relation_query = db.query(CodeRelation).filter(CodeRelation.repo_id == pr.repo_id, CodeRelation.pr_id == pr.id)
    relation_filters = []
    if changed_paths:
        relation_filters.append(CodeRelation.path.in_(changed_paths))
    if symbol_names:
        relation_filters.extend([CodeRelation.source_name.in_(symbol_names), CodeRelation.target_name.in_(symbol_names)])
    if relation_filters:
        relation_query = relation_query.filter(or_(*relation_filters))
    relations = relation_query.limit(60).all()
    return {
        "changed_paths": changed_paths[:30],
        "symbols": [
            {
                "name": symbol.name,
                "kind": symbol.kind,
                "path": symbol.path,
                "start_line": symbol.start_line,
                "branch": symbol.branch,
                "commit_sha": symbol.commit_sha,
            }
            for symbol in symbols
        ],
        "relations": [
            {
                "source": relation.source_name,
                "target": relation.target_name,
                "type": relation.relation_type,
                "path": relation.path,
                "line": (relation.meta or {}).get("line"),
            }
            for relation in relations
        ],
    }


def _augment_pr_context_with_snapshot(db: Session, pr: PullRequest, query: str, context: dict, counts: dict, snapshot: dict) -> None:
    # 把 PR 快照的代码/图谱信息补进上下文（就地修改）
    context["pr_snapshot"] = _snapshot_summary(snapshot)
    counts["snapshot_code"] = 0
    counts["impacted_symbols"] = 0
    counts["impacted_relations"] = 0
    if snapshot.get("status") != "ready":
        # 快照没就绪就用「全部改动文件」当空影响面
        context["code_graph_impact"] = {"changed_paths": [file.filename for file in pr.files], "symbols": [], "relations": []}
        return

    repo = db.get(Repository, pr.repo_id)
    snapshot_docs = _document_evidence(
        search_repository_code(
            repo,
            query,
            limit=8,
            checkout_path=snapshot.get("snapshot_path"),  # 用快照路径读真实代码
            metadata={
                "checkout_kind": "pr_snapshot",
                "pr_id": str(pr.id),
                "pr_number": pr.number,
                "branch": snapshot.get("branch"),
                "commit_sha": snapshot.get("commit_sha"),
            },
        )
        if repo and snapshot.get("snapshot_path")
        else [],
        "code",
    )
    context["code_references"] = _dedupe_evidence([*snapshot_docs, *(context.get("code_references") or [])], 10)
    impact = _pr_code_graph_impact(db, pr)
    context["code_graph_impact"] = impact
    counts["snapshot_code"] = len(snapshot_docs)
    counts["impacted_symbols"] = len(impact["symbols"])
    counts["impacted_relations"] = len(impact["relations"])
    counts["code"] = len(context["code_references"])


def _persist_pr_analysis(
    db: Session,
    pr: PullRequest,
    result: dict,
    model_name: str,
    conversation_id: UUID | None,
    evidence_counts: dict,
) -> AnalysisResult:
    # 把 PR 审查结果存库
    analysis = AnalysisResult(
        target_type="pull_request",
        target_id=pr.id,
        analysis_type="pr_review",
        input_snapshot={
            "pr_id": str(pr.id),
            "conversation_id": str(conversation_id) if conversation_id else None,
            "evidence_counts": evidence_counts,
        },
        result_json=result,
        model_name=model_name,
    )
    db.add(analysis)
    db.commit()
    db.refresh(analysis)
    return analysis


def _public_reasoning_fallback(pr: PullRequest, context: dict, counts: dict) -> list[str]:
    # LLM 没流式输出时的兜底段落
    stats = context.get("diff_stats") or {}
    return [
        f"- 我先制定 Review 计划，再读取 PR #{pr.number} 的标题、描述、分支和 {counts['files']} 个变更文件。\n",
        f"- Diff 规模为 +{stats.get('additions', 0)} / -{stats.get('deletions', 0)}，其中敏感文件 {len(stats.get('sensitive_files') or [])} 个。\n",
        f"- 我检索了相关代码 {counts['code']} 条，其中 PR 快照代码 {counts.get('snapshot_code', 0)} 条；项目文档/记忆 {counts['documents']} 条、历史 Issue {counts['issues']} 条和会话证据 {counts['conversation']} 条。\n",
        f"- CI 侧读取到 {counts['failed_ci']} 个失败信号，并结合 {counts['comments']} 条 review comment 判断是否阻塞。\n",
        "- 最后将风险点、阻塞项、测试建议汇总为合入建议：建议合入、修改后合入、暂缓或拒绝。\n",
    ]


async def _stream_public_reasoning(agent: PRReviewAgent, pr: PullRequest, payload: dict, context: dict, counts: dict) -> AsyncIterator[str]:
    # 流式输出 LLM 的公开审查过程
    streamed = False
    user_payload = {
        "pull_request": payload,
        "plan": context.get("plan") or [],
        "evidence_counts": counts,
        "context_preview": {
            "diff_stats": context.get("diff_stats"),
            "pr_snapshot": context.get("pr_snapshot"),
            "code_graph_impact": context.get("code_graph_impact"),
            "code_references": context.get("code_references", [])[:3],
            "project_documents": context.get("project_documents", [])[:3],
            "ci_summary": context.get("ci_summary"),
            "review_comments": context.get("review_comments", [])[:3],
        },
    }
    async for chunk in agent.llm.chat_text_stream(
        PUBLIC_REASONING_PROMPT,
        [{"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)}],
    ):
        streamed = True
        yield chunk
    if streamed:
        return
    for line in _public_reasoning_fallback(pr, context, counts):
        await asyncio.sleep(0.06)
        yield line


async def _pr_analysis_sse(pr_id: UUID, conversation_id: UUID | None, db: Session) -> AsyncIterator[str]:
    # SSE 主流程：把 PR 审查过程实时推给前端
    # 预加载 files 和 review_comments，避免后面反复查库
    pr = db.query(PullRequest).options(selectinload(PullRequest.files), selectinload(PullRequest.review_comments)).filter(PullRequest.id == pr_id).one_or_none()
    if pr is None:
        yield _encode_sse("error", {"message": "未找到 PR"})
        return
    agent = PRReviewAgent()
    payload = _pr_payload(pr)
    plan = _pr_review_plan()
    try:
        yield _encode_sse("trace", _trace_item("step", "制定 Review 计划", "\n".join(f"{index + 1}. {item}" for index, item in enumerate(plan)), "done"))
        yield _encode_sse("trace", _trace_item("step", "执行 1/6：理解 PR", f"读取 PR #{pr.number} 的标题、描述、分支、作者和变更文件列表。", "running"))
        snapshot = _prepare_pr_snapshot_context(db, pr, enabled=True)  # 准备快照
        yield _encode_sse(
            "trace",
            _trace_item(
                "tool",
                "准备 PR 分支快照",
                str(snapshot.get("message") or f"状态：{snapshot.get('status')}；代码图符号：{snapshot.get('indexed_symbols') or 0}。"),
            ),
        )
        _query, context, counts = _build_pr_analysis_context(db, pr, conversation_id)
        _augment_pr_context_with_snapshot(db, pr, _query, context, counts, snapshot)
        context["plan"] = plan
        yield _encode_sse("trace", _trace_item("tool", "执行 2/6：拆分 diff 风险", f"扫描 {counts['files']} 个文件，识别敏感文件 {len((context.get('diff_stats') or {}).get('sensitive_files') or [])} 个。"))
        yield _encode_sse("trace", _trace_item("tool", "执行 3/6：读取代码和测试", f"找到 {counts['code']} 条代码线索，其中 PR 快照代码 {counts.get('snapshot_code', 0)} 条；项目文档/记忆线索 {counts['documents']} 条。"))
        yield _encode_sse("trace", _trace_item("tool", "执行 4/6：检查 CI", f"读取最近 CI 信号，失败 run 数量：{counts['failed_ci']}。"))
        yield _encode_sse("trace", _trace_item("tool", "执行 5/6：检索上下文", f"找到 {counts['issues']} 条历史 Issue、{counts['conversation']} 条会话证据、{counts['comments']} 条 review comment。"))
        yield _encode_sse("trace", _trace_item("thinking", "LLM 公开执行过程", "开始综合 PR diff、代码、CI、文档和 review comment。", "running"))
        async for chunk in _stream_public_reasoning(agent, pr, payload, context, counts):
            yield _encode_sse("thinking_delta", {"content": chunk})
        result = await agent.run(payload, context)  # 跑审查
        analysis = _persist_pr_analysis(db, pr, result, agent.llm.model, conversation_id, counts)
        yield _encode_sse("trace", _trace_item("result", "执行 6/6：形成合入建议", f"结论：{result.get('merge_recommendation')}；风险点：{len(result.get('risk_points') or [])}；阻塞项：{len(result.get('blocking_issues') or [])}。"))
        yield _encode_sse("final", {"analysis_id": str(analysis.id), "result": result})
    except Exception as exc:
        db.rollback()  # 出错回滚
        yield _encode_sse("error", {"message": f"分析失败：{exc}"})


@router.get("/repos/{repo_id}/pull-requests", response_model=list[PullRequestResponse])  # GET PR 列表
async def list_pull_requests(repo_id: UUID, db: Session = Depends(get_db)) -> list[PullRequest]:
    return (
        db.query(PullRequest)
        .options(selectinload(PullRequest.files), selectinload(PullRequest.review_comments))  # 预加载关联，避免 N+1
        .filter(PullRequest.repo_id == repo_id)
        .order_by(PullRequest.number.desc())
        .all()
    )


@router.get("/pull-requests/{pr_id}", response_model=PullRequestResponse)  # GET 单个 PR
async def get_pull_request(pr_id: UUID, db: Session = Depends(get_db)) -> PullRequest:
    pr = db.query(PullRequest).options(selectinload(PullRequest.files), selectinload(PullRequest.review_comments)).filter(PullRequest.id == pr_id).one_or_none()
    if pr is None:
        raise HTTPException(status_code=404, detail="未找到 PR")
    return pr


@router.post("/pull-requests/{pr_id}/analyze", response_model=PRAnalysisResponse)  # POST 审查（一次性）
async def analyze_pull_request(
    pr_id: UUID,
    conversation_id: UUID | None = Query(default=None),
    use_pr_snapshot: bool = Query(default=True),  # 是否用快照
    use_worktree: bool | None = Query(default=None),  # worktree 开关（有则覆盖上面）
    db: Session = Depends(get_db),
) -> PRAnalysisResponse:
    pr = db.query(PullRequest).options(selectinload(PullRequest.files), selectinload(PullRequest.review_comments)).filter(PullRequest.id == pr_id).one_or_none()
    if pr is None:
        raise HTTPException(status_code=404, detail="未找到 PR")
    agent = PRReviewAgent()
    # use_worktree 优先；否则用 use_pr_snapshot 决定要不要快照
    snapshot_enabled = use_pr_snapshot if use_worktree is None else use_worktree
    snapshot = _prepare_pr_snapshot_context(db, pr, snapshot_enabled)
    _query, context, counts = _build_pr_analysis_context(db, pr, conversation_id)
    _augment_pr_context_with_snapshot(db, pr, _query, context, counts, snapshot)
    result = await agent.run(_pr_payload(pr), context)  # 跑审查
    analysis = _persist_pr_analysis(db, pr, result, agent.llm.model, conversation_id, counts)
    return PRAnalysisResponse(analysis_id=analysis.id, result=result)


@router.post("/pull-requests/{pr_id}/analyze/stream")  # POST 流式审查（SSE）
async def analyze_pull_request_stream(
    pr_id: UUID,
    conversation_id: UUID | None = Query(default=None),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    return StreamingResponse(_pr_analysis_sse(pr_id, conversation_id, db), media_type="text/event-stream")


@router.post("/pull-requests/{pr_id}/snapshot")  # POST 准备 PR 快照
async def prepare_pull_request_snapshot(pr_id: UUID, db: Session = Depends(get_db)) -> dict:
    pr = db.query(PullRequest).options(selectinload(PullRequest.files)).filter(PullRequest.id == pr_id).one_or_none()
    if pr is None:
        raise HTTPException(status_code=404, detail="未找到 PR")
    return _prepare_pr_snapshot_context(db, pr, enabled=True)


@router.post("/pull-requests/{pr_id}/worktree")  # POST 准备 PR 工作区（与快照类似）
async def prepare_pull_request_worktree(pr_id: UUID, db: Session = Depends(get_db)) -> dict:
    pr = db.query(PullRequest).options(selectinload(PullRequest.files)).filter(PullRequest.id == pr_id).one_or_none()
    if pr is None:
        raise HTTPException(status_code=404, detail="未找到 PR")
    return _prepare_pr_snapshot_context(db, pr, enabled=True)


@router.post("/repos/{repo_id}/pr-snapshots/cleanup")  # POST 清理过期 PR 快照
async def cleanup_pull_request_snapshots(
    repo_id: UUID,
    max_age_hours: int = Query(default=168, ge=1, le=24 * 90),  # 默认清理 7 天前的
    db: Session = Depends(get_db),
) -> dict:
    repo = db.get(Repository, repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="未找到仓库")
    return cleanup_pr_snapshots(db, repo, max_age_hours=max_age_hours)  # 交给 service 清理


@router.post("/pull-requests/{pr_id}/review-checklist")  # POST 生成审查清单
async def review_checklist(pr_id: UUID, db: Session = Depends(get_db)) -> dict:
    response = await analyze_pull_request(pr_id, db=db)  # 复用审查逻辑
    # 直接返回结果里的 review_checklist 字段
    return {"pr_id": str(pr_id), "checklist": response.result.review_checklist}
