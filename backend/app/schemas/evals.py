"""
evals.py —— 评测（Eval）接口的数据契约（Schema 层）

本文件服务于「跑 RAG / Agent 质量评测」接口（具体打分逻辑在 services/evals/）：
- RagEvalCaseRequest：定义一条评测用例（问句、标准答案、期望命中的来源/工具、确定性规则）；
- EvalRunRequest：发起一次评测运行的参数（评测类型、是否用 Judge 打分、Top-K 等）；
- EvalIssueDraftRequest / EvalRunResponse：给 Issue 出题用的最小请求、评测运行结果。

这些模型是「评测的试题与考卷」：用例描述期望的答案与证据，运行参数控制怎么考。."""

from typing import Any  # Any：任意类型，用于工具调用等自由结构

from pydantic import BaseModel, Field  # BaseModel：基类；Field：字段约束/默认值工厂
from uuid import UUID  # UUID：唯一 ID


class RagEvalCaseRequest(BaseModel):
    """RagEvalCaseRequest."""
    # 一条 RAG 评测用例（题目 + 标准答案 + 验收条件）
    id: str = ""  # 用例 ID；默认空（运行时若没给会自动补）
    question: str  # 评测问句，必填
    reference: str  # 标准答案/参考文本（用于比对），必填
    expected_source_ids: list[str] = Field(default_factory=list)  # 期望命中的来源 ID 列表，默认空
    source_type: str | None = None  # 限定来源类型；可空
    required_tools: list[str] = Field(default_factory=list)  # 必须调用到的工具名列表，默认空
    expected_tool_calls: list[dict[str, Any]] = Field(default_factory=list)  # 期望的工具调用结构，默认空
    kind: str = "positive"  # 用例类别（positive 正向 / negative 负向），默认 positive
    must_include: list[str] = Field(default_factory=list)  # 回答必须包含的关键词，默认空
    must_not_include: list[str] = Field(default_factory=list)  # 回答禁止包含的关键词，默认空
    answer_regex: str | None = None  # 回答必须满足的正则格式；可空（不校验格式）
    source_file: str | None = None  # 该用例来源文件名（用于索引校验）；可空


class EvalRunRequest(BaseModel):
    """EvalRunRequest."""
    # 发起一次评测运行的请求体
    name: str = "basic_eval"  # 本次运行名称，默认 "basic_eval"
    repo_id: UUID | None = None  # 指定仓库；可空（不指定则用默认用例）
    eval_types: list[str] = Field(default_factory=lambda: ["issue_classification", "rag", "pr_summary", "tool_calls"])  # 要跑的评测类型；默认全套四类
    run_judge: bool = True  # 是否调用 LLM 当裁判（Judge）打分；默认是
    top_k: int = Field(default=5, ge=1, le=20)  # 检索 Top-K；默认 5，限定 1~20
    suite_id: str | None = None  # 指定评测集 ID；可空
    cases: list[RagEvalCaseRequest] | None = None  # 自定义用例列表；可空（不传则用内置/自动用例）


class EvalIssueDraftRequest(BaseModel):
    """EvalIssueDraftRequest."""
    # 给「Issue 出题」用的最小请求（可只给标题）
    title: str | None = None  # 想出题的 Issue 标题；可空


class EvalRunResponse(BaseModel):
    """EvalRunResponse."""
    # 评测运行的响应
    eval_id: str  # 本次评测运行 ID，必填
    status: str  # 运行状态，必填
    result: dict  # 评测结果（任意结构，内含各项指标），必填
