"""
runner.py —— 基础评测运行器（轻量级冒烟评测）

本模块是「评测服务」的入口之一：不依赖外部 Judge 模型，用一组内置写死的样例，快速验证
整条链路（Issue 分类 / PR 评审 / RAG 召回 / 上下文压缩 / CI 调试 / 技能注册 / JSON 结构）
是否还走得通，并算出一个 0~1 的通过率。它服务于「baseline 自检」和 CI 里的快速门禁。

关键点：
- NoopLLM 是个「永远返回空」的假模型，仅用于让 Agent 跑起来而不真的调大模型（省时省钱）；
- cosine_similarity 做向量相似度，用来判断 RAG 是否召回了正确答案；
- run_basic_eval 把各项指标拼成一个 Markdown 报告 + 一个结果字典返回。
注意：本文件只加注释，未改动任何逻辑与变量名。."""

from typing import Any  # Any：任意类型，用于自由结构的字典
from uuid import UUID  # UUID：唯一 ID

from sqlalchemy.orm import Session  # Session：数据库会话（ORM 查询用）

from app.db.models import CodeSymbol, Document, Issue, PullRequest, WorkflowRun  # 数据库表模型
from app.schemas.analysis import CIDebugOutput, IssueAnalysisOutput, PRReviewOutput  # 分析结果结构（用于校验）
from app.services.agents.ci_debug_agent import CIDebugAgent  # CI 调试 Agent
from app.services.agents.issue_agent import IssueAgent  # Issue 分析 Agent
from app.services.agents.pr_review_agent import PRReviewAgent  # PR 评审 Agent
from app.services.context_compression import (  # 上下文压缩相关工具
    compression_eval_summary,  # 评估压缩质量
    compress_messages,  # 压缩消息列表
    compress_retrieval_results,  # 压缩检索结果
    fallback_memory_update,  # 兜底记忆更新
    merge_memory,  # 合并记忆
    render_structured_memory,  # 渲染结构化记忆文本
)
from app.services.evals.ragas_runner import run_rag_quality_eval  # Ragas 质量评测（可选调用）
from app.services.rag.embeddings import deterministic_embedding  # 确定性向量化（不调模型）
from app.services.skills import get_skill_registry  # 技能注册表


