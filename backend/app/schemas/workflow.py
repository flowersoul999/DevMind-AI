"""
workflow.py —— 多智能体工作流（Workflow）接口的数据契约（Schema 层）

本文件服务于「规划并执行多 Agent 协作任务」接口，描述一个工作流从「规格定义」到「最终报告」的全结构：
- WorkflowClaim / WorkflowTask / WorkflowSpec：工作流的目标、任务清单、每个任务需要的权限声明；
- WorkflowTaskResult / WorkflowFinding / WorkflowObservation：单任务结果、审查发现、观察者 Agent 的总评；
- WorkflowReport：把上面全部聚合成一份最终报告。

from __future__ import annotations 让注解里的 "|" 和前向引用在老版本 Python 也安全；Literal 把状态/严重程度锁死成固定档位。."""

from __future__ import annotations  # 为什么：允许用更现代的注解写法（如 str | None），并支持类型前向引用

import uuid  # uuid：生成唯一 ID
from datetime import datetime, timezone  # datetime：时间；timezone：时区（这里用 UTC 统一时间）
from typing import Any, Literal  # Any：任意类型；Literal：限定固定取值

from pydantic import BaseModel, Field  # BaseModel：基类；Field：字段约束/默认值工厂


# 工作流中单个任务的状态：待执行/执行中/成功/跳过/出错
WorkflowTaskStatus = Literal["pending", "running", "success", "skipped", "error"]
# 审查发现的严重程度：信息/警告/阻塞
WorkflowFindingSeverity = Literal["info", "warning", "blocker"]


def _workflow_id() -> str:
    # 生成一个工作流 ID，形如 "workflow_<12位随机十六进制>"，作为默认 workflow_id
    return f"workflow_{uuid.uuid4().hex[:12]}"


class WorkflowClaim(BaseModel):
    """WorkflowClaim."""
    # 任务执行前对「权限/来源」的声明：要不要写仓库、为什么，避免 Agent 越权乱改
    target_type: str  # 操作目标类型（如 issue/pr/file），必填
    target_id: str | None = None  # 具体操作目标 ID；可空
    allowed_sources: list[str] = Field(default_factory=list)  # 允许的数据来源列表，默认空
    write_intent: bool = False  # 是否有写入意图（要改东西）；默认否
    reason: str = ""  # 为何需要该权限，默认空


class WorkflowTask(BaseModel):
    """WorkflowTask."""
    # 工作流里的一个具体任务
    id: str  # 任务 ID，必填
    agent_name: str  # 负责该任务的 Agent 名，必填
    task_type: str  # 任务类型，必填
    objective: str  # 任务目标描述，必填
    dependencies: list[str] = Field(default_factory=list)  # 依赖的其他任务 ID 列表，默认空
    claim: WorkflowClaim  # 该任务的权限声明，必填
    input_hint: dict[str, Any] = Field(default_factory=dict)  # 给 Agent 的输入提示，默认空字典
    critical: bool = False  # 是否关键任务（失败则整体失败）；默认否


class WorkflowSpec(BaseModel):
    """WorkflowSpec."""
    # 整个工作流的「规格说明书」：目标、触发消息、任务清单、并发度等
    workflow_id: str = Field(default_factory=_workflow_id)  # 工作流 ID；默认用上面的函数生成
    goal: str  # 整体目标，必填
    trigger_message: str  # 触发本次工作流的消息，必填
    acceptance_criteria: list[str] = Field(default_factory=list)  # 验收标准列表，默认空
    tasks: list[WorkflowTask] = Field(default_factory=list)  # 任务清单，默认空
    max_parallel_tasks: int = Field(default=3, ge=1, le=8)  # 最大并行任务数；默认 3，限定 1~8
    requires_human_confirmation: bool = False  # 是否需要人工确认才能执行；默认否
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))  # 创建时间；默认当前 UTC 时间


class WorkflowTaskResult(BaseModel):
    """WorkflowTaskResult."""
    # 单个任务的执行结果
    task_id: str  # 任务 ID，必填
    agent_name: str  # 执行 Agent，必填
    task_type: str  # 任务类型，必填
    status: WorkflowTaskStatus  # 任务状态（用上面定义的档位），必填
    summary: str = ""  # 结果摘要，默认空
    output: dict[str, Any] = Field(default_factory=dict)  # 结构化输出，默认空字典
    evidence_count: int = 0  # 产出证据条数，默认 0
    confidence: float | None = Field(default=None, ge=0, le=1)  # 置信度 0~1；可空
    citations: list[dict[str, Any]] = Field(default_factory=list)  # 引用列表，默认空
    error: str | None = None  # 错误信息；可空
    started_at: datetime | None = None  # 开始时间；可空
    completed_at: datetime | None = None  # 完成时间；可空


class WorkflowFinding(BaseModel):
    """WorkflowFinding."""
    # 观察者 Agent 发现的一个问题/风险点
    finding_type: str  # 发现类型，必填
    severity: WorkflowFindingSeverity  # 严重程度（用上面档位），必填
    message: str  # 发现描述，必填
    task_ids: list[str] = Field(default_factory=list)  # 关联的任务 ID 列表，默认空
    recommendation: str = ""  # 改进建议，默认空


class WorkflowObservation(BaseModel):
    """WorkflowObservation."""
    # 观察者 Agent 对整个工作流的总评
    observer_agent: str = "observer_agent"  # 观察者 Agent 名，默认 "observer_agent"
    findings: list[WorkflowFinding] = Field(default_factory=list)  # 发现列表，默认空
    overall_confidence: float = Field(default=0.0, ge=0, le=1)  # 整体置信度 0~1，默认 0.0
    human_review_required: bool = False  # 是否需要人工复核；默认否
    summary: str = ""  # 总评摘要，默认空


class WorkflowReport(BaseModel):
    """WorkflowReport."""
    # 工作流最终报告：把规格、各任务结果、总评、最终答案聚合成一份交付物
    spec: WorkflowSpec  # 工作流规格，必填
    task_results: list[WorkflowTaskResult]  # 各任务结果列表，必填
    observation: WorkflowObservation  # 观察者总评，必填
    final_answer: str  # 最终给用户的答案，必填
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)  # 工具调用记录，默认空
    citations: list[dict[str, Any]] = Field(default_factory=list)  # 引用列表，默认空
    metrics: dict[str, Any] = Field(default_factory=dict)  # 各类指标统计，默认空
    agent_events: list[dict[str, Any]] = Field(default_factory=list)  # Agent 事件流，默认空
