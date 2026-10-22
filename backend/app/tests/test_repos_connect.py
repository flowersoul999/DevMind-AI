"""DevMind AI module."""

# pytest。
import pytest
# fastapi 的 HTTPException（连接仓库时目录冲突会抛这个）。
from fastapi import HTTPException
# pydantic 的 ValidationError（请求参数不合法会抛这个）。
from pydantic import ValidationError

# 导入真实的仓库连接相关函数（解析 git 远程地址、推断平台、从父目录算克隆目标）和请求模型。
from app.api.routes.repos import _clone_target_from_parent, _infer_provider, _parse_git_remote_url
from app.schemas.repos import RepoConnectRequest


# @pytest.mark.parametrize：参数化测试——同一段测试逻辑，用多组输入自动跑多遍。
# 这里三组分别是：https 形式、git@ ssh 形式、ssh:// 形式 的远程地址，期望解析出相同的 owner/repo/host。
@pytest.mark.parametrize(
    ("remote_url", "owner", "repo", "host"),
    [
        ("https://github.com/openai/codex.git", "openai", "codex", "github.com"),
        ("git@github.com:openai/codex.git", "openai", "codex", "github.com"),
        ("ssh://git@github.example.com/platform/devflow.git", "platform", "devflow", "github.example.com"),
    ],
)
def test_parse_git_remote_url(remote_url: str, owner: str, repo: str, host: str) -> None:
    # 调用真实解析函数。
    parsed = _parse_git_remote_url(remote_url)

    # 断言：解析出的 owner / repo / host 都符合预期（无论 https 还是 ssh 都能正确归一化）。
    assert parsed.owner == owner
    assert parsed.repo == repo
    assert parsed.host == host


def test_infer_provider_for_custom_remote_host() -> None:
    # 推断平台类型：host 是 github.example.com（自托管）时归为 github_compatible（兼容 github 的协议）。
    assert _infer_provider("github", "github.example.com") == "github_compatible"
    # host 是官方 github.com 时就是 github。
    assert _infer_provider("github", "github.com") == "github"


def test_clone_target_from_parent_rejects_occupied_directory(tmp_path) -> None:
    # 造一个已被占用的目标目录（里面有 README.md）。
    target = tmp_path / "codex"
    target.mkdir()
    (target / "README.md").write_text("# occupied\n", encoding="utf-8")

    # 断言：当目标目录非空时，算克隆目标会抛 HTTPException（不能覆盖用户已有文件）。
    with pytest.raises(HTTPException):
        _clone_target_from_parent(str(tmp_path), "codex")


def test_repo_connect_request_uses_a_small_bounded_initial_sync() -> None:
    # 构造一个合法的连接请求（只给 owner / repo）。
    payload = RepoConnectRequest(owner="openai", repo="openai-quickstart-python")

    # 断言：默认初始同步上限是 5（有界，不会一次拉爆全量历史）。
    assert payload.initial_sync_limit == 5
    # 断言：默认开启 issue / PR / workflow_runs 的同步。
    assert payload.sync_issues is True
    assert payload.sync_pull_requests is True
    assert payload.sync_workflow_runs is True

    # 断言：initial_sync_limit=0 是非法的（下限校验生效），pydantic 会抛 ValidationError。
    with pytest.raises(ValidationError):
        RepoConnectRequest(owner="openai", repo="openai-quickstart-python", initial_sync_limit=0)
