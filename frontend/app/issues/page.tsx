// ============================================================================
// app/issues/page.tsx —— 「Issue 智能分析」页面
// ----------------------------------------------------------------------------
// 功能：左边列出当前第一个仓库的 Issue 列表；点选某个 Issue 后，右边展示它的正文，
//   并提供「分析当前 Issue」按钮，点击会调 POST /api/issues/{id}/analyze，
//   让 AI 生成分类/优先级/负责人建议等，结果以 JSON 文本展示在右侧。
// 关键状态：issues（列表）、selected（当前选中的）、analysis（AI 返回的分析结果）。
// 调用接口：GET /api/repos（拿仓库列表）、GET /api/repos/{id}/issues（拿 Issue）、
//          POST /api/issues/{id}/analyze（让 AI 分析）。
// 这是客户端组件：有交互和状态，需在浏览器运行。
// ============================================================================

"use client";

// useEffect：组件加载后执行副作用（这里用于首次拉数据）；useState：声明可变状态。
import { useEffect, useState } from "react";

// 引入 UI 积木组件（Card/标题/按钮）。
import { Card, GhostButton, PageTitle, PrimaryButton } from "@/components/ui/card";
// 引入统一封装的请求函数。
import { apiGet, apiPost } from "@/lib/api";
// 引入仓库类型（拿仓库列表时用）。
import type { Repository } from "@/lib/types";

// 本页用到的 Issue 数据类型（id/编号/标题/状态/标签/正文）。
type Issue = { id: string; number: number; title: string; state: string; labels: string[]; body?: string };

// 默认导出的页面组件。
export default function IssuesPage() {
  // issues：Issue 列表；初始为空数组 []。泛型 <Issue[]> 表示「这是一个 Issue 数组」。
  const [issues, setIssues] = useState<Issue[]>([]);
  // selected：当前选中的 Issue；初始为 null（没选）。
  const [selected, setSelected] = useState<Issue | null>(null);
  // analysis：AI 分析结果文本；初始空字符串。
  const [analysis, setAnalysis] = useState("");

  // useEffect 第二个参数是空数组 []：表示「只在组件首次挂载时跑一次」（拉初始数据）。
  useEffect(() => {
    // 先拿仓库列表；apiGet 返回 Promise，用 .then 处理成功结果。
    apiGet<Repository[]>("/api/repos").then((repos) => {
      // 取第一个仓库（repos[0]），如果没有仓库就不继续。
      if (repos[0]) {
        // 再拿该仓库的 Issue 列表。
        apiGet<Issue[]>(`/api/repos/${repos[0].id}/issues`).then((items) => {
          setIssues(items); // 存到列表状态
          setSelected(items[0] ?? null); // 默认选中第一条（没有就 null）
        });
      }
    });
  }, []);

  // analyze：点击「分析当前 Issue」时调用。
  async function analyze() {
    if (!selected) return; // 没选中就直接返回，啥也不做
    // 调后端分析接口，传空对象 {} 作为 body；返回结果存进 analysis 状态。
    const data = await apiPost(`/api/issues/${selected.id}/analyze`, {});
    // JSON.stringify(..., null, 2) 把对象漂亮地格式化成带缩进的 JSON 字符串便于阅读。
    setAnalysis(JSON.stringify(data, null, 2));
  }

  return (
    // <> 是 React 的「片段」标签，用来包多个元素但不产生额外 DOM 节点。
    <>
      {/* 页面大标题 + 说明 */}
      <PageTitle title="Issue 智能分析" description="自动生成 Issue 分类、优先级、复杂度、负责人建议、相似问题和下一步行动项。" />
      {/* 两栏布局：左 380px 列表，右自适应；lg 以上才分栏 */}
      <div className="grid gap-4 lg:grid-cols-[380px_1fr]">
        {/* 左栏：Issue 列表卡片 */}
        <Card>
          <div className="flex items-center justify-between">
            <h2 className="font-semibold text-ink">Issue 列表</h2>
            {/* 右上角小徽章显示数量 */}
            <span className="rounded-full bg-panel px-2.5 py-1 text-xs text-slate-500">{issues.length} 条</span>
          </div>
          <div className="mt-4 space-y-2">
            {/* map：把 issues 数组每一项渲染成一个可点击的 GhostButton */}
            {issues.map((issue) => (
              // key 用 issue.id 作为唯一标识；onClick 选中该 issue
              <GhostButton key={issue.id} onClick={() => setSelected(issue)} className={selected?.id === issue.id ? "border-teal-300 bg-teal-50 text-teal-900" : ""}>
                <div className="flex items-center justify-between gap-3">
                  {/* 显示 #编号 + 标题 */}
                  <span>#{issue.number} {issue.title}</span>
                  {/* 右侧状态小标签 */}
                  <span className="rounded-full bg-slate-100 px-2 py-0.5 text-xs">{issue.state}</span>
                </div>
              </GhostButton>
            ))}
          </div>
        </Card>

        {/* 右栏：分析结果卡片 */}
        <Card>
          <h2 className="font-semibold text-ink">分析结果</h2>
          {/* 只有选中了 Issue 才显示它的正文框 */}
          {selected && (
            <div className="mt-3 rounded-md bg-panel p-3 text-sm leading-6 text-slate-600">
              <div className="font-medium text-ink">#{selected.number} {selected.title}</div>
              {/* 正文，没有就显示占位「暂无正文」 */}
              <div className="mt-1">{selected.body || "暂无正文"}</div>
            </div>
          )}
          {/* 分析按钮：没选中时 disabled 不可用 */}
          <PrimaryButton onClick={analyze} disabled={!selected} className="mt-4">分析当前 Issue</PrimaryButton>
          {/* 有分析结果就显示成 <pre> 代码块（黑底白字，可滚动） */}
          {analysis && <pre className="mt-4 max-h-[520px] overflow-auto rounded-md border border-slate-800 bg-slate-950 p-4 text-xs text-slate-100">{analysis}</pre>}
        </Card>
      </div>
    </>
  );
}
