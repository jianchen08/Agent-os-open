# @feature: FP-0.2.二 内部模块 manifest | @ci: python-coverage
"""plugins/shared 根散模块（共享件）分支缺口补测。

按模块逐一钉行为契约（断输入→输出/副作用，不钉实现）：

- project_registry：登记目录解析优先级（显式 > TASKS_STORAGE_DIR > 多租户根）；
  YAML 非 dict 的行跳过不炸；持久化走原子写（.tmp 不残留、内容即登记行）；
  load_project_paths 对非 dict 文件跳过、path 缺失的登记不进映射；
  isolation_config.yaml 定位（AGENTOS_CONFIG_ROOT 命中/未命中回落祖先链）；
  workspace_base_dir 的 root 配置解析（绝对原样、非法配置回落缺省）；
  _slugify 非法字符折叠与限长；ensure_project_folder 的 git init 失败即报错；
  受保护路径删拒；rmtree 只读解锁回调（3.12 onexc / 3.11 onerror 双签名）；
  容器遗留目录删除失败留痕不炸。
- tenant_data：数据根 base 解析（env > 用户空间 > 仓内兜底）；
  config/users base 解析（env > 仓内）；capability 未注入 caller 为 None；
  迁移在 default 已存在时短路、在 base 不存在时返回空。
- repo_anchor：AGENTOS_CONFIG_ROOT 两种形态（指向仓库根 / 指向 config）解析；
  探针全失返回 None；repo_read_verdict 的仓库内三态；repo_walk_prune 剪枝集。
- proc_tree：非法 pid / 已退出进程返回空失败清单；子进程枚举失败与 kill
  失败进失败清单（psutil 为外部依赖，按模块替身注入）；收敛等待仍存活的
  进程进清单；Windows taskkill 兜底异常进清单。
- user_space：env 覆盖四键；OS 数据目录推导（win32 APPDATA 缺省返回 None、
  Linux XDG 覆盖）；空白 env 视为未设。
- state_fields：JSON 字符串还原、required 语义下的解析失败抛错、
  optional 降级留痕。
- bounded_dict：超限逐出最旧、TTL 清扫、env 非法值回落默认。
- uploads_path：UPLOADS_DIR 覆盖优先；/uploads/ 前缀与 basename 穿越拒绝。

结构性不可达/环境依赖残留（逐条说明，勿硬凑）：
1. ``proc_tree._taskkill_fallback`` 只在 os.name == "nt" 触发；本车道在
   Windows 跑得到，Linux CI 不可达（跨平台分支，非缺陷）。
2. ``user_space._os_data_dir`` 的 macOS 分支（``sys.platform == "darwin"``）
   与 Linux XDG 分支互斥于平台，同一进程只能命中一条。
3. ``project_registry.remove_project_folder`` 的 onerror 回退分支只在
   Python < 3.12 生效（``shutil.rmtree`` 无 onexc 参数）；本环境 3.12 恒走
   onexc。以 monkeypatch 替换 rmtree 签名验证分发判据与回退闭包契约。
4. ``bounded_dict.BoundedDict.__setitem__`` 的 ``if oldest is None: break``
   （第 90 行）不可达：``__init__`` 强制 ``_max > 0``（非法值回落 1024），
   而进入该 while 的前置条件是 ``len(_data) >= _max >= 1``——``_data`` 必非空，
   ``min(..., default=None)`` 恒不返回 None。纯防御性护栏，保留不删。
"""

from __future__ import annotations

import importlib
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SHARED_DIR = _REPO_ROOT / "plugins" / "shared"


