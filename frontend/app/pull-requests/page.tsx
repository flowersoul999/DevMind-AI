// ============================================================================
// app/pull-requests/page.tsx —— 「PR Review 助手」页面
// ----------------------------------------------------------------------------
// 功能：展示当前第一个仓库的 Pull Request 列表（左），选中某个 PR 后右侧显示它改了哪些文件
//   （带 +/- 行数），并提供「生成 PR 分析」按钮调 POST /api/pull-requests/{id}/analyze，
//   让 AI 生成 PR 摘要、风险点、Review Checklist 与测试建议，结果以 JSON 展示。
// 关键状态：prs（列表）、selected（选中项）、analysis（AI 结果）。
// 调用接口：GET /api/repos、GET /api/repos/{id}/pull-requests、POST /api/pull-requests/{id}/analyze。
// 结构与 issues 页面几乎一致，只是换成了 PR 数据。
// ============================================================================

"use client";

import { useEffect, useState } from "react";

import { Card, GhostButton, PageTitle, PrimaryButton } from "@/components/ui/card";
import { apiGet, apiPost } from "@/lib/api";
import type { Repository } from "@/lib/types";

// 本页的 PullRequest 类型：除基本字段外，files 是「改动文件数组」，每项含文件名与增删行数。
type PullRequest = {
  id: string;
  number: number;
  title: string;
  state: string;
  files: { filename: string; additions: number; deletions: number }[];
};

export default function PullRequestsPage() {
  const [prs, setPrs] = useState<PullRequest[]>([]); // PR 列表
  const [selected, setSelected] = useState<PullRequest | null>(null); // 当前选中的 PR
  const [analysis, setAnalysis] = useState(""); // AI 分析结果

  // 首次挂载时拉数据：先仓库列表，再该仓库的 PR 列表，并默认选中第一条。
  useEffect(() => {
    apiGet<Repository[]>("/api/repos").then((repos) => {
      if (repos[0]) {
        apiGet<PullRequest[]>(`/api/repos/${repos[0].id}/pull-requests`).then((items) => {
          setPrs(items);
          setSelected(items[0] ?? null);
        });
      }
    });
  }, []);

  // 调后端分析该 PR。
  async function analyze() {
    if (!selected) return;
    const data = await apiPost(`/api/pull-requests/${selected.id}/analyze`, {});
    setAnalysis(JSON.stringify(data, null, 2)); // 漂亮格式化成 JSON 文本
  }

  return (
    <>
      <PageTitle title="PR Review 助手" description="读取 changed files 和 diff，生成 PR 摘要、风险点、Review Checklist 与测试建议。" />
      {/* 左右两栏：左列表 380px，右详情 */}
      <div className="grid gap-4 lg:grid-cols-[380px_1fr]">
        <Card>
          <div className="flex items-center justify-between">
            <h2 className="font-semibold text-ink">Pull Requests</h2>
            <span className="rounded-full bg-panel px-2.5 py-1 text-xs text-slate-500">{prs.length} 个</span>
          </div>
          <div className="mt-4 space-y-2">
            {prs.map((pr) => (
              <GhostButton key={pr.id} onClick={() => setSelected(pr)} className={selected?.id === pr.id ? "border-teal-300 bg-teal-50 text-teal-900" : ""}>
                #{pr.number} {pr.title}
              </GhostButton>
            ))}
          </div>
        </Card>

        <Card>
          <h2 className="font-semibold text-ink">Review 报告</h2>
          {/* 选中 PR 才显示其改动文件 */}
          {selected && (
            <div className="mt-3 rounded-md bg-panel p-3">
              <div className="text-sm font-medium text-ink">#{selected.number} {selected.title}</div>
              <div className="mt-3 grid gap-2">
                {/* 把每个改动文件渲染一行：文件名 + 增删行数 */}
                {selected.files.map((file) => (
                  <div key={file.filename} className="flex items-center justify-between rounded-md bg-white px-3 py-2 text-sm text-slate-600">
                    <span className="truncate">{file.filename}</span> {/* truncate：文件名过长就省略号 */}
                    {/* 显示 +新增 / -删除 行数 */}
                    <span className="ml-3 whitespace-nowrap text-xs text-slate-500">+{file.additions} / -{file.deletions}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
          <PrimaryButton onClick={analyze} disabled={!selected} className="mt-4">生成 PR 分析</PrimaryButton>
          {analysis && <pre className="mt-4 max-h-[520px] overflow-auto rounded-md border border-slate-800 bg-slate-950 p-4 text-xs text-slate-100">{analysis}</pre>}
        </Card>
      </div>
    </>
  );
}
