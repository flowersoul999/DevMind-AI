"""DevMind AI module."""

from collections.abc import Iterable
from typing import Literal


# 检索模式只有两种：
# - "rag"：走「检索增强生成」管线，把资料切块、转向量、存进 Milvus，问答时语义召回。
# - "direct"：结构化、实时的数据（如团队成员、项目清单）留在各自的数据表 / API 里直接查，不做向量化。
RetrievalMode = Literal["rag", "direct"]

# 为什么：RAG 适合「可复用、非结构化的项目知识」，比如 Issue、PR、CI 运行记录、文档、知识库文件、笔记。
# 这些内容的价值在于「按需语义召回」，所以放进向量库。
# 结构化、实时的事实（项目概览、清单、成员、会话记忆等）则保留在原表，避免把本来就能精确查询的数据硬塞进向量库。
RAG_SOURCE_TYPES = frozenset(
    {
        "issue",            # GitHub Issue
        "pull_request",     # 合并请求（PR）
        "workflow_run",     # CI / 工作流运行记录
        "project_doc",      # 项目文档
        "knowledge_file",   # 用户上传的知识库文件
        "memory_note",      # 笔记 / 记忆
    }
)

# 这些类型不走 RAG，而是「直接检索」：它们是结构化、实时、需要精确字段的事实，不靠语义相似度。
DIRECT_SOURCE_TYPES = frozenset(
    {
        "project_overview",     # 项目概览
        "project_manifest",     # 项目清单 / 清单文件
        "team_member",          # 团队成员
        "weekly_report",        # 周报
        "chat_session",         # 聊天会话
        "conversation_memory",  # 对话记忆
        "thread_memory",        # 会话线程记忆
    }
)


def retrieval_mode(source_type: str) -> RetrievalMode:
    # 判断一个「资料来源类型」到底走 RAG 还是走 direct 检索
    if source_type in RAG_SOURCE_TYPES:
        return "rag"  # 属于可向量化的集合，就走 RAG
    return "direct"   # 否则直接检索


def normalize_source_types(source_types: Iterable[str | None] | None) -> list[str]:
    # 把调用方传进来的一串来源类型「洗干净」：去掉空值、去掉首尾空格、保持原顺序去重。
    # 为什么：外部传参经常带 None、空串或重复项，先统一清洗一遍，后面过滤 / 检索时才不会出错。
    normalized = [str(source_type).strip() for source_type in (source_types or []) if source_type and str(source_type).strip()]
    # dict.fromkeys 利用字典键唯一来去重，同时保留出现顺序（比 set 更可控）
    return list(dict.fromkeys(normalized))


def should_vectorize(source_type: str) -> bool:
    # 这个类型是否需要转成向量写进 Milvus？只有走 RAG 模式的才需要。
    return retrieval_mode(source_type) == "rag"


def is_searchable_source(source_type: str) -> bool:
    # 这个类型是否可以被检索召回？语义同上，纯属给调用方一个更语义化的名字。
    return retrieval_mode(source_type) == "rag"


def policy_metadata(source_type: str) -> dict[str, object]:
    # 给每条资料附加「策略元数据」：记录它属于哪种检索模式、是否启用 RAG。
    # 为什么：资料落库时把策略写进 metadata，后续检索、调试、审计时一眼就能看出这条记录当初是怎么被处理的。
    mode = retrieval_mode(source_type)
    return {
        "retrieval_mode": mode,
        "rag_enabled": mode == "rag",
    }