@pytest.fixture
def shared(monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 plugins/shared 置顶到 sys.path（裸名 import 解析真身）。"""
    d = str(_SHARED_DIR)
    if d in sys.path:
        sys.path.remove(d)
    sys.path.insert(0, d)
    return _SHARED_DIR


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清掉会改变共享模块解析的外部 env（隔离真实部署环境）。"""
    for name in (
        "TASKS_STORAGE_DIR",
        "AGENTOS_DATA_DIR",
        "AGENTOS_CONFIG_USERS_DIR",
        "AGENTOS_CONFIG_ROOT",
        "AGENTOS_USER_ROOT",
        "AGENTOS_USER_CONFIG_DIR",
        "AGENTOS_USER_PLUGINS_DIR",
        "APPDATA",
        "XDG_DATA_HOME",
        "UPLOADS_DIR",
        "AGENTOS_BOUNDED_DICT_TTL_SECONDS",
        "AGENTOS_BOUNDED_DICT_MAX_ENTRIES",
    ):
        monkeypatch.delenv(name, raising=False)


# ══════════════════════════ project_registry ══════════════════════════


class TestRegistryDataDirResolution:
    """登记目录 = 与 TaskStorage 同根的 projects/ 子目录，三级优先级。"""

    def test_explicit_dir_wins_over_env(self, tmp_path: Path, shared: Path,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
        pr = importlib.import_module("project_registry")
        monkeypatch.setenv("TASKS_STORAGE_DIR", str(tmp_path / "env_root"))

        assert pr.registry_data_dir(tmp_path / "explicit") == (tmp_path / "explicit" / "projects")

    def test_env_dir_used_when_no_explicit(self, tmp_path: Path, shared: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
        pr = importlib.import_module("project_registry")
        monkeypatch.setenv("TASKS_STORAGE_DIR", str(tmp_path / "env_root"))

        assert pr.registry_data_dir() == tmp_path / "env_root" / "projects"

    def test_tenant_root_used_when_no_env(self, tmp_path: Path, shared: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
        """无显式与 env → 多租户根 data/{tenant}/tasks（AGENTOS_DATA_DIR 重定向）。"""
        pr = importlib.import_module("project_registry")
        monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path / "data"))

        assert pr.registry_data_dir(tenant_id="acme") == (
            tmp_path / "data" / "acme" / "tasks" / "projects"
        )
        assert pr.registry_data_dir() == (
            tmp_path / "data" / "default" / "tasks" / "projects"
        )


class TestRegistryLoadRobustness:
    def test_non_dict_yaml_line_skipped(self, tmp_path: Path, shared: Path) -> None:
        """YAML 解析成列表/标量的登记文件不是登记行 → 跳过且不炸。"""
        pr = importlib.import_module("project_registry")
        data_dir = tmp_path / "tasks"
        reg = pr.ProjectRegistry(data_dir=data_dir)
        projects_dir = data_dir / "projects"
        (projects_dir / "scalar.yaml").write_text("just-a-string\n", encoding="utf-8")
        (projects_dir / "list.yaml").write_text("- a\n- b\n", encoding="utf-8")

        reloaded = pr.ProjectRegistry(data_dir=data_dir)

        assert reloaded.list() == reg.list()
        assert (projects_dir / "scalar.yaml").exists(), "损坏/异形文件不被删除"

    def test_load_paths_skips_non_dict_and_pathless(self, tmp_path: Path, shared: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
        """load_project_paths：非 dict 跳过；id 缺失回落文件名；path 空不进映射。"""
        pr = importlib.import_module("project_registry")
        data_dir = tmp_path / "tasks"
        projects_dir = data_dir / "projects"
        projects_dir.mkdir(parents=True)
        (projects_dir / "withid.yaml").write_text(
            "id: pid1\npath: D:/x\n", encoding="utf-8"
        )
        (projects_dir / "noid.yaml").write_text("path: D:/y\n", encoding="utf-8")
        (projects_dir / "nopath.yaml").write_text("id: pid3\npath: ''\n", encoding="utf-8")
        (projects_dir / "notdict.yaml").write_text("'nope'\n", encoding="utf-8")
        (projects_dir / "broken.yaml").write_text("a: [unclosed\n", encoding="utf-8")
        monkeypatch.setenv("TASKS_STORAGE_DIR", str(data_dir))

        paths = pr.load_project_paths()

        assert paths == {"pid1": "D:/x", "noid": "D:/y"}
        assert "pid3" not in paths, "path 为空的登记行不得进入路径映射"


class TestRegistryPersistence:
    def test_persist_is_atomic_and_roundtrips(self, tmp_path: Path, shared: Path) -> None:
        """save 写盘后无 .tmp 残留，磁盘内容即登记行（跨实例真往返）。"""
        pr = importlib.import_module("project_registry")
        reg = pr.ProjectRegistry(data_dir=tmp_path / "tasks")
        saved = reg.save(pr.ProjectModel(id="abc123", title="原题", path="D:/p"))

        projects_dir = tmp_path / "tasks" / "projects"
        written = sorted(p.name for p in projects_dir.iterdir())
        assert written == ["abc123.yaml"], "原子写的临时文件不得残留"

        fresh = pr.ProjectRegistry(data_dir=tmp_path / "tasks")
        row = fresh.get("abc123")
        assert row is not None
        assert (row.title, row.path, row.id) == ("原题", "D:/p", "abc123")
        assert row.created_at == saved.created_at

    def test_delete_missing_file_still_reports_true(self, tmp_path: Path, shared: Path) -> None:
        """内存行存在而磁盘文件缺席（手工删/同步丢失）→ delete 仍成功报 True。"""
        pr = importlib.import_module("project_registry")
        reg = pr.ProjectRegistry(data_dir=tmp_path / "tasks")
        reg.save(pr.ProjectModel(id="gone00000001", title="t", path="D:/g"))
        (tmp_path / "tasks" / "projects" / "gone00000001.yaml").unlink()

        assert reg.delete("gone00000001") is True
        assert reg.get("gone00000001") is None


class TestIsolationConfigPath:
    def test_env_config_root_hit(self, tmp_path: Path, shared: Path,
                                 monkeypatch: pytest.MonkeyPatch) -> None:
        """AGENTOS_CONFIG_ROOT 下存在 isolation_config.yaml → 直接用该文件。"""
        pr = importlib.import_module("project_registry")
        cfg = tmp_path / "deploy" / "config"
        target = cfg / "plugins" / "isolation" / "isolation_config.yaml"
        target.parent.mkdir(parents=True)
        target.write_text("workspace:\n  root: X\n", encoding="utf-8")
        monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(cfg))

        assert pr._isolation_config_path() == target

    def test_fallback_when_env_path_absent(self, tmp_path: Path, shared: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
        """env 指向的路径无该文件 → 回落祖先链探针（本仓布局命中仓库真文件）。"""
        pr = importlib.import_module("project_registry")
        monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(tmp_path / "nowhere" / "config"))

        resolved = pr._isolation_config_path()

        assert resolved.exists()
        assert resolved.name == "isolation_config.yaml"


class TestWorkspaceBaseDir:
    def test_absolute_root_used_verbatim(self, tmp_path: Path, shared: Path,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
        pr = importlib.import_module("project_registry")
        cfg = tmp_path / "config"
        target = cfg / "plugins" / "isolation" / "isolation_config.yaml"
        target.parent.mkdir(parents=True)
        abs_root = tmp_path / "elsewhere" / "ws"
        target.write_text(f"workspace:\n  root: {abs_root}\n", encoding="utf-8")
        monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(cfg))

        assert pr.workspace_base_dir() == abs_root

    def test_relative_root_joined_to_project_root(self, tmp_path: Path, shared: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
        """相对 root 拼项目根（project_root_of_tree 真值），非 CWD。"""
        pr = importlib.import_module("project_registry")
        cfg = tmp_path / "proj" / "config"
        target = cfg / "plugins" / "isolation" / "isolation_config.yaml"
        target.parent.mkdir(parents=True)
        target.write_text("workspace:\n  root: rel_ws\n", encoding="utf-8")
        monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(cfg))

        base = pr.workspace_base_dir()

        assert base == tmp_path / "proj" / "rel_ws"

    @pytest.mark.parametrize(
        ("root_value", "expect_default"),
        [
            ("''", True),          # 空串 → 缺省
            ("'   '", True),       # 空白 → 缺省
            ("null", True),        # 显式 null → 缺省
            ("'ws_root'", False),  # 合法串 → 采用
        ],
    )
    def test_malformed_root_falls_back_to_default(
        self, tmp_path: Path, shared: Path,
        root_value: str, expect_default: bool, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pr = importlib.import_module("project_registry")
        cfg = tmp_path / "proj" / "config"
        target = cfg / "plugins" / "isolation" / "isolation_config.yaml"
        target.parent.mkdir(parents=True)
        target.write_text(f"workspace:\n  root: {root_value}\n", encoding="utf-8")
        monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(cfg))

        base = pr.workspace_base_dir()

        assert base.name == (".ai_workspaces" if expect_default else "ws_root")

    def test_unreadable_config_warns_and_defaults(self, tmp_path: Path, shared: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
        """配置文件读取异常 → warning 留痕 + 缺省 .ai_workspaces（不抛）。"""
        pr = importlib.import_module("project_registry")
        cfg = tmp_path / "proj" / "config"
        target = cfg / "plugins" / "isolation" / "isolation_config.yaml"
        target.parent.mkdir(parents=True)
        target.write_text("a: [unclosed\n", encoding="utf-8")  # yaml 解析错
        monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(cfg))

        base = pr.workspace_base_dir()

        assert base.name == ".ai_workspaces"
        assert base.is_absolute()


class TestSlugify:
    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("我的 项目", "我的_项目"),
            ("a/b\\c:d*e?f", "a_b_c_d_e_f"),
            ('<x>|"y"', "x_y"),
            ("...", "project"),          # 全折叠/全点 → 兜底 project
            ("", "project"),
            ("  lead", "lead"),           # 首尾点/空白剔除
        ],
    )
    def test_slug_rules(self, title: str, expected: str, shared: Path) -> None:
        pr = importlib.import_module("project_registry")
        assert pr._slugify(title) == expected

    def test_slug_capped_at_50(self, shared: Path) -> None:
        pr = importlib.import_module("project_registry")
        slug = pr._slugify("x" * 120)
        assert len(slug) == 50
        assert slug == "x" * 50


