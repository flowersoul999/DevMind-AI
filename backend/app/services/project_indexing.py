"""项目索引服务（project_indexing.py）。

本模块负责"扫描仓库里的项目文档（README、架构文档、清单文件如 package.json 等），
把它们切块写进向量库，并维护一个索引状态"。类比：给项目写一份"目录与简介"，让 AI 能快速了解
这个仓库是干嘛的、用了什么技术栈、有哪些重要文档。它和知识图谱联动（建完索引顺手重建图谱）。

关键函数：
- load_project_index_policy：读 .devflow/index.yml 里的索引策略（auto/allowlist、包含/排除规则）。
- scan_project_index：真正执行扫描——发现文档→切块→写库→更新状态→重建证据与知识图谱。
- get_project_index_state / mark_project_index_building / snooze_project_index：状态的查询与标记。
- project_fingerprint：用"文件清单+修改时间"算一个指纹，用来判断"文档变没变、要不要重建"。

与其他模块的关系：依赖 code_analysis（拿仓库本地路径）、rag.chunking/vector_store（切块+入库）、
knowledge_graph（重建图谱）、chat_memory.EvidenceStore（刷新证据）。."""


# 标准库：fnmatch（路径通配）、hashlib（指纹哈希）、json、os、uuid、dataclasses、datetime、pathlib、typing
import fnmatch
import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml  # 解析 YAML 配置文件
from sqlalchemy.orm import Session

from app.db.models import ProjectIndexState, Repository  # 索引状态表、仓库表
from app.db.session import SessionLocal  # 独立数据库会话（后台任务用）
from app.services.knowledge_graph import rebuild_knowledge_graph  # 重建知识图谱
from app.services.rag.chunking import chunk_document  # 把文档切成块
from app.services.code_analysis import _repo_checkout_path, _stored_checkout_path  # 取仓库本地路径
from app.services.rag.vector_store import DocumentPayload, replace_documents, stable_source_id  # 入库相关

# 项目索引支持的文档来源类型
PROJECT_INDEX_SOURCE_TYPES = ["project_overview", "project_doc", "project_manifest"]
# 索引状态枚举：缺失/过时/构建中/就绪/失败
PROJECT_INDEX_STATUSES = {"missing", "stale", "building", "ready", "failed"}
# 索引配置文件路径（仓库根目录下的 .devflow/index.yml）
PROJECT_INDEX_CONFIG_PATH = Path(".devflow/index.yml")
PROJECT_INDEX_MODES = {"auto", "allowlist"}  # 索引模式：自动发现 / 白名单

# 顶级"权威"文档（最该索引）
AUTHORITATIVE_TOP_LEVEL = {"README", "ARCHITECTURE", "CONTRIBUTING"}
# 清单文件（描述依赖/构建，如 package.json、pyproject.toml）
MANIFEST_FILES = {
    "package.json",
    "requirements.txt",
    "pyproject.toml",
    "poetry.lock",
    "pnpm-workspace.yaml",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "Gemfile",
    "composer.json",
}
# 二级"软线索"文档（有价值但非核心）
SOFT_CLUE_TOP_LEVEL = {"CHANGELOG", "HISTORY", "RELEASES"}
# 遍历时要跳过的目录（依赖/缓存/构建产物等，不索引）
SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".next",
    ".nuxt",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "coverage",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}
# 可索引的隐藏目录（只有 .github 的 ISSUE 模板等有价值）
INDEXABLE_HIDDEN_DIRS = {".github"}
# 敏感文件名（绝不索引，避免泄露密钥）
SECRET_NAMES = {".env", ".env.local", ".env.production", ".npmrc", ".pypirc", "credentials.json"}
SECRET_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}  # 敏感后缀


@dataclass(frozen=True)
class ProjectDocCandidate:
    # 一个"待索引文档候选"：路径、相对路径、来源类型、层级（authoritative/soft_clue/derived）
    path: Path
    relative_path: str
    source_type: str
    tier: str


