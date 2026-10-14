"""
repos.py —— 仓库（Repository）连接/同步接口的数据契约（Schema 层）

本文件服务于「连接仓库」「查看仓库信息」「同步仓库数据」接口：
- RepoConnectRequest：连接一个 GitHub/本地仓库时传的参数；
- RepoResponse / RepoConnectResponse：仓库信息、连接结果的响应；
- RepoSyncRequest / RepoSyncResponse：触发同步（Issue/PR/CI）及结果。

demo_mode=True 时后端不真正拉代码，而是用内置样例数据，方便新人快速体验。."""

from datetime import datetime  # datetime：时间戳
from uuid import UUID  # UUID：唯一 ID

from pydantic import BaseModel, Field  # BaseModel：基类；Field：字段约束


class RepoConnectRequest(BaseModel):
    """RepoConnectRequest."""
    # 连接一个仓库的请求体（大多可空：自动探测 GitHub 仓库时只需 owner/repo）
    owner: str | None = None  # 仓库所属者（用户名/组织），可空
    repo: str | None = None  # 仓库名，可空
    token: str | None = None  # 访问令牌（旧字段），可空
    github_token: str | None = None  # GitHub 令牌，可空
    provider: str = "github"  # 代码托管供应商，默认 github
    api_base_url: str | None = None  # API 根地址（自建 GitLab 等），可空
    local_path: str | None = None  # 本地仓库路径（连本地目录时用），可空
    clone_parent_dir: str | None = None  # 克隆到的父目录，可空
    demo_mode: bool = False  # 是否演示模式（用样例数据，不真拉代码），默认否
    initial_sync_limit: int = Field(default=5, ge=1, le=50)  # 首次同步条数上限；默认 5，限定 1~50
    sync_issues: bool = True  # 是否同步 Issue，默认是
    sync_pull_requests: bool = True  # 是否同步 PR，默认是
    sync_workflow_runs: bool = True  # 是否同步 CI 运行，默认是


class RepoResponse(BaseModel):
    """RepoResponse."""
    # 仓库信息响应
    id: UUID  # 仓库记录 ID，必填
    owner: str  # 所属者，必填
    name: str  # 仓库名，必填
    full_name: str  # 全名（owner/name），必填
    provider: str = "github"  # 供应商，默认 github
    api_base_url: str | None = None  # API 根地址；可空
    clone_url: str | None = None  # 克隆地址；可空
    local_path: str | None = None  # 本地路径；可空
    checkout_mode: str = "managed"  # 检出方式（managed 托管式），默认 managed
    description: str | None = None  # 描述；可空
    default_branch: str | None = None  # 默认分支；可空
    last_sync_at: datetime | None = None  # 上次同步时间；可空
    updated_at: datetime | None = None  # 更新时间；可空

    model_config = {"from_attributes": True}  # 为什么：允许用 ORM 对象直接构造


class RepoConnectResponse(BaseModel):
    """RepoConnectResponse."""
    # 连接仓库后的响应
    repo_id: UUID  # 新仓库 ID，必填
    full_name: str  # 全名，必填
    provider: str  # 供应商，必填
    api_base_url: str | None = None  # API 根地址；可空
    clone_url: str | None = None  # 克隆地址；可空
    local_path: str | None = None  # 本地路径；可空
    checkout_mode: str = "managed"  # 检出方式，默认 managed
    default_branch: str | None  # 默认分支；必填（可能 None 也允许）
    connected: bool  # 是否连接成功，必填
    demo_mode: bool  # 是否演示模式，必填
    message: str  # 提示信息，必填
    sync_status: str = "not_started"  # 同步状态，默认未开始
    synced: dict[str, int] | None = None  # 各类已同步数量（如 {"issues": 3}）；可空


class RepoSyncRequest(BaseModel):
    """RepoSyncRequest."""
    # 触发同步的请求体
    sync_issues: bool = True  # 同步 Issue，默认是
    sync_pull_requests: bool = True  # 同步 PR，默认是
    sync_workflow_runs: bool = True  # 同步 CI 运行，默认是
    limit: int = 30  # 本次同步条数上限，默认 30


class RepoSyncResponse(BaseModel):
    """RepoSyncResponse."""
    # 同步结果的响应
    repo_id: UUID  # 仓库 ID，必填
    status: str  # 同步状态，必填
    synced: dict[str, int]  # 各类已同步数量，必填
