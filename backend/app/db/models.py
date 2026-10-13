"""数据库模型（ORM 映射层 / 数据表_schema）。

这个文件属于「ORM 映射层」，是整个后端数据结构的「总图纸」。它用 SQLAlchemy 2.0 风格的
「类型化映射（Mapped / mapped_column）」把每一个 Python 类对应到数据库里的一张表，
类里的每个属性对应表里的一列。后端所有要持久化的数据——用户、仓库、Issue、PR、CI、
聊天记录、知识库、代码图谱、评测结果等——都在这里定义。

关键内容：
- 顶部 import：引入 uuid / datetime / typing 以及 SQLAlchemy 的列类型与映射工具。
- 40 多个模型类，每个都继承 db.session.Base，用 __tablename__ 指定表名，
  用 Mapped[...] = mapped_column(...) 声明列，用 relationship() 声明表间关联。
- 通用套路：id 用 UUID 主键、created_at/updated_at 记录时间、带 index=True 的列会建索引加速查询。

和其他模块的配合：
- db/session.py 提供 Base 和 engine；本文件所有模型继承 Base；
- main.py 启动时用 Base.metadata.create_all(engine) 建表；
- 各路由 / 服务通过 get_db() 拿到会话后，直接操作这些模型类做增删改查。
可以把它类比成「公司的人事档案模板」：每张表是一种档案（员工表、部门表、工资表），
列就是档案上的每个格子，relationship 就是「这个员工属于哪个部门」的连线。."""

# 引入 uuid 模块：用来生成全局唯一标识符（主键 id 用）
import uuid
# 从 datetime 引入 datetime：用来表示「时间」类型的字段
from datetime import datetime
# 引入 typing.Any：标注「任意类型」，主要给 JSON 字段里的字典用
from typing import Any

# 从 sqlalchemy 引入用到的列类型与工具：
# JSON（存任意 JSON）、BigInteger（大整数，存 GitHub 的数字 id）、DateTime（时间）、
# Float（浮点）、ForeignKey（外键）、Integer（整数）、String（字符串）、Text（长文本）、
# Uuid（UUID 类型）、func（数据库函数，比如 func.now() 取数据库当前时间）
from sqlalchemy import JSON, BigInteger, DateTime, Float, ForeignKey, Integer, String, Text, Uuid, func
# 从 sqlalchemy.orm 引入映射工具：Mapped（类型标注）、mapped_column（声明列）、relationship（声明关联）
from sqlalchemy.orm import Mapped, mapped_column, relationship

# 引入 ORM 公共基类 Base（所有模型都继承它，SQLAlchemy 才知道要建表）
from app.db.session import Base


# 用户表模型：记录用这个系统的用户
class User(Base):
    """User."""
    # 指定数据库表名为 users
    __tablename__ = "users"

    # 主键 id：UUID 类型，默认用 uuid.uuid4() 自动生成
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 用户名：字符串最长 200，默认 "demo-user"
    name: Mapped[str] = mapped_column(String(200), default="demo-user")
    # 角色：字符串最长 80，默认 "owner"，建索引方便按角色查
    role: Mapped[str] = mapped_column(String(80), default="owner", index=True)
    # 加密后的 GitHub Token：长文本，可空（用户可能没绑 GitHub）
    github_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 创建时间：带时区的时间，默认取数据库当前时间
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# 仓库表模型：记录被接入系统的 GitHub 仓库（或其他 Git 平台仓库）
class Repository(Base):
    """Repository."""
    # 指定表名为 repositories
    __tablename__ = "repositories"

    # 主键 id：UUID，默认自动生成
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 仓库所属者（GitHub 用户名/组织名）：建索引
    owner: Mapped[str] = mapped_column(String(200), index=True)
    # 仓库名：建索引
    name: Mapped[str] = mapped_column(String(200), index=True)
    # 完整名 owner/name：唯一且建索引，避免重复接入同一个仓
    full_name: Mapped[str] = mapped_column(String(400), unique=True, index=True)
    # 提供方：默认 "github"（也支持自建 Git 平台）
    provider: Mapped[str] = mapped_column(String(80), default="github")
    # API 基础地址：可空（自建平台才需要）
    api_base_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 克隆地址：可空
    clone_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 本地路径：可空（克隆到本机的目录）
    local_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 检出模式：默认 "managed"（由系统管理克隆）
    checkout_mode: Mapped[str] = mapped_column(String(80), default="managed")
    # 加密后的 GitHub Token：可空（用仓库级别或用户级别的 Token）
    github_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 仓库描述：可空长文本
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 默认分支名：可空
    default_branch: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # GitHub 仓库数字 id：大整数，可空
    github_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # 上次同步时间：可空（还没同步过就是空）
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 创建时间：默认数据库当前时间
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # 更新时间：默认当前时间，且每次更新行时自动刷新为当前时间
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # 一对多关联：一个仓库有多个 Issue（反向由 Issue.repo 指回来），cascade 表示删仓库时它的 Issue 也一起删
    issues: Mapped[list["Issue"]] = relationship(back_populates="repo", cascade="all, delete-orphan")
    # 一对多关联：一个仓库有多个 PR
    pull_requests: Mapped[list["PullRequest"]] = relationship(back_populates="repo", cascade="all, delete-orphan")
    # 一对多关联：一个仓库有多个 CI 工作流运行记录
    workflow_runs: Mapped[list["WorkflowRun"]] = relationship(back_populates="repo", cascade="all, delete-orphan")


