"""
services/code_search.py —— 在「已下载到本地的仓库代码」里做关键词搜索
============================================================================

上一步 code_analysis.py 把仓库克隆到了本地。本文件就在这个本地副本上搜索：用户输入
一句话（比如「用户登录失败怎么处理」或「AuthService.login」），它被拆成若干关键词，
然后在代码文件里找命中行，按相关度排序返回「文件路径 + 行号 + 那行内容」。

两个搜索后端：
  - 优先用 ripgrep（rg，超快的命令行搜索工具），装了就用；
  - 没装 rg 就用纯 Python 扫描兜底（慢但能用）。

安全细节：跳过依赖目录（node_modules 等）、绝不搜 .env 等敏感配置文件，并防止路径
逃出仓库目录（路径穿越防护）。."""

import json  # 解析 ripgrep 的 --json 输出
import re  # 正则：分词、拼搜索 pattern
import shutil  # 判断 rg 是否安装
import subprocess  # 调 rg
from pathlib import Path  # 路径
from typing import Any  # 任意类型

from app.db.models import Repository  # 仓库表


# 搜索时跳过的目录（和 code_analysis 一致）
SKIP_DIRS = {
    ".git", ".hg", ".svn", ".next", ".nuxt", ".venv", "venv", "node_modules",
    "dist", "build", "coverage", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
}

# 绝不搜索的敏感配置文件名（防止把密钥泄露到搜索结果）
SECRET_FILENAMES = {".env", ".env.local", ".env.production", ".npmrc", ".pypirc"}

# 视为文本源码的后缀集合
TEXT_EXTENSIONS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".java", ".go", ".rs", ".cs",
    ".cpp", ".c", ".h", ".hpp", ".php", ".rb", ".swift", ".kt", ".kts", ".scala", ".sql",
    ".sh", ".ps1", ".bat", ".cmd", ".html", ".css", ".scss", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".md", ".mdx", ".txt",
}

TEXT_FILENAMES = {
    "Dockerfile", "Makefile", "README", "LICENSE", "package.json", "requirements.txt", "pyproject.toml",
}

# 把用户查询拆词的正则：抓英文/带符号标识符、3 位以上数字、2 字以上中文
_QUERY_TOKEN_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_.$:/\\-]*|[0-9]{3,}|[\u4e00-\u9fff]{2,}")
# 停用词：这些词太泛、对搜索没用，直接忽略（中英文都有）
_STOP_TERMS = {
    "about", "after", "before", "code", "error", "failed", "failure", "file", "from", "into",
    "issue", "project", "pull", "request", "test", "tests", "that", "the", "this", "with",
    "代码", "问题", "失败", "项目", "相关", "这个",
}


def repository_checkout_path(repo: Repository) -> Path:
    # 算出仓库本地 checkout 目录：优先用用户配置的 local_path，否则用 code_analysis 的默认路径
    raw_path = str(getattr(repo, "local_path", None) or "").strip()
    if raw_path:
        path = Path(raw_path).expanduser()  # 展开 ~
        if not path.is_absolute():
            path = Path.cwd() / path
        return path.resolve()
    # 延迟导入，避免顶层循环依赖
    from app.services.code_analysis import _repo_checkout_path
    return _repo_checkout_path(repo).resolve()


def resolve_search_root(checkout_path: str | Path, relative_path: str | None = None) -> tuple[Path, Path]:
    # 校验并解析「要搜索的根目录」。返回 (checkout 根, 实际搜索目录)
    checkout = Path(checkout_path).expanduser().resolve()
    if not checkout.exists() or not checkout.is_dir():
        raise ValueError("仓库代码还没有同步到本地。")  # 没同步就搜不了
    # 规整相对路径：空或根符号都当 "."（当前目录）
    requested = str(relative_path or ".").strip()
    requested = "." if requested in {"", ".", "./", "/", "\\"} else requested.lstrip("/\\")
    root = (checkout / requested).resolve()
    try:
        root.relative_to(checkout)  # 确保 root 在 checkout 之内
    except ValueError as exc:
        # 路径穿越防护：若 root 逃出 checkout 目录，拒绝
        raise ValueError("搜索路径位于仓库工作区之外。") from exc
    if any(part in SKIP_DIRS for part in root.relative_to(checkout).parts):
        raise ValueError("搜索路径位于被跳过的目录中。")
    if root.name in SECRET_FILENAMES:
        raise ValueError("不会搜索敏感配置文件。")  # 安全：不搜 .env 等
    if not root.exists():
        raise ValueError("搜索路径不存在。")
    return checkout, root


