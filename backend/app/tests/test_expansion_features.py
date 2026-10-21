"""DevMind AI module."""

# os：设置环境变量（指定内存数据库）。
import os
# datetime / timedelta / timezone：构造时间戳、计算"30 天前"等。
from datetime import datetime, timedelta, timezone
# UUID：构造固定 uuid（用于在数据库里定位某条 PR 快照记录）。
from uuid import UUID

# pytest。
import pytest
# SQLAlchemy。
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# 必须在 import 数据库相关模块前，把 DATABASE_URL 设成内存库。
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

# 导入各种"扩展能力"的真实路由/服务：动作草稿、CI 上下文、代码图、PR 上下文、工作区周报、代码分析、代码搜索、向量库缓存、PR 快照清理。
from app.api.routes.action_drafts import confirm_action_draft, create_action_draft
from app.api.routes.ci import _augment_ci_context_with_snapshot, _build_ci_analysis_context
from app.api.routes.code_graph import search_code_graph
from app.api.routes.pull_requests import _augment_pr_context_with_snapshot, _build_pr_analysis_context
from app.api.routes.workspaces import create_workspace, multi_repo_report
from app.core.config import settings
from app.db.models import Base, CodeRelation, CodeSymbol, Issue, PRFile, PRReviewComment, PullRequest, Repository, User, WorkflowRun
from app.schemas.action_drafts import ActionDraftCreateRequest
from app.schemas.workspaces import MultiRepoReportRequest, WorkspaceCreateRequest
from app.services.code_analysis import _sync_code_graph
from app.services.code_search import search_repository_code
from app.services.rag.vector_store import reset_milvus_store_cache
from app.services.worktree_manager import _sync_pr_snapshot_analysis, cleanup_pr_snapshots


def _db_session():
    """_db_session."""
    # 建内存库 + 建表 + 出会话（测试固定套路）。
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _user(db):
    """_user."""
    # 造一个 owner 用户并写入库（flush 拿到 id）。
    user = User(name="owner", role="owner")
    db.add(user)
    db.flush()
    return user


def _repo(db, name: str = "demo") -> Repository:
    # 造一个仓库（managed 模式、带克隆地址），写入并 flush。
    repo = Repository(owner="local", name=name, full_name=f"local/{name}", checkout_mode="managed", clone_url=f"https://github.com/local/{name}.git")
    db.add(repo)
    db.flush()
    return repo


@pytest.mark.asyncio
async def test_send_report_action_draft_confirm_records_execution() -> None:
    db = _db_session()
    user = _user(db)
    repo = _repo(db)

    # 创建一条"发送周报"的动作草稿。
    draft = await create_action_draft(
        ActionDraftCreateRequest(repo_id=repo.id, draft_type="send_report", target_type="repository", title="Weekly report", content="Report body"),
        db=db,
        user=user,
    )

    # 用户确认这条草稿——触发执行。
    result = await confirm_action_draft(draft.id, db=db, user=user)

    # 断言：草稿状态变成 executed（已执行）。
    assert result["draft"].status == "executed"
    # 断言：执行结果里记录了 status=recorded（执行动作被落库记录）。
    assert result["draft"].execution_result["status"] == "recorded"
    # 断言：产生了审计 id（audit_id），说明这次执行被留痕，可事后追溯。
    assert result["audit_id"]


