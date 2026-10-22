"""DevMind AI module."""

# uuid：生成唯一 id。
import uuid
# SimpleNamespace：用来伪造仓库对象，免得为了测试去构造完整数据库模型。
from types import SimpleNamespace

# 导入真实的代码搜索服务：把自然语言问题拆成检索词（query_terms），并在本地/仓库代码里搜（search_checkout_code / search_repository_code）。
from app.services.code_search import query_terms, search_checkout_code, search_repository_code


def test_query_terms_prioritizes_code_identifiers() -> None:
    # 一句中文问题，里面夹着代码标识符 refreshAccessToken 和文件路径 auth/service.py。
    terms = query_terms("为什么 refreshAccessToken 在 auth/service.py 里失败？")

    # 断言：拆词后保留了 refreshaccesstoken（函数名，统一小写方便匹配）。
    assert "refreshaccesstoken" in terms
    # 断言：文件路径 auth/service.py 也被当作一个检索词保留下来。
    # 为什么：用户问的是"这段代码/这个文件"，把代码标识符和路径当成关键词，检索才精准，而不是只拿中文词去搜。
    assert "auth/service.py" in terms


def test_search_checkout_code_reads_current_files_without_database_index(tmp_path) -> None:
    # 在临时目录里造真实的文件结构：src/auth.py 和一个 .env（含密钥）。
    (tmp_path / "src").mkdir()
    source = tmp_path / "src" / "auth.py"
    source.write_text(
        "def refresh_access_token():\n"
        "    return rotate_credentials()\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("REFRESH_ACCESS_TOKEN=secret\n", encoding="utf-8")

    # 直接在"当前检出的代码目录"里搜，不需要数据库索引（实时读磁盘）。
    results = search_checkout_code(tmp_path, "refresh_access_token", limit=5)

    # 断言：搜到了结果。
    assert results
    # 断言：结果来源类型是 workspace_file（工作区文件）。
    assert results[0]["source_type"] == "workspace_file"
    # 断言：来源 id 是相对路径 src/auth.py。
    assert results[0]["source_id"] == "src/auth.py"
    # 断言：检索模式标记为 workspace（实时工作区检索，而非数据库索引）。
    assert results[0]["metadata"]["retrieval_mode"] == "workspace"
    # 断言：结果里没有 .env 文件——敏感配置文件被排除，避免把密钥泄露进检索结果。
    assert all(item["source_id"] != ".env" for item in results)


def test_search_checkout_code_respects_worktree_subdirectory(tmp_path) -> None:
    # 造两层目录：.github/workflows 和 src。
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / "src").mkdir()
    (tmp_path / ".github" / "workflows" / "test.yml").write_text("name: backend-tests\n", encoding="utf-8")
    (tmp_path / "src" / "test.py").write_text("backend_tests = True\n", encoding="utf-8")

    # 指定只在 relative_path=".github/workflows" 子目录里搜 "backend-tests"。
    results = search_checkout_code(
        tmp_path,
        "backend-tests",
        relative_path=".github/workflows",
    )

    # 断言：有结果，而且命中的恰好只有 .github/workflows/test.yml——子目录范围限制真的生效了，不会越界搜到 src。
    assert results and {item["source_id"] for item in results} == {".github/workflows/test.yml"}


def test_search_repository_code_uses_configured_local_checkout(tmp_path) -> None:
    # 临时目录下写个 service.py。
    (tmp_path / "service.py").write_text("def handle_payment_webhook():\n    pass\n", encoding="utf-8")
    # 伪造仓库对象，带上 local_path（指向临时目录）和 full_name。
    repo = SimpleNamespace(id=uuid.uuid4(), local_path=str(tmp_path), full_name="local/demo")

    # 通过"仓库对象"去搜，函数内部会用自己的 local_path 定位代码。
    results = search_repository_code(repo, "handle_payment_webhook")

    # 断言：有结果，且 metadata 里记录了正确的 repo_id（说明它确实用到了传入仓库的配置）。
    assert results and results[0]["metadata"]["repo_id"] == str(repo.id)


def test_search_checkout_code_observes_file_changes_without_reindex(tmp_path) -> None:
    # 先写一个 service.py，里面是 old_handler。
    source = tmp_path / "service.py"
    source.write_text("def old_handler():\n    pass\n", encoding="utf-8")
    # 第一轮：能搜到 old_handler（说明实时读磁盘，根本没建索引）。
    assert search_checkout_code(tmp_path, "old_handler")

    # 把文件改成 current_handler（模拟用户刚改了代码，还没重新索引）。
    source.write_text("def current_handler():\n    pass\n", encoding="utf-8")

    # 第二轮：能搜到 current_handler。
    assert search_checkout_code(tmp_path, "current_handler")
    # 断言：旧的 old_handler 现在搜不到了（返回空列表）——证明它是实时读文件，改完立刻反映，不需要手动 reindex。
    assert search_checkout_code(tmp_path, "old_handler") == []
