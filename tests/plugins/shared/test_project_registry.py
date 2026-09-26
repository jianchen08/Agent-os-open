# @feature: FP-0.2.二 内部模块统一 manifest 化 | @vision: V3 可嵌入 | @ci: python-coverage
"""project_registry 共享模块测试（project = 文件夹 + 登记行）。

- ProjectRegistry：CRUD + YAML 持久化往返 + 列表时序。
- load_project_paths：只读 id → path 解析（跨插件消费面）。
- ensure_project_folder：显式路径复用（非空非 git 自动 init）、缺省 slug 重名后缀、
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
    """共享层 + tasks 插件裸名逐出 + 代际还原（串扰防线，与 tasks 测试同款）。

    tasks/shared 目录强制置顶（先移除再插入）：其他测试文件收集期/运行期
    会把同名裸名插件的目录残留在 sys.path 前部（如 multimodal 的 storage.py），
    仅判断"已在"不纠位会让 from storage import 命中敌意同名模块。
    """
    evict = ("project_registry", "task_types", "storage", "service", "service_access")
    was: dict[str, Any] = {}
    for m in evict:
        if m in sys.modules:
            was[m] = sys.modules.pop(m)
    d = str(_SHARED_DIR)
    if d in sys.path:
        sys.path.remove(d)
    sys.path.insert(0, d)
    t = str(_TASKS_PLUGIN_DIR)
    task_dir_was_present = t in sys.path
    if t in sys.path:
        sys.path.remove(t)
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

    def test_explicit_nonempty_non_git_auto_inits(self, tmp_path: Path) -> None:
        from project_registry import ensure_project_folder

        folder = tmp_path / "occupied"
        folder.mkdir()
        (folder / "f.txt").write_text("x", encoding="utf-8")
        # 非空非 git 目录不再拒绝：自动 git init 复用（幂等不删既有文件）
        path = ensure_project_folder("标题", str(folder))
        assert Path(path) == folder
        assert (folder / "f.txt").read_text(encoding="utf-8") == "x"
        assert (folder / ".git").is_dir()

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


class TestWorkflowState:
    """workflow_state 方案工作流状态（ADR 2026-09-17-plan-mode-project-state-gate）。

    与任务状态机无关（项目仍非任务实体）：这是方案生命周期能轴，
    plan→running / running→plan 为门控迁移（审批在工具层）。
    """

    def test_default_plan_and_persisted_roundtrip(self, tmp_path: Path) -> None:
        from project_registry import ProjectModel, ProjectRegistry

        reg = ProjectRegistry(data_dir=tmp_path / "tasks")
        p = reg.save(ProjectModel(title="P", path="D:/x/p"))
        assert p.workflow_state == "plan"
        loaded = ProjectRegistry(data_dir=tmp_path / "tasks").get(p.id)
        assert loaded is not None and loaded.workflow_state == "plan"

    def test_legacy_row_without_field_loads_as_plan(self, tmp_path: Path) -> None:
        """存量登记行缺 workflow_state 字段 → 默认 plan（破坏性迁移禁止）。"""
        import project_registry as pr

        data_dir = tmp_path / "tasks"
        reg = pr.ProjectRegistry(data_dir=data_dir)
        p = reg.save(pr.ProjectModel(title="旧", path="D:/x/old"))
        assert p.workflow_state == "plan"
        # 手工抹掉字段模拟存量行
        row = data_dir / "projects" / f"{p.id}.yaml"
        text = row.read_text(encoding="utf-8").replace("workflow_state: plan\n", "")
        row.write_text(text, encoding="utf-8")
        loaded = pr.ProjectRegistry(data_dir=data_dir).get(p.id)
        assert loaded is not None and loaded.workflow_state == "plan"

    def test_legal_transitions(self, tmp_path: Path) -> None:
        from project_registry import ProjectModel, transition_workflow_state

        p = ProjectModel(title="P")
        assert transition_workflow_state(p, "running").workflow_state == "running"
        assert transition_workflow_state(p, "plan").workflow_state == "plan"
        transition_workflow_state(p, "running")
        assert transition_workflow_state(p, "done").workflow_state == "done"

    def test_illegal_transitions_fail_closed(self, tmp_path: Path) -> None:
        from project_registry import ProjectModel, transition_workflow_state

        p = ProjectModel(title="P")
        with pytest.raises(ValueError, match="plan → done"):
            transition_workflow_state(p, "done")
        done = ProjectModel(title="D", workflow_state="done")
        with pytest.raises(ValueError, match="终态"):
            transition_workflow_state(done, "running")


class TestRegistrationWhitelist:
    """登记白名单（locked 用户配置）+ 前缀授权 + 重叠检测。"""

    def test_load_whitelist_missing_entries_and_corrupt(self, tmp_path: Path) -> None:
        from project_registry import load_registration_whitelist

        base = tmp_path / "cfgusers"
        assert load_registration_whitelist(base=base) == []

        d = base / "default"
        d.mkdir(parents=True)
        (d / "project_whitelist.yaml").write_text(
            "locked: true\nentries:\n"
            f"  - {tmp_path / 'work'}\n"
            f"  - {tmp_path / 'space two'}\n",
            encoding="utf-8",
        )
        assert load_registration_whitelist(base=base) == [
            str(tmp_path / "work"),
            str(tmp_path / "space two"),
        ]

        (d / "project_whitelist.yaml").write_text("entries: [unclosed", encoding="utf-8")
        assert load_registration_whitelist(base=base) == []

    def test_load_read_deny_and_zone_policy_sections(self, tmp_path: Path) -> None:
        """read_deny 节与 entries 同文件解析（ADR 2026-09-24 决策1/4）；缺节 = 无排除。"""
        from project_registry import load_read_deny, load_registration_whitelist

        base = tmp_path / "cfgusers"
        d = base / "default"
        d.mkdir(parents=True)
        (d / "project_whitelist.yaml").write_text(
            "entries:\n"
            f"  - {tmp_path / 'work'}\n"
            "read_deny:\n"
            f"  - {tmp_path / 'private'}\n"
            f"  - {tmp_path / 'space two'}\n",
            encoding="utf-8",
        )
        assert load_registration_whitelist(base=base) == [str(tmp_path / "work")]
        assert load_read_deny(base=base) == [
            str(tmp_path / "private"),
            str(tmp_path / "space two"),
        ]

        (d / "project_whitelist.yaml").write_text(
            f"entries:\n  - {tmp_path / 'work'}\n", encoding="utf-8"
        )
        assert load_read_deny(base=base) == []

        # 非字典 YAML（标量/列表形态）= 空名单，不炸
        (d / "project_whitelist.yaml").write_text("- scalar\n", encoding="utf-8")
        assert load_registration_whitelist(base=base) == []
        assert load_read_deny(base=base) == []

    def test_missing_user_space_file_falls_back_to_legacy_base(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """真值迁移期读取回退（ADR 2026-09-24 决策5）：用户空间文件缺失 → legacy base 读。

        显式 base（钉桩）不回退；用户空间有文件即真值优先，不与 legacy 合并。
        """
        import project_registry as pr

        legacy = tmp_path / "repo_cfg_users"
        d = legacy / "default"
        d.mkdir(parents=True)
        (d / "project_whitelist.yaml").write_text(
            f"entries:\n  - {tmp_path / 'legacy_zone'}\n"
            f"read_deny:\n  - {tmp_path / 'legacy_deny'}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(pr, "legacy_config_users_base", lambda: legacy)
        # 默认解析（用户空间 base）钉到 tmp：不读宿主真实用户空间（机器状态敏感）。
        monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "usroot"))

        assert pr.load_registration_whitelist() == [str(tmp_path / "legacy_zone")]
        assert pr.load_read_deny() == [str(tmp_path / "legacy_deny")]

        empty = tmp_path / "explicit_base"
        assert pr.load_registration_whitelist(base=empty) == []

        user_space_dir = tmp_path / "user_space_users"
        (user_space_dir / "default").mkdir(parents=True)
        (user_space_dir / "default" / "project_whitelist.yaml").write_text(
            f"entries:\n  - {tmp_path / 'us_zone'}\n", encoding="utf-8"
        )
        monkeypatch.setattr(
            pr,
            "tenant_config_dir",
            lambda tenant_id, base=None: user_space_dir / tenant_id,
        )
        assert pr.load_registration_whitelist() == [str(tmp_path / "us_zone")]
        assert pr.load_read_deny() == []

    def test_match_registration_scope_prefix_any_depth(self, tmp_path: Path) -> None:
        import project_registry as pr

        scope = str(tmp_path / "work")
        hit = pr.match_registration_scope(
            pr._canonical_path(tmp_path / "work" / "a" / "b" / "c" / "new"), [scope]
        )
        assert hit == pr._canonical_path(scope)
        # .. 归一化后仍命中（前缀判定在 canonicalize 之后）
        tricky = pr._canonical_path(tmp_path / "work" / "a" / ".." / "b")
        assert pr.match_registration_scope(tricky, [scope]) is not None
        assert pr.match_registration_scope(pr._canonical_path(tmp_path / "elsewhere"), [scope]) is None

    def test_ws_base_is_default_member(self, ws_base: Path) -> None:
        import project_registry as pr

        assert pr.match_registration_scope(pr._canonical_path(ws_base / "projects" / "x"), []) is not None
        outside = ws_base.parent / "out"
        assert pr.match_registration_scope(pr._canonical_path(outside), []) is None

    def test_explicit_path_outside_scope_rejected_before_folder_creation(
        self, tmp_path: Path, ws_base: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import project_registry as pr

        reg = pr.ProjectRegistry(data_dir=tmp_path / "tasks")
        monkeypatch.setattr(pr, "load_registration_whitelist", lambda **k: [str(tmp_path / "work")])
        outside = tmp_path / "elsewhere" / "proj"
        with pytest.raises(ValueError, match="白名单"):
            pr.ensure_project_registered(
                title="越界", explicit_path=str(outside), registry=reg
            )
        assert not outside.exists()
        assert reg.list() == []

    def test_explicit_nested_any_depth_allowed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import project_registry as pr

        reg = pr.ProjectRegistry(data_dir=tmp_path / "tasks")
        monkeypatch.setattr(pr, "load_registration_whitelist", lambda **k: [str(tmp_path / "work")])
        deep = tmp_path / "work" / "a" / "b" / "尚未存在"
        project, created = pr.ensure_project_registered(
            title="深层", explicit_path=str(deep), registry=reg
        )
        assert created is True and project.workflow_state == "plan"
        assert deep.is_dir()

    def test_reuse_precedes_whitelist(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """同路径幂等复用先于白名单闸：存量登记不受新闸影响。"""
        import project_registry as pr

        reg = pr.ProjectRegistry(data_dir=tmp_path / "tasks")
        legacy = pr.ProjectModel(title="存量", path=str(tmp_path / "legacy"))
        reg.save(legacy)
        monkeypatch.setattr(pr, "load_registration_whitelist", lambda **k: [])
        project, created = pr.ensure_project_registered(
            title="存量", explicit_path=str(tmp_path / "legacy"), registry=reg
        )
        assert created is False and project.id == legacy.id

    def test_overlap_with_existing_project_rejected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import project_registry as pr

        reg = pr.ProjectRegistry(data_dir=tmp_path / "tasks")
        monkeypatch.setattr(pr, "load_registration_whitelist", lambda **k: [str(tmp_path)])
        outer = tmp_path / "scope" / "outer"
        pr.ensure_project_registered(title="外层", explicit_path=str(outer), registry=reg)

        inner = outer / "inner"
        with pytest.raises(ValueError, match="重叠"):
            pr.ensure_project_registered(title="内嵌", explicit_path=str(inner), registry=reg)
        containing = tmp_path / "scope"
        with pytest.raises(ValueError, match="重叠"):
            pr.ensure_project_registered(title="包裹", explicit_path=str(containing), registry=reg)
        assert reg.list()[0].path == str(outer)


class TestProjectRootOfTree:
    def test_env_config_root_resolves_parent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """AGENTOS_CONFIG_ROOT（指向 <project_root>/config）→ 父目录即项目根。"""
        import project_registry as pr

        cfg = tmp_path / "proj" / "config"
        cfg.mkdir(parents=True)
        monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(cfg))
        assert pr.project_root_of_tree() == tmp_path / "proj"

    def test_config_ancestor_found_without_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """无 env 时向上找 config/ 祖先：本仓布局命中仓库根（正常布局不变）。"""
        import project_registry as pr

        monkeypatch.delenv("AGENTOS_CONFIG_ROOT", raising=False)
        root = pr.project_root_of_tree()
        assert (root / "config").is_dir()

    def test_all_probes_miss_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """探针全失（env 无效 + 祖先链无 config/）→ raise，不再兜底落插件树。"""
        import project_registry as pr

        monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(tmp_path / "elsewhere" / "config"))
        fake_tree = tmp_path / "deploy" / "plugins" / "shared"
        fake_tree.mkdir(parents=True)
        monkeypatch.setattr(pr, "__file__", str(fake_tree / "project_registry.py"))
        with pytest.raises(RuntimeError, match="AGENTOS_CONFIG_ROOT"):
            pr.project_root_of_tree()
