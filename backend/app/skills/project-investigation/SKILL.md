---
name: project-investigation
title: 项目证据调查
version: 0.1.0
description: 联合当前工作区、项目 RAG 和会话记忆回答实现与历史决策问题。
category: project-analysis
entrypoint: devflow_search_evidence
tools:
  - workspace.list_files
  - workspace.search_code
  - workspace.read_file
  - rag.search_similar_documents
  - devflow_search_evidence
  - devflow_get_thread_context
  - devflow_read_session_events
input_modes:
  - current_implementation
  - historical_decision
  - conversation_recall
triggers:
  - 当前实现
  - 源码
  - 代码在哪
  - 历史原因
  - 历史决策
  - 历史证据
  - 以前讨论
  - 项目证据
  - 项目记忆
  - 会话原话
  - 封存会话
workflow_steps:
  - identify_current_or_historical_question
  - search_current_workspace_for_current_code
  - search_project_evidence_for_history
  - read_exact_file_or_session_when_needed
  - answer_with_citations_and_uncertainty
output_contract: evidence_answer
safety_level: read_only
---

# 项目证据调查

当用户同时询问当前实现、历史原因或此前讨论时，使用这个技能。

## 工作流

1. 先判断问题是在问当前源码、项目历史，还是当前会话。
2. 当前源码使用 `workspace.search_code`，命中后用 `workspace.read_file` 阅读上下文。
3. 历史 Issue、PR、CI、项目知识和已批准记忆使用 `devflow_search_evidence`。
4. 用户追问原始对话时使用 `devflow_get_thread_context`；需要封存摘要时使用 `devflow_read_session_events`。
5. 回答时区分证据、推断和未知项，并保留可验证引用。

## 边界

这个技能只读运行。它不能修改源码、写入 GitHub、批准候选记忆或替用户执行外部动作。
