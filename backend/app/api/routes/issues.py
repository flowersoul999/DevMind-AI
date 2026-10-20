"""DevMind AI module."""

# issues.py —— Issue 分析路由层
#
# 业务域：GitHub Issue 的智能分析。前端可以按仓库列出 Issue、查看单个 Issue，
# 以及让 LLM Agent 分析某个 Issue（结论/优先级/复杂度/建议负责人），支持普通和流式两种返回。
#
# 暴露端点：
#   GET  /api/repos/{repo_id}/issues        —— 列出某仓库的 Issue
#   GET  /api/issues/{issue_id}              —— 查看单个 Issue
#   POST /api/issues/{issue_id}/analyze      —— 用 Agent 分析 Issue（一次性返回）
#   POST /api/issues/{issue_id}/analyze/stream —— 流式分析 Issue（SSE 实时返回进度）
#   GET  /api/issues/{issue_id}/similar      —— 找相似的 Issue
#
# 依赖 service：app.services.agents.issue_agent.IssueAgent（分析 Agent）、
#              app.services.chat_memory.ContextAssembler（拼上下文）、
#              app.services.code_search / app.services.rag.retrieval（检索证据）
import asyncio  # 异步工具（用于流式时模拟打字节奏）
import json  # JSON 序列化（SSE 数据）
from collections.abc import AsyncIterator  # 异步迭代器类型（流式用）
from uuid import UUID  # UUID

from fastapi import APIRouter, Depends, HTTPException, Query  # 路由、依赖、异常、查询参数
from fastapi.responses import StreamingResponse  # 流式响应（SSE）
from sqlalchemy.orm import Session  # 数据库会话

from app.db.models import AnalysisResult, Conversation, Document, Issue, Repository  # 表模型
from app.db.session import get_db  # 会话依赖
from app.schemas.issues import IssueAnalysisResponse, IssueResponse  # 响应模型
from app.services.agents.issue_agent import IssueAgent  # 真正分析 Issue 的 Agent
from app.services.chat_memory import ContextAssembler  # 把对话/记忆拼成上下文
from app.services.code_search import search_repository_code  # 在代码里搜相关片段
from app.services.rag.retrieval import search_similar_documents  # 语义检索相似文档

router = APIRouter()  # 路由器

# 哪些文档类型算「项目文档」（用于检索 Issue 背景）
PROJECT_DOC_TYPES = {"knowledge_file", "memory_note", "project_doc"}
# 给 LLM 的「公开分析过程」提示词：告诉它用中文、只输出可审计的摘要，不输出隐藏推理
PUBLIC_REASONING_PROMPT = """
你是 DevMind AI 的 Issue Agent。请用中文流式输出一段“公开分析过程”，帮助用户理解你如何分析当前 Issue。
要求：
- 只输出面向用户的可审计分析摘要，不输出隐藏链路思维或私有推理链。
- 简洁说明你检查了哪些证据、如何判断结论/优先级/复杂度/负责人。
- 不要编造输入中没有的文件、人员、Issue 或 PR。
- 不要输出最终 JSON。
"""


def _encode_sse(event: str, data: dict) -> str:
    # 把事件名和数据编码成 SSE 格式的一行（Server-Sent Events：每行 "event:" / "data:"，以空行结尾）
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _document_evidence(items: list[dict], default_source_type: str) -> list[dict]:
    # 把检索到的原始证据整理成统一格式（标题、片段、来源 ID、相关度分数）
    evidence = []
    for item in items:
        snippet = str(item.get("snippet") or "").strip()  # 证据片段
        title = str(item.get("title") or "").strip()  # 证据标题
        if not snippet or not title:
            continue  # 没标题或没片段的跳过
        evidence.append(
            {
                "source_type": item.get("source_type") or default_source_type,
                "title": title,
                "snippet": snippet,
                "source_id": str(item.get("source_id") or item.get("id") or ""),
                "score": item.get("score"),  # 相似度分数
                "metadata": item.get("metadata") or {},
            }
        )
    return evidence


def _fallback_project_documents(db: Session, repo_id: UUID) -> list[dict]:
    # 当语义检索不到项目文档时，退而求其次：直接查最近的几条项目文档
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
            "snippet": doc.content[:260],  # 取前 260 字
            "source_id": str(doc.source_id),
            "score": None,
            "metadata": doc.meta or {},
        }
        for doc in rows
    ]


