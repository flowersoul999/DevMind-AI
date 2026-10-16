"""Repository checkout synchronization and code-graph analysis.（仓库克隆同步 + 代码图谱分析）

这个文件是「代码理解」的基础：它负责把 GitHub 仓库下载到本地（clone/fetch），然后
扫描里面的源码文件，抽取「符号」（函数、类、接口、类型等）和「关系」（谁 import 谁、
谁调用谁），写进数据库的 CodeSymbol / CodeRelation 表。这就是「代码图谱」——前端
代码搜索、PR 快照都要靠它。

为什么需要它：LLM 不可能一次读下整个大仓库，先建好符号/调用图谱，检索时就能精准
定位「某个函数定义在哪、被谁调用」。."""

import asyncio  # 把同步的 git 操作丢到线程里跑，避免阻塞异步事件循环
import logging  # 日志
import os  # 展开环境变量/用户目录路径
import shutil  # 判断 git 是否存在、删目录
import subprocess  # 调用 git 命令
import uuid  # 处理 UUID
import re  # 正则表达式，用来从源码里抠符号/调用/import
from pathlib import Path  # 路径
from urllib.parse import quote, urlparse, urlunparse  # 给 clone URL 注入 token

from sqlalchemy.orm import Session  # 数据库会话

from app.core.config import settings  # 配置
from app.core.security import decrypt_token  # 解密 token
from app.db.models import CodeRelation, CodeSymbol, Document, Repository  # 相关表
from app.db.session import SessionLocal  # 创建会话


logger = logging.getLogger(__name__)

# 扫描时要跳过的目录名（依赖、构建产物、缓存、版本控制目录）——这些里面没业务源码
SKIP_DIRS = {
    ".git", ".hg", ".svn", ".next", ".nuxt", ".venv", "venv", "node_modules",
    "dist", "build", "coverage", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
}

# 视为「文本源码」的文件后缀集合；只分析这些，二进制（图片/音视频）跳过
TEXT_EXTENSIONS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".java", ".go", ".rs", ".cs",
    ".cpp", ".c", ".h", ".hpp", ".php", ".rb", ".swift", ".kt", ".kts", ".scala", ".sql",
    ".sh", ".ps1", ".bat", ".cmd", ".html", ".css", ".scss", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".md", ".mdx", ".txt", ".dockerfile",
}

# 没有典型后缀、但明确是文本配置/说明的文件名
TEXT_FILENAMES = {
    "Dockerfile", "Makefile", "README", "LICENSE", "package.json", "requirements.txt", "pyproject.toml",
}

# 用正则匹配 Python 的符号：行首的 def 或 class 后面跟名字
PY_SYMBOL_RE = re.compile(r"^\s*(def|class)\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)
# 匹配 TS/JS 的符号：function/class/interface/type/const/let（可能带 export/async 前缀）
TS_SYMBOL_RE = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?(?:function|class|interface|type|const|let)\s+([A-Za-z_$][A-Za-z0-9_$]*)",
    re.MULTILINE,
)
# 匹配函数调用：名字后面跟左括号
CALL_RE = re.compile(r"\b([A-Za-z_][$A-Za-z0-9_]*)\s*\(")
# 匹配 import 语句（Python 的 from/import 和 JS 的 import ... from）
IMPORT_RE = re.compile(r"^\s*(?:from\s+([\w.\/-]+)\s+import|import\s+([\w.\/-]+)|import\s+.*?\s+from\s+['\"]([^'\"]+)['\"])", re.MULTILINE)


def _checkout_root() -> Path:
    # 所有 checkout 的根目录（来自配置），相对路径则拼到当前目录，并确保存在
    root = Path(settings.repo_checkout_dir)
    if not root.is_absolute():
        root = Path.cwd() / root
    root.mkdir(parents=True, exist_ok=True)
    return root


def _repo_checkout_path(repo: Repository) -> Path:
    # 这个仓库默认分支的 checkout 目录：根目录 / {仓库id}-{全名(斜杠换__)}
    safe_name = repo.full_name.replace("/", "__").replace("\\", "__")
    return _checkout_root() / f"{repo.id}-{safe_name}"


def _stored_checkout_path(repo: Repository) -> Path | None:
    # 如果仓库配置了「本地已有路径」（local_path），展开环境变量/用户目录后返回绝对路径，否则 None
    raw_path = getattr(repo, "local_path", None)
    if not raw_path:
        return None
    path = Path(os.path.expandvars(os.path.expanduser(str(raw_path))))
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def _clone_url(repo: Repository) -> str:
    # 取 clone 地址；没配就用 GitHub 标准 https 地址拼出来
    if repo.clone_url:
        return repo.clone_url
    return f"https://github.com/{repo.owner}/{repo.name}.git"