# 工作空间表模型：把多个仓库归到一组，方便统一对话/管理
class Workspace(Base):
    """Workspace."""
    # 指定表名为 workspaces
    __tablename__ = "workspaces"

    # 主键 id：UUID
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 工作空间名称：建索引
    name: Mapped[str] = mapped_column(String(200), index=True)
    # 描述：可空
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 关联的仓库 id 列表：用 JSON 存储（一个空间挂多个仓库）
    repo_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    # 创建者用户 id：外键指向 users.id，可空，建索引
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    # 创建时间：建索引（常按时间排序）
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    # 更新时间：自动刷新
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# 项目索引状态表：记录某个仓库的「文档/代码索引」建得怎么样了
class ProjectIndexState(Base):
    """ProjectIndexState."""
    # 指定表名为 project_index_states
    __tablename__ = "project_index_states"

    # 主键 id：UUID
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 仓库 id：外键指向 repositories.id，唯一（一个仓一条状态）且建索引
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id"), unique=True, index=True)
    # 索引状态：默认 "missing"，建索引
    status: Mapped[str] = mapped_column(String(40), default="missing", index=True)
    # 指纹：用来判断仓库内容是否变化（默认空串）
    fingerprint: Mapped[str] = mapped_column(String(200), default="")
    # 上次扫描时间：可空
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 暂停索引直到某个时间（可空，用于「先别索引」的场景）
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 已索引文档数：默认 0
    docs_indexed: Mapped[int] = mapped_column(Integer, default=0)
    # 文档总数：默认 0
    docs_total: Mapped[int] = mapped_column(Integer, default=0)
    # 错误信息：可空
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 摘要 JSON：存索引结果摘要，可空
    summary_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # 创建时间
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # 更新时间：自动刷新
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # 多对一关联：指向所属仓库（这里没写 back_populates，是单向关联）
    repo: Mapped[Repository] = relationship()


# 会话表模型：一次 AI 对话的「会话」容器
class Conversation(Base):
    """Conversation."""
    # 指定表名为 conversations
    __tablename__ = "conversations"

    # 主键 id：UUID
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 所属仓库 id：外键指向 repositories.id，建索引
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id"), index=True)
    # 会话标题：默认 "默认会话"
    title: Mapped[str] = mapped_column(String(300), default="默认会话")
    # 会话状态：默认 "active"，建索引
    status: Mapped[str] = mapped_column(String(80), default="active", index=True)
    # 消息条数：默认 0
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    # 创建时间：建索引
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    # 更新时间：自动刷新且建索引
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True)


# Issue 表模型：从 GitHub 同步过来的议题
class Issue(Base):
    """Issue."""
    # 指定表名为 issues
    __tablename__ = "issues"

    # 主键 id：UUID
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 所属仓库 id：外键，建索引
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id"), index=True)
    # GitHub 上的 issue 数字 id：大整数，可空
    github_issue_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Issue 编号（仓库内自增号）：建索引
    number: Mapped[int] = mapped_column(Integer, index=True)
    # 标题：最长 500
    title: Mapped[str] = mapped_column(String(500))
    # 正文：可空长文本
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 状态：默认 "open"
    state: Mapped[str] = mapped_column(String(50), default="open")
    # 标签列表：JSON 数组
    labels: Mapped[list[str]] = mapped_column(JSON, default=list)
    # 作者：可空
    author: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # 指派人列表：JSON 数组
    assignees: Mapped[list[str]] = mapped_column(JSON, default=list)
    # 创建时间：可空（GitHub 没给就空）
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 更新时间：可空
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 关闭时间：可空
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # 多对一关联：指向所属仓库（和 Repository.issues 双向关联）
    repo: Mapped[Repository] = relationship(back_populates="issues")


