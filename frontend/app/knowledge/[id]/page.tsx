// ============================================================================
// app/knowledge/[id]/page.tsx —— 单个知识库「详情页」（文档 / 召回测试 / 设置）
// ----------------------------------------------------------------------------
// 功能：进入某个知识库后，有三个 Tab：
//   - 文档：上传文件、看解析状态、点开查看真实分段内容（支持失败重试）
//   - 召回测试：输入问题，看被召回的分段、分数、排序，以及检索流水线 trace
//   - 设置：调 RetrievalSettings 组件改模型/分段/检索参数，保存（必要时触发重索引）
// 这个页面是本项目里交互最复杂的之一，大量用到 useState / useEffect / useRef / useCallback。
// 调用接口：GET /api/rag/repositories/{id}、/documents、/api/rag/models、
//   POST 上传、POST retrieval-tests、PATCH /config、POST /reindex 等。
// 注意 [id] 是「动态路由」：URL 里的 id 决定看哪个知识库（用 useParams 取）。
// ============================================================================

"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation"; // 取动态路由参数
import {
  ArrowLeft,
  BookOpen,
  Check,
  ChevronRight,
  FileSearch,
  FileText,
  History,
  LoaderCircle,
  Play,
  Plus,
  RotateCcw,
  Save,
  Settings,
  TestTube2,
  X
} from "lucide-react"; // 图标库

import { RetrievalSettings } from "@/components/rag/retrieval-settings"; // 设置表单组件
import { apiGet, apiPatch, apiPost, apiUpload } from "@/lib/api"; // 各类请求封装
import {
  formatBytes,
  formatDate,
  formatNumber,
  statusLabel,
  type KnowledgeBase,
  type KnowledgeChunk,
  type KnowledgeConfig,
  type KnowledgeDocument,
  type ModelCatalog,
  type RetrievalHistory,
  type RetrievalResponse
} from "@/lib/rag"; // 类型 + 格式化工具

// Tab 类型：只能是这三个之一。
type Tab = "documents" | "retrieval" | "settings";

// 三个 Tab 的展示配置（值/中文/图标）。
const tabs: { value: Tab; label: string; icon: typeof FileText }[] = [
  { value: "documents", label: "文档", icon: FileText },
  { value: "retrieval", label: "召回测试", icon: TestTube2 },
  { value: "settings", label: "设置", icon: Settings }
];

// 检索流水线各阶段的英文 -> 中文标签。
const pipelineStageLabel: Record<string, string> = {
  vector_recall: "向量召回",
  keyword_recall: "BM25 召回",
  weighted_fusion: "加权融合",
  single_retriever_selection: "单路候选",
  deduplication_and_parent_aggregation: "去重聚合",
  rerank: "重排",
  parent_expansion: "父级扩展",
  score_threshold: "分数门槛",
  context_compression: "证据压缩",
  answer_gate: "回答判断"
};

// 答案闸门决策的中文标签。
const answerGateLabel: Record<string, string> = {
  answer: "证据充足",
  ask_clarification: "需要补充范围",
  insufficient_evidence: "证据不足",
  conflict: "证据冲突"
};

