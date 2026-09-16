# @feature: FP-0.2.一 插件协议 | @ci: python-coverage
"""isolation 系统插件族（簇 E）分支缺口补测。

目标行语义对齐 HEAD 实测（仓库 coverage.xml 为 2026-09-14 02:18 生成，之后
81cc5195d/42bcd0ffb/c87a268f1/b84daeb7b/68890388d 等提交移动了行号；本文件
按 **当前 HEAD 行号** 覆盖，并逐条给出语义）。

分组覆盖（行号 = 当前 HEAD）：
1. manager.py 68-69：`_load_provider_config` 仓库 yaml 缺失 → 返回 {}（走默认）。
2. manager.py 398-399 / 482 / 601-602：wsl_native 后端三处跳过（容器恢复扫描 /
   镜像清理 / 停机清点）——wsl_native 无常驻进程，清点无意义。
3. manager.py 858-865：`_find_existing_container` 的 wsl_native 委托通路
   （委托 provider / provider 缺失返 None / 委托抛错吞掉按不存在处理）。
4. manager.py 1101-1106：`_destroy_container_by_name` 的 wsl_native 按名删档
   委托（provider 缺失静默返回 / 删除失败如实告警）。
5. decider.py 65-91：decide 不做可用性检查直返 / 级别不可用抛 IsolationError
   不降级 / 可用直返 / 缺键按不可用 / resolve 便捷通路 / policy_loader 属性。
6. workspace_lifecycle.py 可达分支（288-306 显式 plain 建目录）与非 plain
   返 None；332-342 的不可达证明见第 12 节。
7. checkpoint.py 107/130-131/146/344：盘符相对路径字面量拒绝、`_rel_or_none`
   越界 ValueError 出口、`_collect_workspace_files` 的 project_root 回退、
   `.git` 部件忽略。
8. approval.py 336-338/371-372：HOST 命令执行类叠加危险操作（风险分封顶 1.0）/
   非 HOST 检测到危险操作风险分累加 0.4。
9. hardware_profile.py 170/174：docker info 字段数异常、数值非正 → None。
10. wsl_health.py 275-276：保活会话 terminate 失败升级 kill、kill 再抛 OSError
    仅告警（幂等收尾不抛）。
11. sensitive_paths.py 58-59：`Path.resolve()` 抛 OSError/ValueError 时回退原
    字符串比较（坏路径不使判定崩溃）。
12. providers/docker_provider.py：op_type 分派（command/file_operation/未知）、
    撞名预检 `_start_one` 抛异常 → 存在性未知不推进 create（369-370）、
    Bridge env 注入（AGENTOS_BRIDGE_URL/TOKEN）。Bridge 两行在 HEAD 为
    653/656（旧快照语义行 572/575 经 difflib 对齐；旧快照生成时内容为
    `args.extend(["-e", f"AGENTOS_BRIDGE_URL=..."])`，对应 HEAD 653）。
13. providers/host_provider.py 32：`_SHARED_ROOT` 不在 sys.path 时插入一次，
    且 proc_tree 真实可导入；已在表内则不重复插入。

────────────────────────────────────────────────────────────
不可达残留（逐条说明，未用替身伪造可达性）
────────────────────────────────────────────────────────────
* **manager.py 1483-1531（`_repair_host_mount` 重挂主体）——不可达，且疑似真缺陷。**
  守卫式 `parts = PurePath(workspace).parts; if len(parts) < 2 or parts[0] != "/mnt"
  or len(parts[1]) != 1: return False`。`PurePath.parts` 的 [0] 元素是**锚**
  （POSIX 为 `"/"`，Windows 为 `"\\"` 或 `"C:\\"`），永不为 `"/mnt"`：Linux 上
  `PurePosixPath("/mnt/d/x").parts == ('/', 'mnt', 'd', 'x')`。故该守卫在**所有
  平台恒真**，1483 起全部行（含 mount_point/drive_letter、umount/mount 调用与
  四条返回出口）在生产中不可达，EIO 宿主挂载自愈静默失效（源码注释「本分支是
  WSL/Linux 专用修复」的预期模型与实际不符；正确判定应为 `parts[1] != "mnt"`）。
  仅当 monkeypatch 模块内 `PurePath` 为伪造语义才可"覆盖"，那是在测死代码的
  作者意图而非行为，故按要求保留缺行并在此说明。
* **workspace_lifecycle.py 332-342（显式 plain 空目录分支）——结构性死分支。**
  `_resolve_ws_mode` 输出穷尽三种：①显式 mode → 原样返回；②无显式 mode 且
  `_has_explicit_workspace` → default_mode（默认 worktree）；③其余 → "plain"。
  第 310 行 `if _ws_mode == "plain":`（**无 workspace 条件**）已承接全部 plain
  输出并 return，故第 331 行 `_ws_mode == "plain" and not has_explicit_workspace`
  中的左半永真不成立。历史同结论见提交 42bcd0ffb（"余 332-342 为结构性死分支"）。
  本文件用 `test_plain_without_explicit_returns_via_earlier_branch` 断言实际
  承接分支（mode="shared"），确认该组合不走 332-342。

隔离策略（对齐仓库铁律）：docker 边界一律用替身（fake provider / 伪 client /
subprocess 替身），不依赖真实 docker/容器；FS 用真实 tmp_path（唯一例外：
`_collect_workspace_files` 回退用例用 Path 子类注入 base 外路径，因真实 rglob
不可能产出 workspace 之外的条目）；时间不 sleep。文件放在 isolation 目录内，
自动随 BASE_TEST_PATHS 的 `plugins/shared/system/isolation/` 进插桩车道。

本文件落地后整条 plugins-coverage 车道实测（--cov=plugins）：
  approval/checkpoint/decider/hardware_profile/sensitive_paths/wsl_health/
  docker_provider/host_provider 缺行 = 0；
  manager.py 仅余 1483-1531（上述死分支，其 wsl_native 委托面 858-865、
  1101-1106 与跳过面 398-399/482/601-602 已清零）；
  workspace_lifecycle.py 仅余 332-342（上述结构性死分支）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from enum import Enum
from pathlib import Path
from typing import Any

import pytest

from agentos_plugin_sdk.isolation_types import (
    EnvironmentStatus,
    ExecutionResult,
    IsolationContext,
    IsolationEnvironment,
    IsolationLevel,
    TaskType,
)

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent  # plugins/shared/system/isolation/


def _load(name: str, rel: str) -> Any:
    """动态加载（唯一模块名，防裸名串扰——同目录既有测试同款）。"""
    if name in sys.modules:
        del sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _PLUGIN_DIR / rel)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _run(coro: Any) -> Any:
    """独立 loop（共享进程主 loop 可能已被关闭）。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _ctx(task_id: str = "t1", workspace: str | None = None) -> IsolationContext:
    ctx = IsolationContext(task_id=task_id, task_type=TaskType.ATOMIC)
    ctx.workspace = workspace
    return ctx


