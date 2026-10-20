"""DevMind AI module."""

import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.models import ActionDraft, EvalRun, Repository, User
from app.db.session import SessionLocal, get_db
from app.schemas.evals import EvalIssueDraftRequest, EvalRunRequest, EvalRunResponse
from app.services.evals.ragas_runner import (
    REQUIRED_RAGAS_METRICS,
    RagEvalCase,
    rescore_rag_result,
    run_rag_quality_eval,
)
from app.services.evals.suites import AUTO_SMOKE_SUITE_ID, list_eval_suites, load_eval_suite
from app.services.evals.runner import run_basic_eval
from app.services.permissions import require_permission, write_audit_log

router = APIRouter()
logger = logging.getLogger(__name__)


def _request_case(item) -> RagEvalCase:
    return RagEvalCase(
        id=item.id,
        question=item.question,
        reference=item.reference,
        expected_source_ids=item.expected_source_ids,
        source_type=item.source_type,
        required_tools=item.required_tools,
        expected_tool_calls=item.expected_tool_calls,
        kind=item.kind,
        must_include=item.must_include,
        must_not_include=item.must_not_include,
        answer_regex=item.answer_regex,
        source_file=item.source_file,
    )


def _custom_evaluation_set(payload: EvalRunRequest) -> dict:
    serialized = [item.model_dump(mode="json") for item in payload.cases or []]
    canonical = json.dumps(serialized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "id": payload.name or "custom-cases",
        "name": payload.name or "custom-cases",
        "label": payload.name or "自定义评测集",
        "description": "通过 API 提交的固定评测用例",
        "source": "api_cases",
        "sha256": hashlib.sha256(canonical).hexdigest(),
        "case_count": len(serialized),
    }