# Pull Request 表模型：从 GitHub 同步过来的 PR
class PullRequest(Base):
    """PullRequest."""
    # 指定表名为 pull_requests
    __tablename__ = "pull_requests"

    # 主键 id：UUID
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 所属仓库 id：外键，建索引
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id"), index=True)
    # GitHub 上的 PR 数字 id：大整数，可空
    github_pr_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # PR 编号：建索引
    number: Mapped[int] = mapped_column(Integer, index=True)
    # 标题：最长 500
    title: Mapped[str] = mapped_column(String(500))
    # 正文：可空
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 状态：默认 "open"
    state: Mapped[str] = mapped_column(String(50), default="open")
    # 作者：可空
    author: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # 目标分支（合入哪个分支）：可空
    base_branch: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # 来源分支（从哪个分支提的）：可空
    head_branch: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # 合入时间：可空
    merged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 创建时间：可空
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 更新时间：可空
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # 多对一关联：指向所属仓库
    repo: Mapped[Repository] = relationship(back_populates="pull_requests")
    # 一对多关联：一个 PR 有多个改动文件
    files: Mapped[list["PRFile"]] = relationship(back_populates="pr", cascade="all, delete-orphan")
    # 一对多关联：一个 PR 有多条评审评论
    review_comments: Mapped[list["PRReviewComment"]] = relationship(back_populates="pr", cascade="all, delete-orphan")


# PR 改动文件表模型：记录一个 PR 改了哪些文件
class PRFile(Base):
    """PRFile."""
    # 指定表名为 pr_files
    __tablename__ = "pr_files"

    # 主键 id：UUID
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 所属 PR id：外键指向 pull_requests.id，建索引
    pr_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pull_requests.id"), index=True)
    # 文件名：最长 500
    filename: Mapped[str] = mapped_column(String(500))
    # 文件状态（added/modified/deleted 等）
    status: Mapped[str] = mapped_column(String(50))
    # 新增行数：默认 0
    additions: Mapped[int] = mapped_column(Integer, default=0)
    # 删除行数：默认 0
    deletions: Mapped[int] = mapped_column(Integer, default=0)
    # 代码差异 patch：可空长文本
    patch: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 多对一关联：指向所属 PR
    pr: Mapped[PullRequest] = relationship(back_populates="files")


# PR 评审评论表模型：记录 PR 上的某条 review 评论
class PRReviewComment(Base):
    """PRReviewComment."""
    # 指定表名为 pr_review_comments
    __tablename__ = "pr_review_comments"

    # 主键 id：UUID
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 所属 PR id：外键，建索引
    pr_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pull_requests.id"), index=True)
    # GitHub 评论数字 id：大整数，可空
    github_comment_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # 评论内容：可空
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 评论所在文件路径：可空
    path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # 评论所在行号：可空
    line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 作者：可空
    author: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # 创建时间：可空
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 更新时间：可空
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # 多对一关联：指向所属 PR
    pr: Mapped[PullRequest] = relationship(back_populates="review_comments")


# CI 工作流运行表模型：记录 GitHub Actions 等一次 CI 运行
class WorkflowRun(Base):
    """WorkflowRun."""
    # 指定表名为 workflow_runs
    __tablename__ = "workflow_runs"

    # 主键 id：UUID
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 所属仓库 id：外键，建索引
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id"), index=True)
    # GitHub 运行数字 id：大整数，可空
    github_run_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # 工作流名称：最长 300
    name: Mapped[str] = mapped_column(String(300))
    # 状态（in_progress/completed 等）
    status: Mapped[str] = mapped_column(String(80))
    # 结论（success/failure/cancelled 等）：可空
    conclusion: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # CI 页面链接：可空
    html_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 各 job 详情：JSON 数组，默认空列表
    jobs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # 日志文本：可空
    logs_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 创建时间：可空
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 更新时间：可空
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # 多对一关联：指向所属仓库
    repo: Mapped[Repository] = relationship(back_populates="workflow_runs")