# ═══════════════════════════════════════════════════════════
# manager 伪依赖（仅 manager 测试类按需启用，不污染 provider 测试）
# ═══════════════════════════════════════════════════════════

_MANAGER_STUB_KEYS = (
    "decider",
    "providers",
    "providers.base",
    "providers.docker_provider",
    "providers.host_provider",
    "providers.wsl_native_provider",
    "hardware_profile",
    "tasks",
    "tasks.types",
)


def _install_manager_stubs() -> None:
    decider = types.ModuleType("decider")

    class IsolationUnrecoverableError(Exception):
        pass

    class IsolationDecider:
        def __init__(self, policy: Any | None = None) -> None:
            self._policy = policy

        async def decide(self, *args: Any, **kwargs: Any) -> Any:
            return self._policy

    decider.IsolationDecider = IsolationDecider
    decider.IsolationUnrecoverableError = IsolationUnrecoverableError
    sys.modules["decider"] = decider

    providers_pkg = types.ModuleType("providers")
    providers_pkg.__path__ = []
    sys.modules["providers"] = providers_pkg

    base_mod = types.ModuleType("providers.base")

    class IsolationProvider:
        pass

    base_mod.IsolationProvider = IsolationProvider
    sys.modules["providers.base"] = base_mod

    docker_mod = types.ModuleType("providers.docker_provider")

    class DockerProvider:
        @classmethod
        def _is_io_error(cls, err: Any) -> bool:
            return bool(err) and "input/output error" in str(err).lower()

    docker_mod.DockerProvider = DockerProvider
    sys.modules["providers.docker_provider"] = docker_mod

    host_mod = types.ModuleType("providers.host_provider")

    class HostProvider:
        pass

    host_mod.HostProvider = HostProvider
    sys.modules["providers.host_provider"] = host_mod

    wsl_mod = types.ModuleType("providers.wsl_native_provider")

    class WslNativeProvider:
        pass

    wsl_mod.WslNativeProvider = WslNativeProvider
    sys.modules["providers.wsl_native_provider"] = wsl_mod

    hp = types.ModuleType("hardware_profile")
    hp.get_resource_profile = lambda: {
        "max_environments": 3,
        "container_memory": "256m",
        "container_cpus": "0.25",
        "memory_swap": "256m",
        "pids_limit": 64,
        "max_concurrent_tasks": 3,
        "tier": "test",
    }
    sys.modules["hardware_profile"] = hp

    tasks_pkg = types.ModuleType("tasks")
    tasks_pkg.__path__ = []
    sys.modules["tasks"] = tasks_pkg

    class TaskStatus(str, Enum):
        RUNNING = "running"
        COMPLETED = "completed"

    tasks_types = types.ModuleType("tasks.types")
    tasks_types.TaskStatus = TaskStatus
    sys.modules["tasks.types"] = tasks_types


@pytest.fixture
def manager_stubs() -> Any:
    """按需安装 manager 伪依赖，测后恢复（不 autouse，避免污染 provider 直载）。"""
    saved = {k: sys.modules.get(k) for k in _MANAGER_STUB_KEYS}
    _install_manager_stubs()
    yield
    for key, value in saved.items():
        if value is None:
            sys.modules.pop(key, None)
        else:
            sys.modules[key] = value


def _load_manager() -> Any:
    return _load("isolation_manager_gaps_test", "manager.py")


def _make_manager(mod: Any) -> Any:
    decider_cls = sys.modules["decider"].IsolationDecider
    return mod.IsolationManager(
        providers={IsolationLevel.CONTAINER: object()},
        decider=decider_cls(None),
    )


# ═══════════════════════════════════════════════════════════
# 1. _load_provider_config yaml 缺失回退（68-69）
# ═══════════════════════════════════════════════════════════


class TestProviderConfigYamlFallback:
    def test_missing_repo_yaml_returns_empty(self, manager_stubs: None) -> None:
        """ConfigCenter 不可达 + 仓库 yaml 不存在 → 返回 {}（调用方走默认）。"""
        mod = _load_manager()

        # 运行时具体 Path 子类作动态基类（mypy 无法静态解析，misc）
        class _MissingYamlPath(type(Path())):  # type: ignore[misc]
            def is_file(self) -> bool:
                return False

        original = mod.Path
        try:
            mod.Path = _MissingYamlPath
            assert mod._load_provider_config() == {}
        finally:
            mod.Path = original

    def test_real_repo_yaml_returns_providers(self, manager_stubs: None) -> None:
        """对照组：仓库真实 yaml 在位 → 解析出 providers 段（非空且含 host）。"""
        mod = _load_manager()
        providers = mod._load_provider_config()
        assert isinstance(providers, dict)
        assert providers  # 真实仓库 yaml 应解析出非空 providers 段


# ═══════════════════════════════════════════════════════════
# 2/3/4. manager 的 wsl_native 委托与跳过分支（398-399/482/601-602/858-865/1101-1106）
# ═══════════════════════════════════════════════════════════


