"""Git worktree lifecycle for live PR code analysis.（PR 分支快照的生命周期管理）

这个文件专门管理「PR 分支快照」：当要看某个 PR 改动的代码长啥样时，项目会用 git
worktree 把那个 PR 的分支单独「检出一个独立文件夹」，然后在上面建代码图谱，供实时
搜索/审查用。它和 code_analysis.py 是搭档——code_analysis 负责默认分支，本文件
负责「PR 临时分支」这种增量场景。."""

import json  # 读写快照元数据 JSON
import shutil  # 删目录（rtree）
import subprocess  # 调 git 命令
import threading  # 线程锁，防止同一 PR 并发快照冲突
import uuid  # 处理 UUID
from datetime import datetime, timedelta, timezone  # 时间计算（清理过期快照用）
from pathlib import Path  # 路径处理
from typing import Any  # 任意类型

from sqlalchemy.orm import Session  # 数据库会话

from app.core.config import settings  # 配置
from app.core.security import decrypt_token  # 解密 token
from app.db.models import CodeRelation, CodeSymbol, PullRequest, Repository  # 相关表
from app.services.code_analysis import _git_text, _sync_checkout, _sync_code_graph  # 复用 code_analysis 的工具


# 每个 PR 快照一把锁，用字典存；_SNAPSHOT_LOCKS_GUARD 保护这个字典本身（字典操作也要线程安全）
_SNAPSHOT_LOCKS: dict[str, threading.Lock] = {}
_SNAPSHOT_LOCKS_GUARD = threading.Lock()


def _checkout_root() -> Path:
    # 算出「所有 checkout 存放的根目录」。配置里可能是相对路径，这里统一转成绝对路径并确保目录存在
    root = Path(settings.repo_checkout_dir)
    if not root.is_absolute():
        root = Path.cwd() / root  # 相对路径就拼到当前工作目录后面
    root.mkdir(parents=True, exist_ok=True)  # parents=True 建多级；exist_ok 已存在也不报错
    return root.resolve()  # resolve 解析成规范绝对路径


def _snapshot_lock(key: str) -> threading.Lock:
    # 取（或建）某把锁。用 guard 锁住「查字典+可能新建」这段，避免多线程同时建同一把锁
    with _SNAPSHOT_LOCKS_GUARD:
        lock = _SNAPSHOT_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _SNAPSHOT_LOCKS[key] = lock
        return lock


def _run_git(args: list[str], cwd: Path) -> str:
    # 在 cwd 目录里跑一条 git 命令，返回 stdout（去掉首尾空白）
    # check=True：命令失败就抛异常；timeout=180：最多等 3 分钟；text=True：按文本处理
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=180,
    )
    return result.stdout.strip()


def pr_worktree_path(repo: Repository, pr: PullRequest) -> Path:
    # 算出这个 PR 的 worktree 文件夹路径。把仓库全名里的 / 换成 __ 避免路径非法
    safe_name = repo.full_name.replace("/", "__").replace("\\", "__")
    return _checkout_root() / "worktrees" / str(repo.id) / f"pr-{pr.number}-{safe_name}"


def _snapshot_meta_path(path: Path) -> Path:
    # 快照元数据文件名固定叫 .devflow-pr-snapshot.json，放在快照目录里
    return path / ".devflow-pr-snapshot.json"


def _write_snapshot_meta(path: Path, meta: dict[str, Any]) -> None:
    # 写快照元数据：把传入的 meta 合并一个 updated_at 时间戳，再写成格式化的 JSON 文件
    payload = {**meta, "updated_at": datetime.now(timezone.utc).isoformat()}
    _snapshot_meta_path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _sync_pr_snapshot_analysis(
    db: Session,
    repo: Repository,
    pr: PullRequest,
    snapshot_path: Path,
    branch: str,
    commit_sha: str | None,
) -> int:
    # 委托 code_analysis 的 _sync_code_graph，在这个 PR 快照目录上建代码图谱（带 pr_id/pr_number 标记）
    return _sync_code_graph(
        db,
        repo,
        snapshot_path,
        branch=branch,
        commit_sha=commit_sha,
        pr_id=pr.id,
        pr_number=pr.number,
    )


def _snapshot_response(
    *,
    status: str,
    message: str | None = None,
    branch: str | None = None,
    commit_sha: str | None = None,
    pr: PullRequest,
    snapshot_path: Path | None = None,
    indexed_symbols: int | None = None,
    fallback: str | None = None,
) -> dict[str, Any]:
    # 统一的「快照结果」返回结构，方便前端展示。status 可能是 ready/unsupported/missing_head/failed/fallback
    return {
        "status": status,
        "message": message,
        "snapshot_name": "PR 分支快照",
        "branch": branch,
        "commit_sha": commit_sha,
        "pr_id": str(pr.id),
        "pr_number": pr.number,
        "snapshot_path": str(snapshot_path) if snapshot_path else None,
        "indexed_symbols": indexed_symbols,
        "fallback": fallback,
        # checkout_kind 告诉前端这次用的是 PR 快照还是默认分支；status==ready 才是真正的 PR 快照
        "checkout_kind": "pr_snapshot" if status == "ready" else "default_checkout",
    }


