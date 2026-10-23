// ============================================================================
// lib/rag.ts —— 知识库（RAG）相关类型 + 格式化工具
// ----------------------------------------------------------------------------
// RAG = Retrieval-Augmented Generation（检索增强生成）：先从一个「知识库」里
//   搜出相关文档片段，再喂给大模型，让回答有据可依。这个文件集中定义：
//   1) 知识库配置/模型/文档/检索结果等数据结构（方便各页面共享、获得类型提示）
//   2) 几个纯展示用的小工具函数（状态中文名、数字/字节/日期格式化）
// 为什么单独放这里：知识库功能字段很多，放一起便于复用，也避免页面文件过长。
// ============================================================================

// 检索方式：向量（按语义相似度）、全文（关键词匹配）、混合（两者结合）。
// 用联合类型限定取值只能是这三个之一，写错会立刻被 TS 拦住。
export type RetrievalMethod = "vector" | "fulltext" | "hybrid";

// KnowledgeConfig：一个知识库的「检索与向量化配置」。
// 这些参数决定「怎么把文档切块、怎么算相似度、搜多少条、要不要重排」等。
export type KnowledgeConfig = {
  embedding_provider: string; // 向量化（embedding）服务提供商，如 openai / deepseek
  embedding_model: string; // 具体用哪个向量模型
  embedding_dimensions: number; // 向量维度（每条文本转成多少维的数字数组）
  retrieval_method: RetrievalMethod; // 检索方式：vector / fulltext / hybrid
  rerank_enabled: boolean; // 是否开启「重排」（用更强的模型对初筛结果再排序，提升精度）
  rerank_provider: string; // 重排模型提供商
  rerank_model: string; // 重排模型名
  top_k: number; // 检索时最多返回几条（取最相关的 K 条）
  score_threshold_enabled: boolean; // 是否启用「分数门槛」（低于阈值的不要）
  score_threshold: number; // 相似度门槛值（0~1 之间）
  chunk_size: number; // 文档切块大小（按字符数），太大漏细节、太小丢上下文
  chunk_overlap: number; // 相邻块的重叠字符数，避免一句话被切在两半而检索不到
};

// RuntimeModel：描述某个「模型在运行时」的可用状态（比如配置好了但服务商不可用）。
export type RuntimeModel = {
  provider: string; // 模型提供商
  model: string; // 模型名
  available: boolean; // 当前是否可用（key 是否有效、服务是否在线）
  fallback?: string | null; // 不可用时回退到哪个模型
  enabled?: boolean; // 是否在界面上被启用
  dimensions?: number; // 该模型的向量维度
};

// KnowledgeBase：一个知识库的整体信息（含配置和运行时状态）。
export type KnowledgeBase = {
  id: string;
  name: string; // 知识库显示名
  full_name: string; // 全名（可能带工作区前缀）
  description: string; // 简介
  provider: string; // 来源方（如 github 仓库、上传文件等）
  document_count: number; // 文档数量
  chunk_count: number; // 切出的文本块数量
  character_count: number; // 总字符数
  config: KnowledgeConfig; // 检索/向量化配置（上面那个类型）
  runtime: { embedding: RuntimeModel; rerank: RuntimeModel }; // 两个模型的运行时状态
  created_at?: string;
  updated_at?: string;
};

// KnowledgeDocument：知识库里的一份「文档」及其处理进度。
export type KnowledgeDocument = {
  id: string;
  repo_id: string; // 来源仓库 ID
  name: string; // 文件名
  source_type: string; // 来源类型（如 file / repo）
  status: "queued" | "parsing" | "splitting" | "embedding" | "completed" | "failed"; // 处理状态机
  content_type?: string; // MIME 类型，如 text/markdown
  file_size: number; // 文件字节大小
  character_count: number; // 字符数
  chunk_count: number; // 切块数
  error_message?: string; // 失败时的人类可读错误
  created_at?: string;
  completed_at?: string;
};

