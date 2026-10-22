"""DevMind AI module."""

# re：正则；json：读写 JSON；Path：路径。
import re
import json
from pathlib import Path

# pytest。
import pytest
# fastapi 的后台任务（用于异步跑评测）。
from fastapi import BackgroundTasks
# langchain 的消息类型（用来伪造模型返回）。
from langchain_core.messages import AIMessage
# SQLAlchemy。
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# 导入配置、模型、评测路由、评测请求 schema、ChatAgent、ragas 评测运行器、suite 加载、基础评测运行器、向量库缓存。
from app.core.config import settings
from app.db.models import Base, Conversation, Document, EvalRun, Repository
from app.api.routes.evaluations import _eval_comparison, _issue_markdown
from app.schemas.evals import EvalRunRequest
from app.services.agents.chat_agent import ChatAgent
from app.services.evals.ragas_runner import (
    CORE_RAGAS_METRICS,
    RagEvalCase,
    _default_cases,
    _hard_rules,
    _evaluation_project_context,
    _ensure_ragas_compatibility,
    _split_faithfulness_response,
    rescore_rag_result,
    run_rag_quality_eval,
)
from app.services.evals.suites import list_eval_suites, load_eval_suite
from app.services.evals.runner import run_basic_eval
from app.services.rag.vector_store import reset_milvus_store_cache


@pytest.mark.asyncio
async def test_basic_eval_runner_returns_real_metrics() -> None:
    # 跑基础评测（真实逻辑，不 mock），返回一个指标字典。
    result = await run_basic_eval()
    # 断言：各核心指标都拿到了合理值——issue 分类准确、工具调用准确、RAG 前 3 命中率 1.0、CI pytest 失败检测 1.0。
    assert result["issue_classification_accuracy"] > 0
    assert result["tool_call_accuracy"] > 0
    assert result["rag_hit_rate_at_3"] == 1.0
    assert result["ci_pytest_failure_detection"] == 1.0
    # 断言：报告 markdown 里包含"Eval Report"和"CI pytest failure detection"标题（报告生成正常）。
    assert "Eval Report" in result["report_markdown"]
    assert "CI pytest failure detection" in result["report_markdown"]


def test_ragas_collections_api_imports_with_current_langchain() -> None:
    # 确保和当前 langchain 版本兼容的 ragas 指标能正常导入。
    _ensure_ragas_compatibility()
    from ragas.metrics.collections import AnswerRelevancy, ContextPrecision, ContextRecall, Faithfulness

    # 断言：四种核心指标对象都能取到（导入没崩）。
    assert all([AnswerRelevancy, ContextPrecision, ContextRecall, Faithfulness])


def test_faithfulness_chunking_keeps_all_answer_content() -> None:
    # 造一个很长的"事实回答"，中间那段重复 90 遍。
    response = "第一条事实。\n" + ("第二条很长的事实内容" * 90) + "！\n最后一条事实。"

    # 按每段最多 120 字切分（用于逐段判定忠诚度 faithfulness）。
    chunks = _split_faithfulness_response(response, max_chars=120)

    # 断言：切出超过 2 段。
    assert len(chunks) > 2
    # 断言：每段都非空且不超过 120 字。
    assert all(0 < len(chunk) <= 120 for chunk in chunks)
    # 断言：把所有段拼起来（去掉空白）和原文（去空白）完全一致——切分没有丢字、没重复。
    assert re.sub(r"\s+", "", "".join(chunks)) == re.sub(r"\s+", "", response)


def test_default_ragas_cases_fall_back_to_structured_repository_data() -> None:
    # 建内存库，造一个仓库 + 一条 issue。
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    repo = Repository(owner="local", name="fallback", full_name="local/fallback")
    db.add(repo)
    db.flush()
    from app.db.models import Issue

    issue = Issue(repo_id=repo.id, number=7, title="Refresh token fails", body="Retry once with an idempotency key", state="open", labels=["bug"])
    db.add(issue)
    db.flush()

    # 没给自定义 case 时，_default_cases 应回退到"仓库里的结构化数据"（如 issue）来生成评测用例。
    cases = _default_cases(db, str(repo.id))

    # 断言：生成了 1 个 case，id 是 issue-7，来源是 issue，且期望命中的 source 就是这条 issue。
    assert len(cases) == 1
    assert cases[0].id == "issue-7"
    assert cases[0].source_type == "issue"
    assert cases[0].expected_source_ids == [str(issue.id)]