@dataclass(frozen=True)
class ProjectIndexPolicy:
    # 索引策略（从 .devflow/index.yml 解析而来）
    mode: str = "auto"
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    include_manifests: bool = True
    config_path: str | None = None

    def as_dict(self) -> dict[str, Any]:
        # 转成 API 友好的字典（驼峰键名）
        return {
            "mode": self.mode,
            "include": list(self.include),
            "exclude": list(self.exclude),
            "includeManifests": self.include_manifests,
            "configPath": self.config_path,
        }


@dataclass(frozen=True)
class ProjectDocDiscovery:
    # 文档发现结果汇总
    candidates: list[ProjectDocCandidate]
    excluded_paths: list[str]
    policy: ProjectIndexPolicy
    truncated: bool = False  # 是否因超过 max_files 被截断


def _now() -> datetime:
    # 当前 UTC 时间（统一用，避免各处写重复）
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    # 给"没有时区"的时间补上 UTC 时区（存库/比较前统一，避免 naive datetime 报错）
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _repo_root(repo: Repository) -> Path:
    # 取仓库本地根目录：优先用配置的 local_path，否则用程序管理的 checkout 路径
    stored = _stored_checkout_path(repo)
    if stored:
        return stored
    return _repo_checkout_path(repo)


def _safe_project_root(repo: Repository) -> Path | None:
    # 安全地取项目根：不存在/不是目录/解析失败都返回 None（不让异常冒泡）
    root = _repo_root(repo)
    if not root.exists() or not root.is_dir():
        return None
    try:
        return root.resolve()  # 解析成绝对规范路径
    except OSError:
        return None


def _is_secret(path: Path) -> bool:
    # 判断文件是不是敏感文件（按名/后缀/前缀），敏感文件绝不索引
    name = path.name
    return name in SECRET_NAMES or path.suffix.lower() in SECRET_SUFFIXES or name.lower().startswith("credentials")


def _read_text(path: Path, max_bytes: int = 400_000) -> str | None:
    # 读文本文件，限定最大字节，且要"看起来像文本"（含二进制就不读）。多编码尝试解码。
    try:
        with path.open("rb") as handle:
            data = handle.read(max_bytes + 1)  # 多读 1 字节用来判断"是否超长"
    except OSError:
        return None
    if not data or len(data) > max_bytes or b"\x00" in data[:4096]:
        return None  # 空 / 超长 / 前 4K 含空字节（基本是二进制）→ 不读
    for encoding in ["utf-8-sig", "utf-8", "gb18030", "latin-1"]:
        # 依次尝试：先 UTF-8（带 BOM），再 UTF-8，再中文常用 GBK 超集，最后 latin-1 兜底
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def _stem_upper(path: Path) -> str:
    # 文件名主干转大写（用于不区分大小写地匹配 README/ARCHITECTURE 等）
    return path.stem.upper()


def _classify_file(path: Path, rel: str) -> tuple[str, str] | None:
    # 判断一个文件"该不该索引、算哪种类型、什么层级"。返回 None 表示跳过。
    if _is_secret(path):
        return None  # 敏感文件直接排除
    name = path.name
    stem = _stem_upper(path)
    rel_posix = rel.replace("\\", "/")
    if path.suffix.lower() == ".md":
        # Markdown 文档的分级：docs/ 下或权威名或 ADR → authoritative；变更日志类 → soft_clue
        if rel_posix.startswith("docs/") or stem in AUTHORITATIVE_TOP_LEVEL or stem.startswith("ADR"):
            return "project_doc", "authoritative"
        if stem in SOFT_CLUE_TOP_LEVEL or rel_posix.startswith(".github/ISSUE_TEMPLATE/"):
            return "project_doc", "soft_clue"
    if name in MANIFEST_FILES:
        return "project_manifest", "derived"  # 清单文件
    return None  # 其它文件不索引


