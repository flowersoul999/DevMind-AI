"""配置中心（配置子系统）。

这个文件属于「配置子系统」，是整个后端唯一存放「运行时可调参数」的地方。
它用 pydantic-settings 的 BaseSettings 把「环境变量 + .env 文件 + 代码里的默认值」
合并成一个统一的配置对象 `settings`，其他地方只要 `from app.core.config import settings`
就能拿到数据库连接串、GitHub Token、LLM 地址等所有配置，不用到处硬编码。

关键内容：
- 一个 Settings 类：把上百个配置项声明成类属性（带默认值），类型清晰、IDE 能自动补全。
- 一个全局单例 `settings = Settings()`：程序启动时构造一次，全项目共享同一份配置。

和其他模块的配合：
- db/session.py 用 `settings.database_url` 连数据库；
- core/security.py 用 `settings.token_encryption_key` 加密 GitHub Token；
- main.py 用 `settings.app_name / cors_origins / auto_sync_enabled` 等控制应用行为。
可以把它理解成「整个项目的遥控器」：改 .env 文件就等于按遥控器上的按钮，代码不用动。."""

# 从 pydantic-settings 引入两个东西：BaseSettings（配置基类）和 SettingsConfigDict（配置字典类型的声明）
from pydantic_settings import BaseSettings, SettingsConfigDict