def query_terms(query: str, limit: int = 16) -> list[str]:
    # 把用户的一句话查询拆成「关键词列表」，按相关度排序，截取前 limit 个
    candidates: list[tuple[int, int, str]] = []  # (是否像代码, 词长, 词) —— 用作排序键
    fallback_candidates: list[tuple[int, int, str]] = []  # 兜底（就算全是停用词也保留）
    seen: set[str] = set()  # 去重
    for position, match in enumerate(_QUERY_TOKEN_RE.finditer(query or "")):
        value = match.group(0).strip()
        normalized = value.lower()  # 统一小写比对的键
        if len(normalized) < 2 or normalized in seen:
            continue  # 太短或重复跳过
        seen.add(normalized)
        # 判断「是否像代码标识符」：含特殊符号/数字，或同时有大写和小写（驼峰）
        code_like = bool(
            re.search(r"[_.$:/\\\-0-9]", value)
            or (any(char.islower() for char in value) and any(char.isupper() for char in value))
        )
        candidate = (1 if code_like else 0, len(value), normalized)
        fallback_candidates.append(candidate)
        if normalized not in _STOP_TERMS:
            candidates.append(candidate)  # 停用词不放进正式候选
    candidates = candidates or fallback_candidates  # 全被过滤就用兜底，至少返回点东西
    # 排序：代码词优先(-1)、长的优先(-len)、最后按字母；取前 limit（至少 1 个）
    candidates.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return [item[2] for item in candidates[: max(1, limit)]]


def _is_text_file(path: Path) -> bool:
    # 这个文件是否可搜：排除敏感名、日志/数据库二进制后缀，其余按文本规则判断
    if path.name in SECRET_FILENAMES or path.suffix.lower() in {".log", ".db", ".sqlite", ".sqlite3"}:
        return False
    return path.name in TEXT_FILENAMES or path.suffix.lower() in TEXT_EXTENSIONS or path.name.lower().endswith("dockerfile")


def _read_text(path: Path, max_bytes: int = 180_000) -> str | None:
    # 安全读文本（和 code_analysis 的版本类似）：超长/二进制/读不出都返回 None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if not data or len(data) > max_bytes or b"\x00" in data[:4096]:
        return None
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def read_code_excerpt(checkout_path: str | Path, relative_path: str, start_line: int, line_count: int = 7) -> str:
    # 读取某文件从 start_line 起 line_count 行的片段，返回「path:行范围 + 带行号的代码」
    checkout, path = resolve_search_root(checkout_path, relative_path)
    if not path.is_file() or not _is_text_file(path):
        return ""  # 不是可搜文件就返回空
    text = _read_text(path)
    if text is None:
        return ""
    lines = text.splitlines()  # 按行拆
    start = max(int(start_line), 1)  # 起始行至少 1
    end = min(start + max(int(line_count), 1) - 1, len(lines))  # 结束行不超过总行数
    if start > end:
        return ""
    relative = path.relative_to(checkout).as_posix()
    # 拼成 "path:start-end\n行号: 内容..." 方便 LLM/前端展示
    return f"{relative}:{start}-{end}\n" + "\n".join(
        f"{line_number}: {lines[line_number - 1]}" for line_number in range(start, end + 1)
    )


def _score_match(relative_path: str, line: str, terms: list[str]) -> float:
    # 给「某行」打分：命中词种类越多越准、出现次数越多越准、路径里命中加权（路径命中通常更相关）
    path_text = relative_path.lower()
    line_text = line.lower()
    # matched：有几个不同的 term 出现在这一行或路径里
    matched = sum(1 for term in terms if term in line_text or term in path_text)
    # occurrences：每个 term 在行里出现次数（最多算 4 次，防刷分）
    occurrences = sum(min(line_text.count(term), 4) for term in terms)
    path_hits = sum(path_text.count(term) for term in terms)  # 路径命中次数
    # 最终分：命中种类×4 + 出现次数 + 路径命中×2
    return float(matched * 4 + occurrences + path_hits * 2)


def _ripgrep_matches(checkout: Path, root: Path, terms: list[str], max_matches: int) -> list[tuple[float, str, int, str]]:
    # 用 ripgrep 搜索，返回 (分数, 相对路径, 行号, 行内容) 列表，每个文件只保留最高分行
    pattern = "|".join(re.escape(term) for term in terms)  # 把多个关键词用 | 拼成正则「或」
    command = [
        "rg", "--json", "--ignore-case", "--line-number", "--max-columns", "500",
        "--glob", "!**/.env*", "--glob", "!**/.npmrc", "--glob", "!**/.pypirc",
        "--glob", "!**/*.log", "--glob", "!**/*.db", "--glob", "!**/*.sqlite*",
        pattern, str(root),
    ]
    # 为什么排除 .env/.npmrc 等：这些是敏感文件，即使 rg 默认会搜，我们也硬性屏蔽
    result = subprocess.run(
        command, cwd=str(checkout), check=False, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", timeout=15,
    )
    if result.returncode not in {0, 1}:
        # rg 返回 0=有匹配，1=无匹配，其它都是真错误
        raise RuntimeError(result.stderr.strip() or "ripgrep 搜索失败")

    best_by_file: dict[str, tuple[float, str, int, str]] = {}  # 每文件只留最高分那行
    for raw_line in result.stdout.splitlines():
        try:
            event = json.loads(raw_line)  # rg --json 每行是一个 JSON 事件
        except json.JSONDecodeError:
            continue
        if event.get("type") != "match":
            continue  # 只要 match 事件
        data = event.get("data") or {}
        raw_path = str((data.get("path") or {}).get("text") or "")
        path = Path(raw_path)
        if not path.is_absolute():
            path = checkout / path  # 相对路径拼成绝对
        try:
            relative = path.resolve().relative_to(checkout).as_posix()
        except ValueError:
            continue  # 逃出 checkout 的跳过
        if any(part in SKIP_DIRS for part in Path(relative).parts) or Path(relative).name in SECRET_FILENAMES:
            continue  # 跳过忽略目录/敏感文件
        line_number = int(data.get("line_number") or 1)
        line = str((data.get("lines") or {}).get("text") or "").strip()[:320]  # 行内容截断 320 字
        score = _score_match(relative, line, terms)
        candidate = (score, relative, line_number, line)
        current = best_by_file.get(relative)
        if current is None or candidate[0] > current[0]:
            best_by_file[relative] = candidate  # 同文件更高分就替换
        if len(best_by_file) >= max_matches:
            break  # 达到上限提前结束
    return list(best_by_file.values())


