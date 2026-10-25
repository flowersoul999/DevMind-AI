"""知识图谱服务（knowledge_graph.py）。

本模块把"仓库里的各种实体"（Issue、PR、CI 运行、文档、记忆、代码文件、团队成员等）之间的
关系抽成一张图：节点是实体，边是关系（如 PR resolves Issue、PR touches_file 代码文件、记忆 cites 文档）。
它用来给前端画"项目知识图谱"，让人一眼看清"谁在改什么、哪个 PR 修了哪个 Issue、哪段记忆引用了哪个文件"。

关键函数：
- rebuild_knowledge_graph：清空并重新生成整张图的边（批量重建）。
- knowledge_graph_payload：取一个"子图"给前端（可指定中心点 + 深度，自动截断到节点上限）。
- _generate_edges：核心——从各表扫描文本，靠正则和引用提取出实体间的关系边。

与其他模块的关系：依赖 db.models 的多种表；被记忆中心/API 调用，用来可视化展示项目脉络。
类比：这就像把项目的"人际关系网"画出来——谁和谁有关联，一图胜千言。."""


# 标准库：json（序列化元数据）、re（正则提取引用）、uuid（生成稳定边 ID）、
# collections（defaultdict/deque 做邻接表与 BFS）、dataclasses、typing
import json
import re
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

# 数据表模型
from app.db.models import (
    AgentRun,         # Agent 运行（含工具调用轨迹）
    ConversationMemory,  # 会话记忆（含决策/引用）
    Document,         # 向量库文档
    Issue,            # GitHub Issue
    KnowledgeGraphEdge,  # 知识图谱边（本模块的核心产出）
    PRFile,           # PR 改动的文件
    PullRequest,      # GitHub PR
    Repository,       # 仓库
    ThreadMemory,     # 线程记忆
    WorkflowRun,      # CI 工作流运行
)


GRAPH_NODE_LIMIT = 72  # 前端一次最多展示的节点数（超过就截断，避免图太大卡死）

# 正则：从文本里提取 "#123" 形式的 Issue 引用
ISSUE_REF_RE = re.compile(r"(?<![A-Za-z])#(\d+)\b")
# 正则：提取 "fix #123" / "closes issue #123" 这种"关闭 Issue"的引用（区分于普通提及）
CLOSING_ISSUE_RE = re.compile(
    r"\b(?:fix(?:e[sd])?|close[sd]?|resolve[sd]?)\s+(?:issue\s+)?#(\d+)\b",
    re.IGNORECASE,
)
# 正则：提取 "PR #123" / "pull request 123" 形式的 PR 引用
PR_REF_RE = re.compile(r"\b(?:PR|pull request)\s*#?(\d+)\b", re.IGNORECASE)

# 节点类型别名表：把各种叫法归一化（如 pr / pull_request 都归成 pull_request）
NODE_TYPE_ALIASES = {
    "pr": "pull_request",
    "pull_request": "pull_request",
    "workflow_run": "ci_run",
    "ci": "ci_run",
    "ci_run": "ci_run",
    "memory": "memory_note",
    "memory_note": "memory_note",
    "knowledge_file": "document",
    "weekly_report": "document",
    "project_doc": "document",
    "project_manifest": "document",
    "project_overview": "document",
    "document": "document",
    "code_file": "code_file",
    "team_member": "team_member",
    "issue": "issue",
    "session": "session_decision",
    "session_decision": "session_decision",
}

# 这些来源类型的文档在图谱里都算 "document" 节点
DOCUMENT_NODE_TYPES = {"knowledge_file", "weekly_report", "project_doc", "project_manifest", "project_overview"}


@dataclass
class DocumentGroup:
    # 把"同一来源被切成多块"的文档合并成一个组（用 source_type+source_id 当键）
    source_type: str
    source_id: str
    title: str
    content: str
    metadata: dict[str, Any]
    created_at: Any = None


def normalize_node_type(value: str | None) -> str:
    # 把节点类型归一化（查别名表，查不到就原样返回）
    key = (value or "").strip()
    return NODE_TYPE_ALIASES.get(key, key)


