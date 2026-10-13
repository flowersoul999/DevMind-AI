<div align="center">

# DevFlow AI

**面向 GitHub PR / Issue 研发协作的全栈 AI Agent 平台**

把散落在 Issue、PR diff、CI 日志、项目文档与本地代码里的线索，收拢成一份可执行、可追溯、可审计的工程决策备忘录。

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Next.js](https://img.shields.io/badge/Next.js-15-000000?style=flat-square&logo=nextdotjs&logoColor=white)](https://nextjs.org/)
[![React](https://img.shields.io/badge/React-19-61DAFB?style=flat-square&logo=react&logoColor=black)](https://react.dev/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16%20%2B%20pgvector-4169E1?style=flat-square&logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![Milvus](https://img.shields.io/badge/Milvus-2.5-00A1EA?style=flat-square)](https://milvus.io/)
[![LLM](https://img.shields.io/badge/LLM-OpenAI%20Compatible-6E56CF?style=flat-square)](https://platform.openai.com/docs/api-reference)
[![Tests](https://img.shields.io/badge/pytest-27%20suites-0A9EDC?style=flat-square&logo=pytest&logoColor=white)](#十一测试与评测)
[![PRs](https://img.shields.io/badge/PRs-welcome-brightgreen?style=flat-square)](#十五贡献指南)

</div>

---

## 目录

- [一、项目简介](#一项目简介)
- [二、研发背景与动机](#二研发背景与动机)
- [三、解决的实际问题](#三解决的实际问题)
- [四、适用场景与用户群体](#四适用场景与用户群体)
- [五、整体架构](#五整体架构)
- [六、关键模块详解](#六关键模块详解)
  - [6.1 数据接入与同步层](#61-数据接入与同步层)
  - [6.2 ChatAgent：原生工具调用的推理中枢](#62-chatagent原生工具调用的推理中枢)
  - [6.3 工具矩阵](#63-工具矩阵)
  - [6.4 多 Agent 工程审查工作流](#64-多-agent-工程审查工作流)
  - [6.5 Skill Runtime：按需装载的能力包](#65-skill-runtime按需装载的能力包)
  - [6.6 RAG 知识问答管线](#66-rag-知识问答管线)
  - [6.7 记忆体系：从证据到长期记忆](#67-记忆体系从证据到长期记忆)
  - [6.8 代码图谱与 PR 工作树快照](#68-代码图谱与-pr-工作树快照)
  - [6.9 安全边界：草稿、审计与权限](#69-安全边界草稿审计与权限)
  - [6.10 上下文压缩与 Token 预算](#610-上下文压缩与-token-预算)
  - [6.11 评测体系](#611-评测体系)
- [七、一次对话的完整链路](#七一次对话的完整链路)
- [八、安装部署](#八安装部署)
- [九、使用方法](#九使用方法)
- [十、依赖与运行环境](#十依赖与运行环境)
- [十一、测试与评测](#十一测试与评测)
- [十二、项目结构](#十二项目结构)
- [十三、常见问题 FAQ](#十三常见问题-faq)
- [十四、路线图](#十四路线图)
- [十五、贡献指南](#十五贡献指南)

---

## 一、项目简介

**DevFlow AI**（GitHub 仓库名 `DevMind-AI`）是一个面向 **GitHub PR / Issue 研发协作** 的全栈 AI Agent 项目。它连接仓库的真实数据，分析 Issue 描述、PR diff、Review 评论、CI 日志、项目知识库和本地工作区代码，输出**结构化建议**与**安全的操作草稿**。

它不是一个"聊天框套壳"，而是一条完整链路：

> **接入数据 → 检索证据 → 多 Agent 推理 → 给出结构化结论 → 落成可审计草稿 → 人工确认**

### 一句话概括各层职责

| 层次 | 它负责什么 | 关键实现 |
| :--- | :--- | :--- |
| **数据** | 把 GitHub 上的 Issue / PR / Review / Workflow Run / Job 日志同步进本地库 | `services/repos_sync.py`、`services/github/*` |
| **检索** | 语义召回历史讨论与文档 + 词法检索当前代码 + 结构化查询实时状态 | `services/rag/*`、`services/code_search.py` |
| **推理** | 原生工具调用驱动的对话 Agent，串联 8 类专用能力 | `services/agents/chat_agent.py` |
| **编排** | Planner → 专用 Agent → Observer → Synthesis 的闭环工作流 | `services/agents/workflow_orchestrator.py` |
| **治理** | 高风险写操作只出草稿、人工确认、全程审计 | `services/agents/safety_agent.py`、`services/permissions.py` |
| **度量** | 基础 Eval + RAGAS 语义评测 + 回归对比 | `services/evals/*` |
| **交互** | 17 个页面的 Next.js 控制台，支持 SSE 流式输出 | `frontend/app/**` |

### 核心能力速览

- 🔗 **仓库数据同步**：Issue、Pull Request、PR 文件、Review 评论、Workflow Run、Job 与日志，支持定时自动同步。
- 🏷️ **Issue 分析**：分类、优先级、复杂度、推荐负责人、重复候选、行动项。
- 🔍 **PR 审查**：摘要、关键变更、P1/P2/P3 风险分级、检查清单、测试建议、重点关注文件、可合并性判断。
- 🚑 **CI 排障**：失败类型、可能原因、排查步骤、相关上下文定位。
- 💬 **Agent 对话**：`ChatAgent` 使用原生工具调用，串联工作区、记忆、RAG、Issue、PR、CI、周报与安全工具。
- 🧭 **多 Agent 工作流审查**：`WorkflowOrchestrator` 构建 Planner → 专用 Agent → Observer → Synthesis 闭环。
- 🧩 **Skill Runtime**：按显式指定或触发条件选择 `SKILL.md`，只向当前任务加载完整指令与受限资源。
- 📁 **工作区代码工具**：安全地列目录、读文件、搜索本地代码。
- 📚 **聚焦式 RAG**：只把"需要跨来源语义召回、会反复使用的非结构化知识"向量化。
- 📝 **工程周报**：按仓库生成周报，支持多仓库合并汇总。
- 🧪 **评测体系**：基础 Eval + RAGAS 五项语义指标 + 工具调用指标，支持失败诊断与回归对比。

---

## 二、研发背景与动机

### 故事从三个"断点"开始

想象一个典型的中型研发团队：早上站会，一位工程师问"这个 PR 能合了吗？"——于是他要做这些事：

1. 打开 PR 页面，读 diff（可能 40 个文件）；
2. 翻 20 条 Review 评论，找哪条还没解决；
3. 切到 Actions 页面，看那个红色 ✗ 的 job 到底为什么失败；
4. 想起"上次好像有人踩过这个坑"，去搜索历史 Issue；
5. 问一下"这块是谁负责的"，翻团队文档；
6. 最后凭记忆拼出一个判断。

**这六步散落在四个系统里，全靠人脑做上下文拼装。** 而 LLM 恰好最擅长这种"多源信息摘要 + 结构化输出"的活。

DevFlow AI 的动机就是：**把这个拼装过程，变成一条可复现的流水线。**

### 为什么不是"又一个 PR 总结器"？

大多数 AI 代码审查工具停留在「把 diff 丢给模型，让它说几句」。这有三个隐患：

| 隐患 | 后果 | DevFlow AI 的应对 |
| :--- | :--- | :--- |
| **没有真实上下文** | 模型只能看到 diff，看不到历史讨论、CI 失败原因、项目约定 | 混合检索：向量 + 词法 + 结构化查询，三路并进 |
| **容易一本正经胡说** | 无证据的"看起来没问题"最危险 | 证据优先 + Answer Gate + 忠实度（Faithfulness）评测门槛 |
| **不敢让它动手** | 一旦自动写 GitHub 就是事故 | 写操作只产出 `ActionDraft`，必须人工确认 + 审计留痕 |

### 关键设计取舍

| 决策 | 我们怎么做的 | 替代方案 | 代价 / 权衡 |
| :--- | :--- | :--- | :--- |
| **不是所有数据都进向量库** | 代码走 `workspace.*` 工具现查；实时状态走结构化查询 | 全量向量化 | 需要 `source_policy` 做路由判断，架构更复杂 |
| **对话用原生工具调用而非固定流水线** | 模型自主决定下一步调哪个工具、要不要改写 query 补查 | 硬编码 if-else 流程 | Token 消耗更高，需要上下文压缩兜底 |
| **多 Agent 而非单 Agent 一把梭** | Planner 拆任务 → 并行执行 → Observer 挑刺 → Synthesis 收口 | 单个大 Prompt | 延迟与成本增加，但跨领域决策质量显著提升 |
| **写操作一律草稿化** | 生成 → 人工确认 → 才落库/外发 | 直接调用 GitHub 写 API | 牺牲自动化爽感，换来可审计与可控 |
| **课程项目式的 `create_all` 建表** | 启动时自动建表 + 补字段 | Alembic 迁移 | 快速迭代友好，生产需替换为正规迁移 |

> ⚠️ **诚实声明**：这是一个**教学 / 作品集级别的完整实现**。它把生产系统的骨架搭得很全（多 Agent、评测、审计、降级），但在**多用户认证、真正的迁移体系、应用层容器化**上做了简化。详见 [第十三章 FAQ](#十三常见问题-faq) 与 [第十四章 路线图](#十四路线图)。

---

## 三、解决的实际问题

### 痛点 → 方案对照

| # | 真实痛点 | 传统做法 | DevFlow AI 的做法 | 产出物 |
| :-- | :--- | :--- | :--- | :--- |
| 1 | **Issue 堆积无人分诊** | 靠人扫标题，凭感觉打标签 | 分类 + 优先级 + 复杂度 + 推荐负责人 + 重复候选 | 结构化 `IssueAnalysis` |
| 2 | **PR 审查耗时且标准不一** | 资深工程师逐行看，新人不敢说话 | P1/P2/P3 分级发现，每条带证据与是否阻塞 | `PRReviewOutput` + 合并就绪结论 |
| 3 | **CI 挂了不知道为什么** | 翻几千行日志，grep 关键字 | 定位失败 job → 提取关键日志 → 推断失败类型与排查步骤 | `CIAnalysis` |
| 4 | **历史知识"沉在聊天记录里"** | 人肉搜 Issue / 群里问 | RAG 混合检索历史讨论、文档、失败日志 | 带编号引用的答案 |
| 5 | **跨模块决策没人敢拍板** | 拉个会，各自说一段 | Planner 拆解 → 并行取证 → Observer 找冲突 → Synthesis 出备忘录 | `WorkflowSpec` + 决策备忘录 |
| 6 | **AI 建议无法追溯** | 复制一段回答贴到 PR | 记录工具轨迹、引用证据、Agent 运行记录 | `AgentRun` + `EvidenceItem` |
| 7 | **AI 说得好听但质量不稳定** | 靠感觉 | RAGAS 五项语义指标 + 硬规则 + 回归对比 | 可量化的质量门槛 |
| 8 | **不敢让 AI 直接改线上** | 干脆不用 | 高风险动作只出草稿，人工确认后执行并写审计 | `ActionDraft` + `AuditLog` |

### 它真正省下的时间

一条"这个 PR 能合吗"的完整判断，人工大约需要 **15–30 分钟**（跨 4 个系统、20+ 次页面跳转）。DevFlow AI 把这个过程压缩成一次提问 + 一份带证据的结论，**把人的时间从"收集"转移到"决策"**。

---

## 四、适用场景与用户群体

### 目标用户

| 角色 | 用它做什么 | 最关心的能力 |
| :--- | :--- | :--- |
| **一线开发工程师** | 提交 PR 前自查；看不懂别人的改动时快速摸清脉络 | PR 审查、代码搜索、CI 排障 |
| **Tech Lead / 评审者** | 快速判断 PR 是否可合、风险点在哪、缺哪些测试 | 合并就绪度、阻塞项、跨模块影响 |
| **新人 / 转岗同学** | 补业务上下文，问"这块代码为什么这么写" | RAG 问答、知识图谱、周报 |
| **项目经理 / 交付** | 掌握仓库健康度与阻塞情况 | 周报、仓库健康度、多仓库汇总 |
| **平台 / 效能团队** | 度量 AI 辅助的实际效果，评估是否值得推广 | RAGAS 评测、反馈指标、工具轨迹 |
| **AI 工程学习者** | 学习一个完整的 Agent 系统怎么写 | 全部源码与 `docs/` |

### 典型场景

<table>
<tr><td width="33%" valign="top">

**场景 A：早会前的 3 分钟**

> "昨天那个 #482 的 PR 现在什么状态？能合吗？"

Agent 自动拉取 PR diff、未解决评论、最近一次 CI 结果，输出：
- 变更摘要（3 条）
- 阻塞项（P1：迁移脚本缺少回滚）
- 建议补充的测试（2 条）

</td><td width="33%" valign="top">

**场景 B：CI 红了**

> "build 这个 job 为什么挂了？"

Agent 读取失败 job 日志 → 定位到依赖版本冲突 → 给出排查步骤与相关历史 Issue 链接。

</td><td width="33%" valign="top">

**场景 C：新人摸底**

> "我们这个项目的鉴权是怎么做的？"

RAG 检索项目文档 + 历史 Review 讨论，代码部分走 `workspace.search_code`，返回带编号引用的答案。

</td></tr>
</table>

### 它**不**适合什么

明确边界，比吹嘘能力更重要：

- ❌ **不能替代人工 Code Review**：它是"第一遍筛查 + 上下文供给"，P1 判定仍需人确认。
- ❌ **不会自动修改仓库**：所有写操作都是草稿，需人工在 `/action-drafts` 页面确认。
- ❌ **不适合作为公网多租户 SaaS 直接上线**：当前认证是演示级（见 FAQ）。
- ❌ **不适合超大规模单仓**：代码图谱与 PR 快照按需构建，巨型仓库需评估构建耗时。

---

## 五、整体架构

### 5.1 分层架构

```mermaid
flowchart TB
    subgraph L1["🖥 表现层 · Next.js 15 / React 19"]
        direction LR
        P1["仓库管理"]:::fe
        P2["Issues"]:::fe
        P3["Pull Requests"]:::fe
        P4["CI 运行"]:::fe
        P5["对话工作室"]:::fe
        P6["知识库 / RAG"]:::fe
        P7["代码图谱"]:::fe
        P8["评测中心"]:::fe
        P9["动作草稿"]:::fe
    end

    subgraph L2["⚙️ 接入层 · FastAPI · 18 个路由组"]
        direction LR
        A1["CORS / 异常降级"]:::api
        A2["RBAC 依赖注入"]:::api
        A3["SSE 流式响应"]:::api
        A4["Webhook 验签"]:::api
    end

    subgraph L3["🧠 编排层 · Agent Runtime"]
        direction TB
        C1["ChatAgent<br/>原生工具调用循环"]:::agent
        C2["WorkflowOrchestrator<br/>任务图 DAG"]:::agent
        C3["Planner / Observer / Synthesis"]:::agent
        C4["专用分析 Agent<br/>Issue · PR · CI · Report · Safety"]:::agent
        C5["Skill Registry<br/>SKILL.md 装载"]:::agent
    end

    subgraph L4["🔎 能力层 · Tools"]
        direction LR
        T1["workspace.*<br/>本地代码只读"]:::tool
        T2["rag.*<br/>语义混合检索"]:::tool
        T3["devflow_*<br/>MCP 记忆工具"]:::tool
        T4["结构化查询<br/>Issue/PR/CI/团队"]:::tool
    end

    subgraph L5["💾 数据层"]
        direction LR
        D1[("PostgreSQL 16<br/>40+ 业务表")]:::db
        D2[("Milvus 2.5<br/>向量集合")]:::db
        D3["本地 checkout<br/>.data/repos"]:::db
    end

    subgraph EXT["🌐 外部依赖"]
        direction LR
        E1["GitHub REST API"]:::ext
        E2["OpenAI 兼容 LLM"]:::ext
        E3["Qwen Embedding<br/>/ Rerank"]:::ext
        E4["飞书机器人<br/>可选"]:::ext
    end

    L1 --> L2 --> L3 --> L4 --> L5
    L4 --> EXT
    L3 -.-> EXT

    classDef fe fill:#EEF2FF,stroke:#6366F1,stroke-width:1.5px,color:#1E1B4B
    classDef api fill:#ECFDF5,stroke:#10B981,stroke-width:1.5px,color:#064E3B
    classDef agent fill:#FEF3C7,stroke:#F59E0B,stroke-width:1.5px,color:#78350F
    classDef tool fill:#E0F2FE,stroke:#0EA5E9,stroke-width:1.5px,color:#0C4A6E
    classDef db fill:#F3E8FF,stroke:#A855F7,stroke-width:1.5px,color:#4C1D95
    classDef ext fill:#FEE2E2,stroke:#EF4444,stroke-width:1.5px,color:#7F1D1D
```

### 5.2 部署拓扑

```mermaid
flowchart LR
    U["浏览器<br/>localhost:3000"] -->|HTTP| FE["Next.js<br/>开发服务器"]
    FE -->|REST / SSE| BE["FastAPI<br/>uvicorn :8000"]
    BE --> PG[("PostgreSQL<br/>:5432")]
    BE --> MV[("Milvus<br/>:19530")]
    MV --- ETCD["etcd :2379"]
    MV --- MINIO["MinIO :9000 / :9001"]
    BE -->|拉取数据| GH["GitHub API"]
    BE -->|推理| LLM["LLM 服务"]
    BE -->|Embedding / Rerank| QW["Qwen 服务"]
    BE -->|可选告警| FS["飞书 Webhook"]
```

> 📌 **注意**：`docker-compose.yml` 只编排**基础设施**（PostgreSQL / Redis / etcd / MinIO / Milvus），**应用层需本地启动**。项目当前**未内置应用 Dockerfile**——这是有意为之的简化，生产化改造方向见[路线图](#十四路线图)。

### 5.3 技术选型

| 领域 | 选型 | 版本 | 选择理由 |
| :--- | :--- | :--- | :--- |
| Web 框架 | FastAPI | 0.115.6 | 异步原生、Pydantic 集成、自动 OpenAPI 文档 |
| 数据校验 | Pydantic / pydantic-settings | 2.10.4 | 类型安全的请求/响应契约 + `.env` 配置合并 |
| ORM | SQLAlchemy | 2.0.36 | 成熟、支持异步与原生 SQL 混用 |
| 关系库 | PostgreSQL + pgvector | 16 | JSON 字段 + 向量能力兜底 + 强一致 |
| 向量库 | Milvus | 2.5.x | 独立扩缩、支持大规模集合、与业务库解耦 |
| Agent 编排 | LangChain / LangGraph + 原生工具调用 | 1.2.17 / 1.1.10 | 工具调用循环 + 状态机式图编译 |
| 评测 | Ragas | 0.4.3（Collections API） | 语义指标标准化，可做回归对比 |
| 前端 | Next.js / React | 15.1.2 / 19 | App Router、服务端组件、流式渲染 |
| 样式 | Tailwind CSS | 3.4 | 原子化、与控制台风格契合 |
| 数据请求 | TanStack React Query | 5.62 | 缓存、轮询、乐观更新 |
| 容器 | Docker Compose | — | 一键起四件套基础设施 |

---

## 六、关键模块详解

### 6.1 数据接入与同步层

**目标**：让分析永远基于"新鲜"的真实数据，而不是某次手工同步的快照。

```mermaid
flowchart LR
    A["POST /api/repos/connect"] --> B["加密存储 Token<br/>Fernet"]
    B --> C["git clone 到<br/>.data/repos"]
    C --> D["首次全量同步"]
    D --> E{"定时循环<br/>每 300s"}
    E -->|Scheduler| F["sync_repository_by_id"]
    F --> G["Issues / PRs /<br/>PR Files / Reviews"]
    F --> H["Workflow Runs /<br/>Jobs / Logs"]
    G --> I[("PostgreSQL")]
    H --> I
    I --> J["Agent 分析"]
```

| 组件 | 文件 | 职责 |
| :--- | :--- | :--- |
| 仓库连接 | `api/routes/repos.py` | 校验 Token、克隆、首次同步、删除级联清理 |
| 同步编排 | `services/repos_sync.py` | 分页拉取、增量 upsert、状态回写 |
| GitHub 客户端 | `services/github/client.py` | 统一 httpx 调用、限流与错误处理 |
| 定时调度 | `services/scheduler.py` | `asyncio` 常驻循环，启动 5s 后开始，按 `AUTO_SYNC_INTERVAL_SECONDS` 触发 |
| 增量通知 | `api/routes/webhooks.py` | 接收 GitHub Webhook，HMAC 验签后触发同步 |
| Token 安全 | `core/security.py` | `Fernet` 对称加密，密钥来自 `TOKEN_ENCRYPTION_KEY` |

**关键设计**：定时器不是系统 cron，而是**进程内 asyncio 任务**（`app.state.repo_sync_task`），随应用启停优雅创建与取消。简单、零外部依赖，代价是**只能单进程运行**（多副本会重复同步）。

---

### 6.2 ChatAgent：原生工具调用的推理中枢

`ChatAgent`（`services/agents/chat_agent.py`，约 2000 行）是整个系统的"大脑皮层"。它不是固定流程，而是一个**工具调用循环**：

```mermaid
stateDiagram-v2
    [*] --> 预检索
    预检索 --> 上下文组装: ContextAssembler 注入证据
    上下文组装 --> 模型推理
    模型推理 --> 判定: 是否需要工具?
    判定 --> 工具执行: 是
    工具执行 --> 结果裁剪: 超长则截断/压缩
    结果裁剪 --> 模型推理
    判定 --> 最终回答: 否
    最终回答 --> 证据绑定
    证据绑定 --> [*]
```

**它做对了哪几件难事：**

| 机制 | 说明 | 代码位置 |
| :--- | :--- | :--- |
| **强制工具预算** | 某些场景必须至少调用某工具（如必须读代码），否则不允许直接回答 | `required_*` 工具注入 |
| **预取（prefetch）** | 明显需要项目概览时，先自动 `list_files` + `read_file`，减少一轮往返 | `_prefetch` 逻辑 |
| **工具名安全化** | 把 `workspace.read_file` 映射为 `workspace__read_file`，兼容模型对函数名的限制 | `_safe_tool_name` |
| **查询改写** | 允许模型用不同 query 多次调用 `rag.search_similar_documents` 交叉验证 | 系统提示词约束 |
| **重复调用抑制** | 对同一工具 + 同一参数计算 fingerprint，避免死循环 | `_tool_call_fingerprint` |
| **观测兜底回答** | 模型不可用时，用工具观测结果拼出本地回答 | `_local_observation_answer` |
| **证据与引用** | 只有真正用到的工具结果才进入引用列表 | `_citations` |

---

### 6.3 工具矩阵

Agent 能做的事，全部收敛为**一组带权限边界的工具**：

| 工具名 | 类别 | 读写 | 作用 |
| :--- | :--- | :---: | :--- |
| `workspace.list_files` | 工作区 | 只读 | 列出所选仓库 checkout 下的文件 |
| `workspace.read_file` | 工作区 | 只读 | 读取文本文件，支持 `start_line` / `line_count` |
| `workspace.search_code` | 工作区 | 只读 | 在当前 checkout 中搜索代码或文档文本 |
| `rag.search_similar_documents` | 检索 | 只读 | 检索 Issue/PR 讨论、失败 CI 日志、项目文档、上传知识、已批准记忆 |
| `devflow_search_evidence` | 记忆 | 只读 | MCP 工具：搜索持久记忆、会话摘要、仓库证据、Agent 运行记录 |
| `devflow_get_thread_context` | 记忆 | 只读 | MCP 工具：跨压缩边界读取完整原始转录，支持分页 |
| `devflow_read_session_events` | 记忆 | 只读 | MCP 工具：读取已封存的会话摘要与工具事件 |
| `issue_agent.analyze_issue` | 分析 | 只读 | 分析最新同步的 Issue |
| `pr_review_agent.analyze_pr` | 分析 | 只读 | 审查最新同步的 PR |
| `ci_debug_agent.analyze_workflow_run` | 分析 | 只读 | 分析最新失败的 CI 运行 |
| `report_agent.generate_weekly_report` | 报告 | 只读 | 生成仓库周报 |
| `repo_health.generate_summary` | 报告 | 只读 | 总结仓库健康状态 |
| `safety_agent.classify_action_risk` | 治理 | 只读 | 对高风险写操作做风险评估，产出草稿 |
| `workflow.run_engineering_review` | 编排 | 只读 | 触发多 Agent 工程审查工作流 |

> 🔐 **整张表里没有"写 GitHub"的工具。** 这是刻意的安全边界——能力可以强，手不能伸到仓库里。

---

### 6.4 多 Agent 工程审查工作流

当问题跨越多个领域（"这个 PR 能不能合" → 涉及代码风险 + CI 状态 + 历史约定 + 阻塞项），单 Agent 容易顾此失彼。`WorkflowOrchestrator` 用一个**任务图 DAG** 来组织：

```mermaid
flowchart TB
    S["用户问题"] --> P["🧭 PlannerAgent<br/>产出 WorkflowSpec"]
    P --> G["任务图 DAG<br/>tasks + dependencies"]
    G --> T1["PR 审查任务"]
    G --> T2["CI 状态任务"]
    G --> T3["RAG 历史证据任务"]
    G --> T4["仓库健康任务"]
    T1 --> J{"依赖满足?"}
    T2 --> J
    T3 --> J
    T4 --> J
    J -->|并行就绪| R["执行结果聚合"]
    R --> O["👁 ObserverAgent<br/>合并门禁 · 冲突 · 低置信 · 证据缺失"]
    O --> D{"需要重规划?"}
    D -->|是, max_replans| P
    D -->|否| Y["🧩 SynthesisAgent<br/>统一决策备忘录"]
    Y --> DB[("AgentWorkflowRun<br/>AgentTaskRun")]
```

**工程化细节（这些才是真实系统的样子）：**

- **依赖阻塞传播**：某任务失败时，依赖它的任务标记为 `dependency_blocked`，不会拿着空结果硬算。
- **单任务超时**：`workflow_task_timeout_seconds`（默认 90s），避免一个慢任务拖死整条链路。
- **重规划上限**：`max_replans` 防止 Planner 与 Observer 互相"吵架"无限循环。
- **同构图检测**：`_same_task_graph` 比较重规划后的图签名，若与上一轮相同则停止，避免空转。
- **全链路持久化**：每个任务的输入、输出、耗时、错误都落库，可回放。

---

### 6.5 Skill Runtime：按需装载的能力包

传统做法是把所有分析指令塞进一个巨大的 System Prompt——既浪费 Token，又容易互相干扰。本项目采用 **Skill 机制**：

```mermaid
flowchart LR
    Q["用户消息"] --> M{"显式指定<br/>或触发词匹配?"}
    M -->|命中| L["加载 SKILL.md<br/>完整指令"]
    M -->|未命中| N["仅保留基础工具"]
    L --> V["校验：工具白名单<br/>输出契约 · 安全级别"]
    V --> X["执行"]
    X --> T["Trace 记录<br/>version + 激活原因 +<br/>指令摘要 + 校验结果"]
    N --> X
```

内置 6 个 Skill：

| Skill | 触发意图 | 输出契约 | 安全级别 |
| :--- | :--- | :--- | :--- |
| `issue-triage` | 分诊、优先级、派活 | Issue 分析结构 | `read_only` |
| `pr-review` | 能不能合、代码审查 | `PRReviewOutput` | `read_only` |
| `ci-debug` | 流水线挂了、job 失败 | CI 分析结构 | `read_only` |
| `project-investigation` | 摸清项目、这块怎么实现的 | 调查报告 | `read_only` |
| `weekly-report` | 周报、这周干了啥 | 周报结构 | `read_only` |
| `safety-draft` | 发出去、改一下 | `ActionDraft` | `draft_only` |

每个 `SKILL.md` 都是标准 YAML Front Matter + 正文工作流，例如：

```yaml
---
name: pr-review
title: PR 审查
version: 0.1.0
description: 审查已同步的 Pull Request，输出摘要、关键变更、P1/P2/P3 分级发现、阻塞项、测试缺口。
category: github-analysis
entrypoint: pr_review_agent.analyze_pr
tools:
  - pr_review_agent.analyze_pr
  - workspace.read_file
  - workspace.search_code
  - rag.search_similar_documents
triggers:
  - pr
  - pull request
  - 合并请求
  - 代码审查
workflow_steps:
  - collect_pr_diff_and_comments
  - summarize_key_changes
  - classify_findings_by_p1_p2_p3
  - identify_risky_files
  - propose_tests
  - produce_merge_readiness_notes
output_contract: PRReviewOutput
safety_level: read_only
---
```

> 💡 **亮点**：`pr-review` 还有一个 `references/risk-checklist.md`，只在需要时读取——这就是"渐进式披露"：Skill 告诉模型**去哪找细节**，而不是一次性全塞进去。

---

### 6.6 RAG 知识问答管线

#### 核心哲学：不是所有数据都该向量化

```mermaid
flowchart TB
    Q["用户问题"] --> R{"数据属于哪一类?"}
    R -->|"当前源码 / 配置"| W["workspace.search_code<br/>workspace.read_file"]
    R -->|"实时状态<br/>Issue/PR/CI/团队"| S["结构化查询<br/>直接查库"]
    R -->|"历史讨论 / 文档 /<br/>失败日志 / 上传知识"| V["RAG 混合检索"]
    W --> A["证据聚合"]
    S --> A
    V --> A
    A --> G["Answer Gate<br/>证据充分性判定"]
    G --> ANS["带编号引用的回答"]
```

| 数据类型 | 检索方式 | 理由 |
| :--- | :--- | :--- |
| 源码、配置 | 词法检索（工作区工具） | 代码要**精确**匹配，向量召回会"看起来像"但不对 |
| Issue/PR/CI 状态字段 | 结构化查询 | 状态是事实，不能靠语义猜 |
| Issue 描述、Review 讨论、失败日志、文档、上传知识 | RAG 混合检索 | 需要跨来源语义召回、会反复使用 |
| 已批准的长期记忆 | RAG | 团队约定需要长期沉淀 |

#### 索引管线

```mermaid
flowchart LR
    F["上传文件<br/>TXT/MD/PDF/DOCX/JSON/CSV/LOG"] --> H{"SHA-256<br/>重复?"}
    H -->|是| SKIP["跳过"]
    H -->|否| EX["document_extraction<br/>文本抽取"]
    EX --> CH["chunking<br/>段落感知切片<br/>（代码用 AST）"]
    CH --> EM["embeddings<br/>Qwen3-Embedding-0.6B<br/>1024 维"]
    EM --> MIL[("Milvus<br/>devflow_rag_qwen1024")]
    CH --> KW[("关键词索引<br/>PostgreSQL")]
```

#### 检索管线

```mermaid
flowchart LR
    Q["查询"] --> R1["向量召回<br/>Milvus TopK"]
    Q --> R2["关键词召回<br/>BM25 式打分"]
    R1 --> M["候选合并去重"]
    R2 --> M
    M --> RR["rerank<br/>qwen3-rerank<br/>Top 24"]
    RR --> AG["answer_gate<br/>分数阈值 + 证据充分性"]
    AG --> CTX["上下文组装<br/>编号引用"]
    CTX --> LLM["基于证据生成"]
```

**降级设计**（这是真实系统的必备素养）：

| 故障场景 | 系统行为 |
| :--- | :--- |
| Milvus 不可用 | 抛出 `MilvusUnavailableError` → 全局异常处理器返回 **503** `{"status": "degraded"}`，前端明确显示"向量检索降级" |
| Embedding 模型缺失 | **显式报错**，绝不静默切换到另一套向量空间（否则向量会变成"鸡同鸭讲"） |
| 仅演示环境 | 可显式选择 `deterministic` embedding + 启发式重排，验证页面与流程，但状态标注 `degraded` |

> 🧠 **一句话记住**：**RAG 返回的是候选证据，不是事实。** 涉及实时状态和当前代码时，必须回到权威数据源核验。

#### RAG API 一览

```text
GET    /api/rag/models                                  模型目录
GET    /api/rag/repositories                            知识库列表
GET    /api/rag/repositories/{repo_id}                  知识库详情
PATCH  /api/rag/repositories/{repo_id}/config            更新切片/模型配置
GET    /api/rag/repositories/{repo_id}/documents         文档列表
POST   /api/rag/repositories/{repo_id}/documents         上传文档（202 异步处理）
GET    /api/rag/repositories/{repo_id}/documents/{id}/chunks   查看切片
POST   /api/rag/repositories/{repo_id}/documents/{id}/retry    重试处理
DELETE /api/rag/repositories/{repo_id}/documents/{id}    删除文档
POST   /api/rag/repositories/{repo_id}/retrieval-tests   召回测试
GET    /api/rag/repositories/{repo_id}/retrieval-tests   测试历史
GET    /api/rag/{repo_id}/status                         知识库状态
POST   /api/rag/{repo_id}/search                         语义搜索
POST   /api/rag/{repo_id}/ask                            直接问答
POST   /api/rag/{repo_id}/reindex                        重建索引
```

---

### 6.7 记忆体系：从证据到长期记忆

记忆不是"把所有聊天记录塞回去"，而是**分层 + 分级信任**：

```mermaid
flowchart TB
    subgraph S1["短期 · 会话内"]
        M1["ChatMessage<br/>原始消息"]
        M2["ChatSession<br/>会话摘要"]
    end
    subgraph S2["中期 · 仓库内"]
        M3["ThreadMemory<br/>facts / tasks / preferences"]
        M4["EvidenceItem<br/>引用证据"]
    end
    subgraph S3["长期 · 跨会话"]
        M5["MemoryCandidate<br/>候选记忆（待批准）"]
        M6["MemoryHub<br/>已批准记忆 → 进入 RAG"]
    end
    S1 -->|压缩封装| S2
    S2 -->|提炼| M5
    M5 -->|人工批准| M6
    M6 -->|召回| R["RAG 检索"]
```

| 设计点 | 说明 |
| :--- | :--- |
| **候选制** | Agent 提炼出的"事实"先进 `memory_candidates`，**人工批准**后才进入长期记忆，防止错误信息自我强化 |
| **召回留痕** | 每次召回写 `RecallEvent`，可回答"这条结论当初是基于哪条记忆" |
| **知识图谱** | `KnowledgeGraphEdge` 记录实体间关系，支持 `/knowledge/graph` 可视化与重建 |
| **MCP 接口** | `app/mcp/memory_server.py` 暴露标准 MCP 工具，可被外部 Agent 客户端复用 |

---

### 6.8 代码图谱与 PR 工作树快照

想审查 PR，就得先"看到"那个 PR 的代码状态。做法不是切分支（会污染工作区），而是**git worktree**：

```mermaid
flowchart LR
    A["POST /api/pull-requests/{id}/snapshot"] --> B["git worktree add<br/>独立目录"]
    B --> C["AST 解析<br/>Python ast"]
    C --> D["CodeSymbol<br/>函数/类/方法"]
    C --> E["CodeRelation<br/>调用/继承/导入"]
    D --> F[("PostgreSQL")]
    E --> F
    F --> G["代码图谱搜索<br/>符号定位 · 影响面分析"]
    G --> H["PR 审查 Agent 使用"]
    B -.->|过期清理| I["pr-snapshots/cleanup"]
```

| 特性 | 实现 |
| :--- | :--- |
| **并发安全** | 每个 PR 一把 `threading.Lock`，防止同一 PR 并发快照冲突 |
| **按需构建** | 只在需要时构建，不是全量常驻 |
| **过期回收** | 按时间清理，避免磁盘膨胀 |
| **增量场景** | 默认分支图谱由 `code_analysis.py` 负责，PR 增量由 `worktree_manager.py` 负责 |

---

### 6.9 安全边界：草稿、审计与权限

这是整个项目最"不像玩具"的地方。三层防线：

```mermaid
flowchart TB
    U["用户：帮我把这个 issue 关掉 / 发个公告"] --> SA["SafetyAgent<br/>classify_action_risk"]
    SA --> RISK{"风险等级?"}
    RISK -->|低| D1["ActionDraft<br/>status=pending"]
    RISK -->|高| D1
    D1 --> UI["前端 /action-drafts<br/>展示预览与影响面"]
    UI --> H{"人工确认?"}
    H -->|取消| C["status=cancelled"]
    H -->|确认| EX["执行<br/>status=confirmed"]
    EX --> AU[("AuditLog<br/>谁 · 对什么 · 做了什么 · 结果")]
    C --> AU
```

| 防线 | 机制 | 文件 |
| :--- | :--- | :--- |
| **① 草稿化** | 任何写操作/外发都先成为 `ActionDraft`，不入库不改仓库 | `services/agents/safety_agent.py` |
| **② 权限** | 角色-权限表：`owner` / `maintainer` / `developer` / `viewer` | `services/permissions.py` |
| **③ 审计** | 所有关键动作写 `AuditLog`，可回溯 | `db/models.py::AuditLog` |
| **Token 加密** | GitHub Token 用 Fernet 对称加密落库 | `core/security.py` |
| **Webhook 验签** | `GITHUB_WEBHOOK_SECRET` HMAC 校验 | `api/routes/webhooks.py` |
| **CORS 收敛** | 生产只认精确白名单，非生产放行 localhost | `main.py::_cors_origin_regex` |

**RBAC 权限矩阵：**

| 权限 \ 角色 | owner | maintainer | developer | viewer |
| :--- | :---: | :---: | :---: | :---: |
| `repo:read` | ✅ | ✅ | ✅ | ✅ |
| `repo:sync` | ✅ | ✅ | ❌ | ❌ |
| `agent:run` | ✅ | ✅ | ✅ | ❌ |
| `draft:create` | ✅ | ✅ | ✅ | ❌ |
| `draft:approve` | ✅ | ✅ | ❌ | ❌ |
| `eval:read` | ✅ | ✅ | ✅ | ✅ |
| `settings:manage` | ✅ | ❌ | ❌ | ❌ |

> ⚠️ **当前限制**：项目没有真正的登录体系，`ensure_demo_user()` 会自动创建一个 `demo-owner` 作为"当前用户"。RBAC 是**结构性演示**，不是安全边界。生产化必须接入真实 SSO。

---

### 6.10 上下文压缩与 Token 预算

长对话必然撑爆窗口。系统用一套**预算分配 + 分级压缩**策略：

```mermaid
flowchart TB
    IN["输入消息 + 历史 + 工具结果"] --> EST["估算 Token"]
    EST --> BUD{"占窗口比例?"}
    BUD -->|"< 告警线"| OK["直接发送"]
    BUD -->|"≥ 自动压缩线"| MC["Microcompact<br/>保留最近 8 条 + 摘要"]
    BUD -->|"≥ 激进线"| AC["Aggressive Compact"]
    BUD -->|"≥ 手动线"| MAN["提示用户手动压缩"]
    MC --> FAIL{"连续失败?"}
    AC --> FAIL
    FAIL -->|"≤ 3 次"| OK
    FAIL -->|"> 3 次"| ABORT["放弃压缩<br/>走兜底回答"]
```

**预算分配**（默认比例，可调）：

| 区块 | 占比 | 配置项 |
| :--- | :---: | :--- |
| System Prompt | 12% | `CONTEXT_SYSTEM_RATIO` |
| 记忆 | 22% | `CONTEXT_MEMORY_RATIO` |
| 证据 | 34% | `CONTEXT_EVIDENCE_RATIO` |
| 最近消息 | 22% | `CONTEXT_RECENT_RATIO` |
| 工具结果 | 10% | `CONTEXT_TOOL_RATIO` |

> 🎯 **证据占最大份额（34%）**——这直接体现了"**有据可依优先于聊得热闹**"的产品价值观。

---

### 6.11 评测体系

没有度量的 Agent 系统，等于没有仪表盘的飞机。项目有两套并行的评测：

#### ① 基础 Eval（`services/evals/runner.py`）

| 维度 | 衡量什么 |
| :--- | :--- |
| Issue 分类准确度 | 分类/优先级是否与标注一致 |
| PR 覆盖度 | 关键变更是否被摘要覆盖 |
| RAG 命中率 | 检索是否召回了应该召回的文档 |
| 工具 Schema 就绪度 | 工具定义是否可被模型正确解析 |
| 结构化输出校验 | 返回是否符合 Pydantic 契约 |

#### ② RAGAS 语义评测（`services/evals/ragas_runner.py`）

用 **Ragas 0.4 Collections API** 计算五项核心指标，外加工具调用质量：

| 指标 | 含义 | 默认门槛 |
| :--- | :--- | :---: |
| Context Precision | 检索到的上下文有多"精准" | ≥ 0.75 |
| Context Recall | 该召回的是否都召回了 | ≥ 0.80 |
| Faithfulness | 回答是否忠于证据（不胡说） | ≥ 0.90 |
| Answer Relevancy | 回答是否切题 | ≥ 0.80 |
| Agent Goal Accuracy | 是否真正完成了用户目标 | ≥ 0.80 |
| Tool Call Accuracy | 工具选对了吗 | ≥ 0.90 |
| Tool Call F1 | 工具调用的精确率/召回率调和 | ≥ 0.90 |

**评测不糊弄人的几个设计：**

- **隔离会话真实运行**：在独立 session 中跑**生产版** ChatAgent 和工具集，不是 mock。
- **异步任务化**：提交后立即返回 `queued` + `eval_id`，后台跑；刷新页面不中断。
- **状态与质量分离**：低分是**质量失败**；Judge 超时/指标缺失是**评测不完整**——绝不把"没测出来"伪装成"质量好"。
- **仅重试异常评分**：Judge 超时时可复用已保存的回答和证据重新打分，不必重跑 Agent。
- **回归对比**：同 Hash + 同配置 + 同 TopK 的两次结果可逐题对比。
- **失败即草稿**：可把失败 Case 一键生成 `create_issue` 类型的 `ActionDraft`，进入人工确认流程。

```bash
# 提交一次 RAGAS 评测
curl -X POST http://localhost:8000/api/evals/rag/run \
  -H "Content-Type: application/json" \
  -d '{
    "repo_id": "<repository-uuid>",
    "run_judge": true,
    "top_k": 5,
    "suite_id": "devflow-real-project-rag-baseline"
  }'

# 轮询进度
curl http://localhost:8000/api/evals/<eval_id>
```

---

## 七、一次对话的完整链路

把上面所有模块串起来看，一次"这个 PR 能合吗"的完整旅程：

```mermaid
sequenceDiagram
    autonumber
    participant U as 用户
    participant FE as Next.js
    participant API as FastAPI
    participant CA as ChatAgent
    participant SK as Skill Registry
    participant WF as WorkflowOrchestrator
    participant TL as 工具层
    participant DB as PostgreSQL
    participant MV as Milvus
    participant LLM as LLM 服务

    U->>FE: "PR #482 能合吗？"
    FE->>API: POST /api/chat/stream
    API->>CA: 组装上下文
    CA->>SK: 匹配触发词 "能合吗" → pr-review
    SK-->>CA: 加载 SKILL.md + 工具白名单
    CA->>MV: 预检索历史讨论
    MV-->>CA: 候选证据
    CA->>LLM: 推理：需要哪些工具？
    LLM-->>CA: 调用 workflow.run_engineering_review
    CA->>WF: 启动工作流
    WF->>TL: PR 审查 / CI 状态 / RAG 证据 / 仓库健康（并行）
    TL->>DB: 读 PR diff、评论、Workflow Run
    TL->>MV: 语义检索历史阻塞项
    TL-->>WF: 四路任务结果
    WF->>LLM: Observer 检查冲突与低置信
    LLM-->>WF: 发现：P1 迁移无回滚 + CI 失败
    WF->>LLM: Synthesis 生成决策备忘录
    LLM-->>WF: 结构化结论
    WF->>DB: 持久化 AgentWorkflowRun / AgentTaskRun
    WF-->>CA: 备忘录 + 引用
    CA->>DB: 写 ChatMessage / EvidenceItem / AgentRun
    CA-->>API: 流式 token
    API-->>FE: SSE 事件流
    FE-->>U: 结论 + 证据 + 建议动作
    Note over U,FE: 若需写操作 → ActionDraft → 人工确认 → 审计
```

**SSE 流式事件类型**（前端据此渲染）：

| 事件 | 含义 |
| :--- | :--- |
| `token` | 逐字输出 |
| `tool_call` | 模型决定调用工具 |
| `tool_result` | 工具返回（含耗时、摘要） |
| `citation` | 绑定一条证据 |
| `workflow_task` | 工作流子任务状态变化 |
| `done` | 完成，携带最终结构化结果 |

---

## 八、安装部署

### 8.1 环境要求

| 依赖 | 版本 | 说明 |
| :--- | :--- | :--- |
| Python | **3.10+**（推荐 3.11 / 3.12） | 代码使用 `str \| None` 等新语法 |
| Node.js | **18.18+**（推荐 20 LTS） | Next.js 15 要求 |
| Docker / Docker Compose | 任意较新版本 | 起 PostgreSQL / Milvus 等基础设施 |
| 内存 | ≥ 8 GB | Milvus standalone + MinIO + etcd 较吃内存 |
| 磁盘 | ≥ 5 GB 可用 | 镜像 + 仓库 checkout + 向量数据 |
| LLM API | OpenAI 兼容 | DeepSeek / 通义 / OpenAI 均可 |

### 8.2 步骤一：克隆与配置

```bash
git clone https://github.com/flowersoul999/DevMind-AI.git
cd DevMind-AI

# Windows (Git Bash / PowerShell)
cp .env.example .env

# macOS / Linux
cp .env.example .env
```

编辑 `.env`，**至少填这三项**：

```ini
GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxx      # 建议 classic token，勾 repo + workflow 权限
LLM_API_KEY=sk-xxxxxxxxxxxxxxxxxxxx
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-v4-pro
```

> 💡 如果想让 **RAG 向量检索真正跑通**，还需要配置 Embedding：
> - **方案 A（本地）**：`pip install -r backend/requirements-local-embedding.txt`，`EMBEDDING_PROVIDER=qwen_local`
> - **方案 B（云端百炼）**：配置 `DASHSCOPE_API_KEY`、`RERANK_BASE_URL`，切换 `text-embedding-v4` + `qwen3-rerank`

### 8.3 步骤二：启动基础设施

```bash
# 起 PostgreSQL + etcd + MinIO + Milvus
docker compose up -d postgres etcd minio milvus

# 查看状态（等 healthcheck 变 healthy）
docker compose ps
```

可选服务（按需）：

```bash
docker compose --profile optional up -d redis
```

### 8.4 步骤三：启动后端

**Windows PowerShell：**

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

**macOS / Linux：**

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

首次启动会自动：
1. `Base.metadata.create_all()` 建全部表；
2. 检查并补齐历史缺失字段（兼容旧库）；
3. 清理旧版代码向量；
4. 启动后台定时同步任务（`AUTO_SYNC_ENABLED=true` 时）。

### 8.5 步骤四：启动前端

```bash
cd frontend
npm install
npm run dev
```

### 8.6 步骤五：访问

| 服务 | 地址 |
| :--- | :--- |
| 前端控制台 | http://localhost:3000 |
| 后端 API | http://localhost:8000 |
| Swagger 文档 | http://localhost:8000/docs |
| ReDoc | http://localhost:8000/redoc |
| 健康检查 | http://localhost:8000/health |
| Milvus 控制台（Attu 可选） | http://localhost:9091 |
| MinIO 控制台 | http://localhost:9001 |

### 8.7 零依赖演示模式（Windows）

不想装 Docker、只想先看界面和流程？用内置脚本起一个 **SQLite + 降级检索** 的演示环境：

```powershell
.\scripts\start_rag.ps1

# 若已配置 LLM_API_KEY，想让模型真正生成回答
.\scripts\start_rag.ps1 -UseConfiguredLLM

# 自定义端口
.\scripts\start_rag.ps1 -BackendPort 8001 -FrontendPort 3001
```

脚本会设置：

| 环境变量 | 值 | 作用 |
| :--- | :--- | :--- |
| `DATABASE_URL` | `sqlite:///.data/devflow-local-demo.db` | 免 PostgreSQL |
| `MILVUS_ENABLED` | `false` | 关闭向量库 |
| `EMBEDDING_PROVIDER` | `deterministic` | 哈希向量占位 |
| `DEMO_MODE` | `true` | 注入演示数据 |
| `AUTO_SYNC_ENABLED` | `false` | 关闭自动同步 |

> ⚠️ **明确边界**：演示模式可以验证**页面、上传解析、关系库存储、关键词分支检索、引用展示**，但向量检索状态会显示 `degraded`。**它不能用来证明完整 RAG 已跑通**——要验证向量召回、混合检索与重建索引，必须走 8.3 的完整基础设施。

### 8.8 核心配置项说明

<details>
<summary><b>点击展开完整配置表</b></summary>

#### 应用与网络

| 变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `APP_ENV` | `development` | `production` 时 CORS 只认精确白名单 |
| `APP_NAME` | `DevFlow AI` | 显示在 API 文档标题 |
| `API_HOST` / `API_PORT` | `0.0.0.0` / `8000` | 监听地址与端口 |
| `CORS_ORIGINS` | `http://localhost:3000,...` | 逗号分隔的允许来源 |

#### 数据与 GitHub

| 变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `DATABASE_URL` | `postgresql+psycopg://devflow:devflow@localhost:5432/devflow` | 关系库连接串 |
| `GITHUB_TOKEN` | 空 | 个人访问令牌，**建议加密后存库** |
| `GITHUB_API_BASE_URL` | `https://api.github.com` | 支持 GitHub Enterprise / 自建 |
| `GITHUB_WEBHOOK_SECRET` | 空 | Webhook 验签密钥 |
| `TOKEN_ENCRYPTION_KEY` | `change-me-in-local-dev` | **生产必须更换** |
| `REPO_CHECKOUT_DIR` | `.data/repos` | 仓库克隆目录 |
| `UPLOAD_DIR` | `.data/uploads` | 上传文件目录 |

#### LLM 与向量

| 变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | 空 / DeepSeek / `deepseek-v4-pro` | 主推理模型 |
| `EMBEDDING_PROVIDER` | `qwen_local` | `qwen_local` / `openai_compatible` / `deterministic` |
| `EMBEDDING_MODEL` | `Qwen/Qwen3-Embedding-0.6B` | 向量模型 |
| `EMBEDDING_DIMENSIONS` | `1024` | **必须与 Milvus 集合维度一致** |
| `DASHSCOPE_API_KEY` | 空 | 阿里云百炼 Key |
| `MILVUS_ENABLED` | `true` | 关闭则走降级 |
| `MILVUS_URI` | `http://localhost:19530` | Milvus 地址 |
| `MILVUS_COLLECTION` | `devflow_rag_qwen1024` | 集合名（含维度后缀） |
| `RAG_RERANK_TOP_N` | `24` | 重排保留条数 |

#### 评测阈值

| 变量 | 默认值 |
| :--- | :---: |
| `RAGAS_ENABLED` | `true` |
| `RAGAS_CONTEXT_PRECISION_MIN` | `0.75` |
| `RAGAS_CONTEXT_RECALL_MIN` | `0.80` |
| `RAGAS_FAITHFULNESS_MIN` | `0.90` |
| `RAGAS_ANSWER_RELEVANCY_MIN` | `0.80` |
| `RAGAS_AGENT_GOAL_ACCURACY_MIN` | `0.80` |
| `RAGAS_TOOL_CALL_ACCURACY_MIN` | `0.90` |
| `RAGAS_TOOL_CALL_F1_MIN` | `0.90` |
| `RAGAS_TIMEOUT_SECONDS` | `120` |

#### 上下文与同步

| 变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `CONTEXT_MAX_INPUT_TOKENS` | `24000` | 单次上下文上限 |
| `CONTEXT_AUTO_COMPACT_ENABLED` | `true` | 自动压缩开关 |
| `CONTEXT_AUTO_COMPACT_BUFFER_TOKENS` | `13000` | 触发自动压缩的剩余量 |
| `CONTEXT_MAX_COMPACTION_FAILURES` | `3` | 压缩失败上限 |
| `AUTO_SYNC_ENABLED` | `true` | 自动同步开关 |
| `AUTO_SYNC_INTERVAL_SECONDS` | `300` | 同步间隔 |
| `AUTO_SYNC_LIMIT` | `50` | 单次同步条数上限 |
| `FEISHU_FEEDBACK_WEBHOOK_URL` | 空 | 负反馈告警机器人 |

</details>

### 8.9 生产化改造清单

> 如果你打算真的部署给团队用，必须处理这些（当前是教学级实现）：

- [ ] **替换 `create_all` 为 Alembic 迁移**（项目已装 `alembic==1.14.0` 但未建迁移目录）
- [ ] **接入真实认证**（OIDC / SSO），移除 `ensure_demo_user`
- [ ] **更换 `TOKEN_ENCRYPTION_KEY`** 并用 KMS 管理
- [ ] **`APP_ENV=production`**，收紧 CORS 白名单
- [ ] **把后台同步改为外部任务队列**（当前 asyncio 循环只支持单进程）
- [ ] **补应用层 Dockerfile + 反向代理 + TLS**
- [ ] **Milvus 换集群模式**，开启持久化与备份
- [ ] **加速率限制与请求配额**（LLM 成本控制）

---

## 九、使用方法

### 9.1 页面导览

| 路由 | 页面 | 你能做什么 |
| :--- | :--- | :--- |
| `/` | 总览 | 查看仓库健康度、快捷入口 |
| `/repos` | 仓库管理 | 连接仓库、同步、删除（级联清理） |
| `/workspaces` | 工作区 | 组合多个仓库、生成合并周报 |
| `/issues` | Issue 列表 | 分诊分析、相似 Issue、流式输出 |
| `/pull-requests` | PR 列表 | 审查、检查清单、快照准备 |
| `/ci` | CI 运行 | 查看失败 job、日志分析与排查建议 |
| `/chat` | 对话工作室 | 与 Agent 多轮对话、工具轨迹、引用 |
| `/workflow-runs` | 工作流记录 | 回看多 Agent 审查的每个子任务 |
| `/knowledge` | 知识库 | 上传文档、切片查看、召回测试 |
| `/knowledge/[id]` | 知识库详情 | 文档管理、重建索引、知识图谱 |
| `/rag` | RAG 工作室 | 语义搜索、直接问答、模型配置 |
| `/code-graph` | 代码图谱 | 符号搜索、关系浏览、重建 |
| `/evals` | 评测中心 | 跑 RAGAS、看失败诊断、回归对比 |
| `/action-drafts` | 动作草稿 | 预览、确认、取消、审计日志 |
| `/reports` | 周报 | 生成与查看工程周报 |

### 9.2 界面结构示意

控制台采用「左侧导航 + 顶部上下文 + 主内容区」三段式布局：

```text
┌──────────────────────────────────────────────────────────────────────┐
│  DevFlow AI            [ 仓库选择器 ▾ ]        搜索   反馈   设置    │
├────────────┬─────────────────────────────────────────────────────────┤
│            │                                                         │
│  ▸ 总览    │   ┌─────────────────────┐  ┌─────────────────────┐      │
│  ▸ 仓库    │   │  仓库健康度          │  │  待处理动作草稿      │      │
│  ▸ 工作区  │   │  ● CI 通过率 82%    │  │  ⚠ 关闭 Issue #482   │      │
│  ▸ Issues  │   │  ● 未解决评论 12    │  │  ⚠ 发布公告草稿      │      │
│  ▸ PR      │   └─────────────────────┘  └─────────────────────┘      │
│  ▸ CI      │                                                         │
│  ▸ 对话    │   ┌─────────────────────────────────────────────────┐   │
│  ▸ 工作流  │   │  最近分析                                        │   │
│  ▸ 知识库  │   │  PR #482  P1×1  P2×3   8 分钟前                  │   │
│  ▸ RAG     │   │  Issue #501  分类: bug  优先级: 高   21 分钟前   │   │
│  ▸ 代码图谱│   └─────────────────────────────────────────────────┘   │
│  ▸ 评测    │                                                         │
│  ▸ 草稿    │                                                         │
│  ▸ 周报    │                                                         │
└────────────┴─────────────────────────────────────────────────────────┘
```

对话页的证据面板结构：

```text
┌───────────────────────────────────────────────────────────────┐
│ 你：PR #482 能合吗？                                          │
├───────────────────────────────────────────────────────────────┤
│ Agent：不建议现在合入。                                       │
│                                                               │
│ 【阻塞项】                                                    │
│ 1. 迁移脚本缺少回滚逻辑 [1]                                   │
│ 2. CI build job 失败：依赖版本冲突 [2]                        │
│                                                               │
│ 【建议补充测试】                                              │
│ · 空数据场景                                                  │
│ · 并发写入幂等性                                              │
├───────────────────────────────────────────────────────────────┤
│ 📎 证据 (2)   🔧 工具轨迹 (5)   ⏱ 12.4s   🧩 pr-review v0.1.0 │
│ [1] pr_files/2024_add_index.py  L12-40                        │
│ [2] workflow_runs/9931 · build · exit 1  (日志片段)            │
└───────────────────────────────────────────────────────────────┘
```

> 📷 **想加真实截图？** 建议在仓库建 `docs/images/` 目录，把控制台、对话、评测页截图放进去，然后把上面的示意图替换为：
> `![对话工作台](docs/images/chat.png)`

### 9.3 API 使用示例

#### 连接一个仓库

```bash
curl -X POST http://localhost:8000/api/repos/connect \
  -H "Content-Type: application/json" \
  -d '{
    "provider": "github",
    "owner": "flowersoul999",
    "name": "DevMind-AI",
    "token": "ghp_xxxxxxxxxxxx"
  }'
```

#### 手动同步

```bash
curl -X POST http://localhost:8000/api/repos/<repo_id>/sync \
  -H "Content-Type: application/json" \
  -d '{"limit": 50}'
```

#### 分析一个 Issue（流式）

```bash
curl -N -X POST http://localhost:8000/api/issues/<issue_id>/analyze/stream \
  -H "Content-Type: application/json" \
  -d '{}'
```

#### 审查一个 PR（同步返回）

```bash
curl -X POST http://localhost:8000/api/pull-requests/<pr_id>/analyze \
  -H "Content-Type: application/json" \
  -d '{}'
```

#### 与 Agent 对话（流式）

```bash
curl -N -X POST http://localhost:8000/api/chat/stream \
  -H "Content-Type: application/json" \
  -d '{
    "repo_id": "<repo_id>",
    "message": "这个 PR 有什么风险？需要补哪些测试？",
    "skill": "pr-review"
  }'
```

#### 语义搜索

```bash
curl -X POST http://localhost:8000/api/rag/<repo_id>/search \
  -H "Content-Type: application/json" \
  -d '{"query": "鉴权是怎么实现的", "top_k": 5}'
```

#### 生成周报

```bash
curl -X POST http://localhost:8000/api/reports/weekly \
  -H "Content-Type: application/json" \
  -d '{"repo_id": "<repo_id>", "since_days": 7}'
```

#### 确认一个动作草稿

```bash
# 先看草稿
curl http://localhost:8000/api/action-drafts/<draft_id>

# 人工确认后执行
curl -X POST http://localhost:8000/api/action-drafts/<draft_id>/confirm \
  -H "Content-Type: application/json" \
  -d '{"approved": true}'
```

### 9.4 主要 API 分组

| 前缀 | 路由文件 | 典型端点 |
| :--- | :--- | :--- |
| `/api/repos` | `repos.py` | connect / list / sync / select / delete / sync-status |
| `/api/repos` | `conversations.py` | 会话列表 / 新建 / 删除 |
| `/api/repos` | `knowledge.py` | 知识列表、记忆候选、团队成员、知识图谱 |
| `/api/repos` | `project_index.py` | 索引状态 / 触发扫描 / 暂停 |
| `/api/workspaces` | `workspaces.py` | 新建 / 列表 / 多仓合并周报 |
| `/api` | `issues.py` | 列表 / 详情 / 分析 / 流式分析 / 相似 Issue |
| `/api` | `pull_requests.py` | 列表 / 详情 / 审查 / 流式审查 / 快照 / 检查清单 |
| `/api` | `ci.py` | 运行列表 / 失败 job / 分析 / 流式分析 |
| `/api` | `code_graph.py` | 重建图谱 / 符号列表 / 图谱搜索 |
| `/api/chat` | `chat.py` | 对话 / 流式 / 历史 / 计划执行 / 推荐问题 |
| `/api/chat/feedback` | `feedback.py` | 提交反馈 / 指标 / 轨迹 |
| `/api/action-drafts` | `action_drafts.py` | 创建 / 列表 / 确认 / 取消 / 审计日志 |
| `/api/reports` | `reports.py` | 周报 / 仓库健康度 |
| `/api/search` | `search.py` | 全局搜索 |
| `/api/skills` | `skills.py` | Skill 列表 / 详情 / MCP 状态 |
| `/api/rag` | `rag.py` | 知识库全生命周期（见 6.6） |
| `/api/evals` | `evaluations.py` | 评测集 / 运行 / 重打分 / 对比 / 失败转草稿 |
| `/api/webhooks` | `webhooks.py` | GitHub 回调 |

完整字段定义请访问 **http://localhost:8000/docs**（自动生成的 OpenAPI）。

---

## 十、依赖与运行环境

### 10.1 后端核心依赖

| 包 | 版本 | 用途 |
| :--- | :--- | :--- |
| `fastapi` | 0.115.6 | Web 框架 |
| `uvicorn[standard]` | 0.34.0 | ASGI 服务器 |
| `pydantic` | 2.10.4 | 数据校验 |
| `pydantic-settings` | ≥2.10.1 | 配置管理 |
| `SQLAlchemy` | 2.0.36 | ORM |
| `psycopg[binary]` | 3.2.3 | PostgreSQL 驱动 |
| `pgvector` | 0.3.6 | 向量类型支持 |
| `pymilvus` | ≥2.5.0 | Milvus 客户端 |
| `alembic` | 1.14.0 | 迁移工具（已装待用） |
| `httpx` | 0.28.1 | 异步 HTTP |
| `langchain` | 1.2.17 | Agent 基建 |
| `langchain-community` | 0.4.1 | 社区集成 |
| `langgraph` | 1.1.10 | 状态图编排 |
| `langchain-openai` | 1.2.1 | OpenAI 兼容层 |
| `openai` | 2.53.0 | 官方 SDK |
| `ragas` | 0.4.3 | RAG 评测 |
| `datasets` | ≥3.0.0 | Ragas 数据集 |
| `python-dotenv` | 1.0.1 | `.env` 加载 |
| `PyYAML` | 6.0.3 | Skill Front Matter 解析 |
| `cryptography` | 44.0.0 | Fernet 加密 |
| `python-multipart` | 0.0.20 | 文件上传 |
| `pypdf` | ≥5.1.0 | PDF 解析 |
| `python-docx` | ≥1.1.2 | DOCX 解析 |

**可选（本地 Embedding）** `requirements-local-embedding.txt`：

```text
sentence-transformers>=5.0.0
```

### 10.2 前端依赖

| 包 | 版本 | 用途 |
| :--- | :--- | :--- |
| `next` | 15.1.2 | 框架（App Router） |
| `react` / `react-dom` | 19.0.0 | UI |
| `@tanstack/react-query` | ^5.62.7 | 数据请求与缓存 |
| `react-markdown` | ^9.0.1 | Markdown 渲染 |
| `remark-gfm` | ^4.0.1 | GFM 表格等语法 |
| `lucide-react` | ^0.468.0 | 图标 |
| `clsx` | ^2.1.1 | 类名拼接 |
| `tailwindcss` | ^3.4.17 | 样式 |
| `typescript` | ^5.7.2 | 类型系统 |

### 10.3 基础设施镜像

| 服务 | 镜像 | 端口 |
| :--- | :--- | :--- |
| PostgreSQL | `pgvector/pgvector:pg16` | 5432 |
| Redis（可选） | `redis:7-alpine` | 6379 |
| etcd | `quay.io/coreos/etcd:v3.5.18` | 2379 |
| MinIO | `minio/minio:latest` | 9000 / 9001 |
| Milvus | `milvusdb/milvus:v2.5.4` | 19530 / 9091 |

---

## 十一、测试与评测

### 11.1 运行单元测试

```bash
cd backend
python -m pytest app/tests -v
```

覆盖 **27 个测试模块**，重点场景包括：

| 测试文件 | 覆盖内容 |
| :--- | :--- |
| `test_chat_agent.py` | 对话主流程、工具调用、兜底回答 |
| `test_workflow_orchestrator.py` | 任务图、依赖阻塞、重规划 |
| `test_skill_registry.py` | Skill 加载、工具白名单、触发匹配 |
| `test_rag_retrieval_pipeline.py` | 混合检索全链路 |
| `test_rag_rerank.py` | 重排逻辑与分数 |
| `test_rag_qa.py` | 基于证据的问答与引用 |
| `test_rag_source_policy.py` | 数据源路由策略 |
| `test_rag_project_isolation.py` | 多仓库数据隔离（**关键安全测试**） |
| `test_vector_store.py` | Milvus 读写与降级 |
| `test_chunking.py` | 段落感知切片 |
| `test_context_compression.py` | 压缩与预算 |
| `test_context_memory_projection.py` | 记忆投影 |
| `test_code_search.py` / `test_code_indexing.py` | 代码检索与索引 |
| `test_repos_sync.py` / `test_repos_connect.py` | 同步与连接 |
| `test_repo_deletion.py` / `test_conversation_deletion.py` | 级联删除正确性 |
| `test_eval_runner.py` | 评测执行器 |
| `test_knowledge_graph.py` | 知识图谱 |
| `test_expansion_features.py` | 扩展特性回归 |

### 11.2 运行质量评测

打开 http://localhost:3000/evals ：

1. 选择仓库；
2. 选择固定评测集 `devflow-real-project-rag-baseline`；
3. 设置 Top-K（默认 5）；
4. 点击「运行固定 RAGAS 评测」；
5. 查看进度 → 结果 → 展开失败 Case 诊断。

正式基准位于 `docs/datas/evals/rag_cases.json`，启动时会校验用例引用的文档是否已进入知识库。

### 11.3 评测结果怎么读

| 状态 | 含义 | 该怎么做 |
| :--- | :--- | :--- |
| `passed` | 硬规则通过且五项指标全部达标 | 可发布 |
| `failed` | 指标低于门槛 | 看失败 Case，改检索/提示词/切片 |
| `error` | 执行异常 | 看错误堆栈与环境 |
| `no_cases` | 评测集为空或不匹配 | 检查 `suite_id` 与文档索引 |
| 指标缺失 | **评测不完整**，非质量结论 | 点「仅重试异常评分」 |

---

## 十二、项目结构

```text
DevFlow-AI/
├── backend/
│   ├── app/
│   │   ├── main.py                 # 应用工厂：路由挂载、CORS、启动/关闭事件
│   │   ├── api/routes/             # 18 个路由模块（repos / issues / pr / ci / rag / evals ...）
│   │   ├── core/
│   │   │   ├── config.py           # 唯一的配置中心（pydantic-settings）
│   │   │   ├── security.py         # GitHub Token 的 Fernet 加解密
│   │   │   └── logging.py
│   │   ├── db/
│   │   │   ├── models.py           # 40+ ORM 模型（1000 行）
│   │   │   └── session.py          # 引擎与会话工厂
│   │   ├── schemas/                # 18 个 Pydantic 契约模块
│   │   ├── services/
│   │   │   ├── agents/             # 11 个 Agent（chat / planner / observer / synthesis / 专用分析）
│   │   │   ├── rag/                # 12 个 RAG 模块（切片/向量/检索/重排/问答/门禁）
│   │   │   ├── evals/              # 基础 Eval + RAGAS
│   │   │   ├── github/             # GitHub 客户端与资源操作
│   │   │   ├── llm/                # LLM 客户端、提示词、结构化输出
│   │   │   ├── skills/             # Skill 注册中心
│   │   │   ├── code_analysis.py    # AST 代码图谱
│   │   │   ├── worktree_manager.py # PR 工作树快照
│   │   │   ├── context_compression.py
│   │   │   ├── chat_memory.py / memory_hub.py
│   │   │   ├── permissions.py      # RBAC + 审计
│   │   │   └── scheduler.py        # 定时同步循环
│   │   ├── skills/                 # 6 个 SKILL.md 能力包
│   │   ├── mcp/memory_server.py    # MCP 记忆服务
│   │   └── tests/                  # 27 个 pytest 模块
│   ├── requirements.txt
│   └── requirements-local-embedding.txt
├── frontend/
│   ├── app/                        # 17 个页面（App Router）
│   ├── components/                 # AppShell / RAG 设置 / 反馈控件 / UI 基础组件
│   ├── lib/                        # api.ts（统一请求层）/ rag.ts / types.ts
│   ├── package.json
│   └── next.config.js
├── docs/
│   ├── api/openapi.md
│   ├── architecture/               # system_design / agent_workflow / database_schema / security
│   ├── examples/                   # RAG 示例语料
│   └── datas/evals/                # 评测基准用例
├── scripts/
│   ├── start_rag.ps1               # 零依赖演示启动
│   ├── init_pgvector.sql
│   ├── seed_demo_data.py
│   └── ...
├── docker-compose.yml              # 基础设施编排
└── .env.example
```

---

## 十三、常见问题 FAQ

<details>
<summary><b>Q1：启动报错「找不到 Milvus」或向量检索一直 degraded？</b></summary>

**原因**：Milvus 未启动，或 `MILVUS_URI` 配错。

**排查**：
```bash
docker compose ps                    # milvus 是否 healthy
curl http://localhost:9091/healthz   # 探活
```
**解决**：`docker compose up -d etcd minio milvus`，等 `depends_on` 链完成后重试。若只是看界面，可 `MILVUS_ENABLED=false` 走降级模式，但检索质量会下降。
</details>

<details>
<summary><b>Q2：上传文档成功，但检索搜不到？</b></summary>

按顺序检查：

1. **索引是否完成**：`GET /api/rag/{repo_id}/status` 看 `degraded` 或 `indexed` 状态。
2. **维度是否一致**：`EMBEDDING_DIMENSIONS` 必须与 `MILVUS_COLLECTION` 后缀（`...qwen1024`）匹配。不一致会导致写入失败。
3. **是否需要重建**：从旧版本升级后，必须对每个知识库调用一次 `POST /api/rag/{repo_id}/reindex`。**只部署代码不会自动清理旧索引。**
4. **分数阈值**：演示模式下调低阈值才能看到候选（仅限演示，别带进生产）。
5. **文件类型**：确认是支持的格式（TXT/MD/PDF/DOCX/JSON/CSV/LOG）；重复文件会按 SHA-256 跳过，看日志确认是否被跳过。
</details>

<details>
<summary><b>Q3：`Embedding 依赖缺失` 报错，为什么不像别家那样自动降级？</b></summary>

**这是刻意的设计。** 不同 Embedding 模型的向量空间互不兼容——静默切换到另一套模型，会让查询向量和库里的向量"鸡同鸭讲"，检索结果看似有返回、实则全是噪声。

所以系统选择**显式报错**。修复方式：
- 装依赖：`pip install -r backend/requirements-local-embedding.txt`
- 或配云端：设置 `DASHSCOPE_API_KEY` + `EMBEDDING_PROVIDER=openai_compatible`
- 或明确接受降级：仅演示环境用 `EMBEDDING_PROVIDER=deterministic`
</details>

<details>
<summary><b>Q4：Agent 回答很慢，或者上下文超限？</b></summary>

- **降低工具半径**：明确指定 `skill`，减少模型自由探索。
- **调小预算**：`CONTEXT_MAX_INPUT_TOKENS`、`RAG_RERANK_TOP_N`。
- **开启自动压缩**：确认 `CONTEXT_AUTO_COMPACT_ENABLED=true`。
- **检查是否死循环**：看 `/api/chat/feedback/trace/{run_id}`，确认是否有重复工具调用。
- **模型侧**：`LLM_MODEL` 换成更快的型号；`PROMPT_CACHE_ENABLED=true` 可复用系统提示词缓存。
</details>

<details>
<summary><b>Q5：为什么 Agent 不直接帮我把 PR 合了 / Issue 关了？</b></summary>

**这是安全设计，不是能力缺失。** 所有写操作都走 `ActionDraft`：Agent 生成草稿 → 你在 `/action-drafts` 页面看到完整预览与影响面 → 手动确认 → 才执行并写 `AuditLog`。

这样可以避免：模型幻觉导致误操作、Prompt 注入诱导删除数据、无痕操作难以追责。
</details>

<details>
<summary><b>Q6：定时同步会不会重复跑？多副本部署怎么办？</b></summary>

当前调度器是**进程内 asyncio 循环**，多副本部署会**重复同步**。

- 单机 / 演示：`AUTO_SYNC_ENABLED=true` 即可。
- 多副本：设 `AUTO_SYNC_ENABLED=false`，改由外部 Cron / 任务队列调用 `POST /api/repos/{repo_id}/sync`。
</details>

<details>
<summary><b>Q7：支持 GitLab / Gitee 等非 GitHub 平台吗？</b></summary>

数据库层已预留 `provider`、`api_base_url`、`clone_url` 等字段，`scheduler` 也识别 `github_compatible`。但要真正接入 GitLab/Gitee，需要实现对应的客户端适配（当前 `services/github/*` 是按 GitHub REST API 写的）。
</details>

<details>
<summary><b>Q8：`.env` 里的 `DEMO_MODE` 是干什么的？</b></summary>

`DEMO_MODE=true` 会注入演示数据、放宽部分校验，用于课程演示。**生产必须设为 `false`**（`.env.example` 里默认就是 `false`）。
</details>

<details>
<summary><b>Q9：为什么 README 里项目叫 DevFlow AI，仓库叫 DevMind-AI？</b></summary>

历史原因：仓库创建时用 `DevMind-AI`，产品化后统一对外名称是 **DevFlow AI**（配置项 `APP_NAME`、前端 `metadata.title` 均为 DevFlow AI）。两者指同一个项目。
</details>

<details>
<summary><b>Q10：跑评测时提示「用例引用的文档未进入知识库」？</b></summary>

评测基准 `docs/datas/evals/rag_cases.json` 引用了特定文档。需要先把这些文档上传到对应仓库的知识库并等索引完成，否则 Case 无法正确判定。
</details>

---

## 十四、路线图

| 阶段 | 目标 | 状态 |
| :--- | :--- | :---: |
| v0.1 | 数据接入 + Issue/PR/CI 单点分析 | ✅ 已完成 |
| v0.2 | ChatAgent 原生工具调用 + Skill Runtime | ✅ 已完成 |
| v0.3 | 多 Agent 工作流 + Observer 门禁 | ✅ 已完成 |
| v0.4 | 完整 RAG（切片/混合检索/重排/引用）+ 记忆体系 | ✅ 已完成 |
| v0.5 | RAGAS 评测 + 回归对比 + 失败转草稿 | ✅ 已完成 |
| v0.6 | **真实认证与多租户** | 🚧 规划中 |
| v0.7 | **Alembic 迁移体系 + 应用容器化** | 🚧 规划中 |
| v0.8 | **GitLab / Gitee 适配器** | 💡 构想 |
| v0.9 | **外部任务队列（Celery / Arq）替代进程内调度** | 💡 构想 |
| v1.0 | **写操作真实闭环（带审批流的 GitHub 写回）** | 💡 构想 |

---

## 十五、贡献指南

欢迎 Issue 与 PR。提交前请确认：

1. **分支**：从 `main` 切出 `feature/<your-topic>`。
2. **测试**：新增逻辑请补 `backend/app/tests/` 下的用例，确保 `pytest` 全绿。
3. **契约**：修改接口请同步 `schemas/` 下的 Pydantic 模型，保持 OpenAPI 一致。
4. **配置**：新增配置项请同时更新 `core/config.py` 与 `.env.example`。
5. **文档**：涉及架构变化时，同步更新 `docs/architecture/`。
6. **不要提交**：`.env`、`.data/`、`*.db`、`node_modules/`、`.venv/`。

```bash
# 推荐的自检流程
cd backend && python -m pytest app/tests -q
cd ../frontend && npm run lint && npm run build
```

---

<div align="center">

### 最后一句

> 好的 AI 工程系统不是"让模型说更多"，
> 而是**让模型说得有据、动得有界、错得有痕**。

**DevFlow AI** —— 把研发协作里的碎片，缝成一张能用的地图。🗺️

<br/>

`Python` · `FastAPI` · `Next.js` · `PostgreSQL` · `Milvus` · `LangGraph` · `Ragas`

</div>
