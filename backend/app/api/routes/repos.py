"""DevMind AI module."""

# repos.py —— 仓库（Repository）连接/同步/删除路由层
#
# 业务域：仓库管理。这是和 GitHub「对接」最核心的模块：连接一个仓库（支持本地路径、
# 远程 clone、或 demo 模式）、列出仓库、手动触发同步、选择仓库、删除仓库（级联清掉
# 所有相关数据：Issue/PR/CI/记忆/向量库等）。还做 git 远程地址解析等辅助工作。
#
# 暴露端点：
#   POST /api/repos/connect           —— 连接一个仓库
#   GET  /api/repos                   —— 列出仓库
#   POST /api/repos/{repo_id}/sync    —— 手动同步
#   POST /api/repos/{repo_id}/select  —— 选中（前台高亮）仓库
#   DELETE /api/repos/{repo_id}       —— 删除仓库（级联清理）
#   GET  /api/repos/{repo_id}/sync-status —— 同步状态
#
# 依赖 service：app.services.github.client.GitHubClient、app.services.repos_sync.sync_repository、
#              app.services.code_analysis、app.services.rag.vector_store
import os  # 操作系统接口（路径展开）
import re  # 正则（解析 git 远程地址）
import subprocess  # 调用外部命令（git）
from dataclasses import dataclass  # 轻量数据结构
from datetime import datetime, timezone  # 时间
from pathlib import Path  # 路径
from urllib.parse import urlparse  # 解析 URL
from uuid import UUID  # UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException  # 路由、后台任务、依赖、异常
from sqlalchemy import or_  # SQL 或
from sqlalchemy.orm import Session  # 会话

# 引入大量表模型（删除时要逐个清）
from app.db.models import ActionDraft, AgentRun, AgentTaskRun, AgentWorkflowRun, AnalysisResult, AuditLog, ChatFeedback, ChatMessage, ChatSession, CodeRelation, CodeSymbol, Conversation, ConversationMemory, Document, EvidenceItem, Issue, KnowledgeBaseConfig, KnowledgeGraphEdge, KnowledgeSourceDocument, MemoryCandidate, PRFile, PRReviewComment, ProjectIndexState, PullRequest, RecallEvent, Repository, RetrievalTestRun, ThreadMemory, WorkflowRun, Workspace
from app.db.session import get_db  # 会话依赖
from app.schemas.repos import RepoConnectRequest, RepoConnectResponse, RepoResponse, RepoSyncRequest, RepoSyncResponse  # 模型
from app.core.config import settings  # 配置（demo 模式开关、密钥）
from app.core.security import encrypt_token  # 加密 GitHub token 再入库
from app.services.github.client import GitHubClient  # GitHub 客户端
from app.services.code_analysis import sync_repository_code_analysis_by_id  # 代码分析（后台）
from app.services.rag.vector_store import delete_milvus_documents  # 删向量库文档
from app.services.repos_sync import sync_repository  # 同步仓库（Issue/PR/CI）

router = APIRouter()  # 路由器

# 支持的「连接提供方」类型
SUPPORTED_CONNECT_PROVIDERS = {"github", "github_compatible"}
# GitHub 官方主机名（用来判断是不是 github.com）
GITHUB_HOSTS = {"github.com", "www.github.com"}


@dataclass(frozen=True)  # 不可变的数据类：解析后的 git 远程信息
class GitRemoteInfo:
    owner: str  # 仓库所有者
    repo: str  # 仓库名
    clone_url: str  # clone 地址
    host: str | None = None  # 主机（自建 GitLab 等）
    default_branch: str | None = None  # 默认分支


def _clean_text(value: str | None) -> str | None:
    # 去掉首尾空白，空串转 None（统一「空」的表示）
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _resolve_user_path(raw_path: str, field_name: str) -> Path:
    # 把用户给的路径字符串解析成绝对 Path；支持 ~ 和环境变量、去掉引号
    cleaned = raw_path.strip().strip('"').strip("'")
    if not cleaned:
        raise HTTPException(status_code=400, detail=f"{field_name} cannot be empty.")
    expanded = os.path.expandvars(os.path.expanduser(cleaned))  # 展开 ~ 和 $VAR
    path = Path(expanded)
    if not path.is_absolute():
        path = Path.cwd() / path  # 相对就拼到当前目录
    return path.resolve()  # 解析成规范绝对路径


