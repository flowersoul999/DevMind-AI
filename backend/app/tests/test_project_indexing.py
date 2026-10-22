"""DevMind AI module."""

# os：内存库环境变量（import 数据库模块前）。
import os

# 在 import 数据库相关代码前，把 DATABASE_URL 设成内存库。
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

# SQLAlchemy。
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# 导入模型、知识图谱收集节点、项目索引服务、RAG 检索服务。
from app.db.models import Base, Document, EvidenceItem, Repository
from app.services.knowledge_graph import _collect_nodes
from app.services.project_indexing import discover_project_docs, get_project_index_state, scan_project_index, snooze_project_index
from app.services.rag.retrieval import search_similar_documents


def _db_session():
    """_db_session."""
    # 建内存库 + 建表 + 出会话。
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _write_allowlist_config(project) -> None:
    # 在项目根目录下的 .devflow/index.yml 写一份"白名单"配置：
    # 只索引 README.md 和 docs/architecture/**，排除 docs/course/** 和 "docs/demo code/**"，并开启 include_manifests。
    config_dir = project / ".devflow"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "index.yml").write_text(
        """version: 1
project_docs:
  mode: allowlist
  include:
    - README.md
    - docs/architecture/**
  exclude:
    - docs/course/**
    - "docs/demo code/**"
include_manifests: true.""",
        encoding="utf-8",
    )


def test_scan_project_index_writes_state_and_project_documents(tmp_path, monkeypatch) -> None:
    # 哨兵：项目预扫描阶段不应该依赖向量库，一旦误用就报错。
    def fail_if_vector_runtime_is_used(*_args, **_kwargs):
        """fail_if_vector_runtime_is_used."""
        raise AssertionError("project preflight must not depend on embeddings or Milvus")

    monkeypatch.setattr("app.services.rag.vector_store.embed_texts", fail_if_vector_runtime_is_used)
    monkeypatch.setattr("app.services.rag.vector_store.delete_milvus_documents", fail_if_vector_runtime_is_used)
    monkeypatch.setattr("app.services.rag.vector_store.upsert_milvus_documents", fail_if_vector_runtime_is_used)
    # 在临时目录造一个项目：docs 目录、README、一个架构文档、package.json。
    project = tmp_path / "demo"
    (project / "docs").mkdir(parents=True)
    (project / "README.md").write_text("# Demo\n\nProject overview.", encoding="utf-8")
    (project / "docs" / "ADR-001.md").write_text("# ADR 001\n\nUse FastAPI.", encoding="utf-8")
    (project / "package.json").write_text('{"name":"demo","version":"1.0.0"}', encoding="utf-8")

    db = _db_session()
    # 仓库以 local 模式指向这个目录。
    repo = Repository(owner="local", name="demo", full_name="local/demo", local_path=str(project), checkout_mode="local")
    db.add(repo)
    db.flush()

    # 扫描项目索引。
    state = scan_project_index(db, repo)

    # 断言：索引状态 ready；docs_total=3（README + ADR + package.json 共 3 个被发现）。
    assert state["status"] == "ready"
    assert state["docs_total"] == 3
    # 断言：实际入库文档数 >= 4（README、ADR 作为 project_doc，package.json 作为 manifest，外加一个自动生成的 project_overview）。
    assert state["docs_indexed"] >= 4
    # 断言：摘要里"权威层"覆盖数为 2（README + ADR 被归为 authoritative 权威文档）。
    assert state["summary_json"]["tierCoverage"]["authoritative"] == 2
    # 断言：project_doc 类型入库 >= 2 条，project_manifest 类型 = 1 条（package.json）。
    assert db.query(Document).filter(Document.repo_id == repo.id, Document.source_type == "project_doc").count() >= 2
    assert db.query(Document).filter(Document.repo_id == repo.id, Document.source_type == "project_manifest").count() == 1
    # 取各类代表文档。
    project_doc = db.query(Document).filter(Document.repo_id == repo.id, Document.source_type == "project_doc").first()
    manifest = db.query(Document).filter(Document.repo_id == repo.id, Document.source_type == "project_manifest").one()
    overview = db.query(Document).filter(Document.repo_id == repo.id, Document.source_type == "project_overview").one()
    # 断言：项目文档没有 embedding 维度信息、也没有 embedding 字段——项目文档走"直连检索"，不进向量库。
    assert project_doc is not None and "embedding_dimensions" not in project_doc.meta
    assert not hasattr(project_doc, "embedding")
    # 断言：manifest 的检索模式是 direct（直接检索，不向量化）。
    assert manifest.meta["retrieval_mode"] == "direct"
    assert not hasattr(overview, "embedding")