def rebuild_knowledge_graph(db: Session, repo: Repository) -> dict[str, int]:
    # 重建知识图谱：先删这个仓库的旧边，再重新生成并批量插入，最后提交。返回边数/节点数。
    db.query(KnowledgeGraphEdge).filter(KnowledgeGraphEdge.repo_id == repo.id).delete(synchronize_session=False)
    edges = _generate_edges(db, repo.id)  # 生成所有边
    db.add_all(
        # add_all：一次性插入多条，比逐条 add 高效
        KnowledgeGraphEdge(
            repo_id=repo.id,
            from_type=edge["from_type"],
            from_id=edge["from_id"],
            to_type=edge["to_type"],
            to_id=edge["to_id"],
            relation=edge["relation"],
            confidence=edge["confidence"],
            source=edge["source"],
            meta=edge["metadata"],
        )
        for edge in edges
    )
    db.commit()  # 一次性提交
    nodes = _collect_nodes(db, repo.id)  # 收集节点（用于返回统计）
    return {"edges": len(edges), "nodes": len(nodes)}


def knowledge_graph_payload(
    db: Session,
    repo: Repository,
    *,
    center_type: str | None = None,
    center_id: str | None = None,
    depth: int = 1,
) -> dict[str, Any]:
    # 取"给前端画的子图"。可指定中心点（某个 Issue/PR/文件）和扩散深度（1~2 跳）。
    edge_count = db.query(KnowledgeGraphEdge).filter(KnowledgeGraphEdge.repo_id == repo.id).count()
    if edge_count == 0:
        rebuild_knowledge_graph(db, repo)  # 还没建过图就先建（懒初始化）

    nodes_by_key = _collect_nodes(db, repo.id)  # 所有节点 {key: 节点信息}
    edge_rows = db.query(KnowledgeGraphEdge).filter(KnowledgeGraphEdge.repo_id == repo.id).all()
    edges = [_edge_payload(row) for row in edge_rows]
    center_key = None
    resolved_center_type = normalize_node_type(center_type)  # 中心点类型归一化
    if resolved_center_type and center_id:
        center_key = _node_key(resolved_center_type, center_id)
        nodes_by_key.setdefault(
            # 即使中心点不在节点表里，也造一个"占位节点"，保证前端能高亮它
            center_key,
            _fallback_node(resolved_center_type, center_id, highlighted=True),
        )

    # 选中"以 center 为中心、depth 跳内"的子图；truncated 表示是否还有更多没展示
    selected_keys, truncated = _select_subgraph(nodes_by_key, edges, center_key, max(1, min(depth, 2)))
    selected_edges = [
        # 只保留"两端都在选中节点内"的边
        edge for edge in edges if _node_key(edge["from_type"], edge["from_id"]) in selected_keys and _node_key(edge["to_type"], edge["to_id"]) in selected_keys
    ]
    for edge in selected_edges:
        if edge["relation"] == "used_as_evidence":
            # 被当作证据用的目标节点标为高亮（让前端突出显示"支撑证据"）
            nodes_by_key[_node_key(edge["to_type"], edge["to_id"])]["highlighted"] = True

    return {
        "repo_id": repo.id,
        "center_type": resolved_center_type or None,
        "center_id": center_id,
        "depth": depth,
        "nodes": [nodes_by_key[key] for key in selected_keys if key in nodes_by_key],
        "edges": selected_edges,
        "stats": {
            "total_nodes": len(nodes_by_key),
            "total_edges": len(edges),
            "visible_nodes": len(selected_keys),
            "visible_edges": len(selected_edges),
            "truncated": truncated,  # 是否因节点上限被截断
        },
    }


