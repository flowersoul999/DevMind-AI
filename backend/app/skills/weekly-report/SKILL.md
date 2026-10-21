---
name: weekly-report
title: 研发周报
version: 0.1.0
description: 根据指定范围内的 Issue、Pull Request、CI 运行和趋势上下文生成仓库研发周报。
category: reporting
entrypoint: report_agent.generate_weekly_report
tools:
  - report_agent.generate_weekly_report
input_modes:
  - repository_activity_window
  - report_request_from_chat
triggers:
  - 周报
  - weekly report
  - 工程摘要
  - 活动摘要
  - 项目健康
  - 仓库报告
workflow_steps:
  - collect_activity_window
  - summarize_issue_and_pr_flow
  - summarize_ci_health
  - identify_risks_and_next_actions
  - persist_report_to_archive
output_contract: WeeklyReportResponse
safety_level: read_only
---

# 研发周报

当用户请求周报、工程摘要、活动摘要或项目健康概览时，使用这个技能。

## 工作流

1. 将 Issue、Pull Request 和 workflow run 限定在用户请求的时间窗口内。
2. 汇总已完成工作、未关闭风险和 CI 健康状态。
3. 生成可用于团队同步的 Markdown 报告。
4. 将报告持久化到项目归档，但不把派生周报作为 RAG 的事实来源。

## 边界

这个技能只读取指定时间窗口内的结构化事实并生成内部报告；向外发送报告需要先经过安全技能。