class _NamedEnvProvider:
    """duck-typed wsl_native provider：按名查找/销毁可脚本化。"""

    def __init__(self, found: Any = None, destroy_ok: bool = True) -> None:
        self._found = found
        self._destroy_ok = destroy_ok
        self.queried: list[str] = []
        self.destroyed: list[str] = []

    async def find_environment_by_name(self, name: str) -> Any:
        self.queried.append(name)
        if isinstance(self._found, Exception):
            raise self._found
        return self._found

    async def destroy_environment(self, env_id: str) -> bool:
        self.destroyed.append(env_id)
        return self._destroy_ok


def _wsl_native_manager(mod: Any, provider: Any | None) -> Any:
    mgr = _make_manager(mod)
    mgr._providers = {} if provider is None else {IsolationLevel.CONTAINER: provider}
    mgr._container_backend_is_wsl_native = lambda: True  # type: ignore[method-assign]
    return mgr


class TestManagerWslNativeDelegation:
    def test_find_existing_container_delegates(self, manager_stubs: None) -> None:
        """wsl_native：按名查找委托 provider（返回其环境对象）。"""
        mod = _load_manager()
        env = IsolationEnvironment(
            env_id="cua-ws-a",
            level=IsolationLevel.CONTAINER,
            provider_type="wsl_native",
            status=EnvironmentStatus.READY.value,
            context=_ctx(),
        )
        provider = _NamedEnvProvider(found=env)
        mgr = _wsl_native_manager(mod, provider)
        assert _run(mgr._find_existing_container("cua-ws-a")) is env
        assert provider.queried == ["cua-ws-a"]

    def test_find_existing_container_swallows_provider_error(self, manager_stubs: None) -> None:
        """委托抛错 → 吞掉返 None（上层按不存在处理，create 自带防撞名保护）。"""
        mod = _load_manager()
        provider = _NamedEnvProvider(found=RuntimeError("wsl unreachable"))
        mgr = _wsl_native_manager(mod, provider)
        assert _run(mgr._find_existing_container("cua-ws-b")) is None

    def test_find_existing_container_without_provider(self, manager_stubs: None) -> None:
        """后端为 wsl_native 但 CONTAINER 槽无 provider → None，不抛。"""
        mod = _load_manager()
        mgr = _wsl_native_manager(mod, None)
        assert _run(mgr._find_existing_container("cua-ws-c")) is None

    def test_destroy_by_name_success(self, manager_stubs: None) -> None:
        """按名销毁：委托 provider 删档，成功时不额外告警。"""
        mod = _load_manager()
        provider = _NamedEnvProvider(destroy_ok=True)
        mgr = _wsl_native_manager(mod, provider)
        _run(mgr._destroy_container_by_name("ws-a"))
        assert provider.destroyed == [f"{mgr.CONTAINER_NAME_PREFIX}ws-a"]

    def test_destroy_by_name_failure_still_returns(self, manager_stubs: None) -> None:
        """删除失败（provider 返 False）→ 如实返回（不抛、不谎报）。"""
        mod = _load_manager()
        provider = _NamedEnvProvider(destroy_ok=False)
        mgr = _wsl_native_manager(mod, provider)
        _run(mgr._destroy_container_by_name("ws-b"))
        assert provider.destroyed == [f"{mgr.CONTAINER_NAME_PREFIX}ws-b"]

    def test_destroy_by_name_without_provider_is_noop(self, manager_stubs: None) -> None:
        """无 provider → 直接返回（不触 docker 通路）。"""
        mod = _load_manager()
        mgr = _wsl_native_manager(mod, None)
        _run(mgr._destroy_container_by_name("ws-c"))

    def test_lifecycle_scans_skipped_on_wsl_native(self, manager_stubs: None) -> None:
        """wsl_native 无容器可恢复/清理/停机：三处扫描直接返回（不触 docker）。"""
        mod = _load_manager()
        mgr = _wsl_native_manager(mod, _NamedEnvProvider())

        def _forbidden(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("wsl_native 后端不应调用 docker 通路")

        mgr._run_docker_sync = _forbidden  # type: ignore[method-assign]
        _run(mgr._resume_containers())
        _run(mgr._stop_containers())
        _run(mgr._prune_docker_images())


# ═══════════════════════════════════════════════════════════
# 5. decider（65-91）
# ═══════════════════════════════════════════════════════════


class _FakeLoader:
    def __init__(self, policy: Any) -> None:
        self._policy = policy
        self.calls: list[tuple[str, str | None]] = []

    def resolve(self, tool_name: str, category: str | None = None) -> Any:
        self.calls.append((tool_name, category))
        return self._policy


def _policy(level: IsolationLevel, execution: str = "command_in_container") -> Any:
    from agentos_plugin_sdk.isolation_policy import ToolIsolationPolicy

    return ToolIsolationPolicy(isolation=level, execution=execution)


class TestIsolationDecider:
    def _decider(self, level: IsolationLevel) -> tuple[Any, Any]:
        mod = _load("isolation_decider_gaps_test", "decider.py")
        loader = _FakeLoader(_policy(level))
        return mod, mod.IsolationDecider(policy_loader=loader)

    def test_decide_without_availability_returns_policy(self) -> None:
        """available_providers=None → 跳过可用性检查，返回策略并透传参数。"""
        mod, decider = self._decider(IsolationLevel.HOST)
        got = _run(decider.decide("bash_execute", "shell"))
        assert got.isolation is IsolationLevel.HOST
        assert decider.policy_loader.calls == [("bash_execute", "shell")]

    @pytest.mark.parametrize("level", [IsolationLevel.HOST, IsolationLevel.CONTAINER])
    def test_decide_unavailable_raises_isolation_error(self, level: IsolationLevel) -> None:
        """配置级别不可用 → IsolationError（点名工具与级别，不降级）。"""
        mod, decider = self._decider(level)
        with pytest.raises(mod.IsolationError) as exc:
            _run(decider.decide("some_tool", available_providers={level: False}))
        assert "some_tool" in str(exc.value)
        assert level.value in str(exc.value)

    @pytest.mark.parametrize("level", [IsolationLevel.HOST, IsolationLevel.CONTAINER])
    def test_decide_available_returns_policy(self, level: IsolationLevel) -> None:
        """级别可用 → 返回该策略（两级均有区分度）。"""
        mod, decider = self._decider(level)
        got = _run(decider.decide("some_tool", available_providers={level: True}))
        assert got.isolation is level

    def test_decide_missing_level_key_treated_unavailable(self) -> None:
        """可用性表缺该级别键 → fail-closed 视为不可用（不静默放行）。"""
        mod, decider = self._decider(IsolationLevel.HOST)
        with pytest.raises(mod.IsolationError):
            _run(decider.decide("t", available_providers={IsolationLevel.CONTAINER: True}))

    def test_resolve_skips_availability_check(self) -> None:
        """resolve 便捷通路：直取策略（不做可用性检查），保留 execution 语义。"""
        mod = _load("isolation_decider_gaps_test", "decider.py")
        loader = _FakeLoader(_policy(IsolationLevel.CONTAINER, "host_direct"))
        decider = mod.IsolationDecider(policy_loader=loader)
        got = decider.resolve("task_submit", "host")
        assert got.execution == "host_direct"
        assert loader.calls == [("task_submit", "host")]

    def test_policy_loader_property_returns_injected_instance(self) -> None:
        """policy_loader 属性暴露注入实例（同一性可观察）。"""
        mod = _load("isolation_decider_gaps_test", "decider.py")
        loader = _FakeLoader(_policy(IsolationLevel.CONTAINER))
        assert mod.IsolationDecider(policy_loader=loader).policy_loader is loader


# ═══════════════════════════════════════════════════════════
# 6. workspace_lifecycle（可达分支 + 死分支证明）
# ═══════════════════════════════════════════════════════════


def _make_lifecycle(mod: Any, tmp_path: Path) -> Any:
    mgr = mod.WorkspaceLifecycleManager.__new__(mod.WorkspaceLifecycleManager)
    mgr._config = {}
    mgr._ws_meta_store = {}
    mgr._base_path = tmp_path
    mgr._merge_locks = {}
    mgr._main_branch = ""
    return mgr


class TestStartPlainRootBranches:
    def test_explicit_plain_with_workspace_creates_dir(self, tmp_path: Path) -> None:
        """显式 plain + 显式 workspace：目录按需创建并写入 ws_meta。"""
        mod = _load("isolation_ws_lifecycle_gaps_test", "workspace_lifecycle.py")
        mgr = _make_lifecycle(mod, tmp_path)
        target = tmp_path / "explicit" / "ws"
        meta = mod.WorkspaceLifecycleManager._start_plain_root(
            mgr, "t1", str(target), {"workspace_mode": "plain", "_has_explicit_workspace": True}
        )
        assert meta is not None
        assert meta["mode"] == "plain"
        assert meta["path"] == str(target)
        assert Path(meta["path"]).is_dir()
        assert mgr._ws_meta_store["t1"] == meta

    def test_plain_without_workspace_uses_scenario_root(self, tmp_path: Path) -> None:
        """无显式 workspace 的 plain：由场景检测目录承接（mode=shared）。"""
        mod = _load("isolation_ws_lifecycle_gaps_test", "workspace_lifecycle.py")
        mgr = _make_lifecycle(mod, tmp_path)
        scenario_root = tmp_path / "proj"

        def _detect(ws: str, td: dict) -> tuple[str, str]:
            return "new_project", str(scenario_root)

        mgr._detect_scenario = _detect  # type: ignore[method-assign]
        meta = mod.WorkspaceLifecycleManager._start_plain_root(mgr, "t2", "", {})
        assert meta is not None
        assert meta["mode"] == "shared"
        assert Path(meta["path"]) == scenario_root

    def test_non_plain_without_workspace_returns_none(self, tmp_path: Path) -> None:
        """非 plain 且无显式 workspace → None（交 worktree 路径）。"""
        mod = _load("isolation_ws_lifecycle_gaps_test", "workspace_lifecycle.py")
        mgr = _make_lifecycle(mod, tmp_path)
        meta = mod.WorkspaceLifecycleManager._start_plain_root(mgr, "t3", "", {"workspace_mode": "worktree"})
        assert meta is None

    def test_plain_without_explicit_returns_via_earlier_branch(self, tmp_path: Path) -> None:
        """死分支证明：(plain, 无显式) 组合在第 310 行分支即返回，不走 332-342。"""
        mod = _load("isolation_ws_lifecycle_gaps_test", "workspace_lifecycle.py")
        mgr = _make_lifecycle(mod, tmp_path)
        scenario_root = tmp_path / "proj3"

        def _detect(ws: str, td: dict) -> tuple[str, str]:
            return "new_project", str(scenario_root)

        mgr._detect_scenario = _detect  # type: ignore[method-assign]
        # _resolve_ws_mode 对 (无显式 mode, 无显式 workspace) 输出 "plain"
        resolved = mod.WorkspaceLifecycleManager._resolve_ws_mode(mgr, {})
        assert resolved == "plain"
        # 该组合下实际由 310 行分支承接：mode="shared"（而非 332 的 "plain"）
        meta = mod.WorkspaceLifecycleManager._start_plain_root(mgr, "t4", "", {})
        assert meta is not None
        assert meta["mode"] == "shared"


# ═══════════════════════════════════════════════════════════
# 7. checkpoint（107/130-131/146/344）
# ═══════════════════════════════════════════════════════════


class TestCheckpointPathGaps:
    def _manager(self, tmp_path: Path) -> Any:
        mod = _load("isolation_checkpoint_gaps_test", "checkpoint.py")
        return mod, mod.CheckpointManager(str(tmp_path))

    @pytest.mark.parametrize("bad", ["C:windows\\system32", "c:tmp\\x", "D:secret"])
    def test_drive_relative_path_rejected(self, tmp_path: Path, bad: str) -> None:
        """盘符相对路径（is_absolute()=False）由盘符字面量规则补判拒绝。"""
        mod, mgr = self._manager(tmp_path)
        with pytest.raises(ValueError, match="非法路径"):
            mgr.create_checkpoint("t1", "ws", files_to_backup=[bad])

    def test_drive_literal_not_safe_relative(self) -> None:
        """_is_safe_relative_path 同款规则：盘符前缀不安全，普通相对路径安全。"""
        mod = _load("isolation_checkpoint_gaps_test", "checkpoint.py")
        checker = mod.CheckpointManager._is_safe_relative_path
        assert checker("C:evil") is False
        assert checker("c:/evil") is False
        assert checker("src/app.py") is True
        assert checker("./src/app.py") is True

    def test_rel_or_none_outside_base(self, tmp_path: Path) -> None:
        """`_rel_or_none`：不在 base 子树下 → ValueError 出口返 None。"""
        mod = _load("isolation_checkpoint_gaps_test", "checkpoint.py")
        base = tmp_path / "base"
        base.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("x", encoding="utf-8")
        assert mod.CheckpointManager._rel_or_none(outside, base) is None
        inside = base / "in.txt"
        inside.write_text("y", encoding="utf-8")
        assert mod.CheckpointManager._rel_or_none(inside, base) == "in.txt"

    def test_collect_falls_back_to_project_root(self, tmp_path: Path) -> None:
        """rglob 产出 workspace 之外、project_root 之下的文件 → 回退按项目根算相对路径。"""
        mod, mgr = self._manager(tmp_path)
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "inner.txt").write_text("i", encoding="utf-8")
        shared = tmp_path / "shared.txt"
        shared.write_text("s", encoding="utf-8")

        ws_extra = type(ws)(str(ws))

        # 运行时具体 Path 子类作动态基类（mypy 无法静态解析，misc）
        class _Widened(type(ws)):  # type: ignore[misc]
            def rglob(self, pattern: str) -> Any:
                yield from super().rglob(pattern)
                yield shared

        widened = _Widened(str(ws))
        assert isinstance(ws_extra, type(ws))
        collected = {c.replace("\\", "/") for c in mgr._collect_workspace_files(widened)}
        assert collected == {"inner.txt", "shared.txt"}

    @pytest.mark.parametrize(
        ("rel", "expected"),
        [
            (".git/config", True),
            (".gitignore", True),
            ("__pycache__/c.pyc", True),
            ("node_modules/lib.js", True),
            ("src/main.py", False),
        ],
    )
    def test_should_ignore_rules(self, tmp_path: Path, rel: str, expected: bool) -> None:
        """忽略规则族（.git/隐藏/缓存/依赖目录）与放行对照组。"""
        mod, mgr = self._manager(tmp_path)
        assert mgr._should_ignore(tmp_path / rel) is expected

    def test_git_dir_content_not_backed_up(self, tmp_path: Path) -> None:
        """端到端（真实 FS）：.git 内文件不进备份，同层源码进备份。"""
        mod, mgr = self._manager(tmp_path)
        (tmp_path / "ws" / ".git").mkdir(parents=True)
        (tmp_path / "ws" / ".git" / "config").write_text("[core]", encoding="utf-8")
        (tmp_path / "ws" / "src.py").write_text("print(1)", encoding="utf-8")
        cp = mgr.create_checkpoint("t-git", "ws")
        assert [f.original_path for f in cp.files] == ["src.py"]