@router.get("/suites")
async def get_eval_suites(
    repo_id: UUID = Query(...),
    db: Session = Depends(get_db),
    _user=Depends(require_permission("eval:read")),
) -> list[dict]:
    try:
        return list_eval_suites(db, repo_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/run", response_model=EvalRunResponse)
async def run_eval(
    payload: EvalRunRequest,
    db: Session = Depends(get_db),
    _user=Depends(require_permission("eval:read")),
) -> EvalRunResponse:
    result = await run_basic_eval(db, str(payload.repo_id) if payload.repo_id else None)
    eval_run = EvalRun(name=payload.name, result_json=result)
    db.add(eval_run)
    db.commit()
    db.refresh(eval_run)
    return EvalRunResponse(
        eval_id=str(eval_run.id),
        status="completed",
        result=result,
    )


@router.post("/rag/run", response_model=EvalRunResponse)
async def run_rag_eval(
    payload: EvalRunRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _user=Depends(require_permission("eval:read")),
) -> EvalRunResponse:
    if payload.repo_id is None:
        raise HTTPException(status_code=400, detail="RAG Eval 需要 repo_id")
    if db.get(Repository, payload.repo_id) is None:
        raise HTTPException(status_code=404, detail="未找到仓库")
    if payload.cases is not None and payload.suite_id:
        raise HTTPException(status_code=400, detail="cases 与 suite_id 不能同时提交")
    if payload.cases is not None:
        cases = [_request_case(item) for item in payload.cases]
        evaluation_set = _custom_evaluation_set(payload)
    elif payload.suite_id:
        try:
            suite = load_eval_suite(db, payload.repo_id, payload.suite_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        cases = None if payload.suite_id == AUTO_SMOKE_SUITE_ID else suite.cases
        evaluation_set = suite.metadata
    else:
        cases = None
        evaluation_set = {
            "id": AUTO_SMOKE_SUITE_ID,
            "name": AUTO_SMOKE_SUITE_ID,
            "label": "自动文档冒烟测试",
            "description": "未指定固定评测集，使用当前仓库文档临时出题。",
            "source": "auto_smoke",
            "sha256": None,
            "case_count": None,
        }
    result = _job_result(
        status="queued",
        stage="queued",
        message="评测任务已提交，正在等待后台执行。",
        repo_id=str(payload.repo_id),
        evaluation_set=evaluation_set,
    )
    eval_run = EvalRun(name=payload.name or "rag_eval", result_json=result)
    db.add(eval_run)
    db.commit()
    db.refresh(eval_run)
    background_tasks.add_task(
        _execute_rag_eval_job,
        str(eval_run.id),
        str(payload.repo_id),
        cases,
        payload.top_k,
        payload.run_judge,
        evaluation_set,
    )
    return EvalRunResponse(eval_id=str(eval_run.id), status="queued", result=result)


async def _execute_rag_eval_job(
    eval_id: str,
    repo_id: str,
    cases: list[RagEvalCase] | None,
    limit: int,
    run_judge: bool,
    evaluation_set: dict,
) -> None:
    db = SessionLocal()
    # Yield once after Starlette sends the queued response so the client receives
    # the Eval ID before imports, index loading, or model setup can block the loop.
    await asyncio.sleep(0)

    async def persist_progress(progress: dict) -> None:
        eval_run = db.get(EvalRun, UUID(eval_id))
        if eval_run is None:
            return
        eval_run.result_json = _job_result(
            status="running",
            stage=str(progress.get("stage") or "running"),
            message=str(progress.get("message") or "评测正在后台运行。"),
            repo_id=repo_id,
            evaluation_set=evaluation_set,
            **{
                key: value
                for key, value in progress.items()
                if key not in {"stage", "message"}
            },
        )
        db.add(eval_run)
        db.commit()

    try:
        await persist_progress({"stage": "starting", "message": "后台任务已启动，正在加载仓库上下文。"})
        result = await run_rag_quality_eval(
            db,
            repo_id,
            cases=cases,
            limit=limit,
            run_judge=run_judge,
            progress_callback=persist_progress,
            evaluation_set=evaluation_set,
        )
        result["progress"] = {
            "stage": "completed",
            "message": "评测已完成。",
            "updated_at": datetime.now(UTC).isoformat(),
        }
    except Exception as exc:
        logger.exception("RAGAS background evaluation failed: eval_id=%s", eval_id)
        result = _job_result(
            status="error",
            stage="error",
            message=f"评测运行失败：{exc}",
            repo_id=repo_id,
            evaluation_set=evaluation_set,
        )
        result.update(
            {
                "passed": False,
                "ragas": {
                    "requested": run_judge,
                    "available": False,
                    "passed": None,
                    "error": f"{type(exc).__name__}: {exc}",
                    "metrics": {},
                },
                "cases": [],
            }
        )

    try:
        eval_run = db.get(EvalRun, UUID(eval_id))
        if eval_run is not None:
            eval_run.result_json = result
            db.add(eval_run)
            db.commit()
    finally:
        db.close()


def _job_result(
    *,
    status: str,
    stage: str,
    message: str,
    repo_id: str | None = None,
    evaluation_set: dict | None = None,
    **progress: object,
) -> dict:
    return {
        "kind": "chatagent_ragas",
        "status": status,
        "execution_status": status,
        "quality_status": "incomplete",
        "evaluation_complete": False,
        "passed": False,
        "target": {"name": "ChatAgent", "repo_id": repo_id} if repo_id else {"name": "ChatAgent"},
        "evaluation_set": evaluation_set or {},
        "progress": {
            "stage": stage,
            "message": message,
            "updated_at": datetime.now(UTC).isoformat(),
            **progress,
        },
    }


@router.post("/{eval_id}/rescore", response_model=EvalRunResponse)
async def rescore_eval(
    eval_id: UUID,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _user=Depends(require_permission("eval:read")),
) -> EvalRunResponse:
    source = db.get(EvalRun, eval_id)
    if source is None:
        raise HTTPException(status_code=404, detail="未找到 Eval run")
    source_result = source.result_json or {}
    if not source_result.get("cases"):
        raise HTTPException(status_code=400, detail="原评测没有可用于重评分的逐题现场")
    error_count = sum(bool(item.get("ragas_errors")) for item in source_result.get("cases") or [])
    missing_count = sum(
        any(name not in (item.get("ragas") or {}) for name in ("context_precision", "context_recall", "faithfulness", "answer_relevancy", "agent_goal_accuracy"))
        for item in source_result.get("cases") or []
    )
    if error_count == 0 and missing_count == 0:
        raise HTTPException(status_code=400, detail="原评测没有失败或缺失的 Judge 指标")
    repo_id = str((source_result.get("target") or {}).get("repo_id") or "")
    evaluation_set = dict(source_result.get("evaluation_set") or {})
    queued = _job_result(
        status="queued",
        stage="queued",
        message=f"已提交重评分任务，将复用 Eval {str(eval_id)[:8]} 的 ChatAgent 现场。",
        repo_id=repo_id or None,
        evaluation_set=evaluation_set,
        rescore_of=str(eval_id),
    )
    target = EvalRun(name=f"rescore-{source.name}", result_json=queued)
    db.add(target)
    db.commit()
    db.refresh(target)
    background_tasks.add_task(
        _execute_rescore_job,
        str(target.id),
        str(source.id),
        source_result,
        repo_id,
        evaluation_set,
    )
    return EvalRunResponse(eval_id=str(target.id), status="queued", result=queued)


async def _execute_rescore_job(
    target_eval_id: str,
    source_eval_id: str,
    source_result: dict,
    repo_id: str,
    evaluation_set: dict,
) -> None:
    db = SessionLocal()
    await asyncio.sleep(0)

    async def persist_progress(progress: dict) -> None:
        row = db.get(EvalRun, UUID(target_eval_id))
        if row is None:
            return
        row.result_json = _job_result(
            status="running",
            stage=str(progress.get("stage") or "rescore"),
            message=str(progress.get("message") or "正在重试异常评分。"),
            repo_id=repo_id or None,
            evaluation_set=evaluation_set,
            rescore_of=source_eval_id,
            **{key: value for key, value in progress.items() if key not in {"stage", "message"}},
        )
        db.add(row)
        db.commit()

    try:
        result = await rescore_rag_result(
            source_result,
            eval_id=source_eval_id,
            progress_callback=persist_progress,
        )
        result["progress"] = {
            "stage": "completed",
            "message": "异常指标重评分已完成；ChatAgent 回答和检索现场均未重新运行。",
            "updated_at": datetime.now(UTC).isoformat(),
        }
    except Exception as exc:
        logger.exception("RAGAS rescore failed: eval_id=%s", target_eval_id)
        result = _job_result(
            status="error",
            stage="error",
            message=f"重评分失败：{exc}",
            repo_id=repo_id or None,
            evaluation_set=evaluation_set,
            rescore_of=source_eval_id,
        )
        result["ragas"] = {
            "requested": True,
            "available": False,
            "complete": False,
            "passed": None,
            "error": f"{type(exc).__name__}: {exc}",
            "metrics": {},
        }
        result["cases"] = source_result.get("cases") or []
    try:
        row = db.get(EvalRun, UUID(target_eval_id))
        if row is not None:
            row.result_json = result
            db.add(row)
            db.commit()
    finally:
        db.close()


def _eval_comparison(base: EvalRun, candidate: EvalRun) -> dict:
    base_result = base.result_json or {}
    candidate_result = candidate.result_json or {}
    base_set = base_result.get("evaluation_set") or {}
    candidate_set = candidate_result.get("evaluation_set") or {}
    reasons: list[str] = []
    if base_set.get("sha256") != candidate_set.get("sha256"):
        reasons.append("评测集 Hash 不一致")
    if (base_result.get("ragas") or {}).get("version") != (candidate_result.get("ragas") or {}).get("version"):
        reasons.append("Ragas 版本不一致")
    if (base_result.get("ragas") or {}).get("judge_model") != (candidate_result.get("ragas") or {}).get("judge_model"):
        reasons.append("Judge 模型不一致")
    if (base_result.get("pipeline") or {}).get("top_k") != (candidate_result.get("pipeline") or {}).get("top_k"):
        reasons.append("Top-K 不一致")
    base_metrics = base_result.get("summary_metrics") or {}
    candidate_metrics = candidate_result.get("summary_metrics") or {}
    metric_deltas = []
    for name in sorted(set(base_metrics) | set(candidate_metrics)):
        left = base_metrics.get(name)
        right = candidate_metrics.get(name)
        if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
            continue
        metric_deltas.append(
            {"name": name, "base": float(left), "candidate": float(right), "delta": round(float(right) - float(left), 4)}
        )
    base_cases = {str(item.get("id")): item for item in base_result.get("cases") or []}
    candidate_cases = {str(item.get("id")): item for item in candidate_result.get("cases") or []}
    case_deltas = []
    for case_id in sorted(set(base_cases) | set(candidate_cases)):
        left = base_cases.get(case_id) or {}
        right = candidate_cases.get(case_id) or {}
        left_passed = bool((left.get("hard_rules") or {}).get("passed")) and not bool(left.get("ragas_errors"))
        right_passed = bool((right.get("hard_rules") or {}).get("passed")) and not bool(right.get("ragas_errors"))
        state = "unchanged"
        if not left_passed and right_passed:
            state = "improved"
        elif left_passed and not right_passed:
            state = "regressed"
        case_deltas.append(
            {"id": case_id, "base_passed": left_passed, "candidate_passed": right_passed, "state": state}
        )
    return {
        "base_eval_id": str(base.id),
        "candidate_eval_id": str(candidate.id),
        "comparable": not reasons,
        "incompatibility_reasons": reasons,
        "metric_deltas": metric_deltas,
        "case_deltas": case_deltas,
        "improved_cases": sum(item["state"] == "improved" for item in case_deltas),
        "regressed_cases": sum(item["state"] == "regressed" for item in case_deltas),
        "candidate_quality_status": candidate_result.get("quality_status"),
        "candidate_complete": bool(candidate_result.get("evaluation_complete")),
        "release_gate_passed": bool(not reasons and candidate_result.get("passed")),
    }


@router.get("/compare")
async def compare_evals(
    base_eval_id: UUID = Query(...),
    candidate_eval_id: UUID = Query(...),
    db: Session = Depends(get_db),
    _user=Depends(require_permission("eval:read")),
) -> dict:
    base = db.get(EvalRun, base_eval_id)
    candidate = db.get(EvalRun, candidate_eval_id)
    if base is None or candidate is None:
        raise HTTPException(status_code=404, detail="未找到用于对比的 Eval run")
    return _eval_comparison(base, candidate)


def _issue_markdown(eval_run: EvalRun) -> str:
    result = eval_run.result_json or {}
    suite = result.get("evaluation_set") or {}
    ragas = result.get("ragas") or {}
    lines = [
        "## 复现环境",
        "",
        f"- Eval ID：`{eval_run.id}`",
        f"- 评测集：`{suite.get('name') or '-'}`",
        f"- 评测集 Hash：`{suite.get('sha256') or '-'}`",
        f"- Ragas：`{ragas.get('version') or '-'}`",
        f"- Judge：`{ragas.get('judge_model') or '-'}`",
        f"- 运行状态：`{result.get('status') or '-'}`",
        f"- 质量状态：`{result.get('quality_status') or '-'}`",
        "",
        "## 聚合结果",
        "",
        "| 指标 | 分数 | 门槛 |",
        "| --- | ---: | ---: |",
    ]
    for name, detail in (ragas.get("metrics") or {}).items():
        if name not in REQUIRED_RAGAS_METRICS:
            continue
        score = detail.get("score")
        threshold = detail.get("threshold")
        lines.append(
            f"| {name} | {score if score is not None else 'ERROR'} | {threshold if threshold is not None else '-'} |"
        )
    lines.extend(["", "## 失败用例", ""])
    for case in result.get("cases") or []:
        diagnoses = [item for item in case.get("diagnoses") or [] if item.get("category") != "passed"]
        if not diagnoses and not case.get("ragas_errors"):
            continue
        lines.extend(
            [
                f"### {case.get('id') or 'unknown'}",
                "",
                f"- 问题：{case.get('question') or '-'}",
                f"- 标准答案：{case.get('reference') or '-'}",
                f"- 实际答案：{case.get('response') or '-'}",
            ]
        )
        if case.get("ragas_errors"):
            lines.append(f"- Judge 错误：`{json.dumps(case['ragas_errors'], ensure_ascii=False)}`")
        for diagnosis in diagnoses:
            lines.append(f"- {diagnosis.get('title')}：{diagnosis.get('action')}")
        lines.append("")
    lines.extend(
        [
            "## 验收标准",
            "",
            "- 使用同一评测集版本重新运行。",
            "- Judge 指标计算零错误。",
            "- 所有硬规则通过，所有门禁指标达到当前阈值。",
            "- 与本 Eval 做逐题对比，不新增回归用例。",
        ]
    )
    return "\n".join(lines)


@router.post("/{eval_id}/issue-draft")
async def create_eval_issue_draft(
    eval_id: UUID,
    payload: EvalIssueDraftRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("draft:create")),
) -> dict:
    eval_run = db.get(EvalRun, eval_id)
    if eval_run is None:
        raise HTTPException(status_code=404, detail="未找到 Eval run")
    result = eval_run.result_json or {}
    repo_id = (result.get("target") or {}).get("repo_id")
    if not repo_id:
        raise HTTPException(status_code=400, detail="该历史评测没有保存仓库信息，无法创建 Issue 草稿")
    repo = db.get(Repository, UUID(str(repo_id)))
    if repo is None:
        raise HTTPException(status_code=404, detail="未找到评测对应仓库")
    suite_label = (result.get("evaluation_set") or {}).get("label") or "RAGAS"
    title = payload.title or f"RAG 质量回归：{suite_label} 未通过"
    draft = ActionDraft(
        repo_id=repo.id,
        draft_type="create_issue",
        target_type="repository",
        title=title,
        content=_issue_markdown(eval_run),
        risk_level="medium",
        created_by=user.id,
        meta={"eval_id": str(eval_run.id), "quality_status": result.get("quality_status")},
    )
    db.add(draft)
    write_audit_log(
        db,
        user=user,
        repo_id=repo.id,
        action="eval.issue_draft.create",
        target_type="eval_run",
        target_id=str(eval_run.id),
        request_json={"eval_id": str(eval_run.id), "title": title},
    )
    db.commit()
    db.refresh(draft)
    return {"draft_id": str(draft.id), "status": draft.status, "title": draft.title}


@router.get("")
async def list_evals(
    limit: int = 20,
    repo_id: UUID | None = None,
    db: Session = Depends(get_db),
    _user=Depends(require_permission("eval:read")),
) -> list[dict]:
    rows = db.query(EvalRun).order_by(EvalRun.created_at.desc()).limit(100).all()
    if repo_id is not None:
        rows = [
            row
            for row in rows
            if str(((row.result_json or {}).get("target") or {}).get("repo_id") or "") == str(repo_id)
        ]
    rows = rows[: max(1, min(limit, 100))]
    return [
        {
            "eval_id": str(row.id),
            "name": row.name,
            "status": str((row.result_json or {}).get("status") or "completed"),
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "metrics": _summary_metrics(row.result_json or {}),
            "result": row.result_json,
        }
        for row in rows
    ]


@router.get("/{eval_id}")
async def get_eval(
    eval_id: str,
    db: Session = Depends(get_db),
    _user=Depends(require_permission("eval:read")),
) -> dict:
    try:
        eval_uuid = UUID(eval_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Eval id 无效") from exc
    eval_run = db.get(EvalRun, eval_uuid)
    if eval_run is None:
        raise HTTPException(status_code=404, detail="未找到 Eval run")
    result = eval_run.result_json or {}
    return {
        "eval_id": str(eval_run.id),
        "name": eval_run.name,
        "status": str(result.get("status") or "completed"),
        "result": result,
    }


def _summary_metrics(result: dict) -> dict[str, float]:
    summary = result.get("summary_metrics")
    metrics = {
        key: float(value)
        for key, value in (summary.items() if isinstance(summary, dict) else result.items())
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    return metrics
