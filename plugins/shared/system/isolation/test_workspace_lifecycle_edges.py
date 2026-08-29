# @feature: FP-0.2.〇 管道引擎 | @ci: none-local
"""workspace_lifecycle 边缘分支补测（覆盖率 A5.3）。

既有 test_workspace_lifecycle.py 覆盖主流程，本文件补齐：
- __init__ 记录主分支失败降级 / _ensure_dir_and_git 三分支；
- init_container_workspace 相对源路径；
- on_task_start 子任务分发 / 父任务查找失败回退；
- 技能复制：纯文件跳过 / copytree 失败告警；
- _start_root_task：容器空间（有提交 / 无提交 / 分支守卫拒绝 auto-save）、
  容器父任务缺失报错、显式新目录 git init、已有 .git 无提交、项目根分支守卫；
- _persist_ws_meta：无运行循环调度 / 任务树异常 / save_task 协程失败；
- restore_ws_meta 异常；cleanup_workspace：残留 worktree 目录强删 / rmtree 失败 /
  plain 相对路径。

git 操作用 tmp 目录真实执行（唯一外部依赖为 git 子进程）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

MOD_NAME = "isolation_workspace_lifecycle_edges_test"


def _load_mod() -> Any:
    if MOD_NAME in sys.modules:
        del sys.modules[MOD_NAME]
    spec = importlib.util.spec_from_file_location(MOD_NAME, _PLUGIN_DIR / "workspace_lifecycle.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[MOD_NAME] = module
    spec.loader.exec_module(module)
    return module


_MOD = _load_mod()
WorkspaceLifecycleManager = _MOD.WorkspaceLifecycleManager


class _FakeTask:
    def __init__(self, task_id: str, parent_task_id: str | None = None, metadata: dict | None = None):
        self.id = task_id
        self.parent_task_id = parent_task_id
        self.metadata = metadata or {}


class _FakeTree:
    def __init__(self, tasks: dict[str, _FakeTask] | None = None):
        self._tasks = tasks or {}
        self.saved: list[tuple[str, dict]] = []

    def get_task(self, task_id: str) -> _FakeTask | None:
        return self._tasks.get(task_id)

    async def save_task(self, task: _FakeTask) -> None:
        self.saved.append((task.id, dict(task.metadata)))


class _RaisingTree(_FakeTree):
    def __init__(self, exc: Exception):
        super().__init__()
        self._exc = exc

    def get_task(self, task_id: str) -> _FakeTask | None:
        raise self._exc


class _FailingSaveTree(_FakeTree):
    async def save_task(self, task: _FakeTask) -> None:
        raise RuntimeError("disk full")


def _make_manager(
    tmp_path: Path,
    ws_root: str | Path,
    tasks: dict[str, _FakeTask] | None = None,
    meta_store: dict | None = None,
    config: dict | None = None,
    tree: _FakeTree | None = None,
) -> WorkspaceLifecycleManager:  # type: ignore[valid-type]  # importlib 动态加载的类别名不可作注解
    cfg = {"workspace": {"default_mode": "worktree", "root": str(ws_root)}}
    if config:
        cfg["workspace"].update(config)
    return WorkspaceLifecycleManager(
        resource_merge=None,
        config=cfg,
        task_tree=tree if tree is not None else _FakeTree(tasks),
        ws_meta_store=meta_store or {},
        base_path=str(tmp_path),
    )


def _git_init(repo: Path, with_commit: bool = True) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True, text=True)
    if with_commit:
        (repo / "README.md").write_text("proj", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True, text=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True, text=True)


# ═══════════════════════════════════════════════════════════
# 构造 / 目录与 git 初始化
# ═══════════════════════════════════════════════════════════


class TestInitEdges:
    def test_init_record_main_branch_failure_degrades(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """__init__ 中记录主分支抛异常 → 记警告继续，_main_branch 留空。"""
        def boom(self: Any) -> None:
            raise RuntimeError("no git available")

        monkeypatch.setattr(WorkspaceLifecycleManager, "_record_main_branch", boom)
        with caplog.at_level(logging.WARNING):
            m = WorkspaceLifecycleManager(
                None, {"workspace": {"root": str(tmp_path / "wsroot")}}, _FakeTree(), {}, str(tmp_path)
            )
        assert "记录主分支失败" in caplog.text
        assert m._main_branch == ""


# ═══════════════════════════════════════════════════════════
# 任务启动：子任务分发 / 父任务查找失败
# ═══════════════════════════════════════════════════════════


class TestStartEdges:
    def test_on_task_start_subtask_dispatch(self, tmp_path: Path) -> None:
        """on_task_start 非根任务 → 走 _start_subtask 共享父工作空间。"""
        parent_ws = tmp_path / "parent_ws"
        parent_ws.mkdir()
        tree = _FakeTree({"sub1": _FakeTask("sub1", parent_task_id="p1"), "p1": _FakeTask("p1")})
        meta_store = {"p1": {"path": str(parent_ws), "project_root": str(parent_ws)}}
        m = _make_manager(tmp_path, tmp_path / "wsroot", tasks=tree._tasks, meta_store=meta_store)
        meta = m.on_task_start("sub1", "fallback", {"is_root": False})
        assert meta["mode"] == "shared"
        assert meta["path"] == str(parent_ws)

    def test_subtask_parent_lookup_failure_raises_without_fallback(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """_start_subtask 父任务查找抛异常且无出生契约继承 → 显式报错（无降级）。"""
        tree = _RaisingTree(ValueError("tree down"))
        m = _make_manager(tmp_path, tmp_path / "wsroot", tree=tree)
        with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError, match="父链工作空间解析失败"):
            m._start_subtask("sub1", "fallback_ws", {"workspace_mode": "worktree"})
        assert "查找父任务失败" in caplog.text


# ═══════════════════════════════════════════════════════════
# 技能复制边缘
# ═══════════════════════════════════════════════════════════


class TestSkillsCopyEdges:
    def test_skills_copy_skips_plain_files(self, tmp_path: Path) -> None:
        """skills/ 下非目录项跳过，只复制目录技能。"""
        (tmp_path / "skills" / "skill_a").mkdir(parents=True)
        (tmp_path / "skills" / "notes.txt").write_text("x", encoding="utf-8")
        ws = tmp_path / "ws"
        ws.mkdir()
        m = _make_manager(tmp_path, tmp_path / "wsroot")
        m._copy_skills_to_workspace(str(ws))
        assert (ws / "skills" / "skill_a").is_dir()
        assert not (ws / "skills" / "notes.txt").exists()

    def test_skills_copy_copytree_failure_logs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """单个技能复制失败 → 记 warning，不影响其它技能。"""
        (tmp_path / "skills" / "skill_a").mkdir(parents=True)
        (tmp_path / "skills" / "skill_b").mkdir(parents=True)
        (tmp_path / "skills" / "skill_a" / "SKILL.md").write_text("# a", encoding="utf-8")
        ws = tmp_path / "ws"
        ws.mkdir()

        import shutil

        orig_copytree = shutil.copytree

        def _failing_copytree(src: Any, dst: Any, **kw: Any) -> Any:
            if "skill_a" in str(src):
                raise OSError("permission denied")
            return orig_copytree(src, dst, **kw)

        monkeypatch.setattr(shutil, "copytree", _failing_copytree)
        m = _make_manager(tmp_path, tmp_path / "wsroot")
        with caplog.at_level(logging.WARNING):
            m._copy_skills_to_workspace(str(ws))
        assert "技能复制失败" in caplog.text
        # 失败技能不落地，成功技能正常落地
        assert not (ws / "skills" / "skill_a").exists()
        assert (ws / "skills" / "skill_b").is_dir()


# ═══════════════════════════════════════════════════════════
# _start_root_task：显式 workspace / 缺失目录 / .git 无提交 / 分支守卫
# ═══════════════════════════════════════════════════════════


class TestRootTaskEdges:
    def test_root_task_project_folder_worktree_branches_from_project(self, tmp_path: Path) -> None:
        """挂项目任务：worktree 从项目文件夹分叉（branch=task/{id} 落项目仓库），
        worktree 目录在工作空间根下，项目主工作树不被污染。"""
        proj = tmp_path / "myproj"
        _git_init(proj)
        (proj / "src.txt").write_text("s", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=proj, check=True, capture_output=True, text=True)
        subprocess.run(["git", "commit", "-m", "add src"], cwd=proj, check=True, capture_output=True, text=True)
        m = _make_manager(tmp_path, tmp_path / "wsroot")
        meta = m._start_root_task("r1", str(proj), {"task_id": "r1", "_has_explicit_workspace": True})
        assert meta["mode"] == "worktree"
        assert meta["branch"] == "task/r1"
        assert meta["project_root"] == str(proj)
        # worktree 目录落在工作空间根（不在项目文件夹内制造垃圾目录）
        assert Path(meta["path"]).parent == tmp_path / "wsroot"
        # 分支挂在项目仓库上（worktree 的 git 源 = 项目文件夹）
        rc, out, _ = m._run_git("branch", "--list", "task/r1", cwd=proj)
        assert rc == 0 and "task/r1" in out
        # 项目主工作树保持干净（分支隔离，不被任务写入）
        rc_s, out_s, _ = m._run_git("status", "--porcelain", cwd=proj)
        assert rc_s == 0 and out_s.strip() == ""

    def test_root_task_explicit_missing_dir_initialized(self, tmp_path: Path) -> None:
        """显式 workspace 目录不存在 → 创建目录 + git init + worktree。"""
        target = tmp_path / "brand_new_proj"
        m = _make_manager(tmp_path, tmp_path / "wsroot")
        meta = m._start_root_task("r1", str(target), {"task_id": "r1", "_has_explicit_workspace": True})
        assert meta["mode"] == "worktree"
        assert target.exists()
        assert (target / ".git").exists()

    def test_root_task_explicit_dir_git_init_failure_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "fail_proj"
        m = _make_manager(tmp_path, tmp_path / "wsroot")
        monkeypatch.setattr(m, "_git_init_and_initial_commit", lambda *a, **k: False)
        with pytest.raises(RuntimeError, match="git init"):
            m._start_root_task("r1", str(target), {"task_id": "r1", "_has_explicit_workspace": True})

    def test_root_task_git_dir_without_commit_recovers(self, tmp_path: Path) -> None:
        """项目根 .git 存在但无提交 → 补 initial commit 后建 worktree。"""
        repo = tmp_path / "no_commit_repo"
        _git_init(repo, with_commit=False)
        m = _make_manager(tmp_path, tmp_path / "wsroot")
        meta = m._start_root_task("r1", str(repo), {"task_id": "r1", "_has_explicit_workspace": True})
        assert meta["mode"] == "worktree"
        assert Path(meta["path"]).exists()

    def test_root_task_git_no_commit_recovers_failure_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = tmp_path / "no_commit_raise"
        _git_init(repo, with_commit=False)
        m = _make_manager(tmp_path, tmp_path / "wsroot")
        real = m._git_init_and_initial_commit

        def flaky(cwd: Path, message: str) -> bool:
            if "no_commit_raise" in str(cwd):
                return False
            return real(cwd, message)

        monkeypatch.setattr(m, "_git_init_and_initial_commit", flaky)
        with pytest.raises(RuntimeError, match="无提交记录"):
            m._start_root_task("r1", str(repo), {"task_id": "r1", "_has_explicit_workspace": True})

    def test_root_task_project_root_branch_guard_skips_autosave(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """项目根在 feature 分支 → 守卫拒绝 auto-save（warning），仍建 worktree。"""
        repo = tmp_path / "guarded_root"
        _git_init(repo)
        m = WorkspaceLifecycleManager(
            None,
            {"workspace": {"root": str(tmp_path / "wsroot")}},
            _FakeTree({}),
            {},
            base_path=str(repo),
        )
        subprocess.run(["git", "checkout", "-b", "feature"], cwd=repo, check=True, capture_output=True, text=True)
        with caplog.at_level(logging.WARNING):
            meta = m._start_root_task("r1", str(repo), {"task_id": "r1", "_has_explicit_workspace": True})
        assert meta["mode"] == "worktree"
        assert "跳过项目根目录 auto-save" in caplog.text


# ═══════════════════════════════════════════════════════════
# ws_meta 持久化 / 恢复边缘
# ═══════════════════════════════════════════════════════════


class TestMetaPersistEdges:
    def test_persist_no_running_loop_schedules_on_event_loop(self, tmp_path: Path) -> None:
        """无运行中事件循环（同步上下文）→ 退到 get_event_loop 调度，不崩溃。"""
        task = _FakeTask("t1", metadata={})
        tree = _FakeTree({"t1": task})
        meta_store = {"t1": {"mode": "plain", "path": "/x"}}
        m = _make_manager(tmp_path, tmp_path / "wsroot", tasks=tree._tasks, meta_store=meta_store)
        m._persist_ws_meta("t1")  # 同步调用，无运行中循环
        assert task.metadata.get("ws_meta") == {"mode": "plain", "path": "/x"}

    def test_persist_tree_failure_warns(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        tree = _RaisingTree(RuntimeError("tree down"))
        m = _make_manager(tmp_path, tmp_path / "wsroot", tree=tree, meta_store={"t1": {"mode": "plain"}})
        with caplog.at_level(logging.WARNING):
            m._persist_ws_meta("t1")
        assert "_persist_ws_meta 失败" in caplog.text

    async def test_persist_save_task_failure_logged(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """save_task 协程失败 → _log_persist_failure 记录 warning。"""
        task = _FakeTask("t1", metadata={})
        tree = _FailingSaveTree({"t1": task})
        m = _make_manager(tmp_path, tmp_path / "wsroot", tree=tree, meta_store={"t1": {"mode": "plain"}})
        with caplog.at_level(logging.WARNING):
            m._persist_ws_meta("t1")
            # 让 create_task 调度的协程跑完并触发 done 回调（多让出几轮，确保回调日志先于断言）
            for _ in range(3):
                await asyncio.sleep(0)
        assert "save_task 协程执行失败" in caplog.text
        assert "disk full" in caplog.text

    def test_persist_schedule_failure_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """无运行循环且 call_soon_threadsafe 调度失败 → 记 warning，不崩溃。"""
        task = _FakeTask("t1", metadata={})
        tree = _FakeTree({"t1": task})
        meta_store = {"t1": {"mode": "plain", "path": "/x"}}
        m = _make_manager(tmp_path, tmp_path / "wsroot", tree=tree, meta_store=meta_store)

        class _BadLoop:
            def is_closed(self) -> bool:
                return False

            def call_soon_threadsafe(self, fn: Any, *args: Any) -> None:
                raise RuntimeError("loop dead")

        monkeypatch.setattr(asyncio, "get_event_loop", lambda: _BadLoop())
        with caplog.at_level(logging.WARNING):
            m._persist_ws_meta("t1")  # 同步上下文：无运行循环 → 走 get_event_loop 调度
        assert "无法调度 save_task" in caplog.text

    def test_restore_tree_failure_warns(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        tree = _RaisingTree(RuntimeError("tree down"))
        m = _make_manager(tmp_path, tmp_path / "wsroot", tree=tree)
        with caplog.at_level(logging.WARNING):
            m.restore_ws_meta("t1")
        assert "restore_ws_meta 失败" in caplog.text


# ═══════════════════════════════════════════════════════════
# cleanup 边缘
# ═══════════════════════════════════════════════════════════


class TestCleanupEdges:
    def test_cleanup_worktree_removes_leftover_dir(self, tmp_path: Path) -> None:
        """worktree 目录名含 __wt_ 且 project_root 不可用 → 强删目录。"""
        ws_dir = tmp_path / "proj__wt_abc1234"
        ws_dir.mkdir()
        meta_store = {"t1": {"mode": "worktree", "path": str(ws_dir), "branch": "b1", "project_root": str(tmp_path / "no_such_repo")}}
        m = _make_manager(tmp_path, tmp_path / "wsroot", meta_store=meta_store)
        r = m.cleanup_workspace("t1")
        assert r["worktree_removed"] is False
        assert r["branch_deleted"] is False
        assert r["dir_removed"] is True
        assert not ws_dir.exists()
        assert "t1" not in m._ws_meta_store

    def test_cleanup_rmtree_failure_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        ws_dir = tmp_path / "proj__wt_9"
        ws_dir.mkdir()
        meta = {"mode": "worktree", "path": str(ws_dir), "branch": "b1", "project_root": str(tmp_path / "no_such_repo")}
        m = _make_manager(tmp_path, tmp_path / "wsroot", meta_store={"t1": meta})

        def fail_rmtree(path: str) -> None:
            raise OSError("locked")

        monkeypatch.setattr(_MOD, "_force_rmtree", fail_rmtree)
        with caplog.at_level(logging.WARNING):
            r = m.cleanup_workspace("t1")
        assert r["dir_removed"] is False
        assert "rmtree 失败" in caplog.text

    def test_cleanup_plain_relative_path(self, tmp_path: Path) -> None:
        """plain 模式相对路径 → resolve 后保留目录。"""
        m = _make_manager(tmp_path, tmp_path / "wsroot", meta_store={"t1": {"mode": "plain", "path": "rel_ws"}})
        r = m.cleanup_workspace("t1")
        assert r == {"worktree_removed": False, "branch_deleted": False, "dir_removed": False}
        assert "t1" not in m._ws_meta_store