// 默认导出的详情页组件。
export default function KnowledgeBaseDetailPage() {
  // useParams 取出 URL 里的 id（动态路由参数）；String 转成字符串。
  const id = String(useParams<{ id: string }>().id);
  // useRef：拿到 <input type=file> 的真实 DOM，用来编程式触发选择文件。
  const fileInput = useRef<HTMLInputElement>(null);
  const [tab, setTab] = useState<Tab>("documents"); // 当前 Tab
  const [knowledge, setKnowledge] = useState<KnowledgeBase | null>(null); // 知识库本体
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]); // 文档列表
  const [catalog, setCatalog] = useState<ModelCatalog | null>(null); // 可选模型清单
  const [config, setConfig] = useState<KnowledgeConfig | null>(null); // 检索配置（受控于设置表单）
  const [selectedDocument, setSelectedDocument] = useState<KnowledgeDocument | null>(null); // 当前查看分段详情的文档
  const [chunks, setChunks] = useState<KnowledgeChunk[]>([]); // 该文档的分段
  const [query, setQuery] = useState(""); // 召回测试问题
  const [retrieval, setRetrieval] = useState<RetrievalResponse | null>(null); // 召回结果
  const [history, setHistory] = useState<RetrievalHistory[]>([]); // 召回测试历史
  const [loading, setLoading] = useState(true);
  const [uploading, setUploading] = useState(false);
  const [testing, setTesting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState(""); // 成功提示
  const [error, setError] = useState("");

  // useCallback：把「加载核心数据」包成稳定函数（依赖 id），避免每次渲染都新建。
  // 同时拉知识库、文档列表、模型清单（Promise.all 并发），并把 config 初始化为 base.config。
  const load = useCallback(async () => {
    const [base, docs, models] = await Promise.all([
      apiGet<KnowledgeBase>(`/api/rag/repositories/${id}`),
      apiGet<KnowledgeDocument[]>(`/api/rag/repositories/${id}/documents`),
      apiGet<ModelCatalog>("/api/rag/models")
    ]);
    setKnowledge(base);
    setDocuments(docs);
    setCatalog(models);
    setConfig(base.config);
  }, [id]);

  // 加载召回测试历史。
  const loadHistory = useCallback(() => {
    apiGet<RetrievalHistory[]>(`/api/rag/repositories/${id}/retrieval-tests`).then(setHistory).catch(() => null);
  }, [id]);

  // 挂载时：先看 URL 里 ?tab= 想进哪个 Tab；再加载数据；最后加载历史。
  useEffect(() => {
    const initial = new URLSearchParams(window.location.search).get("tab");
    if (initial === "retrieval" || initial === "settings") setTab(initial);
    load()
      .catch((reason: Error) => setError(reason.message))
      .finally(() => setLoading(false));
    loadHistory();
  }, [load, loadHistory]);

  // 轮询：只要还有文档处于「处理中」（非 completed/failed），就每秒重新 load 一次，
  // 直到全部处理完。这样上传/解析进度会自动刷新。清理函数里清除定时器。
  useEffect(() => {
    if (!documents.some((item) => !["completed", "failed"].includes(item.status))) return; // 没有处理中的就退出
    const timer = window.setInterval(() => load().catch(() => null), 1000);
    return () => window.clearInterval(timer); // 组件卸载或依赖变化前清掉定时器，避免内存泄漏
  }, [documents, load]);

  // 切换 Tab，并用 replaceState 把 ?tab= 写进 URL（不刷新页面，方便刷新后停留在当前 Tab）。
  function changeTab(next: Tab) {
    setTab(next);
    window.history.replaceState(null, "", `${window.location.pathname}?tab=${next}`);
  }

  // 上传文件：逐个用 FormData 上传，全部传完 reload。
  async function uploadFiles(list: FileList | null) {
    if (!list?.length) return; // 没选文件直接返回（?. 防 null）
    setUploading(true);
    setError("");
    try {
      // FileList 不是真正的数组，用 Array.from 转数组再遍历。
      for (const file of Array.from(list)) {
        const form = new FormData(); // 上传文件要用 FormData（不是 JSON）
        form.append("file", file); // 字段名 file
        await apiUpload<KnowledgeDocument>(`/api/rag/repositories/${id}/documents`, form);
      }
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "上传失败");
    } finally {
      setUploading(false);
      if (fileInput.current) fileInput.current.value = ""; // 清空 input，允许重复选同一文件
    }
  }

  // 点开某文档看分段。
  async function openChunks(document: KnowledgeDocument) {
    setSelectedDocument(document);
    setChunks([]); // 先清空，等加载
    try {
      setChunks(await apiGet<KnowledgeChunk[]>(`/api/rag/repositories/${id}/documents/${document.id}/chunks`));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "读取分段失败");
    }
  }

  // 失败文档重试解析。
  async function retry(document: KnowledgeDocument) {
    await apiPost(`/api/rag/repositories/${id}/documents/${document.id}/retry`, {});
    await load();
  }

  // 召回测试：把 query 和当前 config 相关参数发给后端。
  async function runRetrieval() {
    if (!query.trim() || !config) return;
    setTesting(true);
    setError("");
    try {
      const result = await apiPost<RetrievalResponse>(`/api/rag/repositories/${id}/retrieval-tests`, {
        query: query.trim(),
        top_k: config.top_k,
        retrieval_method: config.retrieval_method,
        rerank_enabled: config.rerank_enabled,
        score_threshold_enabled: config.score_threshold_enabled,
        score_threshold: config.score_threshold
      });
      setRetrieval(result);
      loadHistory(); // 刷新测试历史
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "召回测试失败");
    } finally {
      setTesting(false);
    }
  }

  // 保存设置：PATCH config；如果换了 embedding 模型且已有分段，则触发重索引。
  async function saveSettings() {
    if (!config || !knowledge) return;
    setSaving(true);
    setMessage("");
    setError("");
    // 判断是否需要重索引：embedding 模型或 provider 变了。
    const reindexNeeded = config.embedding_model !== knowledge.config.embedding_model || config.embedding_provider !== knowledge.config.embedding_provider;
    try {
      const updated = await apiPatch<KnowledgeBase>(`/api/rag/repositories/${id}/config`, config);
      setKnowledge(updated);
      setConfig(updated.config);
      if (reindexNeeded && updated.chunk_count) {
        await apiPost(`/api/rag/${id}/reindex`, {});
        setMessage("设置已保存，现有分段已重新向量化。平时修改分段长度只会影响新上传的文档。");
      } else {
        setMessage("设置已保存。");
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "保存失败");
    } finally {
      setSaving(false);
    }
  }

  // 还在加载：显示加载占位（提前 return，下面的页面结构不渲染）。
  if (loading) return <div className="grid min-h-[70vh] place-items-center text-sm text-slate-500">正在加载项目知识...</div>;
  // 没拿到知识库或配置：显示错误（可能是没找到）。
  if (!knowledge || !config) return <div className="m-6 border border-red-200 bg-red-50 p-4 text-sm text-red-700">{error || "未找到仓库"}</div>;

  return (
    <div className="min-h-[calc(100vh-74px)] bg-[#f7f8fa]">
      {/* 页头：返回按钮 + 名称 + 统计 */}
      <header className="border-b border-line bg-white px-4 py-3">
        <div className="flex min-w-0 items-center gap-3">
          {/* 返回知识库列表 */}
          <Link href="/knowledge" title="返回项目知识" className="grid h-9 w-9 shrink-0 place-items-center rounded-md border border-line text-slate-600 hover:text-ink"><ArrowLeft className="h-4 w-4" /></Link>
          <span className="grid h-9 w-9 shrink-0 place-items-center rounded-md bg-teal-50 text-teal-700"><BookOpen className="h-4 w-4" /></span>
          <div className="min-w-0">
            <h1 className="truncate text-sm font-semibold text-ink">{knowledge.name}</h1>
            <p className="truncate text-xs text-slate-400">{knowledge.full_name}</p>
          </div>
          <div className="ml-auto hidden items-center gap-5 text-xs text-slate-500 sm:flex">
            <span>{formatNumber(knowledge.document_count)} 文档</span><span>{formatNumber(knowledge.chunk_count)} 分段</span><span>{formatNumber(knowledge.character_count)} 字符</span>
          </div>
        </div>
      </header>

      <div className="grid min-h-[calc(100vh-135px)] md:grid-cols-[190px_minmax(0,1fr)]">
        {/* 左侧栏：Tab 导航 + 运行状态 */}
        <aside className="border-b border-line bg-white p-3 md:border-b-0 md:border-r">
          <nav className="flex gap-1 overflow-x-auto md:block md:space-y-1">
            {tabs.map((item) => {
              const Icon = item.icon; // 取出图标组件
              return (
                <button key={item.value} type="button" onClick={() => changeTab(item.value)} className={`flex h-10 shrink-0 items-center gap-2 rounded-md px-3 text-sm transition md:w-full ${tab === item.value ? "bg-teal-50 font-medium text-teal-800" : "text-slate-600 hover:bg-slate-50"}`}>
                  <Icon className="h-4 w-4" /> {item.label}
                </button>
              );
            })}
          </nav>
          {/* 运行状态（Embedding/Rerank 是否可用） */}
          <div className="mt-5 hidden border-t border-line pt-4 text-xs leading-5 text-slate-400 md:block">
            <div className="font-medium text-slate-600">运行状态</div>
            <div className="mt-2">Embedding</div>
            <div className={knowledge.runtime.embedding.available ? "text-emerald-700" : "text-amber-700"}>
              {knowledge.runtime.embedding.available
                ? `${knowledge.runtime.embedding.provider} 就绪`
                : `${knowledge.runtime.embedding.provider} 不可用`}
            </div>
            <div className="mt-2">Rerank</div>
            <div className={knowledge.runtime.rerank.available ? "text-emerald-700" : "text-amber-700"}>
              {knowledge.runtime.rerank.available
                ? `${knowledge.runtime.rerank.provider} 就绪`
                : knowledge.runtime.rerank.fallback === "heuristic"
                  ? "启发式重排"
                  : `${knowledge.runtime.rerank.provider} 不可用`}
            </div>
          </div>
        </aside>

        {/* 主内容区：按当前 Tab 渲染不同内容 */}
        <main className="min-w-0 p-4 sm:p-6">
          {/* 错误条（可关闭） */}
          {error ? <div className="mb-4 flex items-center justify-between bg-red-50 px-4 py-3 text-sm text-red-700"><span>{error}</span><button onClick={() => setError("")} title="关闭"><X className="h-4 w-4" /></button></div> : null}

          {/* —— 文档 Tab —— */}
          {tab === "documents" ? (
            <div className="mx-auto max-w-6xl">
              <div className="flex items-end justify-between gap-4">
                <div><h2 className="text-lg font-semibold text-ink">文档</h2><p className="mt-1 text-sm text-slate-500">查看解析状态和真实分段内容。</p></div>
                {/* 隐藏的 file input，accept 限制可上传类型；触发上传用 uploadFiles */}
                <input ref={fileInput} type="file" multiple accept=".txt,.md,.markdown,.pdf,.docx,.json,.csv,.log" className="hidden" onChange={(event) => uploadFiles(event.target.files)} />
                {/* 点击「添加文件」时通过 ref 调 input.click() 打开系统文件框 */}
                <button type="button" onClick={() => fileInput.current?.click()} disabled={uploading} className="inline-flex h-10 items-center gap-2 rounded-md bg-teal-700 px-4 text-sm font-medium text-white disabled:opacity-50">
                  {uploading ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />} 添加文件
                </button>
              </div>

              {/* 文档表格（含表头 + 每行） */}
              <div className="mt-5 overflow-hidden border border-line bg-white shadow-sm">
                {/* 表头（仅 md 以上显示） */}
                <div className="hidden grid-cols-[minmax(220px,1fr)_120px_100px_100px_140px_34px] gap-3 border-b border-line bg-slate-50 px-4 py-2.5 text-xs font-medium text-slate-500 md:grid">
                  <span>名称</span><span>状态</span><span>分段</span><span>字符数</span><span>上传时间</span><span />
                </div>
                {documents.map((document) => (
                  // 整行是一个按钮；只有 completed 才能点开看分段
                  <button key={document.id} type="button" onClick={() => document.status === "completed" && openChunks(document)} className="grid w-full gap-2 border-b border-slate-100 px-4 py-3 text-left last:border-b-0 hover:bg-slate-50 md:grid-cols-[minmax(220px,1fr)_120px_100px_100px_140px_34px] md:items-center md:gap-3">
                    <span className="flex min-w-0 items-center gap-2"><FileText className="h-4 w-4 shrink-0 text-teal-700" /><span className="truncate text-sm font-medium text-ink">{document.name}</span></span>
                    {/* 状态标签：completed 绿、failed 红、其它黄 */}
                    <span className={`w-fit rounded px-2 py-1 text-xs ${document.status === "completed" ? "bg-emerald-50 text-emerald-700" : document.status === "failed" ? "bg-red-50 text-red-700" : "bg-amber-50 text-amber-700"}`}>
                      {statusLabel[document.status]}
                    </span>
                    <span className="text-xs text-slate-500"><span className="md:hidden">分段 </span>{formatNumber(document.chunk_count)}</span>
                    <span className="text-xs text-slate-500"><span className="md:hidden">字符 </span>{formatNumber(document.character_count)}</span>
                    <span className="text-xs text-slate-400">{formatDate(document.created_at)}</span>
                    {/* 失败文档显示「重试」按钮；否则显示箭头。stopPropagation 防止触发整行的 openChunks */}
                    {document.status === "failed" ? <span onClick={(event) => { event.stopPropagation(); retry(document); }} title="重试" className="grid h-7 w-7 place-items-center text-red-600"><RotateCcw className="h-4 w-4" /></span> : <ChevronRight className="hidden h-4 w-4 text-slate-300 md:block" />}
                  </button>
                ))}
                {/* 没文档的空态 */}
                {!documents.length ? <div className="py-20 text-center text-sm text-slate-500">还没有文档，添加一份资料开始构建。</div> : null}
              </div>
            </div>
          ) : null}

          {/* —— 召回测试 Tab —— */}
          {tab === "retrieval" ? (
            <div className="mx-auto max-w-7xl">
              <div><h2 className="text-lg font-semibold text-ink">召回测试</h2><p className="mt-1 text-sm text-slate-500">输入真实问题，观察被召回的分段、分数和排序。</p></div>
              <div className="mt-5 grid gap-5 xl:grid-cols-[minmax(360px,.72fr)_minmax(520px,1.28fr)]">
                <div className="space-y-5">
                  {/* 查询输入 + 测试记录 */}
                  <section className="border border-line bg-white p-4 shadow-sm">
                    <label className="text-xs font-medium text-slate-600">查询文本</label>
                    <textarea value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => { if ((event.ctrlKey || event.metaKey) && event.key === "Enter") runRetrieval(); }} maxLength={250} placeholder="例如：新员工可以请几天年假？" className="mt-2 min-h-36 w-full resize-y rounded-md border border-line p-3 text-sm leading-6 outline-none focus:border-teal-600" />
                    {/* onKeyDown：Ctrl/Cmd + Enter 快捷提交 */}
                    <div className="mt-3 flex items-center justify-between"><span className="text-xs text-slate-400">{query.length} / 250</span><button type="button" disabled={!query.trim() || testing} onClick={runRetrieval} className="inline-flex h-9 items-center gap-2 rounded-md bg-teal-700 px-4 text-sm font-medium text-white disabled:opacity-40">{testing ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />} 测试</button></div>
                  </section>
                  <section className="border border-line bg-white p-4 shadow-sm">
                    <div className="flex items-center justify-between"><h3 className="flex items-center gap-2 text-sm font-semibold text-ink"><History className="h-4 w-4" /> 测试记录</h3><span className="text-xs text-slate-400">最近 {history.length} 条</span></div>
                    <div className="mt-3 divide-y divide-slate-100">
                      {history.map((item) => <div key={item.id} className="py-3"><div className="truncate text-sm text-slate-700">{item.query}</div><div className="mt-1 flex gap-3 text-xs text-slate-400"><span>{item.result_count} 条结果</span><span>{item.took_ms} ms</span><span>{formatDate(item.created_at)}</span></div></div>)}
                      {!history.length ? <div className="py-8 text-center text-xs text-slate-400">暂无测试记录</div> : null}
                    </div>
                  </section>
                </div>

                {/* 召回结果 + 检索 trace */}
                <section className="min-w-0">
                  <div className="flex items-center justify-between"><h3 className="text-sm font-semibold text-ink">召回结果</h3>{retrieval ? <span className="text-xs text-slate-500">{retrieval.result_count} 条 · {retrieval.took_ms} ms</span> : null}</div>
                  {/* 有流水线阶段才显示 trace */}
                  {retrieval?.retrieval_config.pipeline?.stages?.length ? (
                    <div className="mt-3 border border-line bg-white p-4 shadow-sm">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <div className="text-xs font-medium text-slate-600">Retrieval Trace</div>
                        {/* 答案闸门决策标签 */}
                        <span className={`rounded px-2 py-1 text-xs font-medium ${retrieval.retrieval_config.answer_gate?.decision === "answer" ? "bg-emerald-50 text-emerald-700" : "bg-amber-50 text-amber-700"}`}>
                          {answerGateLabel[String(retrieval.retrieval_config.answer_gate?.decision ?? "")] ?? "等待判断"}
                        </span>
                      </div>
                      <div className="mt-3 flex flex-wrap items-center gap-2">
                        {retrieval.retrieval_config.pipeline.stages.map((stage, index) => {
                          const name = String(stage.name ?? "unknown");
                          const outputCount = stage.output_count ?? stage.candidate_count;
                          return (
                            <div key={`${name}-${index}`} className="flex items-center gap-2">
                              {index ? <span className="text-slate-300">→</span> : null} {/* 阶段之间用箭头连接 */}
                              <span className="rounded border border-slate-200 bg-slate-50 px-2 py-1 text-xs text-slate-600">
                                {pipelineStageLabel[name] ?? name}{typeof outputCount === "number" ? ` · ${outputCount}` : ""}
                              </span>
                            </div>
                          );
                        })}
                      </div>
                      {retrieval.retrieval_config.answer_gate?.reason ? <p className="mt-3 text-xs leading-5 text-slate-500">{retrieval.retrieval_config.answer_gate.reason}</p> : null}
                    </div>
                  ) : null}
                  <div className="mt-3 space-y-3">
                    {/* 每条召回结果一张卡片 */}
                    {retrieval?.results.map((result, index) => (
                      <article key={`${result.document_id}-${index}`} className="border border-line bg-white p-4 shadow-sm">
                        <div className="flex items-center gap-3 border-b border-slate-100 pb-3">
                          <span className="grid h-7 w-7 shrink-0 place-items-center rounded bg-teal-50 text-xs font-semibold text-teal-700">{index + 1}</span>
                          <div className="min-w-0 flex-1"><h4 className="truncate text-sm font-medium text-ink">{result.title}</h4><p className="mt-0.5 truncate text-xs text-slate-400">{String(result.metadata.filename ?? result.source_type)}</p></div>
                          <span className="rounded bg-emerald-50 px-2 py-1 text-xs font-semibold text-emerald-700">{result.score.toFixed(3)}</span>
                        </div>
                        <p className="mt-3 whitespace-pre-wrap text-sm leading-6 text-slate-700">{result.snippet}</p>
                        {/* 底部元信息：分段号、字符数、后端/融合/rerank 方式 */}
                        <div className="mt-3 flex flex-wrap gap-3 text-xs text-slate-400"><span>分段 #{String(result.metadata.chunk_index ?? index + 1)}</span><span>{result.snippet.length} 字符</span><span>{String(result.retrieval.keyword_backend ?? result.retrieval.vector_backend ?? "retrieval")}</span><span>{String(result.retrieval.fusion_method ?? retrieval.retrieval_config.fusion_method ?? config.retrieval_method)}</span><span>{String(result.retrieval.rerank_mode ?? result.retrieval.mode ?? "retrieval")}</span></div>
                      </article>
                    ))}
                    {!retrieval ? <div className="grid min-h-80 place-items-center border border-dashed border-slate-300 bg-white text-center"><div><FileSearch className="mx-auto h-8 w-8 text-slate-300" /><p className="mt-3 text-sm text-slate-500">输入问题后查看真实召回分段</p></div></div> : null}
                    {retrieval && !retrieval.results.length ? <div className="grid min-h-64 place-items-center border border-line bg-white text-sm text-slate-500">没有分段通过当前检索条件。</div> : null}
                  </div>
                </section>
              </div>
            </div>
          ) : null}

          {/* —— 设置 Tab —— */}
          {tab === "settings" ? (
            <div className="mx-auto max-w-4xl">
              <div><h2 className="text-lg font-semibold text-ink">知识库设置</h2><p className="mt-1 text-sm text-slate-500">控制后续入库和召回测试使用的模型与参数。</p></div>
              <div className="mt-5 border border-line bg-white p-5 shadow-sm sm:p-7">
                {/* 复用检索设置表单组件；value 受控、onChange 直接 setConfig */}
                <RetrievalSettings value={config} catalog={catalog} onChange={setConfig} />
                {message ? <div className="mt-5 flex items-center gap-2 bg-emerald-50 px-3 py-2.5 text-sm text-emerald-700"><Check className="h-4 w-4" /> {message}</div> : null}
                {/* 保存按钮：重叠 >= 分段长度时禁用（不合法配置） */}
                <div className="mt-6 flex justify-end border-t border-line pt-5"><button type="button" onClick={saveSettings} disabled={saving || config.chunk_overlap >= config.chunk_size} className="inline-flex h-10 items-center gap-2 rounded-md bg-teal-700 px-5 text-sm font-medium text-white disabled:opacity-40">{saving ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />} 保存设置</button></div>
              </div>
            </div>
          ) : null}
        </main>
      </div>

      {/* 分段详情抽屉：从右侧滑出的浮层。点遮罩关闭，点内容区不关闭（stopPropagation） */}
      {selectedDocument ? (
        <div className="fixed inset-0 z-50 flex justify-end bg-black/30" onMouseDown={() => setSelectedDocument(null)}>
          <div className="h-full w-full max-w-2xl overflow-y-auto border-l border-line bg-[#f7f8fa] shadow-2xl" onMouseDown={(event) => event.stopPropagation()}>
            <header className="sticky top-0 z-10 flex items-center gap-3 border-b border-line bg-white px-5 py-4"><FileText className="h-5 w-5 text-teal-700" /><div className="min-w-0 flex-1"><h2 className="truncate text-sm font-semibold text-ink">{selectedDocument.name}</h2><p className="text-xs text-slate-400">{selectedDocument.chunk_count} 个分段 · {formatBytes(selectedDocument.file_size)}</p></div><button title="关闭" onClick={() => setSelectedDocument(null)} className="grid h-8 w-8 place-items-center text-slate-500"><X className="h-4 w-4" /></button></header>
            <div className="space-y-3 p-4 sm:p-5">
              {chunks.map((chunk) => <article key={chunk.id} className="border border-line bg-white p-4 shadow-sm"><div className="mb-3 flex items-center justify-between"><span className="text-xs font-semibold text-teal-700">分段 #{chunk.position}</span><span className="text-xs text-slate-400">{chunk.character_count} 字符</span></div><p className="whitespace-pre-wrap text-sm leading-6 text-slate-700">{chunk.content}</p></article>)}
              {!chunks.length ? <div className="py-20 text-center text-sm text-slate-500">正在读取分段...</div> : null}
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