def _conversation_evidence(db: Session, issue: Issue, conversation_id: UUID | None, query: str) -> list[dict]:
    # 如果从某个会话发起分析，把会话里的历史上下文也作为证据
    if not conversation_id:
        return []  # 没有会话就返回空
    conversation = db.get(Conversation, conversation_id)
    if conversation is None or conversation.repo_id != issue.repo_id:
        return []  # 会话不存在或不属于本仓库，忽略
    repo = db.get(Repository, issue.repo_id)
    assembled = ContextAssembler(db).assemble(repo, conversation, query, limit=8)  # 拼会话上下文
    evidence = _document_evidence(assembled.evidence, "conversation")
    if assembled.system_context.strip():
        # 会话的系统上下文也加一条证据
        evidence.append(
            {
                "source_type": "conversation",
                "title": f"会话上下文：{conversation.title}",
                "snippet": assembled.system_context[:320],
                "source_id": str(conversation.id),
                "score": None,
            }
        )
    return evidence[:5]  # 最多 5 条


def _issue_payload(issue: Issue) -> dict:
    # 把 Issue 数据库对象整理成喂给 Agent 的精简字典
    return {
        "id": str(issue.id),
        "title": issue.title,
        "body": issue.body,
        "labels": issue.labels,
        "number": issue.number,
        "state": issue.state,
        "assignees": issue.assignees,
        "author": issue.author,
    }


def _build_issue_analysis_context(db: Session, issue: Issue, conversation_id: UUID | None = None) -> tuple[str, dict, dict]:
    # 构建分析所需的「上下文」：相似 Issue、相关代码、项目文档、会话证据、团队成员
    query = f"{issue.title}\n{issue.body or ''}"  # 用标题+正文当检索词
    repo = db.get(Repository, issue.repo_id)
    # 语义检索相似历史 Issue
    similar_docs = search_similar_documents(db, str(issue.repo_id), query, "issue", 4)
    duplicates = [
        {
            "issue_id": item["source_id"],
            "title": item["title"],
            "score": max(0.0, min(1.0, float(item["score"]))),  # 分数限制在 0~1
        }
        for item in similar_docs
        if item["source_id"] != str(issue.id)  # 排除自己
    ]
    # 在代码里搜相关片段
    code_docs = _document_evidence(
        search_repository_code(repo, query, limit=4) if repo else [],
        "code",
    )
    # 语义检索项目文档/知识
    project_docs = _document_evidence(
        search_similar_documents(
            db,
            str(issue.repo_id),
            query,
            limit=4,
            source_types=sorted(PROJECT_DOC_TYPES),
        ),
        "document",
    )
    if not project_docs:
        project_docs = _fallback_project_documents(db, issue.repo_id)  # 没检索到就用兜底
    chat_evidence = _conversation_evidence(db, issue, conversation_id, query)  # 会话证据
    # 团队画像（谁擅长什么），从文档的 team_member 类型里取
    team_members = [
        {
            "name": (doc.meta or {}).get("name") or doc.title.replace("团队成员：", "").replace("Team member: ", ""),
            "role": (doc.meta or {}).get("role") or "",
            "strengths": (doc.meta or {}).get("strengths") or "",
            "techStack": (doc.meta or {}).get("tech_stack") or "",
        }
        for doc in db.query(Document).filter(Document.repo_id == issue.repo_id, Document.source_type == "team_member").all()
    ]
    # 汇总成 context 字典
    context = {
        "repo_id": str(issue.repo_id),
        "duplicate_candidates": duplicates,
        "code_references": code_docs,
        "project_documents": project_docs,
        "conversation_evidence": chat_evidence,
        "team_members": team_members,
    }
    # 各证据数量（用于前端展示和思考过程）
    counts = {
        "duplicates": len(duplicates),
        "code": len(code_docs),
        "documents": len(project_docs),
        "conversation": len(chat_evidence),
        "team_members": len(team_members),
    }
    return query, context, counts