@pytest.mark.asyncio
async def test_multi_repo_report_aggregates_workspace_repositories(monkeypatch) -> None:
    # 关掉 Milvus（本测试不需要向量库），并清掉向量库缓存避免串味。
    monkeypatch.setattr(settings, "milvus_enabled", False)
    reset_milvus_store_cache()
    db = _db_session()
    user = _user(db)
    repo_a = _repo(db, "api")
    repo_b = _repo(db, "web")
    # 给 repo_a 造 1 个 open issue；给 repo_b 造 1 个 open PR 和 1 条失败 CI。
    db.add(Issue(repo_id=repo_a.id, number=1, title="Fix auth", state="open", created_at=datetime.now(timezone.utc)))
    db.add(PullRequest(repo_id=repo_b.id, number=2, title="UI update", state="open", created_at=datetime.now(timezone.utc)))
    db.add(WorkflowRun(repo_id=repo_b.id, github_run_id=3, name="test", status="completed", conclusion="failure", created_at=datetime.now(timezone.utc)))
    db.flush()

    # 建一个工作区，把 repo_a、repo_b 都拉进来。
    workspace = await create_workspace(WorkspaceCreateRequest(name="team", repo_ids=[repo_a.id, repo_b.id]), db=db, user=user)
    # 生成多仓库周报（统计区间就是今天）。
    result = await multi_repo_report(
        MultiRepoReportRequest(
            workspace_id=workspace.id,
            start_date=datetime.now(timezone.utc).date(),
            end_date=datetime.now(timezone.utc).date(),
        ),
        db=db,
        user=user,
    )

    # 断言：聚合指标正确——2 个仓库、1 个 open issue、1 个 open PR、1 条失败 CI。
    assert result["metrics"]["repos"] == 2
    assert result["metrics"]["open_issues"] == 1
    assert result["metrics"]["open_prs"] == 1
    assert result["metrics"]["failed_ci"] == 1
    # 断言：报告 markdown 里包含标题"多仓库研发周报"。
    assert "多仓库研发周报" in result["report_markdown"]


@pytest.mark.asyncio
async def test_code_graph_indexes_symbols_and_relations(tmp_path) -> None:
    db = _db_session()
    user = _user(db)
    repo = _repo(db)
    # 在临时目录造一个真实 python 文件：handler 调用 helper，还有一个 Worker 类。
    project = tmp_path / "repo"
    project.mkdir()
    (project / "service.py").write_text(
        "from lib import helper\n\n"
        "def handler():\n"
        "    return helper()\n\n"
        "class Worker:\n"
        "    pass\n",
        encoding="utf-8",
    )

    # 同步代码图：把符号和关系抽取进数据库，返回索引到的符号数量。
    count = _sync_code_graph(db, repo, project, branch="main", commit_sha="abc123")
    db.commit()
    # 搜一下 handler 这个符号。
    result = await search_code_graph(repo.id, q="handler", limit=20, db=db, _user=user)

    # 断言：索引到了 2 个符号（handler、Worker——注意 helper 是从 lib 导入的，不在本文件定义，所以不计入）。
    assert count == 2
    # 断言：搜到的第一个符号名是 handler。
    assert result["symbols"][0].name == "handler"
    # 断言：关系里存在一条 calls（调用）关系——handler 调用了 helper。
    assert any(item["relation_type"] == "calls" for item in result["relations"])


def test_pr_snapshot_live_search_is_pr_scoped(tmp_path) -> None:
    db = _db_session()
    repo = _repo(db)
    project = tmp_path / "repo"
    project.mkdir()
    (project / "feature.py").write_text("def feature_flag():\n    return True\n", encoding="utf-8")

    # 在"PR 快照"模式下搜 feature_flag，并带上 PR 相关的元数据（分支/commit/pr_id 等）。
    snapshot_results = search_repository_code(
        repo,
        "feature_flag",
        checkout_path=project,
        metadata={
            "branch": "feature/pr-snapshot",
            "commit_sha": "abc123",
            "pr_id": "pr-123",
            "pr_number": 12,
            "checkout_kind": "pr_snapshot",
        },
    )

    # 断言：有结果。
    assert snapshot_results
    metadata = snapshot_results[0]["metadata"]
    # 断言：结果元数据完整保留了 PR 快照信息（checkout_kind / branch / commit_sha / pr_id / pr_number）——说明这次搜索是"限定在这个 PR 范围内"的。
    assert metadata["checkout_kind"] == "pr_snapshot"
    assert metadata["branch"] == "feature/pr-snapshot"
    assert metadata["commit_sha"] == "abc123"
    assert metadata["pr_id"] == "pr-123"
    assert metadata["pr_number"] == 12
    # 断言：来源还是 workspace_file（工作区文件）。
    assert snapshot_results[0]["source_type"] == "workspace_file"