def _clone_url_with_token(url: str, token: str | None) -> str:
    # 把 token 塞进 URL，变成 https://x-access-token:{token}@github.com/...，这样 git 能免密拉私有库
    if not token:
        return url  # 没 token 原样返回
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or "@" in parsed.netloc:
        return url  # 非 http(s) 或已带凭证就不动
    safe_token = quote(token, safe="")  # 把 token 里的特殊字符转义，避免破坏 URL
    # _replace 替换 netloc（网络地址部分），在用户名位置注入 token
    return urlunparse(parsed._replace(netloc=f"x-access-token:{safe_token}@{parsed.netloc}"))


def _run_git(args: list[str], cwd: Path | None = None) -> None:
    # 跑一条 git 命令（不关心输出，只关心成不成功）。check=True 失败就抛
    subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        check=True,
        stdout=subprocess.DEVNULL,  # 丢弃正常输出
        stderr=subprocess.PIPE,  # 错误留着方便排错
        text=True,
        timeout=180,
    )


def _is_git_work_tree(path: Path) -> bool:
    # 判断 path 是不是一个 git 工作树（目录存在 + 有 git + rev-parse 说 true）
    if not path.exists() or not path.is_dir() or not shutil.which("git"):
        return False
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=str(path),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=30,
    )
    return result.returncode == 0 and result.stdout.strip().lower() == "true"


def _sync_checkout(repo: Repository, token: str | None) -> Path:
    # 核心：确保仓库代码已在本地、且是最新。返回 checkout 目录路径
    stored_path = _stored_checkout_path(repo)  # 用户配置的本地路径（可能为空）
    checkout_mode = getattr(repo, "checkout_mode", None) or "managed"  # 默认 managed（由我们管理克隆）

    if stored_path and checkout_mode == "local":
        # 本地模式：直接用用户给的路径，但要先验证它真是 git 工作树
        if not _is_git_work_tree(stored_path):
            raise RuntimeError(f"已配置的本地仓库路径不是 git 工作树：{stored_path}")
        return stored_path

    target = stored_path or _repo_checkout_path(repo)  # 目标目录：用户给的或自动生成
    clone_url = _clone_url_with_token(_clone_url(repo), token)  # 带 token 的克隆地址
    branch = repo.default_branch or "main"  # 默认分支

    if not shutil.which("git"):
        raise RuntimeError("未找到 git 可执行文件，无法克隆仓库代码")

    if (target / ".git").exists() or _is_git_work_tree(target):
        # 已经克隆过：尝试 fetch + checkout + pull 更新（--ff-only 只快进，避免冲突）
        try:
            _run_git(["fetch", "--depth", "1", "origin", branch], cwd=target)
            _run_git(["checkout", branch], cwd=target)
            _run_git(["pull", "--ff-only", "origin", branch], cwd=target)
        except subprocess.CalledProcessError as exc:
            # 更新失败不致命，用已有缓存文件继续（记个警告）
            logger.warning("更新现有 checkout %s 失败，将使用缓存文件：%s", target, (exc.stderr or exc).strip())
        return target

    # 全新克隆
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if stored_path and any(target.iterdir()):
            # 用户指定了下载目标却非空且不是 git 工作树，不能随便覆盖，报错
            raise RuntimeError(f"Configured download target already exists and is not an empty git work tree: {target}")
        shutil.rmtree(target)  # 否则清空重来
    # --depth 1 是「浅克隆」，只拉最新一次提交，省时间省空间
    clone_args = ["clone", "--depth", "1"]
    if branch:
        clone_args.extend(["--branch", branch])
    clone_args.extend([clone_url, str(target)])
    _run_git(clone_args)
    return target


def _is_text_candidate(path: Path) -> bool:
    # 判断一个文件是否值得当文本分析：文件名在 TEXT_FILENAMES，或后缀在 TEXT_EXTENSIONS，或叫 xxx.dockerfile
    if path.name in TEXT_FILENAMES:
        return True
    suffix = path.suffix.lower()
    return suffix in TEXT_EXTENSIONS or path.name.lower().endswith("dockerfile")