def _is_inside(root: Path, candidate: Path) -> bool:
    # 判断 candidate 是否位于 root 之内（防目录穿越）
    try:
        candidate.resolve().relative_to(root)
        return True
    except (OSError, ValueError):
        return False


def _normalize_patterns(value: Any, field_name: str) -> tuple[str, ...]:
    # 把配置里的 include/exclude 列表规范成"仓库相对 glob"（去 ./ 前缀、.. 越界报错、目录尾加 **）。
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list of repository-relative glob patterns")
    patterns: list[str] = []
    for raw_pattern in value:
        if not isinstance(raw_pattern, str) or not raw_pattern.strip():
            raise ValueError(f"{field_name} entries must be non-empty strings")
        pattern = raw_pattern.strip().replace("\\", "/")
        while pattern.startswith("./"):
            pattern = pattern[2:]
        pattern = pattern.lstrip("/")  # 去掉多余前导斜杠
        if pattern == ".." or pattern.startswith("../"):
            raise ValueError(f"{field_name} patterns must stay inside the repository")  # 禁止跳出仓库
        if pattern.endswith("/"):
            pattern += "**"  # 目录模式表示"其下所有文件"
        patterns.append(pattern)
    return tuple(dict.fromkeys(patterns))  # 保序去重


def load_project_index_policy(root: Path) -> ProjectIndexPolicy:
    # 读取并校验 .devflow/index.yml，返回索引策略。没配置文件就用默认（auto 模式）。
    root = root.resolve()
    config_path = root / PROJECT_INDEX_CONFIG_PATH
    if not config_path.is_file():
        return ProjectIndexPolicy()  # 没配置：默认策略
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"Could not read {PROJECT_INDEX_CONFIG_PATH.as_posix()}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{PROJECT_INDEX_CONFIG_PATH.as_posix()} must contain a YAML object")
    version = raw.get("version", 1)
    if version != 1:
        raise ValueError(f"Unsupported project index config version: {version}")
    project_docs = raw.get("project_docs") or {}
    if not isinstance(project_docs, dict):
        raise ValueError("project_docs must be a YAML object")
    mode = str(project_docs.get("mode") or "auto").strip().lower()
    if mode not in PROJECT_INDEX_MODES:
        raise ValueError(f"project_docs.mode must be one of: {', '.join(sorted(PROJECT_INDEX_MODES))}")
    include = _normalize_patterns(project_docs.get("include"), "project_docs.include")
    exclude = _normalize_patterns(project_docs.get("exclude"), "project_docs.exclude")
    if mode == "allowlist" and not include:
        # 白名单模式必须给 include，否则什么都索引不了
        raise ValueError("project_docs.include cannot be empty when mode is allowlist")
    include_manifests = raw.get("include_manifests", True)
    if not isinstance(include_manifests, bool):
        raise ValueError("include_manifests must be true or false")
    return ProjectIndexPolicy(
        mode=mode,
        include=include,
        exclude=exclude,
        include_manifests=include_manifests,
        config_path=PROJECT_INDEX_CONFIG_PATH.as_posix(),
    )


def _matches_any(relative_path: str, patterns: tuple[str, ...]) -> bool:
    # 用 fnmatch 判断相对路径是否匹配任一 glob 模式（大小写敏感）
    return any(fnmatch.fnmatchcase(relative_path, pattern) for pattern in patterns)


def _policy_allows(candidate: ProjectDocCandidate, policy: ProjectIndexPolicy) -> bool:
    # 根据策略判断一个候选"是否允许索引"：先排除名单；清单文件看 include_manifests；
    # 白名单模式必须命中 include；auto 模式默认允许。
    if _matches_any(candidate.relative_path, policy.exclude):
        return False
    if candidate.source_type == "project_manifest":
        return policy.include_manifests
    if policy.mode == "allowlist":
        return _matches_any(candidate.relative_path, policy.include)
    return True


