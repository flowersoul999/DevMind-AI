// ============================================================================
// lib/types.ts —— 全局共享的「仓库（Repository）」数据类型定义
// ----------------------------------------------------------------------------
// 这个文件只定义了仓库相关的一个类型。TypeScript 的 type 就像「填表模板」：
//   提前约定好一个对象有哪些字段、每个字段是什么类型，页面里用到仓库数据时就能
//   自动获得代码提示，写错字段名/字段类型时编辑器会立刻报错。
// 注意：项目里知识库相关的类型都放在 lib/rag.ts，本文件只管「代码仓库」这一类。
// ============================================================================

// Repository（代码仓库）：描述一个接入 DevFlow 的 Git 仓库有哪些信息。
// 每个字段后面的 ? 表示「这个字段可能不存在」（可选），因为不同来源/不同步状态下字段可能为空。
export type Repository = {
  id: string; // 仓库在系统里的唯一 ID
  owner: string; // 仓库所有者（组织或个人名），例如 github 上的用户名
  name: string; // 仓库名
  full_name: string; // 完整名，通常是 "owner/name"，方便展示和定位
  provider?: string; // 托管平台，例如 github / gitlab（可选）
  api_base_url?: string; // 该平台 API 地址（可选，用于自托管 gitlab 等）
  clone_url?: string | null; // 克隆地址（git clone 用的 URL），可能为 null
  local_path?: string | null; // 在服务器本地磁盘的存放路径，可能为 null
  checkout_mode?: string; // 检出模式，例如 branch / tag / commit
  description?: string; // 仓库描述
  default_branch?: string; // 默认分支，如 main / master
  last_sync_at?: string | null; // 最近一次同步时间（ISO 字符串），从未同步过就是 null
};
