---
name: issue-triage
title: Issue 分诊
version: 0.1.0
description: 将已同步的 GitHub Issue 分析为分类、优先级、复杂度、负责人建议和行动项。
category: github-analysis
entrypoint: issue_agent.analyze_issue
tools:
  - issue_agent.analyze_issue
  - rag.search_similar_documents
  - devflow_search_evidence
input_modes:
  - latest_synced_issue
  - issue_context_from_chat
triggers:
  - issue
  - bug
  - 工单
  - 分诊
  - 优先级
  - 负责人
  - 分配
workflow_steps:
  - collect_issue_context
  - classify_impact_and_type
  - estimate_complexity
  - recommend_owner_or_skill_area
  - produce_action_items
output_contract: IssueAnalysisOutput
safety_level: read_only
---

# Issue 分诊

当用户想理解、分类、排序、分配或规划某个 GitHub Issue 时，使用这个技能。

## 工作流

1. 收集最新同步的 Issue 上下文，并在有帮助时读取相关仓库记忆。
2. 根据明确的 Issue 证据判断分类、优先级和复杂度。
3. 只有当团队上下文或 GitHub 已分配人员给出可选人选时，才推荐现有团队成员。
4. 如果复现步骤、验收标准、影响范围或团队成员上下文不足，明确输出需要澄清的问题，不要硬猜。
5. 返回维护者可以验证的行动项，并说明每个核心判断的 WHY。

## 边界

这个技能只读运行，不能直接给 Issue 打标签、关闭 Issue、发表评论或分配负责人。