# 定义一个配置类 Settings，继承自 BaseSettings，所有配置项都写成类属性
class Settings(BaseSettings):
    """Settings."""
    # 当前运行环境：development（开发）/ production（生产），默认开发
    app_env: str = "development"
    # 应用名字，会显示在接口文档标题上，默认 DevMind AI
    app_name: str = "DevFlow AI"
    # API 服务监听的 IP 地址，0.0.0.0 表示监听所有网卡（能被同局域网其他机器访问）
    api_host: str = "0.0.0.0"
    # API 服务监听的端口号，默认 8000
    api_port: int = 8000
    # 允许跨域访问的前端来源（CORS），逗号分隔；前端跑在 3000/3001 端口所以要放行
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000,http://localhost:3001,http://127.0.0.1:3001"

    # 数据库连接串：这里用 PostgreSQL + psycopg 驱动，连本机 5432 端口的 devflow 库
    # 为什么：把连接信息写成一个 URL 串，SQLAlchemy 一拿到就能建连，约定俗成的最小配置
    database_url: str = "postgresql+psycopg://devflow:devflow@localhost:5432/devflow"

    # GitHub 个人访问令牌（用于调 GitHub API 拉 Issue/PR/CI），可为空
    github_token: str | None = None
    # GitHub API 的根地址，默认官方地址
    github_api_base_url: str = "https://api.github.com"

    # 大模型（LLM）相关配置：调用 LLM 做分析时要用
    llm_api_key: str | None = None  # LLM 的 API Key，可为空（本地模型可能不需要）
    llm_base_url: str = "https://api.deepseek.com"  # LLM 服务地址，默认 DeepSeek
    llm_model: str = "deepseek-v4-pro"  # 默认使用的模型名
    rag_llm_enabled: bool = True  # 是否启用 RAG（检索增强生成）链路里的 LLM，默认开
    embedding_provider: str = "qwen_local"  # 向量化（把文字变向量）的服务提供方，默认本地 Qwen
    embedding_model: str = "Qwen/Qwen3-Embedding-0.6B"  # 向量化用的模型名
    embedding_dimensions: int = 1024  # 向量维度，1024 维；要和向量库的集合维度对上，否则存不进去
    embedding_api_key: str | None = None  # 云端向量化服务的 Key，本地模型可空
    embedding_base_url: str | None = None  # 云端向量化服务的地址，本地模型可空
    local_embedding_model: str = "Qwen/Qwen3-Embedding-0.6B"  # 本地向量化模型的名字
    local_embedding_device: str | None = None  # 本地模型跑在哪个设备（cpu/gpu），为空让框架自己选
    qwen_embedding_model: str = "Qwen/Qwen3-Embedding-0.6B"  # 阿里云 Qwen 向量模型名
    dashscope_api_key: str | None = None  # 阿里云 DashScope 的 Key
    dashscope_embedding_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"  # DashScope 兼容模式地址
    rerank_api_key: str | None = None  # 重排序（rerank）服务的 Key
    rerank_base_url: str | None = None  # 重排序服务的地址
    qwen_rerank_model: str = "qwen3-rerank"  # 重排序用的模型名
    # 重排序的指令：告诉模型「给定用户问题，找出能回答它所需事实的段落」
    qwen_retrieval_instruction: str = "Given a user question, retrieve passages that contain the facts needed to answer it."

    # 向量数据库 Milvus 相关配置
    milvus_enabled: bool = True  # 是否启用 Milvus，默认开
    milvus_uri: str = "http://localhost:19530"  # Milvus 服务地址
    milvus_token: str | None = None  # Milvus 的访问令牌，可为空
    milvus_collection: str = "devflow_rag_qwen1024"  # 向量集合名，1024 对应上面的向量维度
    rag_rerank_top_n: int = 24  # RAG 检索后先做重排序，保留前 24 条
    # 下面一大组 ragas_* 是「自动评测（RAGAS）」相关的阈值与超时配置，用来衡量回答质量
    ragas_enabled: bool = True  # 是否启用 RAGAS 自动评测
    ragas_judge_api_key: str | None = None  # 评测用的裁判模型 Key
    ragas_judge_base_url: str | None = None  # 评测裁判模型地址
    ragas_judge_model: str | None = None  # 评测裁判模型名
    ragas_timeout_seconds: float = 120.0  # 单次评测超时时间（秒）
    ragas_judge_max_tokens: int = 8192  # 裁判模型最大输出 token 数
    ragas_answer_relevancy_strictness: int = 1  # 答案相关性严格度
    ragas_max_cases: int = 5  # 一次评测最多用几个样本
    ragas_context_precision_min: float = 0.75  # 上下文精确度最低分
    ragas_context_recall_min: float = 0.80  # 上下文召回率最低分
    ragas_faithfulness_min: float = 0.90  # 忠实度（不胡说）最低分
    ragas_answer_relevancy_min: float = 0.80  # 答案相关性最低分
    ragas_agent_goal_accuracy_min: float = 0.80  # 智能体目标达成率最低分
    ragas_tool_call_accuracy_min: float = 0.90  # 工具调用准确率最低分
    ragas_tool_call_f1_min: float = 0.90  # 工具调用 F1 最低分
    # 下面一组 context_* 是「上下文窗口管理」配置：控制和 LLM 对话时怎么塞上下文、何时压缩
    context_max_input_tokens: int = 24000  # 单次请求上下文最大 token 数
    context_reserved_response_tokens: int = 3000  # 预留给模型输出的 token 数
    context_dynamic_model_window_enabled: bool = True  # 是否开启按模型窗口动态适配
    context_model_window_tokens: int | None = None  # 模型窗口大小，为空则自动探测
    context_compact_reserved_output_tokens: int = 12000  # 压缩时预留的输出 token
    context_auto_compact_enabled: bool = True  # 是否自动压缩过长的上下文
    context_warning_buffer_tokens: int = 20000  # 接近上限多少 token 时告警
    context_auto_compact_buffer_tokens: int = 13000  # 剩余多少 token 时自动压缩
    context_aggressive_buffer_tokens: int = 8000  # 激进压缩的阈值
    context_manual_buffer_tokens: int = 3000  # 手动压缩的阈值
    context_max_compaction_failures: int = 3  # 最多允许连续压缩失败几次
    context_microcompact_keep_recent_messages: int = 8  # 微压缩时保留最近几条消息
    context_compact_keep_recent_messages: int = 8  # 普通压缩时保留最近几条消息
    context_compact_summary_tokens: int = 1800  # 压缩后摘要的目标 token 数
    context_system_ratio: float = 0.12  # 系统提示词占上下文的比例
    context_memory_ratio: float = 0.22  # 记忆占上下文的比例
    context_evidence_ratio: float = 0.34  # 证据占上下文的比例
    context_recent_ratio: float = 0.22  # 最近消息占上下文的比例
    context_tool_ratio: float = 0.10  # 工具结果占上下文的比例
    workflow_task_timeout_seconds: float = 90.0  # 单个工作流任务超时（秒）
    prompt_cache_enabled: bool = True  # 是否开启提示词缓存（省钱提速）
    prompt_cache_key: str | None = "devflow-agent-v1"  # 缓存的 key 标识
    prompt_cache_retention: str | None = None  # 缓存保留策略，为空用默认
    repo_checkout_dir: str = ".data/repos"  # 把 GitHub 仓库克隆到本地的目录
    upload_dir: str = ".data/uploads"  # 用户上传文件存放目录

    # GitHub Token 加密的密钥；默认是占位符，生产必须改
    # 为什么：Token 不能明文存数据库，要用这个密钥做对称加密；默认值是课程项目用的，千万别上生产
    token_encryption_key: str = "change-me-in-local-dev"
    demo_mode: bool = True  # 是否为演示模式（课程项目默认开，会塞一些示例数据）
    github_webhook_secret: str | None = None  # GitHub Webhook 的签名密钥，用来验证回调真伪
    feishu_feedback_webhook_url: str | None = None  # 飞书反馈机器人的 Webhook 地址
    feedback_trace_base_url: str | None = None  # 反馈链路追踪的基础 URL
    auto_sync_enabled: bool = True  # 是否开启「自动定时同步 GitHub 数据」
    auto_sync_interval_seconds: int = 300  # 自动同步间隔（秒），5 分钟一次
    auto_sync_limit: int = 50  # 单次同步最多处理多少条

    # pydantic-settings 的配置：从 .env 文件读环境变量，支持 ../.env 和 .env 两个位置
    # 为什么：把密钥/地址写进 .env（不进 git），既能保密又能本地灵活改；extra="ignore" 表示多余变量直接忽略不报错
    model_config = SettingsConfigDict(env_file=("../.env", ".env"), env_file_encoding="utf-8", extra="ignore")


# 在模块加载时就构造一个全局配置对象 settings，全项目 import 这一个实例即可
settings = Settings()
