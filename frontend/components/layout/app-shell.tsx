// ============================================================================
// components/layout/app-shell.tsx —— 全站「外壳」布局（顶部导航栏）
// ----------------------------------------------------------------------------
// 功能：这是每个页面外面都包着的一层框架。它在页面最上方放一条导航栏，
//   列出所有主要功能入口（Workspace / 知识 / RAG 工作室 / Repos …）。
//   子页面内容通过 children 塞进下方主区域。它只是一个静态展示组件，没有状态。
// 为什么抽成外壳：导航在每个页面都一样，单独写一次、所有页面复用，改导航只改这里。
// 用法：在 app/layout.tsx 里把 <AppShell>{children}</AppShell> 包住所有页面内容。
// ============================================================================

// next/link 的 Link 是 Next.js 的「客户端路由跳转」组件：点击不刷新整页，只换内容，更快。
import Link from "next/link";

// 导航项数组：每个入口有「跳转路径 href」和「显示文字 label」。
// 为什么用数组：后面用 map 一下就能渲染出一排链接，想加菜单项只改这里一行。
const navItems = [
  { href: "/", label: "Workspace" },
  { href: "/knowledge", label: "项目知识" },
  { href: "/rag", label: "RAG 工作室" },
  { href: "/repos", label: "Repos" },
  { href: "/action-drafts", label: "Drafts" },
  { href: "/workflow-runs", label: "Runs" },
  { href: "/workspaces", label: "Reports" },
  { href: "/code-graph", label: "Code Graph" },
  { href: "/evals", label: "Evals" }
];

// AppShell：外壳组件。children 是包在它里面的真实页面内容。
export function AppShell({ children }: { children: React.ReactNode }) {
  return (
    // <main> 是页面主体语义标签；min-h-screen 保证至少占满整屏高度。
    <main className="min-h-screen">
      {/* 顶部导航条：浅色背景 + 底部细线 */}
      <div className="border-b border-line bg-white/85 px-4 py-3">
        <div className="flex flex-wrap items-center gap-2">
          {/* 左侧品牌名 */}
          <div className="mr-2 text-sm font-semibold text-ink">DevMind AI</div>
          {/* 用 map 把 navItems 渲染成一组导航链接 */}
          {navItems.map((item) => (
            <Link key={item.href} href={item.href} className="rounded-md px-3 py-1.5 text-sm text-slate-600 transition hover:bg-teal-50 hover:text-teal-800">
              {item.label}
            </Link>
          ))}
        </div>
      </div>
      {/* 主内容区：把子页面渲染在这里，带一点内边距 */}
      <div className="w-full p-3">{children}</div>
    </main>
  );
}
