"""DevMind AI module."""

# code_graph.py —— 代码图谱（Code Graph：符号与调用关系）路由层
#
# 业务域：代码静态分析。DevFlow 把仓库里的函数/类/变量等「符号（symbol）」以及它们之间的
# 调用/引用关系抽成「代码图谱」，让前端能做代码检索、看某个函数被谁调用。
#
# 暴露端点：
#   POST /api/repos/{repo_id}/code-graph/rebuild —— 重建代码图谱（重新分析）
#   GET  /api/repos/{repo_id}/code-symbols       —— 列出代码符号（可按路径/PR 过滤）
#   GET  /api/repos/{repo_id}/code-graph/search  —— 搜代码符号 + 关系 + 代码片段
#
# 依赖 service：app.services.code_analysis（分析）、app.services.code_search（读代码片段）、
#              app.services.worktree_manager（PR 分支快照路径）
from uuid import UUID  # UUID

from fastapi import APIRouter, Depends, HTTPException, Query  # 路由、依赖、异常、查询参数
from sqlalchemy import or_  # SQL「或」条件
from sqlalchemy.orm import Session  # 数据库会话

from app.db.models import CodeRelation, CodeSymbol, PullRequest, Repository, User  # 表模型
from app.db.session import get_db  # 会话依赖
from app.schemas.code_graph import CodeGraphSearchResponse, CodeSymbolResponse  # 响应模型
from app.services.code_analysis import sync_repository_code_analysis  # 重建代码分析
from app.services.code_search import read_code_excerpt, repository_checkout_path  # 读代码片段/仓库本地路径
from app.services.permissions import require_permission  # 权限校验
from app.services.worktree_manager import pr_worktree_path  # 取 PR 分支的工作区路径

router = APIRouter()  # 路由器


@router.post("/repos/{repo_id}/code-graph/rebuild")  # POST 重建代码图谱
async def rebuild_code_graph(
    repo_id: UUID,
    db: Session = Depends(get_db),
    _user: User = Depends(require_permission("repo:read")),  # 需读权限
) -> dict:
    repo = db.get(Repository, repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="未找到仓库")
    # 重新扫描仓库源码，抽出符号和关系；返回索引到的符号数量
    symbol_count = await sync_repository_code_analysis(db, repo)
    return {"repo_id": str(repo.id), "indexed_symbols": symbol_count}


@router.get("/repos/{repo_id}/code-symbols", response_model=list[CodeSymbolResponse])  # GET 符号列表
async def list_code_symbols(
    repo_id: UUID,
    path: str | None = None,  # 按路径模糊匹配（如 "src/utils"）
    pr_id: UUID | None = None,  # 只看某个 PR 引入的符号
    pr_number: int | None = None,  # 或按 PR 编号
    limit: int = Query(default=100, ge=1, le=500),  # 最多 100
    db: Session = Depends(get_db),
    _user: User = Depends(require_permission("repo:read")),
) -> list[CodeSymbol]:
    query = db.query(CodeSymbol).filter(CodeSymbol.repo_id == repo_id)  # 该仓库符号
    if path:
        query = query.filter(CodeSymbol.path.contains(path))  # 路径包含关键字
    if pr_id:
        query = query.filter(CodeSymbol.pr_id == pr_id)
    if pr_number:
        query = query.filter(CodeSymbol.pr_number == pr_number)
    # 按文件路径、起始行排序，并限制条数
    return query.order_by(CodeSymbol.path.asc(), CodeSymbol.start_line.asc()).limit(limit).all()


@router.get("/repos/{repo_id}/code-graph/search", response_model=CodeGraphSearchResponse)  # GET 搜代码图谱
async def search_code_graph(
    repo_id: UUID,
    q: str = Query(default=""),  # 搜索关键词（默认空）
    pr_id: UUID | None = None,
    pr_number: int | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    _user: User = Depends(require_permission("repo:read")),
) -> dict:
    repo = db.get(Repository, repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="未找到仓库")
    text = q.strip()  # 去掉首尾空格
    query = db.query(CodeSymbol).filter(CodeSymbol.repo_id == repo_id)
    if pr_id:
        query = query.filter(CodeSymbol.pr_id == pr_id)
    if pr_number:
        query = query.filter(CodeSymbol.pr_number == pr_number)
    if text:
        # 按名称或路径模糊匹配（或条件）
        query = query.filter(or_(CodeSymbol.name.contains(text), CodeSymbol.path.contains(text)))
    symbols = query.order_by(CodeSymbol.path.asc(), CodeSymbol.start_line.asc()).limit(limit).all()
    symbol_ids = [item.id for item in symbols]  # 命中的符号 ID
    relations = []  # 关系结果
    if symbol_ids:
        # 找和这些符号相关的「调用/引用」关系
        rows = (
            db.query(CodeRelation)
            .filter(
                CodeRelation.repo_id == repo_id,
                CodeRelation.pr_id == pr_id if pr_id else True,  # 有 pr_id 才过滤，否则不过滤
                CodeRelation.pr_number == pr_number if pr_number else True,
                or_(CodeRelation.source_symbol_id.in_(symbol_ids), CodeRelation.target_symbol_id.in_(symbol_ids), CodeRelation.target_name.in_([item.name for item in symbols])),
            )
            .limit(limit * 4)  # 关系比符号多取一些
            .all()
        )
        relations = [
            {
                "id": str(row.id),
                "source_symbol_id": str(row.source_symbol_id) if row.source_symbol_id else None,
                "target_symbol_id": str(row.target_symbol_id) if row.target_symbol_id else None,
                "source_name": row.source_name,
                "target_name": row.target_name,
                "relation_type": row.relation_type,  # 关系类型（调用/定义等）
                "path": row.path,
                "branch": row.branch,
                "pr_id": str(row.pr_id) if row.pr_id else None,
                "pr_number": row.pr_number,
                "metadata": row.meta or {},
            }
            for row in rows
        ]
    documents = []  # 代码片段结果
    checkout_path = repository_checkout_path(repo)  # 仓库在本地磁盘的路径
    selected_pr = None
    if pr_id:
        selected_pr = db.get(PullRequest, pr_id)  # 指定了 PR 就取它
    elif pr_number:
        selected_pr = (
            db.query(PullRequest)
            .filter(PullRequest.repo_id == repo_id, PullRequest.number == pr_number)
            .one_or_none()
        )
    if selected_pr and selected_pr.repo_id == repo_id:
        # 如果指定了 PR，优先用 PR 分支的快照路径（能看到 PR 里改的代码）
        snapshot_path = pr_worktree_path(repo, selected_pr)
        if snapshot_path.exists():
            checkout_path = snapshot_path
    if checkout_path.exists():
        # 给每个命中的符号读取一小段源码，方便前端直接预览
        for symbol in symbols[:limit]:
            snippet = read_code_excerpt(checkout_path, symbol.path, symbol.start_line, line_count=7)  # 读附近 7 行
            if not snippet:
                continue
            documents.append(
                {
                    "id": f"workspace:{symbol.path}:{symbol.start_line}",
                    "title": f"{symbol.path}:{symbol.start_line}",
                    "source_id": symbol.path,
                    "path": symbol.path,
                    "symbol": symbol.name,
                    "start_line": symbol.start_line,
                    "end_line": symbol.start_line + 6,
                    "pr_id": str(selected_pr.id) if selected_pr else None,
                    "pr_number": selected_pr.number if selected_pr else None,
                    "snippet": snippet[:1200],  # 片段最多 1200 字符
                }
            )
    # 把符号、关系、代码片段一起返回
    return {"query": text, "symbols": symbols, "relations": relations, "documents": documents}
