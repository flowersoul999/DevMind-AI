// ============================================================================
// components/rag/retrieval-settings.tsx —— 知识库「检索与索引参数」设置表单
// ----------------------------------------------------------------------------
// 功能：一个可复用的设置面板，让用户在创建/编辑知识库时调整：
//   分段大小/重叠、Embedding 模型选择、检索方式（向量/全文/混合）、
//   是否开 Rerank 及选模型、Top K、Score 阈值等。全部基于 KnowledgeConfig 配置。
// 关键设计：它是「受控组件」——自己不保存数据，所有改动通过 onChange 回调交给父组件。
//   父组件把当前 value 和可选模型清单 catalog 传进来，改了就回传新 value。
// 为什么做成受控：设置可能分散在多个页面，统一由父组件持有配置，提交时一次性发给后端。
// 这是客户端组件（"use client"），因为有很多输入交互。
// ============================================================================

"use client";

// 引入图标：对勾、文件搜索、分层、放大镜。
import { Check, FileSearch, Layers3, Search } from "lucide-react";

// 引入相关类型定义（见 lib/rag.ts）。
import type { KnowledgeConfig, ModelCatalog, RetrievalMethod } from "@/lib/rag";

// Props：父组件需要传进来的东西。
type Props = {
  value: KnowledgeConfig; // 当前配置（受控值）
  catalog: ModelCatalog | null; // 可选模型清单（可能还没加载到，所以是 null）
  onChange: (value: KnowledgeConfig) => void; // 配置变化时的回调
  showChunking?: boolean; // 是否显示「分段设置」块（默认 true）
};

// 三种检索方式的展示信息（值 / 中文名 / 说明 / 对应图标）。
const methods: { value: RetrievalMethod; label: string; note: string; icon: typeof Search }[] = [
  { value: "vector", label: "向量检索", note: "擅长理解语义相近的表达", icon: Layers3 },
  { value: "fulltext", label: "全文检索", note: "适合专有名词与精确关键词", icon: FileSearch },
  { value: "hybrid", label: "混合检索", note: "融合语义与关键词结果", icon: Search }
];