def _read_text(path: Path, max_bytes: int = 300_000) -> str | None:
    # 安全读文本：只读前 max_bytes 字节；空文件、超大文件（疑似二进制）、含 NUL 的都返回 None
    try:
        with path.open("rb") as handle:
            data = handle.read(max_bytes + 1)  # 多读 1 字节用来判断是否超长
    except OSError:
        return None
    if not data or len(data) > max_bytes or b"\x00" in data[:4096]:
        return None  # 二进制文件通常含 NUL 字节，前 4KB 出现就判定非文本
    # 依次尝试几种编码解码，utf-8 不行试 gb18030（中文 Windows 常见），都不行返回 None
    for encoding in ["utf-8-sig", "utf-8", "gb18030", "latin-1"]:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def _language_for(path: Path) -> str:
    # 从后缀反推语言名（去点、转小写）；没后缀就用文件名
    suffix = path.suffix.lower().lstrip(".")
    return suffix or path.name


def _git_text(args: list[str], cwd: Path) -> str | None:
    # 跑一条 git 命令并返回文本输出（去掉空白）；失败返回 None（不像 _run_git 会抛）
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=30,
        )
        return result.stdout.strip() or None
    except (OSError, subprocess.CalledProcessError):
        return None


def _line_for_offset(text: str, offset: int) -> int:
    # 把「字符偏移量」换算成「第几行」：数 offset 之前出现了几个换行符 +1
    return text.count("\n", 0, offset) + 1


def _symbol_matches(path: Path, text: str) -> list[dict[str, object]]:
    # 从一段源码文本里找出所有「符号定义」，返回 [{name, kind, language, start_line}, ...]
    language = _language_for(path)
    matches = []
    if path.suffix.lower() == ".py":
        # Python：用 PY_SYMBOL_RE 找 def/class
        for match in PY_SYMBOL_RE.finditer(text):
            # match.group(1) 是 def/class（种类），group(2) 是名字
            matches.append({"name": match.group(2), "kind": match.group(1), "language": language, "start_line": _line_for_offset(text, match.start())})
    elif path.suffix.lower() in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}:
        # TS/JS：用 TS_SYMBOL_RE，再根据声明里的关键字判断 kind（class/interface/type/function）
        for match in TS_SYMBOL_RE.finditer(text):
            declaration = match.group(0)  # 整个匹配到的声明文本，用来判断是 class 还是 interface 等
            kind = "class" if "class " in declaration else "interface" if "interface " in declaration else "type" if "type " in declaration else "function"
            matches.append({"name": match.group(1), "kind": kind, "language": language, "start_line": _line_for_offset(text, match.start())})
    return matches


