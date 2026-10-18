"""
ragas_runner.py —— 用 Ragas 真实跑 ChatAgent 并打分的 RAG 质量评测器

本模块是「评测服务」的核心：它不造假，而是真正启动生产环境的 ChatAgent，让它去回答一组
评测问题，然后用 Ragas（一个 RAG 评测框架）的若干指标给回答打分，再用一套「硬规则」做确定性
门禁，最后汇成一份带诊断建议的评测报告。它还支持「重评分」：Judge 某指标失败时，不重跑 Agent，
只把缺失指标补算一遍。

主要组成：
- RagEvalCase：一条评测用例（题目 + 标准答案 + 期望来源/工具 + 确定性规则）；
- RagasCollectionsScorer：把本项目的模型客户端适配成 Ragas 0.4 Collections API 的打分器；
- run_rag_quality_eval：主流程——逐题跑 Agent、算指标、做硬规则、汇总报告；
- rescore_rag_result：只补算失败/缺失的 Judge 指标，不重跑 Agent。

本文件只加注释，未改动任何逻辑、变量名与算法。."""

import asyncio  # asyncio：异步并发（同时跑多个评分任务）
import copy  # copy：深拷贝原评测结果，避免重评分时污染原始数据
import importlib.metadata  # 读取已安装包版本（如 ragas 版本）
import inspect  # inspect：判断对象是否可 await（协程）
import math  # math：数学函数（log2、isfinite）
import re  # re：正则（拆句、答案格式校验）
import sys  # sys：操作系统模块表（用于打补丁）
import time  # time：计时（统计每条/整体耗时）
import types  # types：动态创建模块对象（补丁用）
import uuid  # uuid：生成唯一 ID
import warnings  # warnings：屏蔽特定弃用警告
from collections.abc import Awaitable, Callable  # 类型：可等待对象、可调用对象
from dataclasses import dataclass, field  # dataclass：数据类；field：字段默认值工厂
from typing import Any, Protocol  # Any：任意类型；Protocol：结构化子类型（接口约定）

from sqlalchemy.orm import Session  # Session：数据库会话

from app.core.config import settings  # 全局配置（模型地址、密钥、阈值等）
from app.db.models import Conversation, Document, Issue, PullRequest, Repository, WorkflowRun  # 数据库表模型
from app.services.agents.chat_agent import ChatAgent  # 被评测的生产 ChatAgent
from app.services.chat_memory import ContextAssembler, EvidenceStore  # 上下文拼装与证据存储
from app.services.rag.embeddings import embed_text  # 向量化函数
from app.services.rag.source_policy import RAG_SOURCE_TYPES  # 允许进入评测的来源类型集合


# Ragas 的「核心 RAG 指标」名称：上下文精确度、上下文召回、忠实度、答案相关性
CORE_RAGAS_METRICS = (
    "context_precision",
    "context_recall",
    "faithfulness",
    "answer_relevancy",
)
# 必过的指标（核心指标 + agent 目标准确率）
REQUIRED_RAGAS_METRICS = (*CORE_RAGAS_METRICS, "agent_goal_accuracy")
# 仅与 agent 工具调用相关的指标：目标准确率、工具调用准确率、工具调用 F1
AGENT_RAGAS_METRICS = (
    "agent_goal_accuracy",
    "tool_call_accuracy",
    "tool_call_f1",
)

_FAITHFULNESS_CHUNK_CHARS = 700  # 忠实度评分时，单段答案的最大字符数（过长就切段）


def _split_faithfulness_response(text: str, max_chars: int = _FAITHFULNESS_CHUNK_CHARS) -> list[str]:
    """Split a long answer without dropping content before native Ragas scoring.

    忠实度评分先让裁判模型抽取答案里的所有「断言」。某些推理模型对中等长度答案也会耗尽结构化
    输出额度。把答案切成更小的语义片段再评分，可保持该原生指标可靠；最终分数按各片段字数加权。."""

    normalized = text.strip()  # 去掉首尾空白
    if not normalized:
        return [""]  # 空答案返回一个空片段

    # 按中文/英文句号、问号、感叹号或换行切句，并去掉空片段
    units = [unit.strip() for unit in re.split(r"(?<=[。！？.!?])\s+|\n+", normalized) if unit.strip()]
    chunks: list[str] = []  # 收集切好的片段
    current = ""  # 当前正在累积的片段
    for unit in units:
        while len(unit) > max_chars:
            # 单句本身就超长：先把 current 收尾，再把超长句硬切成 max_chars 的小块
            if current:
                chunks.append(current)
                current = ""
            chunks.append(unit[:max_chars])
            unit = unit[max_chars:]
        candidate = f"{current}\n{unit}".strip() if current else unit  # 尝试把本句接到 current 后面
        if current and len(candidate) > max_chars:
            # 接上会超长：先把 current 收尾，本句另起一段
            chunks.append(current)
            current = unit
        else:
            current = candidate
    if current:
        chunks.append(current)  # 收尾剩余片段
    return chunks or [normalized]  # 兜底：至少返回原文


class RagasUnavailableError(RuntimeError):
    """Ragas、其裁判模型或评测向量不可用时的异常。."""


@dataclass
class RagEvalCase:
    # 一条评测用例的数据结构（纯数据，无逻辑）
    question: str  # 评测问句，必填
    reference: str  # 标准答案/参考文本，必填
    expected_source_ids: list[str]  # 期望命中的来源 ID 列表，必填
    source_type: str | None = None  # 限定来源类型；可空
    required_tools: list[str] = field(default_factory=list)  # 必须调用的工具名列表，默认空
    expected_tool_calls: list[dict[str, Any]] = field(default_factory=list)  # 期望工具调用结构，默认空
    kind: str = "positive"  # 用例类别（positive/negative），默认 positive
    must_include: list[str] = field(default_factory=list)  # 回答必须包含的关键词，默认空
    must_not_include: list[str] = field(default_factory=list)  # 回答禁止包含的关键词，默认空
    answer_regex: str | None = None  # 回答应满足的正则格式；可空
    source_file: str | None = None  # 来源文件名；可空
    id: str = ""  # 用例 ID，默认空


class SemanticScorer(Protocol):
    """SemanticScorer."""
    # 评分器的「接口约定」：任何实现了 version/judge_model 与 score() 的对象都可当评分器
    version: str  # 评分器版本
    judge_model: str  # 裁判模型名

    async def score(
        self,
        *,
        user_input: str,
        response: str,
        retrieved_contexts: list[str],
        reference: str,
        tool_calls: list[dict[str, Any]],
        tool_observations: list[dict[str, Any]],
        expected_tool_calls: list[dict[str, Any]],
        metric_names: set[str] | None = None,
    ) -> tuple[dict[str, float], dict[str, str]]: ...
    # 返回 (各指标分数, 各指标错误信息)


ProgressCallback = Callable[[dict[str, Any]], Awaitable[None] | None]  # 进度回调类型：接收字典，可异步可同步


