"""
pull_requests.py —— Pull Request（PR/合并请求）相关接口的数据契约（Schema 层）

本文件服务于「查看 PR 列表/详情」和「对 PR 做智能评审」两个接口：
- PRFileResponse / PRReviewCommentResponse：PR 里变更的文件、以及行内评审评论；
- PullRequestResponse：一次 PR 的完整响应（聚合上面的文件与评论）；
- PRAnalysisResponse：把一次 PR 评审结果（PRReviewOutput）打包返回。

analysis.py 的 PRReviewOutput 负责「评审结论长啥样」，本文件负责「PR 本身及其附件长啥样」。."""

from datetime import datetime  # datetime：时间戳
from uuid import UUID  # UUID：唯一 ID

from pydantic import BaseModel  # BaseModel：模型基类

from app.schemas.analysis import PRReviewOutput  # 复用 analysis.py 里定义好的 PR 评审输出结构


class PRFileResponse(BaseModel):
    """PRFileResponse."""
    # PR 中一个变更文件的响应结构
    id: UUID  # 文件记录 ID，必填
    filename: str  # 文件名/路径，必填
    status: str  # 变更状态（added/modified/deleted 等），必填
    additions: int  # 新增行数，必填
    deletions: int  # 删除行数，必填
    patch: str | None = None  # 具体 diff 文本；可空（大文件可能不发）

    model_config = {"from_attributes": True}  # 为什么：允许用 ORM 对象直接构造，便于数据库行→JSON


class PRReviewCommentResponse(BaseModel):
    """PRReviewCommentResponse."""
    # PR 中一条行内评审评论的响应结构
    id: UUID  # 评论 ID，必填
    body: str | None = None  # 评论正文；可空
    path: str | None = None  # 评论所在文件路径；可空
    line: int | None = None  # 评论所在行号；可空
    author: str | None = None  # 评论作者；可空
    created_at: datetime | None = None  # 创建时间；可空
    updated_at: datetime | None = None  # 更新时间；可空

    model_config = {"from_attributes": True}  # 为什么：允许用 ORM 对象直接构造


class PullRequestResponse(BaseModel):
    """PullRequestResponse."""
    # 一次 PR 的完整响应结构
    id: UUID  # PR 记录 ID，必填
    repo_id: UUID  # 所属仓库 ID，必填
    number: int  # PR 在 GitHub 上的编号，必填
    title: str  # 标题，必填
    body: str | None = None  # 描述正文；可空
    state: str  # 状态（open/closed/merged 等），必填
    author: str | None = None  # 作者；可空
    base_branch: str | None = None  # 目标分支（合入到哪）；可空
    head_branch: str | None = None  # 来源分支（从哪合出）；可空
    merged_at: datetime | None = None  # 合入时间；未合入则为 None
    created_at: datetime | None = None  # 创建时间；可空
    updated_at: datetime | None = None  # 更新时间；可空
    files: list[PRFileResponse] = []  # 变更文件列表，默认空
    review_comments: list[PRReviewCommentResponse] = []  # 行内评审评论列表，默认空

    model_config = {"from_attributes": True}  # 为什么：允许用 ORM 对象直接构造


class PRAnalysisResponse(BaseModel):
    """PRAnalysisResponse."""
    # PR 分析结果的响应结构
    analysis_id: UUID | str  # 本次分析记录 ID，必填
    result: PRReviewOutput  # 具体分析内容，复用 analysis.py 的 PRReviewOutput，必填
