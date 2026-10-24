// ============================================================================
// app/action-drafts/page.tsx —— 「安全草稿与人工确认」页面
// ----------------------------------------------------------------------------
// 功能：Agent 想做的「外部写操作」（如发报告）不直接执行，而是先落成「草稿」，
//   等有权限的人「确认执行」才真正执行，并把操作写入审计记录。这就是「安全闸门」。
//   左侧建草稿，右侧列出草稿（可确认/取消）+ 底部审计时间线。
// 关键状态：repos、repoId、drafts 草稿列表、audits 审计记录、表单字段、loading。
// 调用接口：GET /api/repos、GET /api/action-drafts、GET /api/action-drafts/audit-logs、
//   POST /api/action-drafts（建草稿）、POST /api/action-drafts/{id}/{confirm|cancel}。
// ============================================================================

"use client";

import { useEffect, useMemo, useState } from "react";
import { Check, CheckCircle2, Clock3, FileText, Send, ShieldAlert, X } from "lucide-react"; // 一批图标

import { apiGet, apiPost } from "@/lib/api";
import type { Repository } from "@/lib/types";
import { PageTitle, PrimaryButton } from "@/components/ui/card";

// 一条草稿的数据结构。
type ActionDraft = {
  id: string;
  repo_id: string;
  draft_type: string; // 草稿类型，如 send_report
  target_type: string; // 目标类型，如 repository
  title: string;
  content: string; // 待审查内容
  risk_level: string; // 风险等级 high/medium...
  status: string; // 状态：等待确认/已执行/已取消/失败
  execution_result?: Record<string, unknown> | null; // 执行结果
  error_message?: string | null; // 失败原因
  created_at: string;
  executed_at?: string | null;
};

// 一条审计记录。
type AuditLog = { id: string; action: string; status: string; target_type?: string | null; target_id?: string | null; created_at: string };

// 把草稿状态枚举映射成「中文标签 + 标签样式」。集中管理，渲染时直接取。
const statusMeta: Record<string, { label: string; className: string }> = {
  pending_confirmation: { label: "等待人工确认", className: "bg-amber-50 text-amber-800 border-amber-200" },
  executed: { label: "已确认并执行", className: "bg-emerald-50 text-emerald-700 border-emerald-200" },
  cancelled: { label: "已取消", className: "bg-slate-100 text-slate-600 border-slate-200" },
  failed: { label: "执行失败", className: "bg-rose-50 text-rose-700 border-rose-200" }
};