def test_fixed_eval_suite_resolves_repository_sources(tmp_path: Path) -> None:
    # 建内存库，造仓库 + 一份知识文档（01_repository_facts.md）。
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    repo = Repository(owner="DemoOrg", name="DevFlow-AI", full_name="DemoOrg/DevFlow-AI")
    db.add(repo)
    db.flush()
    source_id = __import__("uuid").uuid4()
    document = Document(
        repo_id=repo.id,
        source_type="knowledge_file",
        source_id=source_id,
        title="Facts",
        content="默认分支是 main。",
        meta={"filename": "01_repository_facts.md"},
    )
    db.add(document)
    db.flush()
    # 在临时目录写一个 eval suite（baseline.json），里面 case 引用 source_file=01_repository_facts.md。
    suite_dir = tmp_path / "evals"
    suite_dir.mkdir()
    (suite_dir / "baseline.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "name": "fixed-baseline",
                "label": "固定基线",
                "repository_names": ["DevFlow-AI"],
                "recommended": True,
                "cases": [
                    {
                        "id": "default-branch",
                        "question": "默认分支是什么？",
                        "reference": "main",
                        "source_file": "01_repository_facts.md",
                        "must_include": ["main"],
                        "answer_regex": "^main$",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # 列出可用 suite，并加载这个 suite。
    suites = list_eval_suites(db, repo.id, suite_dir=suite_dir)
    loaded = load_eval_suite(db, repo.id, "fixed-baseline", suite_dir=suite_dir)

    # 断言：suite 就绪、是推荐项、有 sha256 指纹。
    assert suites[0]["ready"] is True
    assert suites[0]["recommended"] is True
    assert suites[0]["sha256"]
    # 断言：case 的 expected_source_ids 解析到了文档 id（以及知识文件 source_id），must_include / answer_regex 都对。
    assert loaded.cases[0].expected_source_ids == [str(document.id), str(source_id)]
    assert loaded.cases[0].must_include == ["main"]
    assert loaded.cases[0].answer_regex == "^main$"


def test_hard_rules_enforce_required_forbidden_and_output_format() -> None:
    # 构造一个 hard rule case：必须包含"当前 Git checkout"、禁止"GitHub 仓库"、答案必须匹配正则。
    case = RagEvalCase(
        id="authority",
        question="权威来源？",
        reference="当前 Git checkout。",
        expected_source_ids=["source-1"],
        must_include=["当前 Git checkout"],
        must_not_include=["GitHub 仓库"],
        answer_regex=r"^当前 Git checkout[。.]?$",
    )
    # 实际回答是"GitHub 仓库"（既没包含必含短语，又命中了禁止短语，且不匹配正则）。
    result = {"answer": "GitHub 仓库", "tool_calls": [], "citations": []}
    hard = _hard_rules(case, result, [{"source_id": "source-1"}], ["evidence"])

    # 断言：硬规则没过；缺的短语、禁止出现的短语、正则是否匹配，都被精确报告出来。
    assert hard["passed"] is False
    assert hard["missing_phrases"] == ["当前 Git checkout"]
    assert hard["forbidden_phrases"] == ["GitHub 仓库"]
    assert hard["answer_pattern_matched"] is False


def test_eval_project_context_matches_frontend_team_and_memory_shape() -> None:
    # 建内存库，造团队文档（星哥，前端）和记忆笔记文档。
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    repo = Repository(owner="local", name="context", full_name="local/context")
    db.add(repo)
    db.flush()
    db.add_all(
        [
            Document(
                repo_id=repo.id,
                source_type="team_member",
                source_id=__import__("uuid").uuid4(),
                title="团队成员：星哥",
                content="name: 星哥",
                meta={
                    "name": "星哥",
                    "role": "前端开发工程师",
                    "strengths": "擅长 Electron",
                    "tech_stack": "JavaScript、Electron、TypeScript",
                },
            ),
            Document(
                repo_id=repo.id,
                source_type="memory_note",
                source_id=__import__("uuid").uuid4(),
                title="桌面化上下文",
                content="Issue #1 是桌面化需求。",
                meta={},
            ),
        ]
    )
    db.flush()

    # 取"评测用的项目上下文"。
    context = _evaluation_project_context(db, repo.id)

    # 断言：team_members 和 project_memory 的形状（字段名 techStack 等）和前端约定一致，方便前端直接渲染。
    assert context["team_members"] == [
        {
            "name": "星哥",
            "role": "前端开发工程师",
            "strengths": "擅长 Electron",
            "techStack": "JavaScript、Electron、TypeScript",
        }
    ]
    assert context["project_memory"] == [
        {"title": "桌面化上下文", "content": "Issue #1 是桌面化需求。"}
    ]


# 一个假答案模型：bind_tools 返回自己；ainvoke 永远返回固定回答（模拟 LLM 输出）。
class EvalAnswerModel:
    def bind_tools(self, tools):
        """bind_tools."""
        return self

    async def ainvoke(self, messages):
        """ainvoke."""
        return AIMessage(content="Retries use an idempotency key and only retry once.")


# 一个假打分器：记录每次调用参数，返回一套假指标。
class RecordingScorer:
    version = "test-ragas-0.4"
    judge_model = "recording-judge"

    def __init__(self) -> None:
        self.calls = []

    async def score(self, **kwargs):
        """score."""
        """score."""
        self.calls.append(kwargs)
        return (
            {
                "context_precision": 0.95,
                "context_recall": 0.96,
                "faithfulness": 0.97,
                "answer_relevancy": 0.94,
                "agent_goal_accuracy": 1.0,
            },
            {},
        )


# 一个"只补打缺失指标"的假打分器：第一次 score 记下 metric_names，只返回 faithfulness。
class RetryScorer:
    version = "test-ragas-0.4"
    judge_model = "retry-judge"

    def __init__(self) -> None:
        self.metric_names = None

    async def score(self, **kwargs):
        self.metric_names = kwargs["metric_names"]
        return ({"faithfulness": 0.99}, {})


@pytest.mark.asyncio
async def test_rescore_reuses_saved_agent_evidence_and_only_retries_missing_metrics() -> None:
    scorer = RetryScorer()
    # 一个"上一次评测"的结果：case 里 ragas 已有 4 项，只有 faithfulness 报错（TimeoutError）。
    source_result = {
        "target": {"repo_id": "repo-1"},
        "evaluation_set": {"name": "fixed", "sha256": "abc"},
        "pipeline": {"top_k": 5},
        "retrieval": {"recall_at_k": 1.0, "precision_at_k": 1.0, "mrr": 1.0, "ndcg": 1.0},
        "performance": {"cases": 1, "avg_latency_ms": 100},
        "cases": [
            {
                "id": "case-1",
                "question": "问题",
                "reference": "答案",
                "response": "答案",
                "retrieved_contexts": ["答案"],
                "retrieved_sources": [{"source_id": "source-1"}],
                "tool_calls": [],
                "tool_observations": [],
                "expected_tool_calls": [],
                "hard_rules": {"passed": True, "source_hit": True, "contexts_present": True, "answer_present": True},
                "ragas": {
                    "context_precision": 1.0,
                    "context_recall": 1.0,
                    "answer_relevancy": 1.0,
                    "agent_goal_accuracy": 1.0,
                },
                # 只有 faithfulness 这次超时失败了。
                "ragas_errors": {"faithfulness": "TimeoutError: "},
            }
        ],
    }

    # 重新打分：应当复用已有证据，只重试缺失的 faithfulness。
    rescored = await rescore_rag_result(source_result, eval_id="eval-1", scorer=scorer)

    # 断言：打分器只被要求补 faithfulness 这一项。
    assert scorer.metric_names == {"faithfulness"}
    # 断言：运行模式是 rescore，标明重评了哪个 eval；agent 证据被复用（agent_reused=True）。
    assert rescored["run_mode"] == "rescore"
    assert rescored["rescore_of"] == "eval-1"
    assert rescored["performance"]["agent_reused"] is True
    # 断言：faithfulness 错误被清空、分值补成 0.99，整体通过。
    assert rescored["cases"][0]["ragas_errors"] == {}
    assert rescored["cases"][0]["ragas"]["faithfulness"] == 0.99
    assert rescored["evaluation_complete"] is True
    assert rescored["status"] == "passed"


def test_eval_comparison_requires_same_baseline_and_reports_regressions() -> None:
    # 基准和被比较项共用同一份"评测集+ragas 版本+pipeline"信息（这样才可比）。
    shared = {
        "evaluation_set": {"name": "fixed", "sha256": "abc"},
        "ragas": {"version": "0.4.3", "judge_model": "judge"},
        "pipeline": {"top_k": 5},
        "evaluation_complete": True,
    }
    # 基线：faithfulness 0.95，通过。
    base = EvalRun(
        name="base",
        result_json={
            **shared,
            "passed": True,
            "quality_status": "passed",
            "summary_metrics": {"faithfulness": 0.95},
            "cases": [{"id": "case-1", "hard_rules": {"passed": True}, "ragas_errors": {}}],
        },
    )
    # 候选：faithfulness 降到 0.80，hard_rules 失败，整体失败。
    candidate = EvalRun(
        name="candidate",
        result_json={
            **shared,
            "passed": False,
            "quality_status": "failed",
            "summary_metrics": {"faithfulness": 0.80},
            "cases": [
                {"id": "case-1", "hard_rules": {"passed": False}, "ragas_errors": {}},
            ],
        },
    )

    # 对比两者。
    comparison = _eval_comparison(base, candidate)

    # 断言：可比，且 faithfulness 跌了 -0.15；有 1 个 case 退步；发布门槛没过（因为 candidate 失败）。
    assert comparison["comparable"] is True
    assert comparison["metric_deltas"] == [
        {"name": "faithfulness", "base": 0.95, "candidate": 0.8, "delta": -0.15}
    ]
    assert comparison["regressed_cases"] == 1
    assert comparison["release_gate_passed"] is False

    # 把 candidate 的评测集 hash 改成不一样，再比。
    candidate.result_json = {
        **candidate.result_json,
        "evaluation_set": {"name": "fixed", "sha256": "different"},
    }
    incompatible = _eval_comparison(base, candidate)
    # 断言：hash 不一致则不可比，并给出原因"评测集 Hash 不一致"。
    assert incompatible["comparable"] is False
    assert "评测集 Hash 不一致" in incompatible["incompatibility_reasons"]


def test_issue_draft_markdown_contains_reproduction_and_actions() -> None:
    # 构造一个"失败评测运行"，含 faithfulness 超时错误和一条诊断（重试异常评分）。
    eval_run = EvalRun(
        name="failed-run",
        result_json={
            "status": "error",
            "quality_status": "failed",
            "evaluation_set": {"name": "fixed", "sha256": "abc"},
            "ragas": {
                "version": "0.4.3",
                "judge_model": "judge",
                "metrics": {
                    "faithfulness": {"score": None, "threshold": 0.9},
                    "tool_call_accuracy": {"score": None, "threshold": 0.9},
                },
            },
            "cases": [
                {
                    "id": "case-1",
                    "question": "问题",
                    "reference": "标准答案",
                    "response": "错误答案",
                    "ragas_errors": {"faithfulness": "TimeoutError"},
                    "diagnoses": [
                        {"category": "judge", "title": "Judge 异常", "action": "仅重试异常评分"}
                    ],
                }
            ],
        },
    )

    # 生成"用来开 issue 的 markdown"。
    markdown = _issue_markdown(eval_run)

    # 断言：markdown 含"复现环境""验收标准"章节，含错误 TimeoutError 和诊断动作"仅重试异常评分"；
    #       但不含 tool_call_accuracy（它没报错，不该出现在 issue 里凑数）。
    assert "## 复现环境" in markdown
    assert "TimeoutError" in markdown
    assert "仅重试异常评分" in markdown
    assert "tool_call_accuracy" not in markdown
    assert "## 验收标准" in markdown


@pytest.mark.asyncio
async def test_rag_quality_eval_runs_real_chatagent_and_rolls_back_eval_conversation(monkeypatch) -> None:
    # 关 Milvus。
    monkeypatch.setattr(settings, "milvus_enabled", False)
    reset_milvus_store_cache()
    # 建内存库，造仓库 + 一份"重试策略"文档。
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    repo = Repository(owner="local", name="ragas", full_name="local/ragas")
    db.add(repo)
    db.flush()
    source_id = __import__("uuid").uuid4()
    document = Document(
        repo_id=repo.id,
        source_type="project_doc",
        source_id=source_id,
        title="Retry policy",
        content="Authentication retries use an idempotency key and retry only once.",
        meta={},
    )
    db.add(document)
    db.commit()
    scorer = RecordingScorer()
    progress_events = []

    # 跑真实 RAG 质量评测：用 EvalAnswerModel 当模型、RecordingScorer 当打分器。
    result = await run_rag_quality_eval(
        db,
        str(repo.id),
        cases=[
            RagEvalCase(
                id="retry-policy",
                question="How should authentication retries work?",
                reference="Use an idempotency key and retry only once.",
                expected_source_ids=[str(document.id), str(source_id)],
                source_type="project_doc",
            )
        ],
        scorer=scorer,
        # agent_factory：用我们固定的假模型造 ChatAgent（保证回答可预测）。
        agent_factory=lambda: ChatAgent(model=EvalAnswerModel()),  # type: ignore[arg-type]
        progress_callback=lambda event: progress_events.append(event),
    )

    # 断言：结果是 chatagent_ragas 类型、目标 ChatAgent、状态通过、硬门槛过、召回率 1.0。
    assert result["kind"] == "chatagent_ragas"
    assert result["target"]["name"] == "ChatAgent"
    assert result["status"] == "passed"
    assert result["hard_gate"]["passed"] is True
    assert result["retrieval"]["recall_at_k"] == 1.0
    # 断言：ragas 版本是假打分器的版本，且所有核心指标都打出了分数（非 None）。
    assert result["ragas"]["version"] == "test-ragas-0.4"
    assert all(result["ragas"]["metrics"][name]["score"] is not None for name in CORE_RAGAS_METRICS)
    # 断言：打分器收到的 response 以"Retries use an idempotency key"开头（模型确实输出了），
    #       且 retrieved_contexts 非空（确实检索到了上下文）。
    assert scorer.calls[0]["response"].startswith("Retries use an idempotency key")
    assert scorer.calls[0]["retrieved_contexts"]
    # 断言：进度事件按 preparing -> chatagent -> ragas -> case_complete -> finalizing 顺序推进。
    assert [event["stage"] for event in progress_events] == [
        "preparing",
        "chatagent",
        "ragas",
        "case_complete",
        "finalizing",
    ]
    # 断言：评测期间临时建的"evaluation"会话被回滚掉了（不留垃圾数据）。
    assert db.query(Conversation).filter(Conversation.status == "evaluation").count() == 0


@pytest.mark.asyncio
async def test_ragas_api_persists_summary_metrics(monkeypatch) -> None:
    # 导入评测路由模块。
    from app.api.routes import evaluations as evaluation_routes

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    repo = Repository(owner="local", name="api", full_name="local/api")
    db.add(repo)
    db.commit()

    # 假 run 函数：校验参数后，返回一个 failed 的结果（带 summary_metrics）。
    async def fake_run(db_arg, repo_id, **kwargs):
        """fake_run."""
        assert db_arg is db
        assert repo_id == str(repo.id)
        assert kwargs["run_judge"] is True
        await kwargs["progress_callback"](
            {
                "stage": "ragas",
                "message": "scoring",
                "completed_cases": 0,
                "total_cases": 1,
            }
        )
        return {
            "kind": "chatagent_ragas",
            "status": "failed",
            "passed": False,
            "summary_metrics": {"faithfulness": 0.62, "context_recall": 0.91},
            "ragas": {"available": True, "metrics": {}},
            "cases": [],
        }

    # 把真实的 run_rag_quality_eval 和 SessionLocal 都换成假的，避免真的跑重评测。
    monkeypatch.setattr(evaluation_routes, "run_rag_quality_eval", fake_run)
    monkeypatch.setattr(evaluation_routes, "SessionLocal", lambda: db)
    background_tasks = BackgroundTasks()
    # 触发"提交一次评测运行"的 API（会进后台任务队列）。
    response = await evaluation_routes.run_rag_eval(
        EvalRunRequest(name="api-ragas", repo_id=repo.id, run_judge=True),
        background_tasks=background_tasks,
        db=db,
        _user=object(),
    )
    # 先列一下历史（此时应该还是 queued 状态）。
    queued_history = await evaluation_routes.list_evals(db=db, _user=object())

    # 断言：接口返回 queued，历史第一条也是 queued（已入队）。
    assert response.status == "queued"
    assert queued_history[0]["status"] == "queued"

    # 真正执行后台任务（把上面 queued 的评测跑掉）。
    await background_tasks()
    history = await evaluation_routes.list_evals(db=db, _user=object())
    detail = await evaluation_routes.get_eval(response.eval_id, db=db, _user=object())

    # 断言：库里只有 1 条 EvalRun；跑完后状态变成 failed，且 summary_metrics 被正确持久化（faithfulness 0.62 等）。
    assert db.query(EvalRun).count() == 1
    assert history[0]["status"] == "failed"
    assert history[0]["metrics"] == {"faithfulness": 0.62, "context_recall": 0.91}
    assert detail["status"] == "failed"
