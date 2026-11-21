# @feature: FP-0.2.五 资源治理 | @ci: python-coverage
# @ci: python-coverage
"""_workspace_git_ops（Git 操作 Mixin）契约测试。

覆盖：_run_git 错误映射（瞬态失败 rc=-1 与 git 自身失败区分）、合并锁、
index.lock 清理、主分支动态检测（不硬编码 main/master）、分支守卫
（绝不 checkout）、init+首提（含 corrupt .git 重建与瞬态失败失败闭合）、
脏检测提交（tracked-only 变体）、auto-save 数据保护中断、worktree
prune 修复重试、worktree 依赖链接、场景检测 skip 目录语义。

关键路径走真实 git（临时仓库），mock 仅限时钟类外部故障注入
（subprocess 超时/异常）与宿主协作方法。
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_mod() -> Any:
    mod_name = "isolation_workspace_git_ops_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "_workspace_git_ops.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_MOD = _load_mod()


class _GitOpsHost(_MOD._GitOpsMixin):  # type: ignore[name-defined]  # 动态加载模块的属性作基类，mypy 静态不可解析
    """满足 Mixin 宿主属性约定的最小宿主。"""

    def __init__(self, base_path: Path, config: dict[str, Any] | None = None) -> None:
        self._config = config if config is not None else {}
        self._base_path = Path(base_path)
        self._main_branch = ""
        self._merge_locks: dict[str, threading.Lock] = {}
        self._global_lock = threading.Lock()


def _git(args: list[str], cwd: Path) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r.stdout.strip()


def _init_repo(path: Path, branch: str = "main") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "--initial-branch", branch], path)
    _git(["config", "user.email", "t@t.local"], path)
    _git(["config", "user.name", "t"], path)
    (path / "README.md").write_text("hello", encoding="utf-8")
    _git(["add", "-A"], path)
    _git(["commit", "-m", "init"], path)
    return path


@pytest.fixture
def host(tmp_path: Path) -> _GitOpsHost:
    # 注入 workspace.root（测试直构走注入分支，不触 isolation.workspace 懒加载）
    return _GitOpsHost(tmp_path, {"workspace": {"root": str(tmp_path)}})


# ──────────────────────────────────────────────
# _safe_ws_name
# ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("project", "expected_prefix"),
    [
        ("My Project", "My_Project__wt_"),
        ('a<b>:"c|d?*e', "a_b_c_d_e__wt_"),
        ("  ..__", "ws__wt_"),  # 清洗后为空 → 回退 ws
        ("x" * 300, "x" * 100 + "__wt_"),
    ],
)
def test_safe_ws_name_sanitization(project: str, expected_prefix: str) -> None:
    name = _MOD._safe_ws_name(project, "task-id-12345678")
    assert name.startswith(expected_prefix)
    assert name.endswith("task-id-")  # task_id[:8]


def test_safe_ws_name_truncation_strips_trailing_dots() -> None:
    name = _MOD._safe_ws_name("x." * 80, "task-id-12345678")
    assert len(name.split("__wt_")[0]) <= 100
    assert not name.split("__wt_")[0].endswith(".")


# ──────────────────────────────────────────────
# _run_git 错误映射
# ──────────────────────────────────────────────


class TestRunGit:
    def test_success_returns_stdout(self, host: _GitOpsHost, tmp_path: Path) -> None:
        rc, out, err = host._run_git("--version", cwd=tmp_path)
        assert rc == 0 and "git version" in out and err == ""

    def test_git_failure_carries_stderr(self, host: _GitOpsHost, tmp_path: Path) -> None:
        rc, out, err = host._run_git("rev-parse", "HEAD", cwd=tmp_path)  # 非 git 目录
        assert rc != 0 and rc > 0 and out == ""

    def test_timeout_maps_to_transient_minus_one(
        self, host: _GitOpsHost, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*a: Any, **kw: Any) -> None:
            raise subprocess.TimeoutExpired(cmd="git", timeout=1)

        monkeypatch.setattr(_MOD.subprocess, "run", _boom)
        rc, out, err = host._run_git("status", cwd=tmp_path)
        assert rc == -1 and "超时" in err

    def test_missing_git_maps_to_minus_one(
        self, host: _GitOpsHost, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*a: Any, **kw: Any) -> None:
            raise FileNotFoundError("git")

        monkeypatch.setattr(_MOD.subprocess, "run", _boom)
        rc, _, err = host._run_git("status", cwd=tmp_path)
        assert rc == -1 and "未找到 git" in err

    def test_invalid_cwd_maps_to_minus_one(self, host: _GitOpsHost, tmp_path: Path) -> None:
        rc, _, err = host._run_git("status", cwd=tmp_path / "no-such-dir")
        assert rc == -1 and "无效或不存在" in err


# ──────────────────────────────────────────────
# 合并锁 / index.lock
# ──────────────────────────────────────────────


def test_merge_lock_is_per_project_singleton(host: _GitOpsHost) -> None:
    a1 = host._get_merge_lock("/proj/a")
    a2 = host._get_merge_lock("/proj/a")
    b = host._get_merge_lock("/proj/b")
    assert a1 is a2 and a1 is not b


def test_remove_index_lock(host: _GitOpsHost, tmp_path: Path) -> None:
    assert host._remove_index_lock(tmp_path) is False  # 无锁
    lock = tmp_path / ".git" / "index.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("pid", encoding="utf-8")  # index.lock 是文件而非目录
    # 活锁（新鲜）：并发 git 正在持有，必须保留
    assert host._remove_index_lock(tmp_path) is False
    assert lock.exists()
    # 陈旧锁（mtime 拨回 1h）才删除
    old = time.time() - 3600
    os.utime(lock, (old, old))
    assert host._remove_index_lock(tmp_path) is True
    assert not lock.exists()


def test_run_git_injects_no_maintenance_env(
    host: _GitOpsHost, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """所有 git 调用统一 gc.auto=0 / maintenance.auto=false（防后台 repack）。"""
    captured: dict[str, Any] = {}

    def _fake_run(args: Any, **kw: Any) -> Any:
        captured["env"] = kw.get("env")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(_MOD.subprocess, "run", _fake_run)
    host._run_git("status", cwd=tmp_path)
    env = captured["env"]
    assert env is not None
    pairs = {
        env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
        for i in range(int(env["GIT_CONFIG_COUNT"]))
    }
    assert pairs["gc.auto"] == "0"
    assert pairs["maintenance.auto"] == "false"


def test_git_env_tolerates_invalid_config_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """宿主环境里损坏的 GIT_CONFIG_COUNT 不得中断注入（按 0 起编）。"""
    monkeypatch.setenv("GIT_CONFIG_COUNT", "not-a-number")
    env = _MOD._git_env()
    assert env["GIT_CONFIG_COUNT"] == "2"
    assert env["GIT_CONFIG_KEY_0"] == "gc.auto"


def test_ws_config_returns_empty_for_non_dict_host_config(tmp_path: Path) -> None:
    host = _GitOpsHost(tmp_path)
    host._config = "broken"  # type: ignore[assignment]
    assert host._ws_config() == {}


def test_remove_index_lock_stat_failure_keeps_lock(
    host: _GitOpsHost, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """stat 失败（权限/瞬态 IO）不得删锁——无法判定陈旧即按活锁保留。"""
    lock = tmp_path / ".git" / "index.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("pid", encoding="utf-8")
    real_stat = Path.stat

    def _failing_stat(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path.name == "index.lock":
            raise PermissionError("denied")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", _failing_stat)
    assert host._remove_index_lock(tmp_path) is False
    assert os.path.exists(lock)


def test_remove_index_lock_invalid_stale_config_falls_back(tmp_path: Path) -> None:
    """index_lock_stale_seconds 非法值按缺省阈值处理（陈旧锁仍可清）。"""
    host = _GitOpsHost(tmp_path, {"workspace": {"index_lock_stale_seconds": "bad"}})
    lock = tmp_path / ".git" / "index.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("pid", encoding="utf-8")
    old = time.time() - 3600
    os.utime(lock, (old, old))
    assert host._remove_index_lock(tmp_path) is True


# ──────────────────────────────────────────────
# 主分支检测与分支守卫
# ──────────────────────────────────────────────


class TestMainBranch:
    def test_detect_main_and_master(self, tmp_path: Path) -> None:
        host_main = _GitOpsHost(tmp_path)
        assert host_main._resolve_main_branch(_init_repo(tmp_path / "r1", "main")) == "main"
        host_master = _GitOpsHost(tmp_path)
        assert host_master._resolve_main_branch(_init_repo(tmp_path / "r2", "master")) == "master"

    def test_feature_branch_falls_back_to_master(self, tmp_path: Path) -> None:
        """HEAD 在 feature 分支且无 main：先试 verify main 失败 → 回退 master 名。"""
        repo = _init_repo(tmp_path / "r3", "master")
        _git(["checkout", "-b", "feature"], repo)
        host = _GitOpsHost(tmp_path)
        assert host._resolve_main_branch(repo) == "master"

    def test_assert_on_branch_never_checks_out(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "r4", "main")
        host = _GitOpsHost(tmp_path)
        assert host._assert_on_branch("main", repo) is True
        assert host._assert_on_branch("other", repo) is False
        assert _git(["rev-parse", "--abbrev-ref", "HEAD"], repo) == "main"  # 分支未被切换

    def test_record_main_branch_on_feature_warns(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "r5", "main")
        _git(["checkout", "-b", "feature"], repo)
        host = _GitOpsHost(tmp_path / "r5")
        host._base_path = repo
        host._record_main_branch()
        assert host._main_branch == "feature"

    def test_guard_root_branch(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "r6", "main")
        host = _GitOpsHost(repo)
        host._main_branch = "main"
        assert host._guard_root_branch(repo) is True  # 匹配
        _git(["checkout", "-b", "feature"], repo)
        assert host._guard_root_branch(repo) is False  # 根目录分支被外部切换 → 拒绝
        assert host._guard_root_branch(tmp_path / "elsewhere") is True  # 非根目录放行
        host._main_branch = ""
        assert host._guard_root_branch(repo) is True  # 未记录 → 放行


# ──────────────────────────────────────────────
# init + 首提
# ──────────────────────────────────────────────


class TestInitAndInitialCommit:
    def test_init_flag_fallback_to_plain_init_and_checkout(
        self, host: _GitOpsHost, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--initial-branch 不被支持（老 git）→ 裸 init + checkout -b main 降级。"""
        ws = tmp_path / "ws_old_git"
        ws.mkdir()
        real_run = _MOD.subprocess.run

        def _old_git(args: Any, **kw: Any) -> Any:
            args = list(args)
            if args[:2] == ["git", "init"] and "--initial-branch=main" in args:
                # 模拟老 git：未知选项 → 退出码 128
                r = subprocess.CompletedProcess(args, returncode=128, stdout="", stderr="error: unknown option `initial-branch=main'")
                return r
            return real_run(args, **kw)

        monkeypatch.setattr(_MOD.subprocess, "run", _old_git)
        assert host._git_init_and_initial_commit(ws, "legacy") is True
        assert (ws / ".git").exists()
        assert _git(["rev-parse", "--abbrev-ref", "HEAD"], ws) == "main"

    def test_workspace_root_injection_branches(self, tmp_path: Path) -> None:
        # 绝对路径（盘符形态）原样返回（D:/ 形态在两类平台都命中 WIN_ABS_PATH）
        host_abs = _GitOpsHost(tmp_path, {"workspace": {"root": "D:/wsroot"}})
        assert host_abs._get_workspace_root() == Path("D:/wsroot")
        # 相对路径基于 base_path（项目根），不拼 cwd
        host_rel = _GitOpsHost(tmp_path, {"workspace": {"root": ".ai_workspaces"}})
        assert host_rel._get_workspace_root() == (tmp_path / ".ai_workspaces").resolve()

    def test_fresh_dir_initializes_commits_and_gitignore(self, host: _GitOpsHost, tmp_path: Path) -> None:
        ws = tmp_path / "ws1"
        ws.mkdir()
        (ws / "a.txt").write_text("data", encoding="utf-8")
        assert host._git_init_and_initial_commit(ws, "init msg") is True
        assert (ws / ".git").exists()
        assert (ws / ".gitignore").exists()  # 缺失时生成最小保护版本
        assert ".task_runtime/" in (ws / ".gitignore").read_text(encoding="utf-8")
        assert _git(["rev-parse", "--abbrev-ref", "HEAD"], ws) == "main"
        assert _git(["log", "--oneline"], ws)  # 有提交

    def test_existing_repo_with_head_skips_reinit(self, host: _GitOpsHost, tmp_path: Path) -> None:
        ws = _init_repo(tmp_path / "ws2")
        before = _git(["rev-parse", "HEAD"], ws)
        assert host._git_init_and_initial_commit(ws, "second") is True
        after = _git(["rev-parse", "HEAD"], ws)
        assert after != before  # 追加了新提交

    def test_empty_git_dir_is_rebuilt(self, host: _GitOpsHost, tmp_path: Path) -> None:
        ws = tmp_path / "ws3"
        ws.mkdir()
        (ws / ".git").mkdir()  # 存在但空（无 HEAD）→ 视为损坏重建
        (ws / "f.txt").write_text("x", encoding="utf-8")
        assert host._git_init_and_initial_commit(ws, "re-init") is True
        assert (ws / ".git" / "HEAD").exists()

    def test_transient_revparse_failure_preserves_git_dir(
        self, host: _GitOpsHost, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """瞬态失败（rc=-1）绝不能删 .git：失败闭合返回 False。"""
        ws = tmp_path / "ws4"
        _init_repo(ws)
        marker = ws / ".git" / "HEAD"
        sentinel = ws / ".git" / "precious"
        sentinel.write_text("keep", encoding="utf-8")

        real_run = _MOD.subprocess.run

        def _flaky(args: Any, **kw: Any) -> Any:
            if args[:2] == ["git", "rev-parse"]:
                raise subprocess.TimeoutExpired(cmd="git", timeout=1)
            return real_run(args, **kw)

        monkeypatch.setattr(_MOD.subprocess, "run", _flaky)
        assert host._git_init_and_initial_commit(ws, "x") is False
        assert marker.exists() and sentinel.exists()  # .git 原封


# ──────────────────────────────────────────────
# 脏检测提交与 auto-save 保护
# ──────────────────────────────────────────────


class TestDirtyCommit:
    def test_clean_repo_returns_none(self, host: _GitOpsHost, tmp_path: Path) -> None:
        ws = _init_repo(tmp_path / "ws5")
        assert host._git_add_commit_if_dirty(ws, "m") is None

    def test_tracked_change_commits_and_returns_hash(self, host: _GitOpsHost, tmp_path: Path) -> None:
        ws = _init_repo(tmp_path / "ws6")
        (ws / "README.md").write_text("changed", encoding="utf-8")
        h = host._git_add_commit_if_dirty(ws, "modify")
        assert h and len(h) == 40

    def test_dirty_commit_ignores_untracked_artifacts(self, host: _GitOpsHost, tmp_path: Path) -> None:
        """auto-save 只提交 tracked diff：untracked 构建产物不得入库。"""
        ws = _init_repo(tmp_path / "ws6b")
        (ws / "artifact.bin").write_bytes(b"x" * 1024)
        assert host._git_add_commit_if_dirty(ws, "only-untracked") is None
        assert "artifact.bin" not in _git(["ls-files"], ws)

        (ws / "README.md").write_text("changed", encoding="utf-8")
        h = host._git_add_commit_if_dirty(ws, "tracked-only")
        assert h is not None
        assert len(h) == 40
        assert "artifact.bin" not in _git(["ls-files"], ws)
        assert (ws / "artifact.bin").exists()  # 未跟踪文件原样保留

    def test_tracked_only_commit_ignores_untracked(self, host: _GitOpsHost, tmp_path: Path) -> None:
        ws = _init_repo(tmp_path / "ws7")
        (ws / "README.md").write_text("changed", encoding="utf-8")
        (ws / "new_untracked.txt").write_text("junk", encoding="utf-8")
        h = host._git_add_tracked_and_commit(ws, "tracked only")
        assert h and len(h) == 40
        assert (ws / "new_untracked.txt").exists()
        assert _git(["status", "--porcelain"], ws).strip() != ""  # untracked 仍未入库

    def test_autosave_aborts_when_tracked_changes_remain(
        self, host: _GitOpsHost, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """auto-save 提交失败（此处以 no-op 模拟）+ 残留 tracked 脏 → RuntimeError 中断。"""
        ws = _init_repo(tmp_path / "ws8")
        (ws / "README.md").write_text("dirty", encoding="utf-8")
        monkeypatch.setattr(host, "_git_add_commit_if_dirty", lambda cwd, msg: None)
        with pytest.raises(RuntimeError, match="auto-save 失败"):
            host._autosave_before_worktree(ws, "m", "task-1")

    def test_autosave_passes_on_clean_worktree(self, host: _GitOpsHost, tmp_path: Path) -> None:
        ws = _init_repo(tmp_path / "ws9")
        host._autosave_before_worktree(ws, "m", "task-1")  # 不抛即通过
        (ws / "untracked.txt").write_text("x", encoding="utf-8")
        host._autosave_before_worktree(ws, "m", "task-1")  # untracked 不阻塞


# ──────────────────────────────────────────────
# worktree 创建修复与依赖链接
# ──────────────────────────────────────────────


class TestWorktree:
    def test_fresh_worktree_created_with_branch(self, host: _GitOpsHost, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "r")
        ws_dir = tmp_path / "wt1"
        host._worktree_add_with_repair(repo, "task/1", ws_dir, "task-1")
        assert (ws_dir / "README.md").exists()
        assert _git(["rev-parse", "--abbrev-ref", "HEAD"], ws_dir) == "task/1"

    def test_conflict_repaired_via_prune_and_retry(self, host: _GitOpsHost, tmp_path: Path) -> None:
        """裸删目录留下 stale worktree 注册 → add 失败 → prune + branch -D + 重试成功。

        prune 只清失效注册：有效 worktree 不受影响，故必须模拟生产中
        「清理不彻底（目录被裸删、注册残留）」的真实冲突形态。
        """
        repo = _init_repo(tmp_path / "r2")
        ws_dir = tmp_path / "wt2"
        host._worktree_add_with_repair(repo, "task/2", ws_dir, "task-2")
        shutil.rmtree(ws_dir)  # 裸删：不 git worktree remove，注册残留
        host._worktree_add_with_repair(repo, "task/2", ws_dir, "task-2")
        assert (ws_dir / "README.md").exists()
        assert _git(["rev-parse", "--abbrev-ref", "HEAD"], ws_dir) == "task/2"

    def test_transient_worktree_failure_fails_closed(
        self, host: _GitOpsHost, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """瞬态失败（rc=-1）必须抛错保留现场，不得走 branch -D 清理。"""
        repo = _init_repo(tmp_path / "r3")
        real_run = _MOD.subprocess.run

        def _flaky(args: Any, **kw: Any) -> Any:
            if args[:2] == ["git", "worktree"]:
                raise subprocess.TimeoutExpired(cmd="git", timeout=1)
            return real_run(args, **kw)

        monkeypatch.setattr(_MOD.subprocess, "run", _flaky)
        with pytest.raises(RuntimeError, match="瞬态失败"):
            host._worktree_add_with_repair(repo, "task/3", tmp_path / "wt3", "task-3")

    def test_worktree_timeout_reclaims_completed_worktree(
        self, host: _GitOpsHost, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """add 超时但工作树已完整落地 → 认领并解锁，不判失败、不删分支。"""
        repo = _init_repo(tmp_path / "r3b")
        ws_dir = tmp_path / "wt3b"
        _git(["worktree", "add", "-b", "task/3b", str(ws_dir), "HEAD"], repo)
        real_run = _MOD.subprocess.run

        def _timeout_add(args: Any, **kw: Any) -> Any:
            if args[:3] == ["git", "worktree", "add"]:
                raise subprocess.TimeoutExpired(cmd="git", timeout=1)
            return real_run(args, **kw)

        with monkeypatch.context() as mp:
            mp.setattr(_MOD.subprocess, "run", _timeout_add)
            host._worktree_add_with_repair(repo, "task/3b", ws_dir, "task-3b")
        assert (ws_dir / "README.md").exists()
        assert _git(["rev-parse", "--abbrev-ref", "HEAD"], ws_dir) == "task/3b"
        assert not (repo / ".git" / "worktrees" / ws_dir.name / "locked").exists()

    def test_worktree_timeout_incomplete_fails_closed(
        self, host: _GitOpsHost, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """add 超时且现场不完整（半截目录）→ 仍 fail-closed 抛错。"""
        repo = _init_repo(tmp_path / "r3c")
        ws_dir = tmp_path / "wt3c"
        ws_dir.mkdir()
        (ws_dir / ".git").write_text("gitdir: nowhere\n", encoding="utf-8")
        real_run = _MOD.subprocess.run

        def _timeout_worktree(args: Any, **kw: Any) -> Any:
            if args[:3] == ["git", "worktree", "add"]:
                raise subprocess.TimeoutExpired(cmd="git", timeout=1)
            return real_run(args, **kw)

        with monkeypatch.context() as mp:
            mp.setattr(_MOD.subprocess, "run", _timeout_worktree)
            with pytest.raises(RuntimeError, match="瞬态失败"):
                host._worktree_add_with_repair(repo, "task/3c", ws_dir, "task-3c")

    def test_worktree_add_timeout_config_override(self, tmp_path: Path) -> None:
        host = _GitOpsHost(tmp_path, {"workspace": {"worktree_add_timeout_seconds": 240}})
        assert host._worktree_add_timeout() == 240
        bad = _GitOpsHost(tmp_path, {"workspace": {"worktree_add_timeout_seconds": "bad"}})
        assert bad._worktree_add_timeout() == _MOD._DEFAULT_WORKTREE_ADD_TIMEOUT

    def test_link_dependencies_creates_dir_and_file_links(self, host: _GitOpsHost, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "r4")
        ws_dir = tmp_path / "wt4"
        host._worktree_add_with_repair(repo, "task/4", ws_dir, "task-4")
        (repo / "node_modules").mkdir()
        (repo / "node_modules" / "lib").write_text("x", encoding="utf-8")
        (repo / "secret.txt").write_text("s", encoding="utf-8")
        host._config = {"workspace": {"worktree_link_patterns": ["node_modules", "secret.txt", "missing/"]}}
        host._link_worktree_dependencies(ws_dir, repo)
        assert (ws_dir / "node_modules" / "lib").exists()  # 目录链接（junction/symlink）
        if os.name != "nt":
            assert (ws_dir / "secret.txt").exists()  # 文件符号链接（POSIX）
        # Windows 文件 mklink 需管理员/开发者模式，无权限时按设计仅告警不阻断
        # （失败吞掉属契约内降级），继续处理后续 pattern：
        assert not (ws_dir / "missing").exists()  # 源不存在 → 跳过
        # 幂等：目标已存在 → 不重复创建不报错
        host._link_worktree_dependencies(ws_dir, repo)


class TestWorktreeReclaimHelpers:
    """超时认领辅助面：注册表解析 / 解锁 / 未完成现场拒绝。"""

    def test_worktree_entries_empty_when_git_fails(self, host: _GitOpsHost, tmp_path: Path) -> None:
        assert host._worktree_entries(tmp_path / "not-a-repo") == []

    def test_find_entry_returns_none_when_target_unresolvable(
        self, host: _GitOpsHost, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        real_resolve = Path.resolve

        def _resolve(path: Path, *args: Any, **kwargs: Any) -> Path:
            if path == ws_dir:
                raise OSError("unresolvable")
            return real_resolve(path, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", _resolve)
        assert host._find_worktree_entry(tmp_path, ws_dir) is None

    def test_find_entry_skips_entries_without_path_or_unresolvable(
        self, host: _GitOpsHost, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        ghost = tmp_path / "ghost"

        def _entries(repo: Path) -> list[dict[str, str]]:
            return [{"bare": ""}, {"worktree": str(ghost)}]

        host._worktree_entries = _entries  # type: ignore[method-assign]
        real_resolve = Path.resolve

        def _resolve(path: Path, *args: Any, **kwargs: Any) -> Path:
            if path == ghost:
                raise OSError("unresolvable")
            return real_resolve(path, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", _resolve)
        assert host._find_worktree_entry(tmp_path, ws_dir) is None

    def test_unlock_worktree_git_success(self, host: _GitOpsHost, tmp_path: Path) -> None:
        calls: list[tuple[str, ...]] = []

        def _run_git(*args: str, **kwargs: Any) -> tuple[int, str, str]:
            calls.append(args)
            return 0, "", ""

        host._run_git = _run_git  # type: ignore[method-assign]
        host._unlock_worktree(tmp_path, tmp_path / "ws")
        assert calls
        assert calls[0][:2] == ("worktree", "unlock")

    def test_unlock_worktree_admin_locked_fallback(self, host: _GitOpsHost, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        locked = repo / ".git" / "worktrees" / ws_dir.name / "locked"
        locked.parent.mkdir(parents=True)
        locked.write_text("initializing", encoding="utf-8")

        def _run_git(*args: str, **kwargs: Any) -> tuple[int, str, str]:
            return 1, "", "unlock unsupported"

        host._run_git = _run_git  # type: ignore[method-assign]
        host._unlock_worktree(repo, ws_dir)
        assert not locked.exists()

    def test_reclaim_rejects_unresolvable_head(self, host: _GitOpsHost, tmp_path: Path) -> None:
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        (ws_dir / ".git").write_text("gitdir: x", encoding="utf-8")

        def _find(repo: Path, ws: Path) -> dict[str, str]:
            return {"branch": "refs/heads/task/x"}

        def _run_git(*args: str, **kwargs: Any) -> tuple[int, str, str]:
            return 1, "", ""

        host._find_worktree_entry = _find  # type: ignore[method-assign]
        host._run_git = _run_git  # type: ignore[method-assign]
        assert host._reclaim_completed_worktree(tmp_path, "task/x", ws_dir, "t-x") is False

    def test_reclaim_rejects_dirty_worktree(self, host: _GitOpsHost, tmp_path: Path) -> None:
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        (ws_dir / ".git").write_text("gitdir: x", encoding="utf-8")
        responses = iter([(0, "", ""), (0, " M src/a.py", "")])

        def _find(repo: Path, ws: Path) -> dict[str, str]:
            return {"branch": "refs/heads/task/y"}

        def _run_git(*args: str, **kwargs: Any) -> tuple[int, str, str]:
            return next(responses)

        host._find_worktree_entry = _find  # type: ignore[method-assign]
        host._run_git = _run_git  # type: ignore[method-assign]
        assert host._reclaim_completed_worktree(tmp_path, "task/y", ws_dir, "t-y") is False

    def test_reclaim_swallows_unexpected_exception(self, host: _GitOpsHost, tmp_path: Path) -> None:
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        (ws_dir / ".git").write_text("gitdir: x", encoding="utf-8")

        def _raise(repo: Path, ws: Path) -> dict[str, str]:
            raise RuntimeError("registry broken")

        host._find_worktree_entry = _raise  # type: ignore[method-assign]
        assert host._reclaim_completed_worktree(tmp_path, "task/z", ws_dir, "t-z") is False


# ──────────────────────────────────────────────
# 场景检测
# ──────────────────────────────────────────────


class TestDetectScenario:
    def test_empty_workspace_routes_to_task_dir(self, host: _GitOpsHost) -> None:
        host._config = {"workspace": {"root": str(host._base_path)}}
        scenario, path = host._detect_scenario("", {"task_id": "t-123"})
        assert scenario == "new_project"
        assert path == str(host._base_path / "t-123")

    def test_nonexistent_path_is_new_project(self, host: _GitOpsHost, tmp_path: Path) -> None:
        scenario, path = host._detect_scenario(str(tmp_path / "nope"), {"task_id": "t"})
        assert scenario == "new_project" and path.endswith("nope")

    def test_skip_dirs_do_not_count_as_existing(self, host: _GitOpsHost, tmp_path: Path) -> None:
        ws = tmp_path / "ws_only_meta"
        (ws / ".git").mkdir(parents=True)
        (ws / "data").mkdir()
        (ws / "__pycache__").mkdir()
        scenario, _ = host._detect_scenario(str(ws), {"task_id": "t"})
        assert scenario == "new_project"

    def test_real_file_makes_existing_project(self, host: _GitOpsHost, tmp_path: Path) -> None:
        ws = tmp_path / "ws_real"
        ws.mkdir()
        (ws / "main.py").write_text("print(1)", encoding="utf-8")
        scenario, _ = host._detect_scenario(str(ws), {"task_id": "t"})
        assert scenario == "existing_project"

    def test_nested_dir_content_counts_as_existing(self, host: _GitOpsHost, tmp_path: Path) -> None:
        ws = tmp_path / "ws_nested"
        (ws / "src").mkdir(parents=True)
        (ws / "src" / "m.py").write_text("x", encoding="utf-8")
        scenario, _ = host._detect_scenario(str(ws), {"task_id": "t"})
        assert scenario == "existing_project"


class TestEnsureGitignore:
    """任务运行时目录排除行：缺失播种 / 存量幂等追加 / 已含不重复。"""

    def test_missing_writes_minimal_version_with_runtime_dir(self, tmp_path: Path) -> None:
        host = _GitOpsHost(tmp_path, {})
        ws = tmp_path / "ws_a"
        ws.mkdir()
        host._ensure_gitignore(ws)
        text = (ws / ".gitignore").read_text(encoding="utf-8")
        assert text.startswith("data/\n")
        assert ".task_runtime/\n" in text

    def test_existing_without_line_appends_and_preserves_content(self, tmp_path: Path) -> None:
        host = _GitOpsHost(tmp_path, {})
        ws = tmp_path / "ws_b"
        ws.mkdir()
        (ws / ".gitignore").write_text("dist/\n", encoding="utf-8")
        host._ensure_gitignore(ws)
        text = (ws / ".gitignore").read_text(encoding="utf-8")
        assert text == "dist/\n.task_runtime/\n"

    def test_existing_with_line_is_idempotent(self, tmp_path: Path) -> None:
        host = _GitOpsHost(tmp_path, {})
        ws = tmp_path / "ws_c"
        ws.mkdir()
        (ws / ".gitignore").write_text("data/\n.task_runtime/\n", encoding="utf-8")
        host._ensure_gitignore(ws)
        assert (ws / ".gitignore").read_text(encoding="utf-8") == "data/\n.task_runtime/\n"