def _generate_edges(db: Session, repo_id: uuid.UUID) -> list[dict[str, Any]]:
    # 核心：扫描仓库里各表，提取实体之间的关系边。这是知识图谱的"数据抽取"逻辑。
    issues = db.query(Issue).filter(Issue.repo_id == repo_id).all()
    prs = db.query(PullRequest).filter(PullRequest.repo_id == repo_id).all()
    runs = db.query(WorkflowRun).filter(WorkflowRun.repo_id == repo_id).all()
    # PR 改动的文件（需要先 join 到 PR 再按仓库过滤）
    pr_files = db.query(PRFile).join(PullRequest, PRFile.pr_id == PullRequest.id).filter(PullRequest.repo_id == repo_id).all()
    docs = _document_groups(db, repo_id)  # 文档分组

    # 建几个"查找表"，避免后面每条边都查库（空间换时间）
    issues_by_number = {issue.number: issue for issue in issues}
    prs_by_number = {pr.number: pr for pr in prs}
    team_by_name = _team_members_by_name(docs)  # 名字 -> 成员节点 ID
    code_path_to_id = _code_path_to_node_id(pr_files)  # 文件路径 -> 代码节点 ID
    # 用"按 5 元组去重"的字典存边（同一条边多次出现就合并）
    edges: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}

    def add_edge(
        from_type: str,
        from_id: str,
        to_type: str,
        to_id: str,
        relation: str,
        *,
        confidence: float = 1.0,
        source: str = "explicit",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        # 内部：加一条边。会归一化类型、去重、自环跳过、已存在则合并（取更高置信度）。
        from_type = normalize_node_type(from_type)
        to_type = normalize_node_type(to_type)
        if not from_id or not to_id or (from_type == to_type and from_id == to_id):
            return  # 空 ID 或自己连自己（自环）不要
        key = (from_type, str(from_id), to_type, str(to_id), relation)  # 5 元组唯一键
        existing = edges.get(key)
        if existing:
            # 同边已存在：保留更高置信度，并把新元数据合并进去
            existing["confidence"] = max(existing["confidence"], confidence)
            existing["metadata"].update(metadata or {})
            return
        edges[key] = {
            "id": _edge_id(from_type, str(from_id), to_type, str(to_id), relation),
            "from_type": from_type,
            "from_id": str(from_id),
            "to_type": to_type,
            "to_id": str(to_id),
            "relation": relation,
            "confidence": confidence,  # 置信度：这条关系有多可信（0~1）
            "source": source,          # 来源：显式引用 / 记忆引用 / 工具调用轨迹等
            "metadata": metadata or {},
        }

    for issue in issues:
        # Issue -> 指派给的成员
        for assignee in issue.assignees or []:
            member_id = team_by_name.get(str(assignee).strip().lower())
            if member_id:
                add_edge("issue", str(issue.id), "team_member", member_id, "assigned_to", metadata={"assignee": assignee})
        # Issue 正文里提到的其它 Issue/PR/代码文件
        _add_text_reference_edges(
            add_edge,
            "issue",
            str(issue.id),
            f"{issue.title}\n{issue.body or ''}",
            issues_by_number,
            prs_by_number,
            code_path_to_id,
            relation_for_issue="mentions",
            relation_for_pr="mentions",
            relation_for_code="mentions",
        )

    for pr in prs:
        text = f"{pr.title}\n{pr.body or ''}\n{pr.head_branch or ''}\n{pr.base_branch or ''}"
        closing_refs = _closing_issue_refs(text)  # 显式"关闭"的 Issue
        for number in closing_refs:
            issue = issues_by_number.get(number)
            if issue:
                # PR 解决了某个 Issue（强关系）
                add_edge("pull_request", str(pr.id), "issue", str(issue.id), "resolves", metadata={"issue_number": number})
        for number in _issue_refs(text) - closing_refs:
            # 提到但没说"关闭"的 Issue（弱关系 mentions）
            issue = issues_by_number.get(number)
            if issue:
                add_edge("pull_request", str(pr.id), "issue", str(issue.id), "mentions", metadata={"issue_number": number})
        author_member = team_by_name.get(str(pr.author or "").strip().lower())
        if author_member:
            add_edge("pull_request", str(pr.id), "team_member", author_member, "mentions", metadata={"author": pr.author})

    for pr_file in pr_files:
        # PR 触摸了哪个代码文件
        code_id = code_path_to_id.get(pr_file.filename) or _path_node_id(pr_file.filename)
        add_edge("pull_request", str(pr_file.pr_id), "code_file", code_id, "touches_file", metadata={"path": pr_file.filename, "status": pr_file.status})

    for run in runs:
        # 只关注"失败"的 CI 运行（成功的 CI 关系价值低）
        if (run.conclusion or "").lower() != "failure":
            continue
        text = f"{run.name}\n{run.logs_text or ''}"
        for number in _pr_refs(text):
            pr = prs_by_number.get(number)
            if pr:
                # 这次 CI 失败关联到某个 PR（置信度 0.85）
                add_edge("ci_run", str(run.id), "pull_request", str(pr.id), "failed_in_ci", confidence=0.85, metadata={"pr_number": number})
        for number in _issue_refs(text):
            issue = issues_by_number.get(number)
            if issue:
                add_edge("ci_run", str(run.id), "issue", str(issue.id), "failed_in_ci", confidence=0.75, metadata={"issue_number": number})

    for doc in docs:
        if doc.source_type == "team_member":
            continue  # 团队成员节点单独处理，这里跳过
        from_type = "memory_note" if doc.source_type == "memory_note" else "document"
        relation = "cites" if doc.source_type == "memory_note" else "mentions"
        text = f"{doc.title}\n{doc.content}\n{json.dumps(doc.metadata, ensure_ascii=False, default=str)}"  # 文档文本+元数据
        _add_text_reference_edges(
            add_edge,
            from_type,
            doc.source_id,
            text,
            issues_by_number,
            prs_by_number,
            code_path_to_id,
            relation_for_issue=relation,
            relation_for_pr=relation,
            relation_for_code="cites" if doc.source_type == "memory_note" else "documents",
        )

    for decision in _decision_nodes(db, repo_id):
        # 记忆里的"决策"节点：它的正文引用了哪些 Issue/PR/代码
        text = str(decision["metadata"].get("content") or decision["title"])
        _add_text_reference_edges(
            add_edge,
            "session_decision",
            decision["id"],
            text,
            issues_by_number,
            prs_by_number,
            code_path_to_id,
            relation_for_issue="cites",
            relation_for_pr="cites",
            relation_for_code="cites",
        )
        for citation in decision["metadata"].get("citations") or []:
            # 决策里显式记录的引用（citations 字段）
            target_type = normalize_node_type(str(citation.get("type") or citation.get("source_type") or ""))
            target_id = str(citation.get("id") or citation.get("source_id") or "")
            if target_type and target_id:
                add_edge("session_decision", decision["id"], target_type, target_id, "cites", confidence=0.9, source="memory_citation")

    for run in db.query(AgentRun).filter(AgentRun.repo_id == repo_id).order_by(AgentRun.created_at.desc()).limit(25):
        # 最近 25 次 Agent 运行：从工具调用轨迹里抽"它把谁当证据用过"
        source_id = f"agent_run:{run.id}"
        for citation in _extract_citation_like_values(run.tool_calls or []):
            target_type = normalize_node_type(str(citation.get("type") or citation.get("source_type") or ""))
            target_id = str(citation.get("id") or citation.get("source_id") or "")
            if target_type and target_id:
                add_edge("session_decision", source_id, target_type, target_id, "used_as_evidence", confidence=0.75, source="tool_call_trace")

    return list(edges.values())  # 字典去重后的边列表


def _add_text_reference_edges(
    add_edge: Any,
    from_type: str,
    from_id: str,
    text: str,
    issues_by_number: dict[int, Issue],
    prs_by_number: dict[int, PullRequest],
    code_path_to_id: dict[str, str],
    *,
    relation_for_issue: str,
    relation_for_pr: str,
    relation_for_code: str,
) -> None:
    # 内部：从一段文本里用正则抽出 Issue/PR/代码文件的引用，并以指定关系加边。
    # 为什么集中写：Issue/PR/文档/记忆都需要在正文里"扫引用"，逻辑一样，抽成公共函数避免重复。
    for number in _issue_refs(text):
        issue = issues_by_number.get(number)
        if issue:
            add_edge(from_type, from_id, "issue", str(issue.id), relation_for_issue, confidence=0.75, metadata={"issue_number": number})
    for number in _pr_refs(text):
        pr = prs_by_number.get(number)
        if pr:
            add_edge(from_type, from_id, "pull_request", str(pr.id), relation_for_pr, confidence=0.8, metadata={"pr_number": number})
    for path, code_id in code_path_to_id.items():
        # 路径太短（<6 字符）容易误匹配，跳过
        if len(path) >= 6 and path in text:
            add_edge(from_type, from_id, "code_file", code_id, relation_for_code, confidence=0.8, metadata={"path": path})


def _collect_nodes(db: Session, repo_id: uuid.UUID) -> dict[str, dict[str, Any]]:
    # 收集图谱里所有节点（每个实体一个），返回 {节点key: 节点信息}。
    nodes: dict[str, dict[str, Any]] = {}

    def put(node_type: str, node_id: str, title: str, subtitle: str = "", metadata: dict[str, Any] | None = None, highlighted: bool = False) -> None:
        # 内部：放一个节点；已存在则合并元数据（不覆盖已有高亮）
        normalized = normalize_node_type(node_type)
        key = _node_key(normalized, str(node_id))
        if key in nodes:
            nodes[key]["metadata"].update(metadata or {})
            nodes[key]["highlighted"] = nodes[key]["highlighted"] or highlighted
            return
        nodes[key] = {
            "id": str(node_id),
            "type": normalized,
            "title": title or str(node_id),
            "subtitle": subtitle or "",
            "metadata": metadata or {},
            "highlighted": highlighted,
        }

    for issue in db.query(Issue).filter(Issue.repo_id == repo_id).all():
        put("issue", str(issue.id), f"#{issue.number} {issue.title}", issue.state, {"number": issue.number, "labels": issue.labels or [], "assignees": issue.assignees or []})
    for pr in db.query(PullRequest).filter(PullRequest.repo_id == repo_id).all():
        subtitle = "merged" if pr.merged_at else pr.state  # 合并了就标 merged
        put("pull_request", str(pr.id), f"#{pr.number} {pr.title}", subtitle, {"number": pr.number, "author": pr.author, "head_branch": pr.head_branch})
    for run in db.query(WorkflowRun).filter(WorkflowRun.repo_id == repo.id).all():
        put("ci_run", str(run.id), run.name, run.conclusion or run.status, {"status": run.status, "conclusion": run.conclusion, "html_url": run.html_url})

    docs = _document_groups(db, repo_id)
    for doc in docs:
        if doc.source_type == "team_member":
            # 团队成员节点
            put("team_member", doc.source_id, doc.metadata.get("name") or doc.title, doc.metadata.get("role") or "", doc.metadata)
        elif doc.source_type == "memory_note":
            put("memory_note", doc.source_id, doc.title, "memory note", doc.metadata)
        elif doc.source_type in DOCUMENT_NODE_TYPES:
            put("document", doc.source_id, doc.metadata.get("display_title") or doc.title, doc.source_type, doc.metadata | {"source_type": doc.source_type})

    for pr_file in db.query(PRFile).join(PullRequest, PRFile.pr_id == PullRequest.id).filter(PullRequest.repo_id == repo_id).all():
        # 代码文件节点（用路径当 ID）
        put("code_file", _path_node_id(pr_file.filename), pr_file.filename, "PR touched file", {"path": pr_file.filename, "status": pr_file.status})

    for decision in _decision_nodes(db, repo_id):
        put("session_decision", decision["id"], decision["title"], decision["subtitle"], decision["metadata"])

    for run in db.query(AgentRun).filter(AgentRun.repo_id == repo_id).order_by(AgentRun.created_at.desc()).limit(25):
        put("session_decision", f"agent_run:{run.id}", f"AI answer: {run.route}", "tool evidence path", {"route": run.route, "created_at": run.created_at.isoformat() if run.created_at else None})

    for edge in db.query(KnowledgeGraphEdge).filter(KnowledgeGraphEdge.repo_id == repo_id).all():
        # 边里引用、但上面没建出来的节点：用占位节点补上，保证图完整（边两端都有节点）
        from_key = _node_key(edge.from_type, edge.from_id)
        to_key = _node_key(edge.to_type, edge.to_id)
        nodes.setdefault(from_key, _fallback_node(edge.from_type, edge.from_id))
        nodes.setdefault(to_key, _fallback_node(edge.to_type, edge.to_id))

    return nodes


def _select_subgraph(
    nodes_by_key: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    center_key: str | None,
    depth: int,
) -> tuple[list[str], bool]:
    # 从所有节点里挑出"要展示的子集"：有中心点时做 BFS 扩散 depth 跳；没有则按优先级取前 N 个。
    # 返回 (选中的节点 key 列表, 是否还有更多被截断)
    if center_key and center_key in nodes_by_key:
        adjacency: dict[str, set[str]] = defaultdict(set)  # 邻接表（无向）
        for edge in edges:
            a = _node_key(edge["from_type"], edge["from_id"])
            b = _node_key(edge["to_type"], edge["to_id"])
            adjacency[a].add(b)
            adjacency[b].add(a)  # 双向加，图当无向走
        selected: list[str] = []
        seen = {center_key}
        queue: deque[tuple[str, int]] = deque([(center_key, 0)])  # BFS 队列：(节点, 距中心距离)
        while queue and len(selected) < GRAPH_NODE_LIMIT:
            key, distance = queue.popleft()
            selected.append(key)
            if distance >= depth:
                continue  # 到深度上限就不再往外扩
            for nxt in sorted(adjacency.get(key, []), key=lambda item: nodes_by_key.get(item, {}).get("title", item)):
                if nxt in seen:
                    continue
                seen.add(nxt)
                queue.append((nxt, distance + 1))
        return selected, bool(queue)  # queue 还有东西说明被节点上限截断

    # 没有中心点：先收集"至少连了一条边"的节点，再按 (是否孤立, 类型优先级, 标题) 排序取前 N
    touched: set[str] = set()
    for edge in edges:
        touched.add(_node_key(edge["from_type"], edge["from_id"]))
        touched.add(_node_key(edge["to_type"], edge["to_id"]))
    ranked = sorted(
        nodes_by_key,
        key=lambda key: (0 if key in touched else 1, _node_priority(nodes_by_key[key]["type"]), nodes_by_key[key]["title"]),
    )
    selected = ranked[:GRAPH_NODE_LIMIT]
    return selected, len(ranked) > GRAPH_NODE_LIMIT


def _document_groups(db: Session, repo_id: uuid.UUID) -> list[DocumentGroup]:
    # 把"同一来源被切成多块(chunk)"的文档按 (source_type, source_id) 合并成一组。
    groups: dict[tuple[str, str], list[Document]] = defaultdict(list)
    for doc in db.query(Document).filter(Document.repo_id == repo_id).order_by(Document.created_at.asc()).all():
        groups[(doc.source_type, str(doc.source_id))].append(doc)

    output: list[DocumentGroup] = []
    for (source_type, source_id), docs in groups.items():
        # 同组文档按 chunk 顺序排，取第一块的标题/元数据，内容拼前 4 块（够画图用）
        docs.sort(key=lambda item: ((item.meta or {}).get("chunk_index", 0), item.created_at))
        first = docs[0]
        metadata = dict(first.meta or {})
        title = str(metadata.get("display_title") or first.title)
        content = "\n".join(doc.content or "" for doc in docs[:4])
        output.append(DocumentGroup(source_type, source_id, title, content, metadata, first.created_at))
    return output


def _decision_nodes(db: Session, repo_id: uuid.UUID) -> list[dict[str, Any]]:
    # 从线程记忆和会话记忆里抽出"决策"节点（每条 decision 变成一个 session_decision 节点）。
    output: list[dict[str, Any]] = []
    thread_memories = db.query(ThreadMemory).filter(ThreadMemory.repo_id == repo_id).all()
    conversation_memories = db.query(ConversationMemory).filter(ConversationMemory.repo_id == repo_id).all()
    # 把两种记忆打上不同前缀，统一成 [(prefix, memory), ...]
    for prefix, memory in [("thread", item) for item in thread_memories] + [("conversation", item) for item in conversation_memories]:
        for index, decision in enumerate(memory.decisions or []):
            text = str(decision).strip()
            if not text:
                continue
            output.append(
                {
                    "id": f"{prefix}:{memory.id}:decision:{index}",  # 稳定唯一 ID
                    "title": _clip(text, 72),
                    "subtitle": "session decision",
                    "metadata": {
                        "content": text,
                        "memory_id": str(memory.id),
                        "citations": memory.citations or [],
                        "sessions_incorporated": memory.sessions_incorporated,
                    },
                }
            )
    return output


def _team_members_by_name(docs: list[DocumentGroup]) -> dict[str, str]:
    # 建立"成员名(小写) -> 节点 ID"的查找表（team_member 文档里带 name 字段）
    output: dict[str, str] = {}
    for doc in docs:
        if doc.source_type != "team_member":
            continue
        for value in [doc.metadata.get("name"), doc.title]:
            text = str(value or "").strip().lower()
            if text:
                output[text] = doc.source_id
    return output


def _code_path_to_node_id(pr_files: list[PRFile]) -> dict[str, str]:
    # 建立"文件路径 -> 代码节点 ID"的查找表（ID 形如 "path:src/foo.py"）
    output: dict[str, str] = {}
    for pr_file in pr_files:
        output.setdefault(pr_file.filename, _path_node_id(pr_file.filename))
    return output


def _extract_citation_like_values(value: Any) -> list[dict[str, Any]]:
    # 递归遍历任意结构（dict/list），找出"像引用"的对象（同时含 type/source_type 与 id/source_id）。
    # 为什么递归：工具调用轨迹是嵌套结构，要一路挖到最里层的引用。
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if ("source_type" in value or "type" in value) and ("source_id" in value or "id" in value):
            found.append(value)  # 像引用就收下
        for child in value.values():
            found.extend(_extract_citation_like_values(child))  # 继续往下挖
    elif isinstance(value, list):
        for child in value:
            found.extend(_extract_citation_like_values(child))
    return found


def _edge_payload(row: KnowledgeGraphEdge) -> dict[str, Any]:
    # 把边数据库行转成字典（meta 字段改名 metadata 更友好）
    return {
        "id": str(row.id),
        "from_type": row.from_type,
        "from_id": row.from_id,
        "to_type": row.to_type,
        "to_id": row.to_id,
        "relation": row.relation,
        "confidence": row.confidence,
        "source": row.source,
        "metadata": row.meta or {},
    }


def _fallback_node(node_type: str, node_id: str, highlighted: bool = False) -> dict[str, Any]:
    # 占位节点：当某条边引用了"没在节点表登记的实体"时，造一个最简节点保证图完整
    return {
        "id": str(node_id),
        "type": normalize_node_type(node_type),
        "title": str(node_id),
        "subtitle": "referenced item",
        "metadata": {},
        "highlighted": highlighted,
    }


def _node_key(node_type: str, node_id: str) -> str:
    # 节点的唯一 key：用 "类型:ID" 拼接（便于查重和建邻接表）
    return f"{normalize_node_type(node_type)}:{node_id}"


def _edge_id(from_type: str, from_id: str, to_type: str, to_id: str, relation: str) -> str:
    # 用 uuid5 + 固定命名空间生成"稳定的"边 ID：同样两端+关系永远得到同一 ID（幂等重建友好）
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"devflow-graph:{from_type}:{from_id}:{relation}:{to_type}:{to_id}"))