def _persist_issue_analysis(
    db: Session,
    issue: Issue,
    result: dict,
    model_name: str,
    conversation_id: UUID | None,
    evidence_counts: dict,
) -> AnalysisResult:
    # 把分析结果存进数据库（AnalysisResult 表），方便以后查看/审计
    analysis = AnalysisResult(
        target_type="issue",  # 分析对象类型
        target_id=issue.id,  # 具体 Issue ID
        analysis_type="issue_analysis",
        input_snapshot={
            "issue_id": str(issue.id),
            "conversation_id": str(conversation_id) if conversation_id else None,
            "evidence_counts": evidence_counts,
        },
        result_json=result,  # Agent 返回的结构化结论
        model_name=model_name,  # 用了哪个模型
    )
    db.add(analysis)
    db.commit()
    db.refresh(analysis)
    return analysis


def _trace_item(kind: str, title: str, content: str, status: str = "done") -> dict:
    # 构造一条「分析轨迹」事件（前端进度条用）：kind 是步骤类型，title 是标题
    return {"kind": kind, "title": title, "content": content, "status": status}


def _public_reasoning_fallback(issue: Issue, context: dict) -> list[str]:
    # 当 LLM 没能正常流式输出时，用规则拼一段「公开分析过程」兜底
    duplicates = context.get("duplicate_candidates") or []
    code_refs = context.get("code_references") or []
    docs = context.get("project_documents") or []
    members = context.get("team_members") or []
    return [
        f"- 我先读取 Issue #{issue.number} 的标题、正文、标签和当前状态，确认它描述的是「{issue.title}」。\n",
        f"- 然后检索相似历史 Issue，找到 {len(duplicates)} 个候选；如果相似度很高，会优先建议合并到已有 Issue。\n",
        f"- 接着查看代码和项目文档证据：代码线索 {len(code_refs)} 条，文档/记忆线索 {len(docs)} 条，用于判断影响范围和复杂度。\n",
        f"- 最后结合团队画像，当前可参考成员 {len(members)} 人；负责人只会从已配置成员或明确方向中选择。\n",
        "- 这些公开分析步骤会汇总成结构化结论：结论、优先级、复杂度、负责人、证据、checklist 和可选草稿。\n",
    ]


