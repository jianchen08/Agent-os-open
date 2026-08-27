# @feature: FP-0.2.二 内部模块统一 manifest 化 | @vision: V3 可嵌入 | @ci: python-coverage
"""projects 模块测试（project = 文件夹 + 登记行）。

- ProjectRegistry：CRUD + YAML 持久化往返 + 列表时序。
- ensure_project_folder：显式路径校验（非空非 git 拒绝）、缺省 slug 重名后缀、
  git init 幂等、空目录复用。
- remove_project_folder：受保护路径拒删（盘符根/仓库根/工作空间基目录）。
- purge_legacy_container_data：容器行清除、子任务挂靠退化、container_* 目录
  删除、幂等。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent

_EVICT_NAMES = (
    "task_types",
    "state_machine",
    "storage",
    "service",
    "timer_manager",
    "agents_types",
    "enum_utils",
    "workspace",
    "service_access",
    "projects",
    "_task_cleanup",
    "_task_crud",
    "_task_state",
    "server",
    "http_api",
)


@pytest.fixture(autouse=True)
def _isolate_tasks_plugin_modules():
    """裸名逐出 + 代际还原（同 test_tasks_plugin.py，串扰防线）。"""
    d = str(_PLUGIN_DIR)
    was_present = d in sys.path
    if d in sys.path:
        sys.path.remove(d)
    sys.path.insert(0, d)
    evicted: dict[str, ModuleType] = {}
    for m in _EVICT_NAMES:
        if m in sys.modules:
            evicted[m] = sys.modules.pop(m)
    yield
    if d in sys.path:
        sys.path.remove(d)
    if was_present:
        sys.path.insert(0, d)
    for m in _EVICT_NAMES:
        if m in evicted:
            sys.modules[m] = evicted[m]
        else:
            sys.modules.pop(m, None)


@pytest.fixture
def ws_base(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """工作空间基目录指向临时目录（隔离仓库真实 .ai_workspaces）。"""
    import projects as projects_mod

    base = tmp_path / "ws"
    base.mkdir()
    monkeypatch.setattr(projects_mod, "workspace_base_dir", lambda: base)
    return base


class TestProjectRegistry:
    def test_crud_and_persistence_roundtrip(self, tmp_path: Path) -> None:
        from projects import ProjectModel, ProjectRegistry

        reg = ProjectRegistry(data_dir=tmp_path / "tasks")
        p = reg.save(ProjectModel(title="项目一", path="D:/x/proj"))
        assert reg.get(p.id).title == "项目一"

        # 新实例从磁盘加载（持久化真往返）
        reg2 = ProjectRegistry(data_dir=tmp_path / "tasks")
        loaded = reg2.get(p.id)
        assert loaded is not None
        assert (loaded.id, loaded.title, loaded.path) == (p.id, "项目一", "D:/x/proj")

        assert reg2.delete(p.id) is True
        assert reg2.get(p.id) is None
        assert reg2.delete(p.id) is False
        assert (tmp_path / "tasks" / "projects" / f"{p.id}.yaml").exists() is False

    def test_list_newest_first(self, tmp_path: Path) -> None:
        from projects import ProjectModel, ProjectRegistry

        reg = ProjectRegistry(data_dir=tmp_path / "tasks")
        old = reg.save(ProjectModel(title="旧", created_at="2026-01-01T00:00:00"))
        new = reg.save(ProjectModel(title="新", created_at="2026-02-01T00:00:00"))
        assert [p.id for p in reg.list()] == [new.id, old.id]

    def test_corrupt_yaml_skipped(self, tmp_path: Path) -> None:
        from projects import ProjectRegistry

        data_dir = tmp_path / "tasks"
        reg = ProjectRegistry(data_dir=data_dir)
        reg_dir = data_dir / "projects"
        (reg_dir / "bad.yaml").write_text("not: [closed", encoding="utf-8")
        reg2 = ProjectRegistry(data_dir=data_dir)
        assert reg2.list() == reg.list()


class TestEnsureProjectFolder:
    def test_explicit_path_creates_folder_and_git(self, tmp_path: Path) -> None:
        from projects import ensure_project_folder

        folder = tmp_path / "explicit"
        got = ensure_project_folder("任意标题", str(folder))
        assert Path(got) == folder
        assert folder.is_dir()
        assert (folder / ".git").exists()

    def test_explicit_nonempty_non_git_rejected(self, tmp_path: Path) -> None:
        from projects import ensure_project_folder

        folder = tmp_path / "occupied"
        folder.mkdir()
        (folder / "f.txt").write_text("x", encoding="utf-8")
        with pytest.raises(ValueError, match="非空且不是 git 仓库"):
            ensure_project_folder("标题", str(folder))

    def test_default_slug_and_conflict_suffix(self, ws_base: Path) -> None:
        from projects import ensure_project_folder

        first = ensure_project_folder("我的 项目")
        assert Path(first).parent == ws_base / "projects"
        assert Path(first).name == "我的_项目"

        # 同名已存在非空（git init 后有 .git）→ 后缀 -2
        second = ensure_project_folder("我的 项目")
        assert Path(second).name == "我的_项目-2"

    def test_git_init_idempotent_on_existing_repo(self, tmp_path: Path) -> None:
        from projects import ensure_project_folder

        folder = tmp_path / "repo"
        ensure_project_folder("标题", str(folder))
        # 二次创建同一路径（已是 git 仓库）不报错、不重建
        again = ensure_project_folder("标题", str(folder))
        assert Path(again) == folder

    def test_empty_existing_folder_reused(self, tmp_path: Path) -> None:
        from projects import ensure_project_folder

        folder = tmp_path / "precreated"
        folder.mkdir()
        got = ensure_project_folder("标题", str(folder))
        assert Path(got) == folder


class TestRemoveProjectFolder:
    def test_removes_folder(self, tmp_path: Path) -> None:
        from projects import remove_project_folder

        folder = tmp_path / "victim"
        folder.mkdir()
        (folder / "f.txt").write_text("x", encoding="utf-8")
        assert remove_project_folder(str(folder)) is True
        assert not folder.exists()

    def test_guarded_paths_refused(self, ws_base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import projects as projects_mod
        from projects import remove_project_folder

        # 工作空间基本身 + 仓库根（_project_root_of_tree 真值）拒删
        assert remove_project_folder(str(ws_base)) is False
        assert remove_project_folder(str(projects_mod._project_root_of_tree())) is False
        assert ws_base.exists()


class TestPurgeLegacyContainerData:
    def _storage(self, tmp_path: Path) -> Any:
        from storage import TaskStorage

        return TaskStorage(data_dir=tmp_path / "tasks")

    def _mk_task(self, storage: Any, tid: str, **meta: Any) -> None:
        from task_types import TaskModel

        storage.save(TaskModel(id=tid, title=tid, metadata=dict(meta)))

    def test_purges_containers_detaches_children_and_dirs(
            self, tmp_path: Path, ws_base: Path) -> None:
        from projects import purge_legacy_container_data

        storage = self._storage(tmp_path)
        self._mk_task(storage, "c-1", task_scope="container")
        self._mk_task(storage, "c-2", task_scope="container")
        self._mk_task(storage, "t-1")  # 普通任务保留
        self._mk_task(storage, "t-2")  # 容器子任务 → 退化
        storage.update("t-2", parent_task_id="c-1")

        container_dir = ws_base / "container_c-1"
        container_dir.mkdir(parents=True)
        (container_dir / "artifacts.txt").write_text("x", encoding="utf-8")
        keep_dir = ws_base / "task_t-1"
        keep_dir.mkdir(parents=True)

        stats = purge_legacy_container_data(storage)

        assert stats == {"removed_containers": 2, "detached_children": 1, "removed_dirs": 1}
        assert storage.get("c-1") is None and storage.get("c-2") is None
        assert storage.get("t-1") is not None
        assert storage.get("t-2").parent_task_id is None
        assert not container_dir.exists()
        assert keep_dir.exists()  # container_ 前缀外目录不动

    def test_idempotent_and_noop_on_clean_state(
            self, tmp_path: Path, ws_base: Path) -> None:
        from projects import purge_legacy_container_data

        storage = self._storage(tmp_path)
        self._mk_task(storage, "t-1")

        first = purge_legacy_container_data(storage)
        second = purge_legacy_container_data(storage)

        assert first == {"removed_containers": 0, "detached_children": 0, "removed_dirs": 0}
        assert second == first
        assert storage.get("t-1") is not None

    def test_soft_deleted_scope_key_ignored(self, tmp_path: Path, ws_base: Path) -> None:
        """scope 键只认 task_scope=container；其他 metadata 不误删。"""
        from projects import purge_legacy_container_data

        storage = self._storage(tmp_path)
        self._mk_task(storage, "t-9", source="project", task_scope="non_container")
        stats = purge_legacy_container_data(storage)
        assert stats["removed_containers"] == 0
        assert storage.get("t-9") is not None
