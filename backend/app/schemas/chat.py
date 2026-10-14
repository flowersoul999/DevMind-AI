"""
chat.py —— 对话（Chat）接口的数据契约（Schema 层）

本文件定义前端与后端「智能对话」相关接口的请求体与响应体：
- ChatRequest / ChatPlanExecuteRequest：用户发一条消息时携带的数据；
- ChatResponse / ChatHistoryMessage：后端返回的一次回答 / 一条历史消息；
- ToolCallTrace：agent 调用工具（如搜索代码、查数据库）的调用轨迹，用于前端展示「AI 在背后干了啥」；
- PromptRecommendationRequest / Response：给用户的提示词（问题）推荐。

这些模型让前后端对「发什么、回什么」有统一约定；UUID 是全局唯一 ID，dict 是自由结构（键值对）。."""

from uuid import UUID  # UUID：通用唯一标识符，后端用它做各类对象的 id

from pydantic import BaseModel, Field  # BaseModel：模型基类；Field：字段约束


class ChatRequest(BaseModel):
    """ChatRequest."""
    # 发起一次对话请求时前端要传的数据
    repo_id: UUID  # 仓库 ID（必填）：告诉后端这次问答针对哪个仓库
    conversation_id: UUID | None = None  # 会话 ID；|None 表示可空，首次开聊可省略（默认 None）
    message: str  # 用户实际发送的消息文本，必填
    context: dict = Field(default_factory=dict)  # 附加上下文（任意键值对），默认空字典；用工厂函数保证每实例独立


class ChatPlanExecuteRequest(ChatRequest):
    """ChatPlanExecuteRequest."""
    # 在普通对话基础上，额外带上「工作流计划」去执行：继承 ChatRequest 的全部字段
    workflow_spec: dict  # 工作流规格（任意结构），必填——描述要按什么计划去执行任务


class ToolCallTrace(BaseModel):
    """ToolCallTrace."""
    # 工具调用轨迹：记录 agent 调用某个工具的一次完整过程，前端可用来展示调用链
    tool_name: str  # 被调用的工具名，必填
    arguments: dict  # 调用该工具时传入的参数（键值对），必填
    status: str  # 调用状态（如 success/error/skipped），必填
    error: str | None = None  # 报错信息；成功则为 None
    error_kind: str | None = None  # 错误类型归类；可空
    retryable: bool | None = None  # 该错误是否可重试；可空（未判定则为 None）
    attempts: int | None = None  # 已尝试次数；int=整数，可空
    tool_use_id: str | None = None  # 本次工具调用的唯一标识；可空
    skill_name: str | None = None  # 若由某个「技能(skill)」触发，记录技能名；可空
    skill_title: str | None = None  # 技能标题；可空
    skill_version: str | None = None  # 技能版本号；可空
    skill_steps: list[str] = Field(default_factory=list)  # 技能的步骤列表，默认空
    skill_activation_mode: str | None = None  # 技能激活方式；可空
    skill_activation_reason: str | None = None  # 技能为何被激活；可空
    skill_instruction_digest: str | None = None  # 技能指令摘要（用于审计/展示）；可空


class ChatResponse(BaseModel):
    """ChatResponse."""
    # 一次对话回答的完整返回结构
    run_id: UUID | str  # 本次运行 ID（可能是 UUID 或普通字符串），必填
    message_id: UUID | str  # 本条消息 ID，必填
    route: str  # 路由：后端把请求分发到了哪条处理链（如 rag / agent），必填
    tool_calls: list[ToolCallTrace]  # 本次回答过程中发生的工具调用轨迹列表，必填
    agent_events: list[dict] = []  # agent 内部事件列表（任意结构），默认空
    answer: str  # 最终给用户的回答文本，必填
    citations: list[dict] = []  # 引用来源列表（哪些文档被用到），默认空
    completion: dict = {}  # 模型补全的原始信息，默认空字典
    compression_stats: dict = {}  # 上下文压缩统计，默认空
    progressive_compaction: dict = {}  # 渐进式压缩信息，默认空
    model_usage: list[dict] = []  # 各模型用量记录列表，默认空
    context_diagnostics: dict = {}  # 上下文诊断信息，默认空


class ChatHistoryMessage(BaseModel):
    """ChatHistoryMessage."""
    # 历史会话里的一条消息（用于前端回显聊天记录）
    id: UUID | str  # 消息 ID，必填
    conversation_id: UUID | str | None = None  # 所属会话 ID；可空
    role: str  # 角色（user 用户 / assistant 助手 / system 系统），必填
    content: str  # 消息正文，必填
    route: str | None = None  # 当时走的路由；可空
    tool_calls: list[ToolCallTrace] = []  # 该消息关联的调用轨迹，默认空
    agent_events: list[dict] = []  # agent 事件，默认空
    compression_stats: dict = {}  # 压缩统计，默认空
    progressive_compaction: dict = {}  # 渐进压缩信息，默认空
    model_usage: list[dict] = []  # 模型用量，默认空
    context_diagnostics: dict = {}  # 上下文诊断，默认空
    created_at: str | None = None  # 创建时间（字符串）；可空


class PromptRecommendationRequest(BaseModel):
    """PromptRecommendationRequest."""
    # 请求「推荐提问」时传的数据
    repo_id: UUID  # 仓库 ID，必填
    exclude: list[str] = Field(default_factory=list)  # 想排除的推荐项 ID 列表，默认空
    limit: int = Field(default=5, ge=4, le=5)  # 返回条数；默认 5，且限定在 4~5 之间（ge=4, le=5）


class PromptRecommendationResponse(BaseModel):
    """PromptRecommendationResponse."""
    # 「推荐提问」的返回结构
    agent: str  # 由哪个 agent 给出推荐，必填
    suggestions: list[str]  # 推荐的问题文本列表，必填
