"""DevMind AI module."""

# subprocess：标准库，用来在测试里模拟执行 git 命令（被 mock 用）。
import subprocess
# uuid：生成唯一 id。
import uuid
# SimpleNamespace：标准库里一个"随便挂属性的空对象"。我们用它来伪造一个仓库对象，而不必构造完整 ORM 模型。
from types import SimpleNamespace

# 导入配置（settings）和代码分析服务（code_analysis）。
from app.core.config import settings
from app.services import code_analysis


def test_sync_checkout_uses_cached_checkout_when_update_fails(tmp_path, monkeypatch) -> None:
    # tmp_path 是 pytest 提供的临时目录，测试结束自动清理，不会弄脏你的磁盘。
    # 把"仓库检出根目录"配置临时指向这个临时目录。
    monkeypatch.setattr(settings, "repo_checkout_dir", str(tmp_path))
    # 用 SimpleNamespace 伪造一个仓库对象：有 id、全名、克隆地址、默认分支。
    repo = SimpleNamespace(
        id=uuid.uuid4(),
        full_name="owner/repo",
        clone_url="https://github.com/owner/repo.git",
        default_branch="main",
    )
    # 算出这个仓库应该被检出的本地路径。
    checkout = code_analysis._repo_checkout_path(repo)
    # 手动在那个路径下建一个 .git 目录，假装"这个仓库之前已经克隆过、有缓存了"。
    (checkout / ".git").mkdir(parents=True)

    # 定义一个假 git：无论传什么参数，都抛 CalledProcessError（git 退出码 128 = 失败，模拟网络断了）。
    def fail_run(args, cwd=None):
        """fail_run."""
        """fail_run."""
        raise subprocess.CalledProcessError(128, ["git", *args], stderr="network failed")

    # 把真实的 _run_git 函数替换成这个"必定失败"的版本。
    monkeypatch.setattr(code_analysis, "_run_git", fail_run)

    # 断言：尽管更新（pull）失败了，_sync_checkout 仍然返回那个已有的 checkout 路径——即"用缓存兜底"。
    # 为什么：本地已经有代码就不该因为一次网络抖动就全盘失败，能读缓存就先读缓存。
    assert code_analysis._sync_checkout(repo, None) == checkout


def test_sync_checkout_uses_configured_local_repo_without_updating(tmp_path, monkeypatch) -> None:
    # 造一个本地仓库目录（模拟用户机器上已经存在的真实代码目录）。
    local_repo = tmp_path / "local-repo"
    local_repo.mkdir()
    # 伪造仓库对象，声明它是 local 模式、且有 local_path 指向这个本地目录。
    repo = SimpleNamespace(
        id=uuid.uuid4(),
        full_name="owner/repo",
        clone_url="https://github.com/owner/repo.git",
        default_branch="main",
        local_path=str(local_repo),
        checkout_mode="local",
    )

    # mock 掉"判断某路径是不是 git 工作区"的函数：只有当路径等于这个本地仓库时才返回 True。
    monkeypatch.setattr(code_analysis, "_is_git_work_tree", lambda path: path == local_repo.resolve())

    # 假 git：如果它被调用了，直接抛 AssertionError——因为我们预期"本地仓库根本不该去 fetch/checkout/pull"。
    def fail_run(args, cwd=None):
        raise AssertionError("local repository indexing should not fetch, checkout, or pull")

    monkeypatch.setattr(code_analysis, "_run_git", fail_run)

    # 断言：返回的是本地仓库的真实路径（说明直接用了本地代码，全程没碰网络）。
    assert code_analysis._sync_checkout(repo, None) == local_repo.resolve()


def test_sync_checkout_rejects_non_empty_configured_download_target(tmp_path, monkeypatch) -> None:
    # 造一个目标目录，里面已经有一个 README.md，说明"这个目录非空、被人占用了"。
    target = tmp_path / "repo"
    target.mkdir()
    (target / "README.md").write_text("# occupied\n", encoding="utf-8")
    # 伪造仓库，声明 checkout_mode=managed（由系统托管下载），并指定 local_path 为这个非空目录。
    repo = SimpleNamespace(
        id=uuid.uuid4(),
        full_name="owner/repo",
        clone_url="https://github.com/owner/repo.git",
        default_branch="main",
        local_path=str(target),
        checkout_mode="managed",
    )

    # mock 掉 shutil.which("git")，假装系统里能找到 git 可执行文件（让函数走到"要克隆"的分支）。
    monkeypatch.setattr(code_analysis.shutil, "which", lambda name: "git")
    # mock 掉"判断 git 工作区"：这个非空目录没有 .git，所以返回 False。
    monkeypatch.setattr(code_analysis, "_is_git_work_tree", lambda path: False)

    try:
        # 预期：对一个非空的目标目录去托管下载，应该被拒绝（不能覆盖用户已有文件）。
        code_analysis._sync_checkout(repo, None)
    except RuntimeError as exc:
        # 断言：报错信息提到 "already exists"（目标已存在）。
        assert "already exists" in str(exc)
    else:
        # 如果没抛异常，说明安全检查漏了——主动让测试失败。
        raise AssertionError("expected configured non-empty download target to be rejected")
