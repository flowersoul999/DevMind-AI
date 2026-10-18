"""
suites.py —— 评测集（Eval Suite）的加载与管理

本模块负责把「放在仓库 docs/datas/evals 目录下的 JSON 评测集」读进来，并转换成程序能用的
评测用例（RagEvalCase）。它还负责：校验 JSON 格式、判断某评测集是否适用于当前仓库、检查
评测集引用的来源文档是否已在库里建好索引、列出可用评测集、以及按 ID 载入某个评测集。

关键概念：
- 评测集 = 一组人工维护的「固定回归题」，相当于考试题库，保证每次发布前都过一遍；
- AUTO_SMOKE_SUITE_ID = 不读文件的「自动冒烟」套题：临时从当前仓库文档出题，只验证链路能跑通；
- LoadedEvalSuite = 一个不可变的数据类（frozen dataclass），装着「元信息 + 用例列表」。
本文件只加注释，未改动任何逻辑与变量名。."""

from __future__ import annotations  # 为什么：允许现代注解写法并支持类型前向引用

import hashlib  # hashlib：算文件哈希（sha256），用于标识评测集版本
import json  # json：解析 JSON 文件
from dataclasses import dataclass  # dataclass：省去手写 __init__ 的数据类
from pathlib import Path  # Path：跨平台的文件路径操作
from typing import Any  # Any：任意类型

from sqlalchemy.orm import Session  # Session：数据库会话

from app.db.models import Document, Repository  # 数据库表模型：文档、仓库
from app.services.evals.ragas_runner import RagEvalCase  # 单条评测用例结构


# 默认评测集目录：本文件向上数 4 级（evals -> services -> app -> backend -> 项目根）再进 docs/datas/evals
DEFAULT_SUITE_DIR = Path(__file__).resolve().parents[4] / "docs" / "datas" / "evals"
# 「自动文档冒烟」评测集的固定 ID
AUTO_SMOKE_SUITE_ID = "auto-document-smoke"


@dataclass(frozen=True)
class LoadedEvalSuite:
    # 载入后的评测集：frozen=True 表示创建后不可改（像只读试卷）
    metadata: dict[str, Any]  # 评测集元信息（id/name/描述/是否就绪等）
    cases: list[RagEvalCase]  # 该评测集包含的用例列表


def _read_suite(path: Path) -> tuple[dict[str, Any], str]:
    # 读取并解析一个评测集 JSON 文件，返回 (解析后的字典, sha256 哈希)
    raw = path.read_bytes()  # 以字节读入原始内容
    payload = json.loads(raw.decode("utf-8"))  # 解码成 UTF-8 字符串再解析 JSON
    if not isinstance(payload, dict):
        raise ValueError(f"评测集 {path.name} 必须是 JSON 对象")  # 顶层必须是对象（{}）
    name = str(payload.get("name") or "").strip()  # 取出 name 字段并去空白
    cases = payload.get("cases")  # 取出 cases 字段
    if not name or not isinstance(cases, list) or not cases:
        raise ValueError(f"评测集 {path.name} 缺少 name 或 cases")  # 两者缺一不可
    return payload, hashlib.sha256(raw).hexdigest()  # 返回内容与整文件哈希


def _matching_documents(db: Session, repo_id: Any, source_file: str) -> list[Document]:
    # 在数据库里找出「文件名/标题/来源文件」与给定 source_file 匹配、且属于该仓库的文档
    expected = Path(source_file).name.casefold()  # 只取文件名并转小写，便于不区分大小写比较
    documents = (
        db.query(Document)
        .filter(Document.repo_id == repo_id)  # 限定当前仓库
        .order_by(Document.created_at.asc())  # 按创建时间升序
        .all()
    )
    matches: list[Document] = []  # 收集匹配到的文档
    for document in documents:
        metadata = document.meta or {}  # 文档的元数据（可能为空）
        # 候选匹配键：文件名、展示标题、来源文件 三个名字都试一遍
        candidates = {
            Path(str(metadata.get("filename") or "")).name.casefold(),
            Path(str(metadata.get("display_title") or "")).name.casefold(),
            Path(str(metadata.get("source_file") or "")).name.casefold(),
        }
        if expected in candidates:
            matches.append(document)  # 任一候选命中即算匹配
    return matches


