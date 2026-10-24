"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  FileWarning,
  Gauge,
  GitCompareArrows,
  History,
  LoaderCircle,
  Play,
  RefreshCcw,
  SearchCheck,
  Wrench
} from "lucide-react";

import { PageTitle, PrimaryButton } from "@/components/ui/card";
import { apiGet, apiPost } from "@/lib/api";
import type { Repository } from "@/lib/types";

type MetricDetail = {
  score: number | null;
  threshold: number;
  passed: boolean | null;
  scored_cases: number;
};

type Diagnosis = {
  category: string;
  severity: "passed" | "failed" | "error";
  title: string;
  summary: string;
  action: string;
};

type RetrievedSource = {
  id?: string;
  source_id?: string;
  source_type?: string;
  title?: string;
  score?: number;
};

type ToolCall = {
  tool_name?: string;
  status?: string;
  arguments?: Record<string, unknown>;
  error?: unknown;
};

type HardRules = {
  passed: boolean;
  source_hit: boolean;
  answer_present: boolean;
  contexts_present: boolean;
  missing_tools: string[];
  tool_errors: unknown[];
  missing_phrases: string[];
  forbidden_phrases: string[];
  answer_pattern_matched: boolean;
};

type RagCaseResult = {
  id: string;
  question: string;
  reference?: string;
  response?: string;
  route?: string;
  source_file?: string | null;
  latency_ms: number;
  retrieved_contexts: string[];
  retrieved_sources: RetrievedSource[];
  tool_calls: ToolCall[];
  hard_rules: HardRules;
  ragas?: Record<string, number> | null;
  ragas_errors?: Record<string, string>;
  ragas_skipped_reason?: string | null;
  diagnoses?: Diagnosis[];
};

type EvalSet = {
  id?: string;
  name?: string;
  label?: string;
  description?: string;
  source?: string;
  sha256?: string | null;
  case_count?: number | null;
};

type EvalResult = {
  kind?: string;
  status?: string;
  execution_status?: string;
  quality_status?: string;
  evaluation_complete?: boolean;
  passed?: boolean;
  run_mode?: string;
  rescore_of?: string | null;
  target?: { name?: string; repo_id?: string; execution?: string };
  evaluation_set?: EvalSet;
  pipeline?: { top_k?: number };
  summary_metrics?: Record<string, number>;
  retrieval?: { recall_at_k?: number; precision_at_k?: number; mrr?: number; ndcg?: number };
  performance?: {
    cases?: number;
    total_latency_ms?: number;
    avg_latency_ms?: number;
    rescore_latency_ms?: number;
    rescore_cases?: number;
    agent_reused?: boolean;
  };
  hard_gate?: { passed?: boolean; pass_rate?: number; passed_cases?: number; total_cases?: number };
  progress?: {
    stage?: string;
    message?: string;
    current_case?: number;
    completed_cases?: number;
    total_cases?: number;
    updated_at?: string;
  };
  ragas?: {
    requested?: boolean;
    available?: boolean;
    complete?: boolean;
    passed?: boolean | null;
    version?: string | null;
    judge_model?: string | null;
    error?: string | null;
    case_errors?: number;
    metrics?: Record<string, MetricDetail>;
  };
  diagnostics?: {
    failed_cases?: number;
    error_cases?: number;
    category_counts?: Record<string, number>;
  };
  cases?: RagCaseResult[];
};

type EvalResponse = { eval_id: string; status: string; result: EvalResult };
type EvalHistoryItem = EvalResponse & { name: string; created_at?: string | null; metrics?: Record<string, number> };

type EvalSuite = {
  id: string;
  name: string;
  label: string;
  description: string;
  schema_version: number;
  case_count: number | null;
  sha256: string | null;
  source: "fixed" | "auto_smoke";
  ready: boolean;
  missing_sources: string[];
  recommended: boolean;
};

type EvalComparison = {
  base_eval_id: string;
  candidate_eval_id: string;
  comparable: boolean;
  incompatibility_reasons: string[];
  metric_deltas: Array<{ name: string; base: number; candidate: number; delta: number }>;
  case_deltas: Array<{ id: string; base_passed: boolean; candidate_passed: boolean; state: string }>;
  improved_cases: number;
  regressed_cases: number;
  candidate_quality_status?: string;
  candidate_complete: boolean;
  release_gate_passed: boolean;
};

type IssueDraftResponse = { draft_id: string; status: string; title: string };

const ACTIVE_STATUSES = new Set(["queued", "running"]);

const METRICS = [
  { key: "context_precision", label: "Context Precision", description: "有用证据是否排在前面，前排是否混入噪声" },
  { key: "context_recall", label: "Context Recall", description: "参考答案所需事实是否被检索齐全" },
  { key: "faithfulness", label: "Faithfulness", description: "最终回答是否忠于检索证据" },
  { key: "answer_relevancy", label: "Answer Relevancy", description: "回答是否直接回应用户问题" },
  { key: "agent_goal_accuracy", label: "Agent Goal", description: "ChatAgent 是否完成格式与任务要求" }
] as const;