# 分析结果表模型：缓存对某个目标（Issue/PR/...）的 LLM 分析结果
class AnalysisResult(Base):
    """AnalysisResult."""
    # 指定表名为 analysis_results
    __tablename__ = "analysis_results"

    # 主键 id：UUID
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 被分析目标的类型（issue/pr/...）：建索引
    target_type: Mapped[str] = mapped_column(String(80), index=True)
    # 被分析目标的 id（UUID）：建索引
    target_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), index=True)
    # 分析类型（如 risk/summary）：建索引
    analysis_type: Mapped[str] = mapped_column(String(100), index=True)
    # 分析时的输入快照：JSON，默认空字典（便于复现）
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # 分析结果：JSON，默认空字典
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # 用的模型名：可空
    model_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # 创建时间
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# 文档表模型：RAG 用的「切片文档」（从仓库/知识库切出来的小段文字）
class Document(Base):
    """Document."""
    # 指定表名为 documents
    __tablename__ = "documents"

    # 主键 id：UUID
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 所属仓库 id：外键，建索引
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id"), index=True)
    # 来源类型（code_file/doc/...）：建索引
    source_type: Mapped[str] = mapped_column(String(80), index=True)
    # 来源 id（指向原始对象的 UUID）：建索引
    source_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), index=True)
    # 标题：最长 500
    title: Mapped[str] = mapped_column(String(500))
    # 正文内容：长文本（注意：向量不存这里，存外部 Milvus）
    content: Mapped[str] = mapped_column(Text)
    # 元数据：映射到数据库列名 "metadata"，JSON，默认空字典
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    # 创建时间
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# 知识库配置表模型：每个仓库的 RAG 检索参数配置
class KnowledgeBaseConfig(Base):
    """KnowledgeBaseConfig."""
    # 指定表名为 knowledge_base_configs
    __tablename__ = "knowledge_base_configs"

    # 主键 id：UUID
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 所属仓库 id：外键，唯一（一个仓一套配置）且建索引
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id"), unique=True, index=True)
    # 向量化提供方：默认 "qwen_local"
    embedding_provider: Mapped[str] = mapped_column(String(80), default="qwen_local")
    # 向量化模型名：最长 240
    embedding_model: Mapped[str] = mapped_column(String(240), default="Qwen/Qwen3-Embedding-0.6B")
    # 向量维度：默认 1024
    embedding_dimensions: Mapped[int] = mapped_column(Integer, default=1024)
    # 检索方式：默认 "hybrid"（混合：关键词 + 向量）
    retrieval_method: Mapped[str] = mapped_column(String(40), default="hybrid")
    # 是否开启重排序：默认开
    rerank_enabled: Mapped[bool] = mapped_column(default=True)
    # 重排序提供方：默认 "qwen_api"
    rerank_provider: Mapped[str] = mapped_column(String(80), default="qwen_api")
    # 重排序模型名：最长 240
    rerank_model: Mapped[str] = mapped_column(String(240), default="qwen3-rerank")
    # 召回条数 top_k：默认 3
    top_k: Mapped[int] = mapped_column(Integer, default=3)
    # 是否开启分数阈值过滤：默认开
    score_threshold_enabled: Mapped[bool] = mapped_column(default=True)
    # 分数阈值：默认 0.8
    score_threshold: Mapped[float] = mapped_column(Float, default=0.8)
    # 切片大小（字符数）：默认 800
    chunk_size: Mapped[int] = mapped_column(Integer, default=800)
    # 切片重叠：默认 100（相邻切片重叠一点，避免切断语义）
    chunk_overlap: Mapped[int] = mapped_column(Integer, default=100)
    # 创建时间
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # 更新时间：自动刷新
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# 知识库源文档表模型：记录用户上传的、待建索引的原始文件
class KnowledgeSourceDocument(Base):
    """KnowledgeSourceDocument."""
    # 指定表名为 knowledge_source_documents
    __tablename__ = "knowledge_source_documents"

    # 主键 id：UUID
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 所属仓库 id：外键，建索引
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id"), index=True)
    # 文件名：最长 500
    name: Mapped[str] = mapped_column(String(500))
    # 来源类型：默认 "upload_file"（上传文件）
    source_type: Mapped[str] = mapped_column(String(80), default="upload_file")
    # 处理状态：默认 "queued"（排队中），建索引
    status: Mapped[str] = mapped_column(String(40), default="queued", index=True)
    # 文件在本地存储的路径：长文本
    stored_path: Mapped[str] = mapped_column(Text)
    # 内容类型（MIME）：可空
    content_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # 文件校验和（checksum）：建索引，用来去重/判断内容是否变化
    checksum: Mapped[str] = mapped_column(String(64), index=True)
    # 文件大小（字节）：默认 0
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    # 字符数：默认 0
    character_count: Mapped[int] = mapped_column(Integer, default=0)
    # 切片数：默认 0
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    # 错误信息：可空
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 创建时间：建索引
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    # 更新时间：自动刷新
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    # 完成时间：可空（建索引完成的时间）
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# 检索测试运行表模型：记录一次 RAG 检索效果的测试
class RetrievalTestRun(Base):
    """RetrievalTestRun."""
    # 指定表名为 retrieval_test_runs
    __tablename__ = "retrieval_test_runs"

    # 主键 id：UUID
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 所属仓库 id：外键，建索引
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id"), index=True)
    # 查询问题：长文本
    query: Mapped[str] = mapped_column(Text)
    # 检索配置快照：JSON，默认空字典
    retrieval_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # 检索结果：JSON 数组，默认空列表
    results_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # 耗时（毫秒）：默认 0
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    # 创建时间：建索引
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