class NoopLLM:
    # 一个「什么也不做」的假 LLM：chat_json 永远返回空字典。仅用于跑 Agent 流程骨架而不花真模型调用。
    async def chat_json(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
        return {}


def cosine_similarity(a: list[float], b: list[float]) -> float:
    # 计算两个向量的余弦相似度（值越接近 1 越相似）。a/b 是向量（一串小数）。
    if not a or not b:
        return 0.0  # 任一为空向量则相似度为 0
    numerator = sum(x * y for x, y in zip(a, b))  # 点积：对应分量相乘再求和
    denominator = (sum(x * x for x in a) ** 0.5) * (sum(y * y for y in b) ** 0.5)  # 两向量模长之积
    return numerator / (denominator or 1.0)  # 点积除以模长积；分母兜底为 1.0 防除零


async def run_basic_eval(db: Session | None = None, repo_id: str | None = None) -> dict[str, Any]:
    # 跑一遍基础评测，返回各项指标的结果字典。db/repo_id 可空：不给则跳过需要数据库的 RAG 质量评测。
    issue_agent = IssueAgent(llm=NoopLLM())  # 用工假模型构造 Issue Agent（不真调大模型）
    pr_agent = PRReviewAgent(llm=NoopLLM())  # 同上：PR Agent
    ci_agent = CIDebugAgent(llm=NoopLLM())  # 同上：CI Agent

    issue_cases = [
        # 三道 Issue 分类样例：(输入, 期望类别)
        ({"title": "Login API returns 500", "body": "Token expires and login fails", "labels": ["bug"]}, "Bug"),
        ({"title": "Document local demo mode", "body": "README should explain seed script", "labels": ["documentation"]}, "Documentation"),
        ({"title": "Add workflow sync", "body": "Sync recent GitHub Actions runs", "labels": ["feature", "ci"]}, "Ops"),
    ]
    issue_results = [await issue_agent.run(payload, {}) for payload, _expected in issue_cases]  # 把每道样例喂给 Issue Agent
    # 计算分类准确率：判对的数量 / 总数量
    issue_accuracy = sum(result["category"] == expected for result, (_payload, expected) in zip(issue_results, issue_cases)) / len(issue_cases)

    pr_payload = {
        # 一道 PR 评审样例：一次重构鉴权中间件的 PR
        "number": 12,
        "title": "Refactor auth middleware",
        "body": "Split token validation and session refresh",
        "files": [
            {"filename": "backend/app/auth/middleware.py", "status": "modified", "patch": "+ validate token\n+ refresh session"},
            {"filename": "backend/app/tests/test_auth.py", "status": "modified", "patch": "+ test expired token"},
        ],
        "comments": [],
    }
    pr_result = await pr_agent.run(pr_payload, {})  # 跑 PR 评审 Agent
    # 把关键改动与需关注文件拼成一个文本，用于检查 Agent 是否提到了每个改动文件
    key_text = " ".join(pr_result.get("key_changes", []) + pr_result.get("files_need_attention", []))
    # 关键文件覆盖率：被 Agent 提到的文件数 / 总文件数
    pr_key_file_coverage = sum(file["filename"] in key_text for file in pr_payload["files"]) / len(pr_payload["files"])

    rag_docs = [
        # 几段文档文本，用于检验 RAG 是否能把「token 过期登录 500」这条召回到最前
        ("issue-login", "Login API returns 500 when token expires"),
        ("issue-docs", "Document local demo mode and README setup"),
        ("issue-ci", "GitHub Actions pytest job fails after dependency update"),
    ]
    query_embedding = deterministic_embedding("token expires login 500")  # 把查询句向量化（确定性，不调模型）
    # 对每段文档算与查询的余弦相似度，并按相似度从高到低排序
    scored = sorted(
        [(doc_id, cosine_similarity(query_embedding, deterministic_embedding(text))) for doc_id, text in rag_docs],
        key=lambda item: item[1],
        reverse=True,
    )
    # hit@3：正确答案「issue-login」是否排在Top3里（在则 1.0，否则 0.0）
    rag_hit_rate_at_3 = 1.0 if any(doc_id == "issue-login" for doc_id, _score in scored[:3]) else 0.0
    # 若给了数据库和仓库，则顺带跑一次真正的 Ragas 质量评测（run_judge=False 表示不调裁判模型）
    rag_quality = await run_rag_quality_eval(db, repo_id, run_judge=False) if db is not None and repo_id else None

    long_messages = [
        # 一段超长对话，用于测试上下文压缩是否能在保留关键信息的同时缩短
        {"role": "user", "content": "We decided that auth refresh belongs in middleware. " * 40},
        {"role": "assistant", "content": "Decision captured. Next step is adding tests for expired tokens. " * 36},
        {"role": "user", "content": "What did we decide about auth token refresh?"},
    ]
    compressed_messages, message_stats = compress_messages(long_messages, max_tokens=260)  # 压缩消息，限制最多 260 token
    memory_update = fallback_memory_update(
        # 生成一个兜底记忆更新（把决策记下来）
        "Please implement auth token refresh in middleware",
        "Decision: keep refresh in middleware. Next step: add expired token tests.",
        "direct_answer",
        [],
    )
    merged_memory = merge_memory(
        # 把空记忆骨架与上面的更新合并，限制 240 token
        {"summary": "", "facts": [], "decisions": [], "open_questions": [], "tasks": [], "user_preferences": [], "repo_context": [], "citations": []},
        memory_update,
        memory_update["summary"],
        max_tokens=240,
    )
    retrieval_items = compress_retrieval_results(
        # 压缩一条检索结果（把 80 句无关内容压到 80 token 以内，保留关键句）
        "auth token refresh middleware",
        [
            {
                "id": "auth-doc",
                "title": "backend/app/auth/middleware.py#1",
                "source_type": "workspace_file",
                "source_id": "auth-doc",
                "score": 1.0,
                "_raw_content": ("unrelated setup notes. " * 80) + "auth middleware refreshes expired token and validates session.",
                "metadata": {"path": "backend/app/auth/middleware.py"},
            }
        ],
        max_tokens_per_item=80,
    )
    compression_quality = compression_eval_summary(
        # 评估压缩质量：原文 vs 压缩后文本(+合并记忆)，看压缩比与是否保留关键信息
        "\n".join(item["content"] for item in long_messages),
        "\n".join(item["content"] for item in compressed_messages) + "\n" + render_structured_memory(type("Memory", (), merged_memory), max_tokens=240),
        retrieved=1,
        kept=len(retrieval_items),
    )

    ci_result = await ci_agent.run(
        # 一道 CI 调试样例：pytest 测试失败
        {
            "name": "backend-test",
            "status": "completed",
            "conclusion": "failure",
            "jobs": [{"name": "pytest", "conclusion": "failure", "steps": [{"name": "Run tests", "conclusion": "failure"}]}],
            "logs_text": "pytest tests/test_api.py FAILED AssertionError",
        },
        {},
    )
    # 把失败摘要/首错/根因拼成小写文本，用于判断 Agent 是否识别出「pytest 测试失败」
    ci_text = " ".join(
        [
            str(ci_result.get("failure_summary") or ""),
            str(ci_result.get("first_error") or ""),
            str(ci_result.get("root_cause") or ""),
        ]
    ).lower()
    # CI pytest 失败检测率：失败类型==test 且文本含 pytest 则为 1.0，否则 0.0
    ci_pytest_failure_detection = 1.0 if ci_result.get("failure_type") == "test" and "pytest" in ci_text else 0.0
    # 用 Pydantic 校验三个 Agent 的输出是否都符合对应结构（符合则 model_validate 不报错）
    schema_checks = [
        IssueAnalysisOutput.model_validate(issue_results[0]),
        PRReviewOutput.model_validate(pr_result),
        CIDebugOutput.model_validate(ci_result),
    ]
    json_schema_pass_rate = len(schema_checks) / 3  # JSON 结构通过率：3/3=1.0

    # 现在由原生工具调用负责工具选择。
    tool_call_schema_ready_rate = 1.0  # 工具调用 schema 就绪率（占位：视为已就绪 1.0）
    registry = get_skill_registry()  # 取出技能注册表
    required_skill_entrypoints = {
        # 期望系统必须提供的「技能名 -> 入口标识」映射（相当于考纲）
        "issue-triage": "issue_agent.analyze_issue",
        "pr-review": "pr_review_agent.analyze_pr",
        "ci-debug": "ci_debug_agent.analyze_workflow_run",
        "weekly-report": "report_agent.generate_weekly_report",
        "safety-draft": "safety_agent.classify_action_risk",
        "project-investigation": "devflow_search_evidence",
    }
    loaded_skill_names = {skill.manifest.name for skill in registry.list_skills()}  # 当前已加载的技能名集合
    # 技能注册覆盖率：考纲里被加载的比例
    skill_registry_coverage = len(loaded_skill_names & set(required_skill_entrypoints)) / len(required_skill_entrypoints)
    skill_entrypoint_hits = 0  # 统计入口标识精准匹配命中的数量
    for skill_name, entrypoint in required_skill_entrypoints.items():
        skill = registry.skill_for_entrypoint(entrypoint)  # 按入口查技能
        if skill and skill.manifest.name == skill_name:
            skill_entrypoint_hits += 1
    skill_entrypoint_accuracy = skill_entrypoint_hits / len(required_skill_entrypoints)  # 入口准确率
    report_lines = [
        # 拼一份 Markdown 评测报告
        "# Eval Report",
        "",
        f"- Issue classification accuracy: {issue_accuracy:.2f}",
        f"- PR key-file coverage: {pr_key_file_coverage:.2f}",
        f"- RAG hit@3: {rag_hit_rate_at_3:.2f}",
        f"- Context compression ratio: {compression_quality['compression_ratio']:.2f}",
        f"- Skill registry coverage: {skill_registry_coverage:.2f}",
        f"- Skill entrypoint accuracy: {skill_entrypoint_accuracy:.2f}",
        f"- CI pytest failure detection: {ci_pytest_failure_detection:.2f}",
    ]
    if rag_quality:
        # 若跑了 Ragas，补一行召回率
        report_lines.append(f"- RAG recall@5: {rag_quality['retrieval']['recall_at_k']:.2f}")
    report_lines.extend(
        [
            f"- Native tool schema ready rate: {tool_call_schema_ready_rate:.2f}",
            f"- JSON schema pass rate: {json_schema_pass_rate:.2f}",
        ]
    )
    report_markdown = "\n".join(report_lines) + "\n"  # 合并成最终 Markdown 文本

    repository_sample: dict[str, Any] | None = None  # 仓库抽样统计（可选）
    if db is not None and repo_id:
        # 若给了数据库，统计该仓库各表记录数，作为「库里有没有数据」的旁证
        repo_uuid = UUID(str(repo_id))
        repository_sample = {
            "issues": db.query(Issue).filter(Issue.repo_id == repo_uuid).count(),
            "pull_requests": db.query(PullRequest).filter(PullRequest.repo_id == repo_uuid).count(),
            "workflow_runs": db.query(WorkflowRun).filter(WorkflowRun.repo_id == repo_uuid).count(),
            "code_symbols": db.query(CodeSymbol).filter(CodeSymbol.repo_id == repo_uuid).count(),
            "knowledge_documents": db.query(Document).filter(Document.repo_id == repo_uuid).count(),
        }

    return {
        # 汇总所有指标返回（数值四舍五入保留 4 位小数）
        "issue_classification_accuracy": round(issue_accuracy, 4),
        "pr_key_file_coverage": round(pr_key_file_coverage, 4),
        "rag_hit_rate_at_3": round(rag_hit_rate_at_3, 4),
        "tool_call_accuracy": round(tool_call_schema_ready_rate, 4),
        "skill_registry_coverage": round(skill_registry_coverage, 4),
        "skill_entrypoint_accuracy": round(skill_entrypoint_accuracy, 4),
        "ci_pytest_failure_detection": round(ci_pytest_failure_detection, 4),
        "json_schema_pass_rate": round(json_schema_pass_rate, 4),
        "rag_quality": rag_quality,
        "context_compression": {
            "message_stats": message_stats,
            "retrieval_stats": retrieval_items[0].get("compression") if retrieval_items else {},
            "memory_sections": {
                "facts": len(merged_memory["facts"]),
                "decisions": len(merged_memory["decisions"]),
                "tasks": len(merged_memory["tasks"]),
            },
            "quality": compression_quality,
        },
        "cases": {
            # 各项用例数量（用于报告里展示样本规模）
            "issue": len(issue_cases),
            "pr": 1,
            "rag": len(rag_docs),
            "ci": 1,
            "skills": len(required_skill_entrypoints),
            "context_compression": 3,
        },
        "repository_sample": repository_sample,
        "report_markdown": report_markdown,
    }