def test_pr_snapshot_analysis_writes_code_graph_without_documents(tmp_path, monkeypatch) -> None:
    # 关 Milvus + 清缓存（快照分析不依赖向量库）。
    monkeypatch.setattr(settings, "milvus_enabled", False)
    reset_milvus_store_cache()
    db = _db_session()
    repo = _repo(db)
    # 造一个 open 的 PR（编号 12）。
    pr = PullRequest(repo_id=repo.id, number=12, title="Feature flag", state="open", head_branch="feature/pr-snapshot")
    db.add(pr)
    db.flush()
    project = tmp_path / "repo"
    project.mkdir()
    (project / "feature.py").write_text("def handler():\n    return helper()\n\ndef helper():\n    return True\n", encoding="utf-8")

    # 对 PR 快照做代码图分析（只索引符号/关系，不写文档到向量库）。
    indexed = _sync_pr_snapshot_analysis(db, repo, pr, project, "feature/pr-snapshot", "abc123")
    db.commit()

    # 断言：索引到 2 个符号（handler、helper）。
    assert indexed == 2
    # 断言：数据库里确实有 2 条归属于这个 PR（pr_id + pr_number 都对得上）的符号记录。
    assert db.query(CodeSymbol).filter(CodeSymbol.pr_id == pr.id, CodeSymbol.pr_number == 12).count() == 2


def test_pr_analysis_context_prefers_snapshot_docs_and_graph(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "milvus_enabled", False)
    reset_milvus_store_cache()
    db = _db_session()
    repo = _repo(db)
    pr = PullRequest(repo_id=repo.id, number=4, title="Use handler", state="open", head_branch="feature/handler")
    db.add(pr)
    db.flush()
    db.add(PRFile(pr_id=pr.id, filename="service.py", status="modified", additions=4, deletions=1, patch="handler()"))
    db.flush()
    project = tmp_path / "repo"
    project.mkdir()
    (project / "service.py").write_text("def handler():\n    return helper()\n\ndef helper():\n    return True\n", encoding="utf-8")
    # 先做 PR 快照分析，把符号图建好。
    _sync_pr_snapshot_analysis(db, repo, pr, project, "feature/handler", "abc123")
    db.commit()
    db.refresh(pr)

    # 准备一个上下文字典和计数字典，再构造一份"快照就绪"信息。
    context = {"code_references": []}
    counts = {"code": 0}
    snapshot = {"status": "ready", "snapshot_name": "PR 分支快照", "pr_id": str(pr.id), "pr_number": pr.number, "branch": pr.head_branch, "commit_sha": "abc123", "indexed_symbols": 2, "snapshot_path": str(project)}
    # 用快照去"增强"PR 分析上下文。
    _augment_pr_context_with_snapshot(db, pr, "handler service.py", context, counts, snapshot)

    # 断言：快照代码被计入（snapshot_code > 0）。
    assert counts["snapshot_code"] > 0
    # 断言：代码引用里带了正确的 pr_id（说明来自该 PR 的快照）。
    assert context["code_references"][0]["metadata"]["pr_id"] == str(pr.id)
    # 断言：代码图影响分析里，第一个符号是 handler。
    assert context["code_graph_impact"]["symbols"][0]["name"] == "handler"