def _discover_project_docs(root: Path, max_files: int = 500) -> ProjectDocDiscovery:
    # 遍历仓库目录，发现所有"该索引"的项目文档候选。用 os.walk 并动态裁剪跳过的目录。
    root = root.resolve()
    policy = load_project_index_policy(root)
    results: list[ProjectDocCandidate] = []
    excluded_paths: list[str] = []
    truncated = False
    for current_root, dir_names, file_names in os.walk(root):
        # 就地修改 dir_names 来剪枝：跳过 SKIP_DIRS，且默认跳过点开头的目录（除了 .github）
        dir_names[:] = sorted(
            name
            for name in dir_names
            if name not in SKIP_DIRS and (not name.startswith(".") or name in INDEXABLE_HIDDEN_DIRS)
        )
        for filename in sorted(file_names):
            path = Path(current_root) / filename
            if not _is_inside(root, path):
                continue
            rel = path.relative_to(root).as_posix()
            classification = _classify_file(path, rel)
            if not classification:
                continue  # 不该索引
            source_type, tier = classification
            candidate = ProjectDocCandidate(path=path, relative_path=rel, source_type=source_type, tier=tier)
            if not _policy_allows(candidate, policy):
                excluded_paths.append(rel)  # 被策略排除
                continue
            if len(results) >= max_files:
                truncated = True  # 超过上限，截断
                break
            results.append(candidate)
        if truncated:
            break
    return ProjectDocDiscovery(results, excluded_paths, policy, truncated)


def discover_project_docs(root: Path, max_files: int = 500) -> list[ProjectDocCandidate]:
    # 对外暴露的"只返回候选列表"版本（方便单独调用）
    return _discover_project_docs(root, max_files=max_files).candidates


def project_fingerprint(repo: Repository) -> str:
    # 计算项目指纹：把"策略 + 每个文档路径 + 修改时间 + 大小"哈希成一个字符串。
    # 为什么：指纹变了说明文档有改动，需要重建索引；没变就跳过（省时）。
    root = _safe_project_root(repo)
    if not root:
        return ""
    hasher = hashlib.sha256()
    discovery = _discover_project_docs(root, max_files=1000)
    # 先把策略加进哈希（改策略也应触发重建）
    hasher.update(json.dumps(discovery.policy.as_dict(), ensure_ascii=False, sort_keys=True).encode("utf-8"))
    for candidate in discovery.candidates:
        try:
            stat = candidate.path.stat()
        except OSError:
            continue
        hasher.update(candidate.relative_path.encode("utf-8"))
        hasher.update(str(stat.st_mtime_ns).encode("ascii"))  # 修改时间（纳秒）
        hasher.update(str(stat.st_size).encode("ascii"))       # 文件大小
    digest = hasher.hexdigest()
    return f"files:{digest}"


def _get_state_row(db: Session, repo_id: uuid.UUID) -> ProjectIndexState | None:
    # 取索引状态行（没有返回 None）
    return db.query(ProjectIndexState).filter(ProjectIndexState.repo_id == repo_id).one_or_none()


def _get_or_create_state(db: Session, repo_id: uuid.UUID) -> ProjectIndexState:
    # 取索引状态行，没有就新建一个 status="missing"
    row = _get_state_row(db, repo_id)
    if row:
        return row
    row = ProjectIndexState(repo_id=repo_id, status="missing")
    db.add(row)
    db.flush()
    return row


