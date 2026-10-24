# Agent 工作流

用户消息进入 `/api/chat` 后：

1. `ChatAgent` 使用原生工具调用能力选择下一步要使用的工具。
2. 工作区工具可以列出文件、读取文件以及搜索本地代码。
3. 记忆工具用于检索当前范围内的对话历史。RAG 用于检索可复用的非结构化证据，例如 Issue 描述、PR 讨论、失败的 CI 日志、项目文档、用户上传的知识以及经过确认的记忆笔记。工作区工具和结构化工具则负责处理当前代码与实时事实。
4. 专项 Agent 分别执行 Issue、PR、CI、报告或安全分析。
5. `SafetyAgent` 负责处理对外写入意图。在 MVP 阶段，它只生成操作草稿，不会直接写回 GitHub。
6. 后端记录工具调用、引用来源、最终回答以及持久化的聊天记忆。

对于涉及多个领域的工程决策，`ChatAgent` 可以调用 `workflow.run_engineering_review`：

1. `PlannerAgent` 创建一个边界明确的 `WorkflowSpec`，其中包含任务声明、依赖关系和验收标准。
2. `WorkflowOrchestrator` 在依赖条件满足时，并行执行已经就绪的任务。
3. 现有的 Issue、PR、CI、RAG、仓库健康检查和安全能力会作为工作流任务执行。
4. `ObserverAgent` 检查任务输出中的合并门禁、跨 Agent 冲突、低置信度、证据缺失以及需要人工确认的事项。
5. `SynthesisAgent` 生成一份统一的决策备忘录，后端同时持久化 `AgentWorkflowRun` 和 `AgentTaskRun` 记录，以便后续追踪和审查。