def test_pr_analysis_keeps_patch_out_of_semantic_queries(monkeypatch) -> None:
    monkeypatch.setattr(settings, "milvus_enabled", False)
    reset_milvus_store_cache()
    db = _db_session()
    repo = _repo(db)
    pr = PullRequest(
        repo_id=repo.id,
        number=8,
        title="Retry token refresh",
        body="Retry once after a refresh failure.",
        state="open",
    )
    db.add(pr)
    db.flush()
    db.add_all(
        [
            # PR 文件：patch 内容是一段"敏感补丁"，只应在代码检索里出现，绝不该进语义检索。
            PRFile(
                pr_id=pr.id,
                filename="auth.py",
                status="modified",
                additions=2,
                deletions=1,
                patch="SENSITIVE_PATCH_ONLY_FOR_CODE_LOOKUP",
            ),
            # PR 评审评论：一句普通建议。
            PRReviewComment(
                pr_id=pr.id,
                body="Keep the retry idempotent.",
                path="auth.py",
                line=42,
                author="reviewer",
            ),
        ]
    )
    db.commit()
    db.refresh(pr)
    # 记录两类检索分别被调用了什么 query，方便事后检查敏感串有没有泄露。
    semantic_calls: list[str] = []
    code_calls: list[str] = []

    # 假语义检索：把 query 记下来，返回空。
    def fake_search(_db, _repo_id, query, source_type=None, _limit=5, **kwargs):
        """fake_search."""
        """fake_search."""
        semantic_calls.append(query)
        return []

    # 假代码检索：把 query 记下来，返回空。
    def fake_code_search(_repo, query, **_kwargs):
        """fake_code_search."""
        """fake_code_search."""
        code_calls.append(query)
        return []

    # 用 monkeypatch 把真实检索函数替换成这两个记录器。
    monkeypatch.setattr("app.api.routes.pull_requests.search_similar_documents", fake_search)
    monkeypatch.setattr("app.api.routes.pull_requests.search_repository_code", fake_code_search)

    # 构建 PR 分析上下文，拿到 code_query。
    code_query, _context, _counts = _build_pr_analysis_context(db, pr)

    # 断言：敏感 patch 串出现在 code_query 里（代码检索可以用它）。
    assert "SENSITIVE_PATCH_ONLY_FOR_CODE_LOOKUP" in code_query
    # 断言：代码检索的某次调用里也包含敏感串。
    assert any("SENSITIVE_PATCH_ONLY_FOR_CODE_LOOKUP" in query for query in code_calls)
    # 断言：语义检索确实被调用了（非空）。
    assert semantic_calls
    # 断言：语义检索的每一条 query 都不含敏感 patch 串——敏感 diff 不会拿去向量检索/上传，避免泄密。
    assert all("SENSITIVE_PATCH_ONLY_FOR_CODE_LOOKUP" not in query for query in semantic_calls)
    # 断言：语义检索里确实包含了评审评论那句话（普通文本可以进语义检索）。
    assert any("Keep the retry idempotent." in query for query in semantic_calls)


def test_ci_context_uses_related_pr_snapshot_docs_and_graph(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "milvus_enabled", False)
    reset_milvus_store_cache()
    db = _db_session()
    repo = _repo(db)
    pr = PullRequest(repo_id=repo.id, number=7, title="Fix service", state="open", head_branch="feature/ci")
    db.add(pr)
    db.flush()
    project = tmp_path / "repo"
    project.mkdir()
    (project / "service.py").write_text("def test_target():\n    return build()\n\ndef build():\n    return True\n", encoding="utf-8")
    # 先做 PR 快照分析（建符号图）。
    _sync_pr_snapshot_analysis(db, repo, pr, project, "feature/ci", "abc123")
    # 造一条失败的 CI 运行，日志提到 service.py test_target 失败。
    run = WorkflowRun(repo_id=repo.id, github_run_id=99, name="tests", status="completed", conclusion="failure", logs_text="ERROR service.py test_target failed")
    db.add(run)
    db.commit()

    context = {"code_references": [], "related_files_from_logs": ["service.py"]}
    counts = {"code": 0}
    snapshot = {"status": "ready", "snapshot_name": "PR 分支快照", "pr_id": str(pr.id), "pr_number": pr.number, "branch": pr.head_branch, "commit_sha": "abc123", "indexed_symbols": 2, "snapshot_path": str(project)}
    # 用快照增强 CI 上下文。
    _augment_ci_context_with_snapshot(db, run, "service.py test_target failed", context, counts, snapshot)

    # 断言：快照代码被计入。
    assert counts["snapshot_code"] > 0
    # 断言：代码引用标记了 checkout_kind=pr_snapshot（来自 PR 快照）。
    assert context["code_references"][0]["metadata"]["checkout_kind"] == "pr_snapshot"
    # 断言：代码图影响分析第一个符号是 test_target（CI 失败对应的测试函数）。
    assert context["code_graph_impact"]["symbols"][0]["name"] == "test_target"