# 代码符号表模型：记录代码里的「符号」（函数/类/变量等）
class CodeSymbol(Base):
    """CodeSymbol."""
    # 指定表名为 code_symbols
    __tablename__ = "code_symbols"

    # 主键 id：UUID
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 所属仓库 id：外键，建索引
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id"), index=True)
    # 文件路径：建索引
    path: Mapped[str] = mapped_column(String(500), index=True)
    # 符号名：建索引
    name: Mapped[str] = mapped_column(String(300), index=True)
    # 符号种类（function/class/...）：默认 "symbol"，建索引
    kind: Mapped[str] = mapped_column(String(80), default="symbol", index=True)
    # 编程语言：默认 "text"，建索引
    language: Mapped[str] = mapped_column(String(80), default="text", index=True)
    # 起始行：可空
    start_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 结束行：可空
    end_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 所在分支：可空，建索引
    branch: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    # 提交哈希：可空，建索引
    commit_sha: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    # 关联 PR 的 id：可空，建索引（这个符号属于哪个 PR 引入的）
    pr_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)
    # 关联 PR 的编号：可空，建索引
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # 元数据：映射到列名 "metadata"，JSON，默认空字典
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    # 创建时间：建索引
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


# 代码关系表模型：记录符号之间的调用/引用关系（代码图谱的边）
class CodeRelation(Base):
    """CodeRelation."""
    # 指定表名为 code_relations
    __tablename__ = "code_relations"

    # 主键 id：UUID
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 所属仓库 id：外键，建索引
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id"), index=True)
    # 源符号 id：外键指向 code_symbols.id，可空，建索引
    source_symbol_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("code_symbols.id"), nullable=True, index=True)
    # 目标符号 id：外键指向 code_symbols.id，可空，建索引
    target_symbol_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("code_symbols.id"), nullable=True, index=True)
    # 源符号名：可空，建索引
    source_name: Mapped[str | None] = mapped_column(String(300), nullable=True, index=True)
    # 目标符号名：建索引
    target_name: Mapped[str] = mapped_column(String(300), index=True)
    # 关系类型：默认 "calls"（调用），建索引
    relation_type: Mapped[str] = mapped_column(String(80), default="calls", index=True)
    # 文件路径：可空，建索引
    path: Mapped[str | None] = mapped_column(String(500), nullable=True, index=True)
    # 所在分支：可空，建索引
    branch: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    # 关联 PR id：可空，建索引
    pr_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)
    # 关联 PR 编号：可空，建索引
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # 元数据：映射到列名 "metadata"，JSON，默认空字典
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    # 创建时间：建索引
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


# 聊天消息表模型：一次对话里的一条消息（用户或助手）
class ChatMessage(Base):
    """ChatMessage."""
    # 指定表名为 chat_messages
    __tablename__ = "chat_messages"

    # 主键 id：UUID
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 所属仓库 id：外键，可空（全局问答可能不挂仓库），建索引
    repo_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("repositories.id"), nullable=True, index=True)
    # 所属会话 id：外键指向 conversations.id，可空，建索引
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversations.id"), nullable=True, index=True)
    # 角色（user/assistant/system）：建索引
    role: Mapped[str] = mapped_column(String(40), index=True)
    # 消息内容：长文本
    content: Mapped[str] = mapped_column(Text)
    # 路由（走哪条处理链）：可空
    route: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # 工具调用记录：JSON 数组，默认空列表
    tool_calls: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # 元数据：映射到列名 "metadata"，JSON，默认空字典
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    # 创建时间：建索引
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


# 聊天会话快照表模型：把一轮「多轮对话」封口成一份可回看的记录
class ChatSession(Base):
    """ChatSession."""
    # 指定表名为 chat_sessions
    __tablename__ = "chat_sessions"

    # 主键 id：UUID
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 所属仓库 id：外键，可空，建索引
    repo_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("repositories.id"), nullable=True, index=True)
    # 所属会话 id：外键，可空，建索引
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversations.id"), nullable=True, index=True)
    # 用户消息 id：外键指向 chat_messages.id，可空，建索引
    user_message_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)
    # 助手消息 id：外键，可空，建索引
    assistant_message_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)
    # 路由：默认 "unknown"
    route: Mapped[str] = mapped_column(String(100), default="unknown")
    # 转写记录（整轮对话的 JSON）：默认空字典
    transcript_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # 摘要：默认空串
    digest: Mapped[str] = mapped_column(Text, default="")
    # 状态：默认 "sealed"（已封口），建索引
    status: Mapped[str] = mapped_column(String(80), default="sealed", index=True)
    # 创建时间：建索引
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    # 封口时间：可空（封口那一刻记下来）
    sealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# 线程记忆表模型：跨多轮对话积累的「线程级」记忆（摘要/事实/任务等）
