---
name: ci-debug
title: CI 排障
version: 0.1.0
description: 根据 job 元数据和日志诊断失败的 GitHub Actions workflow run，并提出可能原因与排查步骤。
category: github-analysis
entrypoint: ci_debug_agent.analyze_workflow_run
tools:
  - ci_debug_agent.analyze_workflow_run
  - workspace.search_code
  - workspace.read_file
  - rag.search_similar_documents
input_modes:
  - latest_failed_workflow_run
  - ci_context_from_chat
triggers:
  - ci
  - workflow
  - github actions
  - 流水线
  - 构建失败
  - 测试失败
workflow_steps:
  - collect_failed_jobs_and_logs
  - classify_failure_type
  - identify_likely_root_causes
  - map_failure_to_code_or_config
  - produce_debug_steps
output_contract: CIDebugOutput
safety_level: read_only
---

# CI 排障

当用户询问 CI 为什么失败、哪个测试或 job 出错、或如何排查 workflow 失败时，使用这个技能。

## 工作流

1. 读取最新失败的 workflow run、jobs、steps 和可用日志。
2. 先判断失败类型，再提出修复方向。
3. 将可能原因绑定到日志或仓库上下文中的证据；没有日志依据时标记为推断并降低置信度。
4. 优先提取首个关键错误块，不要用后续连锁错误替代根因入口。
5. 在可用时返回聚焦的排查步骤和相关文件。

## 边界

这个技能只负责诊断和起草下一步，不能 push commit、重新运行 workflow 或编辑仓库文件。
