// ============================================================================
// components/feedback-controls.tsx —— 聊天回答的「点赞/点踩 + 反馈」小组件
// ----------------------------------------------------------------------------
// 功能：在每条 AI 回答下面显示 👍/👎 两个按钮。点 👍 直接记录「有帮助」；
//   点 👎 弹出一个小面板，让用户选「哪里不好」（不准确/不相关…）并可补充文字，
//   再提交到后端保存。提交会调用 POST /api/chat/feedback。
// 关键状态（useState）：
//   feedback   已保存的反馈记录（初始可能是父组件传来的，表示之前点过）
//   panelOpen  那个「吐槽面板」是否打开
//   reason     选中的不爽原因；comment 补充文字
//   pending    是否正在提交（用来显示转圈、禁用按钮）；error 提交失败提示
// 这是客户端组件（"use client"）：因为它有交互和自己的状态，需要在浏览器里运行。
// ============================================================================

// "use client"：声明这是一个客户端组件（有状态/事件，不能在服务器侧静态渲染）。
"use client";

// 从图标库引入需要用到的图标（对勾、转圈、上下拇指、关闭叉）。
import { Check, LoaderCircle, ThumbsDown, ThumbsUp, X } from "lucide-react";
// React 的钩子：useEffect 在「某些值变化时执行副作用」，useState 声明一个可变状态。
import { useEffect, useState } from "react";

// 引入统一封装的 POST 请求函数（见 lib/api.ts 的解释）。
import { apiPost } from "@/lib/api";

// ChatFeedbackRecord：后端返回的一条「反馈记录」长什么样。
export type ChatFeedbackRecord = {
  id: string;
  repo_id: string; // 所属仓库
  conversation_id: string; // 所属对话
  assistant_message_id: string; // 被评价的 AI 消息 ID
  run_id?: string | null; // 关联的一次模型运行（用于追踪）
  rating: "helpful" | "unhelpful"; // 好/差评
  reason?: FeedbackReason | null; // 差评原因（好评为 null）
  comment?: string | null; // 补充说明
  review_status: "open" | "in_review" | "resolved" | "dismissed"; // 人工处理状态
  notification_status: string; // 通知是否发出
  notification_error?: string | null; // 通知失败信息
};

// FeedbackReason：差评可选原因（联合类型，只能是这些字符串之一）。
type FeedbackReason =
  | "inaccurate"
  | "not_relevant"
  | "missing_context"
  | "unreliable_citation"
  | "tool_error"
  | "other";

// FeedbackControlsProps：这个组件接收哪些参数（由父组件传入）。
type FeedbackControlsProps = {
  repoId: string;
  conversationId: string;
  messageId: string;
  initialFeedback?: ChatFeedbackRecord; // 可选的「已有反馈」，用于回显
  onSaved: (feedback: ChatFeedbackRecord) => void; // 保存成功后回调父组件
};

// 把原因枚举映射成「中文标签 + 值」，用于渲染那一排可点选的原因按钮。
const REASONS: Array<{ value: FeedbackReason; label: string }> = [
  { value: "inaccurate", label: "内容不准确" },
  { value: "not_relevant", label: "没有解决问题" },
  { value: "missing_context", label: "缺少关键上下文" },
  { value: "unreliable_citation", label: "引用不可靠" },
  { value: "tool_error", label: "工具执行异常" },
  { value: "other", label: "其他" }
];