def _suite_applies_to_repo(payload: dict[str, Any], repo: Repository) -> bool:
    # 判断该评测集是否适用于当前仓库（评测集可声明只用于某些仓库）
    names = payload.get("repository_names")  # 评测集声明的适用仓库名列表
    if not isinstance(names, list) or not names:
        return True  # 没声明则对所有仓库适用
    accepted = {str(item).casefold() for item in names if str(item).strip()}  # 归一化后的适用名集合
    # 当前仓库名或全名（owner/name）命中其一即适用
    return repo.name.casefold() in accepted or repo.full_name.casefold() in accepted


def _suite_metadata(
    db: Session,
    repo: Repository,
    path: Path,
    payload: dict[str, Any],
    sha256: str,
) -> dict[str, Any]:
    # 计算一个评测集的「元信息」：包括是否就绪（引用的来源是否都已索引）
    missing_sources: list[str] = []  # 收集缺失的来源文件
    for item in payload["cases"]:
        source_file = str(item.get("source_file") or "").strip()  # 用例期望的来源文件
        if source_file and not _matching_documents(db, repo.id, source_file):
            missing_sources.append(source_file)  # 库里没匹配到该来源则记为缺失
    missing_sources = list(dict.fromkeys(missing_sources))  # 去重且保持顺序
    return {
        "id": str(payload["name"]),  # 评测集 ID = name
        "name": str(payload["name"]),
        "label": str(payload.get("label") or payload["name"]),  # 展示标签（缺则用 name）
        "description": str(payload.get("description") or "人工维护的固定回归评测集"),  # 描述，缺则给默认
        "schema_version": int(payload.get("schema_version") or 1),  # 评测集格式版本，默认 1
        "case_count": len(payload["cases"]),  # 用例数量
        "sha256": sha256,  # 文件哈希（用于版本标识）
        "source": "fixed",  # 来源类型：固定题库
        "filename": path.name,  # 文件名
        "ready": not missing_sources,  # 是否就绪：没有缺失来源即为 True
        "missing_sources": missing_sources,  # 缺失来源列表（用于前端提示）
        "recommended": bool(payload.get("recommended", False)),  # 是否官方推荐
    }


def list_eval_suites(
    db: Session,
    repo_id: Any,
    *,
    suite_dir: Path = DEFAULT_SUITE_DIR,
) -> list[dict[str, Any]]:
    # 列出当前仓库可用的所有评测集（含固定题库 + 自动冒烟），返回元信息列表
    repo = db.get(Repository, repo_id)  # 按 ID 取仓库
    if repo is None:
        raise ValueError(f"Repository {repo_id} was not found")  # 仓库不存在直接报错
    suites: list[dict[str, Any]] = []  # 收集结果
    if suite_dir.is_dir():
        # 遍历目录下所有 *.json 评测集
        for path in sorted(suite_dir.glob("*.json")):
            payload, sha256 = _read_suite(path)  # 读并解析
            if _suite_applies_to_repo(payload, repo):
                suites.append(_suite_metadata(db, repo, path, payload, sha256))  # 适用的才加入
    # 排序：推荐的优先、就绪的优先、再按 label 字母序
    suites.sort(key=lambda item: (not item["recommended"], not item["ready"], item["label"]))
    suites.append(
        {
            # 追加「自动文档冒烟」套题（它不读文件，永远可用）
            "id": AUTO_SMOKE_SUITE_ID,
            "name": AUTO_SMOKE_SUITE_ID,
            "label": "自动文档冒烟测试",
            "description": "从当前仓库文档临时出题，只验证链路能运行，不能作为发布基线。",
            "schema_version": 1,
            "case_count": None,  # 用例数未知（临时生成）
            "sha256": None,
            "source": "auto_smoke",
            "filename": None,
            "ready": True,
            "missing_sources": [],
            "recommended": False,
        }
    )
    return suites