def _state_payload(repo: Repository, row: ProjectIndexState | None, fingerprint: str | None = None) -> dict[str, Any]:
    # 把索引状态行（或缺失）整理成 API 友好的字典；并判断"ready 但指纹变了"则标 stale（过时）。
    current_fingerprint = fingerprint if fingerprint is not None else project_fingerprint(repo)
    if row is None:
        return {
            "repo_id": repo.id,
            "status": "missing",
            "fingerprint": current_fingerprint,
            "docs_indexed": 0,
            "docs_total": 0,
            "error_message": None,
            "summary_json": None,
            "snoozed_until": None,
            "last_scan_at": None,
        }
    status = row.status if row.status in PROJECT_INDEX_STATUSES else "missing"
    if status == "ready" and current_fingerprint and row.fingerprint != current_fingerprint:
        status = "stale"  # 曾经就绪但内容变了 → 过时，需要重建
    return {
        "repo_id": row.repo_id,
        "status": status,
        "fingerprint": row.fingerprint or current_fingerprint,
        "docs_indexed": row.docs_indexed or 0,
        "docs_total": row.docs_total or 0,
        "error_message": row.error_message,
        "summary_json": row.summary_json,
        "snoozed_until": row.snoozed_until,
        "last_scan_at": row.last_scan_at,
    }


def get_project_index_state(db: Session, repo: Repository) -> dict[str, Any]:
    # 对外：获取一个仓库的索引状态
    return _state_payload(repo, _get_state_row(db, repo.id))


def mark_project_index_building(db: Session, repo: Repository) -> ProjectIndexState:
    # 标记索引"构建中"：记录指纹、清错误、清 snooze、提交。给"开始扫描"前调用。
    row = _get_or_create_state(db, repo.id)
    row.status = "building"
    row.fingerprint = project_fingerprint(repo)
    row.error_message = None
    row.snoozed_until = None
    row.updated_at = _now()
    db.commit()
    return row


def snooze_project_index(db: Session, repo: Repository, days: int = 7) -> ProjectIndexState:
    # "暂缓"索引：把 snoozed_until 设到 N 天后（期间 UI 不再提示重建）。
    row = _get_or_create_state(db, repo.id)
    row.snoozed_until = _now() + timedelta(days=days)  # 当前时间 + 天数
    row.updated_at = _now()
    if row.status not in {"ready", "building"}:
        row.status = "missing"  # 非就绪状态暂缓后标回 missing
    db.commit()
    db.refresh(row)  # 从库里刷新（拿到最新值）
    return row


def _extract_title(text: str, fallback: str) -> str:
    # 从文档正文取标题：第一行的 "# 标题" 作为标题（Markdown 约定），否则用 fallback。
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()[:500] or fallback
    return fallback


def _summary_from_text(text: str, limit: int = 360) -> str:
    # 从正文抽取一段摘要（取第一个非空段落），超过 limit 截断加"..."
    cleaned = text.strip()
    if not cleaned:
        return ""
    paragraphs = []
    for block in cleaned.split("\n\n"):
        value = " ".join(line.strip() for line in block.splitlines() if line.strip())
        # 跳过标题行、表格、代码块（这些是"结构性"内容，不适合当摘要）
        if value and not value.startswith("#") and not value.startswith("|") and not value.startswith("```"):
            paragraphs.append(value)
    summary = paragraphs[0] if paragraphs else " ".join(cleaned.splitlines()[:4])
    return summary[: limit - 3].rstrip() + "..." if len(summary) > limit else summary


def _detect_tech_stack(root: Path) -> list[str]:
    # 通过"是否存在特征文件"探测技术栈（package.json→node、pyproject.toml→python 等）
    detectors = {
        "package.json": "node",
        "tsconfig.json": "typescript",
        "pyproject.toml": "python",
        "requirements.txt": "python",
        "go.mod": "go",
        "Cargo.toml": "rust",
        "pom.xml": "java",
        "build.gradle": "java",
    }
    return [tech for filename, tech in detectors.items() if (root / filename).exists()]


