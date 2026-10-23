// ============================================================================
// app/chat/page.tsx —— 「Agent Chat」页面（和 AI Agent 对话）
// ----------------------------------------------------------------------------
// 功能：选一个仓库，输入问题（或用示例问题），点「Ask Agent」后调用流式接口
//   POST /api/chat/stream。Agent 会用工具去查代码、搜记忆、跑分析、起草安全操作。
//   回答是「打字机式」逐字返回的（delta 事件），最后有一个 final 事件给完整 JSON。
// 关键状态：repos、repoId、message（问题）、result（回答）、loading。
// 为什么用 apiPostStream：回答很长，流式能让用户边生成边看到，体验更顺；否则要干等。
// ============================================================================

"use client";

import { useEffect, useState } from "react";

import { Card, PageTitle, PrimaryButton } from "@/components/ui/card";
import { apiGet, apiPostStream } from "@/lib/api"; // apiPostStream 是流式请求封装
import type { Repository } from "@/lib/types";

// 几个示例问题，点击直接填入输入框。
const samples = [
  "Why did the latest CI fail?",
  "Summarize the risk in the latest PR",
  "Who should own the latest Issue?",
  "Generate this week's engineering report",
  "Read backend/app/services/agents/chat_agent.py and explain tool calling",
];

export default function ChatPage() {
  const [repos, setRepos] = useState<Repository[]>([]);
  const [repoId, setRepoId] = useState("");
  const [message, setMessage] = useState("Why did the latest CI fail?"); // 默认问题
  const [result, setResult] = useState(""); // 回答文本/JSON
  const [loading, setLoading] = useState(false);

  // 首次挂载加载仓库列表。
  useEffect(() => {
    apiGet<Repository[]>("/api/repos").then((items) => {
      setRepos(items);
      // 函数式更新：只在当前为空时才填入第一个仓库 ID，避免覆盖用户选择。
      setRepoId((current) => current || items[0]?.id || "");
    });
  }, []);

  // 发送消息：调流式接口，按事件类型处理。
  async function send() {
    setLoading(true);
    setResult(""); // 清空上一次回答
    try {
      // 第二个参数是发给后端的 body；第三个是每收到一个事件的回调。
      await apiPostStream("/api/chat/stream", { repo_id: repoId, message, context: { time_range: "7d", messages: [] } }, (event, rawData) => {
        // event 是事件名；rawData 是该事件的数据（unknown，需按需转换）。
        if (event === "delta") {
          // delta 事件：流式吐出的「增量文本」。把 rawData 当 { content } 用。
          const data = rawData as { content?: string };
          if (data.content) setResult((current) => current + data.content); // 追加到末尾
        }
        if (event === "final") {
          // final 事件：整段对话的最终完整结果，格式化成 JSON 展示。
          setResult(JSON.stringify(rawData, null, 2));
        }
      });
    } finally {
      setLoading(false); // 流结束后关闭 loading
    }
  }

  return (
    <>
      {/* 页面标题 */}
      <PageTitle
        title="Agent Chat"
        description="Use native tool calling to inspect code, search memory, run specialized analysis, and draft safe actions."
      />
      <Card>
        {/* 仓库选择 */}
        <select
          className="mb-3 w-full rounded-md border border-line bg-white px-3 py-2 text-sm"
          value={repoId}
          onChange={(event) => setRepoId(event.target.value)}
        >
          {repos.map((repo) => (
            <option key={repo.id} value={repo.id}>{repo.full_name}</option>
          ))}
        </select>
        {/* 问题输入框（多行） */}
        <textarea
          className="h-32 w-full rounded-md border border-line bg-white px-3 py-3 text-sm leading-6 outline-none transition focus:border-teal-500 focus:ring-2 focus:ring-teal-100"
          value={message}
          onChange={(event) => setMessage(event.target.value)}
        />

        {/* 示例问题快捷按钮：点击把示例填进 message */}
        <div className="mt-3 flex flex-wrap gap-2 text-xs text-slate-500">
          {samples.map((sample) => (
            <button
              key={sample}
              onClick={() => setMessage(sample)}
              className="rounded-full border border-line bg-panel px-3 py-1.5 transition hover:border-teal-200 hover:bg-teal-50"
            >
              {sample}
            </button>
          ))}
        </div>

        {/* 发送按钮：loading 中 / 没选仓库 / 没内容时禁用 */}
        <PrimaryButton onClick={send} disabled={loading || !repoId || !message.trim()} className="mt-4">
          {loading ? "Analyzing..." : "Ask Agent"}
        </PrimaryButton>

        {/* 有回答就展示 */}
        {result && <pre className="mt-4 max-h-[560px] overflow-auto rounded-md border border-slate-800 bg-slate-950 p-4 text-xs text-slate-100">{result}</pre>}
      </Card>
    </>
  );
}
