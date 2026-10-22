// ============================================================================
// components/ui/card.tsx —— 最基础的「UI 积木」组件
// ----------------------------------------------------------------------------
// 这个文件不放业务逻辑，只提供几个到处都能复用的「页面外观零件」：
//   Card          一个带圆角/边框/阴影的白色卡片容器（用来包裹一块内容）
//   PageTitle     页面大标题 + 副标题的统一样式
//   PrimaryButton 主按钮（实心、强调色，用于「确定/提交」等主操作）
//   GhostButton   幽灵按钮（描边、左对齐，常用于侧边栏的轻量选项）
// 为什么集中写：所有页面都用同一套视觉风格，改一处就能全局统一，避免每个页面各写一遍样式。
// ============================================================================

// clsx：一个把「条件类名」拼起来的小工具，比如 clsx("a", 条件 && "b")。
import { clsx } from "clsx";

// Card：卡片容器。props 里 className 是外部可追加的样式；children 是卡片里放的内容。
// React.ReactNode 表示「任何合法的 React 内容」（文字/标签/组件都行）。
export function Card({ className, children }: { className?: string; children: React.ReactNode }) {
  return (
    // <section> 是语义化的「区块」标签；clsx 把默认样式和调用方传入的 className 合并。
    // bg-white/90 的 /90 表示 90% 不透明（半透）；backdrop-blur 是毛玻璃模糊效果。
    <section className={clsx("rounded-lg border border-line/80 bg-white/90 p-5 shadow-sm shadow-slate-200/60 backdrop-blur", className)}>
      {children} {/* 把调用方传进来的子内容原样渲染在这里 */}
    </section>
  );
}

// PageTitle：页面标题区。接收标题 title 和描述 description 两段文案。
export function PageTitle({ title, description }: { title: string; description: string }) {
  return (
    <div className="mb-6 flex flex-col gap-2 border-b border-line/80 pb-5">
      {/* 小字「DevMind AI」品牌标识 */}
      <div className="text-xs font-medium uppercase tracking-[0.18em] text-accent">DevFlow AI</div>
      {/* 真正的页面大标题，md 尺寸以上字号更大 */}
      <h1 className="text-2xl font-semibold tracking-normal text-ink md:text-3xl">{title}</h1>
      {/* 灰色副标题说明文字 */}
      <p className="max-w-3xl text-sm leading-6 text-slate-600">{description}</p>
    </div>
  );
}

// PrimaryButton：主操作按钮。用 ...props 把没显式处理的属性（如 onClick、disabled）原样透传给 <button>。
// React.ButtonHTMLAttributes<HTMLButtonElement> 表示「支持普通 button 的所有原生属性」。
export function PrimaryButton({ children, className, ...props }: React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      className={clsx(
        // 默认样式：强调色背景、白字、悬停变深、禁用时变灰且不可点
        "inline-flex items-center justify-center rounded-md bg-accent px-4 py-2 text-sm font-medium text-white shadow-sm shadow-teal-900/10 transition hover:bg-teal-700 disabled:cursor-not-allowed disabled:opacity-60",
        className
      )}
      {...props} /* 把 onClick / disabled 等外部属性展开到 button 上 */
    >
      {children}
    </button>
  );
}

// GhostButton：次/轻量按钮，常用于侧栏列表项。默认整行可点、左对齐。
export function GhostButton({ children, className, ...props }: React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      className={clsx(
        "w-full rounded-md border border-line bg-white px-3 py-2 text-left text-sm text-slate-700 transition hover:border-teal-200 hover:bg-teal-50/70 hover:text-teal-900",
        className
      )}
      {...props}
    >
      {children}
    </button>
  );
}
