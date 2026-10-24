// ============================================================================
// app/workspaces/page.tsx —— 「团队工作区」页面（多仓库聚合周报）
// ----------------------------------------------------------------------------
// 功能：把多个仓库聚合成一个「Workspace」，可创建 workspace，也可直接勾选若干仓库，
//   然后选时间范围生成「跨仓库研发周报」。右侧展示 workspace 列表与生成的周报。
// 关键状态：repos、workspaces、name、selectedRepos（勾选的仓库 ID 数组）、
//   workspaceId（选中的 workspace）、起止日期、report、loading。
// 调用接口：GET /api/repos、GET /api/workspaces、POST /api/workspaces、
//   POST /api/workspaces/multi-repo-report。
// ============================================================================

"use client";

import { useEffect, useMemo, useState } from "react";

import { apiGet, apiPost } from "@/lib/api";
import type { Repository } from "@/lib/types";
import { Card, PageTitle, PrimaryButton } from "@/components/ui/card";

// 工作区结构：名字 + 描述 + 包含的仓库 ID 列表。
type Workspace = {
  id: string;
  name: string;
  description?: string | null;
  repo_ids: string[];
};

// 多仓库周报返回：Markdown 正文 + 指标字典 + 各仓库摘要。
type MultiRepoReport = {
  report_markdown: string;
  metrics: Record<string, number>;
  repo_summaries: Array<Record<string, unknown>>;
};

// 同 reports 页面：Date -> "YYYY-MM-DD"。
function toDateInput(date: Date) {
  return date.toISOString().slice(0, 10);
}