def _safe_dirs(root: Path, limit: int = 12) -> list[str]:
    # 取仓库根目录下的子目录名（最多 limit 个，跳过点和 SKIP_DIRS），用于概况展示目录结构。
    dirs: list[str] = []
    try:
        for child in sorted(root.iterdir(), key=lambda item: item.name.lower()):
            if len(dirs) >= limit:
                break
            if child.name.startswith(".") or child.name in SKIP_DIRS:
                continue
            if child.is_dir():
                dirs.append(child.name)
    except OSError:
        return []
    return dirs


def _build_summary(
    repo: Repository,
    root: Path,
    candidates: list[ProjectDocCandidate],
    docs_indexed: int,
    discovery: ProjectDocDiscovery,
) -> dict[str, Any]:
    # 构建"项目概况"摘要 JSON：项目名、技术栈、目录结构、文档清单、各层级覆盖数等。
    tier_coverage: dict[str, int] = {}
    source_type_coverage: dict[str, int] = {}
    docs_list: list[dict[str, str]] = []
    for item in candidates:
        tier_coverage[item.tier] = tier_coverage.get(item.tier, 0) + 1
        source_type_coverage[item.source_type] = source_type_coverage.get(item.source_type, 0) + 1
        docs_list.append({"path": item.relative_path, "tier": item.tier, "source_type": item.source_type})
    return {
        "projectName": repo.full_name,
        "rootPath": str(root),
        "techStack": sorted(set(_detect_tech_stack(root))),  # 去重排序
        "dirStructure": _safe_dirs(root),
        "docsList": docs_list[:80],  # 文档清单最多 80
        "tierCoverage": tier_coverage,
        "sourceTypeCoverage": source_type_coverage,
        "docsIndexed": docs_indexed,
        "indexPolicy": discovery.policy.as_dict(),
        "excludedFiles": len(discovery.excluded_paths),
        "excludedPaths": discovery.excluded_paths[:80],
        "discoveryTruncated": discovery.truncated,
    }


def _payloads_for_candidates(repo: Repository, root: Path, candidates: list[ProjectDocCandidate], fingerprint: str) -> list[DocumentPayload]:
    # 把每个候选文档切块，生成一堆要入库的 DocumentPayload（每块一个）。
    payloads: list[DocumentPayload] = []
    scanned_at = _now().isoformat()
    for candidate in candidates:
        text = _read_text(candidate.path)
        if not text or not text.strip():
            continue
        # 稳定 source_id：同仓库+类型+路径永远一致（便于 replace 时定位旧块）
        source_id = stable_source_id(repo.id, candidate.source_type, candidate.relative_path)
        title = _extract_title(text, candidate.relative_path)
        # 结构感知切块：按标题/段落切，max_chars=1800，overlap=180（相邻块重叠，避免切断语义）
        chunks = chunk_document(text, candidate.relative_path, max_chars=1800, overlap=180)
        for index, chunk in enumerate(chunks):
            content = str(chunk["content"])
            section_title = chunk.get("section_title")
            payloads.append(
                DocumentPayload(
                    source_type=candidate.source_type,
                    source_id=source_id,
                    # 标题：有章节名就拼上，多块时标块序号
                    title=f"{title} · {section_title}" if section_title else (f"{title}#{index + 1}" if len(chunks) > 1 else title),
                    content=content,
                    metadata={
                        # 把 chunk 里除 content 外的字段都带上（行号等），并补 DevFlow 自己的溯源字段
                        **{key: value for key, value in chunk.items() if key != "content" and value is not None},
                        "display_title": title,
                        "path": candidate.relative_path,
                        "root_path": str(root),
                        "provenance_tier": candidate.tier,  # 来源层级（authoritative/soft_clue/derived）
                        "chunk_index": index,
                        "chunk_chars": len(content),
                        "total_chunks": len(chunks),
                        "chunk_strategy": "structure_aware_v1",
                        "fingerprint": fingerprint,
                        "scanned_at": scanned_at,
                    },
                )
            )
    return payloads


