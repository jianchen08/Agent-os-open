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
_TASKS_PLUGIN_DIR = _SHARED_DIR / "tasks"
_PROJECT_MANAGER_DIR = _SHARED_DIR / "project_manager"


@pytest.fixture(autouse=True)
def _isolate_registry_module():
    """共享层 + tasks/project_manager 插件裸名逐出 + 代际还原（串扰防线）。

    tasks/project_manager 目录强制置顶（先移除再插入）：其他测试文件收集期/
    运行期会把同名裸名插件的目录残留在 sys.path 前部（如 multimodal 的
    storage.py），仅判断"已在"不纠位会让 from storage import 命中敌意同名
    模块。平铺迁移（ADR 2026-10-13-plugin-flat-layout）后 project_registry
    属主目录 = plugins/shared/project_manager。
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
    pm = str(_PROJECT_MANAGER_DIR)
    pm_dir_was_present = pm in sys.path
    if pm in sys.path:
        sys.path.remove(pm)
    sys.path.insert(0, pm)
    yield
    if not pm_dir_was_present and pm in sys.path:
        sys.path.remove(pm)
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
    """容器任务遗留数据清除·目录半（purge_legacy_workspace_dirs，幂等）。

    T6a 拆分：任务行半（container 行删除+子任务挂靠脱钩）为纯 TaskStorage
    操作已内联 tasks/server.py 启动序列（隔离测试面待 tasks 域补，治理候选）；
    本类钉目录半——工作空间基目录下 container_* 副本目录清除。"""

    def test_removes_container_dirs_keeps_others(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import project_registry as pr

        monkeypatch.setattr(pr, "workspace_base_dir", lambda: tmp_path)
        container_dir = tmp_path / "container_c-1"
        keep_dir = tmp_path / "normal_ws"
        container_dir.mkdir()
        keep_dir.mkdir()

        removed = pr.purge_legacy_workspace_dirs()

        assert removed == 1
        assert not container_dir.exists()
        assert keep_dir.exists()  # container_ 前缀外目录不动

    def test_idempotent_and_noop_on_clean_state(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import project_registry as pr

        monkeypatch.setattr(pr, "workspace_base_dir", lambda: tmp_path)

        first = pr.purge_legacy_workspace_dirs()
        second = pr.purge_legacy_workspace_dirs()

        assert first == 0
        assert second == first

    def test_missing_base_dir_is_noop(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """工作空间基目录不存在 = 空转（首启/全新安装形态）。"""
        import project_registry as pr

        monkeypatch.setattr(pr, "workspace_base_dir", lambda: tmp_path / "nope")

        assert pr.purge_legacy_workspace_dirs() == 0

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
        from project_registry import load_zone_sections

        base = tmp_path / "cfgusers"
        assert load_zone_sections(base=base)["entries"] == []

        d = base / "default"
        d.mkdir(parents=True)
        (d / "project_whitelist.yaml").write_text(
            "locked: true\nentries:\n"
            f"  - {tmp_path / 'work'}\n"
            f"  - {tmp_path / 'space two'}\n",
            encoding="utf-8",
        )
        assert load_zone_sections(base=base)["entries"] == [
            str(tmp_path / "work"),
            str(tmp_path / "space two"),
        ]

        (d / "project_whitelist.yaml").write_text("entries: [unclosed", encoding="utf-8")
        assert load_zone_sections(base=base)["entries"] == []

    def test_load_read_deny_and_zone_policy_sections(self, tmp_path: Path) -> None:
        """read_deny 节与 entries 同文件解析（ADR 2026-09-24 决策1/4）；缺节 = 无排除。"""
        from project_registry import load_zone_sections

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
        assert load_zone_sections(base=base)["entries"] == [str(tmp_path / "work")]
        assert load_zone_sections(base=base)["read_deny"] == [
            str(tmp_path / "private"),
            str(tmp_path / "space two"),
        ]

        (d / "project_whitelist.yaml").write_text(
            f"entries:\n  - {tmp_path / 'work'}\n", encoding="utf-8"
        )
        assert load_zone_sections(base=base)["read_deny"] == []

        # 非字典 YAML（标量/列表形态）= 空名单，不炸
        (d / "project_whitelist.yaml").write_text("- scalar\n", encoding="utf-8")
        assert load_zone_sections(base=base)["entries"] == []
        assert load_zone_sections(base=base)["read_deny"] == []

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

        assert pr.load_zone_sections()["entries"] == [str(tmp_path / "legacy_zone")]
        assert pr.load_zone_sections()["read_deny"] == [str(tmp_path / "legacy_deny")]

        empty = tmp_path / "explicit_base"
        assert pr.load_zone_sections(base=empty)["entries"] == []

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
        assert pr.load_zone_sections()["entries"] == [str(tmp_path / "us_zone")]
        assert pr.load_zone_sections()["read_deny"] == []

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
        monkeypatch.setattr(
            pr, "load_zone_sections",
            lambda *a, **k: {"entries": [str(tmp_path / "work")], "read_deny": [], "read_allow": []},
        )
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
        monkeypatch.setattr(
            pr, "load_zone_sections",
            lambda *a, **k: {"entries": [str(tmp_path / "work")], "read_deny": [], "read_allow": []},
        )
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
        monkeypatch.setattr(
            pr, "load_zone_sections", lambda *a, **k: {"entries": [], "read_deny": [], "read_allow": []},
        )
        project, created = pr.ensure_project_registered(
            title="存量", explicit_path=str(tmp_path / "legacy"), registry=reg
        )
        assert created is False and project.id == legacy.id

    def test_overlap_with_existing_project_rejected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import project_registry as pr

        reg = pr.ProjectRegistry(data_dir=tmp_path / "tasks")
        monkeypatch.setattr(
            pr,
            "load_zone_sections",
            lambda *a, **k: {"entries": [str(tmp_path)], "read_deny": [], "read_allow": []},
        )
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


class TestModeRegistration:
    """mode_tags/default_mode 项目登记（ADR 2026-10-09 批 B8）。

    校验三条（登记/修改写入面 fail-closed）：形态 MODE_ID_RE / 注册表可解析
    （出厂+用户双根）/ default_mode ∈ mode_tags；save 写面变更检测执法。
    """

    def test_default_empty_tags(self, tmp_path: Path) -> None:
        from project_registry import ProjectModel

        p = ProjectModel(title="P", path="D:/x/p")
        assert p.mode_tags == []
        assert p.default_mode == ""

    def test_valid_tags_persisted_roundtrip(self, tmp_path: Path) -> None:
        from project_registry import ProjectModel, ProjectRegistry

        reg = ProjectRegistry(data_dir=tmp_path / "tasks")
        p = reg.save(
            ProjectModel(title="P", path="D:/x/p", mode_tags=["coding", "research"], default_mode="coding")
        )
        loaded = ProjectRegistry(data_dir=tmp_path / "tasks").get(p.id)
        assert loaded is not None
        assert loaded.mode_tags == ["coding", "research"]
        assert loaded.default_mode == "coding"

    @pytest.mark.parametrize(
        "bad_tag",
        ["Bad-Tag", "../evil", "1x", "with space", "x" * 65, "大写"],
    )
    def test_invalid_shape_rejected_names_tag(self, tmp_path: Path, bad_tag: str) -> None:
        from project_registry import ProjectModel, ProjectRegistry

        reg = ProjectRegistry(data_dir=tmp_path / "tasks")
        with pytest.raises(ValueError, match="非法模式标签"):
            reg.save(ProjectModel(title="P", path="D:/x/p", mode_tags=["coding", bad_tag]))

    def test_unresolvable_tag_rejected(self, tmp_path: Path) -> None:
        """出厂+用户双根均无该包 → 拒，错误信息含标签名。"""
        from project_registry import ProjectModel, ProjectRegistry

        reg = ProjectRegistry(data_dir=tmp_path / "tasks")
        with pytest.raises(ValueError, match="不可解析.*no_such_pack"):
            reg.save(ProjectModel(title="P", path="D:/x/p", mode_tags=["no_such_pack"]))

    def test_default_mode_not_in_tags_rejected(self, tmp_path: Path) -> None:
        from project_registry import ProjectModel, ProjectRegistry

        reg = ProjectRegistry(data_dir=tmp_path / "tasks")
        with pytest.raises(ValueError, match="default_mode 'game' 不在 mode_tags"):
            reg.save(
                ProjectModel(title="P", path="D:/x/p", mode_tags=["coding"], default_mode="game")
            )
        # 空集合 + 非空 default 同拒（主域必须是已登记标签之一）
        with pytest.raises(ValueError, match="不在 mode_tags"):
            reg.save(ProjectModel(title="P2", path="D:/x/p2", default_mode="coding"))

    def test_default_mode_empty_allowed_with_tags(self, tmp_path: Path) -> None:
        """default_mode 缺省（空串）= 未标注，合法（主域标注是可选项）。"""
        from project_registry import ProjectModel, ProjectRegistry

        reg = ProjectRegistry(data_dir=tmp_path / "tasks")
        p = reg.save(ProjectModel(title="P", path="D:/x/p", mode_tags=["coding"]))
        assert p.default_mode == ""

    def test_save_change_detection_unchanged_tags_skip_disk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """写面变更检测：mode 字段未变的保存（动作端点翻转 auto_execute 等）
        不重复对盘校验——模式包卸载后陈旧标签不卡死无关写面；改写标签本身
        照常被校验拦截（fail-closed）。"""
        import project_registry as pr

        reg = pr.ProjectRegistry(data_dir=tmp_path / "tasks")
        p = reg.save(pr.ProjectModel(title="P", path="D:/x/p", mode_tags=["coding"]))

        # validate_mode_registration 经模块级导入名消费 find_mode_package_dir
        # （from mode_keys import 绑定），钉桩须落 project_registry 命名空间。
        monkeypatch.setattr(pr, "find_mode_package_dir", lambda mode: None)
        p.auto_execute = True
        saved = reg.save(p)  # mode 字段未变 → 不触盘校验，翻转放行
        assert saved.auto_execute is True
        p.mode_tags = ["coding", "writing"]
        with pytest.raises(ValueError, match="不可解析"):
            reg.save(p)  # mode 字段变了 → 按当前盘面校验，拒

    def test_ensure_reuse_applies_tags_to_existing_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """存量项目补挂：同路径复用通道显式携带 tags → 更新既有登记行
        （created=False）；不带 tags 的复用不动既有登记。"""
        import project_registry as pr

        # 白名单自足：登记闸读用户空间真值（开发机装机副本恰含 Temp 目录时
        # 会偶然放行），显式钉本测 tmp 根为授权条目，不依赖宿主名单状态
        monkeypatch.setattr(
            pr, "load_zone_sections",
            lambda: {"entries": [str(tmp_path)], "read_deny": []},
        )

        folder = tmp_path / "proj"
        folder.mkdir()
        reg = pr.ProjectRegistry(data_dir=tmp_path / "tasks")
        p1, created1 = pr.ensure_project_registered(
            "P", explicit_path=str(folder), registry=reg
        )
        assert created1 is True and p1.mode_tags == []
        p2, created2 = pr.ensure_project_registered(
            "P", explicit_path=str(folder), registry=reg,
            mode_tags=["coding", "research"], default_mode="coding",
        )
        assert created2 is False
        assert p2.id == p1.id
        assert p2.mode_tags == ["coding", "research"]
        assert p2.default_mode == "coding"
        p3, _created3 = pr.ensure_project_registered("P", explicit_path=str(folder), registry=reg)
        assert p3.mode_tags == ["coding", "research"]  # 未传 = 不动

    def test_ensure_rejects_invalid_tag_before_folder_creation(self, tmp_path: Path) -> None:
        """声明非法 tags → 建文件夹之前即拒（失败原子，不留孤儿目录）。"""
        import project_registry as pr

        reg = pr.ProjectRegistry(data_dir=tmp_path / "tasks")
        folder = tmp_path / "never_created"
        with pytest.raises(ValueError, match="非法模式标签"):
            pr.ensure_project_registered(
                "P", explicit_path=str(folder), registry=reg, mode_tags=["Nope"]
            )
        assert not folder.exists()


class TestSeedModeSkills:
    """技能播种到项目根 skills/（批 B8，用户裁定）：增量补缺不覆盖。

    四态（用户裁定验收车道）：复制 / 增量补缺 / 用户改动不覆盖 / tag 变更后
    新包补播；外加失败可见（陈旧标签/复制失败 warning，不阻断登记）。
    模式包经 AGENTOS_USER_ROOT 用户根播种（真实磁盘，find_mode_package_dir
    双根优先序消费），不 mock mode_keys。
    """

    @pytest.fixture
    def seeded(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
        """用户根 mode_demo 包（plugin.json 包标记 + skills/foo）+ 项目文件夹。"""
        user_root = tmp_path / "user_root"
        pkg = user_root / "plugins" / "modes" / "mode_demo"
        pkg.mkdir(parents=True)
        (pkg / "plugin.json").write_text("{}", encoding="utf-8")
        (pkg / "skills" / "foo").mkdir(parents=True)
        (pkg / "skills" / "foo" / "SKILL.md").write_text("foo v1", encoding="utf-8")
        monkeypatch.setenv("AGENTOS_USER_ROOT", str(user_root))
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        return {"user_root": user_root, "pkg": pkg, "proj": project_dir}

    def _project(self, tmp_path: Path, proj: Path, tags: list[str]) -> Any:
        from project_registry import ProjectModel

        return ProjectModel(title="P", path=str(proj), mode_tags=tags)

    def test_copies_tag_pack_skills(self, tmp_path: Path, seeded: dict[str, Path]) -> None:
        from project_registry import seed_mode_skills

        p = self._project(tmp_path, seeded["proj"], ["demo"])
        assert seed_mode_skills(p) == []
        skill = seeded["proj"] / "skills" / "foo" / "SKILL.md"
        assert skill.is_file()
        assert skill.read_text(encoding="utf-8") == "foo v1"

    def test_no_tags_or_no_path_is_noop(self, tmp_path: Path, seeded: dict[str, Path]) -> None:
        from types import SimpleNamespace

        from project_registry import seed_mode_skills

        assert seed_mode_skills(self._project(tmp_path, seeded["proj"], [])) == []
        empty = seed_mode_skills(SimpleNamespace(mode_tags=["demo"], path=""))
        assert empty == []
        assert not (seeded["proj"] / "skills").exists()

    def test_incremental_backfill_keeps_existing(
        self, tmp_path: Path, seeded: dict[str, Path]
    ) -> None:
        """包侧新增技能 + 幂等重跑：缺的补齐，已有保持原样（对账补缺）。"""
        from project_registry import seed_mode_skills

        p = self._project(tmp_path, seeded["proj"], ["demo"])
        seed_mode_skills(p)
        # 包侧升级：新增 bar + 修改 foo
        (seeded["pkg"] / "skills" / "bar").mkdir()
        (seeded["pkg"] / "skills" / "bar" / "SKILL.md").write_text("bar v1", encoding="utf-8")
        (seeded["pkg"] / "skills" / "foo" / "SKILL.md").write_text("foo v2", encoding="utf-8")
        assert seed_mode_skills(p) == []  # 幂等重跑 = 对账补缺
        assert (seeded["proj"] / "skills" / "bar" / "SKILL.md").read_text(encoding="utf-8") == "bar v1"
        # 用户侧 foo 不被新版本覆盖
        assert (seeded["proj"] / "skills" / "foo" / "SKILL.md").read_text(encoding="utf-8") == "foo v1"

    def test_user_edit_not_overwritten(self, tmp_path: Path, seeded: dict[str, Path]) -> None:
        from project_registry import seed_mode_skills

        p = self._project(tmp_path, seeded["proj"], ["demo"])
        seed_mode_skills(p)
        user_file = seeded["proj"] / "skills" / "foo" / "SKILL.md"
        user_file.write_text("用户定制", encoding="utf-8")
        seed_mode_skills(p)
        assert user_file.read_text(encoding="utf-8") == "用户定制"

    def test_tag_change_seeds_new_pack(self, tmp_path: Path, seeded: dict[str, Path]) -> None:
        """tag 集合变更（加第二个包）：新包技能补播，旧包技能原样保留。"""
        pkg2 = seeded["user_root"] / "plugins" / "modes" / "mode_extra"
        pkg2.mkdir(parents=True)
        (pkg2 / "plugin.json").write_text("{}", encoding="utf-8")
        (pkg2 / "skills" / "extra-skill").mkdir(parents=True)
        (pkg2 / "skills" / "extra-skill" / "SKILL.md").write_text("extra", encoding="utf-8")
        from project_registry import seed_mode_skills

        p = self._project(tmp_path, seeded["proj"], ["demo"])
        seed_mode_skills(p)
        p2 = self._project(tmp_path, seeded["proj"], ["demo", "extra"])
        assert seed_mode_skills(p2) == []
        assert (seeded["proj"] / "skills" / "extra-skill" / "SKILL.md").is_file()
        assert (seeded["proj"] / "skills" / "foo" / "SKILL.md").is_file()

    def test_stale_tag_yields_warning_not_raise(
        self, tmp_path: Path, seeded: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """登记后包被卸载（陈旧标签）→ warning 返回不抛（播种不阻断登记）。"""
        import mode_keys

        monkeypatch.setattr(mode_keys, "find_mode_package_dir", lambda mode: None)
        from project_registry import seed_mode_skills

        warnings = seed_mode_skills(self._project(tmp_path, seeded["proj"], ["demo"]))
        assert len(warnings) == 1
        assert "demo" in warnings[0]
        assert not (seeded["proj"] / "skills").exists()

    def test_copy_failure_yields_warning(
        self, tmp_path: Path, seeded: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """单技能复制失败 → warning 返回（错误可见不静默），登记照常。"""
        import project_registry as pr

        def _boom(src: Any, dst: Any, **kw: Any) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(pr.shutil, "copytree", _boom)
        warnings = pr.seed_mode_skills(self._project(tmp_path, seeded["proj"], ["demo"]))
        assert len(warnings) == 1
        assert "disk full" in warnings[0]
