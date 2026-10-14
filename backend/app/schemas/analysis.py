"""
analysis.py —— Issue / PR / CI 三类「智能分析」结果的数据契约（Schema 层）

本文件定义后端在做 AI 分析时，前端与后端之间传递的数据结构，服务于三个分析接口：
1) Issue 分析（IssueAnalysisOutput）：把一条 Issue 自动分类、定级、给出处理结论与建议；
2) PR 评审（PRReviewOutput）：对一次 Pull Request 自动做代码评审，给出合入建议与风险点；
3) CI 调试（CIDebugOutput）：对一次失败的 CI 流水线做根因分析；
4) 安全评估（SafetyOutput）：判断某个待执行的动作是否需要人工确认。

每个类都是 Pydantic 的 BaseModel：可理解为「填空题模板」——字段名是格子，类型决定格子
接受什么内容，等号后是默认值。FastAPI 用它们自动校验入参、生成接口文档，并把数据转成
规整 JSON 返回前端。Literal[...] 表示字段只能取其中固定的几个字符串（像下拉选项）。."""

from typing import Literal  # Literal：把字段的取值范围限定为几个写死的字符串常量（等价于下拉枚举）

from pydantic import BaseModel, Field  # BaseModel：所有数据模型的基类；Field：给字段加约束（如取值区间）与说明


class DuplicateCandidate(BaseModel):
    """DuplicateCandidate."""
    # 疑似重复 Issue 的候选：告诉用户「这条 Issue 可能和已有的某条重复」
    issue_id: str  # 候选重复 Issue 的 ID（字符串，必填）
    title: str  # 候选重复 Issue 的标题（必填）
    score: float = Field(ge=0, le=1)  # 重复度评分：0~1 之间的小数（ge=0 不小于0，le=1 不大于1）


class IssueEvidence(BaseModel):
    """IssueEvidence."""
    # 分析依据：AI 给出结论时引用了哪些证据（来源），便于追溯与可信度判断
    source_type: str  # 证据来源类型（如 issue、pr、doc 等），必填
    title: str  # 证据标题，必填
    snippet: str  # 证据摘要/片段文本，必填
    reference: str | None = None  # 证据原文链接或引用位置；|None 表示「可空」，默认 None（没有）
    confidence: float | None = Field(default=None, ge=0, le=1)  # 该证据可信度 0~1；默认 None=未给出


class IssueDrafts(BaseModel):
    """IssueDrafts."""
    # Issue 处理草稿：AI 可直接生成的两段现成文案，减少人工填写成本
    clarification_comment: str | None = None  # 「需要澄清」时自动生成的评论文案；可空
    task_breakdown: str | None = None  # 「需要拆分」时自动生成的任务拆分文案；可空


class IssueAnalysisOutput(BaseModel):
    """IssueAnalysisOutput."""
    # Issue 分析的最终输出结构：分类、定级、结论、证据、行动项等一整套结果
    summary: str  # 一句话总结这条 Issue，必填
    conclusion: Literal["进入开发", "等待澄清", "关闭", "拆分", "合并到已有 Issue"]  # 处理结论，只能取这 5 个之一
    conclusion_reason: str  # 得出该结论的理由，必填
    category: Literal["Bug", "Feature", "Question", "Documentation", "Refactor", "Test", "Ops"]  # Issue 类别，限 7 选 1
    priority: Literal["P0", "P1", "P2", "P3"]  # 优先级，P0 最高，必填
    complexity: Literal["S", "M", "L", "XL"]  # 工作量复杂度，必填
    suggested_owner: str  # 建议指派人（用户名），必填
    owner_reason: str  # 为什么建议指派给 TA，必填
    # 为什么：list 是「可空可变的容器」，用 default_factory=list 让每个实例拿到独立的新空列表，
    # 避免多个对象共享同一份列表（否则改一个会影响另一个）
    duplicate_candidates: list[DuplicateCandidate] = Field(default_factory=list)  # 重复候选列表；默认空列表
    evidence: list[IssueEvidence] = Field(default_factory=list)  # 证据列表，默认空
    checklist: list[str] = Field(default_factory=list)  # 处理前检查清单（字符串列表），默认空
    action_items: list[str] = Field(default_factory=list)  # 后续行动项（字符串列表），默认空
    drafts: IssueDrafts = Field(default_factory=IssueDrafts)  # 处理草稿对象；默认用空 IssueDrafts 构造
    confidence: float = Field(ge=0, le=1)  # 本次分析整体置信度 0~1，必填


