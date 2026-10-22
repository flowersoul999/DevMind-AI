// ============================================================================
// app/layout.tsx —— 全站「根布局」（所有页面的公共外衣）
// ----------------------------------------------------------------------------
// 这是 Next.js App Router 的约定文件：它包裹每一个页面（包括首页和各子路由）。
// 它负责：1) 引入全局样式 globals.css；2) 设置浏览器标签的 metadata（标题/描述）；
//   3) 用 <html>/<body> 搭好页面骨架，并套上 <AppShell>（顶部导航）来包住页面内容。
// 注意：layout 本身是服务器组件（没有 "use client"），children 就是当前路由的页面。
// ============================================================================

// Metadata 是 Next.js 提供的类型，用来声明页面元信息（SEO / 浏览器标签）。
import type { Metadata } from "next";
// 引入全局样式（颜色变量、基础重置等），路径用相对写法是 Next 约定。
import "./globals.css";
// 引入前面写的外壳组件（顶部导航栏）。
import { AppShell } from "@/components/layout/app-shell";

// 导出 metadata 给 Next.js 使用：浏览器标签页会显示 "DevMind AI"。
export const metadata: Metadata = {
  title: "DevFlow AI",
  description: "PR / Issue intelligent collaboration agent for engineering teams"
};

// 默认导出的根布局组件。children 是「当前要显示的页面内容」，由 Next 自动注入。
// Readonly 表示这个 props 对象不应被修改（React 规范要求）。
export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    // <html> 是文档最外层标签，lang="zh-CN" 告诉浏览器页面是中文。
    <html lang="zh-CN">
      {/* <body> 是页面主体 */}
      <body>
        {/* 用 AppShell 包住页面内容：顶部导航 + 下方渲染 children */}
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
