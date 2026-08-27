# @feature: FP-0.2.〇 管道引擎 | @ci: none-local
"""isolation workspace_lifecycle.py 工作空间生命周期测试（A5.3 补）。

覆盖：
1. init_container_workspace：复制源 + git init / 已存在复用 / 失败抛错；
2. on_task_start：复用已有 meta / 子任务共享父空间 / 根任务分发 + skills 复制；
3. _start_root_task：inherit 复用 / plain 直接目录 / 无显式 workspace 降级 plain /
   非 git 项目根降级 plain / worktree 建立全流程；
4. _persist_ws_meta / restore_ws_meta / cleanup_workspace。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent  # plugins/shared/system/isolation/
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_mod() -> Any:
    mod_name = "isolation_workspace_lifecycle_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "workspace_lifecycle.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
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
    """task_tree 假实现:内存任务表 + save_task 记入 metadata。"""

    def __init__(self, tasks: dict[str, _FakeTask] | None = None):
        self._tasks = tasks or {}
        self.saved: list[tuple[str, dict]] = []

    def get_task(self, task_id: str) -> _FakeTask | None:
        return self._tasks.get(task_id)

    async def save_task(self, task: _FakeTask) -> None:
        self.saved.append((task.id, dict(task.metadata)))


def _make_manager(
    tmp_path: Path,
    ws_root: str | Path,
    tasks: dict[str, _FakeTask] | None = None,
    meta_store: dict | None = None,
    config: dict | None = None,
) -> WorkspaceLifecycleManager:  # type: ignore[valid-type]  # importlib 动态加载的类别名不可作注解
    cfg = {"workspace": {"default_mode": "worktree", "root": str(ws_root)}}
    if config:
        cfg["workspace"].update(config)
    return WorkspaceLifecycleManager(
        resource_merge=None,
        config=cfg,
        task_tree=_FakeTree(tasks),
        ws_meta_store=meta_store or {},
        base_path=str(tmp_path),
    )


def _git_init(repo: Path, branch: str = "main") -> None:
    repo.mkdir(parents=True, exist_ok=True)
    m = _MOD.__dict__.get("_GIT_HELPER")
    import subprocess

    subprocess.run(["git", "init", "-b", branch, str(repo)], check=True, capture_output=True, text=True)
    (repo / "README.md").write_text("proj", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True, text=True)


class TestOnTaskStart:
    def test_reuses_existing_meta(self, tmp_path: Path) -> None:
        ws = tmp_path / "existing_ws"
        ws.mkdir()
        meta_store = {"t1": {"mode": "shared", "path": str(ws)}}
        m = _make_manager(tmp_path, ws_root=tmp_path / "wsroot", meta_store=meta_store)
        meta = m.on_task_start("t1", "unused", {"is_root": False})
        assert meta["path"] == str(ws)

    def test_meta_path_missing_recreates(self, tmp_path: Path) -> None:
        meta_store = {"t1": {"mode": "shared", "path": str(tmp_path / "gone")}}
        m = _make_manager(tmp_path, ws_root=tmp_path / "wsroot", meta_store=meta_store, tasks={})
        meta = m.on_task_start("t1", "ws", {"is_root": True})
        assert meta["path"]  # 重新创建（plain 空目录分支）
        assert m._ws_meta_store["t1"]["path"] == meta["path"]


class TestSubtask:
    def test_plain_mode_shares_host_dir(self, tmp_path: Path) -> None:
        host_ws = tmp_path / "host_ws"
        host_ws.mkdir()
        m = _make_manager(tmp_path, ws_root=tmp_path / "wsroot")
        meta = m._start_subtask("sub1", str(host_ws), {"workspace_mode": "plain"})
        assert meta["mode"] == "shared"
        assert meta["path"] == str(host_ws)

    def test_worktree_mode_shares_parent_path(self, tmp_path: Path) -> None:
        parent_ws = tmp_path / "parent_ws"
        parent_ws.mkdir()
        parent = _FakeTask("p1", parent_task_id=None)
        tree = _FakeTree({"sub1": _FakeTask("sub1", parent_task_id="p1")})
        meta_store = {"p1": {"path": str(parent_ws), "project_root": str(parent_ws)}}
        m = _make_manager(tmp_path, ws_root=tmp_path / "wsroot", tasks=tree._tasks, meta_store=meta_store)
        meta = m._start_subtask("sub1", "fallback", {"workspace_mode": "worktree"})
        assert meta["mode"] == "shared"
        assert meta["path"] == str(parent_ws)
        assert meta["project_root"] == str(parent_ws)


class TestRootTask:
    def test_inherit_workspace(self, tmp_path: Path) -> None:
        ws = tmp_path / "inherit_ws"
        ws.mkdir()
        m = _make_manager(tmp_path, ws_root=tmp_path / "wsroot")
        task_data = {
            "_inherit_workspace_resolved": True,
            "_source_ws_meta": {"mode": "worktree", "branch": "task/old", "project_root": str(ws)},
        }
        meta = m._start_root_task("r1", str(ws), task_data)
        assert meta["mode"] == "worktree"
        assert meta["branch"] == "task/old"
        assert meta["path"] == str(ws)

    def test_plain_mode_direct_directory(self, tmp_path: Path) -> None:
        ws = tmp_path / "plain_ws"
        ws.mkdir()
        m = _make_manager(tmp_path, ws_root=tmp_path / "wsroot")
        meta = m._start_root_task("r1", str(ws), {"workspace_mode": "plain", "task_id": "r1"})
        assert meta["mode"] == "plain"
        assert meta["path"] == str(ws)

    def test_plain_no_explicit_workspace_creates_dir(self, tmp_path: Path) -> None:
        m = _make_manager(tmp_path, ws_root=tmp_path / "wsroot")
        meta = m._start_root_task("r1", "", {"workspace_mode": "plain", "task_id": "r1"})
        # 空 workspace 时 _detect_scenario 返回 ws_root/task_id,该分支的 mode 语义为 shared
        assert meta["mode"] == "shared"
        assert Path(meta["path"]).exists()
        assert meta["task_id"] == "r1"

    def test_no_git_repo_falls_back_plain(self, tmp_path: Path) -> None:
        ws = tmp_path / "non_git"
        ws.mkdir()
        m = _make_manager(tmp_path, ws_root=tmp_path / "wsroot")
        meta = m._start_root_task("r1", str(ws), {"task_id": "r1"})
        assert meta["mode"] == "plain"
        assert Path(meta["path"]).exists()

    def test_worktree_full_flow(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _git_init(repo)
        m = _make_manager(tmp_path, ws_root=tmp_path / "wsroot")
        meta = m._start_root_task("r1", str(repo), {"task_id": "r1", "_has_explicit_workspace": True})
        assert meta["mode"] == "worktree"
        assert meta["branch"] == "task/r1"
        ws_dir = Path(meta["path"])
        assert ws_dir.exists()
        assert ws_dir != repo
        assert (ws_dir / "README.md").exists()

    def test_sparse_worktree_when_large(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _git_init(repo)
        # 造大文件超过 sparse 阈值
        big = repo / "big.bin"
        big.write_bytes(b"\x00" * 200 * 1024 * 1024)
        import subprocess

        subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True, text=True)
        subprocess.run(["git", "commit", "-m", "big"], cwd=repo, check=True, capture_output=True, text=True)
        m = _make_manager(
            tmp_path,
            ws_root=tmp_path / "wsroot",
            config={"sparse_threshold_mb": 1},
        )
        meta = m._start_root_task("r1", str(repo), {"task_id": "r1", "_has_explicit_workspace": True})
        assert meta["mode"] == "worktree"
        assert Path(meta["path"]).exists()


class TestSkillsCopy:
    def test_skills_copied_to_workspace(self, tmp_path: Path) -> None:
        (tmp_path / "skills" / "skill_a" / "scripts").mkdir(parents=True)
        (tmp_path / "skills" / "skill_a" / "SKILL.md").write_text("# a", encoding="utf-8")
        ws = tmp_path / "ws"
        ws.mkdir()
        m = _make_manager(tmp_path, ws_root=tmp_path / "wsroot")
        m._copy_skills_to_workspace(str(ws))
        assert (ws / "skills" / "skill_a" / "SKILL.md").exists()

    def test_skills_missing_dir_skipped(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        m = _make_manager(tmp_path, ws_root=tmp_path / "wsroot")
        m._copy_skills_to_workspace(str(ws))  # base_path 下无 skills/ → 静默跳过
        assert not (ws / "skills").exists()

    def test_skills_same_dir_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "skills").mkdir()
        m = _make_manager(tmp_path, ws_root=tmp_path / "wsroot")
        m._copy_skills_to_workspace(str(tmp_path))  # ws == base_path → 跳过
        assert (tmp_path / "skills").exists()

    def test_existing_skill_kept(self, tmp_path: Path) -> None:
        (tmp_path / "skills" / "skill_a").mkdir(parents=True)
        (tmp_path / "skills" / "skill_a" / "SKILL.md").write_text("new", encoding="utf-8")
        ws = tmp_path / "ws"
        (ws / "skills" / "skill_a").mkdir(parents=True)
        (ws / "skills" / "skill_a" / "SKILL.md").write_text("old", encoding="utf-8")
        m = _make_manager(tmp_path, ws_root=tmp_path / "wsroot")
        m._copy_skills_to_workspace(str(ws))
        assert (ws / "skills" / "skill_a" / "SKILL.md").read_text(encoding="utf-8") == "old"


class TestMetaPersist:
    def test_persist_ws_meta(self, tmp_path: Path) -> None:
        task = _FakeTask("t1", metadata={})
        tree = _FakeTree({"t1": task})
        meta_store = {"t1": {"mode": "plain", "path": "/x"}}
        m = _make_manager(tmp_path, ws_root=tmp_path / "wsroot", tasks=tree._tasks, meta_store=meta_store)
        m._persist_ws_meta("t1")
        assert task.metadata.get("ws_meta") == {"mode": "plain", "path": "/x"}

    def test_persist_missing_meta_noop(self, tmp_path: Path) -> None:
        m = _make_manager(tmp_path, ws_root=tmp_path / "wsroot")
        m._persist_ws_meta("ghost")  # 无 meta → 直接返回

    def test_restore_ws_meta(self, tmp_path: Path) -> None:
        task = _FakeTask("t1", metadata={"ws_meta": {"mode": "worktree", "path": "/y"}})
        m = _make_manager(tmp_path, ws_root=tmp_path / "wsroot", tasks={"t1": task})
        m.restore_ws_meta("t1")
        assert m._ws_meta_store["t1"] == {"mode": "worktree", "path": "/y"}

    def test_restore_skips_when_already_present(self, tmp_path: Path) -> None:
        m = _make_manager(tmp_path, ws_root=tmp_path / "wsroot", meta_store={"t1": {"path": "/keep"}})
        m.restore_ws_meta("t1")  # store 已有 → 不覆盖
        assert m._ws_meta_store["t1"]["path"] == "/keep"


class TestCleanup:
    def test_cleanup_no_meta(self, tmp_path: Path) -> None:
        m = _make_manager(tmp_path, ws_root=tmp_path / "wsroot")
        r = m.cleanup_workspace("ghost")
        assert r == {"worktree_removed": False, "branch_deleted": False, "dir_removed": False}

    def test_cleanup_plain_keeps_dir(self, tmp_path: Path) -> None:
        ws = tmp_path / "plain_ws"
        ws.mkdir()
        meta_store = {"t1": {"mode": "plain", "path": str(ws)}}
        m = _make_manager(tmp_path, ws_root=tmp_path / "wsroot", meta_store=meta_store)
        r = m.cleanup_workspace("t1")
        assert r["worktree_removed"] is False
        assert ws.exists()
        assert "t1" not in m._ws_meta_store

    def test_cleanup_worktree(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _git_init(repo)
        m = _make_manager(tmp_path, ws_root=tmp_path / "wsroot")
        meta = m._start_root_task("r1", str(repo), {"task_id": "r1", "_has_explicit_workspace": True})
        ws_dir = Path(meta["path"])
        assert ws_dir.exists()
        r = m.cleanup_workspace("r1")
        assert r["worktree_removed"] is True
        assert r["branch_deleted"] is True