def test_project_index_state_reports_stale_when_files_change(tmp_path) -> None:
    project = tmp_path / "demo"
    project.mkdir()
    (project / "README.md").write_text("# Demo\n\nProject overview.", encoding="utf-8")

    db = _db_session()
    repo = Repository(owner="local", name="demo", full_name="local/demo", local_path=str(project), checkout_mode="local")
    db.add(repo)
    db.flush()

    # 首次扫描，状态 ready。
    assert scan_project_index(db, repo)["status"] == "ready"
    # 新增一个 CHANGELOG.md（文件变了）。
    (project / "CHANGELOG.md").write_text("# Changelog\n\nInitial release.", encoding="utf-8")

    # 断言：再查索引状态变成 stale（过时）——提示需要重新扫描。
    assert get_project_index_state(db, repo)["status"] == "stale"


def test_allowlist_rescan_removes_course_docs_from_rag_evidence_and_graph(tmp_path) -> None:
    project = tmp_path / "demo"
    (project / "docs" / "architecture").mkdir(parents=True)
    (project / "docs" / "course").mkdir(parents=True)
    (project / "docs" / "demo code").mkdir(parents=True)
    (project / ".data" / "runtime-checkout").mkdir(parents=True)
    (project / "README.md").write_text("# Demo\n\nProject overview.", encoding="utf-8")
    (project / "docs" / "architecture" / "system.md").write_text(
        "# System architecture\n\nFastAPI service boundary.", encoding="utf-8"
    )
    # 一份"课程"文档，里面带 COURSE_ONLY_MARKER，本应被排除。
    (project / "docs" / "course" / "lesson.md").write_text(
        "# Course lesson\n\nCOURSE_ONLY_MARKER should never reach project retrieval.", encoding="utf-8"
    )
    # 一份"教学 demo"文档，带 DEMO_ONLY_MARKER，也应被排除。
    (project / "docs" / "demo code" / "README.md").write_text(
        "# Course demo\n\nDEMO_ONLY_MARKER is teaching material.", encoding="utf-8"
    )
    (project / "package.json").write_text('{"name":"demo","version":"1.0.0"}', encoding="utf-8")
    (project / ".data" / "runtime-checkout" / "package.json").write_text(
        '{"name":"runtime-copy","version":"1.0.0"}', encoding="utf-8"
    )

    db = _db_session()
    repo = Repository(owner="local", name="demo", full_name="local/demo", local_path=str(project), checkout_mode="local")
    db.add(repo)
    db.flush()

    # 没有 allowlist 配置时，自动发现模式会扫到 course 文档。
    assert scan_project_index(db, repo)["status"] == "ready"
    assert any(
        str((doc.meta or {}).get("path") or "").startswith("docs/course/")
        for doc in db.query(Document).filter(Document.repo_id == repo.id, Document.source_type == "project_doc").all()
    )

    # 写入 allowlist 配置后，重新扫描。
    _write_allowlist_config(project)
    state = scan_project_index(db, repo)

    # 收集被索引的路径集合。
    indexed_paths = {
        str((doc.meta or {}).get("path") or "")
        for doc in db.query(Document).filter(Document.repo_id == repo.id, Document.source_type == "project_doc").all()
    }
    # 断言：状态 ready；docs_total=3（README + architecture/system.md + package.json）。
    assert state["status"] == "ready"
    assert state["docs_total"] == 3
    # 断言：索引策略是 allowlist 白名单模式，且排除文件数 = 2（course 和 demo code）。
    assert state["summary_json"]["indexPolicy"]["mode"] == "allowlist"
    assert state["summary_json"]["excludedFiles"] == 2
    # 断言：README 和 architecture/system.md 被收录，而 course / demo code / .data 下的文件都没被收录。
    assert "README.md" in indexed_paths
    assert "docs/architecture/system.md" in indexed_paths
    assert not any(path.startswith("docs/course/") or path.startswith("docs/demo code/") for path in indexed_paths)
    assert not any(path.startswith(".data/") for path in indexed_paths)
    # 断言：证据库（EvidenceItem）里也没有 COURSE_ONLY_MARKER / DEMO_ONLY_MARKER——排除是彻底的，连证据都不留。
    assert not any(
        "COURSE_ONLY_MARKER" in item.content or "DEMO_ONLY_MARKER" in item.content
        for item in db.query(EvidenceItem).filter(EvidenceItem.repo_id == repo.id).all()
    )
    # 断言：用 COURSE_ONLY_MARKER 去检索，全文检索返回空（确实进不去 RAG）。
    assert not search_similar_documents(
        db,
        repo.id,
        "COURSE_ONLY_MARKER",
        retrieval_method="fulltext",
        apply_rerank=False,
    )
    # 断言：用"FastAPI service boundary"能检索到 project_doc（白名单内的正常文档可用）。
    assert any(
        item["source_type"] == "project_doc"
        for item in search_similar_documents(
            db,
            repo.id,
            "FastAPI service boundary",
            retrieval_method="fulltext",
            apply_rerank=False,
        )
    )
    # 断言：知识图谱节点里也没有 course / demo code 路径（排除延伸到图谱层）。
    graph_nodes = _collect_nodes(db, repo.id).values()
    assert not any(
        str((node.get("metadata") or {}).get("path") or "").startswith(("docs/course/", "docs/demo code/"))
        for node in graph_nodes
    )


