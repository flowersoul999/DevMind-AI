---
name: safety-draft
title: 安全草稿
version: 0.1.0
description: 识别有风险的外部发送或 GitHub 写操作，生成可人工确认的草稿，而不是直接执行写入。
category: safety
entrypoint: safety_agent.classify_action_risk
tools:
  - safety_agent.classify_action_risk
  - devflow_search_evidence
input_modes:
  - write_intent_from_chat
  - external_send_intent
triggers:
  - 评论
  - 回复
  - comment
  - 打标签
  - label
  - 关闭 issue
  - 创建 issue
  - 发送报告
  - 外部发送
  - 写入
workflow_steps:
  - detect_write_or_send_intent
  - classify_action_risk
  - draft_human_reviewable_output
  - require_confirmation
output_contract: SafetyDraft
safety_level: guarded_write_draft
---

# 安全草稿

当用户要求评论、回复、打标签、关闭 Issue、创建 Issue、发送报告，或执行任何对话外写操作时，使用这个技能。

## 工作流

1. 识别外部写入或发送意图。
2. 判断操作风险，并识别敏感上下文。
3. 生成可由人工审阅的草稿。
4. 写清为什么需要人工确认，以及确认后下一步应该由哪个工具或人工动作完成。
5. 在执行外部动作前停止。

## 边界

在 MVP 模式下，这个技能永远不执行写操作，只返回草稿和确认要求。
