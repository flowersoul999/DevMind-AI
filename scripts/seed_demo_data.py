"""DevMind AI module."""

from datetime import datetime, timezone

from app.db.models import Issue, PRFile, PRReviewComment, PullRequest, Repository, WorkflowRun
from app.db.session import Base, SessionLocal, engine
from app.services.rag.indexing import index_repository_documents


def main() -> None:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        repo = db.query(Repository).filter(Repository.full_name == "course-demo/devflow-sample-api").one_or_none()
        if repo is None:
            repo = Repository(
                owner="course-demo",
                name="devflow-sample-api",
                full_name="course-demo/devflow-sample-api",
                description="Demo repository for DevMind AI course",
                default_branch="main",
                github_id=10001,
            )
            db.add(repo)
            db.commit()
            db.refresh(repo)

        if db.query(Issue).filter(Issue.repo_id == repo.id).count() == 0:
            issues = [
                Issue(repo_id=repo.id, github_issue_id=101, number=1, title="Login API returns 500 when token expires", body="JWT expires and backend returns 500 instead of 401.", state="open", labels=["bug", "auth"], author="alice", assignees=[], created_at=datetime.now(timezone.utc)),
                Issue(repo_id=repo.id, github_issue_id=102, number=2, title="Add GitHub workflow sync", body="Sync recent workflow runs and expose failed CI runs in dashboard.", state="open", labels=["feature", "ci"], author="bob", assignees=["backend"], created_at=datetime.now(timezone.utc)),
                Issue(repo_id=repo.id, github_issue_id=103, number=3, title="Document local demo mode", body="README should explain how to run without GitHub token.", state="closed", labels=["documentation"], author="carol", assignees=[], created_at=datetime.now(timezone.utc)),
            ]
            db.add_all(issues)
            db.commit()

        if db.query(PullRequest).filter(PullRequest.repo_id == repo.id).count() == 0:
            pr = PullRequest(repo_id=repo.id, github_pr_id=201, number=8, title="Refactor GitHub sync service", body="Split GitHub API calls into issue, PR and actions modules.", state="open", author="dave", base_branch="main", head_branch="refactor/github-sync", created_at=datetime.now(timezone.utc))
            db.add(pr)
            db.commit()
            db.refresh(pr)
            db.add_all([
                PRFile(pr_id=pr.id, filename="backend/app/services/github/client.py", status="modified", additions=42, deletions=8, patch="+ async def get_repo(...)\n+ response.raise_for_status()"),
                PRFile(pr_id=pr.id, filename="backend/app/api/routes/repos.py", status="modified", additions=55, deletions=12, patch="+ @router.post('/connect')\n+ async def connect_repo(...)")
            ])
            db.add_all([
                PRReviewComment(pr_id=pr.id, github_comment_id=401, body="Please verify token handling is not logged.", path="backend/app/api/routes/repos.py", line=24, author="reviewer-a", created_at=datetime.now(timezone.utc)),
                PRReviewComment(pr_id=pr.id, github_comment_id=402, body="Consider splitting GitHub sync into smaller retriable steps.", path="backend/app/services/repos_sync.py", line=57, author="reviewer-b", created_at=datetime.now(timezone.utc)),
            ])

        if db.query(WorkflowRun).filter(WorkflowRun.repo_id == repo.id).count() == 0:
            db.add_all([
                WorkflowRun(
                    repo_id=repo.id,
                    github_run_id=301,
                    name="backend-test",
                    status="completed",
                    conclusion="failure",
                    html_url="https://github.com/course-demo/devflow-sample-api/actions/runs/301",
                    jobs=[
                        {
                            "id": 9001,
                            "name": "pytest",
                            "status": "completed",
                            "conclusion": "failure",
                            "steps": [{"name": "Run backend tests", "status": "completed", "conclusion": "failure", "number": 4}],
                        }
                    ],
                    logs_text="pytest backend/app/tests/test_github_sync.py::test_sync_issues FAILED AssertionError: expected 3 issues, got 2",
                    created_at=datetime.now(timezone.utc),
                ),
                WorkflowRun(repo_id=repo.id, github_run_id=302, name="frontend-lint", status="completed", conclusion="success", html_url="https://github.com/course-demo/devflow-sample-api/actions/runs/302", logs_text="", created_at=datetime.now(timezone.utc))
            ])

        db.flush()
        index_repository_documents(db, repo.id)
        db.commit()
        print(f"Seeded demo data for repo {repo.full_name} ({repo.id})")
    finally:
        db.close()


if __name__ == "__main__":
    main()
