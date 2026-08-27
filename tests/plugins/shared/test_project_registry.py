# @feature: FP-0.2.二 内部模块统一 manifest 化 | @vision: V3 可嵌入 | @ci: python-coverage
"""project_registry 共享模块测试（project = 文件夹 + 登记行）。

- ProjectRegistry：CRUD + YAML 持久化往返 + 列表时序。
- load_project_paths：只读 id → path 解析（跨插件消费面）。
- ensure_project_folder：显式路径校验（非空非 git 拒绝）、缺省 slug 重名后缀、
  git init 幂等、空目录复用。
- remove_project_folder：受保护路径拒删（盘符根/仓库根/工作空间基目录）。
- purge_legacy_container_data：容器行清除、子任务挂靠退化、container_* 目录
  删除、幂等。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_SHARED_DIR = Path(__file__).resolve().parents[3] / "plugins" / "shared"
_TASKS_PLUGIN_DIR = _SHARED_DIR / "system" / "tasks"


@pytest.fixture(autouse=True)
def _isolate_registry_module():
    """共享层 + tasks 插件裸名逐出 + 代际还原（串扰防线，与 tasks 测试同款）。"""
    evict = ("project_registry", "task_types", "storage", "service", "service_access")
    was: dict[str, Any] = {}
    for m in evict:
        if m in sys.modules:
            was[m] = sys.modules.pop(m)
    d = str(_SHARED_DIR)
    if d not in sys.path:
        sys.path.insert(0, d)
    t = str(_TASKS_PLUGIN_DIR)
    task_dir_was_present = t in sys.path
    if not task_dir_was_present:
        sys.path.insert(0, t)
    yield
    if not task_dir_was_present and t in sys.path:
        sys.path.remove(t)
    for m in evict:
        if m in was:
            sys.modules[m] = was[m]
        else:
            sys.modules.pop(m, None)


@pytest.fixture
def ws_base(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """工作空间基目录指向临时目录（隔离仓库真实 .ai_workspaces）。"""
    import project_registry as pr

    base = tmp_path / "ws"
    base.mkdir()
    monkeypatch.setattr(pr, "workspace_base_dir", lambda: base)
    return base


class TestProjectRegistry:
    def test_crud_and_persistence_roundtrip(self, tmp_path: Path) -> None:
        from project_registry import ProjectModel, ProjectRegistry

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
        from project_registry import ProjectModel, ProjectRegistry

        reg = ProjectRegistry(data_dir=tmp_path / "tasks")
        old = reg.save(ProjectModel(title="旧", created_at="2026-01-01T00:00:00"))
        new = reg.save(ProjectModel(title="新", created_at="2026-02-01T00:00:00"))
        assert [p.id for p in reg.list()] == [new.id, old.id]

    def test_corrupt_yaml_skipped(self, tmp_path: Path) -> None:
        from project_registry import ProjectRegistry

        data_dir = tmp_path / "tasks"
        reg = ProjectRegistry(data_dir=data_dir)
        (data_dir / "projects" / "bad.yaml").write_text("not: [closed", encoding="utf-8")
        reg2 = ProjectRegistry(data_dir=data_dir)
        assert reg2.list() == reg.list()


class TestLoadProjectPaths:
    def test_reads_id_to_path_map(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import project_registry as pr

        reg = pr.ProjectRegistry(data_dir=tmp_path / "tasks")
        p1 = reg.save(pr.ProjectModel(title="A", path="D:/a"))
        p2 = reg.save(pr.ProjectModel(title="B", path="D:/b"))
        monkeypatch.setenv("TASKS_STORAGE_DIR", str(tmp_path / "tasks"))

        paths = pr.load_project_paths()
        assert paths == {p1.id: "D:/a", p2.id: "D:/b"}

    def test_missing_dir_returns_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import project_registry as pr

        monkeypatch.setenv("TASKS_STORAGE_DIR", str(tmp_path / "nowhere"))
        assert pr.load_project_paths() == {}


class TestEnsureProjectFolder:
    def test_explicit_path_creates_folder_and_git(self, tmp_path: Path) -> None:
        from project_registry import ensure_project_folder

        folder = tmp_path / "explicit"
        got = ensure_project_folder("任意标题", str(folder))
        assert Path(got) == folder
        assert folder.is_dir()
        assert (folder / ".git").exists()

    def test_explicit_nonempty_non_git_rejected(self, tmp_path: Path) -> None:
        from project_registry import ensure_project_folder

        folder = tmp_path / "occupied"
        folder.mkdir()
        (folder / "f.txt").write_text("x", encoding="utf-8")
        with pytest.raises(ValueError, match="非空且不是 git 仓库"):
            ensure_project_folder("标题", str(folder))

    def test_default_slug_and_conflict_suffix(self, ws_base: Path) -> None:
        from project_registry import ensure_project_folder

        first = ensure_project_folder("我的 项目")
        assert Path(first).parent == ws_base / "projects"
        assert Path(first).name == "我的_项目"

        # 同名已存在非空（git init 后有 .git）→ 后缀 -2
        second = ensure_project_folder("我的 项目")
        assert Path(second).name == "我的_项目-2"

    def test_git_init_idempotent_on_existing_repo(self, tmp_path: Path) -> None:
        from project_registry import ensure_project_folder

        folder = tmp_path / "repo"
        ensure_project_folder("标题", str(folder))
        # 二次创建同一路径（已是 git 仓库）不报错、不重建
        again = ensure_project_folder("标题", str(folder))
        assert Path(again) == folder

    def test_empty_existing_folder_reused(self, tmp_path: Path) -> None:
        from project_registry import ensure_project_folder

        folder = tmp_path / "precreated"
        folder.mkdir()
        got = ensure_project_folder("标题", str(folder))
        assert Path(got) == folder


class TestRemoveProjectFolder:
    def test_removes_folder(self, tmp_path: Path) -> None:
        from project_registry import remove_project_folder

        folder = tmp_path / "victim"
        folder.mkdir()
        (folder / "f.txt").write_text("x", encoding="utf-8")
        assert remove_project_folder(str(folder)) is True
        assert not folder.exists()

    def test_guarded_paths_refused(self, ws_base: Path) -> None:
        import project_registry as pr
        from project_registry import remove_project_folder

        # 工作空间基本身 + 仓库根（project_root_of_tree 真值）拒删
        assert remove_project_folder(str(ws_base)) is False
        assert remove_project_folder(str(pr.project_root_of_tree())) is False
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
        from project_registry import purge_legacy_container_data

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
        from project_registry import purge_legacy_container_data

        storage = self._storage(tmp_path)
        self._mk_task(storage, "t-1")

        first = purge_legacy_container_data(storage)
        second = purge_legacy_container_data(storage)

        assert first == {"removed_containers": 0, "detached_children": 0, "removed_dirs": 0}
        assert second == first
        assert storage.get("t-1") is not None

    def test_soft_deleted_scope_key_ignored(self, tmp_path: Path, ws_base: Path) -> None:
        """scope 键只认 task_scope=container；其他 metadata 不误删。"""
        from project_registry import purge_legacy_container_data

        storage = self._storage(tmp_path)
        self._mk_task(storage, "t-9", source="project", task_scope="non_container")
        stats = purge_legacy_container_data(storage)
        assert stats["removed_containers"] == 0
        assert storage.get("t-9") is not None