def test_ci_analysis_redacts_semantic_queries_but_keeps_local_code_context(monkeypatch) -> None:
    monkeypatch.setattr(settings, "milvus_enabled", False)
    reset_milvus_store_cache()
    db = _db_session()
    repo = _repo(db)
    # 造一个真实样式的 GitHub token（ghp_ + 32 个 a）作为敏感信息。
    secret = "ghp_" + "a" * 32
    run = WorkflowRun(
        repo_id=repo.id,
        github_run_id=101,
        name="tests",
        status="completed",
        conclusion="failure",
        logs_text=f"TOKEN={secret}\nERROR auth.py test_refresh failed",
    )
    db.add(run)
    db.commit()
    semantic_calls: list[str] = []
    code_calls: list[str] = []

    def fake_search(_db, _repo_id, query, source_type=None, _limit=5, **kwargs):
        semantic_calls.append(query)
        return []

    def fake_code_search(_repo, query, **_kwargs):
        code_calls.append(query)
        return []

    monkeypatch.setattr("app.api.routes.ci.search_similar_documents", fake_search)
    monkeypatch.setattr("app.api.routes.ci.search_repository_code", fake_code_search)

    code_query, _context, _counts = _build_ci_analysis_context(db, run)

    # 断言：token 出现在本地 code_query 里（本地代码上下文里要能定位到含密行，方便人工排查）。
    assert secret in code_query
    # 断言：代码检索调用里也带了 token（本地检索允许）。
    assert code_calls and secret in code_calls[0]
    # 断言：语义检索被调用了。
    assert semantic_calls
    # 断言：语义检索的每条 query 都不含原始 token——token 被脱敏了。
    assert all(secret not in query for query in semantic_calls)
    # 断言：语义检索的每条 query 里都出现了 [REDACTED]（脱敏占位符），证明脱敏动作确实发生。
    assert all("[REDACTED]" in query for query in semantic_calls)


def test_cleanup_pr_snapshots_removes_old_dirs_and_code_graph(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "milvus_enabled", False)
    reset_milvus_store_cache()
    # 用临时目录作为检出根目录。
    checkout_root = tmp_path / "checkouts"
    monkeypatch.setattr(settings, "repo_checkout_dir", str(checkout_root))
    db = _db_session()
    repo = _repo(db)
    # 造一个"老旧的 PR 快照目录"，里面放一个标记 updated_at 是 30 天前的快照元数据文件。
    snapshot_dir = checkout_root / "worktrees" / str(repo.id) / "pr-1-local__demo"
    snapshot_dir.mkdir(parents=True)
    (snapshot_dir / ".devflow-pr-snapshot.json").write_text(
        '{"updated_at": "'
        + (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        + '", "pr_id": "00000000-0000-0000-0000-000000000123"}',
        encoding="utf-8",
    )
    pr_id = UUID("00000000-0000-0000-0000-000000000123")
    # 在数据库里造一条属于该 PR 的符号记录和一条调用关系记录。
    symbol = CodeSymbol(
        repo_id=repo.id,
        path="service.py",
        name="handler",
        kind="function",
        language="py",
        start_line=1,
        pr_id=pr_id,
    )
    db.add(symbol)
    db.flush()
    db.add(CodeRelation(repo_id=repo.id, source_symbol_id=symbol.id, source_name="handler", target_name="helper", relation_type="calls", path="service.py", pr_id=pr_id))
    db.commit()

    # 执行清理：max_age_hours=1 表示"超过 1 小时的快照就删"（我们那是 30 天前的，必然被清）。
    result = cleanup_pr_snapshots(db, repo, max_age_hours=1)

    # 断言：清理了 1 个快照目录。
    assert result["removed"] == 1
    # 断言：快照目录真的从磁盘删掉了。
    assert not snapshot_dir.exists()
    # 断言：数据库里该仓库的符号、关系记录也被级联清掉（不留孤儿数据）。
    assert db.query(CodeSymbol).filter(CodeSymbol.repo_id == repo.id).count() == 0
    assert db.query(CodeRelation).filter(CodeRelation.repo_id == repo.id).count() == 0