def _git_output(args: list[str], cwd: Path) -> str:
    # 在 cwd 目录运行 git 命令，返回标准输出；失败抛 400
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            check=True,  # 非零退出码就抛异常
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,  # 30 秒超时，避免卡死
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail="未找到 git 可执行文件，无法检查本地仓库。") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise HTTPException(status_code=400, detail=f"Failed to inspect local repository: {detail}") from exc
    return result.stdout.strip()


def _parse_git_remote_url(remote_url: str) -> GitRemoteInfo:
    # 解析 git 远程地址（支持两种写法：git@host:owner/repo.git 和 https://host/owner/repo.git）
    value = remote_url.strip()
    host: str | None = None
    path = ""
    # SCP 风格：user@host:path（且没有 ://）
    scp_like = re.match(r"^(?:[^@]+@)?(?P<host>[^:]+):(?P<path>.+)$", value)
    if scp_like and "://" not in value:
        host = scp_like.group("host")
        path = scp_like.group("path")
    else:
        # URL 风格：用 urlparse 拆
        parsed = urlparse(value)
        host = parsed.hostname or parsed.netloc or None
        path = parsed.path
    # 路径按 / 拆，取最后两段作为 owner/repo
    parts = [item.strip() for item in path.replace("\\", "/").strip("/").split("/") if item.strip()]
    if len(parts) < 2:
        raise HTTPException(status_code=400, detail=f"Cannot parse owner/repository from git remote URL: {remote_url}")
    owner = parts[-2]
    repo = re.sub(r"\.git$", "", parts[-1], flags=re.IGNORECASE)  # 去掉结尾 .git
    if not owner or not repo:
        raise HTTPException(status_code=400, detail=f"Cannot parse owner/repository from git remote URL: {remote_url}")
    return GitRemoteInfo(owner=owner, repo=repo, clone_url=value, host=host.lower() if host else None)


def _inspect_local_repository(raw_path: str) -> tuple[Path, GitRemoteInfo]:
    # 检查本地仓库目录：确认是 git 仓库，取远程地址、当前分支
    path = _resolve_user_path(raw_path, "local_path")
    if not path.exists() or not path.is_dir():
        raise HTTPException(status_code=400, detail=f"Local repository path does not exist or is not a directory: {path}")
    # 确认在 git 工作树内
    inside_work_tree = _git_output(["rev-parse", "--is-inside-work-tree"], path).lower()
    if inside_work_tree != "true":
        raise HTTPException(status_code=400, detail=f"Local path is not inside a git repository: {path}")
    repo_root = Path(_git_output(["rev-parse", "--show-toplevel"], path)).resolve()  # 仓库根目录
    remote_url = _git_output(["remote", "get-url", "origin"], repo_root)  # origin 远程地址
    remote_info = _parse_git_remote_url(remote_url)
    current_branch = _git_output(["branch", "--show-current"], repo_root) or None
    return repo_root, GitRemoteInfo(
        owner=remote_info.owner,
        repo=remote_info.repo,
        clone_url=remote_info.clone_url,
        host=remote_info.host,
        default_branch=current_branch,
    )


def _infer_provider(requested_provider: str | None, remote_host: str | None) -> str:
    # 推断提供方：如果远端不是 github 官方、却写的是 github，就当作 github_compatible（自建 Git）
    provider = requested_provider or "github"
    if remote_host and remote_host not in GITHUB_HOSTS and provider == "github":
        return "github_compatible"
    return provider


def _effective_api_base_url(provider: str, raw_base_url: str | None, remote_host: str | None) -> str | None:
    # 计算 GitHub API 基地址：github 用官方地址；其它情况用给定或推断的地址
    if provider == "github":
        return "https://api.github.com"
    base_url = _clean_text(raw_base_url)
    if base_url:
        return base_url.rstrip("/")  # 去掉结尾斜杠
    if remote_host and remote_host not in GITHUB_HOSTS:
        return f"https://{remote_host}/api/v3"  # 自建 Git 通常走 /api/v3
    return None