class TestEnsureProjectFolderFailures:
    def test_git_init_failure_raises_with_target(
        self, tmp_path: Path, shared: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """git init 非零退出 → RuntimeError 带上目标路径与 stderr（fail-closed）。"""
        pr = importlib.import_module("project_registry")

        class _Result:
            returncode = 128
            stderr = "fatal: cannot init here"

        monkeypatch.setattr(pr.subprocess, "run", lambda *a, **k: _Result())

        target = tmp_path / "repo"
        with pytest.raises(RuntimeError, match="git init 失败"):
            pr.ensure_project_folder("标题", str(target))
        assert target.is_dir(), "文件夹已建但未 init 时仍报错，不静默通过"

    def test_mkdir_oserror_wrapped_as_runtime_error(
        self, tmp_path: Path, shared: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """mkdir 失败（如父路径被文件占位）→ RuntimeError 而非裸 OSError。"""
        pr = importlib.import_module("project_registry")
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")

        with pytest.raises(RuntimeError, match="项目文件夹创建失败"):
            pr.ensure_project_folder("标题", str(blocker / "child"))

    def test_default_folder_with_empty_existing_dir_reused(
        self, tmp_path: Path, shared: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """缺省 slug 目录已存在但为空 → 复用（不递增后缀）。"""
        pr = importlib.import_module("project_registry")
        base = tmp_path / "ws"
        (base / "projects" / "标题").mkdir(parents=True)
        monkeypatch.setattr(pr, "workspace_base_dir", lambda: base)

        got = pr.ensure_project_folder("标题")

        assert Path(got) == base / "projects" / "标题"


class TestRemoveProjectFolderGuards:
    @pytest.mark.parametrize(
        "path",
        ["C:\\", "D:\\"],
    )
    def test_drive_root_refused_on_windows(self, path: str, shared: Path) -> None:
        if os.name != "nt":
            pytest.skip("盘符根受保护判定仅 Windows 生效")
        pr = importlib.import_module("project_registry")
        assert pr.remove_project_folder(path) is False

    def test_workspace_base_itself_refused(self, tmp_path: Path, shared: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
        pr = importlib.import_module("project_registry")
        base = tmp_path / "ws"
        base.mkdir()
        monkeypatch.setattr(pr, "workspace_base_dir", lambda: base)

        assert pr.remove_project_folder(str(base)) is False
        assert base.is_dir()

    def test_missing_path_returns_false(self, tmp_path: Path, shared: Path) -> None:
        pr = importlib.import_module("project_registry")
        assert pr.remove_project_folder(str(tmp_path / "nope")) is False

    def test_readonly_tree_removed_via_unlock_retry(
        self, tmp_path: Path, shared: Path,
    ) -> None:
        """只读文件（git 对象形态）经 onexc 解锁回调后仍能整体删除。"""
        pr = importlib.import_module("project_registry")
        victim = tmp_path / "victim" / "objects"
        victim.mkdir(parents=True)
        readonly = victim / "pack.idx"
        readonly.write_bytes(b"ro")
        os.chmod(readonly, 0o444)

        assert pr.remove_project_folder(str(victim.parent)) is True
        assert not (tmp_path / "victim").exists()

    def test_rmtree_onexc_callback_unlocks_then_raises_for_others(
        self, tmp_path: Path, shared: Path,
    ) -> None:
        """回调契约：PermissionError → 解锁后重试；其它异常原样抛出。"""
        pr = importlib.import_module("project_registry")
        target = tmp_path / "f.txt"
        target.write_text("x", encoding="utf-8")
        calls: list[str] = []

        def _retry(path: str) -> None:
            calls.append(path)

        pr._rmtree_onexc(_retry, str(target), PermissionError("locked"))
        assert calls == [str(target)]

        with pytest.raises(RuntimeError, match="boom"):
            pr._rmtree_onexc(_retry, str(target), RuntimeError("boom"))
        assert calls == [str(target)], "非权限异常不得触发重试"


class TestPurgeLegacyContainerDataGaps:
    def test_non_dir_container_entry_skipped(self, tmp_path: Path, shared: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
        """container_* 同名文件（非目录）跳过，不计入 removed_dirs。"""
        pr = importlib.import_module("project_registry")
        base = tmp_path / "ws"
        base.mkdir()
        (base / "container_notadir").write_text("x", encoding="utf-8")
        monkeypatch.setattr(pr, "workspace_base_dir", lambda: base)

        class _Storage:
            def list_all(self) -> list[Any]:
                return []

        stats = pr.purge_legacy_container_data(_Storage())

        assert stats == {"removed_containers": 0, "detached_children": 0, "removed_dirs": 0}
        assert (base / "container_notadir").exists()

    def test_absent_ws_base_is_noop(self, tmp_path: Path, shared: Path,
                                    monkeypatch: pytest.MonkeyPatch) -> None:
        """工作空间基目录不存在 → 目录段整体跳过（不炸）。"""
        pr = importlib.import_module("project_registry")
        monkeypatch.setattr(pr, "workspace_base_dir", lambda: tmp_path / "gone")

        class _Storage:
            def list_all(self) -> list[Any]:
                return []

        assert pr.purge_legacy_container_data(_Storage())["removed_dirs"] == 0

    def test_rmtree_failure_is_counted_out_not_raised(
        self, tmp_path: Path, shared: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """目录删除失败 → 留痕并跳过（下次启动重试），不计入统计、不向上抛。"""
        pr = importlib.import_module("project_registry")
        base = tmp_path / "ws"
        (base / "container_c9").mkdir(parents=True)
        monkeypatch.setattr(pr, "workspace_base_dir", lambda: base)

        def _boom(_path: Any, **_kwargs: Any) -> None:
            raise OSError("device busy")

        monkeypatch.setattr(shutil, "rmtree", _boom)

        class _Storage:
            def list_all(self) -> list[Any]:
                return []

        stats = pr.purge_legacy_container_data(_Storage())

        assert stats["removed_dirs"] == 0
        assert (base / "container_c9").is_dir()


# ══════════════════════════ tenant_data ══════════════════════════


class TestTenantDataBaseResolution:
    def test_default_data_base_env_wins(self, tmp_path: Path, shared: Path,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
        td = importlib.import_module("tenant_data")
        monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path / "env_data"))

        assert td._default_data_base() == tmp_path / "env_data"

    def test_default_data_base_falls_back_to_user_space(
        self, tmp_path: Path, shared: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """无 env → 用户空间 <USER_ROOT>/data（用户资产出仓铁律）。"""
        td = importlib.import_module("tenant_data")
        monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "uroot"))

        assert td._default_data_base() == tmp_path / "uroot" / "data"

    def test_default_data_base_repo_fallback_when_user_space_unresolved(
        self, shared: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """用户空间不可解析（无 AGENTOS_USER_ROOT 且 OS 目录缺失）→ 仓内 data/ 兜底。"""
        td = importlib.import_module("tenant_data")
        monkeypatch.setattr(td.user_space, "user_data_dir", lambda: None)

        fallback = td._default_data_base()

        assert fallback == _REPO_ROOT / "data"

    def test_config_users_env_wins(self, tmp_path: Path, shared: Path,
                                   monkeypatch: pytest.MonkeyPatch) -> None:
        td = importlib.import_module("tenant_data")
        monkeypatch.setenv("AGENTOS_CONFIG_USERS_DIR", str(tmp_path / "cfgusers"))

        assert td._default_config_users_base() == tmp_path / "cfgusers"

    def test_config_users_repo_default(self, shared: Path,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
        td = importlib.import_module("tenant_data")
        monkeypatch.delenv("AGENTOS_CONFIG_USERS_DIR", raising=False)

        assert td._default_config_users_base() == _REPO_ROOT / "config" / "users"


class TestTenantCallerConstruction:
    def test_missing_capability_yields_none(self, shared: Path) -> None:
        """能力未注入（get_capability 抛 KeyError）→ caller None（回退 default 租户）。"""
        td = importlib.import_module("tenant_data")

        class _Plugin:
            def get_capability(self, name: str) -> Any:
                raise KeyError(name)

        assert td.make_tenant_context_caller(_Plugin()) is None

    def test_injected_capability_strips_prefix(self, shared: Path) -> None:
        """能力已注入 → 构造出 caller（短方法名透传，句柄侧再拼命名空间）。"""
        td = importlib.import_module("tenant_data")
        seen: list[tuple[str, dict[str, Any]]] = []

        class _Handle:
            async def call(self, method: str, params: dict[str, Any],
                           timeout: float | None = None) -> Any:
                seen.append((method, params))
                return {"tenant_id": "t-9"}

        class _Plugin:
            def get_capability(self, name: str) -> Any:
                assert name == td.TENANT_CONTEXT_CAPABILITY
                return _Handle()

        caller = td.make_tenant_context_caller(_Plugin())
        assert caller is not None

        import asyncio

        assert asyncio.run(caller("get", {})) == {"tenant_id": "t-9"}
        assert seen == [("get", {})], "短方法名原样转交句柄（wire 前缀由句柄拼）"


class TestMigrateLegacyData:
    def test_existing_default_short_circuits(self, tmp_path: Path, shared: Path) -> None:
        td = importlib.import_module("tenant_data")
        (tmp_path / "default").mkdir()
        (tmp_path / "memory").mkdir()

        assert td.migrate_legacy_data_to_default(data_root=tmp_path) == []
        assert (tmp_path / "memory").is_dir(), "已迁移态下不得再移动 legacy 目录"

    def test_absent_base_returns_empty(self, tmp_path: Path, shared: Path) -> None:
        """数据根不存在 → 空列表（不创建 default/）。"""
        td = importlib.import_module("tenant_data")
        missing = tmp_path / "nodata"

        assert td.migrate_legacy_data_to_default(data_root=missing) == []
        assert not missing.exists()

    def test_moves_dirs_and_files_idempotently(self, tmp_path: Path, shared: Path) -> None:
        """首迁移动目录 + 散文件；第二次调用零移动（幂等）。"""
        td = importlib.import_module("tenant_data")
        (tmp_path / "memory").mkdir()
        (tmp_path / "memory" / "m.db").write_text("x", encoding="utf-8")
        (tmp_path / "diag.log").write_text("log", encoding="utf-8")

        first = td.migrate_legacy_data_to_default(data_root=tmp_path)
        second = td.migrate_legacy_data_to_default(data_root=tmp_path)

        assert sorted(first) == ["diag.log", "memory"]
        assert second == []
        assert (tmp_path / "default" / "memory" / "m.db").exists()
        assert (tmp_path / "default" / "diag.log").exists()

    def test_empty_base_yields_no_moves(self, tmp_path: Path, shared: Path) -> None:
        td = importlib.import_module("tenant_data")
        assert td.migrate_legacy_data_to_default(data_root=tmp_path) == []
        assert (tmp_path / "default").is_dir()


# ══════════════════════════ repo_anchor ══════════════════════════


class TestRepoAnchorResolve:
    def test_env_pointing_at_config_dir(self, tmp_path: Path, shared: Path,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
        """env 指向 <repo>/config（内含 kernel/ 子目录）→ 返回其父目录（仓库根）。"""
        ra = importlib.import_module("repo_anchor")
        ra.reset_cache()
        cfg = tmp_path / "repo" / "config"
        (cfg / "kernel").mkdir(parents=True)
        monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(cfg))

        assert ra.resolve_repo_root() == tmp_path / "repo"
        ra.reset_cache()

    def test_env_pointing_at_repo_root(self, tmp_path: Path, shared: Path,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
        """env 指向仓库根（内含 config/kernel）→ 原样返回仓库根。"""
        ra = importlib.import_module("repo_anchor")
        ra.reset_cache()
        root = tmp_path / "repo"
        (root / "config" / "kernel").mkdir(parents=True)
        monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(root))

        assert ra.resolve_repo_root() == root
        ra.reset_cache()

    def test_ancestor_probe_without_env(self, shared: Path,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
        """无 env → 祖先链找含 config/kernel 的目录（本仓布局命中仓库根）。"""
        ra = importlib.import_module("repo_anchor")
        ra.reset_cache()
        monkeypatch.delenv("AGENTOS_CONFIG_ROOT", raising=False)

        assert ra.resolve_repo_root() == _REPO_ROOT
        ra.reset_cache()

    def test_all_probes_miss_returns_none(self, tmp_path: Path, shared: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
        """env 无效且文件树无 config/kernel → None（无仓库锚，退回单根不报错）。"""
        ra = importlib.import_module("repo_anchor")
        ra.reset_cache()
        fake_dir = tmp_path / "deploy"
        fake_dir.mkdir()
        monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(tmp_path / "elsewhere"))
        monkeypatch.setattr(ra, "__file__", str(fake_dir / "repo_anchor.py"))

        assert ra.resolve_repo_root() is None
        # 结果缓存：二次调用不再探测，仍返回 None
        assert ra.resolve_repo_root() is None
        ra.reset_cache()

    def test_reset_cache_reprobes(self, tmp_path: Path, shared: Path,
                                  monkeypatch: pytest.MonkeyPatch) -> None:
        """缓存命中不重探测；reset_cache 后按当前 env 重解析。"""
        ra = importlib.import_module("repo_anchor")
        ra.reset_cache()
        root = tmp_path / "repo"
        (root / "config" / "kernel").mkdir(parents=True)
        monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(root))
        assert ra.resolve_repo_root() == root

        monkeypatch.delenv("AGENTOS_CONFIG_ROOT", raising=False)
        assert ra.resolve_repo_root() == root, "缓存命中：不重探测"

        ra.reset_cache()
        assert ra.resolve_repo_root() == _REPO_ROOT, "缓存清空后按新 env 重解析"
        ra.reset_cache()


class TestRepoReadVerdict:
    @pytest.fixture(autouse=True)
    def _real_repo(self, shared: Path) -> Any:
        ra = importlib.import_module("repo_anchor")
        ra.reset_cache()
        yield ra
        ra.reset_cache()

    def test_source_dir_allowed(self, _real_repo: Any) -> None:
        """仓库根内源码目录（kernel/plugins/tests）允许读：(True, None)。"""
        assert _real_repo.repo_read_verdict(_REPO_ROOT / "kernel" / "crates") == (True, None)
        assert _real_repo.repo_read_verdict(_REPO_ROOT / "tests" / "test_host_shared_gaps.py") == (
            True,
            None,
        )

    @pytest.mark.parametrize("denied", ["config", "data", "logs", ".git", "node_modules"])
    def test_denied_dirs_rejected_with_reason(self, _real_repo: Any, denied: str) -> None:
        """运行时/产物面拒绝读取：命中且原因含目录名与"拒绝"。"""
        hit, reason = _real_repo.repo_read_verdict(_REPO_ROOT / denied / "x.txt")

        assert hit is True
        assert reason is not None
        assert denied in reason and "拒绝" in reason

    def test_outside_repo_not_anchored(self, _real_repo: Any, tmp_path: Path) -> None:
        assert _real_repo.repo_read_verdict(tmp_path / "outside.txt") == (False, None)

    def test_no_anchor_returns_unresolved(self, tmp_path: Path, shared: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
        """无仓库锚（env 无效 + 文件树无 config/kernel）→ (False, None)：退回单根。"""
        ra = importlib.import_module("repo_anchor")
        ra.reset_cache()
        deploy = tmp_path / "deploy"
        deploy.mkdir()
        monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(tmp_path / "elsewhere"))
        monkeypatch.setattr(ra, "__file__", str(deploy / "repo_anchor.py"))

        assert ra.resolve_repo_root() is None
        assert ra.repo_read_verdict(_REPO_ROOT / "kernel") == (False, None), (
            "无锚时不得声称命中仓库（调用方继续走原单根拒绝路径）"
        )
        ra.reset_cache()


class TestRepoWalkPrune:
    @pytest.fixture(autouse=True)
    def _real_repo(self, shared: Path) -> Any:
        ra = importlib.import_module("repo_anchor")
        ra.reset_cache()
        yield ra
        ra.reset_cache()

    def test_root_inside_repo_yields_normcased_denied(self, _real_repo: Any) -> None:
        """搜索根在仓库内 → 返回全部拒绝目录的绝对路径（normcase 归一）。"""
        prune = _real_repo.repo_walk_prune(_REPO_ROOT)

        assert len(prune) == len(_real_repo.REPO_READ_DENIED_DIRS)
        assert os.path.normcase(str(_REPO_ROOT / "config")) in prune
        assert os.path.normcase(str(_REPO_ROOT / ".git")) in prune

    def test_root_outside_repo_yields_empty(self, _real_repo: Any, tmp_path: Path) -> None:
        assert _real_repo.repo_walk_prune(tmp_path / "other") == set()

    def test_no_anchor_yields_empty(self, tmp_path: Path, shared: Path,
                                    monkeypatch: pytest.MonkeyPatch) -> None:
        """无仓库锚 → 返回空剪枝集（调用方按普通工作区遍历）。"""
        ra = importlib.import_module("repo_anchor")
        ra.reset_cache()
        deploy = tmp_path / "deploy"
        deploy.mkdir()
        monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(tmp_path / "elsewhere"))
        monkeypatch.setattr(ra, "__file__", str(deploy / "repo_anchor.py"))

        assert ra.repo_walk_prune(_REPO_ROOT) == set()
        ra.reset_cache()


# ══════════════════════════ proc_tree ══════════════════════════


class _FakeProc:
    """psutil.Process 替身：脚本化 kill/children 行为（外部依赖替身）。"""

    def __init__(
        self,
        pid: int,
        kill_error: BaseException | None = None,
        children: list[Any] | None = None,
        children_error: BaseException | None = None,
    ) -> None:
        self.pid = pid
        self._kill_error = kill_error
        self._children = children or []
        self._children_error = children_error
        self.killed = 0

    def children(self, recursive: bool = False) -> list[Any]:
        if self._children_error is not None:
            raise self._children_error
        return list(self._children)

    def kill(self) -> None:
        self.killed += 1
        if self._kill_error is not None:
            raise self._kill_error


class _FakePsutil:
    """psutil 模块替身：Process/wait_procs 可脚本化（外部依赖替身）。

    异常层次复刻真实 psutil：AccessDenied/NoSuchProcess 均为 Error 子类
    （插件侧 ``except psutil.Error`` 收网依赖该层次）。
    """

    class Error(Exception):
        pass

    class NoSuchProcess(Error):  # noqa: N818 — 对齐 psutil 名
        pass

    class AccessDenied(Error):
        pass

    def __init__(
        self,
        parent: Any = None,
        process_error: BaseException | None = None,
        alive: list[Any] | None = None,
        wait_error: BaseException | None = None,
    ) -> None:
        self._parent = parent
        self._process_error = process_error
        self._alive = alive or []
        self._wait_error = wait_error
        self.last_wait: tuple[list[Any], float] | None = None

    def Process(self, pid: int) -> Any:  # noqa: N802 — psutil API 名
        if self._process_error is not None:
            raise self._process_error
        return self._parent

    def wait_procs(self, procs: list[Any], timeout: float) -> tuple[list[Any], list[Any]]:  # noqa: N802
        self.last_wait = (procs, timeout)
        if self._wait_error is not None:
            raise self._wait_error
        return ([], self._alive)


@pytest.fixture
def proc_tree(shared: Path) -> Any:
    """proc_tree 模块（psutil 为模块内延迟 import，按 sys.modules 注入替身）。"""
    return importlib.import_module("proc_tree")


class TestProcTreeKill:
    def test_nonpositive_pid_noop(self, proc_tree: Any) -> None:
        """pid <= 0（负值与 0）视为无可终止对象，不抛不失败。"""
        assert proc_tree.kill_process_tree(0) == []
        assert proc_tree.kill_process_tree(-5) == []

    def test_nonexistent_process_is_success(self, proc_tree: Any,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
        """目标进程已退出（NoSuchProcess）→ 空失败清单（终止目标已达成）。"""
        monkeypatch.setitem(
            sys.modules, "psutil", _FakePsutil(process_error=_FakePsutil.NoSuchProcess())
        )

        assert proc_tree.kill_process_tree(4242) == []

    def test_invalid_pid_value_error_is_success(self, proc_tree: Any,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
        """psutil 对非法 pid 抛 ValueError → 同"已退出"语义，空清单。"""
        monkeypatch.setitem(
            sys.modules, "psutil", _FakePsutil(process_error=ValueError("invalid pid"))
        )

        assert proc_tree.kill_process_tree(999999) == []

    def test_access_error_reported(self, proc_tree: Any,
                                   monkeypatch: pytest.MonkeyPatch) -> None:
        """访问进程失败（权限等）→ 失败清单带 pid 与原因。"""
        monkeypatch.setitem(
            sys.modules, "psutil", _FakePsutil(process_error=_FakePsutil.AccessDenied("denied"))
        )

        failures = proc_tree.kill_process_tree(77)

        assert len(failures) == 1
        assert "77" in failures[0] and "psutil 访问失败" in failures[0]
        assert "denied" in failures[0]

    def test_children_enumeration_error_recorded_but_kill_continues(
        self, proc_tree: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """子进程枚举失败 → 记失败清单，根进程照常杀（不中断收尾）。"""
        parent = _FakeProc(11, children_error=_FakePsutil.Error("boom"))
        monkeypatch.setitem(sys.modules, "psutil", _FakePsutil(parent=parent))

        failures = proc_tree.kill_process_tree(11)

        assert any("子进程枚举失败" in f and "11" in f for f in failures)
        assert parent.killed == 1, "枚举失败不得阻断根进程终止"

    def test_children_killed_leaf_first_and_missing_child_tolerated(
        self, proc_tree: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """叶→根杀：子进程先于根；杀中先退的子进程（NoSuchProcess）不算失败。"""
        order: list[str] = []

        class _Child(_FakeProc):
            def kill(self) -> None:
                order.append(f"child{self.pid}")
                super().kill()

        class _Parent(_FakeProc):
            def kill(self) -> None:
                order.append("parent")
                super().kill()

        gone = _Child(21, kill_error=_FakePsutil.NoSuchProcess())
        bad = _Child(22, kill_error=_FakePsutil.AccessDenied("nope"))
        good = _Child(23)
        # psutil children(recursive=True) 的实测顺序是根→叶，故杀序必须是其逆序
        parent = _Parent(20, children=[good, bad, gone])
        monkeypatch.setitem(sys.modules, "psutil", _FakePsutil(parent=parent))

        failures = proc_tree.kill_process_tree(20)

        assert order == ["child21", "child22", "child23", "parent"], "必须叶→根顺序终止"
        assert order[-1] == "parent", "根进程最后退（收窄杀过程中新子进程逃逸窗口）"
        assert any("pid=22 kill 失败" in f for f in failures)
        assert not any("pid=21" in f for f in failures), "杀中先退的子进程不算失败"
        assert good.killed == 1 and parent.killed == 1

    def test_parent_kill_error_recorded(self, proc_tree: Any,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
        parent = _FakeProc(30, kill_error=_FakePsutil.AccessDenied("locked"))
        monkeypatch.setitem(sys.modules, "psutil", _FakePsutil(parent=parent))

        failures = proc_tree.kill_process_tree(30)

        assert any("pid=30 kill 失败" in f for f in failures)

    def test_parent_already_gone_during_kill_is_success(
        self, proc_tree: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """根进程在杀的过程中先退（NoSuchProcess）→ 目标已达成，不进失败清单。"""
        parent = _FakeProc(31, kill_error=_FakePsutil.NoSuchProcess())
        monkeypatch.setitem(sys.modules, "psutil", _FakePsutil(parent=parent))

        failures = proc_tree.kill_process_tree(31)

        assert failures == []

    def test_alive_after_reap_reported(self, proc_tree: Any,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
        """收敛等待后仍存活 → 失败清单带存活 pid 与等待秒数。"""
        parent = _FakeProc(40)
        child = _FakeProc(39)
        survivor = _FakeProc(41)
        fake = _FakePsutil(parent=parent, alive=[survivor])
        parent._children = [child]
        monkeypatch.setitem(sys.modules, "psutil", fake)

        failures = proc_tree.kill_process_tree(40)

        assert len(failures) == 1
        assert "pid=41" in failures[0]
        assert f"{int(proc_tree._REAP_TIMEOUT_SECONDS)}s" in failures[0]
        assert fake.last_wait is not None
        procs, timeout = fake.last_wait
        assert procs == [child, parent], "收敛等待必须覆盖子树 + 根"
        assert timeout == proc_tree._REAP_TIMEOUT_SECONDS

    def test_wait_failure_recorded(self, proc_tree: Any,
                                   monkeypatch: pytest.MonkeyPatch) -> None:
        """收敛等待本身异常 → 记失败清单，不向上抛。"""
        parent = _FakeProc(50)
        monkeypatch.setitem(
            sys.modules, "psutil",
            _FakePsutil(parent=parent, wait_error=_FakePsutil.Error("wait boom")),
        )

        failures = proc_tree.kill_process_tree(50)

        assert any("终止收敛等待失败" in f and "wait boom" in f for f in failures)

    def test_clean_tree_yields_empty_and_kills_parent_only(
        self, proc_tree: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """健康路径：无子进程 → 恰杀根一次、无失败项。"""
        parent = _FakeProc(60)
        monkeypatch.setitem(sys.modules, "psutil", _FakePsutil(parent=parent))

        assert proc_tree.kill_process_tree(60) == []
        assert parent.killed == 1

    def test_taskkill_fallback_failure_recorded(
        self, proc_tree: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Windows 兜底 taskkill 调用异常 → 记失败清单并留痕（不吞）。"""
        parent = _FakeProc(70)
        monkeypatch.setitem(sys.modules, "psutil", _FakePsutil(parent=parent))

        def _boom(*_a: Any, **_k: Any) -> None:
            raise OSError("taskkill missing")

        monkeypatch.setattr(proc_tree.subprocess, "run", _boom)

        failures = proc_tree.kill_process_tree(70)

        if os.name == "nt":
            assert any("taskkill 兜底失败" in f for f in failures)
        else:
            assert failures == [], "非 Windows 不执行 taskkill 兜底"


# ══════════════════════════ user_space ══════════════════════════


class TestUserSpaceResolution:
    def test_env_override_wins_for_all_four_roots(
        self, tmp_path: Path, shared: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """四个 env 键各自覆盖对应根（优先级最高）。"""
        us = importlib.import_module("user_space")
        monkeypatch.setenv(us.USER_ROOT_ENV, str(tmp_path / "uroot"))
        monkeypatch.setenv(us.USER_CONFIG_DIR_ENV, str(tmp_path / "ucfg"))
        monkeypatch.setenv(us.USER_DATA_DIR_ENV, str(tmp_path / "udata"))
        monkeypatch.setenv(us.USER_PLUGINS_DIR_ENV, str(tmp_path / "uplugs"))

        assert us.user_root() == tmp_path / "uroot"
        assert us.user_config_dir() == tmp_path / "ucfg"
        assert us.user_data_dir() == tmp_path / "udata"
        assert us.user_plugins_dir() == tmp_path / "uplugs"

    @pytest.mark.parametrize("raw", ["", "   ", "\t\n"])
    def test_blank_env_treated_as_unset(self, raw: str, tmp_path: Path, shared: Path,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
        """空白 env（对齐 Rust trim().is_empty()）→ 视为未设，走 OS 目录推导。"""
        us = importlib.import_module("user_space")
        monkeypatch.setenv(us.USER_ROOT_ENV, raw)
        monkeypatch.setenv(us.USER_DATA_DIR_ENV, raw)

        root = us.user_root()
        assert root is None or str(root) != raw
        assert us._env_path(us.USER_ROOT_ENV) is None

    def test_root_derived_from_os_data_dir(self, tmp_path: Path, shared: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
        """无 env → <OS 数据目录>/agentos，四根同基（相对布局契约）。"""
        us = importlib.import_module("user_space")
        monkeypatch.delenv(us.USER_ROOT_ENV, raising=False)
        monkeypatch.delenv(us.USER_CONFIG_DIR_ENV, raising=False)
        monkeypatch.delenv(us.USER_DATA_DIR_ENV, raising=False)
        monkeypatch.delenv(us.USER_PLUGINS_DIR_ENV, raising=False)
        os_dir = tmp_path / "osdata"
        os_dir.mkdir()
        monkeypatch.setattr(us, "_os_data_dir", lambda: os_dir)

        root = us.user_root()
        assert root == os_dir / "agentos"
        assert us.user_config_dir() == root / "config"
        assert us.user_data_dir() == root / "data"
        assert us.user_plugins_dir() == root / "plugins"

    def test_os_data_dir_unavailable_propagates_none(self, shared: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
        """OS 数据目录不可得（极端环境）→ 各根返回 None 供调用方回退。"""
        us = importlib.import_module("user_space")
        for name in (us.USER_ROOT_ENV, us.USER_CONFIG_DIR_ENV, us.USER_DATA_DIR_ENV,
                     us.USER_PLUGINS_DIR_ENV):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setattr(us, "_os_data_dir", lambda: None)

        assert us.user_root() is None
        assert us.user_config_dir() is None
        assert us.user_data_dir() is None
        assert us.user_plugins_dir() is None

    def test_win32_appdata_and_absent_appdata(self, shared: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
        """Windows 取 %APPDATA%（Roaming）；缺失该变量时返回 None（非 LOCALAPPDATA 回落）。"""
        us = importlib.import_module("user_space")
        monkeypatch.setattr(us.sys, "platform", "win32")
        monkeypatch.setenv("APPDATA", r"C:\Users\u\AppData\Roaming")
        assert us._os_data_dir() == Path(r"C:\Users\u\AppData\Roaming")

        monkeypatch.delenv("APPDATA", raising=False)
        assert us._os_data_dir() is None

    def test_linux_xdg_override_and_default(self, shared: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
        """Linux：XDG_DATA_HOME 有值取之（strip 后）；未设回落 ~/.local/share。"""
        us = importlib.import_module("user_space")
        monkeypatch.setattr(us.sys, "platform", "linux")
        monkeypatch.setenv("XDG_DATA_HOME", "  /xdg/data  ")
        assert us._os_data_dir() == Path("/xdg/data")

        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        assert us._os_data_dir() == Path.home() / ".local" / "share"

        monkeypatch.setenv("XDG_DATA_HOME", "   ")
        assert us._os_data_dir() == Path.home() / ".local" / "share"

    def test_macos_app_support_dir(self, shared: Path,
                                   monkeypatch: pytest.MonkeyPatch) -> None:
        """macOS：~/Library/Application Support（对齐 dirs::data_dir）。"""
        us = importlib.import_module("user_space")
        monkeypatch.setattr(us.sys, "platform", "darwin")

        assert us._os_data_dir() == Path.home() / "Library" / "Application Support"


# ══════════════════════════ state_fields ══════════════════════════


class TestStateFields:
    def test_json_string_parsed_back_to_dict(self, shared: Path) -> None:
        """跨边界序列化形态（JSON 对象字符串）→ 还原为 dict（消费点契约）。"""
        sf = importlib.import_module("state_fields")
        assert sf.as_dict('{"a": 1, "b": {"c": 2}}', field="ws_meta") == {"a": 1, "b": {"c": 2}}
        assert sf.require_dict('  {"x": 1}', field="ws_meta") == {"x": 1}
        assert sf.optional_dict('{"y": 2}', field="opt") == {"y": 2}

    def test_native_dict_passes_through_identity(self, shared: Path) -> None:
        """原生 dict 原样返回（引擎内存边界零拷贝语义）。"""
        sf = importlib.import_module("state_fields")
        payload = {"k": "v"}
        assert sf.as_dict(payload, field="f") is payload

    def test_broken_json_required_raises(self, shared: Path) -> None:
        """required 位：JSON 字符串解析失败 → StateFieldError（fail-closed）。"""
        sf = importlib.import_module("state_fields")
        with pytest.raises(sf.StateFieldError, match="解析失败"):
            sf.as_dict('{"unclosed', field="task.acceptance_criteria", required=True)

    def test_broken_json_optional_degrades_to_none(self, shared: Path) -> None:
        """可选位：解析失败 → None + warning（不抛，不静默改语义）。"""
        sf = importlib.import_module("state_fields")
        assert sf.as_dict('{"unclosed', field="f") is None
        assert sf.optional_dict('{"unclosed', field="f") == {}

    def test_missing_required_error_mentions_field_and_type(self, shared: Path) -> None:
        """缺失/形态错：错误信息含字段名与实际类型（可排障）。"""
        sf = importlib.import_module("state_fields")
        with pytest.raises(sf.StateFieldError) as exc:
            sf.require_dict(None, field="track.llm_usage")

        assert "track.llm_usage" in str(exc.value)
        assert "NoneType" in str(exc.value)

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, {}),
            ("", {}),
            ([1, 2], {}),
            (123, {}),
            ("plain text", {}),
            ('["json", "array"]', {}),  # JSON 合法但非对象 → 形态非法
        ],
    )
    def test_optional_degrades_to_empty_dict(self, value: Any, expected: dict[str, Any],
                                             shared: Path) -> None:
        """可选位各种非法形态统一降级为空 dict（调用方零分支消费）。"""
        sf = importlib.import_module("state_fields")
        assert sf.optional_dict(value, field="f") == expected

    def test_value_none_and_empty_string_no_warning_path(self, shared: Path) -> None:
        """None/空串是"未写"语义，不产生形态告警（与 [1,2] 等形态错区分）。"""
        sf = importlib.import_module("state_fields")
        assert sf.as_dict(None, field="f") is None
        assert sf.as_dict("", field="f") is None
        assert sf.as_dict("   ", field="f") is None


# ══════════════════════════ bounded_dict ══════════════════════════


class TestBoundedDict:
    def test_max_entries_evicts_oldest(self, shared: Path) -> None:
        """超 MAX 逐出最旧（按 ts），新写入恒在且长度恒 ≤ MAX。"""
        bd = importlib.import_module("bounded_dict")
        now = {"t": 1000.0}
        d: Any = bd.BoundedDict(ttl_seconds=3600.0, max_entries=3, clock=lambda: now["t"])

        for i in range(6):
            now["t"] += 1.0
            d[f"k{i}"] = {"i": i}

        assert len(d) == 3
        assert "k5" in d and "k0" not in d
        assert [d[k]["i"] for k in d] == [3, 4, 5]

    def test_ttl_sweep_on_write(self, shared: Path) -> None:
        """写时清扫过期条目：超 TTL 的旧条目在下一次写入时消失。"""
        bd = importlib.import_module("bounded_dict")
        now = {"t": 1000.0}
        d: Any = bd.BoundedDict(ttl_seconds=10.0, max_entries=100, clock=lambda: now["t"])
        d["old"] = {"v": 1}
        now["t"] += 11.0
        d["new"] = {"v": 2}

        assert "old" not in d
        assert "new" in d

    def test_entry_gets_ts_stamp_without_mutating_caller(self, shared: Path) -> None:
        """写入盖 ts（副本），调用方传入的 dict 不被改动。"""
        bd = importlib.import_module("bounded_dict")
        now = {"t": 42.0}
        d: Any = bd.BoundedDict(ttl_seconds=3600.0, max_entries=10, clock=lambda: now["t"])
        source = {"payload": "x"}
        d["k"] = source

        assert d["k"]["ts"] == 42.0
        assert "ts" not in source

    def test_read_surface_matches_dict_semantics(self, shared: Path) -> None:
        """读面与内置 dict 一致：get/contains/迭代/删除/KeyError。"""
        bd = importlib.import_module("bounded_dict")
        d: Any = bd.BoundedDict(ttl_seconds=3600.0, max_entries=10, clock=lambda: 1.0)
        d["a"] = {"n": 1}
        d["b"] = {"n": 2}

        assert sorted(iter(d)) == ["a", "b"]
        assert len(d) == 2
        del d["a"]
        assert "a" not in d
        with pytest.raises(KeyError):
            _ = d["missing"]

    @pytest.mark.parametrize(
        ("ttl_env", "max_env", "expect_ttl", "expect_max"),
        [
            ("60", "5", 60.0, 5),
            ("abc", "xyz", 86400.0, 1024),   # 非数值 → 默认
            ("0", "0", 86400.0, 1024),       # <=0 → 默认（防禁用收敛）
            ("-3", "-9", 86400.0, 1024),
            ("", "", 86400.0, 1024),         # 空 → 默认
        ],
    )
    def test_env_overrides_with_invalid_fallback(
        self, ttl_env: str, max_env: str, expect_ttl: float, expect_max: int,
        shared: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """env 覆盖 TTL/MAX；非法值回落默认（不可被配置禁用收敛）。"""
        bd = importlib.import_module("bounded_dict")
        monkeypatch.setenv(bd._TTL_ENV, ttl_env)
        monkeypatch.setenv(bd._MAX_ENV, max_env)

        d: Any = bd.BoundedDict()
        d["k"] = {"v": 1}

        assert d._ttl == expect_ttl
        assert d._max == expect_max

    def test_explicit_params_beat_env(self, shared: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        bd = importlib.import_module("bounded_dict")
        monkeypatch.setenv(bd._TTL_ENV, "999")
        monkeypatch.setenv(bd._MAX_ENV, "999")

        d: Any = bd.BoundedDict(ttl_seconds=7.0, max_entries=2, clock=lambda: 1.0)
        assert (d._ttl, d._max) == (7.0, 2)


# ══════════════════════════ uploads_path ══════════════════════════


class TestUploadsPath:
    def test_env_dir_overrides_tenant_root(self, tmp_path: Path, shared: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
        """UPLOADS_DIR 最高优先级（存量部署覆盖），tenant_id 不再参与。"""
        up = importlib.import_module("uploads_path")
        monkeypatch.setenv("UPLOADS_DIR", str(tmp_path / "envup"))
        monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path / "data"))

        assert up.resolve_uploads_dir("acme") == tmp_path / "envup"

    def test_tenant_root_used_without_env(self, tmp_path: Path, shared: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
        """无 env → data/{tenant}/uploads；tenant_id None 用 default。"""
        up = importlib.import_module("uploads_path")
        monkeypatch.delenv("UPLOADS_DIR", raising=False)
        monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path / "data"))

        assert up.resolve_uploads_dir() == tmp_path / "data" / "default" / "uploads"
        assert up.resolve_uploads_dir("acme") == tmp_path / "data" / "acme" / "uploads"

    def test_non_uploads_reference_returns_none(self, shared: Path) -> None:
        """非 /uploads/ 形态（http URL、绝对路径、相对路径）→ None（调用方自处理）。"""
        up = importlib.import_module("uploads_path")
        assert up.resolve_uploads_url("https://x/a.png") is None
        assert up.resolve_uploads_url("D:/x/a.png") is None
        assert up.resolve_uploads_url("uploads/a.png") is None
        assert up.resolve_uploads_url("") is None

    @pytest.mark.parametrize(
        ("url", "expected_name"),
        [
            ("/uploads/a.png", "a.png"),
            ("/uploads/../secret.png", "secret.png"),      # 穿越形态被 basename 消解
            ("/uploads/nested/b.png", "b.png"),
            ("/uploads/..%2Fc.png", "..%2Fc.png"),          # 未解码：仍落在目录内
        ],
    )
    def test_basename_pinned_inside_uploads_dir(
        self, url: str, expected_name: str, tmp_path: Path, shared: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """解析结果恒在 uploads 目录内（穿越在形态层即被拒绝）。"""
        up = importlib.import_module("uploads_path")
        monkeypatch.setenv("UPLOADS_DIR", str(tmp_path / "up"))

        resolved = up.resolve_uploads_url(url)
        assert resolved is not None
        assert resolved.parent == tmp_path / "up"
        assert resolved.name == expected_name

    @pytest.mark.parametrize("url", ["/uploads/", "/uploads/.", "/uploads/.."])
    def test_degenerate_filenames_return_none(self, url: str, shared: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
        """空文件名与 . / .. 形态 → None（不指向目录本身）。"""
        up = importlib.import_module("uploads_path")
        assert up.resolve_uploads_url(url) is None


# ═══════════════════ 裸名自举行（sys.path 注入守卫） ═══════════════════


class TestBareNameBootstrap:
    """共享模块顶部的 sys.path 自举行：目录不在路径时自行注入。

    触发场景 = 插件经**文件路径**装载这些模块（sidecar/测试装配的
    spec_from_file_location 形态，与裸名 import 不同：装载本身不依赖
    sys.path），随后模块内的兄弟裸名 import（如 ``from tenant_data import``）
    要求目录在 sys.path 上——自举行就是这一步的兜底。车道已注入该目录，
    这里在摘除后按文件路径装载，验证守卫确实补位。
    """

    @pytest.mark.parametrize(
        ("module_file", "module_name", "sibling"),
        [
            ("project_registry.py", "boot_probe_project_registry", "ProjectRegistry"),
            ("tenant_data.py", "boot_probe_tenant_data", "tenant_data_root"),
            ("uploads_path.py", "boot_probe_uploads_path", "resolve_uploads_dir"),
        ],
    )
    def test_shared_root_reinserted_when_load_by_file_path(
        self, module_file: str, module_name: str, sibling: str,
        shared: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        d = str(shared)
        assert d in sys.path, "前置条件：车道已把 plugins/shared 注入 sys.path"
        monkeypatch.setattr(sys, "path", [p for p in sys.path if p != d])
        spec = importlib.util.spec_from_file_location(module_name, str(shared / module_file))
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod  # dataclass 字符串注解按 __module__ 反查
        try:
            spec.loader.exec_module(mod)  # 装载期执行自举行

            assert d in sys.path, "模块导入必须把 plugins/shared 推回 sys.path"
            assert hasattr(mod, sibling), "兄弟裸名 import 经自举行后可用"
        finally:
            sys.modules.pop(module_name, None)


# ═══════════════════ config 探针全失的兜底返回 ═══════════════════


class TestIsolationConfigPathFallback:
    def test_returns_package_relative_default_when_probes_miss(
        self, tmp_path: Path, shared: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """env 无效且 __file__ 祖先链无该配置文件 → 返回包相对路径兜底
        （路径形态仍指向 isolation_config.yaml，调用方读取失败走缺省值）。"""
        pr = importlib.import_module("project_registry")
        deploy = tmp_path / "deploy"
        deploy.mkdir()
        monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(tmp_path / "elsewhere" / "config"))
        monkeypatch.setattr(pr, "__file__", str(deploy / "project_registry.py"))

        resolved = pr._isolation_config_path()

        assert Path(resolved).name == "isolation_config.yaml"
        assert "plugins" in str(resolved) and "isolation" in str(resolved)


# ═══════════════════ rmtree 双签名分发 ═══════════════════


class TestRemoveProjectFolderSignatureDispatch:
    def _install_legacy_rmtree(self, monkeypatch: pytest.MonkeyPatch,
                               observed: dict[str, Any], hook: Any = None) -> None:
        """用签名不含 onexc 的 rmtree 替身复刻 3.11 协议（真实现经 _real 调用）。"""
        real = shutil.rmtree

        def _legacy_rmtree(path: Any, onerror: Any = None, **_kw: Any) -> None:
            observed["onerror"] = onerror
            if hook is not None:
                hook(onerror, path)
            real(path)

        monkeypatch.setattr(shutil, "rmtree", _legacy_rmtree)

    def test_legacy_onerror_fallback_removes_tree(
        self, tmp_path: Path, shared: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Python < 3.12（rmtree 无 onexc 参数）→ 走 onerror 回退分支且删除成功。

        本环境（3.12）恒走 onexc 分支，回退分支只在 3.11 及以下真实生效；
        以签名不含 onexc 的替身验证分发判据与回退闭包契约本身。
        """
        pr = importlib.import_module("project_registry")
        victim = tmp_path / "victim"
        victim.mkdir()
        (victim / "f.txt").write_text("x", encoding="utf-8")
        observed: dict[str, Any] = {}
        self._install_legacy_rmtree(monkeypatch, observed)

        assert pr.remove_project_folder(str(victim)) is True

        assert not victim.exists()
        assert callable(observed["onerror"]), "3.11 路径必须传入 onerror 回调"

    def test_legacy_onerror_unwraps_excinfo_value(
        self, tmp_path: Path, shared: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """回退闭包把 (type, value, tb) 的 value 位解包为待解锁路径。"""
        pr = importlib.import_module("project_registry")
        victim = tmp_path / "victim"
        victim.mkdir()
        ro = victim / "ro.txt"
        ro.write_text("x", encoding="utf-8")
        os.chmod(ro, 0o444)
        retried: list[str] = []

        def _drive(onerror: Any, path: Any) -> None:
            def _func(p: str) -> None:
                retried.append(p)

            onerror(_func, str(ro), (PermissionError, PermissionError("locked"), None))

        observed: dict[str, Any] = {}
        self._install_legacy_rmtree(monkeypatch, observed, hook=_drive)

        assert pr.remove_project_folder(str(victim)) is True

        assert retried == [str(ro)], "excinfo[1] 必须作为 path 参数传给重试函数"
        assert not victim.exists()


# ═══════════════════ 嵌套子目录模块的 sys.path 自举行 ═══════════════════


class TestNestedModuleBootstrap:
    """分组子目录模块顶部的 sys.path 自举行（coverage 靶行，file-path 装载语境）。

    与 TestBareNameBootstrap 同型，但靶模块嵌在子目录、自举靶不同：
    - system/scene/persistence.py:25、system/review/models.py:20、
      system/artifacts/models.py:22 —— 上溯两级自举 plugins/shared
      （模块内 time_iso/tenant_data 等共享裸模块 import 的路径前提）；
    - pipeline/input/prompt_build/server.py:21 —— 自举靶 = bootstrap 返回的
      group_root（插件目录父级 pipeline/input/；bootstrap 本身只注入插件
      目录与共享根、组根只返回不注入，见 ADR 2026-09-08-plugin-bootstrap-sink
      决策 1，兄弟插件裸名导入靠这一行补位）。

    真实输入下仅「靶根缺失」初态可达（经文件路径 spec 装载——装载本身不依赖
    sys.path——且靶根此前不在 sys.path）。逐例先按 normcase 摘除全部靶根
    条目，再以唯一模块名 fresh import，断言装载完成后靶根回到 sys.path
    （normcase 比对）且模块内裸名 import 可用；finally 恢复 sys.path
    （monkeypatch 整表替换自动还原）与 sys.modules（预逐出裸名回填、装载期
    新增的仓内模块逐出）。
    """

    _SYSTEM_DIR = _REPO_ROOT / "plugins" / "shared" / "system"
    _PIPELINE_INPUT_DIR = _SHARED_DIR / "pipeline" / "input"
    _SHARED_PREFIX = os.path.normcase(os.path.abspath(str(_SHARED_DIR))) + os.sep

    @classmethod
    def _restore_modules_snapshot(cls, modules_before: dict[str, Any]) -> None:
        """sys.modules 恢复：新增的仓内模块（plugins/shared 树下）逐出；
        被预逐出的既有条目回填。仓外新增（SDK/第三方/stdlib 缓存）保留。

        归属探测必须在任何逐出之前完成（此时父子模块都在场——命名空间包
        ``__path__`` 是懒重算的 _NamespacePath，父包摘除后再触碰会按模块名
        回查 sys.modules 而炸 KeyError）；逐出按子模块先于父包的序进行。
        """
        new_names = [n for n in sys.modules if n not in modules_before]
        project_owned: list[str] = []
        for name in new_names:
            mod = sys.modules.get(name)
            origin = getattr(mod, "__file__", None)
            if not origin:
                origin = next(iter(getattr(mod, "__path__", ()) or ()), None)
            if origin and os.path.normcase(os.path.abspath(origin)).startswith(
                cls._SHARED_PREFIX
            ):
                project_owned.append(name)
        for name in sorted(project_owned, key=lambda n: n.count("."), reverse=True):
            sys.modules.pop(name, None)
        for name, module in modules_before.items():
            if name not in sys.modules:
                sys.modules[name] = module

    @pytest.mark.parametrize(
        ("rel_file", "module_name", "target", "precondition_dir", "sibling_attrs"),
        [
            pytest.param(
                "system/scene/persistence.py",
                "boot_probe_scene_persistence",
                _SHARED_DIR,
                _SYSTEM_DIR,  # 自举行之前 scene.models 裸名 import 的组根前提
                ("ScenePersistence", "tenant_data_root"),
                id="scene-persistence",
            ),
            pytest.param(
                "system/review/models.py",
                "boot_probe_review_models",
                _SHARED_DIR,
                None,
                ("ReviewRequest", "_now_iso"),
                id="review-models",
            ),
            pytest.param(
                "system/artifacts/models.py",
                "boot_probe_artifacts_models",
                _SHARED_DIR,
                None,
                ("Artifact", "_now_iso"),
                id="artifacts-models",
            ),
            pytest.param(
                "pipeline/input/prompt_build/server.py",
                "boot_probe_prompt_build_server",
                _PIPELINE_INPUT_DIR,
                None,
                ("plugin", "PromptBuildPlugin"),
                id="prompt-build-server",
            ),
        ],
    )
    def test_target_root_reinserted_when_absent_before_file_path_load(
        self,
        rel_file: str,
        module_name: str,
        target: Path,
        precondition_dir: Path | None,
        sibling_attrs: tuple[str, ...],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        module_path = _SHARED_DIR / rel_file
        assert module_path.is_file(), "靶模块布局漂移：请核对覆盖缺口行归属"
        norm = os.path.normcase
        target_key = norm(str(target))

        desired_path = list(sys.path)
        if precondition_dir is not None:
            desired_path.insert(0, str(precondition_dir))
        desired_path = [p for p in desired_path if norm(p) != target_key]
        monkeypatch.setattr(sys, "path", desired_path)
        assert target_key not in {norm(p) for p in sys.path}, "前置：靶根已彻底摘除"

        modules_before = dict(sys.modules)
        sys.modules.pop("plugin", None)  # 裸名槽位防串扰（finally 回填）

        spec = importlib.util.spec_from_file_location(module_name, str(module_path))
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod
        try:
            spec.loader.exec_module(mod)  # 装载期执行自举行

            assert target_key in {norm(p) for p in sys.path}, (
                "模块导入必须把靶根推回 sys.path"
            )
            for attr in sibling_attrs:
                assert hasattr(mod, attr), f"自举行后的裸名 import 必须可用（缺 {attr}）"
        finally:
            sys.modules.pop(module_name, None)
            self._restore_modules_snapshot(modules_before)