def _python_matches(checkout: Path, root: Path, terms: list[str], max_files: int = 1000) -> list[tuple[float, str, int, str]]:
    # 纯 Python 兜底搜索：遍历文件，逐行比对关键词打分（没装 rg 时用）
    paths = [root] if root.is_file() else root.rglob("*")  # root 是文件就只搜它，否则递归
    matches: list[tuple[float, str, int, str]] = []
    files_seen = 0
    for path in paths:
        if not path.is_file():
            continue
        relative = path.relative_to(checkout)
        if any(part in SKIP_DIRS for part in relative.parts) or not _is_text_file(path):
            continue
        files_seen += 1
        if files_seen > max_files:
            break  # 最多扫 1000 个文件，防止超大仓库卡死
        text = _read_text(path)
        if text is None:
            continue
        relative_text = relative.as_posix()
        best: tuple[float, str, int, str] | None = None  # 该文件最高分行
        for line_number, line in enumerate(text.splitlines(), start=1):
            score = _score_match(relative_text, line, terms)
            if score <= 0:
                continue
            candidate = (score, relative_text, line_number, line.strip()[:320])
            if best is None or candidate[0] > best[0]:
                best = candidate
        if best:
            matches.append(best)
    return matches


def search_checkout_code(
    checkout_path: str | Path,
    query: str,
    *,
    limit: int = 12,
    relative_path: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    # 对外主入口：在指定 checkout 目录里搜代码，返回结构化结果列表
    terms = query_terms(query)  # 拆词
    if not terms:
        return []  # 没有有效词就返回空
    checkout, root = resolve_search_root(checkout_path, relative_path)
    capped_limit = min(max(int(limit), 1), 50)  # limit 限制在 1~50
    search_tool = "python_scan"  # 默认标记用 Python 兜底
    if shutil.which("rg"):
        # 装了 ripgrep 就优先用，更快
        try:
            matches = _ripgrep_matches(checkout, root, terms, max_matches=max(capped_limit * 8, 80))
            search_tool = "ripgrep"
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            matches = _python_matches(checkout, root, terms)  # rg 出错再兜底
    else:
        matches = _python_matches(checkout, root, terms)
    matches.sort(key=lambda item: (-item[0], item[1], item[2]))  # 按分数降序
    if not matches:
        return []
    max_score = max(item[0] for item in matches) or 1.0  # 最高分，用于归一化
    output = []
    for score, path, line_number, line in matches[:capped_limit]:
        # 每条结果带上元数据（路径、行号、检索方式、额外 metadata）
        item_metadata = {
            "path": path,
            "line": line_number,
            "start_line": line_number,
            "end_line": line_number,
            "checkout_path": str(checkout),
            "retrieval_mode": "workspace",
            "search_tool": search_tool,
            **(metadata or {}),
        }
        output.append(
            {
                "id": f"workspace:{path}:{line_number}",  # 结果唯一 id
                "title": f"{path}:{line_number}",  # 标题
                "source_type": "workspace_file",  # 来源类型
                "source_id": path,
                "snippet": f"{path}:{line_number}: {line}",  # 片段文本
                "score": score / max_score,  # 归一化到 0~1
                "metadata": item_metadata,
            }
        )
    return output


def search_repository_code(
    repo: Repository,
    query: str,
    *,
    limit: int = 12,
    relative_path: str | None = None,
    checkout_path: str | Path | None = None,
    metadata: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    # 给「只知道仓库对象」的调用方用的入口：先解析出本地 checkout 目录，再委托 search_checkout_code
    root = Path(checkout_path).resolve() if checkout_path else repository_checkout_path(repo)
    if not root.exists():
        return []  # 没同步到本地就返回空
    try:
        return search_checkout_code(
            root,
            query,
            limit=limit,
            relative_path=relative_path,
            # 把 repo_id 塞进 metadata，方便上层知道搜的是哪个仓库
            metadata={"repo_id": str(repo.id), **(metadata or {})},
        )
    except ValueError:
        return []  # 路径不合法就返回空