def _clone_target_from_parent(raw_parent: str, repo_name: str) -> Path:
    # 计算 clone 目标目录；检查父目录存在、目标不冲突
    parent = _resolve_user_path(raw_parent, "clone_parent_dir")
    if parent.exists() and not parent.is_dir():
        raise HTTPException(status_code=400, detail=f"Download parent path is not a directory: {parent}")
    target = (parent / repo_name).resolve()
    if target.exists() and not target.is_dir():
        raise HTTPException(status_code=400, detail=f"Download target exists and is not a directory: {target}")
    if target.exists() and not (target / ".git").exists() and any(target.iterdir()):
        # 目标存在但不是空 git 仓库（里面已有其它文件），拒绝以免覆盖
        raise HTTPException(
            status_code=400,
            detail=f"Download target already exists and is not an empty git repository: {target}",
        )
    return target


@router.post("/connect", response_model=RepoConnectResponse)  # POST 连接仓库
async def connect_repo(payload: RepoConnectRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)) -> RepoConnectResponse:
    local_path = _clean_text(payload.local_path)  # 本地路径（可选）
    clone_parent_dir = _clean_text(payload.clone_parent_dir)  # clone 父目录（可选）
    owner = _clean_text(payload.owner)
    repo_name = _clean_text(payload.repo)
    requested_provider = payload.provider or "github"
    token = payload.token or payload.github_token  # GitHub token（可选）
    checkout_path: Path | None = None
    checkout_mode = "managed"  # 管理模式（由系统管理 clone）
    remote_info: GitRemoteInfo | None = None

    if local_path:
        # 给了本地路径：直接检查本地仓库，owner/repo 从它推导
        checkout_path, remote_info = _inspect_local_repository(local_path)
        owner = remote_info.owner
        repo_name = remote_info.repo
        checkout_mode = "local"  # 本地模式
    elif not owner or not repo_name:
        # 既没本地路径又没 owner/repo，无法连接
        raise HTTPException(status_code=400, detail="Provide either a local repository path or owner/repository.")

    provider = _infer_provider(requested_provider, remote_info.host if remote_info else None)
    base_url = _effective_api_base_url(provider, payload.api_base_url, remote_info.host if remote_info else None)
    if provider not in SUPPORTED_CONNECT_PROVIDERS:
        # 不支持的提供方（比如 gitlab 还没实现）
        raise HTTPException(
            status_code=400,
            detail=f"{provider} real connection is not implemented yet. Use demo mode or github_compatible with a GitHub-compatible REST API.",
        )

    if not local_path and clone_parent_dir:
        # 给了 clone 父目录就计算目标路径
        checkout_path = _clone_target_from_parent(clone_parent_dir, repo_name or "repository")

    full_name = f"{owner}/{repo_name}"  # owner/repo
    effective_demo_mode = payload.demo_mode  # 是否 demo 模式
    fallback_message: str | None = None
    repo_data = {
        "id": None,
        "description": "Demo repository for DevMind AI",
        "default_branch": remote_info.default_branch if remote_info and remote_info.default_branch else "main",
        "clone_url": remote_info.clone_url if remote_info else f"https://github.com/{owner}/{repo_name}.git",
    }
    if not payload.demo_mode:
        # 非 demo：真正调 GitHub 验证仓库是否存在/可访问
        try:
            repo_data = await GitHubClient(token, base_url).get_repo(owner or "", repo_name or "")
        except Exception as exc:
            if token or not settings.demo_mode:
                # 有 token 或全局要求真实模式，验证失败就报错
                raise HTTPException(status_code=400, detail=f"Repository connection failed: {exc}") from exc
            # 否则退化为「本地缓存模式」：不验证，先建记录
            effective_demo_mode = True
            fallback_message = (
                f"GitHub verification failed without a token: {exc}. "
                "Added the repository in local cache mode; add a token or retry later for full GitHub sync."
            )

    # 查找是否已连过这个仓库（按 full_name）
    repo = db.query(Repository).filter(Repository.full_name == full_name).one_or_none()
    if repo is None:
        # 没连过就新建
        repo = Repository(owner=owner or "", name=repo_name or "", full_name=full_name)
        db.add(repo)
    # 更新仓库字段
    repo.owner = owner or repo.owner
    repo.name = repo_name or repo.name
    repo.provider = provider
    repo.api_base_url = base_url
    if token:
        # 有 token 就加密后入库（明文不落库）
        repo.github_token_encrypted = encrypt_token(token, settings.token_encryption_key)
    repo.description = repo_data.get("description")
    repo.default_branch = repo_data.get("default_branch")
    repo.clone_url = repo_data.get("clone_url") or (remote_info.clone_url if remote_info else f"https://github.com/{owner}/{repo_name}.git")
    repo.local_path = str(checkout_path) if checkout_path else None
    repo.checkout_mode = checkout_mode
    repo.github_id = repo_data.get("id")
    db.commit()  # 先提交仓库记录
    db.refresh(repo)
    sync_result: RepoSyncResponse | None = None
    if not effective_demo_mode:
        # 真实模式：同步 Issue/PR/CI，并在后台分析代码
        sync_result = await sync_repository(
            db,
            repo,
            RepoSyncRequest(
                sync_issues=payload.sync_issues,
                sync_pull_requests=payload.sync_pull_requests,
                sync_workflow_runs=payload.sync_workflow_runs,
                limit=payload.initial_sync_limit,
            ),
        )
        background_tasks.add_task(sync_repository_code_analysis_by_id, repo.id)  # 后台做代码分析
    elif fallback_message:
        background_tasks.add_task(sync_repository_code_analysis_by_id, repo.id)
    # 返回连接结果
    return RepoConnectResponse(
        repo_id=repo.id,
        full_name=repo.full_name,
        provider=repo.provider,
        api_base_url=repo.api_base_url,
        clone_url=repo.clone_url,
        local_path=repo.local_path,
        checkout_mode=repo.checkout_mode,
        default_branch=repo.default_branch,
        connected=True,
        demo_mode=effective_demo_mode,
        message=(
            fallback_message
            or (
                "Demo mode only created a local repository record."
                if effective_demo_mode
                else "Repository connection verified. Initial Issue/PR/CI sync completed; code checkout and graph analysis started."
            )
        ),
        # 同步状态：有结果用结果，否则 fallback/skipped
        sync_status=sync_result.status if sync_result else ("fallback" if fallback_message else "skipped"),
        synced=sync_result.synced if sync_result else {"issues": 0, "pull_requests": 0, "workflow_runs": 0},
    )