async def _emit_progress(callback: ProgressCallback | None, **payload: Any) -> None:
    # 统一的进度上报：把当前阶段信息通过 callback 推给前端；callback 为空则什么都不做
    if callback is None:
        return  # 没传回调直接返回
    pending = callback(payload)  # 调用回调（可能返回协程）
    if inspect.isawaitable(pending):
        await pending  # 若是协程则 await 等待完成


def _ensure_ragas_compatibility() -> None:
    """为 langchain-community 0.4 移除的 Ragas 0.4.3 import 打桥接补丁。

    Ragas 仅为了一次 isinstance 检查而 import 旧的 ChatVertexAI 模块。本项目不用 VertexAI，
    所以放一个最小占位模块，让受支持的 Collections API 可用，而不必降级 LangChain/LangGraph。."""

    module_name = "langchain_community.chat_models.vertexai"  # 被移除的模块名
    if module_name in sys.modules:
        return  # 已经存在则无需处理
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message="`langchain-community` is being sunset.*",
                category=DeprecationWarning,
            )
            __import__(module_name)  # 尝试正常 import
    except ModuleNotFoundError as exc:
        if exc.name != module_name:
            raise  # 不是这个模块缺失就原样抛出
        # 该模块确实不存在：造一个最小占位模块塞进 sys.modules，让 isinstance 检查不报错
        shim = types.ModuleType(module_name)
        shim.ChatVertexAI = type("ChatVertexAI", (), {})
        sys.modules[module_name] = shim


class RagasCollectionsScorer:
    """把 DevFlow 的模型客户端适配成 Ragas 0.4 Collections API 的评分器。."""

    def __init__(self) -> None:
        _ensure_ragas_compatibility()  # 先确保 Ragas 依赖的补丁就位
        try:
            # 延迟 import Ragas 相关类（这些依赖是可选的，没装时不应让模块加载失败）
            from openai import AsyncOpenAI
            from ragas.embeddings.base import BaseRagasEmbedding
            from ragas.llms import llm_factory
            from ragas.metrics.collections import (
                AgentGoalAccuracyWithReference,
                AnswerRelevancy,
                ContextPrecision,
                ContextRecall,
                Faithfulness,
                ToolCallAccuracy,
                ToolCallF1,
            )
        except Exception as exc:  # pragma: no cover - 取决于可选包是否安装
            raise RagasUnavailableError(f"Unable to import Ragas 0.4 Collections API: {exc}") from exc

        # 项目自己的向量化实现，包成 Ragas 需要的接口
        class ProjectEmbedding(BaseRagasEmbedding):
            """ProjectEmbedding."""
            def embed_text(self, text: str, **_: Any) -> list[float]:
                return embed_text(text)  # 同步向量化

            async def aembed_text(self, text: str, **_: Any) -> list[float]:
                return await asyncio.to_thread(embed_text, text)  # 异步向量化（丢到线程池，不阻塞事件循环）

        # 选裁判模型：优先用专门的 ragas 配置，否则退回通用 LLM 配置
        api_key = settings.ragas_judge_api_key or settings.llm_api_key
        base_url = (settings.ragas_judge_base_url or settings.llm_base_url).rstrip("/")  # 去尾部斜杠
        model = settings.ragas_judge_model or settings.llm_model
        if not api_key:
            raise RagasUnavailableError("RAGAS judge API key is not configured")  # 没密钥直接不可用
        if not base_url or not model:
            raise RagasUnavailableError("RAGAS judge base URL or model is not configured")  # 没地址/模型也不可用

        try:
            # 用 OpenAI 兼容客户端连裁判模型（temperature=0 让评分更稳定可复现）
            client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=settings.ragas_timeout_seconds)
            llm = llm_factory(
                model,
                client=client,
                temperature=0,
                max_tokens=settings.ragas_judge_max_tokens,
            )
            embedding = ProjectEmbedding()  # 项目向量化适配器
            self.metrics = {
                # 把各 Ragas 指标实例化好，挂到 self.metrics 上供后面 score() 调用
                "context_precision": ContextPrecision(llm=llm),
                "context_recall": ContextRecall(llm=llm),
                "faithfulness": Faithfulness(llm=llm),
                "answer_relevancy": AnswerRelevancy(
                    llm=llm,
                    embeddings=embedding,
                    strictness=max(1, settings.ragas_answer_relevancy_strictness),
                ),
                "agent_goal_accuracy": AgentGoalAccuracyWithReference(llm=llm),
                "tool_call_accuracy": ToolCallAccuracy(strict_order=False),
                "tool_call_f1": ToolCallF1(),
            }
            self.message_types = self._message_types()  # 记住 Ragas 的消息类型
            self.version = importlib.metadata.version("ragas")  # 记录 ragas 版本
            self.judge_model = model  # 记录裁判模型名
        except Exception as exc:
            raise RagasUnavailableError(f"Unable to initialize Ragas judge: {exc}") from exc

    @staticmethod
    def _message_types() -> tuple[Any, Any, Any, Any]:
        # 延迟取 Ragas 的消息类型（Human/AI/Tool/Call），避免在 import 期就依赖 ragas
        from ragas.messages import AIMessage, HumanMessage, ToolCall, ToolMessage

        return HumanMessage, AIMessage, ToolMessage, ToolCall

    def _messages(
        self,
        user_input: str,
        response: str,
        tool_calls: list[dict[str, Any]],
        tool_observations: list[dict[str, Any]],
    ) -> list[Any]:
        # 把一次 Agent 回答「拼装」成 Ragas 能读懂的消息序列（人类问 -> AI 调工具 -> 工具回 -> AI 答）
        HumanMessage, AIMessage, ToolMessage, ToolCall = self.message_types
        messages: list[Any] = [HumanMessage(content=user_input)]  # 先放用户提问
        ragas_calls = [
            # 把我们的工具调用格式转成 Ragas 的 ToolCall
            ToolCall(
                name=str(item.get("tool_name") or item.get("name") or "unknown"),
                args=dict(item.get("arguments") or item.get("args") or {}),
            )
            for item in tool_calls
            if isinstance(item, dict)
        ]
        if ragas_calls:
            messages.append(AIMessage(content="", tool_calls=ragas_calls))  # 有工具调用则放一条带 tool_calls 的 AI 消息
        for item in tool_observations:
            if isinstance(item, dict):
                # 工具返回结果用 ToolMessage 表示
                messages.append(ToolMessage(content=str(item.get("content") or "")))
        messages.append(AIMessage(content=response))  # 最后放 AI 的最终回答
        return messages

    async def _score_faithfulness(
        self,
        *,
        user_input: str,
        response: str,
        retrieved_contexts: list[str],
    ) -> float:
        # 对「忠实度」指标单独做分段并发评分，再按字数加权求平均
        chunks = _split_faithfulness_response(response)  # 把长答案切段
        semaphore = asyncio.Semaphore(2)  # 最多同时 2 段在跑，避免打爆裁判模型

        async def score_chunk(chunk: str) -> Any:
            # 给单个片段打忠实度分，带超时保护
            async with semaphore:
                return await asyncio.wait_for(
                    self.metrics["faithfulness"].ascore(
                        user_input=user_input,
                        response=chunk,
                        retrieved_contexts=retrieved_contexts,
                    ),
                    timeout=settings.ragas_timeout_seconds,
                )

        results = await asyncio.gather(*(score_chunk(chunk) for chunk in chunks), return_exceptions=True)
        failures = [result for result in results if isinstance(result, BaseException)]  # 收集失败
        if failures:
            first = failures[0]
            raise RagasUnavailableError(
                f"Faithfulness chunk scoring failed ({len(failures)}/{len(chunks)}): "
                f"{type(first).__name__}: {first}"
            ) from first

        # 权重 = 各片段字数（至少 1），最终分数按字数加权平均
        weights = [max(1, len(chunk)) for chunk in chunks]
        return sum(float(result.value) * weight for result, weight in zip(results, weights, strict=True)) / sum(weights)

    async def score(
        self,
        *,
        user_input: str,
        response: str,
        retrieved_contexts: list[str],
        reference: str,
        tool_calls: list[dict[str, Any]],
        tool_observations: list[dict[str, Any]],
        expected_tool_calls: list[dict[str, Any]],
        metric_names: set[str] | None = None,
    ) -> tuple[dict[str, float], dict[str, str]]:
        # 主评分入口：按需计算各指标，返回 (分数字典, 错误字典)
        messages = self._messages(user_input, response, tool_calls, tool_observations)  # 拼 Ragas 消息
        wants = lambda name: metric_names is None or name in metric_names  # 判断是否需要算某指标
        calls: dict[str, Any] = {}  # 收集各指标的异步评分协程
        if wants("context_precision"):
            calls["context_precision"] = self.metrics["context_precision"].ascore(
                user_input=user_input,
                reference=reference,
                retrieved_contexts=retrieved_contexts,
            )
        if wants("context_recall"):
            calls["context_recall"] = self.metrics["context_recall"].ascore(
                user_input=user_input,
                reference=reference,
                retrieved_contexts=retrieved_contexts,
            )
        if wants("faithfulness"):
            # 忠实度走分段并发版本
            calls["faithfulness"] = self._score_faithfulness(
                user_input=user_input,
                response=response,
                retrieved_contexts=retrieved_contexts,
            )
        if wants("answer_relevancy"):
            calls["answer_relevancy"] = self.metrics["answer_relevancy"].ascore(
                user_input=user_input,
                response=response,
            )
        if wants("agent_goal_accuracy"):
            calls["agent_goal_accuracy"] = self.metrics["agent_goal_accuracy"].ascore(
                user_input=messages,
                reference=reference,
            )
        if expected_tool_calls and (wants("tool_call_accuracy") or wants("tool_call_f1")):
            # 工具调用类指标需要「期望的工具调用」作参照
            _, _, _, ToolCall = self.message_types
            reference_calls = [
                ToolCall(
                    name=str(item.get("tool_name") or item.get("name") or "unknown"),
                    args=dict(item.get("arguments") or item.get("args") or {}),
                )
                for item in expected_tool_calls
            ]
            if wants("tool_call_accuracy"):
                calls["tool_call_accuracy"] = self.metrics["tool_call_accuracy"].ascore(
                    user_input=messages,
                    reference_tool_calls=reference_calls,
                )
            if wants("tool_call_f1"):
                calls["tool_call_f1"] = self.metrics["tool_call_f1"].ascore(
                    user_input=messages,
                    reference_tool_calls=reference_calls,
                )

        names = list(calls)  # 实际要算的指标名
        results = await asyncio.gather(
            # 并发执行所有指标评分，单个失败也收集异常而不整体崩溃
            *(
                asyncio.wait_for(calls[name], timeout=settings.ragas_timeout_seconds)
                for name in names
            ),
            return_exceptions=True,
        )
        scores: dict[str, float] = {}  # 分数结果
        errors: dict[str, str] = {}  # 错误结果
        for name, result in zip(names, results, strict=True):
            if isinstance(result, BaseException):
                errors[name] = f"{type(result).__name__}: {result}"  # 该指标失败，记下错误
                continue
            value = result if isinstance(result, (int, float)) else result.value  # 兼容直接数值或带 .value 的对象
            scores[name] = round(float(value), 4)  # 保留 4 位小数
        return scores, errors