async def _stream_public_reasoning(agent: IssueAgent, issue: Issue, payload: dict, context: dict) -> AsyncIterator[str]:
    # 让 LLM 边想边把「公开分析过程」以文本流的形式吐出来（流式体验）
    streamed = False  # 是否真的流到了内容
    user_payload = {
        "issue": payload,
        "evidence_counts": {
            "duplicates": len(context.get("duplicate_candidates") or []),
            "code_references": len(context.get("code_references") or []),
            "project_documents": len(context.get("project_documents") or []),
            "conversation_evidence": len(context.get("conversation_evidence") or []),
            "team_members": len(context.get("team_members") or []),
        },
        "context_preview": {
            "duplicates": context.get("duplicate_candidates", [])[:3],
            "code_references": context.get("code_references", [])[:3],
            "project_documents": context.get("project_documents", [])[:3],
            "team_members": context.get("team_members", [])[:3],
        },
    }
    # 调用 LLM 的流式接口，逐块拿到文字
    async for chunk in agent.llm.chat_text_stream(
        PUBLIC_REASONING_PROMPT,
        [{"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)}],
    ):
        streamed = True
        yield chunk  # 把每块文字往外抛
    if streamed:
        return  # 已经流过了就结束
    # 没流到内容，用兜底段落，每句之间稍作停顿模拟打字
    for line in _public_reasoning_fallback(issue, context):
        await asyncio.sleep(0.06)
        yield line


async def _issue_analysis_sse(issue_id: UUID, conversation_id: UUID | None, db: Session) -> AsyncIterator[str]:
    # SSE 主流程：把整个分析过程拆成多个事件，实时推给前端
    issue = db.get(Issue, issue_id)
    if issue is None:
        yield _encode_sse("error", {"message": "未找到 Issue"})  # 找不到就推一条 error 事件
        return
    agent = IssueAgent()  # 新建分析 Agent
    payload = _issue_payload(issue)  # 整理 Issue 数据
    try:
        # 推「开始读取 Issue」轨迹
        yield _encode_sse("trace", _trace_item("step", "读取 Issue", f"读取 Issue #{issue.number} 的标题、正文、标签、状态和已分配人员。", "running"))
        query, context, counts = _build_issue_analysis_context(db, issue, conversation_id)
        yield _encode_sse("trace", _trace_item("tool", "检索相似历史", f"找到 {counts['duplicates']} 个相似 Issue 候选。"))
        yield _encode_sse("trace", _trace_item("tool", "检索代码与文档", f"找到 {counts['code']} 条代码线索、{counts['documents']} 条项目文档/记忆线索。"))
        yield _encode_sse("trace", _trace_item("tool", "读取团队画像", f"读取 {counts['team_members']} 位团队成员，用于建议负责人。"))
        if counts["conversation"]:
            yield _encode_sse("trace", _trace_item("tool", "读取会话上下文", f"找到 {counts['conversation']} 条与当前会话相关的证据。"))
        yield _encode_sse("trace", _trace_item("thinking", "LLM 公开分析过程", "开始综合 Issue、证据和团队画像。", "running"))
        # 流式输出 LLM 的公开分析过程（每块都包成 thinking_delta 事件）
        async for chunk in _stream_public_reasoning(agent, issue, payload, context):
            yield _encode_sse("thinking_delta", {"content": chunk})
        # 真正跑分析，得到结构化结论
        result = await agent.run(payload, context)
        analysis = _persist_issue_analysis(db, issue, result, agent.llm.model, conversation_id, counts)
        # 推「生成结论」轨迹
        yield _encode_sse("trace", _trace_item("result", "生成结构化结论", f"结论：{result.get('conclusion')}；优先级：{result.get('priority')}；复杂度：{result.get('complexity')}；负责人：{result.get('suggested_owner')}。"))
        # 推最终事件（含分析 ID 和完整结果）
        yield _encode_sse("final", {"analysis_id": str(analysis.id), "result": result})
    except Exception as exc:
        db.rollback()  # 出错回滚数据库
        yield _encode_sse("error", {"message": f"分析失败：{exc}"})


@router.get("/repos/{repo_id}/issues", response_model=list[IssueResponse])  # GET 列出 Issue
async def list_issues(repo_id: UUID, db: Session = Depends(get_db)) -> list[Issue]:
    # 按更新时间倒序返回该仓库全部 Issue
    return db.query(Issue).filter(Issue.repo_id == repo_id).order_by(Issue.number.desc()).all()


@router.get("/issues/{issue_id}", response_model=IssueResponse)  # GET 单个 Issue
async def get_issue(issue_id: UUID, db: Session = Depends(get_db)) -> Issue:
    issue = db.get(Issue, issue_id)
    if issue is None:
        raise HTTPException(status_code=404, detail="未找到 Issue")
    return issue


@router.post("/issues/{issue_id}/analyze", response_model=IssueAnalysisResponse)  # POST 分析（一次性）
async def analyze_issue(
    issue_id: UUID,
    conversation_id: UUID | None = Query(default=None),  # 可选会话 ID
    db: Session = Depends(get_db),
) -> IssueAnalysisResponse:
    issue = db.get(Issue, issue_id)
    if issue is None:
        raise HTTPException(status_code=404, detail="未找到 Issue")
    agent = IssueAgent()
    _query, context, counts = _build_issue_analysis_context(db, issue, conversation_id)  # 构建上下文
    result = await agent.run(_issue_payload(issue), context)  # 跑分析
    analysis = _persist_issue_analysis(db, issue, result, agent.llm.model, conversation_id, counts)  # 存结果
    return IssueAnalysisResponse(analysis_id=analysis.id, result=result)


@router.post("/issues/{issue_id}/analyze/stream")  # POST 流式分析（返回 SSE 流）
async def analyze_issue_stream(
    issue_id: UUID,
    conversation_id: UUID | None = Query(default=None),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    # 为什么：流式能让用户看到「分析进行中」的实时进度，而不是干等好几秒
    return StreamingResponse(_issue_analysis_sse(issue_id, conversation_id, db), media_type="text/event-stream")


@router.get("/issues/{issue_id}/similar")  # GET 相似 Issue
async def similar_issues(issue_id: UUID, db: Session = Depends(get_db)) -> list[dict]:
    issue = db.get(Issue, issue_id)
    if issue is None:
        raise HTTPException(status_code=404, detail="未找到 Issue")
    # 用语义检索找相似的 Issue（最多 5 条）
    return search_similar_documents(db, str(issue.repo_id), f"{issue.title}\n{issue.body or ''}", "issue", 5)
