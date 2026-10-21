---
name: pr-review
title: PR 审查
version: 0.1.0
description: 审查已同步的 Pull Request，输出摘要、关键变更、P1/P2/P3 分级发现、阻塞项、测试缺口和需要关注的文件。
category: github-analysis
entrypoint: pr_review_agent.analyze_pr
tools:
  - pr_review_agent.analyze_pr
  - workspace.read_file
  - workspace.search_code
  - rag.search_similar_documents
input_modes:
  - latest_synced_pr
  - pr_context_from_chat
triggers:
  - pr
  - pull request
  - 合并请求
  - 代码审查
  - review
  - 能不能合并
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

# PR 审查

当用户询问某个 PR 是否适合合并、变更了什么、有哪些风险或应该补哪些测试时，使用这个技能。

开始审查前，按需读取 `references/risk-checklist.md`，根据实际变更面选择检查项；没有相关证据时不要机械制造风险。

## 工作流

1. 收集 PR 标题、正文、变更文件、patch 和 Review 评论。
2. 用维护者能快速理解的语言总结实现变化。
3. 按 P1/P2/P3 输出 `review_findings`，每条都要有证据、需要动作和是否阻塞。
4. P1/P2 必须进入 `blocking_issues`，且不能给出「建议合入」。
5. 标出风险点和需要重点关注的文件。
6. 给出与变更面匹配的测试建议。

## 分级标准

- P1：会导致功能错误、安全问题、数据风险或失败 CI 阻塞，必须修复后再放行。
- P2：测试覆盖不足、重要边界遗漏、未处理 review comment、敏感文件缺少验证，必须补齐验证后再放行。
- P3：风格、命名、可维护性建议或非阻断改进，可以进入 backlog。

不能用「looks good」「没什么大问题」替代审查结论。没有 P1/P2 时，也要说明依据、保留的 P3 建议和最终人工确认 checklist。

## 边界

这个技能只生成审查建议，不能在 GitHub 上批准、要求修改、合并或发表评论。