def test_excluded_files_do_not_make_project_index_stale(tmp_path) -> None:
    project = tmp_path / "demo"
    (project / "docs" / "architecture").mkdir(parents=True)
    (project / "docs" / "course").mkdir(parents=True)
    (project / "README.md").write_text("# Demo\n\nProject overview.", encoding="utf-8")
    architecture = project / "docs" / "architecture" / "system.md"
    architecture.write_text("# Architecture\n\nVersion one.", encoding="utf-8")
    course = project / "docs" / "course" / "lesson.md"
    course.write_text("# Lesson\n\nVersion one.", encoding="utf-8")
    _write_allowlist_config(project)

    db = _db_session()
    repo = Repository(owner="local", name="demo", full_name="local/demo", local_path=str(project), checkout_mode="local")
    db.add(repo)
    db.flush()

    # 首次扫描 ready。
    assert scan_project_index(db, repo)["status"] == "ready"
    # 改了"被排除的" course 文档。
    course.write_text("# Lesson\n\nA much longer excluded course revision.", encoding="utf-8")
    # 断言：索引状态仍是 ready——被排除文件的改动不该让索引变 stale（没意义去重扫）。
    assert get_project_index_state(db, repo)["status"] == "ready"

    # 改了"在白名单内"的 architecture 文档。
    architecture.write_text("# Architecture\n\nVersion two changes project behavior.", encoding="utf-8")
    # 断言：索引状态变成 stale——白名单内文件变了，确实需要重新扫描。
    assert get_project_index_state(db, repo)["status"] == "stale"


def test_discover_project_docs_defaults_to_automatic_mode_without_config(tmp_path) -> None:
    project = tmp_path / "demo"
    (project / "docs" / "course").mkdir(parents=True)
    lesson = project / "docs" / "course" / "lesson.md"
    lesson.write_text("# Lesson\n\nLegacy automatic discovery remains compatible.", encoding="utf-8")

    # 没有 allowlist 配置时，discover_project_docs 走"自动发现"模式，递归找到所有 md。
    assert [item.relative_path for item in discover_project_docs(project)] == ["docs/course/lesson.md"]


def test_snooze_project_index_sets_missing_state_with_cooldown(tmp_path) -> None:
    db = _db_session()
    repo = Repository(owner="local", name="demo", full_name="local/demo", local_path=str(tmp_path), checkout_mode="local")
    db.add(repo)
    db.flush()

    # 把项目索引"休眠"3 天。
    row = snooze_project_index(db, repo, days=3)

    # 断言：状态变成 missing（标记缺失/暂停），且设置了 snoozed_until（冷却到期时间），期间不再自动扫描。
    assert row.status == "missing"
    assert row.snoozed_until is not None