class ThreadMemory(Base):
    """ThreadMemory."""
    # 指定表名为 thread_memories
    __tablename__ = "thread_memories"

    # 主键 id：UUID
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 所属仓库 id：外键，可空，唯一（一个仓一份线程记忆）且建索引
    repo_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("repositories.id"), nullable=True, unique=True, index=True)
    # 摘要：默认空串
    summary: Mapped[str] = mapped_column(Text, default="")
    # 事实列表：JSON 数组
    facts: Mapped[list[str]] = mapped_column(JSON, default=list)
    # 决策列表：JSON 数组
    decisions: Mapped[list[str]] = mapped_column(JSON, default=list)
    # 待解决问题列表：JSON 数组
    open_questions: Mapped[list[str]] = mapped_column(JSON, default=list)
    # 任务列表：JSON 数组
    tasks: Mapped[list[str]] = mapped_column(JSON, default=list)
    # 用户偏好列表：JSON 数组
    user_preferences: Mapped[list[str]] = mapped_column(JSON, default=list)
    # 仓库上下文列表：JSON 数组
    repo_context: Mapped[list[str]] = mapped_column(JSON, default=list)
    # 引用/出处列表：JSON 数组
    citations: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # 已并入的会话数：默认 0
    sessions_incorporated: Mapped[int] = mapped_column(Integer, default=0)
    # 更新时间：自动刷新
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# 会话记忆表模型：单个会话级别的记忆（结构同 ThreadMemory，但按会话维度）
class ConversationMemory(Base):
    """ConversationMemory."""
    # 指定表名为 conversation_memories
    __tablename__ = "conversation_memories"

    # 主键 id：UUID
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 所属仓库 id：外键，建索引
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id"), index=True)
    # 所属会话 id：外键，唯一（一个会话一份记忆）且建索引
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"), unique=True, index=True)
    # 摘要：默认空串
    summary: Mapped[str] = mapped_column(Text, default="")
    # 事实列表：JSON 数组
    facts: Mapped[list[str]] = mapped_column(JSON, default=list)
    # 决策列表：JSON 数组
    decisions: Mapped[list[str]] = mapped_column(JSON, default=list)
    # 待解决问题列表：JSON 数组
    open_questions: Mapped[list[str]] = mapped_column(JSON, default=list)
    # 任务列表：JSON 数组
    tasks: Mapped[list[str]] = mapped_column(JSON, default=list)
    # 用户偏好列表：JSON 数组
    user_preferences: Mapped[list[str]] = mapped_column(JSON, default=list)
    # 仓库上下文列表：JSON 数组
    repo_context: Mapped[list[str]] = mapped_column(JSON, default=list)
    # 引用列表：JSON 数组
    citations: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # 已并入的会话数：默认 0
    sessions_incorporated: Mapped[int] = mapped_column(Integer, default=0)
    # 更新时间：自动刷新
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# 证据项表模型：记录回答时引用的「证据」来源（Issue/PR/文档等）
class EvidenceItem(Base):
    """EvidenceItem."""
    # 指定表名为 evidence_items
    __tablename__ = "evidence_items"

    # 主键 id：UUID
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 所属仓库 id：外键，可空，建索引
    repo_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("repositories.id"), nullable=True, index=True)
    # 所属会话 id：外键，可空，建索引
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversations.id"), nullable=True, index=True)
    # 来源类型（issue/pr/document/...）：建索引
    source_type: Mapped[str] = mapped_column(String(80), index=True)
    # 来源 id（字符串形式）：建索引
    source_id: Mapped[str] = mapped_column(String(120), index=True)
    # 标题：最长 500
    title: Mapped[str] = mapped_column(String(500))
    # 内容：长文本
    content: Mapped[str] = mapped_column(Text)
    # 元数据：映射到列名 "metadata"，JSON，默认空字典
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    # 创建时间：建索引
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


# 知识图谱边表模型：实体之间的关系边（比代码关系更泛化的「知识图谱」）
class KnowledgeGraphEdge(Base):
    """KnowledgeGraphEdge."""
    # 指定表名为 knowledge_graph_edges
    __tablename__ = "knowledge_graph_edges"

    # 主键 id：UUID
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 所属仓库 id：外键，建索引
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id"), index=True)
    # 起点类型（实体种类）：建索引
    from_type: Mapped[str] = mapped_column(String(80), index=True)
    # 起点 id：建索引
    from_id: Mapped[str] = mapped_column(String(180), index=True)
    # 终点类型：建索引
    to_type: Mapped[str] = mapped_column(String(80), index=True)
    # 终点 id：建索引
    to_id: Mapped[str] = mapped_column(String(180), index=True)
    # 关系名：建索引
    relation: Mapped[str] = mapped_column(String(80), index=True)
    # 置信度（0~1）：默认 1.0
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    # 来源（explicit 显式 / inferred 推断）：默认 "explicit"
    source: Mapped[str] = mapped_column(String(120), default="explicit")
    # 元数据：映射到列名 "metadata"，JSON，默认空字典
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    # 创建时间：建索引
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


