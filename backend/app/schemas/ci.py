"""
ci.py —— CI / 工作流运行（WorkflowRun）与 CI 分析结果的数据契约（Schema 层）

本文件服务于「查看一次 CI 运行」和「对失败 CI 做智能分析」两个接口：
- WorkflowRunResponse：把 GitHub Actions 等一次流水线运行的基本信息返回前端（含 jobs、日志）；
- CIAnalysisResponse：把一次 CI 失败的根因分析结果（CIDebugOutput）打包返回。

model_config = {"from_attributes": True} 告诉 Pydantic：可以直接用数据库模型对象的「属性」
来填充本模型（而不是只能吃字典），方便把 ORM 对象转成接口响应。."""

from datetime import datetime  # datetime：日期+时间类型，用于记录运行的时间点
from uuid import UUID  # UUID：对象的全局唯一 ID

from pydantic import BaseModel  # BaseModel：模型基类

from app.schemas.analysis import CIDebugOutput  # 复用 analysis.py 里定义好的 CI 调试输出结构


class WorkflowRunResponse(BaseModel):
    """WorkflowRunResponse."""
    # 一次 CI / 工作流运行的响应结构
    id: UUID  # 运行记录 ID，必填
    repo_id: UUID  # 所属仓库 ID，必填
    name: str  # 运行名称（如 "backend-test"），必填
    status: str  # 运行状态（queued/in_progress/completed 等），必填
    conclusion: str | None = None  # 最终结论（success/failure/cancelled）；进行中则为 None
    html_url: str | None = None  # 在 GitHub 上查看该运行的网页链接；可空
    jobs: list[dict] = []  # 该运行包含的各个 job 信息列表，默认空
    logs_text: str | None = None  # 运行日志文本；可能很大，可空
    created_at: datetime | None = None  # 创建时间；可空
    updated_at: datetime | None = None  # 更新时间；可空

    model_config = {"from_attributes": True}  # 为什么：允许从 ORM 对象（带属性）直接构造，便于数据库行→响应


class CIAnalysisResponse(BaseModel):
    """CIAnalysisResponse."""
    # CI 分析结果的响应结构：把分析 ID 和具体分析内容一起返回
    analysis_id: UUID | str  # 本次分析记录 ID（UUID 或字符串），必填
    result: CIDebugOutput  # 具体分析内容，复用 analysis.py 的 CIDebugOutput，必填