def _default_cases(db: Session, repo_id: str, limit: int | None = None) -> list[RagEvalCase]:
    # 在没有显式提供用例时，自动从仓库数据生成评测用例（文档优先，其次 Issue/PR/CI）
    repo_uuid = uuid.UUID(str(repo_id))  # 转成 UUID
    case_limit = limit or settings.ragas_max_cases  # 用例数量上限
    docs = (
        db.query(Document)
        .filter(Document.repo_id == repo_uuid, Document.source_type.in_(RAG_SOURCE_TYPES))  # 只取受支持的来源类型
        .order_by(Document.created_at.desc())  # 最新的在前
        .limit(case_limit)
        .all()
    )
    cases: list[RagEvalCase] = []  # 收集用例
    for index, doc in enumerate(docs, start=1):
        # 按文档来源类型生成对应问句
        if doc.source_type == "issue":
            question = f"What context exists for issue {doc.title}?"
        elif doc.source_type == "pull_request":
            question = f"What changed in pull request {doc.title}?"
        elif doc.source_type == "workflow_run":
            question = f"What does the CI run {doc.title} show?"
        else:
            question = f"What project knowledge is available about {doc.title}?"
        cases.append(
            RagEvalCase(
                id=f"document-{index}",
                question=question,
                reference=doc.content[:2000],  # 标准答案取文档前 2000 字
                expected_source_ids=[str(doc.id), str(doc.source_id)],
                source_type=doc.source_type,
            )
        )
    if cases:
        return cases  # 有文档用例就直接返回

    # 刚连上的仓库可能还没建好可复用文档索引，但已有结构化 Issue/PR/CI 数据。
    # 生产 ChatAgent 会通过 EvidenceStore 预取这些记录，所以默认套题也要覆盖它们。
    issues = (
        db.query(Issue)
        .filter(Issue.repo_id == repo_uuid)
        .order_by(Issue.updated_at.desc(), Issue.created_at.desc())
        .limit(case_limit)
        .all()
    )
    for issue in issues:
        # 把 Issue 的标题/正文/状态/标签拼成标准答案
        reference = "\n".join(
            value
            for value in [
                issue.title,
                issue.body or "",
                f"state: {issue.state}",
                f"labels: {', '.join(issue.labels or [])}",
            ]
            if value
        )
        cases.append(
            RagEvalCase(
                id=f"issue-{issue.number}",
                question=f"What project context is recorded for issue #{issue.number}: {issue.title}?",
                reference=reference[:2000],
                expected_source_ids=[str(issue.id)],
                source_type="issue",
            )
        )
        if len(cases) >= case_limit:
            return cases  # 达到上限就返回

    pull_requests = (
        db.query(PullRequest)
        .filter(PullRequest.repo_id == repo_uuid)
        .order_by(PullRequest.updated_at.desc(), PullRequest.created_at.desc())
        .limit(case_limit - len(cases))  # 补足剩余名额
        .all()
    )
    for pull_request in pull_requests:
        reference = "\n".join(
            value
            for value in [
                pull_request.title,
                pull_request.body or "",
                f"state: {pull_request.state}",
                f"base: {pull_request.base_branch}",
                f"head: {pull_request.head_branch}",
            ]
            if value
        )
        cases.append(
            RagEvalCase(
                id=f"pull-request-{pull_request.number}",
                question=f"What changed in pull request #{pull_request.number}: {pull_request.title}?",
                reference=reference[:2000],
                expected_source_ids=[str(pull_request.id)],
                source_type="pull_request",
            )
        )
        if len(cases) >= case_limit:
            return cases

    workflow_runs = (
        db.query(WorkflowRun)
        .filter(WorkflowRun.repo_id == repo_uuid, WorkflowRun.conclusion == "failure")  # 只取失败的 CI 运行
        .order_by(WorkflowRun.created_at.desc())
        .limit(case_limit - len(cases))
        .all()
    )
    for workflow_run in workflow_runs:
        reference = "\n".join(
            value
            for value in [
                workflow_run.name,
                workflow_run.status,
                workflow_run.conclusion or "",
                (workflow_run.logs_text or "")[:1600],  # 日志只取前 1600 字
            ]
            if value
        )
        cases.append(
            RagEvalCase(
                id=f"workflow-run-{workflow_run.github_run_id}",
                question=f"What does the failed CI run {workflow_run.name} show?",
                reference=reference[:2000],
                expected_source_ids=[str(workflow_run.id)],
                source_type="workflow_run",
            )
        )
    return cases


