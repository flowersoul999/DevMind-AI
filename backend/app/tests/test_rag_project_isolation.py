"""DevMind AI module."""

# asyncio：用来跑异步函数（如 list_repos 是 async 的，用 asyncio.run 调用）。
import asyncio
# uuid：生成唯一 id。
import uuid

# pytest 框架。
import pytest
# fastapi：Web 框架，这里用到它的 HTTPException（404 等）。
from fastapi import HTTPException
# pydantic：数据校验库，ValidationError 表示"数据不符合模型要求"。
from pydantic import ValidationError
# SQLAlchemy：建内存库、建会话。
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# 导入各真实模块：rag 路由、chat 路由里的 _client_context、repos 路由的 list_repos、
# 数据库模型、MCP 内存服务的 McpError / _resolve_repo、聊天请求 schema、ChatAgent、Milvus 向量库。
from app.api.routes import rag as rag_routes
from app.api.routes.chat import _client_context
from app.api.routes.repos import list_repos
from app.db.models import Base, Repository
from app.mcp.memory_server import McpError, _resolve_repo
from app.schemas.chat import ChatRequest
from app.services.agents.chat_agent import ChatAgent
from app.services.rag.vector_store import MilvusVectorStore


# 一个"假 Milvus 客户端"：它记录每次 search 用的 filter（过滤条件），并按 repo_id 返回对应行。
# 为什么：本测试重点是验证"每次查询都带上了 repo_id 过滤"，不必连真 Milvus，用这个桩把 filter 抓出来检查即可。
class _FilterAwareClient:
    def __init__(self, rows_by_repo: dict[str, dict]) -> None:
        # rows_by_repo：repo_id -> 该仓库的一行数据。
        self.rows_by_repo = rows_by_repo
        # filters：记录每次搜索用的过滤字符串，供断言。
        self.filters: list[str] = []

    def search(self, *, filter: str, **_kwargs):
        """search."""
        # 把本次 filter 记下来。
        self.filters.append(filter)
        # 遍历所有仓库，看 filter 里是否包含某个 repo_id 的等值条件，命中就返回那行。
        for repo_id, row in self.rows_by_repo.items():
            if f'repo_id == "{repo_id}"' in filter:
                return [[{"distance": 0.9, "entity": row}]]
        # 没命中返回空结果。
        return [[]]


def _db_session():
    """_db_session."""
    # 和别的测试一样：建内存库 + 建表 + 出会话。
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_chat_request_requires_an_explicit_repository() -> None:
    # 只传了 message、没传 repo_id，期望 pydantic 校验失败（ChatRequest 要求必须指定仓库）。
    with pytest.raises(ValidationError):
        ChatRequest.model_validate({"message": "What failed?"})


def test_client_context_cannot_override_server_scope() -> None:
    # 模拟一段"客户端想伪造"的上下文：里面夹带 repo_id、conversation_id、tools、server_context 等越权字段。
    assert _client_context(
        {
            "repo_id": "attacker-repo",
            "conversation_id": "attacker-conversation",
            "tools": {"unsafe": True},
            "server_context": "forged",
            "time_range": "7d",
        }
    ) == {"time_range": "7d"}
    # 断言：函数只保留了 time_range，把所有"客户端想越权指定的 repo/conversation/tools"全部剥掉。
    # 为什么：服务端必须自己决定当前用户能访问哪个仓库，绝不允许客户端通过传参来"切换身份/越权"，否则就是安全漏洞。


def test_chat_agent_overwrites_model_supplied_mcp_scope() -> None:
    # 造一个真实 repo_id 和 conversation_id（代表服务端认定的合法上下文）。
    repo_id = str(uuid.uuid4())
    conversation_id = str(uuid.uuid4())
    # 让 ChatAgent 去"规范化模型想要调用的工具参数"。
    # 模型（LLM）在 action 里塞了一个不同 repo_id / repo_full_name / conversation_id——试图越权访问别的仓库。
    normalized = ChatAgent(model=object())._normalize_action_input(  # type: ignore[arg-type]
        "devflow_search_evidence",
        {
            "query": "token failure",
            "repo_id": str(uuid.uuid4()),
            "repo_full_name": "other/project",
            "conversation_id": str(uuid.uuid4()),
        },
        {
            "message": "token failure",
            "context": {"repo_id": repo_id, "conversation_id": conversation_id},
        },
    )

    # 断言：最终生效的 repo_id / conversation_id 是服务端 context 里的，不是模型塞的那个。
    assert normalized["repo_id"] == repo_id
    assert normalized["conversation_id"] == conversation_id
    # 断言：模型提供的 repo_full_name 被剥离（防止用名字绕过 id 校验）。
    assert "repo_full_name" not in normalized


