// ============================================================================
// app/repos/page.tsx —— 「仓库接入」页面（连接 / 同步 / 删除 Git 仓库）
// ----------------------------------------------------------------------------
// 功能：让用户把一个真实 Git 仓库接入 DevFlow。支持两种来源：
//   - remote（GitHub 地址）：填 owner + repo + token，连上后后台自动同步数据
//   - local（本地仓库）：填本地磁盘路径
//   右侧列出已连接仓库，可「设为当前查看 / 立即刷新 / 删除」。
// 调用接口：GET /api/repos、POST /api/repos/connect、POST /api/repos/{id}/sync、
//   POST /api/repos/{id}/select、DELETE /api/repos/{id}。
// 关键状态：一堆表单字段（owner/repo/token…）+ repos 列表 + 结果文本 result + loading。
// ============================================================================

"use client";

import { useEffect, useState } from "react";
import { Folder, Trash2 } from "lucide-react"; // 文件夹、垃圾桶图标

import { Card, PageTitle, PrimaryButton } from "@/components/ui/card";
import { apiDelete, apiGet, apiPost } from "@/lib/api"; // 删/查/增 三类请求封装
import { API_BASE_URL } from "@/lib/api"; // 后端基础地址（用于展示 webhook URL）
import type { Repository } from "@/lib/types";

// 连接仓库后后端返回的结果结构。
type RepoConnectResult = {
  repo_id: string;
  full_name: string;
  provider: string;
  api_base_url?: string | null;
  local_path?: string | null;
  checkout_mode?: string;
  default_branch?: string | null;
  connected: boolean; // 是否连接成功
  demo_mode: boolean; // 是否演示模式
  message: string; // 提示文案
};

// 来源类型：远程地址 or 本地路径。
type RepoSource = "remote" | "local";

// 同步结果结构：返回同步到的 issue/pr/ci 数量。
type RepoSyncResult = {
  repo_id: string;
  status: string;
  synced: {
    issues: number;
    pull_requests: number;
    workflow_runs: number;
  };
};

// 可选托管平台清单（下拉框用）。hint 是给用户的小提示。
const providers = [
  { value: "github", label: "GitHub", hint: "默认平台，支持 public repo 和 Personal Access Token。" },
  { value: "github_compatible", label: "自定义 GitHub-compatible API", hint: "适合 GitHub Enterprise、Gitea 或兼容 GitHub REST 的网关。" },
  { value: "gitlab", label: "GitLab", hint: "界面已预留，真实同步需要单独适配 GitLab API。" },
  { value: "gitee", label: "Gitee", hint: "界面已预留，真实同步需要单独适配 Gitee API。" }
];