# ═══════════════════════════════════════════════════════════
# 8. approval（336-338/371-372）
# ═══════════════════════════════════════════════════════════


class _ToolDef:
    """工具定义替身：仅暴露 DangerChecker 读取的 dangerous_operations。"""

    def __init__(self, ops: list[str]) -> None:
        self.dangerous_operations = ops


def _approval_ctx(
    mod: Any,
    *,
    level: IsolationLevel,
    execution: str,
    command: str,
    ops: list[str] | None = None,
) -> Any:
    from agentos_plugin_sdk.isolation_policy import ToolIsolationPolicy

    return mod.ApprovalContext(
        tool_name="bash_execute",
        tool_definition=_ToolDef(ops if ops is not None else ["rm -rf"]),
        inputs={"command": command},
        isolation_level=level,
        policy=ToolIsolationPolicy(isolation=level, execution=execution),
    )


class TestApprovalEngineDangerBranches:
    def _engine(self) -> Any:
        mod = _load("isolation_approval_gaps_test", "approval.py")
        return mod, mod.ApprovalDecisionEngine()

    def test_host_command_with_dangerous_op_caps_risk(self) -> None:
        """HOST 命令执行类 + 危险操作 → 风险分封顶 1.0，双因子并明示危险操作名。"""
        mod, engine = self._engine()
        decision = _run(
            engine.decide(
                _approval_ctx(
                    mod,
                    level=IsolationLevel.HOST,
                    execution="command_in_container",
                    command="rm -rf /tmp/x",
                )
            )
        )
        assert decision.requires_approval is True
        assert decision.risk_score == 1.0
        assert decision.risk_factors == ["HOST_MODE", "DANGEROUS_OPERATION"]
        assert "rm -rf" in decision.reason
        assert decision.details["dangerous_operation"] == "rm -rf"

    def test_host_command_without_dangerous_op(self) -> None:
        """对照组：同级无危险操作 → COMMAND_EXECUTION 因子、风险分 0.9。"""
        mod, engine = self._engine()
        decision = _run(
            engine.decide(
                _approval_ctx(
                    mod,
                    level=IsolationLevel.HOST,
                    execution="command_in_container",
                    command="ls -la",
                )
            )
        )
        assert decision.requires_approval is True
        assert decision.risk_score == 0.9
        assert decision.risk_factors == ["HOST_MODE", "COMMAND_EXECUTION"]
        assert decision.details["dangerous_operation"] is None

    @pytest.mark.parametrize(
        ("command", "dangerous"),
        [("rm -rf /", True), ("echo hi", False)],
    )
    def test_non_host_risk_score_reflects_danger(self, command: str, dangerous: bool) -> None:
        """非 HOST：检测到危险操作 → 风险分 +0.4 且带因子；否则 0 分无因子。"""
        mod, engine = self._engine()
        decision = _run(
            engine.decide(
                _approval_ctx(
                    mod,
                    level=IsolationLevel.CONTAINER,
                    execution="command_in_container",
                    command=command,
                )
            )
        )
        assert decision.requires_approval is False
        assert decision.decision_type == "AUTO_APPROVED"
        expected = pytest.approx(0.4) if dangerous else 0.0
        assert decision.risk_score == expected
        assert ("DANGEROUS_OPERATION" in decision.risk_factors) is dangerous