const METRIC_LABELS: Record<string, string> = {
  context_precision: "Context Precision",
  context_recall: "Context Recall",
  faithfulness: "Faithfulness",
  answer_relevancy: "Answer Relevancy",
  agent_goal_accuracy: "Agent Goal",
  hard_gate_pass_rate: "Hard Gate",
  retrieval_recall_at_k: "Recall@K",
  retrieval_precision_at_k: "Precision@K"
};

const DIAGNOSIS_LABELS: Record<string, string> = {
  judge_error: "Judge 异常",
  retrieval: "目标证据未命中",
  retrieval_noise: "检索噪声",
  retrieval_recall: "召回不完整",
  generation: "硬规则失败",
  generation_faithfulness: "回答缺少依据",
  generation_relevancy: "回答不够直接",
  agent_goal: "任务未完成"
};

export default function EvalsPage() {
  const [repos, setRepos] = useState<Repository[]>([]);
  const [repoId, setRepoId] = useState("");
  const [suites, setSuites] = useState<EvalSuite[]>([]);
  const [suiteId, setSuiteId] = useState("");
  const [topK, setTopK] = useState(5);
  const [result, setResult] = useState<EvalResponse | null>(null);
  const [history, setHistory] = useState<EvalHistoryItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [activeEvalId, setActiveEvalId] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [draftId, setDraftId] = useState("");
  const [baselineId, setBaselineId] = useState("");
  const [comparison, setComparison] = useState<EvalComparison | null>(null);
  const [comparing, setComparing] = useState(false);

  const loadHistory = useCallback(async (targetRepoId?: string) => {
    const suffix = targetRepoId ? `?repo_id=${encodeURIComponent(targetRepoId)}&limit=50` : "?limit=50";
    const items = await apiGet<EvalHistoryItem[]>(`/api/evals${suffix}`).catch(() => []);
    setHistory(items);
    return items;
  }, []);

  useEffect(() => {
    apiGet<Repository[]>("/api/repos")
      .then((items) => {
        setRepos(items);
        setRepoId(items[0]?.id || "");
      })
      .catch(() => setRepos([]));
  }, []);

  useEffect(() => {
    if (!repoId) return;
    setError("");
    setNotice("");
    setDraftId("");
    setComparison(null);
    Promise.all([
      apiGet<EvalSuite[]>(`/api/evals/suites?repo_id=${encodeURIComponent(repoId)}`).catch(() => []),
      loadHistory(repoId)
    ]).then(([suiteItems, historyItems]) => {
      setSuites(suiteItems);
      const recommended = suiteItems.find((item) => item.recommended && item.ready) || suiteItems.find((item) => item.ready);
      setSuiteId(recommended?.id || "");
      const active = historyItems.find((item) => ACTIVE_STATUSES.has(item.result.status || item.status));
      const latest = active || historyItems[0];
      if (latest) {
        setResult({ eval_id: latest.eval_id, status: latest.status, result: latest.result });
      }
      if (active) {
        setActiveEvalId(active.eval_id);
        setLoading(true);
      }
    });
  }, [repoId, loadHistory]);

  useEffect(() => {
    if (!activeEvalId) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let failures = 0;

    async function poll() {
      try {
        const data = await apiGet<EvalResponse>(`/api/evals/${activeEvalId}`);
        if (cancelled) return;
        failures = 0;
        setResult(data);
        const nextStatus = data.result.status || data.status;
        if (ACTIVE_STATUSES.has(nextStatus)) {
          timer = setTimeout(poll, 2000);
          return;
        }
        setLoading(false);
        setActiveEvalId("");
        await loadHistory(data.result.target?.repo_id || repoId);
      } catch (reason) {
        if (cancelled) return;
        failures += 1;
        if (failures < 3) {
          timer = setTimeout(poll, 2500);
          return;
        }
        setLoading(false);
        setActiveEvalId("");
        setError(`进度查询失败，后台任务可能仍在运行。刷新页面可以恢复。${reason instanceof Error ? ` ${reason.message}` : ""}`);
      }
    }

    void poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [activeEvalId, loadHistory, repoId]);

  const selectedSuite = suites.find((item) => item.id === suiteId);
  const status = result?.result.status || result?.status;
  const active = ACTIVE_STATUSES.has(status || "");
  const cases = result?.result.cases || [];
  const metricRows = useMemo(() => {
    const details = result?.result.ragas?.metrics || {};
    return METRICS.map((item) => ({ ...item, detail: details[item.key] }));
  }, [result]);
  const rescoreAvailable = Boolean(
    result && !active && cases.some((item) => Object.keys(item.ragas_errors || {}).length > 0 || !item.ragas)
  );

  const baselineOptions = useMemo(() => {
    if (!result) return [];
    return history.filter((item) => item.eval_id !== result.eval_id && !ACTIVE_STATUSES.has(item.result.status || item.status));
  }, [history, result]);

  useEffect(() => {
    if (!result || baselineId || !baselineOptions.length) return;
    const currentHash = result.result.evaluation_set?.sha256;
    const compatible = baselineOptions.find((item) => item.result.evaluation_set?.sha256 === currentHash);
    setBaselineId((compatible || baselineOptions[0])?.eval_id || "");
  }, [baselineId, baselineOptions, result]);

  function beginJob(data: EvalResponse, message: string) {
    setResult(data);
    setActiveEvalId(data.eval_id);
    setLoading(true);
    setError("");
    setNotice(message);
    setComparison(null);
    setBaselineId("");
  }

  async function runEval() {
    if (!repoId || !suiteId) {
      setError("请先选择仓库和评测集。");
      return;
    }
    setLoading(true);
    setError("");
    setNotice("");
    setDraftId("");
    try {
      const data = await apiPost<EvalResponse>("/api/evals/rag/run", {
        name: `${suiteId}-${new Date().toISOString().slice(0, 19)}`,
        repo_id: repoId,
        suite_id: suiteId,
        run_judge: true,
        top_k: topK
      });
      beginJob(data, "固定评测任务已提交。");
      await loadHistory(repoId);
    } catch (reason) {
      setLoading(false);
      setError(reason instanceof Error ? reason.message : "RAGAS 评测运行失败");
    }
  }

  async function rescoreEval() {
    if (!result || !rescoreAvailable) return;
    setLoading(true);
    setError("");
    try {
      const data = await apiPost<EvalResponse>(`/api/evals/${result.eval_id}/rescore`, {});
      beginJob(data, "已复用原问题、回答和证据，只重试异常或缺失的 Judge 指标。");
    } catch (reason) {
      setLoading(false);
      setError(reason instanceof Error ? reason.message : "异常指标重评分失败");
    }
  }

  async function createIssueDraft() {
    if (!result) return;
    setError("");
    setNotice("");
    try {
      const data = await apiPost<IssueDraftResponse>(`/api/evals/${result.eval_id}/issue-draft`, {});
      setDraftId(data.draft_id);
      setNotice(`已创建安全 Issue 草稿：${data.title}`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Issue 草稿创建失败");
    }
  }

  async function compareRuns() {
    if (!result || !baselineId) return;
    setComparing(true);
    setError("");
    try {
      const data = await apiGet<EvalComparison>(
        `/api/evals/compare?base_eval_id=${encodeURIComponent(baselineId)}&candidate_eval_id=${encodeURIComponent(result.eval_id)}`
      );
      setComparison(data);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "评测对比失败");
    } finally {
      setComparing(false);
    }
  }

  return (
    <>
      <PageTitle
        title="ChatAgent RAGAS 质量工作台"
        description="使用固定评测集运行真实 ChatAgent，定位检索、生成和 Judge 问题，并完成重评分与回归对比。"
      />

      <section className="grid gap-4 lg:grid-cols-[360px_minmax(0,1fr)]">
        <div className="rounded-md border border-line bg-white p-5">
          <div className="flex items-center gap-2 text-sm font-semibold text-ink">
            <Gauge className="h-4 w-4 text-teal-700" />评测配置
          </div>
          <label className="mt-4 block text-sm font-medium text-slate-700">
            仓库知识上下文
            <select className="mt-2 w-full rounded-md border border-line bg-white px-3 py-2 outline-none focus:border-teal-500 focus:ring-2 focus:ring-teal-100" value={repoId} onChange={(event) => setRepoId(event.target.value)}>
              <option value="">请选择仓库</option>
              {repos.map((repo) => <option key={repo.id} value={repo.id}>{repo.full_name}</option>)}
            </select>
          </label>

          <label className="mt-4 block text-sm font-medium text-slate-700">
            评测集
            <select className="mt-2 w-full rounded-md border border-line bg-white px-3 py-2 outline-none focus:border-teal-500 focus:ring-2 focus:ring-teal-100" value={suiteId} onChange={(event) => setSuiteId(event.target.value)}>
              <option value="">请选择评测集</option>
              {suites.map((suite) => (
                <option key={suite.id} value={suite.id} disabled={!suite.ready}>
                  {suite.label}{suite.recommended ? "（推荐）" : ""}{suite.ready ? "" : "（来源未就绪）"}
                </option>
              ))}
            </select>
          </label>

          {selectedSuite && (
            <div className={`mt-3 rounded-md border p-3 text-xs leading-5 ${selectedSuite.source === "fixed" ? "border-teal-100 bg-teal-50 text-teal-900" : "border-amber-200 bg-amber-50 text-amber-900"}`}>
              <div className="font-semibold">{selectedSuite.source === "fixed" ? "固定回归评测集" : "仅用于冒烟测试"}</div>
              <p className="mt-1">{selectedSuite.description}</p>
              <div className="mt-2 text-[11px] opacity-80">
                {selectedSuite.case_count ? `${selectedSuite.case_count} 条 Case` : "运行时自动生成"}
                {selectedSuite.sha256 ? ` · Hash ${selectedSuite.sha256.slice(0, 12)}` : ""}
              </div>
              {!selectedSuite.ready && <p className="mt-2 text-rose-700">缺少来源：{selectedSuite.missing_sources.join("、")}</p>}
            </div>
          )}

          <label className="mt-4 block text-sm font-medium text-slate-700">
            Top-K
            <select className="mt-2 w-full rounded-md border border-line bg-white px-3 py-2" value={topK} onChange={(event) => setTopK(Number(event.target.value))}>
              {[3, 5, 8, 10].map((value) => <option key={value} value={value}>{value}</option>)}
            </select>
          </label>

          <div className="mt-5 space-y-2 text-sm text-slate-600">
            {["真实 ChatAgent 与生产工具集", "隔离评测会话，不污染聊天记录", "逐题证据、硬规则和工具轨迹", "固定评测集版本与阈值持久化"].map((label) => (
              <div key={label} className="flex items-center gap-2"><CheckCircle2 className="h-4 w-4 text-teal-600" />{label}</div>
            ))}
          </div>

          <PrimaryButton onClick={runEval} disabled={loading || !repoId || !suiteId || selectedSuite?.ready === false} className="mt-6 w-full gap-2">
            {loading ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
            {loading ? "后台任务运行中…" : "运行固定 RAGAS 评测"}
          </PrimaryButton>
        </div>

        <div className="min-w-0 rounded-md border border-line bg-white p-5">
          <div className="flex flex-wrap items-start justify-between gap-3 border-b border-line pb-4">
            <div>
              <div className="text-sm font-semibold text-ink">本次评测结果</div>
              <div className="mt-1 text-xs text-slate-500">
                {result
                  ? `Eval ${result.eval_id.slice(0, 8)} · ${result.result.evaluation_set?.label || "未记录评测集"} · Top-K ${result.result.pipeline?.top_k || "-"}`
                  : "运行后展示质量状态、完整性和逐题修复建议"}
              </div>
            </div>
            {result && <StatusGroup result={result.result} active={active} />}
          </div>

          {error && <div className="mt-4 rounded-md border border-rose-200 bg-rose-50 p-3 text-sm leading-6 text-rose-800">{error}</div>}
          {notice && (
            <div className="mt-4 rounded-md border border-sky-200 bg-sky-50 p-3 text-sm leading-6 text-sky-800">
              {notice}{draftId && <> · <Link href="/action-drafts" className="font-semibold underline">打开安全草稿</Link></>}
            </div>
          )}

          {active && result ? (
            <ProgressPanel progress={result.result.progress} runMode={result.result.run_mode} />
          ) : result ? (
            <>
              {result.result.ragas?.error && (
                <div className="mt-4 flex gap-2 rounded-md border border-rose-200 bg-rose-50 p-3 text-sm text-rose-800">
                  <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                  <div><div className="font-semibold">评测不完整：{result.result.ragas.case_errors || 0} 条用例包含 Judge 错误</div><div className="mt-1 text-xs">{result.result.ragas.error}</div></div>
                </div>
              )}

              <div className="mt-4 flex flex-wrap gap-2">
                <button onClick={rescoreEval} disabled={!rescoreAvailable || loading} className="inline-flex items-center gap-2 rounded-md border border-sky-200 bg-sky-50 px-3 py-2 text-sm font-medium text-sky-800 transition hover:bg-sky-100 disabled:cursor-not-allowed disabled:opacity-40">
                  <RefreshCcw className="h-4 w-4" />仅重试异常评分
                </button>
                <button onClick={createIssueDraft} disabled={loading} className="inline-flex items-center gap-2 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm font-medium text-amber-800 transition hover:bg-amber-100 disabled:opacity-40">
                  <FileWarning className="h-4 w-4" />生成修复 Issue 草稿
                </button>
              </div>

              <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
                {metricRows.map((metric) => <MetricCard key={metric.key} label={metric.label} description={metric.description} detail={metric.detail} />)}
                <MetricCard
                  label="Hard Gate"
                  description="来源命中、关键事实、输出格式和工具错误"
                  detail={{
                    score: result.result.hard_gate?.pass_rate ?? null,
                    threshold: 1,
                    passed: result.result.hard_gate?.passed ?? null,
                    scored_cases: result.result.hard_gate?.total_cases || 0
                  }}
                />
              </div>

              <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
                <Summary label="RAG Recall@K" value={result.result.retrieval?.recall_at_k} />
                <Summary label="RAG Precision@K" value={result.result.retrieval?.precision_at_k} />
                <Summary label={result.result.performance?.agent_reused ? "重评分耗时" : "平均耗时"} value={result.result.performance?.agent_reused ? result.result.performance?.rescore_latency_ms : result.result.performance?.avg_latency_ms} suffix=" ms" digits={0} />
                <Summary label="用例数" value={result.result.performance?.cases ?? cases.length} suffix=" 条" digits={0} />
              </div>

              <DiagnosticOverview result={result.result} />
            </>
          ) : <div className="flex min-h-72 items-center justify-center text-sm text-slate-500">等待运行固定 RAGAS 评测</div>}
        </div>
      </section>

      {result && !active && cases.length > 0 && <CaseDiagnostics cases={cases} />}

      {result && !active && (
        <section className="mt-4 rounded-md border border-line bg-white p-5">
          <div className="flex items-center gap-2 font-semibold text-ink"><GitCompareArrows className="h-4 w-4 text-teal-700" />回归对比</div>
          <p className="mt-2 text-sm text-slate-600">选择一次历史运行作为基线。评测集 Hash、Judge、Ragas 和 Top-K 不一致时，系统会明确拒绝把结果当作严格对照。</p>
          <div className="mt-4 flex flex-col gap-2 sm:flex-row">
            <select className="min-w-0 flex-1 rounded-md border border-line bg-white px-3 py-2 text-sm" value={baselineId} onChange={(event) => { setBaselineId(event.target.value); setComparison(null); }}>
              <option value="">请选择基线运行</option>
              {baselineOptions.map((item) => <option key={item.eval_id} value={item.eval_id}>{item.name} · {item.created_at ? new Date(item.created_at).toLocaleString("zh-CN") : "-"}</option>)}
            </select>
            <button onClick={compareRuns} disabled={!baselineId || comparing} className="inline-flex items-center justify-center gap-2 rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-40">
              {comparing ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <GitCompareArrows className="h-4 w-4" />}生成对比
            </button>
          </div>
          {comparison && <ComparisonPanel comparison={comparison} />}
        </section>
      )}

      <section className="mt-4 rounded-md border border-line bg-white p-5">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2 font-semibold text-ink"><History className="h-4 w-4 text-teal-700" />历史评测</div>
          <span className="text-xs text-slate-500">点击记录恢复完整结果</span>
        </div>
        <div className="mt-4 overflow-x-auto">
          <table className="w-full min-w-[900px] text-left text-sm">
            <thead className="border-b border-line text-xs text-slate-500"><tr><th className="pb-2 font-medium">运行</th><th className="pb-2 font-medium">评测集</th><th className="pb-2 font-medium">时间</th><th className="pb-2 font-medium">Precision</th><th className="pb-2 font-medium">Recall</th><th className="pb-2 font-medium">Faithfulness</th><th className="pb-2 font-medium">完整性</th><th className="pb-2 font-medium">质量</th></tr></thead>
            <tbody>
              {history.map((item) => {
                const metrics = item.metrics || {};
                const itemStatus = item.result.status || item.status;
                return (
                  <tr key={item.eval_id} className="cursor-pointer border-b border-line/70 transition hover:bg-panel" onClick={() => {
                    setResult({ eval_id: item.eval_id, status: item.status, result: item.result });
                    setComparison(null);
                    setBaselineId("");
                    setNotice("");
                    setDraftId("");
                    if (ACTIVE_STATUSES.has(itemStatus)) {
                      setActiveEvalId(item.eval_id);
                      setLoading(true);
                    }
                  }}>
                    <td className="py-3 font-medium text-ink">{item.name}</td>
                    <td className="py-3 text-slate-600">{item.result.evaluation_set?.label || "旧版/未记录"}</td>
                    <td className="py-3 text-slate-500">{item.created_at ? new Date(item.created_at).toLocaleString("zh-CN") : "-"}</td>
                    <Score value={metrics.context_precision} />
                    <Score value={metrics.context_recall} />
                    <Score value={metrics.faithfulness} />
                    <td className="py-3">{ACTIVE_STATUSES.has(itemStatus) ? <RunningLabel /> : item.result.evaluation_complete ? <PassLabel text="完整" /> : <FailLabel text="不完整" />}</td>
                    <td className="py-3"><QualityLabel status={item.result.quality_status || (item.result.passed ? "passed" : "failed")} /></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {history.length === 0 && <div className="py-6 text-sm text-slate-500">当前仓库暂无 RAGAS Eval。</div>}
        </div>
      </section>
    </>
  );
}

function StatusGroup({ result, active }: { result: EvalResult; active: boolean }) {
  if (active) return <RunningLabel />;
  return (
    <div className="flex flex-wrap justify-end gap-2">
      <span className="rounded-full bg-slate-100 px-3 py-1 text-xs font-semibold text-slate-700">运行完成</span>
      <QualityLabel status={result.quality_status || (result.passed ? "passed" : "failed")} />
      {result.evaluation_complete
        ? <span className="rounded-full bg-emerald-50 px-3 py-1 text-xs font-semibold text-emerald-700">评测完整</span>
        : <span className="rounded-full bg-rose-50 px-3 py-1 text-xs font-semibold text-rose-700">评测不完整</span>}
    </div>
  );
}

function QualityLabel({ status }: { status: string }) {
  if (status === "passed") return <PassLabel text="质量通过" />;
  if (status === "incomplete" || status === "partial" || status === "not_evaluated") return <span className="rounded-full bg-amber-50 px-2.5 py-1 text-xs font-medium text-amber-700">质量待判定</span>;
  return <FailLabel text="质量未通过" />;
}

function DiagnosticOverview({ result }: { result: EvalResult }) {
  const counts = result.diagnostics?.category_counts || {};
  const entries = Object.entries(counts);
  return (
    <div className="mt-4 rounded-md border border-line bg-panel p-4">
      <div className="flex items-center gap-2 text-sm font-semibold text-ink"><Wrench className="h-4 w-4 text-teal-700" />下一步动作</div>
      {entries.length ? (
        <div className="mt-3 flex flex-wrap gap-2">
          {entries.map(([category, count]) => <span key={category} className="rounded-full border border-amber-200 bg-white px-3 py-1 text-xs text-amber-800">{DIAGNOSIS_LABELS[category] || category} · {count} 条</span>)}
        </div>
      ) : <p className="mt-2 text-sm text-emerald-700">当前逐题报告没有发现门禁问题，可以进入回归对比。</p>}
      <p className="mt-3 text-xs leading-5 text-slate-600">
        {result.evaluation_complete
          ? "本次 Judge 结果完整。先展开失败用例，根据建议修改检索、生成或评测规则，再用同一评测集运行并对比。"
          : "本次含 Judge 错误。先点击“仅重试异常评分”；重评分不会再次运行 ChatAgent，也不会改变原回答和检索证据。"}
      </p>
    </div>
  );
}

function CaseDiagnostics({ cases }: { cases: RagCaseResult[] }) {
  const ordered = [...cases].sort((left, right) => Number(casePassed(left)) - Number(casePassed(right)));
  return (
    <section className="mt-4 rounded-md border border-line bg-white p-5">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 font-semibold text-ink"><SearchCheck className="h-4 w-4 text-teal-700" />逐题诊断</div>
        <span className="text-xs text-slate-500">失败用例优先排列，点击展开完整运行现场</span>
      </div>
      <div className="mt-4 space-y-3">
        {ordered.map((item) => {
          const passed = casePassed(item);
          return (
            <details key={item.id} className={`group rounded-md border ${passed ? "border-emerald-200" : "border-rose-200"} bg-white`} open={!passed}>
              <summary className="flex cursor-pointer list-none items-start justify-between gap-4 p-4">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2"><span className="font-semibold text-ink">{item.id}</span>{passed ? <PassLabel /> : <FailLabel />}{item.ragas_errors && Object.keys(item.ragas_errors).length > 0 && <span className="rounded-full bg-violet-50 px-2 py-1 text-[11px] text-violet-700">Judge 异常</span>}</div>
                  <p className="mt-2 text-sm leading-6 text-slate-700">{item.question}</p>
                  <div className="mt-2 flex flex-wrap gap-2">
                    {METRICS.map((metric) => <ScoreChip key={metric.key} label={metric.label} value={item.ragas?.[metric.key]} />)}
                  </div>
                </div>
                <ChevronDown className="mt-1 h-5 w-5 shrink-0 text-slate-400 transition group-open:rotate-180" />
              </summary>
              <div className="border-t border-line px-4 pb-5 pt-4">
                <div className="grid gap-4 xl:grid-cols-2">
                  <TextPanel title="标准答案" content={item.reference || "未记录"} />
                  <TextPanel title="实际答案" content={item.response || "未生成答案"} />
                </div>

                <div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                  {(item.diagnoses || []).map((diagnosis, index) => (
                    <div key={`${diagnosis.category}-${index}`} className={`rounded-md border p-3 ${diagnosis.severity === "passed" ? "border-emerald-200 bg-emerald-50/60" : diagnosis.severity === "error" ? "border-violet-200 bg-violet-50/60" : "border-amber-200 bg-amber-50/60"}`}>
                      <div className="text-sm font-semibold text-ink">{diagnosis.title}</div>
                      <p className="mt-1 text-xs leading-5 text-slate-600">{diagnosis.summary}</p>
                      <p className="mt-2 text-xs font-medium leading-5 text-slate-800">建议：{diagnosis.action}</p>
                    </div>
                  ))}
                </div>

                {item.ragas_errors && Object.keys(item.ragas_errors).length > 0 && (
                  <div className="mt-4 rounded-md border border-violet-200 bg-violet-50 p-3 text-xs text-violet-900">
                    <div className="font-semibold">Judge 错误</div>
                    {Object.entries(item.ragas_errors).map(([name, message]) => <div key={name} className="mt-1"><code>{name}</code>：{message}</div>)}
                  </div>
                )}

                <HardRulePanel hard={item.hard_rules} />

                <div className="mt-4 grid gap-4 xl:grid-cols-2">
                  <div className="rounded-md border border-line p-3">
                    <div className="text-xs font-semibold text-slate-700">检索来源 · {item.retrieved_sources.length}</div>
                    <div className="mt-2 space-y-2">
                      {item.retrieved_sources.map((source, index) => (
                        <div key={`${source.source_id || source.id}-${index}`} className="rounded bg-panel px-3 py-2 text-xs text-slate-700">
                          <span className="font-medium">{index + 1}. {source.title || source.source_id || "未知来源"}</span>
                          <span className="ml-2 text-slate-400">{source.source_type || "-"}{typeof source.score === "number" ? ` · ${source.score.toFixed(4)}` : ""}</span>
                        </div>
                      ))}
                      {!item.retrieved_sources.length && <p className="text-xs text-slate-500">没有检索来源。</p>}
                    </div>
                  </div>
                  <div className="rounded-md border border-line p-3">
                    <div className="text-xs font-semibold text-slate-700">工具轨迹 · {item.tool_calls.length}</div>
                    <div className="mt-2 space-y-2">
                      {item.tool_calls.map((tool, index) => (
                        <div key={`${tool.tool_name}-${index}`} className="rounded bg-panel px-3 py-2 text-xs text-slate-700">
                          <div className="flex items-center justify-between gap-2"><span className="font-medium">{tool.tool_name || "unknown"}</span><span className={tool.status === "error" ? "text-rose-600" : "text-emerald-600"}>{tool.status || "-"}</span></div>
                          {tool.arguments && <pre className="mt-1 overflow-x-auto whitespace-pre-wrap text-[11px] text-slate-500">{JSON.stringify(tool.arguments, null, 2)}</pre>}
                        </div>
                      ))}
                      {!item.tool_calls.length && <p className="text-xs text-slate-500">本题没有二次工具调用。</p>}
                    </div>
                  </div>
                </div>

                <details className="mt-4 rounded-md border border-line bg-panel p-3">
                  <summary className="cursor-pointer text-xs font-semibold text-slate-700">查看完整检索上下文 · {item.retrieved_contexts.length} 条</summary>
                  <div className="mt-3 space-y-2">
                    {item.retrieved_contexts.map((context, index) => <pre key={index} className="max-h-64 overflow-auto whitespace-pre-wrap rounded bg-white p-3 text-xs leading-5 text-slate-600">[{index + 1}] {context}</pre>)}
                  </div>
                </details>
                <div className="mt-3 text-xs text-slate-500">路由：{item.route || "-"} · 来源文件：{item.source_file || "-"} · 耗时：{Math.round(item.latency_ms)} ms</div>
              </div>
            </details>
          );
        })}
      </div>
    </section>
  );
}

function HardRulePanel({ hard }: { hard: HardRules }) {
  const rows = [
    ["预期来源命中", hard.source_hit],
    ["取得检索上下文", hard.contexts_present],
    ["生成有效答案", hard.answer_present],
    ["输出格式符合要求", hard.answer_pattern_matched !== false],
    ["工具无错误", !(hard.tool_errors || []).length]
  ] as const;
  return (
    <div className="mt-4 rounded-md border border-line p-3">
      <div className="text-xs font-semibold text-slate-700">确定性硬规则</div>
      <div className="mt-2 flex flex-wrap gap-2">
        {rows.map(([label, ok]) => <span key={label} className={`rounded-full px-2.5 py-1 text-[11px] ${ok ? "bg-emerald-50 text-emerald-700" : "bg-rose-50 text-rose-700"}`}>{ok ? "✓" : "×"} {label}</span>)}
      </div>
      {!!hard.missing_phrases?.length && <p className="mt-2 text-xs text-rose-700">缺少关键事实：{hard.missing_phrases.join("、")}</p>}
      {!!hard.forbidden_phrases?.length && <p className="mt-1 text-xs text-rose-700">包含禁用事实：{hard.forbidden_phrases.join("、")}</p>}
      {!!hard.missing_tools?.length && <p className="mt-1 text-xs text-rose-700">缺少工具：{hard.missing_tools.join("、")}</p>}
    </div>
  );
}

function ComparisonPanel({ comparison }: { comparison: EvalComparison }) {
  return (
    <div className={`mt-4 rounded-md border p-4 ${comparison.comparable ? comparison.release_gate_passed ? "border-emerald-200 bg-emerald-50/50" : "border-amber-200 bg-amber-50/50" : "border-rose-200 bg-rose-50/50"}`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="font-semibold text-ink">{comparison.comparable ? "评测配置可比" : "不能作为严格 A/B 对照"}</div>
        <span className={`rounded-full px-3 py-1 text-xs font-medium ${comparison.release_gate_passed ? "bg-emerald-100 text-emerald-800" : "bg-rose-100 text-rose-800"}`}>{comparison.release_gate_passed ? "回归门禁通过" : "回归门禁未通过"}</span>
      </div>
      {!!comparison.incompatibility_reasons.length && <p className="mt-2 text-sm text-rose-700">{comparison.incompatibility_reasons.join("；")}</p>}
      <div className="mt-3 flex flex-wrap gap-2 text-xs"><span className="rounded-full bg-emerald-100 px-2.5 py-1 text-emerald-800">改善 {comparison.improved_cases} 条</span><span className="rounded-full bg-rose-100 px-2.5 py-1 text-rose-800">回归 {comparison.regressed_cases} 条</span></div>
      <div className="mt-4 overflow-x-auto">
        <table className="w-full min-w-[620px] text-left text-xs"><thead className="border-b border-line text-slate-500"><tr><th className="pb-2">指标</th><th className="pb-2">基线</th><th className="pb-2">候选</th><th className="pb-2">变化</th></tr></thead><tbody>{comparison.metric_deltas.map((item) => <tr key={item.name} className="border-b border-line/60"><td className="py-2 font-medium text-slate-700">{METRIC_LABELS[item.name] || item.name}</td><td className="py-2">{formatMetric(item.name, item.base)}</td><td className="py-2">{formatMetric(item.name, item.candidate)}</td><td className={`py-2 font-medium ${item.delta > 0 ? "text-emerald-700" : item.delta < 0 ? "text-rose-700" : "text-slate-500"}`}>{item.delta > 0 ? "+" : ""}{(item.delta * 100).toFixed(1)} pp</td></tr>)}</tbody></table>
      </div>
    </div>
  );
}

function ProgressPanel({ progress, runMode }: { progress?: EvalResult["progress"]; runMode?: string }) {
  const [now, setNow] = useState(() => Date.now());
  const completed = progress?.completed_cases || 0;
  const total = progress?.total_cases || 0;
  const percent = total > 0 ? Math.min(100, Math.round((completed / total) * 100)) : 8;
  const updated = progress?.updated_at ? new Date(progress.updated_at).toLocaleTimeString("zh-CN") : "刚刚";
  const updatedAt = progress?.updated_at ? new Date(progress.updated_at).getTime() : now;
  const stageSeconds = Math.max(0, Math.floor((now - updatedAt) / 1000));

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);

  return (
    <div className="flex min-h-72 items-center justify-center py-8">
      <div className="w-full max-w-xl rounded-md border border-sky-200 bg-sky-50/60 p-6">
        <div className="flex items-start gap-3">
          <LoaderCircle className="mt-0.5 h-5 w-5 shrink-0 animate-spin text-sky-700" />
          <div className="min-w-0 flex-1"><div className="font-semibold text-ink">{runMode === "rescore" ? "正在复用现场重试 Judge 指标" : "真实 ChatAgent 正在后台评测"}</div><p className="mt-2 text-sm leading-6 text-slate-600">{progress?.message || "后台任务正在启动。"}</p></div>
        </div>
        <div className="mt-5 h-2 overflow-hidden rounded-full bg-sky-100"><div className="h-full rounded-full bg-sky-600 transition-all duration-500" style={{ width: `${percent}%` }} /></div>
        <div className="mt-2 flex items-center justify-between text-xs text-slate-500"><span>{total > 0 ? `已完成 ${completed}/${total}` : "正在准备"}</span><span>阶段持续 {stageSeconds} 秒 · 更新于 {updated}</span></div>
        <div className="mt-4 rounded-md border border-sky-100 bg-white/80 p-3 text-xs leading-5 text-slate-600">{runMode === "rescore" ? "本任务不会重新调用 ChatAgent，原问题、答案、证据和工具轨迹保持不变。" : "单条用例包含真实 ChatAgent 工具调用和 RAGAS Judge 评分。任务在后台运行，可以刷新或离开页面。"}</div>
      </div>
    </div>
  );
}

function MetricCard({ label, description, detail }: { label: string; description: string; detail?: MetricDetail }) {
  const score = detail?.score;
  const ok = detail?.passed === true;
  const unavailable = typeof score !== "number";
  return (
    <div className={`rounded-md border p-3 ${unavailable ? "border-slate-200 bg-slate-50" : ok ? "border-emerald-200 bg-emerald-50/50" : "border-rose-200 bg-rose-50/60"}`}>
      <div className="flex items-center justify-between gap-2"><span className="text-xs font-medium text-slate-700">{label}</span>{unavailable ? <AlertTriangle className="h-4 w-4 text-slate-400" /> : ok ? <CheckCircle2 className="h-4 w-4 text-emerald-600" /> : <AlertTriangle className="h-4 w-4 text-rose-600" />}</div>
      <div className="mt-2 text-2xl font-semibold text-ink">{unavailable ? "—" : `${Math.round(score * 100)}%`}</div>
      <div className="mt-1 text-[11px] leading-4 text-slate-500">{description}</div>
      <div className="mt-2 text-[11px] text-slate-500">门槛 ≥ {Math.round((detail?.threshold || 0) * 100)}% · {detail?.scored_cases || 0} 条</div>
    </div>
  );
}

function TextPanel({ title, content }: { title: string; content: string }) {
  return <div className="rounded-md border border-line bg-panel p-3"><div className="text-xs font-semibold text-slate-700">{title}</div><div className="mt-2 whitespace-pre-wrap text-sm leading-6 text-slate-700">{content}</div></div>;
}

function ScoreChip({ label, value }: { label: string; value?: number }) {
  return <span className="rounded-full bg-slate-100 px-2 py-1 text-[10px] text-slate-600">{label} {typeof value === "number" ? `${Math.round(value * 100)}%` : "—"}</span>;
}

function Summary({ label, value, suffix = "%", digits = 0 }: { label: string; value?: number; suffix?: string; digits?: number }) {
  const display = typeof value === "number" ? `${(suffix === "%" ? value * 100 : value).toFixed(digits)}${suffix}` : "—";
  return <div className="rounded-md border border-line bg-white p-3"><div className="text-xs text-slate-500">{label}</div><div className="mt-1 text-xl font-semibold text-ink">{display}</div></div>;
}

function Score({ value }: { value?: number }) {
  return <td className="py-3 pr-4 font-medium text-slate-700">{typeof value === "number" ? `${Math.round(value * 100)}%` : "—"}</td>;
}

function PassLabel({ text = "通过" }: { text?: string }) {
  return <span className="rounded-full bg-emerald-50 px-2.5 py-1 text-xs font-medium text-emerald-700">{text}</span>;
}

function FailLabel({ text = "未通过" }: { text?: string }) {
  return <span className="rounded-full bg-rose-50 px-2.5 py-1 text-xs font-medium text-rose-700">{text}</span>;
}

function RunningLabel() {
  return <span className="rounded-full bg-sky-50 px-2.5 py-1 text-xs font-medium text-sky-700">运行中</span>;
}

function casePassed(item: RagCaseResult) {
  return item.hard_rules?.passed && !Object.keys(item.ragas_errors || {}).length && !(item.diagnoses || []).some((diagnosis) => diagnosis.severity !== "passed");
}

function formatMetric(name: string, value: number) {
  return name.includes("latency") ? `${Math.round(value)} ms` : `${(value * 100).toFixed(1)}%`;
}