def _source_hit(result: dict[str, Any], expected_source_ids: set[str]) -> bool:
    # 判断一条检索结果是否命中了「期望来源」（看 id 或 source_id 是否在期望集合里）
    return str(result.get("id")) in expected_source_ids or str(result.get("source_id")) in expected_source_ids


def _dcg(hits: list[int]) -> float:
    # 计算 DCG（折损累计增益）：越靠前的命中权重越高（用 log2(排名+2) 折损）
    return sum(hit / math.log2(index + 2) for index, hit in enumerate(hits))


def _retrieval_metrics(cases: list[RagEvalCase], rows: list[dict[str, Any]], k: int) -> dict[str, float]:
    # 计算检索类指标：recall@k、precision@k、MRR（平均倒数排名）、NDCG
    if not cases:
        return {"recall_at_k": 0.0, "precision_at_k": 0.0, "mrr": 0.0, "ndcg": 0.0}  # 没用例直接全 0
    recall_values: list[float] = []
    precision_values: list[float] = []
    reciprocal_ranks: list[float] = []
    ndcg_values: list[float] = []
    for case, row in zip(cases, rows, strict=True):
        expected = set(case.expected_source_ids)  # 该用例期望命中的来源集合
        results = row["results"][:k]  # 只看 Top-K 个结果
        # 每个结果是否命中（1/0）
        hits = [1 if _source_hit(result, expected) else 0 for result in results]
        recall_values.append(1.0 if not expected or any(hits) else 0.0)  # 召回：期望来源里至少一个被命中即记 1
        precision_values.append(sum(hits) / max(len(results), 1))  # 精确率：命中数 / 结果数
        first_hit = next((index + 1 for index, hit in enumerate(hits) if hit), None)  # 第一个命中的排名
        reciprocal_ranks.append(1.0 / first_hit if first_hit else (1.0 if not expected else 0.0))  # MRR
        ideal = [1] + [0] * max(len(hits) - 1, 0)  # 理想排序：第一个命中，其余不命中
        ndcg_values.append(_dcg(hits) / (_dcg(ideal) or 1.0) if expected else 1.0)  # NDCG = 实际DCG / 理想DCG
    return {
        # 各项取平均并保留 4 位小数
        "recall_at_k": round(sum(recall_values) / len(recall_values), 4),
        "precision_at_k": round(sum(precision_values) / len(precision_values), 4),
        "mrr": round(sum(reciprocal_ranks) / len(reciprocal_ranks), 4),
        "ndcg": round(sum(ndcg_values) / len(ndcg_values), 4),
    }