# 召回事件表模型：记录每次「检索召回」的调用与结果（用于复盘/评测）
class RecallEvent(Base):
    """RecallEvent."""
    # 指定表名为 recall_events
    __tablename__ = "recall_events"

    # 主键 id：UUID
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 所属仓库 id：外键，可空，建索引
    repo_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("repositories.id"), nullable=True, index=True)
    # 所属会话 id：外键，可空，建索引
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversations.id"), nullable=True, index=True)
    # 工具名（哪个检索工具）：建索引
    tool_name: Mapped[str] = mapped_column(String(120), index=True)
    # 查询问题：默认空串
    query: Mapped[str] = mapped_column(Text, default="")
    # 作用范围（project/repo/...）：默认 "project"
    scope: Mapped[str] = mapped_column(String(120), default="project")
    # 模式（hybrid/vector/keyword）：默认 "hybrid"
    mode: Mapped[str] = mapped_column(String(80), default="hybrid")
    # 状态：默认 "success"，建索引
    status: Mapped[str] = mapped_column(String(80), default="success", index=True)
    # 结果条数：默认 0
    result_count: Mapped[int] = mapped_column(Integer, default=0)
    # 结果列表：JSON 数组，默认空列表
    results_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # 引用列表：JSON 数组
    citations_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # 元数据：映射到列名 "metadata"，JSON，默认空字典
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    # 创建时间：建索引
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


# 记忆候选表模型：待审核的「记忆候选」（会话封口后提炼出来，等人工确认要不要入库）
class MemoryCandidate(Base):
    """MemoryCandidate."""
    # 指定表名为 memory_candidates
    __tablename__ = "memory_candidates"

    # 主键 id：UUID
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 所属仓库 id：外键，可空，建索引
    repo_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("repositories.id"), nullable=True, index=True)
    # 所属会话 id：外键，可空，建索引
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversations.id"), nullable=True, index=True)
    # 所属会话快照 id：外键指向 chat_sessions.id，可空，建索引
    session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("chat_sessions.id"), nullable=True, index=True)
    # 候选种类：建索引
    kind: Mapped[str] = mapped_column(String(80), index=True)
    # 标题：最长 500
    title: Mapped[str] = mapped_column(String(500))
    # 内容：长文本
    content: Mapped[str] = mapped_column(Text)
    # 状态：默认 "pending"（待审），建索引
    status: Mapped[str] = mapped_column(String(80), default="pending", index=True)
    # 来源：默认 "session_sealer"（会话封口器产出）
    source: Mapped[str] = mapped_column(String(120), default="session_sealer")
    # 元数据：映射到列名 "metadata"，JSON，默认空字典
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    # 创建时间：建索引
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    # 审核时间：可空
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# 智能体运行表模型：记录一次「Agent 跑完整轮对话」的过程与结果
class AgentRun(Base):
    """AgentRun."""
    # 指定表名为 agent_runs
    __tablename__ = "agent_runs"

    # 主键 id：UUID
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 所属仓库 id：外键，可空
    repo_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("repositories.id"), nullable=True)
    # 所属会话 id：外键，可空，建索引
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversations.id"), nullable=True, index=True)
    # 用户消息（原始输入）：长文本
    user_message: Mapped[str] = mapped_column(Text)
    # 路由：最长 100
    route: Mapped[str] = mapped_column(String(100))
    # 工具调用记录：JSON 数组
    tool_calls: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # 最终答案：长文本
    final_answer: Mapped[str] = mapped_column(Text)
    # 状态：默认 "success"
    status: Mapped[str] = mapped_column(String(80), default="success")
    # 创建时间
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# 聊天反馈表模型：用户对某条助手回答的评分/反馈
class ChatFeedback(Base):
    """ChatFeedback."""
    # 指定表名为 chat_feedback
    __tablename__ = "chat_feedback"

    # 主键 id：UUID
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 所属仓库 id：外键，删除仓库时级联删除（CASCADE），建索引
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    # 所属会话 id：外键，删除会话时级联删除，建索引
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    # 被反馈的助手消息 id：外键指向 chat_messages.id，删除消息时级联删除，唯一（一条消息一份反馈）且建索引
    assistant_message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="CASCADE"), unique=True, index=True
    )
    # 关联的运行 id：外键指向 agent_runs.id，删运行时置空（SET NULL），可空，建索引
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True)
    # 反馈用户 id：外键指向 users.id，删用户时置空，可空，建索引
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    # 评分（like/dislike 等）：建索引
    rating: Mapped[str] = mapped_column(String(40), index=True)
    # 原因标签：可空，建索引
    reason: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    # 文字评论：可空
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 审核状态：默认 "open"，建索引
    review_status: Mapped[str] = mapped_column(String(40), default="open", index=True)
    # 审核备注：可空
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 通知状态：默认 "pending"，建索引
    notification_status: Mapped[str] = mapped_column(String(40), default="pending", index=True)
    # 通知错误：可空
    notification_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 已通知时间：可空
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 审核时间：可空
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 创建时间：建索引
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    # 更新时间：默认当前时间、自动刷新，且建索引
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True
    )