export default function ReposPage() {
  const [repos, setRepos] = useState<Repository[]>([]); // 已连接仓库列表
  const [selectedRepoId, setSelectedRepoId] = useState<string>(""); // 当前查看的仓库 ID
  const [source, setSource] = useState<RepoSource>("remote"); // 来源：远程/本地
  const [provider, setProvider] = useState("github"); // 选中的平台
  const [owner, setOwner] = useState(""); // owner/namespace
  const [repo, setRepo] = useState(""); // 仓库名
  const [localPath, setLocalPath] = useState(""); // 本地路径
  const [cloneParentDir, setCloneParentDir] = useState(""); // 下载父目录
  const [apiBaseUrl, setApiBaseUrl] = useState("https://api.github.com"); // API 地址
  const [token, setToken] = useState(""); // 访问令牌
  const [result, setResult] = useState<string>(""); // 操作结果（JSON 文本）
  const [loading, setLoading] = useState(false); // 是否正在请求（控制按钮禁用/文案）

  // 根据 provider 找到对应配置项（找不到用第一个兜底）。
  const selectedProvider = providers.find((item) => item.value === provider) ?? providers[0];
  // 是否支持「真实连接」：目前只有 github / github_compatible 真正实现了同步。
  const supportsRealConnect = provider === "github" || provider === "github_compatible";

  // 重新拉取仓库列表，并可指定「接下来选中哪个仓库」。
  async function refreshRepos(nextSelectedId?: string) {
    const items = await apiGet<Repository[]>("/api/repos");
    setRepos(items);
    // 优先用传入的 ID，其次当前选中的，再次第一个仓库的 ID，都没有就空串。
    setSelectedRepoId(nextSelectedId || selectedRepoId || items[0]?.id || "");
  }

  // 首次挂载时加载仓库列表。catch 里把列表置空，避免中途出错导致白屏。
  useEffect(() => {
    refreshRepos().catch(() => setRepos([]));
  }, []);

  // 同步单个仓库：通知后端去拉 issue/pr/ci 数据（limit=50 上限）。
  async function syncRepo(repoId: string) {
    const data = await apiPost<RepoSyncResult>(`/api/repos/${repoId}/sync`, {
      sync_issues: true,
      sync_pull_requests: true,
      sync_workflow_runs: true,
      limit: 50
    });
    setSelectedRepoId(repoId);
    await refreshRepos(repoId); // 同步完刷新列表
    return data;
  }

  // 连接仓库：先校验必填，再调后端 connect 接口。
  async function connect() {
    // 远程来源必须填 owner 和 repo；.trim() 去掉首尾空格，空则拦截。
    if (source === "remote" && (!owner.trim() || !repo.trim())) {
      setResult(JSON.stringify({ error: "请填写 Owner / Namespace 和 Repository。" }, null, 2));
      return;
    }
    // 本地来源必须填路径。
    if (source === "local" && !localPath.trim()) {
      setResult(JSON.stringify({ error: "请填写本地 Git 仓库路径。" }, null, 2));
      return;
    }
    setLoading(true);
    try {
      // 根据来源拼不同字段：远程传 owner/repo，本地传 local_path。
      const connected = await apiPost<RepoConnectResult>("/api/repos/connect", {
        owner: source === "remote" ? owner.trim() : undefined,
        repo: source === "remote" ? repo.trim() : undefined,
        local_path: source === "local" ? localPath.trim() : undefined,
        clone_parent_dir: source === "remote" && cloneParentDir.trim() ? cloneParentDir.trim() : undefined,
        provider,
        // github 用默认地址所以不传；其它平台传用户填的 apiBaseUrl。
        api_base_url: provider === "github" ? undefined : apiBaseUrl,
        token: token || undefined, // 空串转 undefined，表示不传
        demo_mode: false
      });
      setResult(JSON.stringify({ connected, background_sync: "scheduled" }, null, 2));
      await refreshRepos(connected.repo_id); // 刷新并把新仓库设为当前
    } catch (error) {
      // 出错把错误信息格式化展示。instanceof 保证是 Error 对象才取 message。
      setResult(JSON.stringify({ error: error instanceof Error ? error.message : String(error) }, null, 2));
    } finally {
      setLoading(false);
    }
  }

  // 「立即刷新」按钮：对某个仓库重新同步。
  async function refreshRepo(repoId: string) {
    setLoading(true);
    try {
      const synced = await syncRepo(repoId);
      setResult(JSON.stringify({ refreshed: synced }, null, 2));
    } catch (error) {
      setResult(JSON.stringify({ error: error instanceof Error ? error.message : String(error) }, null, 2));
    } finally {
      setLoading(false);
    }
  }

  // 把某仓库「设为当前查看」。
  async function selectRepo(repoId: string) {
    setSelectedRepoId(repoId);
    const data = await apiPost(`/api/repos/${repoId}/select`, {});
    setResult(JSON.stringify(data, null, 2));
    await refreshRepos(repoId);
  }

  // 删除仓库：先弹窗二次确认（防止误删），确认后才调 DELETE。
  async function deleteRepo(item: Repository) {
    const confirmed = window.confirm(`确认删除仓库 ${item.full_name} 及其本地同步的 Issues、PR、CI、分析结果吗？此操作只删除本系统本地数据，不会删除远程 Git 仓库。`);
    if (!confirmed) return; // 用户取消就直接返回
    setLoading(true);
    try {
      const data = await apiDelete(`/api/repos/${item.id}`);
      setResult(JSON.stringify(data, null, 2));
      await refreshRepos(); // 删除后刷新列表
    } catch (error) {
      setResult(JSON.stringify({ error: error instanceof Error ? error.message : String(error) }, null, 2));
    } finally {
      setLoading(false);
    }
  }

  return (
    <>
      <PageTitle title="仓库接入" description="连接真实 Git 仓库后会立即触发后台同步；生产环境可配置 Webhook 实时触发，并通过定时同步兜底。" />

      {/* 左右两栏：左表单，右已连接列表 */}
      <div className="grid gap-4 xl:grid-cols-[1.15fr_0.85fr]">
        <Card>
          <div className="grid gap-4">
            {/* 来源切换：远程地址 / 本地仓库 两个按钮 */}
            <div className="grid grid-cols-2 overflow-hidden rounded-md border border-line bg-slate-50 p-1">
              <button
                type="button"
                onClick={() => setSource("remote")}
                className={`h-9 rounded px-3 text-sm font-medium transition ${source === "remote" ? "bg-white text-ink shadow-sm" : "text-slate-600 hover:text-ink"}`}
              >
                GitHub 地址
              </button>
              <button
                type="button"
                onClick={() => setSource("local")}
                className={`h-9 rounded px-3 text-sm font-medium transition ${source === "local" ? "bg-white text-ink shadow-sm" : "text-slate-600 hover:text-ink"}`}
              >
                本地仓库
              </button>
            </div>

            {/* 平台选择 + owner/repo 或本地路径 */}
            <div className="grid gap-4 lg:grid-cols-[260px_1fr_1fr]">
              <label className="text-sm font-medium text-slate-700">
                托管平台
                <select
                  className="mt-2 w-full rounded-md border border-line bg-white px-3 py-2 outline-none transition focus:border-teal-500 focus:ring-2 focus:ring-teal-100"
                  value={provider}
                  // 切换平台时顺手把 apiBaseUrl 设为该平台的默认地址
                  onChange={(event) => {
                    const next = event.target.value;
                    setProvider(next);
                    if (next === "github") setApiBaseUrl("https://api.github.com");
                    if (next === "github_compatible") setApiBaseUrl("https://api.example.com");
                    if (next === "gitlab") setApiBaseUrl("https://gitlab.com/api/v4");
                    if (next === "gitee") setApiBaseUrl("https://gitee.com/api/v5");
                  }}
                >
                  {providers.map((item) => (
                    <option key={item.value} value={item.value}>
                      {item.label}
                    </option>
                  ))}
                </select>
              </label>

              {/* 远程来源才显示 owner / repo 两个输入框 */}
              {source === "remote" ? (
                <>
                  <label className="text-sm font-medium text-slate-700">
                    Owner / Namespace
                    <input
                      className="mt-2 w-full rounded-md border border-line bg-white px-3 py-2 outline-none transition focus:border-teal-500 focus:ring-2 focus:ring-teal-100"
                      value={owner}
                      onChange={(event) => setOwner(event.target.value)}
                      placeholder="296569015"
                    />
                  </label>

                  <label className="text-sm font-medium text-slate-700">
                    Repository
                    <input
                      className="mt-2 w-full rounded-md border border-line bg-white px-3 py-2 outline-none transition focus:border-teal-500 focus:ring-2 focus:ring-teal-100"
                      value={repo}
                      onChange={(event) => setRepo(event.target.value)}
                      placeholder="clowder-ai"
                    />
                  </label>
                </>
              ) : (
                // 本地来源显示一个带文件夹图标的路径输入
                <label className="text-sm font-medium text-slate-700 lg:col-span-2">
                  本地仓库路径
                  <div className="mt-2 flex gap-2">
                    <input
                      className="min-w-0 flex-1 rounded-md border border-line bg-white px-3 py-2 outline-none transition focus:border-teal-500 focus:ring-2 focus:ring-teal-100"
                      value={localPath}
                      onChange={(event) => setLocalPath(event.target.value)}
                      placeholder="例如 D:\\code\\my-repo"
                    />
                    <span className="inline-flex w-10 shrink-0 items-center justify-center rounded-md border border-line bg-white text-slate-500">
                      <Folder className="h-4 w-4" />
                    </span>
                  </div>
                </label>
              )}
            </div>
          </div>

          {/* 平台说明卡片（显示当前平台的 hint） */}
          <div className="mt-4 rounded-md border border-teal-100 bg-teal-50/70 p-3 text-sm leading-6 text-slate-700">
            <div className="font-medium text-teal-900">{selectedProvider.label}</div>
            <div>{selectedProvider.hint}</div>
          </div>

          <div className="mt-4 grid gap-4 lg:grid-cols-2">
            {/* API Base URL：github 平台禁用（用默认） */}
            <label className="text-sm font-medium text-slate-700">
              API Base URL
              <input
                className="mt-2 w-full rounded-md border border-line bg-white px-3 py-2 outline-none transition focus:border-teal-500 focus:ring-2 focus:ring-teal-100 disabled:bg-slate-100"
                value={apiBaseUrl}
                disabled={provider === "github"}
                onChange={(event) => setApiBaseUrl(event.target.value)}
              />
            </label>

            {/* 访问令牌：type=password 让输入以点显示 */}
            <label className="text-sm font-medium text-slate-700">
              Access Token
              <input
                type="password"
                className="mt-2 w-full rounded-md border border-line bg-white px-3 py-2 outline-none transition focus:border-teal-500 focus:ring-2 focus:ring-teal-100"
                value={token}
                onChange={(event) => setToken(event.target.value)}
                placeholder="public repo 可留空，私有仓库需要 token"
              />
            </label>

            {/* 远程来源额外的下载父目录输入 */}
            {source === "remote" && (
              <label className="text-sm font-medium text-slate-700 lg:col-span-2">
                下载父目录
                <input
                  className="mt-2 w-full rounded-md border border-line bg-white px-3 py-2 outline-none transition focus:border-teal-500 focus:ring-2 focus:ring-teal-100"
                  value={cloneParentDir}
                  onChange={(event) => setCloneParentDir(event.target.value)}
                  placeholder="例如 D:\\code\\repos，留空则使用默认缓存目录"
                />
              </label>
            )}
          </div>

          <div className="mt-5 flex flex-wrap gap-3">
            {/* 连接按钮：loading / 不支持真实连接 / 必填为空 时禁用 */}
            <PrimaryButton
              onClick={connect}
              disabled={loading || !supportsRealConnect || (source === "remote" ? !owner.trim() || !repo.trim() : !localPath.trim())}
            >
              {/* 根据 loading 切换按钮文字 */}
              {loading ? "连接并同步中..." : "连接并自动同步"}
            </PrimaryButton>
          </div>

          {/* 不支持真实连接的平台给个黄色提醒 */}
          {!supportsRealConnect && (
            <p className="mt-3 text-sm text-amber-700">当前平台已在界面预留，但真实 API 同步尚未实现。请选择 GitHub 或 GitHub-compatible API。</p>
          )}

          {/* 操作结果（JSON）展示 */}
          {result && <pre className="mt-4 overflow-auto rounded-md border border-slate-800 bg-slate-950 p-4 text-xs text-slate-100">{result}</pre>}
        </Card>

        {/* 右栏：已连接仓库列表 */}
        <Card>
          <div className="flex items-center justify-between gap-3">
            <h2 className="font-semibold text-ink">已连接仓库</h2>
            <span className="rounded-full bg-panel px-2.5 py-1 text-xs text-slate-500">{repos.length} 个</span>
          </div>
          <div className="mt-4 space-y-3">
            {/* 没仓库给个引导文案 */}
            {repos.length === 0 && <p className="text-sm text-slate-600">还没有仓库。请连接一个真实 Git 仓库。</p>}
            {repos.map((item) => (
              <div
                key={item.id}
                // 选中的仓库高亮边框/背景
                className={`rounded-lg border p-4 transition ${selectedRepoId === item.id ? "border-teal-300 bg-teal-50/70" : "border-line bg-white"}`}
              >
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <div className="font-medium text-ink">{item.full_name}</div>
                    <div className="mt-1 text-xs text-slate-500">
                      {/* 展示平台（没有就默认 github）和默认分支 */}
                      {item.provider || "github"} · {item.default_branch || "main"}
                    </div>
                    {item.local_path && <div className="mt-1 break-all text-xs text-slate-500">{item.checkout_mode === "local" ? "本地仓库" : "代码路径"}：{item.local_path}</div>}
                  </div>
                  <span className="rounded-full bg-panel px-2.5 py-1 text-xs text-slate-500">{selectedRepoId === item.id ? "当前查看" : "已连接"}</span>
                </div>
                {/* 仓库描述，最多两行（line-clamp-2） */}
                <p className="mt-3 line-clamp-2 text-sm leading-6 text-slate-600">{item.description || "暂无仓库描述"}</p>
                <div className="mt-4 flex flex-wrap gap-2">
                  {/* 设为当前查看 */}
                  <button
                    onClick={() => selectRepo(item.id)}
                    className="rounded-md border border-line bg-white px-3 py-1.5 text-xs font-medium text-slate-700 transition hover:border-teal-200 hover:bg-teal-50"
                  >
                    设为当前查看
                  </button>
                  {/* 立即刷新 */}
                  <button
                    onClick={() => refreshRepo(item.id)}
                    disabled={loading}
                    className="rounded-md bg-accent px-3 py-1.5 text-xs font-medium text-white transition hover:bg-teal-700 disabled:opacity-60"
                  >
                    立即刷新
                  </button>
                  {/* 删除（带垃圾桶图标） */}
                  <button
                    onClick={() => deleteRepo(item)}
                    disabled={loading}
                    className="inline-flex items-center gap-1 rounded-md border border-red-200 bg-white px-3 py-1.5 text-xs font-medium text-red-700 transition hover:bg-red-50 disabled:opacity-60"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                    删除
                  </button>
                </div>
              </div>
            ))}
          </div>
        </Card>
      </div>

      {/* 底部：生产同步策略说明三张卡片 */}
      <Card className="mt-4">
        <h2 className="font-semibold text-ink">生产同步策略</h2>
        <div className="mt-3 grid gap-3 text-sm leading-6 text-slate-600 md:grid-cols-3">
          <div className="rounded-md bg-panel p-3">
            <div className="font-medium text-ink">连接后自动同步</div>
            <p className="mt-1">仓库校验成功后，后端会立即调度一次后台同步。</p>
          </div>
          <div className="rounded-md bg-panel p-3">
            <div className="font-medium text-ink">Webhook 实时触发</div>
            {/* 用 API_BASE_URL 拼出 webhook 地址展示给用户去配置 */}
            <p className="mt-1 break-all">GitHub Webhook URL：{API_BASE_URL}/api/webhooks/github</p>
          </div>
          <div className="rounded-md bg-panel p-3">
            <div className="font-medium text-ink">定时同步兜底</div>
            <p className="mt-1">后端启动后会定时刷新已连接仓库，默认 5 分钟一次。</p>
          </div>
        </div>
      </Card>
    </>
  );
}
