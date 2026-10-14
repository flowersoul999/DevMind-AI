"""
code_graph.py —— 代码图谱（Code Graph）查询的数据契约（Schema 层）

本文件服务于「代码符号检索 / 代码关系图谱」接口：
- CodeSymbolResponse：代码里一个「符号」（函数、类、变量等）的元数据；
- CodeGraphSearchResponse：一次代码图谱搜索的结果（匹配到的符号 + 关系 + 文档）。

代码图谱把仓库里的函数/类及其调用关系结构化，便于做「跳转到定义」「影响面分析」。."""

from datetime import datetime  # datetime：时间戳类型
from uuid import UUID  # UUID：唯一 ID

from pydantic import BaseModel, Field  # BaseModel：基类；Field：字段约束/别名


class CodeSymbolResponse(BaseModel):
    """CodeSymbolResponse."""
    # 单个代码符号的响应结构
    id: UUID  # 符号记录 ID，必填
    repo_id: UUID  # 所属仓库 ID，必填
    path: str  # 符号所在文件路径，必填
    name: str  # 符号名（如函数名/类名），必填
    kind: str  # 符号种类（function/class/variable 等），必填
    language: str  # 编程语言（python/typescript 等），必填
    start_line: int | None = None  # 符号起始行号；可空
    end_line: int | None = None  # 符号结束行号；可空
    branch: str | None = None  # 所在分支名；可空
    commit_sha: str | None = None  # 对应的代码提交哈希；可空
    pr_id: UUID | None = None  # 若来自某次 PR，记录 PR ID；可空
    pr_number: int | None = None  # PR 的编号；可空
    metadata: dict = Field(default_factory=dict, alias="meta")  # 附加元数据；alias="meta" 表示前端可用 "meta" 这个名字传入
    # 为什么用 alias：数据库列名叫 meta，但 meta 在 Python 里含义模糊，用 metadata 更清晰，同时兼容旧字段名
    created_at: datetime  # 记录创建时间，必填

    model_config = {"from_attributes": True, "populate_by_name": True}  # 为什么：from_attributes 支持 ORM；populate_by_name 允许用字段真名 metadata 也能赋值


class CodeGraphSearchResponse(BaseModel):
    """CodeGraphSearchResponse."""
    # 一次代码图谱搜索的响应
    query: str  # 用户搜索的关键词，必填
    symbols: list[CodeSymbolResponse]  # 命中的符号列表，必填
    relations: list[dict]  # 符号间关系（调用/继承等）列表，必填
    documents: list[dict]  # 相关文档列表，必填
