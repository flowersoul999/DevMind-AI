# Database Schema

核心表：users、repositories、issues、pull_requests、pr_files、pr_review_comments、workflow_runs、analysis_results、documents、agent_runs、eval_runs。

当前骨架模型位于 `backend/app/db/models.py`。

补充说明：

- `workflow_runs.jobs` 保存 GitHub Actions jobs 和 steps，用于定位失败 job。
- `documents.metadata` 保存标签、文件路径、状态和时间范围，用于 RAG 过滤。
- `eval_runs.result_json` 保存基础评测指标和 Markdown eval report。
