# DevMind AI API

启动后端后访问 `http://localhost:8000/docs` 查看交互式 OpenAPI。

## Core Endpoints

- `POST /api/repos/connect`
- `GET /api/repos`
- `POST /api/repos/{repo_id}/sync`
- `GET /api/repos/{repo_id}/sync-status`
- `GET /api/repos/{repo_id}/issues`
- `POST /api/issues/{issue_id}/analyze`
- `GET /api/issues/{issue_id}/similar`
- `GET /api/repos/{repo_id}/pull-requests`
- `POST /api/pull-requests/{pr_id}/analyze`
- `POST /api/pull-requests/{pr_id}/review-checklist`
- `GET /api/repos/{repo_id}/workflow-runs`
- `GET /api/workflow-runs/{run_id}/failed-jobs`
- `POST /api/workflow-runs/{run_id}/analyze`
- `POST /api/chat`
- `POST /api/search`
- `POST /api/reports/weekly`
- `POST /api/evals/run`