export default function WorkspacesPage() {
  const today = useMemo(() => new Date(), []);
  const start = useMemo(() => {
    const value = new Date(today);
    value.setDate(value.getDate() - 6); // 默认一周前
    return value;
  }, [today]);
  const [repos, setRepos] = useState<Repository[]>([]); // 仓库列表
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]); // 工作区列表
  const [name, setName] = useState("研发团队"); // 新工作区名字
  const [selectedRepos, setSelectedRepos] = useState<string[]>([]); // 勾选的仓库 ID
  const [workspaceId, setWorkspaceId] = useState(""); // 选中的工作区（用于生成报表）
  const [startDate, setStartDate] = useState(toDateInput(start));
  const [endDate, setEndDate] = useState(toDateInput(today));
  const [report, setReport] = useState<MultiRepoReport | null>(null); // 生成的报告
  const [loading, setLoading] = useState(false);

  // 一次性并发拉取仓库列表和工作区列表（Promise.all 同时发两个请求，更快）。
  async function load() {
    // 解构拿到两个并发请求的结果数组。
    const [repoItems, workspaceItems] = await Promise.all([apiGet<Repository[]>("/api/repos"), apiGet<Workspace[]>("/api/workspaces")]);
    setRepos(repoItems);
    setWorkspaces(workspaceItems);
    // 默认勾选前两个仓库的 ID。
    setSelectedRepos(repoItems.slice(0, 2).map((repo) => repo.id));
    setWorkspaceId(workspaceItems[0]?.id || "");
  }

  // 首次挂载加载数据。
  useEffect(() => {
    load().catch(() => undefined);
  }, []);

  // 创建 workspace：名字必填，调后端创建接口后重新加载列表。
  async function createWorkspace() {
    if (!name.trim()) return;
    setLoading(true);
    try {
      await apiPost<Workspace>("/api/workspaces", { name, repo_ids: selectedRepos });
      await load();
    } finally {
      setLoading(false);
    }
  }

  // 生成多仓库周报：如果选了 workspace 就用它，否则用勾选的仓库。
  async function generateReport() {
    setLoading(true);
    try {
      const data = await apiPost<MultiRepoReport>("/api/workspaces/multi-repo-report", {
        workspace_id: workspaceId || undefined, // 有 workspace 就传
        repo_ids: workspaceId ? [] : selectedRepos, // 没 workspace 才传勾选的仓库
        start_date: startDate,
        end_date: endDate
      });
      setReport(data);
    } finally {
      setLoading(false);
    }
  }

  return (
    <>
      <PageTitle title="团队工作区" description="把多个仓库聚合成 workspace，生成跨仓库研发周报和风险概览。" />
      <div className="grid gap-4 lg:grid-cols-[380px_1fr]">
        {/* 左：创建/配置区 */}
        <Card>
          <label className="text-sm font-medium text-slate-700">
            Workspace 名称
            <input className="mt-2 w-full rounded-md border border-line px-3 py-2 outline-none" value={name} onChange={(event) => setName(event.target.value)} />
          </label>
          <div className="mt-4 text-sm font-medium text-slate-700">仓库</div>
          <div className="mt-2 grid gap-2">
            {/* 每个仓库一个复选框：勾选即加入 selectedRepos */}
            {repos.map((repo) => (
              <label key={repo.id} className="flex items-center gap-2 rounded-md border border-line px-3 py-2 text-sm">
                <input
                  type="checkbox"
                  checked={selectedRepos.includes(repo.id)} // 是否已勾选
                  onChange={(event) => {
                    // 用函数式更新：根据是否勾选决定「加入」还是「过滤掉」该 ID。
                    setSelectedRepos((current) => (event.target.checked ? [...current, repo.id] : current.filter((id) => id !== repo.id)));
                  }}
                />
                {repo.full_name}
              </label>
            ))}
          </div>
          {/* 创建按钮：没勾仓库或正在处理时禁用 */}
          <PrimaryButton className="mt-4" disabled={loading || selectedRepos.length === 0} onClick={createWorkspace}>
            创建 Workspace
          </PrimaryButton>

          <div className="mt-6 grid gap-3 border-t border-line pt-4">
            <label className="text-sm font-medium text-slate-700">
              报表范围
              <select className="mt-2 w-full rounded-md border border-line px-3 py-2" value={workspaceId} onChange={(event) => setWorkspaceId(event.target.value)}>
                <option value="">使用上方勾选仓库</option>
                {workspaces.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name}
                  </option>
                ))}
              </select>
            </label>
            <input className="rounded-md border border-line px-3 py-2" type="date" value={startDate} onChange={(event) => setStartDate(event.target.value)} />
            <input className="rounded-md border border-line px-3 py-2" type="date" value={endDate} onChange={(event) => setEndDate(event.target.value)} />
            {/* 生成按钮：没 workspace 也没勾仓库时禁用 */}
            <PrimaryButton disabled={loading || (!workspaceId && selectedRepos.length === 0)} onClick={generateReport}>
              生成多仓库周报
            </PrimaryButton>
          </div>
        </Card>

        {/* 右：workspace 列表 + 周报 */}
        <div className="grid gap-4">
          <Card>
            <h2 className="font-semibold text-ink">Workspace 列表</h2>
            <div className="mt-3 grid gap-2">
              {workspaces.map((item) => (
                <div key={item.id} className="rounded-md border border-line bg-panel px-3 py-2 text-sm text-slate-700">
                  {item.name} · {item.repo_ids.length} 个仓库
                </div>
              ))}
              {/* 没有就提示 */}
              {workspaces.length === 0 && <div className="text-sm text-slate-500">暂无 workspace。</div>}
            </div>
          </Card>
          <Card>
            <h2 className="font-semibold text-ink">多仓库周报</h2>
            {report ? (
              // 展示 Markdown 报告正文
              <pre className="mt-4 max-h-[620px] overflow-auto rounded-md border border-slate-800 bg-slate-950 p-4 text-sm leading-7 text-slate-100">{report.report_markdown}</pre>
            ) : (
              <div className="mt-4 rounded-md border border-line bg-panel p-4 text-sm text-slate-600">创建 workspace 或勾选仓库后生成周报。</div>
            )}
          </Card>
        </div>
      </div>
    </>
  );
}