def prepare_pr_worktree(db: Session, repo: Repository, pr: PullRequest) -> dict[str, Any]:
    """Prepare a PR branch snapshot for live search and refresh its code graph.（准备 PR 分支快照并刷新代码图谱）."""
    lock_key = f"{repo.id}:{pr.id}"  # 这个 PR 的唯一锁键
    lock = _snapshot_lock(lock_key)
    with lock:
        # 为什么加锁：同一 PR 同时被多人触发会重复 checkout，互相踩；加锁保证串行
        if (repo.checkout_mode or "managed") == "local":
            # 本地仓库模式不自动建/删快照，直接返回 unsupported 并给本地路径兜底
            return _snapshot_response(status="unsupported", message="本地仓库模式不自动创建或删除 PR 分支快照。", pr=pr, fallback=repo.local_path)
        if not pr.head_branch:
            # PR 没有来源分支（极端情况），退回默认分支
            return _snapshot_response(status="missing_head", message="PR 缺少 head branch，继续使用默认分支代码。", pr=pr)
        if not shutil.which("git"):
            # 机器上没装 git，无法做快照，返回失败但还能用默认分支
            return _snapshot_response(status="failed", message="未找到 git，可继续使用默认分支代码。", pr=pr)
        token = decrypt_token(repo.github_token_encrypted, settings.token_encryption_key) if repo.github_token_encrypted else None
        target = pr_worktree_path(repo, pr)  # 这个 PR 的快照目录
        try:
            base_checkout = _sync_checkout(repo, token)  # 先确保「默认分支」已 checkout（作为拉取 PR 分支的基础）
            target.parent.mkdir(parents=True, exist_ok=True)  # 建父目录
            ref = pr.head_branch  # PR 的来源分支名
            local_branch = f"devflow/pr-{pr.number}"  # 本地建一个专门分支名，避免冲突
            _run_git(["fetch", "origin", ref], cwd=base_checkout)  # 从远程拉这个 PR 分支
            if target.exists():
                # 快照目录已存在：进去 fetch + 切到最新
                _run_git(["fetch", "origin", ref], cwd=target)
                _run_git(["checkout", "-B", local_branch, "FETCH_HEAD"], cwd=target)
            else:
                # 不存在：用 git worktree add 新建一个独立工作树（不影响默认分支）
                _run_git(["worktree", "add", "--force", "-B", local_branch, str(target), "FETCH_HEAD"], cwd=base_checkout)
            commit_sha = _git_text(["rev-parse", "HEAD"], target)  # 记下当前 commit 号
            indexed_symbols = _sync_pr_snapshot_analysis(db, repo, pr, target, ref, commit_sha)  # 在这个目录上建代码图谱
            _write_snapshot_meta(
                target,
                {
                    "repo_id": str(repo.id),
                    "pr_id": str(pr.id),
                    "pr_number": pr.number,
                    "branch": ref,
                    "commit_sha": commit_sha,
                    "indexed_symbols": indexed_symbols,
                    "snapshot_kind": "pr_snapshot",
                },
            )
            db.commit()  # 图谱结果落库
            return _snapshot_response(
                status="ready",
                branch=ref,
                commit_sha=commit_sha,
                pr=pr,
                snapshot_path=target,
                indexed_symbols=indexed_symbols,
            )
        except Exception as exc:
            db.rollback()  # 出错了回滚，避免脏数据
            return _snapshot_response(
                status="fallback",
                message=f"PR 分支快照准备失败，继续使用默认分支代码：{exc}",
                branch=repo.default_branch or "main",
                pr=pr,
            )


def cleanup_pr_snapshots(db: Session, repo: Repository, *, max_age_hours: int = 168) -> dict[str, Any]:
    # 清理过期的 PR 快照（默认 7 天=168 小时）。定期跑，防止磁盘被一堆临时分支占满
    root = _checkout_root() / "worktrees" / str(repo.id)
    if not root.exists():
        return {"removed": 0, "paths": []}  # 根目录都没有，没东西可清
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)  # 截止时间：现在往前推 max_age_hours
    removed_paths: list[str] = []
    removed_pr_ids: set[uuid.UUID] = set()  # 用 set 去重收集被删快照对应的 PR id
    for path in root.iterdir():
        if not path.is_dir():
            continue  # 只处理文件夹
        meta_path = _snapshot_meta_path(path)
        updated_at = None
        snapshot_pr_id: uuid.UUID | None = None
        if meta_path.exists():
            try:
                metadata = json.loads(meta_path.read_text(encoding="utf-8"))  # 读元数据
                updated_raw = metadata.get("updated_at")
                updated_at = datetime.fromisoformat(updated_raw) if updated_raw else None  # 转成 datetime
                if metadata.get("pr_id"):
                    snapshot_pr_id = uuid.UUID(str(metadata["pr_id"]))
            except (OSError, ValueError, json.JSONDecodeError):
                updated_at = None  # 元数据坏了就用文件修改时间兜底
        if updated_at is None:
            # 没有可靠时间就用工件夹本身的修改时间
            updated_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if updated_at >= cutoff:
            continue  # 还没过期，保留
        try:
            # 为什么先 git worktree remove：这样主 checkout 不会留下陈旧的 worktree 记录，比直接删目录干净
            main_checkout = next((item for item in _checkout_root().glob(f"{repo.id}-*") if item.is_dir()), None)
            if main_checkout:
                _run_git(["worktree", "remove", "--force", str(path)], cwd=main_checkout)
            elif path.exists():
                shutil.rmtree(path)  # 没有主 checkout 就直接删目录
        except Exception:
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)  # 实在删不掉就强制忽略错误再删
        removed_paths.append(str(path))
        if snapshot_pr_id:
            removed_pr_ids.add(snapshot_pr_id)
    if removed_pr_ids:
        # 同时删掉这些 PR 在代码图谱里留下的符号和关系记录，保持数据库干净
        db.query(CodeRelation).filter(
            CodeRelation.repo_id == repo.id,
            CodeRelation.pr_id.in_(removed_pr_ids),
        ).delete(synchronize_session=False)
        db.query(CodeSymbol).filter(
            CodeSymbol.repo_id == repo.id,
            CodeSymbol.pr_id.in_(removed_pr_ids),
        ).delete(synchronize_session=False)
    db.commit()
    return {"removed": len(removed_paths), "paths": removed_paths}