# 智能体工作流运行表模型：记录一次「多智能体工作流」的整体运行
class AgentWorkflowRun(Base):
    """AgentWorkflowRun."""
    # 指定表名为 agent_workflow_runs
    __tablename__ = "agent_workflow_runs"

    # 主键 id：UUID
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 所属仓库 id：外键，可空，建索引
    repo_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("repositories.id"), nullable=True, index=True)
    # 所属会话 id：外键，可空，建索引
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversations.id"), nullable=True, index=True)
    # 目标任务：长文本
    goal: Mapped[str] = mapped_column(Text)
    # 规格 JSON：默认空字典
    spec_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # 观测 JSON：默认空字典
    observation_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # 最终答案：默认空串
    final_answer: Mapped[str] = mapped_column(Text, default="")
    # 指标 JSON：默认空字典
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # 状态：默认 "success"，建索引
    status: Mapped[str] = mapped_column(String(80), default="success", index=True)
    # 创建时间：建索引
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    # 完成时间：可空
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# 智能体任务运行表模型：工作流里单个「子任务」的运行明细
class AgentTaskRun(Base):
    """AgentTaskRun."""
    # 指定表名为 agent_task_runs
    __tablename__ = "agent_task_runs"

    # 主键 id：UUID
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 所属工作流 id：外键指向 agent_workflow_runs.id，建索引
    workflow_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_workflow_runs.id"), index=True)
    # 任务 id（业务内标识）：建索引
    task_id: Mapped[str] = mapped_column(String(120), index=True)
    # 执行该任务的智能体名：建索引
    agent_name: Mapped[str] = mapped_column(String(120), index=True)
    # 任务类型：建索引
    task_type: Mapped[str] = mapped_column(String(120), index=True)
    # 任务声明 JSON：默认空字典
    claim_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # 输入 JSON：默认空字典
    input_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # 结果 JSON：默认空字典
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # 状态：默认 "success"，建索引
    status: Mapped[str] = mapped_column(String(80), default="success", index=True)
    # 错误信息：可空
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 开始时间：可空
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 完成时间：可空
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# 评测运行表模型：一次 RAGAS 等自动评测的结果
class EvalRun(Base):
    """EvalRun."""
    # 指定表名为 eval_runs
    __tablename__ = "eval_runs"

    # 主键 id：UUID
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 评测名称：最长 200
    name: Mapped[str] = mapped_column(String(200))
    # 结果 JSON：默认空字典
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # 创建时间
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# 动作草稿表模型：需要用户「确认后才执行」的高风险操作草稿（如发 PR、改配置）
class ActionDraft(Base):
    """ActionDraft."""
    # 指定表名为 action_drafts
    __tablename__ = "action_drafts"

    # 主键 id：UUID
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 所属仓库 id：外键，建索引
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id"), index=True)
    # 草稿类型（如 create_pr）：建索引
    draft_type: Mapped[str] = mapped_column(String(100), index=True)
    # 目标类型（要操作的对象种类）：建索引
    target_type: Mapped[str] = mapped_column(String(100), index=True)
    # 目标 id：可空，建索引
    target_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)
    # 标题：默认空串
    title: Mapped[str] = mapped_column(String(300), default="")
    # 内容：默认空串
    content: Mapped[str] = mapped_column(Text, default="")
    # 风险等级：默认 "medium"，建索引
    risk_level: Mapped[str] = mapped_column(String(80), default="medium", index=True)
    # 状态：默认 "pending_confirmation"（待确认），建索引
    status: Mapped[str] = mapped_column(String(80), default="pending_confirmation", index=True)
    # 创建者用户 id：外键指向 users.id，可空，建索引
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    # 确认者用户 id：外键指向 users.id，可空，建索引
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    # 执行时间：可空
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 执行结果：可空 JSON
    execution_result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # 错误信息：可空
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 元数据：映射到列名 "metadata"，JSON，默认空字典
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    # 创建时间：建索引
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    # 更新时间：自动刷新
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# 审计日志表模型：记录「谁在什么时间对什么做了什么」，用于安全追溯
class AuditLog(Base):
    """AuditLog."""
    # 指定表名为 audit_logs
    __tablename__ = "audit_logs"

    # 主键 id：UUID
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 操作用户 id：外键指向 users.id，可空，建索引
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    # 所属仓库 id：外键，可空，建索引
    repo_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("repositories.id"), nullable=True, index=True)
    # 动作名：建索引
    action: Mapped[str] = mapped_column(String(160), index=True)
    # 目标类型：可空，建索引
    target_type: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    # 目标 id：可空，建索引
    target_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    # 状态：默认 "success"，建索引
    status: Mapped[str] = mapped_column(String(80), default="success", index=True)
    # 请求 JSON：默认空字典
    request_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # 结果 JSON：默认空字典
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # 创建时间：建索引
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