def load_eval_suite(
    db: Session,
    repo_id: Any,
    suite_id: str,
    *,
    suite_dir: Path = DEFAULT_SUITE_DIR,
) -> LoadedEvalSuite:
    # 按 ID 载入一个评测集，返回 (元信息 + 转换好的用例列表)
    if suite_id == AUTO_SMOKE_SUITE_ID:
        # 自动冒烟套题：用例在运行时临时生成，这里先返回空用例 + 元信息
        return LoadedEvalSuite(
            metadata={
                "id": AUTO_SMOKE_SUITE_ID,
                "name": AUTO_SMOKE_SUITE_ID,
                "label": "自动文档冒烟测试",
                "description": "从当前仓库文档临时出题，只验证链路能运行，不能作为发布基线。",
                "source": "auto_smoke",
                "sha256": None,
                "case_count": None,
            },
            cases=[],
        )

    repo = db.get(Repository, repo_id)  # 取仓库
    if repo is None:
        raise ValueError(f"Repository {repo_id} was not found")  # 不存在则报错
    # 遍历目录找 name == suite_id 且适用于本仓库的评测集
    for path in sorted(suite_dir.glob("*.json")) if suite_dir.is_dir() else []:
        payload, sha256 = _read_suite(path)
        if str(payload["name"]) != suite_id or not _suite_applies_to_repo(payload, repo):
            continue  # 名字不对或不适用则跳过
        metadata = _suite_metadata(db, repo, path, payload, sha256)
        if not metadata["ready"]:
            # 还没就绪（来源没索引）：明确报错告诉缺了哪些来源
            missing = "、".join(metadata["missing_sources"])
            raise ValueError(f"评测集 {suite_id} 缺少已索引来源：{missing}")
        cases: list[RagEvalCase] = []  # 转换后的用例列表
        for index, item in enumerate(payload["cases"], start=1):
            case_id = str(item.get("id") or f"case-{index}").strip()  # 用例 ID（缺则按序号补）
            question = str(item.get("question") or "").strip()  # 问句
            reference = str(item.get("reference") or "").strip()  # 标准答案
            if not question or not reference:
                raise ValueError(f"评测集 {suite_id} 的 {case_id} 缺少 question 或 reference")  # 两者缺一报错
            source_file = str(item.get("source_file") or "").strip()  # 期望来源文件
            # 若声明了来源文件，查出库里匹配文档，收集它们的 id 与 source_id 作为「期望命中来源」
            documents = _matching_documents(db, repo.id, source_file) if source_file else []
            expected_source_ids = list(
                dict.fromkeys(  # 去重且保序
                    str(value)
                    for document in documents
                    for value in (document.id, document.source_id)
                    if value
                )
            )
            cases.append(
                RagEvalCase(
                    id=case_id,
                    question=question,
                    reference=reference,
                    expected_source_ids=expected_source_ids,
                    source_type=str(item.get("source_type") or "knowledge_file"),  # 缺则默认 knowledge_file
                    required_tools=[str(value) for value in item.get("required_tools") or []],  # 期望调用的工具
                    expected_tool_calls=[dict(value) for value in item.get("expected_tool_calls") or []],  # 期望工具调用结构
                    kind=str(item.get("kind") or "positive"),  # 用例类别，默认 positive
                    must_include=[str(value) for value in item.get("must_include") or []],  # 回答必须包含
                    must_not_include=[str(value) for value in item.get("must_not_include") or []],  # 回答禁止包含
                    answer_regex=str(item.get("answer_regex") or "").strip() or None,  # 回答格式正则（空则 None）
                    source_file=source_file or None,
                )
            )
        metadata["case_count"] = len(cases)  # 回填实际用例数
        return LoadedEvalSuite(metadata=metadata, cases=cases)
    raise ValueError(f"未找到评测集：{suite_id}")  # 遍历完仍没找到则报错