def _path_node_id(path: str) -> str:
    # 代码文件路径对应的节点 ID 前缀
    return f"path:{path}"


def _issue_refs(text: str) -> set[int]:
    # 从文本提取所有 "#数字" Issue 编号（去重成集合）
    return {int(match.group(1)) for match in ISSUE_REF_RE.finditer(text or "")}


def _closing_issue_refs(text: str) -> set[int]:
    # 提取"明确要关闭"的 Issue 编号（fix/close/resolve #数字）
    return {int(match.group(1)) for match in CLOSING_ISSUE_RE.finditer(text or "")}


def _pr_refs(text: str) -> set[int]:
    # 提取 "PR #数字" 形式的 PR 编号
    return {int(match.group(1)) for match in PR_REF_RE.finditer(text or "")}


def _node_priority(node_type: str) -> int:
    # 节点展示优先级：Issue/PR/CI 最该先看，文档/代码文件靠后（无中心点时排序用）
    order = {
        "issue": 0,
        "pull_request": 1,
        "ci_run": 2,
        "team_member": 3,
        "memory_note": 4,
        "session_decision": 5,
        "document": 6,
        "code_file": 7,
    }
    return order.get(node_type, 20)  # 未知类型排最后


def _clip(text: str, limit: int) -> str:
    # 截断文本（超长加"..."），用于节点标题
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."