class PRReviewFinding(BaseModel):
    """PRReviewFinding."""
    # 单条 PR 评审发现：代码里某个具体问题的描述与处置建议
    severity: Literal["P1", "P2", "P3"]  # 严重程度，P1 最严重，必填
    title: str  # 该发现标题，必填
    evidence: str  # 支撑该发现的具体代码/现象，必填
    required_action: str  # 建议的修复动作，必填
    blocking: bool = False  # 是否阻塞合入；bool=布尔值（True/False），默认不阻塞


class PRReviewOutput(BaseModel):
    """PRReviewOutput."""
    # PR 评审的最终输出：总结、合入建议、各类风险与清单
    summary: str  # 评审总结，必填
    plan: list[str] = []  # 预设计划列表，默认空列表（写法等价 default_factory=list，但这里是直接给空列表常量）
    executed_steps: list[str] = []  # 已执行步骤列表，默认空
    merge_recommendation: Literal["建议合入", "修改后合入", "暂缓", "拒绝"] = "暂缓"  # 合入建议，默认「暂缓」
    recommendation_reason: str = ""  # 给出该建议的理由，默认空字符串
    review_findings: list[PRReviewFinding] = Field(default_factory=list)  # 具体发现列表，默认空
    key_changes: list[str] = []  # 本次 PR 关键改动点，默认空
    risk_points: list[str] = []  # 风险点列表，默认空
    blocking_issues: list[str] = []  # 阻塞性问题列表，默认空
    review_checklist: list[str] = []  # 评审核对清单，默认空
    test_suggestions: list[str] = []  # 测试建议列表，默认空
    files_need_attention: list[str] = []  # 需要重点关注的文件路径列表，默认空
    review_comments: list[str] = []  # 评审评论列表，默认空
    confidence: float = Field(ge=0, le=1)  # 本次评审整体置信度 0~1，必填


class CIDebugOutput(BaseModel):
    """CIDebugOutput."""
    # CI 调试输出：对一次失败流水线的根因分析与修复步骤
    failure_summary: str  # 失败现象总结，必填
    failure_type: Literal["test", "build", "lint", "dependency", "permission", "environment", "unknown"]  # 失败类型，限 7 选 1
    plan: list[str] = []  # 调试计划，默认空
    executed_steps: list[str] = []  # 已执行步骤，默认空
    first_error: str | None = None  # 首个报错信息；可空（没抓到就 None）
    root_cause: str = ""  # 推断出的根因，默认空
    possible_causes: list[str] = []  # 可能原因列表，默认空
    fix_steps: list[str] = []  # 修复步骤列表，默认空
    debug_steps: list[str] = []  # 调试过程步骤，默认空
    related_files: list[str] = []  # 相关文件列表，默认空
    is_merge_blocking: bool = True  # 是否阻塞合并；默认 True（失败通常要挡住合入）
    blocking_reason: str = ""  # 阻塞原因，默认空
    confidence: float = Field(ge=0, le=1)  # 本次诊断置信度 0~1，必填


class SafetyOutput(BaseModel):
    """SafetyOutput."""
    # 安全评估输出：判断某个动作是否需要人工确认后再执行
    requires_confirmation: bool  # 是否需要人工确认；必填布尔
    risk_level: Literal["low", "medium", "high"]  # 风险等级，限 3 选 1，必填
    reason: str  # 给出风险等级的理由，必填
    draft: str | None = None  # 若需要确认，附上的动作草稿；可空