def _trace_rows(trace: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    # 从 Agent 的评测轨迹里抽出「检索到的上下文文本」和「来源列表」，并去重
    contexts: list[str] = []  # 去重后的上下文文本
    sources: list[dict[str, Any]] = []  # 去重后的来源
    seen_contexts: set[str] = set()  # 已见过的上下文文本
    seen_sources: set[tuple[str, str, str]] = set()  # 已见过的来源（用 id/source_id/title 三元组判重）
    for retrieval in trace.get("retrievals") or []:
        if not isinstance(retrieval, dict):
            continue  # 跳过非法项
        for value in retrieval.get("contexts") or []:
            text = str(value).strip()
            if text and text not in seen_contexts:
                seen_contexts.add(text)
                contexts.append(text)  # 新上下文才收
        for raw_source in retrieval.get("sources") or []:
            if not isinstance(raw_source, dict):
                continue
            source = dict(raw_source)
            key = (
                str(source.get("id") or ""),
                str(source.get("source_id") or ""),
                str(source.get("title") or ""),
            )
            if key not in seen_sources:
                seen_sources.add(key)
                sources.append(source)  # 新来源才收
    return contexts, sources


def _hard_rules(case: RagEvalCase, result: dict[str, Any], sources: list[dict[str, Any]], contexts: list[str]) -> dict[str, Any]:
    # 对单条用例跑「确定性硬规则」门禁：来源命中、工具齐全、回答合规等；全过才算 passed
    expected = set(case.expected_source_ids)  # 期望来源集合
    source_hit = not expected or any(_source_hit(item, expected) for item in sources)  # 期望来源至少一个命中
    actual_tools = [
        # 实际调用过的工具名列表
        str(item.get("tool_name") or "")
        for item in result.get("tool_calls") or []
        if isinstance(item, dict)
    ]
    missing_tools = [name for name in case.required_tools if name not in actual_tools]  # 该调却没调的工具
    tool_errors = [
        # 调用失败（status==error）的工具，列出名字与错误
        {
            "tool_name": item.get("tool_name"),
            "error": item.get("error"),
        }
        for item in result.get("tool_calls") or []
        if isinstance(item, dict) and item.get("status") == "error"
    ]
    answer = str(result.get("answer") or "").strip()  # 最终回答文本
    answer_present = bool(answer)  # 是否有回答
    contexts_present = bool(contexts)  # 是否有检索上下文
    # 必须包含的关键词里，哪些没在回答里出现（忽略大小写）
    missing_phrases = [value for value in case.must_include if value.casefold() not in answer.casefold()]
    # 禁止包含的关键词里，哪些却出现在了回答里
    forbidden_phrases = [value for value in case.must_not_include if value.casefold() in answer.casefold()]
    # 若有正则要求，则回答必须整串匹配该正则
    answer_pattern_matched = case.answer_regex is None or re.fullmatch(case.answer_regex, answer) is not None
    passed = (
        source_hit
        and not missing_tools
        and not tool_errors
        and answer_present
        and contexts_present
        and not missing_phrases
        and not forbidden_phrases
        and answer_pattern_matched
    )  # 全部满足才通过
    return {
        "passed": passed,
        "source_hit": source_hit,
        "answer_present": answer_present,
        "contexts_present": contexts_present,
        "missing_tools": missing_tools,
        "tool_errors": tool_errors,
        "missing_phrases": missing_phrases,
        "forbidden_phrases": forbidden_phrases,
        "answer_pattern_matched": answer_pattern_matched,
        "citation_count": len(result.get("citations") or []),  # 引用条数（诊断用）
    }


def _evaluation_project_context(db: Session, repo_id: uuid.UUID) -> dict[str, Any]:
    """加载与前端发给 ChatAgent 相同的「项目自有上下文」（团队成员 + 项目记忆）。."""

    documents = (
        db.query(Document)
        .filter(
            Document.repo_id == repo_id,
            Document.source_type.in_(("team_member", "memory_note")),  # 只取团队成员与记忆笔记两类
        )
        .order_by(Document.created_at.asc())
        .all()
    )
    team_members: list[dict[str, str]] = []  # 团队成员列表
    project_memory: list[dict[str, str]] = []  # 项目记忆列表
    for document in documents:
        metadata = document.meta or {}  # 文档元数据
        if document.source_type == "team_member":
            team_members.append(
                {
                    "name": str(metadata.get("name") or document.title.replace("团队成员：", "")),  # 兼容带前缀的标题
                    "role": str(metadata.get("role") or ""),
                    "strengths": str(metadata.get("strengths") or ""),
                    "techStack": str(metadata.get("tech_stack") or metadata.get("techStack") or ""),  # 兼容两种命名
                }
            )
        elif document.source_type == "memory_note":
            project_memory.append({"title": document.title, "content": document.content})
    return {"team_members": team_members, "project_memory": project_memory}


async def _run_chatagent_case(
    db: Session,
    repo: Repository,
    case: RagEvalCase,
    *,
    limit: int,
    agent_factory: Callable[[], ChatAgent],
) -> dict[str, Any]:
    # 真正启动一次生产 ChatAgent 来回答评测问题；用数据库 savepoint 隔离，跑完回滚所有运行时写入
    savepoint = db.begin_nested()  # 开一个嵌套事务（可回滚的「沙盒」）
    try:
        # 为这次评测建一条临时会话（状态标记为 evaluation，不污染真实数据）
        conversation = Conversation(
            repo_id=repo.id,
            title=f"[RAGAS Eval] {case.id or case.question[:40]}",
            status="evaluation",
        )
        db.add(conversation)
        db.flush()  # 落库拿到 conversation.id（但还在 savepoint 内）
        EvidenceStore(db).rebuild_repo(repo, conversation)  # 重建该仓库的证据索引
        assembled = ContextAssembler(db).assemble(repo, conversation, case.question)  # 拼装发给 Agent 的上下文
        from app.api.routes.chat import _build_conversation_tools, _skill_runtime_context  # 延迟 import 避免循环依赖

        context: dict[str, Any] = {
            # 把项目上下文、技能运行时、会话上下文等组装进 Agent 的 context
            **_evaluation_project_context(db, repo.id),
            **_skill_runtime_context(),
            "repo_id": str(repo.id),
            "conversation_id": str(conversation.id),
            "server_context": assembled.system_context,
            "server_messages": assembled.history_messages,
            "evidence": assembled.evidence,
            "compression_stats": assembled.compression_stats,
            "source_type": case.source_type,
            "limit": limit,
            "evaluation_mode": True,  # 标记为评测模式（Agent 内部据此做隔离）
        }
        context["tools"] = _build_conversation_tools(repo, conversation, db)  # 注入可用工具
        return await agent_factory().run({"message": case.question}, context)  # 跑 Agent 并返回结果
    finally:
        if savepoint.is_active:
            savepoint.rollback()  # 无论成功失败，回滚本次评测产生的所有写入，保持数据库干净


def _thresholds() -> dict[str, float]:
    # 从配置读出各语义指标的「通过门槛」
    return {
        "context_precision": settings.ragas_context_precision_min,
        "context_recall": settings.ragas_context_recall_min,
        "faithfulness": settings.ragas_faithfulness_min,
        "answer_relevancy": settings.ragas_answer_relevancy_min,
        "agent_goal_accuracy": settings.ragas_agent_goal_accuracy_min,
        "tool_call_accuracy": settings.ragas_tool_call_accuracy_min,
        "tool_call_f1": settings.ragas_tool_call_f1_min,
    }


def _mean(values: list[float]) -> float | None:
    # 求平均值，但跳过非有限值（NaN/inf）；全无效则返回 None
    finite = [value for value in values if math.isfinite(value)]
    return round(sum(finite) / len(finite), 4) if finite else None


def _metric_details(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    # 汇总所有用例的语义指标：算均值、与门槛比较、统计已评分用例数
    thresholds = _thresholds()
    values: dict[str, list[float]] = {
        # 为各指标准备一个空列表
        name: [] for name in (*CORE_RAGAS_METRICS, *AGENT_RAGAS_METRICS)
    }
    for row in rows:
        for name, value in (row.get("ragas") or {}).items():
            if name in values and isinstance(value, (int, float)) and math.isfinite(float(value)):
                values[name].append(float(value))  # 收集有效分数
    details: dict[str, dict[str, Any]] = {}
    for name, metric_values in values.items():
        score = _mean(metric_values)  # 均值
        details[name] = {
            "score": score,
            "threshold": thresholds[name],
            "passed": None if score is None else score >= thresholds[name],  # 是否达标
            "scored_cases": len(metric_values),  # 实际拿到分数的用例数
        }
    return details


def _case_diagnoses(row: dict[str, Any]) -> list[dict[str, str]]:
    # 对单条用例生成「诊断建议」：根据失败类型给出原因 + 修复动作，供前端展示
    scores = row.get("ragas") or {}  # 语义指标分数
    errors = row.get("ragas_errors") or {}  # 语义指标错误
    hard = row.get("hard_rules") or {}  # 硬规则结果
    thresholds = _thresholds()
    diagnoses: list[dict[str, str]] = []  # 诊断列表

    if errors:
        # 裁判模型某些指标算不出来：先别据此改 RAG，重试评分即可
        names = "、".join(errors)
        diagnoses.append(
            {
                "category": "judge_error",
                "severity": "error",
                "title": "Judge 指标计算异常",
                "summary": f"{names} 未得到可信分数；先仅重试评分，不要据此调整 RAG。",
                "action": "重试异常指标；若仍失败，再检查超时、模型结构化输出和答案长度。",
            }
        )
    if not hard.get("source_hit", True) or not hard.get("contexts_present", True):
        # 目标证据没进上下文：检索链问题
        diagnoses.append(
            {
                "category": "retrieval",
                "severity": "failed",
                "title": "目标证据没有进入回答上下文",
                "summary": "预期来源未命中，或生产检索链没有返回可评分上下文。",
                "action": "检查切片、Query 改写、来源过滤、Top-K 和 Rerank。",
            }
        )
    rule_failures = (
        list(hard.get("missing_phrases") or [])
        + list(hard.get("forbidden_phrases") or [])
    )
    if rule_failures or hard.get("answer_pattern_matched") is False:
        # 违反确定性验收规则（漏写/多写/格式不符）
        diagnoses.append(
            {
                "category": "generation",
                "severity": "failed",
                "title": "回答违反确定性验收规则",
                "summary": "回答遗漏关键事实、包含禁用事实，或没有遵守要求的输出格式。",
                "action": "收紧回答指令，并审查 must_include、must_not_include 与格式规则。",
            }
        )

    # 下面按各语义指标是否低于门槛分别给诊断
    precision = scores.get("context_precision")
    recall = scores.get("context_recall")
    faithfulness = scores.get("faithfulness")
    relevancy = scores.get("answer_relevancy")
    goal = scores.get("agent_goal_accuracy")
    precision_low = isinstance(precision, (int, float)) and precision < thresholds["context_precision"]
    recall_low = isinstance(recall, (int, float)) and recall < thresholds["context_recall"]
    if precision_low and not recall_low:
        # 召回达标但精确率低：证据找全了但噪声多
        diagnoses.append(
            {
                "category": "retrieval_noise",
                "severity": "failed",
                "title": "证据找全了，但上下文噪声过多",
                "summary": "Context Recall 达标而 Context Precision 未达标。",
                "action": "比较 Top-K、Rerank 和来源状态特征，减少旧资料或同主题噪声。",
            }
        )
    elif recall_low:
        # 召回不达标：关键证据没召回齐
        diagnoses.append(
            {
                "category": "retrieval_recall",
                "severity": "failed",
                "title": "关键证据召回不完整",
                "summary": "Context Recall 未达到当前评测门槛。",
                "action": "检查切片、Query 改写、多文档召回和来源过滤。",
            }
        )
    if (
        isinstance(faithfulness, (int, float))
        and faithfulness < thresholds["faithfulness"]
    ):
        # 忠实度低：回答编造/推测超出证据
        diagnoses.append(
            {
                "category": "generation_faithfulness",
                "severity": "failed",
                "title": "回答没有忠于检索证据",
                "summary": "回答包含证据无法支持的扩写、推断或错误选择。",
                "action": "缩短答案，要求逐项依据证据回答，并加强冲突来源处理。",
            }
        )
    if isinstance(relevancy, (int, float)) and relevancy < thresholds["answer_relevancy"]:
        # 相关性低：答非所问或过度展开
        diagnoses.append(
            {
                "category": "generation_relevancy",
                "severity": "failed",
                "title": "回答没有直接回应问题",
                "summary": "答案相关性低于当前门槛，可能存在过度展开或遗漏用户追问。",
                "action": "改进问题拆解和回答结构，删除无关背景与下一步建议。",
            }
        )
    if isinstance(goal, (int, float)) and goal < thresholds["agent_goal_accuracy"]:
        # 目标准确率低：没遵守句数/格式/任务约束
        diagnoses.append(
            {
                "category": "agent_goal",
                "severity": "failed",
                "title": "ChatAgent 没有完成题目要求",
                "summary": "回答可能事实相关，但没有遵守句数、格式或任务约束。",
                "action": "让生成链优先执行用户明确约束，再补充必要解释。",
            }
        )
    if not diagnoses:
        # 一切正常：作为回归基线保留
        diagnoses.append(
            {
                "category": "passed",
                "severity": "passed",
                "title": "该用例未发现门禁问题",
                "summary": "硬规则和已得到的语义指标均符合当前门槛。",
                "action": "保留为回归基线。",
            }
        )
    return diagnoses


def _diagnostic_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    # 汇总所有用例的诊断：统计失败/错误用例数，以及各问题类别出现次数
    counts: dict[str, int] = {}  # 各类别计数
    failed_cases = 0  # 失败用例数
    error_cases = 0  # 含错误的用例数
    for row in rows:
        row["diagnoses"] = _case_diagnoses(row)  # 给每条用例生成诊断
        categories = {item["category"] for item in row["diagnoses"] if item["category"] != "passed"}  # 非 passed 的类别
        if categories:
            failed_cases += 1  # 有非 passed 类别即算失败
        if row.get("ragas_errors"):
            error_cases += 1  # 有语义错误即算错误用例
        for category in categories:
            counts[category] = counts.get(category, 0) + 1  # 类别计数 +1
    return {
        "failed_cases": failed_cases,
        "error_cases": error_cases,
        "category_counts": counts,
    }


def _finalize_result(
    *,
    rows: list[dict[str, Any]],
    retrieval: dict[str, float],
    performance: dict[str, Any],
    judge_requested: bool,
    scorer: SemanticScorer | None,
    judge_error: str | None,
    repo_id: str,
    limit: int,
    evaluation_set: dict[str, Any] | None,
    rescore_of: str | None = None,
) -> dict[str, Any]:
    # 把逐题结果、检索指标、性能指标汇总成最终评测报告（含状态判定与诊断）
    metric_rows = _metric_details(rows)  # 各语义指标汇总
    hard_passed_cases = sum(1 for row in rows if (row.get("hard_rules") or {}).get("passed"))  # 硬规则通过数
    hard_pass_rate = round(hard_passed_cases / len(rows), 4) if rows else 0.0  # 硬规则通过率
    case_metric_errors = sum(bool(row.get("ragas_errors")) for row in rows)  # 含语义错误的用例数
    required_scored = all(metric_rows[name]["score"] is not None for name in REQUIRED_RAGAS_METRICS)  # 必过指标是否都有分
    ragas_available = bool(judge_requested and scorer is not None and required_scored)  # Judge 是否可用且必过指标已算
    evaluation_complete = bool(ragas_available and case_metric_errors == 0)  # 评测是否完整（无错误）
    gated_names = list(REQUIRED_RAGAS_METRICS)  # 门禁指标：必过指标
    gated_names.extend(
        # 若实际跑了工具调用指标，也纳入门禁
        name
        for name in ("tool_call_accuracy", "tool_call_f1")
        if metric_rows[name]["scored_cases"] > 0
    )
    semantic_failed = any(metric_rows[name]["passed"] is False for name in gated_names)  # 是否有门禁指标未达标
    ragas_passed = evaluation_complete and not semantic_failed  # Ragas 维度是否通过
    hard_passed = bool(rows) and hard_passed_cases == len(rows)  # 硬规则是否全过
    # 综合判定整体状态与质量状态
    if not rows:
        quality_status = "not_evaluated"
        status = "no_cases"
    elif not hard_passed or semantic_failed:
        quality_status = "failed"
        status = "error" if judge_requested and not evaluation_complete else "failed"
    elif not judge_requested:
        quality_status = "partial"
        status = "partial"
    elif not evaluation_complete:
        quality_status = "incomplete"
        status = "error"
    else:
        quality_status = "passed"
        status = "passed"

    summary_metrics = {
        # 只放有分数的指标均值
        name: value["score"]
        for name, value in metric_rows.items()
        if value["score"] is not None
    }
    summary_metrics.update(
        {
            # 额外补硬规则通过与检索指标
            "hard_gate_pass_rate": hard_pass_rate,
            "retrieval_recall_at_k": retrieval.get("recall_at_k", 0.0),
            "retrieval_precision_at_k": retrieval.get("precision_at_k", 0.0),
        }
    )
    diagnostics = _diagnostic_summary(rows)  # 诊断汇总
    return {
        "kind": "chatagent_ragas",
        "status": status,
        "execution_status": "completed",
        "quality_status": quality_status,
        "evaluation_complete": evaluation_complete,
        "passed": status == "passed",
        "run_mode": "rescore" if rescore_of else "full",  # 是否重评分模式
        "rescore_of": rescore_of,
        "target": {
            "name": "ChatAgent",
            "repo_id": str(repo_id),
            "execution": "stored_evidence_rescore" if rescore_of else "production_agent_with_isolated_conversation",  # 执行方式
            "evaluation_mode": True,
        },
        "evaluation_set": evaluation_set or {
            # 默认评测集信息（自动冒烟）
            "id": "auto-document-smoke",
            "name": "auto-document-smoke",
            "label": "自动文档冒烟测试",
            "source": "auto_smoke",
            "sha256": None,
            "case_count": len(rows),
        },
        "pipeline": {"top_k": limit},
        **summary_metrics,
        "summary_metrics": summary_metrics,
        "retrieval": retrieval,
        "performance": performance,
        "hard_gate": {
            "passed": hard_passed,
            "pass_rate": hard_pass_rate,
            "passed_cases": hard_passed_cases,
            "total_cases": len(rows),
        },
        "ragas": {
            "requested": judge_requested,
            "available": ragas_available,
            "complete": evaluation_complete,
            "passed": ragas_passed if evaluation_complete else None,
            "version": scorer.version if scorer is not None else None,
            "judge_model": scorer.judge_model if scorer is not None else None,
            "error": judge_error or (
                f"{case_metric_errors} evaluation case(s) contain Ragas metric errors"
                if case_metric_errors
                else None
            ),
            "case_errors": case_metric_errors,
            "metrics": metric_rows,
        },
        "diagnostics": diagnostics,
        "cases": rows,  # 逐题现场（含各题分数与硬规则）
    }


async def run_rag_quality_eval(
    db: Session,
    repo_id: str,
    *,
    cases: list[RagEvalCase] | None = None,
    limit: int = 5,
    run_judge: bool | None = None,
    scorer: SemanticScorer | None = None,
    agent_factory: Callable[[], ChatAgent] | None = None,
    progress_callback: ProgressCallback | None = None,
    evaluation_set: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """用 Ragas 与确定性门禁评测生产 ChatAgent 链路。."""

    repo = db.get(Repository, uuid.UUID(str(repo_id)))  # 取仓库
    if repo is None:
        raise ValueError(f"Repository {repo_id} was not found")  # 不存在则报错
    evaluation_cases = cases if cases is not None else _default_cases(db, repo_id)  # 用例：显式给或用默认生成
    judge_requested = settings.ragas_enabled if run_judge is None else run_judge  # 是否要调裁判模型
    total_cases = len(evaluation_cases)
    await _emit_progress(
        progress_callback,
        stage="preparing",
        message=f"已准备 {total_cases} 条评测用例，正在初始化 Judge。",
        completed_cases=0,
        total_cases=total_cases,
    )
    judge_error: str | None = None  # 裁判模型不可用时的错误
    if judge_requested and scorer is None:
        try:
            scorer = RagasCollectionsScorer()  # 自动构造评分器
        except RagasUnavailableError as exc:
            judge_error = str(exc)  # 构造失败则记下错误（后面按 skipped 处理）

    rows: list[dict[str, Any]] = []  # 逐题结果
    retrieval_rows: list[dict[str, Any]] = []  # 检索行（用于算检索指标）
    started = time.perf_counter()  # 总计时起点
    for index, case in enumerate(evaluation_cases, start=1):
        await _emit_progress(
            progress_callback,
            stage="chatagent",
            message=f"正在运行第 {index}/{total_cases} 条真实 ChatAgent 用例。",
            current_case=index,
            case_id=case.id or f"case-{index}",
            completed_cases=index - 1,
            total_cases=total_cases,
        )
        item_started = time.perf_counter()  # 单题计时起点
        # 真正跑一次生产 ChatAgent
        result = await _run_chatagent_case(
            db,
            repo,
            case,
            limit=limit,
            agent_factory=agent_factory or ChatAgent,
        )
        trace = result.get("evaluation_trace") if isinstance(result.get("evaluation_trace"), dict) else {}  # 评测轨迹
        contexts, sources = _trace_rows(trace)  # 抽上下文与来源
        hard_rules = _hard_rules(case, result, sources, contexts)  # 跑硬规则
        semantic_scores: dict[str, float] | None = None  # 语义指标分数
        semantic_errors: dict[str, str] = {}  # 语义指标错误
        skipped_reason: str | None = None  # 跳过裁判评分的原因
        if not judge_requested:
            skipped_reason = "judge_disabled"  # 配置关闭
        elif scorer is None:
            skipped_reason = "judge_unavailable"  # 裁判不可用
        elif not contexts:
            skipped_reason = "no_retrieved_contexts"  # 没有检索上下文可评
        else:
            await _emit_progress(
                progress_callback,
                stage="ragas",
                message=f"ChatAgent 已完成第 {index}/{total_cases} 条，正在计算 RAGAS 指标。",
                current_case=index,
                case_id=case.id or f"case-{index}",
                completed_cases=index - 1,
                total_cases=total_cases,
            )
            # 调评分器算语义指标
            semantic_scores, semantic_errors = await scorer.score(
                user_input=case.question,
                response=str(result.get("answer") or ""),
                retrieved_contexts=contexts,
                reference=case.reference,
                tool_calls=[dict(item) for item in result.get("tool_calls") or [] if isinstance(item, dict)],
                tool_observations=[
                    dict(item)
                    for item in trace.get("tool_observations") or []
                    if isinstance(item, dict)
                ],
                expected_tool_calls=case.expected_tool_calls,
            )

        latency_ms = round((time.perf_counter() - item_started) * 1000, 2)  # 单题耗时(毫秒)
        retrieval_rows.append({"results": sources})  # 记录该用例来源用于检索指标
        rows.append(
            {
                # 把该题所有现场信息存进 rows，供后续汇总/诊断/重评分使用
                "id": case.id or f"case-{index}",
                "question": case.question,
                "reference": case.reference,
                "expected_source_ids": case.expected_source_ids,
                "source_file": case.source_file,
                "kind": case.kind,
                "must_include": case.must_include,
                "must_not_include": case.must_not_include,
                "answer_regex": case.answer_regex,
                "required_tools": case.required_tools,
                "expected_tool_calls": case.expected_tool_calls,
                "response": result.get("answer"),
                "route": result.get("route"),
                "citations": result.get("citations") or [],
                "tool_calls": result.get("tool_calls") or [],
                "tool_observations": trace.get("tool_observations") or [],
                "completion": result.get("completion") or {},
                "retrieved_contexts": contexts,
                "retrieved_sources": sources,
                "retrieval_trace": trace.get("retrievals") or [],
                "hard_rules": hard_rules,
                "ragas": semantic_scores,
                "ragas_errors": semantic_errors,
                "ragas_skipped_reason": skipped_reason,
                "latency_ms": latency_ms,
            }
        )
        await _emit_progress(
            progress_callback,
            stage="case_complete",
            message=f"第 {index}/{total_cases} 条评测已完成。",
            current_case=index,
            case_id=case.id or f"case-{index}",
            completed_cases=index,
            total_cases=total_cases,
        )

    await _emit_progress(
        progress_callback,
        stage="finalizing",
        message="正在汇总指标并生成最终判定。",
        completed_cases=total_cases,
        total_cases=total_cases,
    )
    retrieval = _retrieval_metrics(evaluation_cases, retrieval_rows, limit)  # 算检索指标（用 limit 当 k）
    latencies = [float(row["latency_ms"]) for row in rows]  # 各题耗时
    performance = {
        # 性能统计：总/平均/最大耗时
        "cases": len(rows),
        "total_latency_ms": round((time.perf_counter() - started) * 1000, 2),
        "avg_latency_ms": round(sum(latencies) / len(latencies), 2) if latencies else 0.0,
        "max_latency_ms": max(latencies) if latencies else 0.0,
    }
    return _finalize_result(
        rows=rows,
        retrieval=retrieval,
        performance=performance,
        judge_requested=judge_requested,
        scorer=scorer,
        judge_error=judge_error,
        repo_id=str(repo.id),
        limit=limit,
        evaluation_set=evaluation_set,
    )


async def rescore_rag_result(
    result: dict[str, Any],
    *,
    eval_id: str,
    scorer: SemanticScorer | None = None,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    """不重跑 ChatAgent，只重试缺失或失败的 Judge 指标。."""

    rows = copy.deepcopy(result.get("cases") or [])  # 深拷贝原逐题现场，避免污染
    if not rows:
        raise ValueError("原评测没有可用于重评分的逐题现场")  # 没现场则无法重评分
    retry_by_case: list[set[str]] = []  # 每条用例需要重试的指标集合
    for row in rows:
        scores = row.get("ragas") or {}
        errors = row.get("ragas_errors") or {}
        retry_names = {name for name in errors if name in (*CORE_RAGAS_METRICS, *AGENT_RAGAS_METRICS)}  # 出错的指标
        retry_names.update(name for name in REQUIRED_RAGAS_METRICS if name not in scores)  # 必过但没分数的
        expected_calls = row.get("expected_tool_calls") or []
        if expected_calls:
            retry_names.update(
                name for name in ("tool_call_accuracy", "tool_call_f1") if name not in scores
            )  # 有期望工具调用但没算的也补
        retry_by_case.append(retry_names)
    retry_count = sum(bool(names) for names in retry_by_case)  # 需要重试的用例数
    if retry_count == 0:
        raise ValueError("原评测没有失败或缺失的 Judge 指标")  # 没啥可重试则报错

    judge_error: str | None = None
    if scorer is None:
        try:
            scorer = RagasCollectionsScorer()
        except RagasUnavailableError as exc:
            judge_error = str(exc)  # 评分器不可用则记下错误
    started = time.perf_counter()
    completed = 0  # 已完成重试数
    for index, (row, retry_names) in enumerate(zip(rows, retry_by_case, strict=True), start=1):
        if not retry_names:
            continue  # 该题无需重试
        await _emit_progress(
            progress_callback,
            stage="rescore",
            message=f"正在重试第 {completed + 1}/{retry_count} 条异常评分。",
            current_case=index,
            case_id=str(row.get("id") or f"case-{index}"),
            completed_cases=completed,
            total_cases=retry_count,
        )
        existing_errors = dict(row.get("ragas_errors") or {})
        if scorer is None:
            # 评分器不可用：把待重试指标都标记为不可用错误
            for name in retry_names:
                existing_errors[name] = f"RagasUnavailableError: {judge_error or 'Judge unavailable'}"
            row["ragas_errors"] = existing_errors
            completed += 1
            continue
        contexts = [str(value) for value in row.get("retrieved_contexts") or [] if str(value).strip()]
        if not contexts:
            # 没有检索上下文：这些指标无法算，标记错误
            for name in retry_names:
                existing_errors[name] = "ValueError: no retrieved contexts"
            row["ragas_errors"] = existing_errors
            completed += 1
            continue
        # 真正重试评分（只传需要重试的指标名，省时）
        scores, errors = await scorer.score(
            user_input=str(row.get("question") or ""),
            response=str(row.get("response") or ""),
            retrieved_contexts=contexts,
            reference=str(row.get("reference") or ""),
            tool_calls=[dict(item) for item in row.get("tool_calls") or [] if isinstance(item, dict)],
            tool_observations=[
                dict(item) for item in row.get("tool_observations") or [] if isinstance(item, dict)
            ],
            expected_tool_calls=[
                dict(item) for item in row.get("expected_tool_calls") or [] if isinstance(item, dict)
            ],
            metric_names=retry_names,
        )
        merged_scores = dict(row.get("ragas") or {})
        merged_scores.update(scores)  # 把新分数合并进原分数
        for name in retry_names:
            existing_errors.pop(name, None)  # 重试成功的指标去掉旧错误
        existing_errors.update(errors)  # 加上本次仍失败的错误
        row["ragas"] = merged_scores
        row["ragas_errors"] = existing_errors
        row["ragas_skipped_reason"] = None  # 清掉跳过原因
        completed += 1
        await _emit_progress(
            progress_callback,
            stage="case_complete",
            message=f"第 {completed}/{retry_count} 条异常评分已重试。",
            current_case=index,
            case_id=str(row.get("id") or f"case-{index}"),
            completed_cases=completed,
            total_cases=retry_count,
        )

    performance = dict(result.get("performance") or {})
    performance.update(
        {
            # 在性能里标注这是重评分（Agent 复用了，不算新跑）
            "agent_reused": True,
            "rescore_cases": retry_count,
            "rescore_latency_ms": round((time.perf_counter() - started) * 1000, 2),
        }
    )
    top_k = int((result.get("pipeline") or {}).get("top_k") or 5)  # 取出原 top_k
    return _finalize_result(
        rows=rows,
        retrieval=dict(result.get("retrieval") or {}),
        performance=performance,
        judge_requested=True,
        scorer=scorer,
        judge_error=judge_error,
        repo_id=str((result.get("target") or {}).get("repo_id") or ""),
        limit=top_k,
        evaluation_set=dict(result.get("evaluation_set") or {}),
        rescore_of=eval_id,  # 标记这是哪次评测的重评分
    )
