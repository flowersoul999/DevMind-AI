// ============================================================================
// app/reports/page.tsx —— 「研发周报生成」页面
// ----------------------------------------------------------------------------
// 功能：选一个仓库 + 起止日期，点「生成周报」调 POST /api/reports/weekly，
//   后端统计这段时间里的 Issues / PRs / 合并 PR / 打开 PR / 失败 CI，并生成
//   一份 Markdown 周报，返回后直接以代码块展示（黑底白字，可复制）。
// 关键状态：repos 列表、repoId 选中、startDate/endDate 日期、report 结果、loading。
// 调用接口：GET /api/repos、POST /api/reports/weekly。
// ============================================================================

"use client";

import { useEffect, useMemo, useState } from "react";

import { Card, PageTitle, PrimaryButton } from "@/components/ui/card";
import { apiGet, apiPost } from "@/lib/api";
import type { Repository } from "@/lib/types";

// 后端返回的周报结构：报告正文是 Markdown 字符串。
type WeeklyReportResponse = {
  report_markdown: string;
};

// 把 Date 转成 input[type=date] 需要的 "YYYY-MM-DD" 字符串（取 ISO 前 10 位）。
function toDateInput(date: Date) {
  return date.toISOString().slice(0, 10);
}

export default function ReportsPage() {
  // useMemo：缓存计算结果，只有依赖变化才重算。这里算出「今天」和「一周前」。
  const today = useMemo(() => new Date(), []);
  const weekStart = useMemo(() => {
    const date = new Date(today);
    date.setDate(date.getDate() - 6); // 往前推 6 天，加上今天正好约一周
    return date;
  }, [today]);

  const [repos, setRepos] = useState<Repository[]>([]); // 仓库列表
  const [repoId, setRepoId] = useState(""); // 选中的仓库
  const [startDate, setStartDate] = useState(toDateInput(weekStart)); // 开始日期（默认一周前）
  const [endDate, setEndDate] = useState(toDateInput(today)); // 结束日期（默认今天）
  const [report, setReport] = useState(""); // 周报正文
  const [loading, setLoading] = useState(false); // 生成中

  // 首次挂载加载仓库列表并默认选中第一个。
  useEffect(() => {
    apiGet<Repository[]>("/api/repos")
      .then((items) => {
        setRepos(items);
        setRepoId(items[0]?.id || "");
      })
      .catch(() => setRepos([])); // 出错置空，别白屏
  }, []);

  // 生成周报。
  async function generateReport() {
    if (!repoId) return; // 没选仓库就返回
    setLoading(true);
    try {
      const data = await apiPost<WeeklyReportResponse>("/api/reports/weekly", {
        repo_id: repoId,
        start_date: startDate,
        end_date: endDate
      });
      setReport(data.report_markdown); // 取出 Markdown 存到状态
    } finally {
      setLoading(false); // 无论成败都结束 loading
    }
  }

  return (
    <>
      <PageTitle title="研发周报生成" description="按时间范围统计 Issues、PRs、Merged PRs、Open PRs 和失败 CI，生成适合发给团队负责人的 Markdown 周报。" />

      <div className="grid gap-4 lg:grid-cols-[360px_1fr]">
        {/* 左：筛选表单 */}
        <Card>
          <div className="grid gap-4">
            <label className="text-sm font-medium text-slate-700">
              仓库
              <select
                className="mt-2 w-full rounded-md border border-line bg-white px-3 py-2 outline-none transition focus:border-teal-500 focus:ring-2 focus:ring-teal-100"
                value={repoId}
                onChange={(event) => setRepoId(event.target.value)}
              >
                {repos.map((repo) => (
                  <option key={repo.id} value={repo.id}>
                    {repo.full_name}
                  </option>
                ))}
              </select>
            </label>

            <label className="text-sm font-medium text-slate-700">
              开始日期
              <input
                type="date"
                className="mt-2 w-full rounded-md border border-line bg-white px-3 py-2 outline-none transition focus:border-teal-500 focus:ring-2 focus:ring-teal-100"
                value={startDate}
                onChange={(event) => setStartDate(event.target.value)}
              />
            </label>

            <label className="text-sm font-medium text-slate-700">
              结束日期
              <input
                type="date"
                className="mt-2 w-full rounded-md border border-line bg-white px-3 py-2 outline-none transition focus:border-teal-500 focus:ring-2 focus:ring-teal-100"
                value={endDate}
                onChange={(event) => setEndDate(event.target.value)}
              />
            </label>

            {/* 生成按钮：没选仓库或正在生成时禁用 */}
            <PrimaryButton onClick={generateReport} disabled={!repoId || loading}>
              {loading ? "生成中..." : "生成周报"}
            </PrimaryButton>
          </div>
        </Card>

        {/* 右：周报展示 */}
        <Card>
          <h2 className="font-semibold text-ink">Markdown 周报</h2>
          {/* 有报告就显示 <pre> 代码块；没有就显示引导文案 */}
          {report ? (
            <pre className="mt-4 max-h-[640px] overflow-auto rounded-md border border-slate-800 bg-slate-950 p-4 text-sm leading-7 text-slate-100">{report}</pre>
          ) : (
            <div className="mt-4 rounded-md border border-line bg-panel p-4 text-sm text-slate-600">选择仓库和时间范围后生成周报。</div>
          )}
        </Card>
      </div>
    </>
  );
}
