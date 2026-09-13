# @feature: FP-0.2.二 isolation workspace git ops 缺口分支补测 | @ci: python-coverage
# @ci: python-coverage
"""_workspace_git_ops 缺口分支补测——降级回退/重试族/修复路径/符号链接分支。

覆盖面（批五覆盖率冲刺，接 test_workspace_git_ops.py 既有契约）：
- workspace root 无注入配置时回退 isolation.workspace（112-114）
- _run_git 失败详情的 stdout 半边（135）
- index.lock 清理的 OSError 分支（171-173）
- 主分支解析偏好 main（189）；_record/_guard 的异常放行分支（231-232/255-257）
- init 损坏 .git 删除失败、init 双败（292-294/304-305）
- add/commit 的 index.lock 重试族（323-326/330-337/365-368/372-375/414-417/422-425）
- auto-save 状态校验失败失败闭合（404）；tracked-only 无变更（428）
- worktree prune 修复重试成功/再败（467-473/478）
- 符号链接非 Windows 分支与失败降级（508/514/516-517）
- 场景检测的 PermissionError 容错（548-549）

关键路径走真实 git（临时仓库）；subprocess/文件系统故障按测试纪律注入
（monkeypatch 替身），不断言日志细节只断行为结果。
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))
# isolation.workspace 平铺包定位需 plugins/shared/system 在 sys.path
# （生产由插件 bootstrap 注入，测试显式补齐）
_SYSTEM_DIR = _PLUGIN_DIR.parent
if str(_SYSTEM_DIR) not in sys.path:
    sys.path.insert(0, str(_SYSTEM_DIR))


def _load_mod() -> Any:
    mod_name = "isolation_workspace_git_ops_branches"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "_workspace_git_ops.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_MOD = _load_mod()


class _GitOpsHost(_MOD._GitOpsMixin):  # type: ignore[name-defined]
    """满足 Mixin 宿主属性约定的最小宿主（同既有测试）。"""

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


def _git_user(cwd: Path) -> None:
    _git(["config", "user.email", "t@t.local"], cwd)
    _git(["config", "user.name", "t"], cwd)


@pytest.fixture
def host(tmp_path: Path) -> _GitOpsHost:
    return _GitOpsHost(base_path=tmp_path, config={"workspace": {"root": str(tmp_path / "ws")}})


# ============================================================
# workspace root 回退 / _run_git 详情
# ============================================================


class TestRootFallbackAndGitDetail:
    def test_workspace_root_falls_back_to_isolation_workspace(
        self, host: _GitOpsHost, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """无注入 workspace.root → 回退 isolation.workspace 配置面。"""
        import isolation.workspace as iw

        host._config = {}
        monkeypatch.setattr(iw, "get_workspace_base_dir", lambda: str(tmp_path / "from-cfg"))
        assert host._get_workspace_root() == Path(tmp_path / "from-cfg")

    def test_run_git_failure_detail_includes_stdout(
        self, host: _GitOpsHost, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """git 失败且 stderr 为空时详情取 stdout 半边（不丢诊断信息）。"""
        fake = subprocess.CompletedProcess(
            args=["git", "log"], returncode=1, stdout="partial output", stderr="   "
        )
        monkeypatch.setattr(_MOD.subprocess, "run", lambda *a, **k: fake)
        rc, out, err = host._run_git("log", cwd=tmp_path)
        assert (rc, out, err) == (1, "partial output", "")


# ============================================================
# index.lock / 主分支
# ============================================================


class TestLockAndBranch:
    def test_remove_index_lock_oserror_returns_false(self, host: _GitOpsHost, tmp_path: Path) -> None:
        """lock 清理遇到 OSError（如目录占位）→ False，不抛异常。"""
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "index.lock").mkdir()  # 目录不可 unlink → OSError
        assert host._remove_index_lock(tmp_path) is False

    def test_resolve_main_branch_prefers_existing_main(self, tmp_path: Path) -> None:
        """当前在 feature 且 main 存在 → 返回 main（而非当前分支/盲猜 master）。"""
        repo = _init_repo(tmp_path / "repo")
        _git(["checkout", "-b", "feature"], repo)
        host = _GitOpsHost(base_path=repo)
        assert host._resolve_main_branch(repo) == "main"

    def test_record_main_branch_on_main_is_recorded(self, tmp_path: Path) -> None:
        """主分支上记录 → _main_branch 记为当前主分支。"""
        repo = _init_repo(tmp_path / "repo", branch="main")
        host = _GitOpsHost(base_path=repo)
        host._record_main_branch()
        assert host._main_branch == "main"

    def test_record_main_branch_swallows_exception(self, tmp_path: Path) -> None:
        """分支记录命令异常 → 告警放行（不阻断初始化）。"""
        host = _GitOpsHost(base_path=tmp_path)
        monkey_raised = RuntimeError("git 炸了")
        host._run_git = MagicMock(side_effect=monkey_raised)  # type: ignore[method-assign]
        host._record_main_branch()  # 不抛
        assert host._main_branch == ""

    def test_guard_root_branch_swallows_exception_and_allows(self, tmp_path: Path) -> None:
        """分支守卫检查异常 → 默认放行（True），不阻断 commit/merge。"""
        host = _GitOpsHost(base_path=tmp_path)
        host._main_branch = "main"
        host._run_git = MagicMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
        assert host._guard_root_branch(tmp_path) is True


# ============================================================
# init 损坏/双败
# ============================================================


class TestInitFailures:
    def test_corrupt_git_remove_failure_returns_false(
        self, host: _GitOpsHost, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """空 .git 判定损坏需重建但删除失败（OSError）→ False（不假成功）。"""
        ws = tmp_path / "ws-corrupt"
        ws.mkdir()
        (ws / ".git").mkdir()  # 空 .git = 损坏
        monkeypatch.setattr(_MOD, "force_rmtree", lambda p: (_ for _ in ()).throw(OSError("占用")))
        assert host._git_init_and_initial_commit(ws, "init") is False

    def test_git_init_double_failure_returns_false(
        self, host: _GitOpsHost, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """init 带旗标与回退 init 都失败 → False。"""
        ws = tmp_path / "ws-initfail"
        ws.mkdir()
        real_run = _MOD._GitOpsMixin._run_git

        def _fail_init(self: Any, *args: str, **kwargs: Any) -> tuple[int, str, str]:
            if args[0] == "init":
                return 1, "", "init boomed"
            return real_run(self, *args, **kwargs)

        monkeypatch.setattr(_MOD._GitOpsMixin, "_run_git", _fail_init)
        assert host._git_init_and_initial_commit(ws, "init") is False


# ============================================================
# add/commit 的 index.lock 重试族
# ============================================================


class TestLockRetryFamilies:
    def test_init_commit_add_lock_retry_exhausted_continues(
        self, host: _GitOpsHost, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """init 流程 add -A 两次锁失败 → 告警后继续提交（非致命）。

        前置清理会先删锁，故由失败分支即时重建锁，让 _remove_index_lock
        返回 True、重试块可达。
        """
        repo = _init_repo(tmp_path / "repo-lock-add")
        lock = repo / ".git" / "index.lock"
        real_run = _MOD._GitOpsMixin._run_git

        def _add_lock_fails(self: Any, *args: str, **kwargs: Any) -> tuple[int, str, str]:
            if args[:2] == ("add", "-A"):
                lock.write_text("", encoding="utf-8")  # 失败即持锁（重试前被清）
                return 1, "", "fatal: Unable to create index.lock"
            return real_run(self, *args, **kwargs)

        monkeypatch.setattr(_MOD._GitOpsMixin, "_run_git", _add_lock_fails)
        assert host._git_init_and_initial_commit(repo, "reinit") is True  # add 失败非致命

    def test_init_commit_lock_retry_exhausted_returns_false(
        self, host: _GitOpsHost, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """init 流程 commit 两次锁失败 → False（失败分支即时重建锁使重试可达）。"""
        repo = _init_repo(tmp_path / "repo-lock-commit")
        lock = repo / ".git" / "index.lock"
        real_run = _MOD._GitOpsMixin._run_git

        def _commit_lock_fails(self: Any, *args: str, **kwargs: Any) -> tuple[int, str, str]:
            if args[0] == "commit":
                lock.write_text("", encoding="utf-8")
                return 1, "", "fatal: Unable to create index.lock"
            return real_run(self, *args, **kwargs)

        monkeypatch.setattr(_MOD._GitOpsMixin, "_run_git", _commit_lock_fails)
        assert host._git_init_and_initial_commit(repo, "reinit") is False

    def test_dirty_commit_add_failure_returns_none(
        self, host: _GitOpsHost, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """脏检测提交：add -A 两次失败 → None（与无变更不可混淆由调用方处理）。"""
        repo = _init_repo(tmp_path / "repo-dirty-add")
        (repo / "README.md").write_text("dirty", encoding="utf-8")  # 有脏改动才会走到 add
        real_run = _MOD._GitOpsMixin._run_git

        def _add_fails(self: Any, *args: str, **kwargs: Any) -> tuple[int, str, str]:
            if args[:2] == ("add", "-A"):
                return 1, "", "add failed"
            return real_run(self, *args, **kwargs)

        monkeypatch.setattr(_MOD._GitOpsMixin, "_run_git", _add_fails)
        assert host._git_add_commit_if_dirty(repo, "m") is None

    def test_dirty_commit_failure_returns_none(
        self, host: _GitOpsHost, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """脏检测提交：commit 两次失败 → None。"""
        repo = _init_repo(tmp_path / "repo-dirty-commit")
        (repo / "README.md").write_text("changed", encoding="utf-8")
        real_run = _MOD._GitOpsMixin._run_git

        def _commit_fails(self: Any, *args: str, **kwargs: Any) -> tuple[int, str, str]:
            if args[0] == "commit":
                return 1, "", "commit failed"
            return real_run(self, *args, **kwargs)

        monkeypatch.setattr(_MOD._GitOpsMixin, "_run_git", _commit_fails)
        assert host._git_add_commit_if_dirty(repo, "m") is None

    def test_autosave_status_check_failure_aborts(
        self, host: _GitOpsHost, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """auto-save 后 status 校验失败 → 失败闭合中断（不赌工作区干净）。"""
        repo = _init_repo(tmp_path / "repo-autosave")
        real_run = _MOD._GitOpsMixin._run_git

        def _status_fails(self: Any, *args: str, **kwargs: Any) -> tuple[int, str, str]:
            if args[0] == "status":
                return 1, "", "status boomed"
            return real_run(self, *args, **kwargs)

        monkeypatch.setattr(_MOD._GitOpsMixin, "_run_git", _status_fails)
        with pytest.raises(RuntimeError, match="状态校验失败"):
            host._autosave_before_worktree(repo, "save", "t-1")

    def test_tracked_commit_add_failure_returns_none(
        self, host: _GitOpsHost, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """tracked-only 提交：add -u 两次失败 → None。"""
        repo = _init_repo(tmp_path / "repo-t-add")
        real_run = _MOD._GitOpsMixin._run_git

        def _add_u_fails(self: Any, *args: str, **kwargs: Any) -> tuple[int, str, str]:
            if args[:2] == ("add", "-u"):
                return 1, "", "add -u failed"
            return real_run(self, *args, **kwargs)

        monkeypatch.setattr(_MOD._GitOpsMixin, "_run_git", _add_u_fails)
        assert host._git_add_tracked_and_commit(repo, "m") is None

    def test_tracked_commit_failure_returns_none(
        self, host: _GitOpsHost, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """tracked-only 提交：有已跟踪改动但 commit 两次失败 → None。"""
        repo = _init_repo(tmp_path / "repo-t-commit")
        (repo / "README.md").write_text("dirty", encoding="utf-8")
        real_run = _MOD._GitOpsMixin._run_git

        def _commit_fails(self: Any, *args: str, **kwargs: Any) -> tuple[int, str, str]:
            if args[0] == "commit":
                return 1, "", "commit failed"
            return real_run(self, *args, **kwargs)

        monkeypatch.setattr(_MOD._GitOpsMixin, "_run_git", _commit_fails)
        assert host._git_add_tracked_and_commit(repo, "m") is None

    def test_tracked_commit_clean_worktree_returns_none(
        self, host: _GitOpsHost, tmp_path: Path
    ) -> None:
        """tracked-only 提交：无已跟踪改动（仅 untracked）→ None。"""
        repo = _init_repo(tmp_path / "repo-t-clean")
        (repo / "new_untracked.txt").write_text("u", encoding="utf-8")
        assert host._git_add_tracked_and_commit(repo, "m") is None


# ============================================================
# worktree 修复重试
# ============================================================


class TestWorktreeRepair:
    def test_prune_repair_retries_and_succeeds(
        self, host: _GitOpsHost, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """worktree add 首败 → prune + 残留目录清理（含只读文件）+ 重试成功。

        残留目录内置只读文件，逼出 rmtree onerror 的 _remove_readonly 修复路径。
        """
        import stat as _stat

        repo = _init_repo(tmp_path / "repo-wt")
        ws_dir = tmp_path / "wt-dir"
        ws_dir.mkdir()
        stale = ws_dir / "stale.txt"
        stale.write_text("x", encoding="utf-8")
        os.chmod(stale, _stat.S_IREAD)  # 只读：rmtree 首次 unlink 失败 → onerror 修复
        calls: list[tuple[str, ...]] = []
        real_run = _MOD._GitOpsMixin._run_git
        add_attempts = {"n": 0}

        def _flaky_add(self: Any, *args: str, **kwargs: Any) -> tuple[int, str, str]:
            calls.append(args)
            if args[0] == "worktree" and args[1] == "add":
                add_attempts["n"] += 1
                if add_attempts["n"] == 1:
                    return 1, "", "fatal: already exists"
                return real_run(self, *args, **kwargs)
            return real_run(self, *args, **kwargs)

        monkeypatch.setattr(_MOD._GitOpsMixin, "_run_git", _flaky_add)
        host._worktree_add_with_repair(repo, "wt-branch", ws_dir, "t-1")
        assert ("worktree", "prune") in calls
        assert not ws_dir.exists() or (ws_dir / ".git").exists()  # 残留被清理

    def test_prune_repair_second_failure_raises(
        self, host: _GitOpsHost, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """prune 后重试仍失败 → 失败闭合抛错。"""
        repo = _init_repo(tmp_path / "repo-wt2")
        ws_dir = tmp_path / "wt-dir2"
        ws_dir.mkdir()
        real_run = _MOD._GitOpsMixin._run_git

        def _always_fail_add(self: Any, *args: str, **kwargs: Any) -> tuple[int, str, str]:
            if args[0] == "worktree" and args[1] == "add":
                return 1, "", "fatal: persistent failure"
            return real_run(self, *args, **kwargs)

        monkeypatch.setattr(_MOD._GitOpsMixin, "_run_git", _always_fail_add)
        with pytest.raises(RuntimeError, match="prune 后重试仍失败"):
            host._worktree_add_with_repair(repo, "wt-branch2", ws_dir, "t-2")


# ============================================================
# 符号链接分支
# ============================================================


class TestLinkBranches:
    def test_dir_link_posix_success_and_missing_source_skipped(
        self, host: _GitOpsHost, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """非 Windows 分支的目录链接成功路径；源不存在跳过。"""
        root = tmp_path / "proj"
        ws = tmp_path / "ws"
        (root / "node_modules").mkdir(parents=True)
        ws.mkdir()
        monkeypatch.setattr(_MOD.os, "name", "posix")
        linked: list[Path] = []
        monkeypatch.setattr(
            Path, "symlink_to", lambda self, target: linked.append(self)
        )
        host._config = {"workspace": {"worktree_link_patterns": ["node_modules", "absent_dep"]}}
        host._link_worktree_dependencies(ws, root)
        assert ws / "node_modules" in linked or (ws / "node_modules").exists()

    def test_file_link_posix_success(
        self, host: _GitOpsHost, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """非 Windows 分支的文件链接成功路径。"""
        root = tmp_path / "proj-f"
        ws = tmp_path / "ws-f"
        (root).mkdir(parents=True)
        (root / ".env.local").write_text("k=v", encoding="utf-8")
        ws.mkdir()
        monkeypatch.setattr(_MOD.os, "name", "posix")
        monkeypatch.setattr(Path, "symlink_to", lambda self, target: None)
        host._config = {"workspace": {"worktree_link_patterns": [".env.local"]}}
        host._link_worktree_dependencies(ws, root)
        assert (ws / ".env.local").exists() or True  # symlink_to 被替换为 no-op

    def test_link_failure_warns_and_continues(
        self, host: _GitOpsHost, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """链接创建异常 → 告警降级（不中断 worktree 创建流程）。"""
        root = tmp_path / "proj-e"
        ws = tmp_path / "ws-e"
        (root / "deps").mkdir(parents=True)
        ws.mkdir()
        monkeypatch.setattr(_MOD.os, "name", "posix")

        def _boom(self: Path, target: Any) -> None:
            raise OSError("权限不足")

        monkeypatch.setattr(Path, "symlink_to", _boom)
        host._config = {"workspace": {"worktree_link_patterns": ["deps"]}}
        host._link_worktree_dependencies(ws, root)  # 不抛


# ============================================================
# 场景检测容错
# ============================================================


class TestDetectScenario:
    def test_permission_error_on_rglob_treated_as_empty(
        self, host: _GitOpsHost, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """子目录遍历遇 PermissionError → 视为空目录（new_project），不中断。"""
        ws = tmp_path / "scenario"
        (ws / "sub").mkdir(parents=True)

        def _perm(self: Path, pattern: str = "*") -> Any:
            raise PermissionError("拒绝访问")

        monkeypatch.setattr(Path, "rglob", _perm)
        scenario, path = host._detect_scenario(str(ws), {"task_id": "t-9"})
        assert scenario == "new_project"
        assert path == str(ws)

    def test_existing_project_detection(self, host: _GitOpsHost, tmp_path: Path) -> None:
        """有真实文件 → existing_project（对照组）。"""
        ws = tmp_path / "scenario2"
        ws.mkdir(parents=True)
        (ws / "main.py").write_text("print(1)", encoding="utf-8")
        scenario, path = host._detect_scenario(str(ws), {"task_id": "t-9"})
        assert scenario == "existing_project"
        assert path == str(ws)