// 组件主体。把 props 解构成单独变量方便使用。
export function FeedbackControls({
  repoId,
  conversationId,
  messageId,
  initialFeedback,
  onSaved
}: FeedbackControlsProps) {
  // 用 useState 声明状态：每个状态是一个「当前值 + 修改它的函数」。
  const [feedback, setFeedback] = useState(initialFeedback); // 已保存的反馈
  const [panelOpen, setPanelOpen] = useState(false); // 吐槽面板开关
  const [reason, setReason] = useState<FeedbackReason | undefined>(initialFeedback?.reason ?? undefined); // 选中的原因
  const [comment, setComment] = useState(initialFeedback?.comment ?? ""); // 补充文字
  const [pending, setPending] = useState(false); // 提交中
  const [error, setError] = useState(""); // 错误提示

  // useEffect：当 initialFeedback 变化时，把组件内部状态同步成外部传入的最新值。
  // 为什么：父组件重新加载出已存在的反馈时，这里要跟着更新回显，否则会显示旧的。
  useEffect(() => {
    setFeedback(initialFeedback);
    setReason(initialFeedback?.reason ?? undefined);
    setComment(initialFeedback?.comment ?? "");
  }, [initialFeedback]); // 依赖数组：只有 initialFeedback 变了才重跑

  // submit：提交反馈。rating 决定是好评还是差评。
  async function submit(rating: "helpful" | "unhelpful") {
    setPending(true); // 进入提交中（按钮会转圈+禁用）
    setError(""); // 清空旧错误
    try {
      // 调后端接口保存反馈；泛型 <ChatFeedbackRecord> 表示返回类型。
      const result = await apiPost<ChatFeedbackRecord>("/api/chat/feedback", {
        repo_id: repoId,
        conversation_id: conversationId,
        assistant_message_id: messageId,
        rating,
        // 只有差评才带上原因和评论；好评这两项传 null。
        reason: rating === "unhelpful" ? reason : null,
        comment: rating === "unhelpful" ? comment.trim() || null : null
      });
      setFeedback(result); // 用后端返回的最新记录覆盖本地状态
      setPanelOpen(false); // 关闭面板
      onSaved(result); // 通知父组件「保存好了」
    } catch (requestError) {
      // 出错时把错误对象转成可读文字；instanceof 判断确保拿到的是 Error 类型。
      setError(requestError instanceof Error ? requestError.message : "反馈提交失败");
    } finally {
      // 无论成功失败，都结束「提交中」状态。
      setPending(false);
    }
  }

  return (
    // 整体是一个带顶部细线的容器
    <div className="mt-3 border-t border-line pt-2">
      {/* 第一行：两个拇指按钮 + 已记录提示 */}
      <div className="flex min-h-8 items-center gap-1">
        {/* 👍 好评按钮 */}
        <button
          type="button" // type=button 避免被误当成表单提交按钮
          title="回答有帮助" // 鼠标悬停提示
          aria-label="回答有帮助" // 读屏软件用的无障碍标签
          aria-pressed={feedback?.rating === "helpful"} // 无障碍：是否被按下
          disabled={pending} // 提交中禁用，防止重复点
          // onClick：点击时调用 submit("helpful")。void 前缀是 TS 写法，表示「不等待这个 Promise」。
          onClick={() => void submit("helpful")}
          className={`flex h-8 w-8 items-center justify-center rounded-md transition disabled:cursor-not-allowed disabled:opacity-50 ${
            feedback?.rating === "helpful" ? "bg-emerald-50 text-emerald-700" : "text-slate-400 hover:bg-slate-100 hover:text-slate-700"
          }`}
        >
          {/* 提交中且面板没开时显示转圈图标，否则显示👍 */}
          {pending && !panelOpen ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <ThumbsUp className="h-4 w-4" />}
        </button>
        {/* 👎 差评按钮：点击只是打开吐槽面板，不直接提交 */}
        <button
          type="button"
          title="回答没帮助"
          aria-label="回答没帮助"
          aria-pressed={feedback?.rating === "unhelpful"}
          disabled={pending}
          onClick={() => {
            setPanelOpen(true); // 打开面板
            setError(""); // 清错误
          }}
          className={`flex h-8 w-8 items-center justify-center rounded-md transition disabled:cursor-not-allowed disabled:opacity-50 ${
            feedback?.rating === "unhelpful" ? "bg-rose-50 text-rose-700" : "text-slate-400 hover:bg-slate-100 hover:text-slate-700"
          }`}
        >
          <ThumbsDown className="h-4 w-4" />
        </button>
        {/* 已经有反馈且面板没开时，显示「已记录」+ 运行 trace 片段 */}
        {feedback && !panelOpen && (
          <span className="ml-1 flex min-w-0 items-center gap-1 text-xs text-slate-500">
            <Check className="h-3.5 w-3.5 shrink-0 text-emerald-600" />
            已记录
            {/* 有 run_id 就显示前 8 位作为追踪标识；slice(0,8) 截取前 8 个字符 */}
            {feedback.run_id && <span className="truncate font-mono text-[11px]">trace {feedback.run_id.slice(0, 8)}</span>}
          </span>
        )}
      </div>

      {/* 只有面板打开时才渲染吐槽面板 */}
      {panelOpen && (
        <div className="mt-2 max-w-xl rounded-md border border-rose-100 bg-rose-50/60 p-3">
          <div className="flex items-center justify-between gap-3">
            <span className="text-xs font-medium text-slate-700">这条回答哪里需要改进？</span>
            {/* 关闭按钮（右上角叉） */}
            <button
              type="button"
              title="关闭"
              aria-label="关闭反馈"
              onClick={() => setPanelOpen(false)} // 点叉关闭面板
              className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-slate-400 hover:bg-white hover:text-slate-700"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
          {/* 一排原因按钮：用 map 把 REASONS 数组渲染成多个可选按钮 */}
          <div className="mt-2 flex flex-wrap gap-2">
            {REASONS.map((item) => (
              <button
                key={item.value} // key 是列表渲染必备的唯一标识，帮助 React 高效更新
                type="button"
                aria-pressed={reason === item.value} // 当前是否选中
                onClick={() => setReason(item.value)} // 选中该原因
                className={`rounded-md border px-2.5 py-1 text-xs transition ${
                  reason === item.value
                    ? "border-rose-300 bg-white text-rose-700" // 选中态高亮
                    : "border-slate-200 bg-white text-slate-600 hover:border-slate-300"
                }`}
              >
                {item.label}
              </button>
            ))}
          </div>
          {/* 补充说明文本框：受控组件，value 绑 state，onChange 更新 state */}
          <textarea
            value={comment}
            maxLength={1000} // 最多 1000 字
            rows={2}
            // 输入框变化就更新 comment；event.target.value 是当前输入内容
            onChange={(event) => setComment(event.target.value)}
            placeholder="补充说明（可选）"
            className="mt-2 w-full resize-y rounded-md border border-slate-200 bg-white px-3 py-2 text-xs leading-5 text-slate-700 outline-none focus:border-rose-300 focus:ring-2 focus:ring-rose-100"
          />
          <div className="mt-2 flex items-center justify-between gap-3">
            {/* 左边显示错误信息（红色），没错误时是空 */}
            <span className="text-xs text-rose-600">{error}</span>
            {/* 提交反馈按钮：调 submit("unhelpful") */}
            <button
              type="button"
              disabled={pending} // 提交中禁用
              onClick={() => void submit("unhelpful")}
              className="flex h-8 shrink-0 items-center gap-1.5 rounded-md bg-slate-800 px-3 text-xs font-medium text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {pending && <LoaderCircle className="h-3.5 w-3.5 animate-spin" />}
              提交反馈
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