def scan_project_index(db: Session, repo: Repository) -> dict[str, Any]:
    # 核心：执行一次完整项目索引。流程：标记 building → 发现文档 → 切块入库 → 写概况 → 刷新证据与图谱 → 标 ready。
    row = _get_or_create_state(db, repo.id)
    fp = project_fingerprint(repo)
    row.status = "building"
    row.fingerprint = fp
    row.error_message = None
    row.snoozed_until = None
    row.updated_at = _now()
    db.commit()  # 先把"构建中"落库（失败也能从 status 看出来）

    try:
        root = _safe_project_root(repo)
        if not root:
            raise RuntimeError("项目本地路径不存在，请先连接本地仓库或完成代码同步")

        discovery = _discover_project_docs(root)
        candidates = discovery.candidates
        payloads = _payloads_for_candidates(repo, root, candidates, fp)
        overview = {
            # 项目概况文档的内容
            "repo": repo.full_name,
            "description": repo.description or "",
            "default_branch": repo.default_branch or "",
            "local_path": str(root),
            "indexed_files": [candidate.relative_path for candidate in candidates[:80]],
        }
        payloads.insert(
            0,
            # 在 payload 列表最前插一条"项目概况"文档（source_type=project_overview）
            DocumentPayload(
                source_type="project_overview",
                source_id=stable_source_id(repo.id, "project_overview", "overview"),
                title=f"项目概况：{repo.full_name}",
                content=json.dumps(overview, ensure_ascii=False, indent=2),
                metadata={"display_title": "项目概况", "fingerprint": fp, "root_path": str(root), "chunk_index": 0},
            ),
        )

        # 用这批 payload 替换掉旧的同类型文档（replace 模式 = 删旧写新）；这里不生成向量、不同步向量库
        docs = replace_documents(
            db,
            repo.id,
            PROJECT_INDEX_SOURCE_TYPES,
            payloads,
            commit=False,
            generate_embeddings=False,
            sync_vector_store=False,
        )
        summary = _build_summary(repo, root, candidates, len(docs), discovery)
        row = _get_or_create_state(db, repo.id)
        row.status = "ready"  # 就绪
        row.fingerprint = fp
        row.docs_indexed = len(docs)
        row.docs_total = len(candidates)
        row.summary_json = summary
        row.last_scan_at = _now()
        row.error_message = None
        row.updated_at = _now()
        # EvidenceItem 是 ChatAgent 记忆工具用的"反范式搜索面"。在同一个事务里刷新它，
        # 这样被排除的项目文档不会在成功重新扫描后"幸存"下来（保持证据和文档一致）。
        from app.services.chat_memory import EvidenceStore

        EvidenceStore(db).rebuild_repo(repo)
        db.commit()  # 提交文档+证据
        db.refresh(row)
        rebuild_knowledge_graph(db, repo)  # 索引变了我们顺手重建知识图谱
        return _state_payload(repo, row, fingerprint=fp)
    except Exception as exc:
        # 任何异常：回滚、标 failed、记录错误信息（不要让索引任务整体崩）
        db.rollback()
        row = _get_or_create_state(db, repo.id)
        row.status = "failed"
        row.error_message = str(exc)
        row.updated_at = _now()
        db.commit()
        db.refresh(row)
        return _state_payload(repo, row, fingerprint=fp)


async def scan_project_index_by_id(repo_id: uuid.UUID) -> None:
    # 后台任务入口：按 repo_id 新开一个数据库会话，执行索引扫描。async 但内部是同步 DB 操作。
    db = SessionLocal()
    try:
        repo = db.get(Repository, repo_id)  # 按 ID 取仓库
        if repo is None:
            return
        scan_project_index(db, repo)
    finally:
        db.close()  # 不管成功失败都关掉会话，释放连接


def is_snoozed(state: dict[str, Any]) -> bool:
    # 判断索引是否处于"暂缓"期（snoozed_until 在未来）
    until = _aware(state.get("snoozed_until"))
    return bool(until and until > _now())