# ═══════════════════════════════════════════════════════════
# 9. hardware_profile（170/174）
# ═══════════════════════════════════════════════════════════


class _HwCompleted:
    def __init__(self, returncode: int, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


class TestDockerHostResourcesParsing:
    @pytest.mark.parametrize("stdout", ["12345", "1 2 3", ""])
    def test_malformed_field_count_returns_none(self, monkeypatch: pytest.MonkeyPatch, stdout: str) -> None:
        """docker info 输出字段数 != 2 → None（不猜测缺失字段）。"""
        mod = _load("isolation_hw_profile_gaps_test", "hardware_profile.py")

        def _fake_run(*args: Any, _s: str = stdout, **kwargs: Any) -> _HwCompleted:
            return _HwCompleted(0, _s)

        monkeypatch.setattr(mod.subprocess, "run", _fake_run)
        assert mod._read_docker_host_resources() is None

    @pytest.mark.parametrize("stdout", ["0 4", "-1 4", "1024 0", "1024 -8"])
    def test_non_positive_values_return_none(self, monkeypatch: pytest.MonkeyPatch, stdout: str) -> None:
        """字段数正确但内存/CPU 非正 → None（拒绝不可用配额）。"""
        mod = _load("isolation_hw_profile_gaps_test", "hardware_profile.py")

        def _fake_run(*args: Any, _s: str = stdout, **kwargs: Any) -> _HwCompleted:
            return _HwCompleted(0, _s)

        monkeypatch.setattr(mod.subprocess, "run", _fake_run)
        assert mod._read_docker_host_resources() is None

    @pytest.mark.parametrize(("stdout", "gb", "ncpu"), [("8589934592 6", 8.0, 6), ("2147483648 2", 2.0, 2)])
    def test_valid_output_scales_correctly(
        self, monkeypatch: pytest.MonkeyPatch, stdout: str, gb: float, ncpu: int
    ) -> None:
        """对照组：合法输出 → (GiB, ncpu)，两值均为正且量纲随输入线性（性质断言）。"""
        mod = _load("isolation_hw_profile_gaps_test", "hardware_profile.py")

        def _fake_run(*args: Any, _s: str = stdout, **kwargs: Any) -> _HwCompleted:
            return _HwCompleted(0, _s)

        monkeypatch.setattr(mod.subprocess, "run", _fake_run)
        got = mod._read_docker_host_resources()
        assert got is not None
        got_gb, got_ncpu = got
        assert got_gb == pytest.approx(gb)
        assert got_ncpu == ncpu
        assert got_gb > 0
        assert got_ncpu > 0


# ═══════════════════════════════════════════════════════════
# 10. wsl_health 保活终止升级（275-276）
# ═══════════════════════════════════════════════════════════


class _KeepaliveProc:
    def __init__(
        self,
        terminate_error: Exception | None = None,
        kill_error: Exception | None = None,
    ) -> None:
        self._terminate_error = terminate_error
        self._kill_error = kill_error
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        if self._terminate_error:
            raise self._terminate_error
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def kill(self) -> None:
        if self._kill_error:
            raise self._kill_error
        self.killed = True


class TestTerminateKeepaliveEscalation:
    def test_clean_terminate_clears_handle_and_is_idempotent(self) -> None:
        """正常终止：terminate 生效、不升级 kill，句柄清空且二次调用无副作用。"""
        mod = _load("isolation_wsl_health_gaps_test", "wsl_health.py")
        proc = _KeepaliveProc()
        mod._keepalive_proc = proc
        mod.terminate_keepalive()
        assert proc.terminated is True
        assert proc.killed is False
        assert mod._keepalive_proc is None
        mod.terminate_keepalive()  # 幂等：无句柄直接返回

    @pytest.mark.parametrize("kill_error", [OSError("process already exited"), FileNotFoundError("gone")])
    def test_kill_oserror_swallowed(self, kill_error: OSError) -> None:
        """terminate 失败升级 kill；kill 抛 OSError/子类 → 仅告警不上抛。"""
        mod = _load("isolation_wsl_health_gaps_test", "wsl_health.py")
        proc = _KeepaliveProc(terminate_error=RuntimeError("terminate denied"), kill_error=kill_error)
        mod._keepalive_proc = proc
        mod.terminate_keepalive()
        assert mod._keepalive_proc is None

    def test_kill_succeeds_after_terminate_failure(self) -> None:
        """对照组：terminate 失败但 kill 成功 → 进程被强杀且句柄清空。"""
        mod = _load("isolation_wsl_health_gaps_test", "wsl_health.py")
        proc = _KeepaliveProc(terminate_error=RuntimeError("terminate timeout"))
        mod._keepalive_proc = proc
        mod.terminate_keepalive()
        assert proc.killed is True
        assert mod._keepalive_proc is None

    def test_no_handle_is_noop(self) -> None:
        """无保活句柄（未拉起过）→ 直接返回，on_unload 可无条件调用。"""
        mod = _load("isolation_wsl_health_gaps_test", "wsl_health.py")
        mod._keepalive_proc = None
        mod.terminate_keepalive()


# ═══════════════════════════════════════════════════════════
# 11. sensitive_paths 坏路径回退（58-59）
# ═══════════════════════════════════════════════════════════


class TestSensitivePathResolveFailure:
    def test_embedded_null_does_not_crash(self) -> None:
        """Path.resolve 抛 ValueError（内嵌 NUL）→ 回退原字符串比较，判定不崩。"""
        mod = _load("isolation_sensitive_paths_gaps_test", "sensitive_paths.py")
        hit, prefix = mod.is_sensitive_path("/etc/passwd\x00evil")
        assert isinstance(hit, bool)
        assert isinstance(prefix, str)

    def test_fallback_raw_string_still_matches_blacklist(self) -> None:
        """回退字符串仍参与黑名单比对：含敏感前缀的坏路径命中（返回该平台前缀之一）。"""
        mod = _load("isolation_sensitive_paths_gaps_test", "sensitive_paths.py")
        blacklist = mod.SENSITIVE_DIRS_WINDOWS if sys.platform == "win32" else mod.SENSITIVE_DIRS_LINUX
        bad = "c:/windows/system32\x00x" if sys.platform == "win32" else "/etc\x00x"
        hit, prefix = mod.is_sensitive_path(bad)
        assert hit is True
        assert prefix in blacklist
        assert bad.lower().replace("\\", "/").startswith(prefix)

    def test_wellformed_paths_unaffected_by_fallback(self, tmp_path: Path) -> None:
        """对照组：合法路径走 resolve 通路，空路径/项目内路径判定为不敏感。"""
        mod = _load("isolation_sensitive_paths_gaps_test", "sensitive_paths.py")
        assert mod.is_sensitive_path("") == (False, "")
        assert mod.is_sensitive_path(str(tmp_path / "proj" / "src"))[0] is False


# ═══════════════════════════════════════════════════════════
# 12. docker_provider 分派 / 存在性未知 / Bridge 注入
# ═══════════════════════════════════════════════════════════


@pytest.fixture
def real_providers() -> Any:
    """确保 providers.* / wsl_health 解析到真实模块（清掉其它测试装的伪模块）。"""
    saved: dict[str, Any] = {}
    keys = ("providers", "providers.base", "providers.docker_provider", "wsl_health")
    for key in keys:
        saved[key] = sys.modules.pop(key, None)
    added = str(_PLUGIN_DIR) not in sys.path
    if added:
        sys.path.insert(0, str(_PLUGIN_DIR))
    yield
    for key in keys:
        sys.modules.pop(key, None)
    for key, value in saved.items():
        if value is not None:
            sys.modules[key] = value
    if added and str(_PLUGIN_DIR) in sys.path:
        sys.path.remove(str(_PLUGIN_DIR))


def _docker_provider() -> Any:
    mod = _load("isolation_docker_provider_gaps_test", "providers/docker_provider.py")
    return mod, mod.DockerProvider({})


def _registered_env(provider: Any, container_id: str | None) -> str:
    info: dict[str, Any] = {} if container_id is None else {"container_id": container_id}
    env = IsolationEnvironment(
        env_id="cua-ws-a",
        level=IsolationLevel.CONTAINER,
        provider_type="docker",
        status=EnvironmentStatus.READY.value,
        context=_ctx(),
        provider_info=info,
    )
    provider._environments["cua-ws-a"] = env
    return "cua-ws-a"


class TestDockerExecuteDispatch:
    def test_command_type_uses_command_path(self, real_providers: None, monkeypatch: pytest.MonkeyPatch) -> None:
        """op_type="command" → 命令执行通路（返回其执行结果）。"""
        mod, provider = _docker_provider()
        env_id = _registered_env(provider, "cid-1")
        sentinel = ExecutionResult(success=True, output={"stdout": "ok"}, metadata={})
        seen: list[tuple[str, dict]] = []

        async def _exec(cid: str, op: dict) -> Any:
            seen.append((cid, op))
            return sentinel

        monkeypatch.setattr(provider, "_exec_in_container", _exec)
        op = {"type": "command", "command": "echo hi"}
        assert _run(provider.execute_in_environment(env_id, op)) is sentinel
        assert seen == [("cid-1", op)]

    def test_file_operation_type_uses_file_path(self, real_providers: None, monkeypatch: pytest.MonkeyPatch) -> None:
        """对照组：op_type="file_operation" → 文件操作通路（区分度输入）。"""
        mod, provider = _docker_provider()
        env_id = _registered_env(provider, "cid-1")
        sentinel = ExecutionResult(success=True, output={"content": "x"}, metadata={})
        seen: list[dict] = []

        async def _file_op(cid: str, op: dict) -> Any:
            seen.append(op)
            return sentinel

        monkeypatch.setattr(provider, "_file_op_in_container", _file_op)
        op = {"type": "file_operation", "action": "read", "path": "/workspace/a.txt"}
        assert _run(provider.execute_in_environment(env_id, op)) is sentinel
        assert seen == [op]

    def test_unknown_type_fails_with_type_in_error(self, real_providers: None) -> None:
        """未知 op_type → 失败并点名类型（不静默当 command）。"""
        mod, provider = _docker_provider()
        env_id = _registered_env(provider, "cid-1")
        got = _run(provider.execute_in_environment(env_id, {"type": "teleport"}))
        assert got.success is False
        assert "teleport" in (got.error or "")

    def test_missing_container_id_fails(self, real_providers: None) -> None:
        """已登记但无 container_id → 失败（分派前置守卫）。"""
        mod, provider = _docker_provider()
        env_id = _registered_env(provider, None)
        got = _run(provider.execute_in_environment(env_id, {"type": "command"}))
        assert got.success is False
        assert "容器ID" in (got.error or "")

    def test_unknown_env_id_fails(self, real_providers: None) -> None:
        """未登记环境 → 失败（另一守卫出口，与上一用例输入有区分度）。"""
        mod, provider = _docker_provider()
        got = _run(provider.execute_in_environment("ghost", {"type": "command"}))
        assert got.success is False
        assert "ghost" in (got.error or "")


class TestDockerCreatePresenceUnknown:
    def test_start_exception_marks_query_unavailable(
        self, real_providers: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """撞名预检 `docker start` 抛异常 = 存在性未知 → 不推进 create、标记 query_unavailable。"""
        mod, provider = _docker_provider()
        created: list[str] = []

        async def _boom(name: str) -> Any:
            raise TimeoutError("docker start timed out")

        async def _should_not_create(name: str, args: Any) -> Any:
            created.append(name)
            raise AssertionError("存在性未知时不得推进 create（防撞名）")

        monkeypatch.setattr(provider, "_start_one", _boom)
        monkeypatch.setattr(provider, "_create_and_start", _should_not_create)
        monkeypatch.setattr(provider, "_ensure_image", _noop)
        ws = tmp_path / "ws"
        ws.mkdir()
        env = _run(provider.create_environment(_ctx(workspace=str(ws)), container_name="cua-ws-a"))
        assert created == []
        assert env.status == EnvironmentStatus.ERROR.value
        assert env.provider_info.get("query_unavailable") is True
        assert "防撞名" in env.provider_info.get("error", "")


async def _noop() -> None:
    return None


class TestDockerBridgeEnvInjection:
    def _run_args(self, provider: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
        ws = tmp_path / "ws"
        ws.mkdir()
        return provider._build_run_args("cua-test", _ctx(workspace=str(ws)))

    def test_bridge_url_and_token_injected(
        self, real_providers: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """配置 bridge_url + 环境 token 齐备 → 两个 -e 注入（URL 取自配置）。"""
        mod, provider = _docker_provider()
        provider._config["bridge_url"] = "http://10.1.2.3:8765"
        monkeypatch.setenv("AGENTOS_BRIDGE_TOKEN", "tok-secret")
        args = self._run_args(provider, monkeypatch, tmp_path)
        envs = [args[i + 1] for i, a in enumerate(args) if a == "-e"]
        assert "AGENTOS_BRIDGE_URL=http://10.1.2.3:8765" in envs
        assert "AGENTOS_BRIDGE_TOKEN=tok-secret" in envs

    def test_missing_token_not_injected(
        self, real_providers: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """对照组：无 token 环境变量 → 不注入 TOKEN（容器内报清晰错误，不静默降级）。"""
        mod, provider = _docker_provider()
        provider._config["bridge_url"] = "http://10.1.2.3:8765"
        monkeypatch.delenv("AGENTOS_BRIDGE_TOKEN", raising=False)
        monkeypatch.delenv("AGENTOS_BRIDGE_URL", raising=False)
        args = self._run_args(provider, monkeypatch, tmp_path)
        envs = [args[i + 1] for i, a in enumerate(args) if a == "-e"]
        assert "AGENTOS_BRIDGE_URL=http://10.1.2.3:8765" in envs
        assert not any(v.startswith("AGENTOS_BRIDGE_TOKEN=") for v in envs)

    def test_bridge_url_falls_back_to_env_var(
        self, real_providers: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """无配置 bridge_url 时取 AGENTOS_BRIDGE_URL 环境变量（探测前的次优先源）。"""
        mod, provider = _docker_provider()
        monkeypatch.setenv("AGENTOS_BRIDGE_URL", "http://from-env:9999")
        monkeypatch.delenv("AGENTOS_BRIDGE_TOKEN", raising=False)
        args = self._run_args(provider, monkeypatch, tmp_path)
        envs = [args[i + 1] for i, a in enumerate(args) if a == "-e"]
        assert "AGENTOS_BRIDGE_URL=http://from-env:9999" in envs

    def test_no_source_no_url_env(self, real_providers: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """三源皆空 → 不注入 URL env（bridge_client 回退 host.docker.internal 候选）。"""
        mod, provider = _docker_provider()
        monkeypatch.delenv("AGENTOS_BRIDGE_URL", raising=False)
        monkeypatch.delenv("AGENTOS_BRIDGE_TOKEN", raising=False)
        monkeypatch.setattr(type(provider), "_detect_host_gateway_ip", staticmethod(lambda: ""))
        args = self._run_args(provider, monkeypatch, tmp_path)
        envs = [args[i + 1] for i, a in enumerate(args) if a == "-e"]
        assert not any(v.startswith("AGENTOS_BRIDGE_URL=") for v in envs)


# ═══════════════════════════════════════════════════════════
# 13. host_provider 共享根 sys.path（32）
# ═══════════════════════════════════════════════════════════


class TestHostProviderSharedRootPath:
    _SHARED_ROOT = str(_PLUGIN_DIR.parents[1])  # plugins/shared

    def test_inserts_shared_root_and_imports_proc_tree(
        self, real_providers: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """共享根不在 sys.path → 插入一次，且真实依赖 proc_tree 因此可导入。"""
        monkeypatch.setattr(sys, "path", [p for p in sys.path if p != self._SHARED_ROOT])
        assert self._SHARED_ROOT not in sys.path
        _load("isolation_host_provider_gaps_test", "providers/host_provider.py")
        assert self._SHARED_ROOT in sys.path
        assert importlib.util.find_spec("proc_tree") is not None

    def test_present_shared_root_not_duplicated(self, real_providers: None, monkeypatch: pytest.MonkeyPatch) -> None:
        """共享根已在表内 → 不重复插入（路径表不膨胀）。"""
        monkeypatch.setattr(
            sys,
            "path",
            [self._SHARED_ROOT, *[p for p in sys.path if p != self._SHARED_ROOT]],
        )
        before = list(sys.path)
        _load("isolation_host_provider_gaps_test", "providers/host_provider.py")
        assert sys.path == before
        assert sys.path.count(self._SHARED_ROOT) == 1