def _sync_code_graph(
    db: Session,
    repo: Repository,
    checkout_path: Path,
    *,
    branch: str | None,
    commit_sha: str | None,
    pr_id: uuid.UUID | None = None,
    pr_number: int | None = None,
    max_files: int = 500,
) -> int:
    # 扫描 checkout 目录，抽取符号和关系，写入数据库。返回抽到的符号数量
    # 先删掉同分支/同 PR 的旧记录，保证重新分析是干净的全量覆盖
    db.query(CodeRelation).filter(CodeRelation.repo_id == repo.id, CodeRelation.branch == branch, CodeRelation.pr_id == pr_id, CodeRelation.pr_number == pr_number).delete(synchronize_session=False)
    db.query(CodeSymbol).filter(CodeSymbol.repo_id == repo.id, CodeSymbol.branch == branch, CodeSymbol.pr_id == pr_id, CodeSymbol.pr_number == pr_number).delete(synchronize_session=False)
    symbol_rows: list[CodeSymbol] = []
    files_seen = 0
    # 第一遍：找符号定义
    for path in checkout_path.rglob("*"):  # rglob 递归遍历所有文件
        if files_seen >= max_files:
            break  # 最多扫 max_files 个文件，防止超大仓库卡死
        if not path.is_file():
            continue  # 跳过目录
        # 路径任何一级在 SKIP_DIRS 里就跳过（如 node_modules）
        if any(part in SKIP_DIRS for part in path.relative_to(checkout_path).parts):
            continue
        if not _is_text_candidate(path):
            continue  # 不是文本源码跳过
        text = _read_text(path, max_bytes=180_000)  # 读文本内容（限制大小）
        if not text:
            continue
        symbols = _symbol_matches(path, text)  # 抽符号
        if not symbols:
            continue  # 这文件没有任何符号（比如纯配置）跳过
        relative_path = path.relative_to(checkout_path).as_posix()  # 相对仓库根的路径（用 /）
        files_seen += 1
        for item in symbols:
            # 每个符号建一条 CodeSymbol 记录
            row = CodeSymbol(
                repo_id=repo.id,
                path=relative_path,
                name=str(item["name"]),
                kind=str(item["kind"]),
                language=str(item["language"]),
                start_line=int(item["start_line"]),
                end_line=None,  # 没算结束行
                branch=branch,
                commit_sha=commit_sha,
                pr_id=pr_id,
                pr_number=pr_number,
                meta={},
            )
            db.add(row)
            symbol_rows.append(row)
    db.flush()  # 先刷进事务，拿到每行 id（下面建关系要用）
    by_name = {row.name: row for row in symbol_rows}  # 用名字建索引，方便后面查「被调用的符号是谁」
    # 第二遍：找关系（import 和 call）
    for path in checkout_path.rglob("*"):
        if not path.is_file() or any(part in SKIP_DIRS for part in path.relative_to(checkout_path).parts) or not _is_text_candidate(path):
            continue
        text = _read_text(path, max_bytes=180_000)
        if not text:
            continue
        relative_path = path.relative_to(checkout_path).as_posix()
        # 用 IMPORT_RE 找所有 import 目标；next(...) 从三组捕获里取非空的那一个
        import_targets = [next((part for part in match.groups() if part), "") for match in IMPORT_RE.finditer(text)]
        for target in [item for item in import_targets if item]:
            # 每条 import 记一条 "imports" 关系
            db.add(
                CodeRelation(
                    repo_id=repo.id,
                    source_name=relative_path,
                    target_name=target,
                    relation_type="imports",
                    path=relative_path,
                    branch=branch,
                    pr_id=pr_id,
                    pr_number=pr_number,
                    meta={},
                )
            )
        # 找本地定义的符号作为「调用来源」
        local_symbols = [row for row in symbol_rows if row.path == relative_path]
        source = local_symbols[0] if local_symbols else None
        seen_calls: set[str] = set()  # 去重：同一名字在同一文件只记一次调用
        for match in CALL_RE.finditer(text):
            target_name = match.group(1)
            # 跳过已经记过的、以及语言关键字（if/for 这些也会被正则误抓）
            if target_name in seen_calls or target_name in {"if", "for", "while", "return", "print", "len", "str", "int"}:
                continue
            seen_calls.add(target_name)
            target = by_name.get(target_name)  # 看被调用的名字是否在本仓库符号表里
            db.add(
                CodeRelation(
                    repo_id=repo.id,
                    source_symbol_id=source.id if source else None,  # 调用方符号 id
                    target_symbol_id=target.id if target else None,  # 被调用方符号 id（找不到就 None）
                    source_name=source.name if source else relative_path,
                    target_name=target_name,
                    relation_type="calls",
                    path=relative_path,
                    branch=branch,
                    pr_id=pr_id,
                    pr_number=pr_number,
                    meta={"line": _line_for_offset(text, match.start())},  # 调用发生在第几行
                )
            )
    return len(symbol_rows)  # 返回抽到多少符号


def purge_legacy_code_documents(db: Session, repo_id: uuid.UUID | None = None) -> int:
    # 删除「老的」代码文件文档（source_type=="code_file"）。现在代码走 CodeSymbol 表，不再用 Document 存代码
    query = db.query(Document).filter(Document.source_type == "code_file")
    if repo_id is not None:
        query = query.filter(Document.repo_id == repo_id)
    return query.delete(synchronize_session=False)


async def sync_repository_code_analysis(db: Session, repo: Repository) -> int:
    # 对外异步入口：克隆/更新仓库 → 建代码图谱 → 清理旧文档 → 落库。返回符号数
    token = decrypt_token(repo.github_token_encrypted, settings.token_encryption_key) if repo.github_token_encrypted else None
    # asyncio.to_thread 把同步的 _sync_checkout 丢到线程池跑，不阻塞事件循环
    checkout_path = await asyncio.to_thread(_sync_checkout, repo, token)
    branch = repo.default_branch or _git_text(["branch", "--show-current"], checkout_path) or "main"  # 取当前分支名
    commit_sha = _git_text(["rev-parse", "HEAD"], checkout_path)  # 取当前 commit
    symbol_count = await asyncio.to_thread(
        _sync_code_graph,
        db,
        repo,
        checkout_path,
        branch=branch,
        commit_sha=commit_sha,
    )
    purge_legacy_code_documents(db, repo.id)  # 清掉旧的代码 Document
    repo.clone_url = repo.clone_url or _clone_url(repo)  # 补全 clone_url
    db.commit()
    return symbol_count


async def sync_repository_code_analysis_by_id(repo_id: uuid.UUID) -> int | None:
    # 按仓库 id 触发的便捷入口（被定时或接口调用）
    db = SessionLocal()
    try:
        repo = db.get(Repository, repo_id)
        if repo is None:
            return None
        try:
            return await sync_repository_code_analysis(db, repo)
        except Exception as exc:
            logger.warning("Failed to sync repository code analysis for %s: %s", repo_id, exc)
            return None  # 失败返回 None 而不是抛，调用方好处理
    finally:
        db.close()