// KnowledgeChunk：文档被切出的「一个文本块」，是检索的最小单位。
export type KnowledgeChunk = {
  id: string;
  position: number; // 在原文档中的顺序位置
  title: string; // 块标题（可能来自章节名）
  content: string; // 块正文
  character_count: number; // 块字符数
  enabled: boolean; // 是否被启用（禁用后不参与检索）
};

// CatalogModel：模型目录里的一项（给前端下拉框用的可选项）。
export type CatalogModel = {
  provider: string;
  model: string;
  label: string; // 展示名（比 model 更友好）
  available: boolean; // 是否可申请可用
  recommended: boolean; // 是否推荐
  fallback?: string | null;
  dimensions?: number;
};

// ModelCatalog：后端返回的「可选模型清单」。
export type ModelCatalog = {
  embedding_models: CatalogModel[]; // 可选向量模型列表
  rerank_models: CatalogModel[]; // 可选重排模型列表
  deepseek: { embedding_available: boolean; rerank_available: boolean; note: string }; // deepseek 特例说明
};

// RetrievalResult：一次检索返回的「单条命中结果」。
export type RetrievalResult = {
  citation: number; // 引用编号（回答里用 [1][2] 指向它）
  document_id: string; // 所属文档 ID
  source_id: string; // 来源 ID
  source_type: string; // 来源类型
  title: string; // 标题
  snippet: string; // 摘要片段（高亮上下文）
  score: number; // 相关度得分（越高越相关）
  metadata: Record<string, unknown>; // 任意附加元信息（key->值 的字典）
  retrieval: Record<string, unknown>; // 检索过程留下的元信息
};

// RetrievalResponse：一次「检索请求」的完整返回。
export type RetrievalResponse = {
  id: string;
  query: string; // 用户的问题
  results: RetrievalResult[]; // 命中的结果列表
  result_count: number; // 命中数量
  took_ms: number; // 耗时（毫秒），用来展示性能
  retrieval_config: KnowledgeConfig & {
    // 在 KnowledgeConfig 基础上再附加一些运行期信息（& 表示「同时具备」这些字段）
    runtime?: unknown;
    fusion_method?: string; // 混合检索的融合策略
    answer_gate?: {
      // 「答案闸门」：模型判断是否需要回答 / 澄清 / 证据不足 / 有冲突
      decision?: "answer" | "ask_clarification" | "insufficient_evidence" | "conflict";
      reason?: string;
      evidence_count?: number;
      top_score?: number;
    };
    pipeline?: {
      // 检索流水线的各阶段（用于前端可视化「先检索→再重排→再过滤」）
      retrieval_method?: string;
      fusion_method?: string;
      stages?: Array<Record<string, unknown> & { name?: string }>;
    };
  };
  created_at?: string;
};

// RetrievalHistory：检索历史里的一条记录（用于历史列表展示）。
export type RetrievalHistory = {
  id: string;
  query: string;
  result_count: number;
  took_ms: number;
  created_at?: string;
};

// statusLabel：把文档状态的英文枚举，映射成中文展示文案。
// 为什么用 Record<...>：保证「每个状态值都必须有对应中文」，少一个 TS 都会报错。
export const statusLabel: Record<KnowledgeDocument["status"], string> = {
  queued: "排队中",
  parsing: "解析中",
  splitting: "分段中",
  embedding: "向量化",
  completed: "可用",
  failed: "失败"
};

// formatNumber：把大数字按中文习惯加千分位，例如 12345 -> 12,345。
export function formatNumber(value: number) {
  return new Intl.NumberFormat("zh-CN").format(value);
}

// formatBytes：把「字节数」换算成 B / KB / MB 的人类可读字符串。
export function formatBytes(value: number) {
  if (value < 1024) return `${value} B`; // 不到 1KB，直接显示字节
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`; // 小于 1MB，显示 KB
  return `${(value / 1024 / 1024).toFixed(1)} MB`; // 更大则显示 MB
}

// formatDate：把 ISO 时间字符串格式化成「月/日 时:分」的中文短格式；空值显示 "-"。
export function formatDate(value?: string) {
  if (!value) return "-"; // 没传时间就给占位符，避免页面显示 undefined
  return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(
    new Date(value) // 把字符串转成 Date 对象再格式化
  );
}