export default function ActionDraftsPage() {
  const [repos, setRepos] = useState<Repository[]>([]);
  const [repoId, setRepoId] = useState("");
  const [drafts, setDrafts] = useState<ActionDraft[]>([]);
  const [audits, setAudits] = useState<AuditLog[]>([]);
  const [title, setTitle] = useState("发布检查报告"); // 草稿标题
  const [content, setContent] = useState(""); // 草稿内容
  const [loading, setLoading] = useState(false);

  // 加载草稿 + 审计。repo 参数默认用当前 repoId；可附带 ?repo_id= 过滤。
  async function load(repo = repoId) {
    const suffix = repo ? `?repo_id=${repo}` : ""; // 拼查询参数
    // 同时拉草稿和审计；审计那个如果报错（.catch）就回退成空数组，不至于整个失败。
    const [draftItems, auditItems] = await Promise.all([
      apiGet<ActionDraft[]>(`/api/action-drafts${suffix}`),
      apiGet<AuditLog[]>(`/api/action-drafts/audit-logs${suffix}`).catch(() => [])
    ]);
    setDrafts(draftItems);
    setAudits(auditItems);
  }

  // 首次挂载：先拉仓库，再加载该仓库的草稿。
  useEffect(() => {
    apiGet<Repository[]>("/api/repos")
      .then((items) => {
        setRepos(items);
        setRepoId(items[0]?.id || "");
        return load(items[0]?.id || ""); // 链式：仓库加载完再 load()
      })
      .catch(() => setRepos([]));
  }, []);

  // 创建草稿：必填仓库和内容。
  async function createDraft() {
    if (!repoId || !content.trim()) return;
    setLoading(true);
    try {
      await apiPost<ActionDraft>("/api/action-drafts", {
        repo_id: repoId,
        draft_type: "send_report",
        target_type: "repository",
        title: title.trim() || "发布检查报告", // 空标题给默认
        content,
        risk_level: "medium", // 默认中风险
        metadata: { source: "manual_review" }
      });
      setContent(""); // 清空内容
      await load(); // 刷新列表
    } finally {
      setLoading(false);
    }
  }

  // 确认或取消某条草稿（action 只能是 confirm / cancel）。
  async function changeDraft(id: string, action: "confirm" | "cancel") {
    setLoading(true);
    try {
      await apiPost(`/api/action-drafts/${id}/${action}`, {});
      await load();
    } finally {
      setLoading(false);
    }
  }

  // useMemo：根据 drafts 算出「待确认数量」和「已执行数量」，依赖变化才重算。
  const pendingCount = useMemo(() => drafts.filter((draft) => draft.status === "pending_confirmation").length, [drafts]);
  const executedCount = useMemo(() => drafts.filter((draft) => draft.status === "executed").length, [drafts]);

  return (
    <>
      <PageTitle title="安全草稿与人工确认" description="Agent 的外部写操作先落成可审查草稿；只有具备批准权限的人确认后，系统才执行并写入审计记录。" />

      {/* 顶部三个统计卡（全部/待确认/已执行） */}
      <section className="grid gap-3 border-y border-line bg-white px-4 py-4 sm:grid-cols-3">
        <Summary icon={FileText} label="全部草稿" value={drafts.length} />
        <Summary icon={Clock3} label="待确认" value={pendingCount} tone="amber" />
        <Summary icon={CheckCircle2} label="已执行" value={executedCount} tone="green" />
      </section>

      <div className="mt-4 grid gap-5 xl:grid-cols-[360px_minmax(0,1fr)]">
        {/* 左：创建草稿表单 */}
        <section className="self-start rounded-md border border-line bg-white p-5">
          <div className="flex items-center gap-2 text-sm font-semibold text-ink"><ShieldAlert className="h-4 w-4 text-amber-600" />创建安全草稿</div>
          <p className="mt-2 text-xs leading-5 text-slate-500">这里只保存建议内容，不会直接写入外部系统。</p>
          <label className="mt-4 block text-sm font-medium text-slate-700">仓库
            <select className="mt-2 w-full rounded-md border border-line bg-white px-3 py-2 outline-none focus:border-teal-500" value={repoId} onChange={(event) => { setRepoId(event.target.value); void load(event.target.value); }}>
              {repos.map((repo) => <option key={repo.id} value={repo.id}>{repo.full_name}</option>)}
            </select>
          </label>
          <label className="mt-4 block text-sm font-medium text-slate-700">标题
            <input className="mt-2 w-full rounded-md border border-line px-3 py-2 outline-none focus:border-teal-500" value={title} onChange={(event) => setTitle(event.target.value)} />
          </label>
          <label className="mt-4 block text-sm font-medium text-slate-700">待审查内容
            <textarea className="mt-2 min-h-40 w-full resize-none rounded-md border border-line px-3 py-2 text-sm leading-6 outline-none focus:border-teal-500" value={content} onChange={(event) => setContent(event.target.value)} placeholder="输入准备发送的报告或评论内容" />
          </label>
          <PrimaryButton className="mt-4 w-full gap-2" disabled={!repoId || !content.trim() || loading} onClick={createDraft}><FileText className="h-4 w-4" />保存为待确认草稿</PrimaryButton>
        </section>

        {/* 右：草稿列表 + 审计时间线 */}
        <div className="space-y-4">
          {drafts.map((draft) => {
            // 取状态对应的样式；取不到就用兜底样式。
            const meta = statusMeta[draft.status] || { label: draft.status, className: "bg-panel text-slate-600 border-line" };
            return (
              <article key={draft.id} className="rounded-md border border-line bg-white p-5">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <h2 className="font-semibold text-ink">{draft.title || draft.draft_type}</h2>
                      {/* 状态标签 */}
                      <span className={`rounded-full border px-2.5 py-1 text-xs font-medium ${meta.className}`}>{meta.label}</span>
                      {/* 风险标签：high 红色，否则灰 */}
                      <span className={`rounded-full px-2.5 py-1 text-xs ${draft.risk_level === "high" ? "bg-rose-50 text-rose-700" : "bg-slate-100 text-slate-600"}`}>{draft.risk_level === "high" ? "高风险" : "中低风险"}</span>
                    </div>
                    {/* 时间格式化展示 */}
                    <div className="mt-2 text-xs text-slate-500">{draft.draft_type} · {draft.target_type} · {new Date(draft.created_at).toLocaleString("zh-CN")}</div>
                  </div>
                  {/* 只有等待确认状态才显示「确认/取消」按钮 */}
                  {draft.status === "pending_confirmation" && (
                    <div className="flex gap-2">
                      <button className="inline-flex items-center gap-1 rounded-md bg-teal-600 px-3 py-2 text-sm font-medium text-white transition hover:bg-teal-700 disabled:opacity-60" disabled={loading} onClick={() => changeDraft(draft.id, "confirm")}><Check className="h-4 w-4" />确认执行</button>
                      <button className="inline-flex items-center gap-1 rounded-md border border-line px-3 py-2 text-sm text-slate-600 transition hover:bg-panel disabled:opacity-60" disabled={loading} onClick={() => changeDraft(draft.id, "cancel")}><X className="h-4 w-4" />取消</button>
                    </div>
                  )}
                </div>
                {/* 草稿正文，保留换行 */}
                <div className="mt-4 whitespace-pre-wrap rounded-md border-l-4 border-teal-500 bg-panel px-4 py-3 text-sm leading-7 text-slate-700">{draft.content}</div>
                {/* 已执行时显示成功提示 */}
                {draft.status === "executed" && <div className="mt-3 flex items-center gap-2 text-sm text-emerald-700"><CheckCircle2 className="h-4 w-4" />人工确认完成，执行结果和操作者已经写入审计记录。</div>}
                {/* 失败原因 */}
                {draft.error_message && <div className="mt-3 rounded-md bg-rose-50 p-3 text-sm text-rose-700">{draft.error_message}</div>}
              </article>
            );
          })}
          {/* 没草稿的空态 */}
          {drafts.length === 0 && <div className="rounded-md border border-dashed border-line bg-white p-5 text-sm text-slate-500">暂无安全草稿。</div>}

          {/* 审计时间线 */}
          <section className="rounded-md border border-line bg-white p-5">
            <div className="flex items-center gap-2 font-semibold text-ink"><Send className="h-4 w-4 text-teal-700" />审计时间线</div>
            <div className="mt-4 space-y-0">
              {/* 只取前 10 条审计记录 */}
              {audits.slice(0, 10).map((item, index) => (
                <div key={item.id} className="relative flex gap-3 pb-4">
                  {/* 用绝对定位画一条连接线（最后一条不画） */}
                  {index < Math.min(audits.length, 10) - 1 && <span className="absolute left-[7px] top-4 h-full w-px bg-line" />}
                  {/* 圆点：成功绿、失败红 */}
                  <span className={`relative mt-1 h-4 w-4 shrink-0 rounded-full border-4 border-white ${item.status === "success" ? "bg-emerald-500" : "bg-rose-500"}`} />
                  <div><div className="text-sm font-medium text-ink">{auditLabel(item.action)}</div><div className="mt-1 text-xs text-slate-500">{item.status} · {new Date(item.created_at).toLocaleString("zh-CN")}</div></div>
                </div>
              ))}
              {audits.length === 0 && <div className="text-sm text-slate-500">暂无审计记录。</div>}
            </div>
          </section>
        </div>
      </div>
    </>
  );
}

// Summary：顶部统计小卡片。icon 是图标组件（大写才能当标签），tone 控制颜色。
function Summary({ icon: Icon, label, value, tone = "teal" }: { icon: typeof FileText; label: string; value: number; tone?: "teal" | "amber" | "green" }) {
  const color = tone === "amber" ? "text-amber-600" : tone === "green" ? "text-emerald-600" : "text-teal-700";
  // 把图标渲染出来（注意 Icon 首字母大写才能作为组件标签 <Icon/>）。
  return <div className="flex items-center gap-3"><Icon className={`h-5 w-5 ${color}`} /><div><div className="text-xs text-slate-500">{label}</div><div className="mt-1 text-xl font-semibold text-ink">{value}</div></div></div>;
}

// auditLabel：把后端返回的审计 action 字符串，翻译成中文标签。
function auditLabel(action: string) {
  if (action.startsWith("action_draft.confirm")) return "人工确认并执行草稿";
  if (action === "action_draft.create") return "创建待确认草稿";
  if (action === "action_draft.cancel") return "取消草稿";
  return action; // 其它直接原样显示
}