// 组件主体。
export function RetrievalSettings({ value, catalog, onChange, showChunking = true }: Props) {
  // update：一个小助手，用来「改配置里的某一个字段」。它复制原 value，只替换 key 字段，再整体回传。
  // 为什么这样写：保证其它字段不变，只更新一处，避免手写 {...value, x: y} 散落多处。
  // 泛型 <K extends keyof KnowledgeConfig> 让 key 的类型和 next 的类型自动对应，写错字段 TS 会拦。
  const update = <K extends keyof KnowledgeConfig,>(key: K, next: KnowledgeConfig[K]) => onChange({ ...value, [key]: next });

  return (
    <div className="space-y-7"> {/* 各区块之间统一留白 */}
      {/* 分段设置块：showChunking 为 true 才显示（用三目运算符控制渲染与否） */}
      {showChunking ? (
        <section>
          <h3 className="text-sm font-semibold text-ink">分段设置</h3>
          <p className="mt-1 text-xs text-slate-500">按自然段落切分，过长段落会继续按长度拆开。</p>
          <div className="mt-3 grid gap-3 sm:grid-cols-2">
            {/* 最大分段长度输入框 */}
            <label className="text-xs font-medium text-slate-600">
              最大分段长度
              <input
                type="number"
                min={200}
                max={4000}
                value={value.chunk_size} // 受控：值来自 value
                // 输入变化：把字符串转成数字（Number()）后调用 update 改 chunk_size
                onChange={(event) => update("chunk_size", Number(event.target.value))}
                className="mt-1.5 h-10 w-full rounded-md border border-line bg-white px-3 text-sm text-ink outline-none focus:border-teal-600"
              />
            </label>
            {/* 分段重叠长度输入框 */}
            <label className="text-xs font-medium text-slate-600">
              分段重叠长度
              <input
                type="number"
                min={0}
                max={1000}
                value={value.chunk_overlap}
                onChange={(event) => update("chunk_overlap", Number(event.target.value))}
                className="mt-1.5 h-10 w-full rounded-md border border-line bg-white px-3 text-sm text-ink outline-none focus:border-teal-600"
              />
            </label>
          </div>
        </section>
      ) : null}

      {/* 索引方式（Embedding 模型选择）块 */}
      <section>
        <h3 className="text-sm font-semibold text-ink">索引方式</h3>
        <p className="mt-1 text-xs text-slate-500">DeepSeek 官方 API 暂无 Embedding，默认使用 Qwen。</p>
        <label className="mt-3 block text-xs font-medium text-slate-600">
          Embedding 模型
          {/* 下拉框：value 用 "provider::model" 这种拼接字符串，便于唯一标识一个模型 */}
          <select
            value={`${value.embedding_provider}::${value.embedding_model}`}
            onChange={(event) => {
              // 把选中的值按 "::" 拆回 provider 和 model
              const [provider, model] = event.target.value.split("::");
              // 从清单里找到对应模型，取它的向量维度（可能取不到就用原来的）
              const selected = catalog?.embedding_models.find((item) => item.provider === provider && item.model === model);
              onChange({ ...value, embedding_provider: provider, embedding_model: model, embedding_dimensions: selected?.dimensions ?? value.embedding_dimensions });
            }}
            className="mt-1.5 h-10 w-full rounded-md border border-line bg-white px-3 text-sm text-ink outline-none focus:border-teal-600"
          >
            {/* 用 map 渲染可选模型为 <option>；catalog 可能为 null，用 ?? [] 兜底成空数组 */}
            {(catalog?.embedding_models ?? []).map((model) => (
              <option key={`${model.provider}-${model.model}`} value={`${model.provider}::${model.model}`}>
                {model.label}{model.available ? "" : " · 未配置，自动降级"}
              </option>
            ))}
            {/* 还没拿到 catalog 时，至少显示当前已选的那个，避免下拉框空白 */}
            {!catalog ? <option value={`${value.embedding_provider}::${value.embedding_model}`}>{value.embedding_model}</option> : null}
          </select>
        </label>
      </section>

      {/* 检索设置块：三种检索方式做成可点选的卡片 */}
      <section>
        <h3 className="text-sm font-semibold text-ink">检索设置</h3>
        <div className="mt-3 grid gap-2 sm:grid-cols-3">
          {/* 用 map 渲染 methods 成三个按钮卡片 */}
          {methods.map((method) => {
            const Icon = method.icon; // 把图标组件取出来（组件名大写才能当标签用）
            const selected = value.retrieval_method === method.value; // 是否当前选中
            return (
              <button
                key={method.value}
                type="button"
                // 点击：调用 update 把 retrieval_method 改成这一项
                onClick={() => update("retrieval_method", method.value)}
                className={`relative min-h-28 rounded-md border p-3 text-left transition ${selected ? "border-teal-600 bg-teal-50/60" : "border-line bg-white hover:border-slate-400"}`}
              >
                {/* 选中时右上角打勾 */}
                {selected ? <Check className="absolute right-2 top-2 h-4 w-4 text-teal-700" /> : null}
                {/* 该项图标 */}
                <Icon className={`h-5 w-5 ${selected ? "text-teal-700" : "text-slate-500"}`} />
                {/* 中文名 + 说明 */}
                <span className="mt-3 block text-sm font-medium text-ink">{method.label}</span>
                <span className="mt-1 block text-xs leading-5 text-slate-500">{method.note}</span>
              </button>
            );
          })}
        </div>
      </section>

      {/* Rerank 模型块：一个开关 + 选中才出现的下拉 */}
      <section className="border-t border-line pt-5">
        <div className="flex items-center justify-between gap-4">
          <div>
            <h3 className="text-sm font-semibold text-ink">Rerank 模型</h3>
            <p className="mt-1 text-xs text-slate-500">对召回候选重新排序，API 不可用时自动使用本地重排。</p>
          </div>
          {/* 开关按钮（role=switch 表示开关语义）。点击切换 rerank_enabled 的布尔值 */}
          <button
            type="button"
            role="switch"
            aria-checked={value.rerank_enabled}
            onClick={() => update("rerank_enabled", !value.rerank_enabled)} // 取反当前值
            className={`relative h-6 w-11 shrink-0 rounded-full transition ${value.rerank_enabled ? "bg-teal-700" : "bg-slate-300"}`}
          >
            {/* 小圆点：开时滑到右边（left-6），关时靠左（left-1） */}
            <span className={`absolute top-1 h-4 w-4 rounded-full bg-white transition ${value.rerank_enabled ? "left-6" : "left-1"}`} />
          </button>
        </div>
        {/* 只有在开启 Rerank 时才显示模型下拉（与 Embedding 下拉逻辑相同） */}
        {value.rerank_enabled ? (
          <select
            value={`${value.rerank_provider}::${value.rerank_model}`}
            onChange={(event) => {
              const [provider, model] = event.target.value.split("::");
              onChange({ ...value, rerank_provider: provider, rerank_model: model });
            }}
            className="mt-3 h-10 w-full rounded-md border border-line bg-white px-3 text-sm text-ink outline-none focus:border-teal-600"
          >
            {(catalog?.rerank_models ?? []).map((model) => (
              <option key={`${model.provider}-${model.model}`} value={`${model.provider}::${model.model}`}>
                {model.label}{model.available ? "" : " · 未配置，自动降级"}
              </option>
            ))}
            {!catalog ? <option value={`${value.rerank_provider}::${value.rerank_model}`}>{value.rerank_model}</option> : null}
          </select>
        ) : null}
      </section>

      {/* 底部：Top K 与 Score 阈值并排 */}
      <section className="grid gap-4 border-t border-line pt-5 sm:grid-cols-2">
        {/* Top K：滑块 + 数字框双向绑定同一个值 */}
        <label className="text-xs font-medium text-slate-600">
          Top K
          <div className="mt-2 flex items-center gap-3">
            {/* range 滑块：min=1 max=10；拖动即改 top_k */}
            <input type="range" min={1} max={10} value={value.top_k} onChange={(event) => update("top_k", Number(event.target.value))} className="min-w-0 flex-1 accent-teal-700" />
            {/* 数字输入框：min=1 max=20，和滑块共享 value，便于精确输入 */}
            <input type="number" min={1} max={20} value={value.top_k} onChange={(event) => update("top_k", Number(event.target.value))} className="h-9 w-16 rounded-md border border-line text-center text-sm outline-none focus:border-teal-600" />
          </div>
        </label>
        <div>
          <div className="flex items-center justify-between">
            <span className="text-xs font-medium text-slate-600">Score 阈值</span>
            {/* 复选框：控制是否启用分数门槛 */}
            <input type="checkbox" checked={value.score_threshold_enabled} onChange={(event) => update("score_threshold_enabled", event.target.checked)} className="h-4 w-4 accent-teal-700" />
          </div>
          <div className="mt-2 flex items-center gap-3">
            {/* 阈值滑块：0~1，步长 0.01；没启用门槛时 disabled 且变灰 */}
            <input type="range" min={0} max={1} step={0.01} disabled={!value.score_threshold_enabled} value={value.score_threshold} onChange={(event) => update("score_threshold", Number(event.target.value))} className="min-w-0 flex-1 accent-teal-700 disabled:opacity-40" />
            {/* 右侧显示当前阈值（保留两位小数） */}
            <span className="w-12 text-right text-sm text-slate-600">{value.score_threshold.toFixed(2)}</span>
          </div>
        </div>
      </section>
    </div>
  );
}