@router.get("", response_model=list[RepoResponse])  # GET 仓库列表
async def list_repos(db: Session = Depends(get_db)) -> list[Repository]:
    # 返回所有「非知识库」类型的仓库，按更新时间倒序
    return (
        db.query(Repository)
        .filter(Repository.provider != "knowledge_base")
        .order_by(Repository.updated_at.desc())
        .all()
    )


@router.post("/{repo_id}/sync", response_model=RepoSyncResponse)  # POST 手动同步
async def sync_repo(
    repo_id: UUID,
    payload: RepoSyncRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> RepoSyncResponse:
    repo = db.get(Repository, repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="未找到仓库")
    result = await sync_repository(db, repo, payload)  # 同步 Issue/PR/CI
    background_tasks.add_task(sync_repository_code_analysis_by_id, repo.id)  # 后台分析代码
    return result


@router.post("/{repo_id}/select")  # POST 选中仓库（仅更新时间，前端高亮用）
async def select_repo(repo_id: UUID, db: Session = Depends(get_db)) -> dict:
    repo = db.get(Repository, repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="未找到仓库")
    repo.updated_at = datetime.now(timezone.utc)  # 刷新「最近使用」时间
    db.commit()
    return {"repo_id": str(repo.id), "full_name": repo.full_name, "selected": True}


@router.delete("/{repo_id}")  # DELETE 删除仓库（级联清理所有相关数据）
async def delete_repo(repo_id: UUID, db: Session = Depends(get_db)) -> dict:
    repo = db.get(Repository, repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="未找到仓库")

    # 1) 先收集该仓库下各子资源的 ID（删主记录前要先删关联）
    issue_ids = [item.id for item in db.query(Issue.id).filter(Issue.repo_id == repo.id).all()]
    pr_ids = [item.id for item in db.query(PullRequest.id).filter(PullRequest.repo_id == repo.id).all()]
    run_ids = [item.id for item in db.query(WorkflowRun.id).filter(WorkflowRun.repo_id == repo.id).all()]
    conversation_ids = [item.id for item in db.query(Conversation.id).filter(Conversation.repo_id == repo.id).all()]
    # 会话可能通过 conversation_id 间接关联，也收集
    session_ids = [
        item.id
        for item in db.query(ChatSession.id)
        .filter(
            or_(
                ChatSession.repo_id == repo.id,
                ChatSession.conversation_id.in_(conversation_ids) if conversation_ids else False,
            )
        )
        .all()
    ]
    workflow_ids = [
        item.id
        for item in db.query(AgentWorkflowRun.id)
        .filter(
            or_(
                AgentWorkflowRun.repo_id == repo.id,
                AgentWorkflowRun.conversation_id.in_(conversation_ids) if conversation_ids else False,
            )
        )
        .all()
    ]

    # 2) 删除分析结果（按 target 类型+ID 批量删）
    for target_type, ids in [
        ("issue", issue_ids),
        ("pull_request", pr_ids),
        ("workflow_run", run_ids),
    ]:
        if ids:
            db.query(AnalysisResult).filter(
                AnalysisResult.target_type == target_type,
                AnalysisResult.target_id.in_(ids),
            ).delete(synchronize_session=False)  # 不同步会话，速度快

    # 3) 收集知识源文档（用于后面删磁盘文件）
    knowledge_sources = (
        db.query(KnowledgeSourceDocument)
        .filter(KnowledgeSourceDocument.repo_id == repo.id)
        .all()
    )
    stored_paths = [Path(source.stored_path) for source in knowledge_sources]

    # 4) 逐个删各类子表（保持顺序，先删最细的依赖）
    if pr_ids:
        db.query(PRFile).filter(PRFile.pr_id.in_(pr_ids)).delete(synchronize_session=False)
        db.query(PRReviewComment).filter(PRReviewComment.pr_id.in_(pr_ids)).delete(synchronize_session=False)
    db.query(Document).filter(Document.repo_id == repo.id).delete(synchronize_session=False)
    db.query(RetrievalTestRun).filter(RetrievalTestRun.repo_id == repo.id).delete(synchronize_session=False)
    db.query(KnowledgeSourceDocument).filter(KnowledgeSourceDocument.repo_id == repo.id).delete(synchronize_session=False)
    db.query(KnowledgeBaseConfig).filter(KnowledgeBaseConfig.repo_id == repo.id).delete(synchronize_session=False)
    db.query(KnowledgeGraphEdge).filter(KnowledgeGraphEdge.repo_id == repo.id).delete(synchronize_session=False)
    db.query(ProjectIndexState).filter(ProjectIndexState.repo_id == repo.id).delete(synchronize_session=False)
    db.query(CodeRelation).filter(CodeRelation.repo_id == repo.id).delete(synchronize_session=False)
    db.query(CodeSymbol).filter(CodeSymbol.repo_id == repo.id).delete(synchronize_session=False)
    db.query(ActionDraft).filter(ActionDraft.repo_id == repo.id).delete(synchronize_session=False)
    db.query(AuditLog).filter(AuditLog.repo_id == repo.id).delete(synchronize_session=False)
    db.query(ChatFeedback).filter(ChatFeedback.repo_id == repo.id).delete(synchronize_session=False)
    # 记忆候选（可能按 repo/会话/会话id 关联）
    db.query(MemoryCandidate).filter(
        or_(
            MemoryCandidate.repo_id == repo.id,
            MemoryCandidate.conversation_id.in_(conversation_ids) if conversation_ids else False,
            MemoryCandidate.session_id.in_(session_ids) if session_ids else False,
        )
    ).delete(synchronize_session=False)
    db.query(RecallEvent).filter(
        or_(
            RecallEvent.repo_id == repo.id,
            RecallEvent.conversation_id.in_(conversation_ids) if conversation_ids else False,
        )
    ).delete(synchronize_session=False)
    if workflow_ids:
        db.query(AgentTaskRun).filter(AgentTaskRun.workflow_id.in_(workflow_ids)).delete(synchronize_session=False)
    db.query(AgentWorkflowRun).filter(
        or_(
            AgentWorkflowRun.repo_id == repo.id,
            AgentWorkflowRun.conversation_id.in_(conversation_ids) if conversation_ids else False,
        )
    ).delete(synchronize_session=False)
    db.query(ChatMessage).filter(
        or_(
            ChatMessage.repo_id == repo.id,
            ChatMessage.conversation_id.in_(conversation_ids) if conversation_ids else False,
        )
    ).delete(synchronize_session=False)
    db.query(ChatSession).filter(
        or_(
            ChatSession.repo_id == repo.id,
            ChatSession.conversation_id.in_(conversation_ids) if conversation_ids else False,
        )
    ).delete(synchronize_session=False)
    db.query(ConversationMemory).filter(
        or_(
            ConversationMemory.repo_id == repo.id,
            ConversationMemory.conversation_id.in_(conversation_ids) if conversation_ids else False,
        )
    ).delete(synchronize_session=False)
    db.query(ThreadMemory).filter(ThreadMemory.repo_id == repo.id).delete(synchronize_session=False)
    db.query(EvidenceItem).filter(
        or_(
            EvidenceItem.repo_id == repo.id,
            EvidenceItem.conversation_id.in_(conversation_ids) if conversation_ids else False,
        )
    ).delete(synchronize_session=False)
    db.query(AgentRun).filter(
        or_(
            AgentRun.repo_id == repo.id,
            AgentRun.conversation_id.in_(conversation_ids) if conversation_ids else False,
        )
    ).delete(synchronize_session=False)
    db.query(Conversation).filter(Conversation.repo_id == repo.id).delete(synchronize_session=False)
    db.query(Issue).filter(Issue.repo_id == repo.id).delete(synchronize_session=False)
    db.query(PullRequest).filter(PullRequest.repo_id == repo.id).delete(synchronize_session=False)
    db.query(WorkflowRun).filter(WorkflowRun.repo_id == repo.id).delete(synchronize_session=False)
    # 5) 从各 workspace 的 repo_ids 列表里移除这个仓库
    for workspace in db.query(Workspace).all():
        next_repo_ids = [item for item in (workspace.repo_ids or []) if str(item) != str(repo.id)]
        if next_repo_ids != (workspace.repo_ids or []):
            workspace.repo_ids = next_repo_ids
    db.delete(repo)  # 6) 最后删仓库主记录
    db.commit()

    # 7) 清理向量库（Milvus）和磁盘上的上传文件
    milvus_deleted = delete_milvus_documents(repo_id=repo.id, strict=False)
    removed_files = 0
    file_cleanup_errors: list[str] = []
    for path in stored_paths:
        try:
            if path.exists() and path.is_file():
                path.unlink()  # 删除文件
                removed_files += 1
        except OSError as exc:
            file_cleanup_errors.append(f"{path}: {exc}")  # 删不掉记下错误，不中断

    # 返回清理汇总
    return {
        "repo_id": str(repo_id),
        "deleted": True,
        "cleanup": {
            "milvus_deleted": milvus_deleted,
            "removed_files": removed_files,
            "file_errors": file_cleanup_errors,
        },
    }


@router.get("/{repo_id}/sync-status")  # GET 同步状态
async def sync_status(repo_id: UUID, db: Session = Depends(get_db)) -> dict:
    repo = db.get(Repository, repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="未找到仓库")
    return {
        "repo_id": str(repo.id),
        # 有 last_sync_at 说明同步过，否则没开始
        "status": "completed" if repo.last_sync_at else "not_started",
        "last_sync_at": repo.last_sync_at,
        "message": "最近一次同步已成功完成。" if repo.last_sync_at else "仓库还没有同步过。",
    }