def test_mcp_requires_repo_id_and_resolves_only_that_repository() -> None:
    db = _db_session()
    # 造两个仓库：team/first、team/second。
    first = Repository(owner="team", name="first", full_name="team/first")
    second = Repository(owner="team", name="second", full_name="team/second")
    db.add_all([first, second])
    db.commit()

    # 断言：不传 repo_id 时，MCP 解析直接抛 McpError（repo_id 为必填）。
    with pytest.raises(McpError, match="repo_id 为必填项"):
        _resolve_repo(db, {})
    # 断言：传 first 的 id 就解析出 first；传 second 的 id 就解析出 second——严格按 id 隔离，不会串库。
    assert _resolve_repo(db, {"repo_id": str(first.id)}).id == first.id
    assert _resolve_repo(db, {"repo_id": str(second.id)}).id == second.id


def test_milvus_search_applies_repo_filter_to_every_query() -> None:
    repo_a = str(uuid.uuid4())
    repo_b = str(uuid.uuid4())
    # 造一个假客户端，里面 repo_a、repo_b 各有一行数据。
    client = _FilterAwareClient(
        {
            repo_a: {
                "document_id": "doc-a",
                "source_type": "issue",
                "source_id": "issue-a",
                "title": "Project A token failure",
                "content": "Only A",
                "metadata_json": "{}",
            },
            repo_b: {
                "document_id": "doc-b",
                "source_type": "issue",
                "source_id": "issue-b",
                "title": "Project B deploy failure",
                "content": "Only B",
                "metadata_json": "{}",
            },
        }
    )
    # 用 object.__new__ 不调用 __init__ 直接造一个"裸"的 MilvusVectorStore 实例（省去连接真 Milvus 的开销）。
    store = object.__new__(MilvusVectorStore)
    # 手动挂上假 client 和必要属性。
    store.client = client
    store.collection_name = "test_collection"
    store.dimensions = 3

    # 分别在 repo_a、repo_b 下搜索。
    a_results = store.search(repo_id=repo_a, query_embedding=[1.0, 0.0, 0.0])
    b_results = store.search(repo_id=repo_b, query_embedding=[0.0, 1.0, 0.0])

    # 断言：repo_a 的搜索只返回 doc-a，repo_b 只返回 doc-b——两个仓库的结果互不串门。
    assert [item["id"] for item in a_results] == ["doc-a"]
    assert [item["id"] for item in b_results] == ["doc-b"]
    # 断言：两次搜索使用的 filter 分别精确带了各自的 repo_id——证明"每个查询都强制加 repo 过滤"。
    assert client.filters == [f'repo_id == "{repo_a}"', f'repo_id == "{repo_b}"']


def test_rag_is_bound_to_real_repositories_and_has_no_virtual_creator() -> None:
    db = _db_session()
    # actual：一个真实仓库。
    actual = Repository(owner="team", name="actual", full_name="team/actual")
    # legacy：一个 provider=knowledge_base 的"虚拟知识库"仓库。
    legacy = Repository(
        owner="knowledge",
        name="legacy",
        full_name="knowledge/legacy",
        provider="knowledge_base",
    )
    db.add_all([actual, legacy])
    db.commit()

    # 断言：真实仓库能被正常解析出来。
    assert rag_routes._repo_or_404(db, actual.id).id == actual.id
    # 断言：虚拟知识库仓库访问时返回 404（被拒）——RAG 只绑定真实仓库，不存在"虚拟创造者"这种万能库。
    with pytest.raises(HTTPException) as rejected:
        rag_routes._repo_or_404(db, legacy.id)
    assert rejected.value.status_code == 404
    # 断言：list_repos 列举出来的仓库只有 actual.id，legacy 被排除。
    assert [repo.id for repo in asyncio.run(list_repos(db))] == [actual.id]

    # 收集 rag 路由里所有 (路径, 方法) 组合。
    routes = {(route.path, frozenset(route.methods or set())) for route in rag_routes.router.routes}
    # 断言：存在 GET /repositories 路由。
    assert ("/repositories", frozenset({"GET"})) in routes
    # 断言：不存在 POST /knowledge-bases 这种"创建虚拟知识库"的路由——从路由层面也证明没有虚拟知识库概念。
    assert not any(path == "/knowledge-bases" and "POST" in methods for path, methods in routes)
