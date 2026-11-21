# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: none-local
"""isolation 插件（隔离环境管理器）单元测试。

覆盖（对齐 plugins/shared/isolation/manager.py）：
1. 环境生命周期：get_or_create_environment（复用/父级复用/新建/上限）、
   destroy_environment、destroy_by_task_id、destroy_if_workspace_idle、
   list/get/get_stats
2. 执行路径：execute_in_isolation（成功/建环境失败计数熔断/setns 自愈/EIO 自愈）
3. 生命周期：start/stop、_resume_containers/_stop_containers（伪 docker client）、
   _should_prune/_mark_prune_done、单例
4. 辅助：_workspace_to_container_name、_resolve_workspace_key、_run_docker_sync、
   _ensure_env_healthy_or_rebuild、_check_providers_availability、_repair_host_mount

隔离策略：manager 顶层 import 的 decider/providers.*/hardware_profile 全部用
sys.modules 伪模块注入（真实 docker_provider 847 行依赖 daemon，不纳入测试）；
isolation_types 用真实模块。伪模块仅在测试期间安装，测试后恢复，
避免污染同进程内其它测试。
"""

from __future__ import annotations

import asyncio
import importlib.util
import subprocess
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
    OperationType,
    TaskType,
)

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent  # plugins/shared/isolation/

_STUB_KEYS = (
    "decider",
    "providers",
    "providers.base",
    "providers.docker_provider",
    "providers.external_docker_provider",
    "providers.host_provider",
    "providers.wsl_native_provider",
    "providers.linux_native_provider",
    "providers.mxc_native_provider",
    "hardware_profile",
    "tasks",
    "tasks.types",
)


# ═══════════════════════════════════════════════════════════
# 伪依赖模块
# ═══════════════════════════════════════════════════════════


def _install_stubs() -> None:
    # decider
    decider = types.ModuleType("decider")

    class IsolationUnrecoverableError(Exception):
        pass

    class IsolationDecider:
        def __init__(self, policy: Any | None = None) -> None:
            self._policy = policy or types.SimpleNamespace(
                isolation=IsolationLevel.HOST, approval=True
            )

        async def decide(self, tool_name=None, tool_category=None, available_providers=None) -> Any:
            return self._policy

    decider.IsolationDecider = IsolationDecider
    decider.IsolationUnrecoverableError = IsolationUnrecoverableError
    sys.modules["decider"] = decider

    # providers 包
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
        _NAMESPACE_DESYNC_MARKERS = (
            "error executing setns",
            "oci runtime exec failed",
            "unable to start container process",
        )
        _IO_ERROR_MARKERS = ("input/output error",)

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            # 记录构造参数,供顶层 _create_providers_from_config 测试断言
            self._config = kwargs.get("config", {})

        @classmethod
        def _is_namespace_desync_error(cls, err: str | bytes | None) -> bool:
            if not err:
                return False
            if isinstance(err, bytes):
                err = err.decode("utf-8", errors="replace")
            low = err.lower()
            return any(m in low for m in cls._NAMESPACE_DESYNC_MARKERS)

        @classmethod
        def _is_io_error(cls, err: str | bytes | None) -> bool:
            if not err:
                return False
            if isinstance(err, bytes):
                err = err.decode("utf-8", errors="replace")
            low = err.lower()
            return any(m in low for m in cls._IO_ERROR_MARKERS)

        def _is_wsl_docker(self) -> bool:
            return False

        def _resolve_mount_path(self, workspace: str | None) -> str | None:
            return workspace

    docker_mod.DockerProvider = DockerProvider
    sys.modules["providers.docker_provider"] = docker_mod

    host_mod = types.ModuleType("providers.host_provider")

    class HostProvider:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

    host_mod.HostProvider = HostProvider
    sys.modules["providers.host_provider"] = host_mod

    # external_docker（TB 外部容器通道）：manager 顶层 import PROVIDER_TYPE 与类
    external_mod = types.ModuleType("providers.external_docker_provider")

    class ExternalDockerProvider:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

    external_mod.ExternalDockerProvider = ExternalDockerProvider
    external_mod.PROVIDER_TYPE = "external_docker"
    sys.modules["providers.external_docker_provider"] = external_mod

    wsl_mod = types.ModuleType("providers.wsl_native_provider")

    class WslNativeProvider:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._config = kwargs.get("config", {})

    wsl_mod.WslNativeProvider = WslNativeProvider
    sys.modules["providers.wsl_native_provider"] = wsl_mod

    linux_mod = types.ModuleType("providers.linux_native_provider")

    class LinuxNativeProvider:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._config = kwargs.get("config", {})

    linux_mod.LinuxNativeProvider = LinuxNativeProvider
    sys.modules["providers.linux_native_provider"] = linux_mod

    mxc_mod = types.ModuleType("providers.mxc_native_provider")

    class MxcNativeProvider:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._config = args[0] if args else kwargs.get("config", {})

        def registered_profiles(self) -> dict[str, str]:
            return {"linux-bash": "wslc", "native-win": "processcontainer"}

        def env_name_suffix(self, metadata: dict[str, Any]) -> str:
            profile = str((metadata or {}).get("execution_profile") or "linux-bash")
            return "" if profile == "linux-bash" else f"--{profile}"

    mxc_mod.MxcNativeProvider = MxcNativeProvider
    sys.modules["providers.mxc_native_provider"] = mxc_mod

    # hardware_profile
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

    # tasks.types（manager._load_active_workspace_keys 依赖 TaskStatus）
    tasks_pkg = types.ModuleType("tasks")
    tasks_pkg.__path__ = []
    sys.modules["tasks"] = tasks_pkg

    class TaskStatus(str, Enum):
        PENDING = "pending"
        RUNNING = "running"
        COMPLETED = "completed"
        FAILED = "failed"
        STOPPED = "stopped"
        TIMEOUT = "timeout"
        CANCELLED = "cancelled"

    tasks_types = types.ModuleType("tasks.types")
    tasks_types.TaskStatus = TaskStatus
    sys.modules["tasks.types"] = tasks_types


@pytest.fixture(autouse=True)
def _stub_env() -> Any:
    """每个测试安装伪依赖模块，测试后恢复，避免污染其它测试。"""
    saved = {k: sys.modules.get(k) for k in _STUB_KEYS}
    _install_stubs()
    yield
    for key, value in saved.items():
        if value is None:
            sys.modules.pop(key, None)
        else:
            sys.modules[key] = value


def _load_manager() -> Any:
    """动态加载 manager.py（每次新建，隔离模块级单例/锁状态）。"""
    mod_name = "isolation_manager_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "manager.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ═══════════════════════════════════════════════════════════
# 伪 Provider / 伪 docker client
# ═══════════════════════════════════════════════════════════


class FakeProvider:
    """duck-typed 隔离提供者：行为可配置。"""

    def __init__(
        self,
        level: IsolationLevel,
        available: bool = True,
        status: EnvironmentStatus = EnvironmentStatus.READY,
    ) -> None:
        self.level = level
        self._available = available
        self._status = status
        self._environments: dict[str, IsolationEnvironment] = {}
        self.created: list[IsolationEnvironment] = []
        self.destroyed: list[tuple[str, bool]] = []
        self.executed: list[tuple[str, dict]] = []
        # 依次弹出的执行结果队列；空 → 默认成功
        self._exec_results: list[ExecutionResult] = []
        self._destroy_result = True
        self._create_error: Exception | None = None
        self._status_override: EnvironmentStatus | None = None

    async def is_available(self) -> tuple[bool, str]:
        return self._available, ("" if self._available else "unavailable")

    async def create_environment(self, context: IsolationContext, container_name: str | None = None) -> IsolationEnvironment:
        if self._create_error:
            raise self._create_error
        env = IsolationEnvironment(
            env_id=container_name or f"env-{len(self.created)}",
            level=self.level,
            provider_type="fake",
            status=EnvironmentStatus.READY.value,
            context=context,
        )
        self._environments[env.env_id] = env
        self.created.append(env)
        return env

    async def execute_in_environment(self, env_id: str, operation: dict) -> ExecutionResult:
        self.executed.append((env_id, operation))
        if self._exec_results:
            return self._exec_results.pop(0)
        return ExecutionResult(success=True, output={"stdout": "ok"}, metadata={})

    async def destroy_environment(self, env_id: str, success: bool = True) -> bool:
        self.destroyed.append((env_id, success))
        self._environments.pop(env_id, None)
        return self._destroy_result

    async def get_environment_status(self, env_id: str) -> EnvironmentStatus:
        return self._status_override or self._status


class FakeContainer:
    def __init__(
        self,
        name: str,
        status: str = "exited",
        mounts: list[dict] | None = None,
        start_error: Exception | None = None,
        stop_error: Exception | None = None,
    ) -> None:
        self.name = name
        self.status = status
        self.attrs = {"Mounts": mounts or []}
        self.started = False
        self.stopped = False
        self.removed = False
        self._start_error = start_error
        self._stop_error = stop_error

    def start(self, **kwargs: Any) -> None:
        if self._start_error:
            raise self._start_error
        self.started = True
        self.status = "running"

    def stop(self, timeout: int = 5) -> None:
        if self._stop_error:
            raise self._stop_error
        self.stopped = True
        self.status = "stopped"

    def remove(self, **kwargs: Any) -> None:
        self.removed = True

    def reload(self) -> None:
        pass


class _FakeContainerCollection:
    """模拟 docker 的 containers 集合（.list / .get）。"""

    def __init__(self, containers: list[FakeContainer]) -> None:
        self._containers = containers

    def list(self, all: bool = True) -> list[FakeContainer]:
        return list(self._containers)

    def get(self, name: str) -> FakeContainer:
        import docker.errors as de

        for c in self._containers:
            if c.name == name:
                return c
        raise de.NotFound("no such container")


class FakeDockerClient:
    def __init__(self, containers: list[FakeContainer] | None = None) -> None:
        self._containers = containers or []
        self.containers = _FakeContainerCollection(self._containers)
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _make_manager(
    mod: Any,
    providers: dict[IsolationLevel, Any] | None = None,
    policy: Any | None = None,
) -> Any:
    """构造带伪 provider/decider 的 IsolationManager。

    默认决策策略与真实 decider 一致：CONTAINER 隔离、无需审批。
    """
    if providers is None:
        providers = {
            IsolationLevel.HOST: FakeProvider(IsolationLevel.HOST),
            IsolationLevel.CONTAINER: FakeProvider(IsolationLevel.CONTAINER),
        }
    if policy is None:
        policy = types.SimpleNamespace(isolation=IsolationLevel.CONTAINER, approval=False)
    decider_cls = sys.modules["decider"].IsolationDecider
    manager = mod.IsolationManager(providers=providers, decider=decider_cls(policy))
    return manager


def _ready_env(level: IsolationLevel = IsolationLevel.CONTAINER, env_id: str = "cua-ws-a") -> IsolationEnvironment:
    return IsolationEnvironment(
        env_id=env_id,
        level=level,
        provider_type="fake",
        status=EnvironmentStatus.READY.value,
        context=IsolationContext(task_id="t1", task_type=TaskType.ATOMIC, isolation_level=level),
    )


class FakeTaskRepo:
    """同时支持 _load_active_workspace_keys（list_all 只读快照）与
    _resolve_workspace_key（.get(task_id)）的伪任务仓储（公共面形态）。"""

    def __init__(self, tasks: dict[str, Any]) -> None:
        self._tasks = tasks

    def list_all(self) -> list[Any]:
        return list(self._tasks.values())

    def get(self, task_id: str) -> Any:
        return self._tasks.get(task_id)


def _active_repo(*active_ws: str) -> FakeTaskRepo:
    """构造含活跃任务的伪 task_repository（TaskStorage 形态）。"""
    tasks = {}
    for i, ws in enumerate(active_ws):
        tasks[f"task-{i}"] = types.SimpleNamespace(
            status=sys.modules["tasks.types"].TaskStatus.RUNNING,
            metadata={"ws_meta": {"path": f"/proj/{ws}"}},
        )
    # 一个终态任务（不应计入活跃）
    tasks["task-terminal"] = types.SimpleNamespace(
        status=sys.modules["tasks.types"].TaskStatus.COMPLETED,
        metadata={"ws_meta": {"path": "/proj/done-ws"}},
    )
    return FakeTaskRepo(tasks)


def _task_row(pipeline_id: str, ws_path: str | None = None) -> dict[str, Any]:
    """构造 pipeline_state 聚合行（``task.ws_meta`` 坐标可选）。"""
    row: dict[str, Any] = {"pipeline_id": pipeline_id}
    if ws_path is not None:
        row["task.ws_meta"] = {"path": ws_path}
    return row


def _state_rows_provider(*rows: dict[str, Any]) -> Any:
    """伪 pipeline_state 聚合行读面（async () -> list[dict] 契约形态）。"""

    async def _provide() -> list[dict[str, Any]]:
        return list(rows)

    return _provide


# ═══════════════════════════════════════════════════════════
# 命名 / 基础查询
# ═══════════════════════════════════════════════════════════


class TestNaming:
    def test_workspace_to_container_name(self) -> None:
        mod = _load_manager()
        assert mod.IsolationManager._workspace_to_container_name("/proj/ws-a", "t1") == "cua-ws-a"
        assert mod.IsolationManager._workspace_to_container_name(None, "t1") == "cua-t1"
        assert mod.IsolationManager._workspace_to_container_name("", "t1") == "cua-t1"

    def test_workspace_to_container_name_non_ascii_hashed(self) -> None:
        """中文等 docker 非法字符 → md5 短 hash（合法且同一 workspace 稳定）。"""
        import hashlib

        mod = _load_manager()
        ws = r"D:\myproject\修仙游戏"
        name = mod.IsolationManager._workspace_to_container_name(ws, "session")
        assert name == "cua-" + hashlib.md5(ws.encode("utf-8")).hexdigest()[:12]
        # 稳定性：同 workspace 多次调用一致
        assert name == mod.IsolationManager._workspace_to_container_name(ws, "other-task")

    def test_workspace_to_container_name_illegal_chars_hashed(self) -> None:
        """含空格/括号等非法字符 → 同样 hash 化，纯合法名保持原样。"""
        mod = _load_manager()
        bad = mod.IsolationManager._workspace_to_container_name(r"C:\proj\my app (2)", "t1")
        assert bad.startswith("cua-") and " " not in bad and "(" not in bad
        assert mod.IsolationManager._workspace_to_container_name("/proj/ws_v1.0", "t1") == "cua-ws_v1.0"

    def test_get_env_and_list(self) -> None:
        mod = _load_manager()
        mgr = _make_manager(mod)
        env = _ready_env(env_id="cua-a")
        mgr._environments["cua-a"] = env
        host_env = _ready_env(level=IsolationLevel.HOST, env_id="host-1")
        mgr._environments["host-1"] = host_env

        assert _run(mgr.get_environment("cua-a")) is env
        assert _run(mgr.get_environment("missing")) is None
        assert {e.env_id for e in _run(mgr.list_environments())} == {"cua-a", "host-1"}
        assert [e.env_id for e in _run(mgr.list_environments(level=IsolationLevel.HOST))] == ["host-1"]

    def test_get_stats(self) -> None:
        mod = _load_manager()
        mgr = _make_manager(mod)
        mgr._environments["cua-a"] = _ready_env()
        stats = mgr.get_stats()
        assert stats["total_environments"] == 1
        assert stats["level_counts"] == {"isolated": 1}
        assert stats["running"] is False


# ═══════════════════════════════════════════════════════════
# get_or_create_environment
# ═══════════════════════════════════════════════════════════


class TestGetOrCreate:
    def test_reuse_existing_ready_env(self) -> None:
        mod = _load_manager()
        mgr = _make_manager(mod)
        env = _ready_env(env_id="cua-ws-a")
        env.last_used_at = "2000-01-01T00:00:00+00:00"
        mgr._environments["cua-ws-a"] = env
        mgr._workspace_env_map["ws-a"] = "cua-ws-a"
        # 复用前 daemon 验活：容器健在（按名可寻）→ 走快路径复用
        mgr._find_existing_container = _async_return(env)  # type: ignore[method-assign]

        result = _run(
            mgr.get_or_create_environment(
                task_id="t1", task_type=TaskType.ATOMIC, workspace="/proj/ws-a"
            )
        )
        assert result is env
        assert result.last_used_at != "2000-01-01T00:00:00+00:00"
        assert mgr._ws_env_fail_counts.get("ws-a") is None

    def test_existing_env_not_ready_goes_to_docker_lookup(self) -> None:
        """内存映射存在但非 READY → 继续走 docker 查找。"""
        mod = _load_manager()
        mgr = _make_manager(mod)
        env = _ready_env(env_id="cua-ws-a")
        env.status = EnvironmentStatus.ERROR.value
        mgr._environments["cua-ws-a"] = env
        mgr._workspace_env_map["ws-a"] = "cua-ws-a"

        found = _ready_env(env_id="cua-ws-a", )
        mgr._find_existing_container = _async_return(found)  # type: ignore[method-assign]

        result = _run(
            mgr.get_or_create_environment(
                task_id="t1", task_type=TaskType.ATOMIC, workspace="/proj/ws-a"
            )
        )
        assert result is found
        # 已注册到 provider 侧
        provider = mgr._providers[IsolationLevel.CONTAINER]
        assert provider._environments["cua-ws-a"] is found

    def test_find_existing_registers_and_reuses(self) -> None:
        mod = _load_manager()
        mgr = _make_manager(mod)
        found = _ready_env(env_id="cua-ws-a")
        mgr._find_existing_container = _async_return(found)  # type: ignore[method-assign]
        result = _run(
            mgr.get_or_create_environment(
                task_id="t1", task_type=TaskType.ATOMIC, workspace="/proj/ws-a"
            )
        )
        assert result is found
        assert mgr._workspace_env_map["ws-a"] == "cua-ws-a"

    def test_parent_env_reuse(self) -> None:
        mod = _load_manager()
        mgr = _make_manager(mod)
        parent = _ready_env(env_id="cua-parent")
        mgr._environments["cua-parent"] = parent
        # 按名分派：子 workspace 容器不存在，父容器验活健在 → 复用父级
        by_name: dict[str, Any] = {"cua-child": None, "cua-parent": parent}

        async def _find(name: str) -> Any:
            return by_name[name]

        mgr._find_existing_container = _find  # type: ignore[method-assign]
        result = _run(
            mgr.get_or_create_environment(
                task_id="child",
                task_type=TaskType.ATOMIC,
                parent_env_id="cua-parent",
                workspace="/proj/child",
            )
        )
        assert result is parent

    def test_create_new_host_env(self) -> None:
        mod = _load_manager()
        host_provider = FakeProvider(IsolationLevel.HOST)
        mgr = _make_manager(mod, providers={IsolationLevel.HOST: host_provider})
        policy = types.SimpleNamespace(isolation=IsolationLevel.HOST, approval=True)
        mgr._decider = sys.modules["decider"].IsolationDecider(policy)  # type: ignore[attr-defined]
        result = _run(
            mgr.get_or_create_environment(
                task_id="t1", task_type=TaskType.ATOMIC, workspace="/proj/ws-a"
            )
        )
        assert host_provider.created, "应调用 provider.create_environment"
        # HOST 层不传 container_name（manager 仅在 CONTAINER 层传）
        assert result.context.isolation_level == IsolationLevel.HOST
        assert result.context.requires_approval is True

    def test_create_with_explicit_level_and_approval(self) -> None:
        mod = _load_manager()
        host_provider = FakeProvider(IsolationLevel.HOST)
        mgr = _make_manager(mod, providers={IsolationLevel.HOST: host_provider})
        result = _run(
            mgr.get_or_create_environment(
                task_id="t1",
                task_type=TaskType.ATOMIC,
                isolation_level=IsolationLevel.HOST,
                workspace="/proj/ws-a",
            )
        )
        assert host_provider.created
        assert result.context.isolation_level == IsolationLevel.HOST

    def test_level_without_provider_raises(self) -> None:
        mod = _load_manager()
        mgr = _make_manager(mod, providers={IsolationLevel.HOST: FakeProvider(IsolationLevel.HOST)})
        with pytest.raises(RuntimeError, match="找不到"):
            _run(
                mgr.get_or_create_environment(
                    task_id="t1",
                    task_type=TaskType.ATOMIC,
                    isolation_level=IsolationLevel.CONTAINER,
                    workspace="/proj/ws-a",
                )
            )

    def test_container_limit_exceeded(self) -> None:
        mod = _load_manager()
        mgr = _make_manager(mod)
        mgr._resource_profile = {"max_environments": 1, "tier": "test"}
        mgr._environments["cua-busy"] = _ready_env()
        with pytest.raises(RuntimeError, match="隔离环境数量已达上限"):
            _run(
                mgr.get_or_create_environment(
                    task_id="t1", task_type=TaskType.ATOMIC, workspace="/proj/other"
                )
            )

    def test_registered_env_dead_rebuilds_same_name(self) -> None:
        """登记 READY 但 daemon 侧容器已死 → 不得外返死条目，同名重建并换登记。

        回归锚（B5 收尾）：复用快路径无验活时，docker daemon 重启清掉容器后
        create_env 持续返回死 env_id，bash 每轮报 "No such container" 直到
        服务进程重启。daemon 查找返回 None = 容器已不存在，走重建。
        """
        mod = _load_manager()
        provider = FakeProvider(IsolationLevel.CONTAINER)
        mgr = _make_manager(mod, providers={IsolationLevel.CONTAINER: provider})
        dead = _ready_env(env_id="cua-ws-a")
        mgr._environments["cua-ws-a"] = dead
        mgr._workspace_env_map["ws-a"] = "cua-ws-a"
        mgr._find_existing_container = _async_return(None)  # type: ignore[method-assign]

        result = _run(
            mgr.get_or_create_environment(
                task_id="t1", task_type=TaskType.ATOMIC, workspace="/proj/ws-a"
            )
        )
        assert result is not dead, "死登记不得外返"
        assert result.env_id == "cua-ws-a"  # 同 workspace 同名重建
        assert result.status == EnvironmentStatus.READY.value
        assert provider.created, "应走 provider 真重建"
        # 登记面已换成新环境，死条目不残留
        assert mgr._environments["cua-ws-a"] is result
        assert mgr._workspace_env_map["ws-a"] == "cua-ws-a"

    def test_registered_env_probe_flaky_recovers_via_adopt(self) -> None:
        """验活瞬时误判失活（daemon 慢/超时）→ 第 3 步按名找回收养，不重建。

        daemon 侧同名容器实际健在时，清登记后 _find_existing_container 复查
        命中即收养复用——验活误判不升级为重复 create（create 撞名由既有
        Conflict 兜底，此处根本走不到 create）。
        """
        mod = _load_manager()
        provider = FakeProvider(IsolationLevel.CONTAINER)
        mgr = _make_manager(mod, providers={IsolationLevel.CONTAINER: provider})
        stale = _ready_env(env_id="cua-ws-a")
        mgr._environments["cua-ws-a"] = stale
        mgr._workspace_env_map["ws-a"] = "cua-ws-a"
        adopted = _ready_env(env_id="cua-ws-a")
        calls = {"n": 0}

        async def _find(name: str) -> Any:
            calls["n"] += 1
            # 首查（验活探针）超时式未命中；复查命中（容器实际在跑）
            return adopted if calls["n"] >= 2 else None

        mgr._find_existing_container = _find  # type: ignore[method-assign]

        result = _run(
            mgr.get_or_create_environment(
                task_id="t1", task_type=TaskType.ATOMIC, workspace="/proj/ws-a"
            )
        )
        assert result is adopted
        assert calls["n"] == 2, "验活 1 次 + 复查收养 1 次"
        assert provider.created == [], "同名容器健在时不得重建"
        assert mgr._environments["cua-ws-a"] is adopted
        assert provider._environments["cua-ws-a"] is adopted

    def test_probe_exception_treated_as_dead_rebuilds(self) -> None:
        """验活探针抛异常 → 按失活处理走重建（不可验证 ≠ 存活，fail-safe）。"""
        mod = _load_manager()
        provider = FakeProvider(IsolationLevel.CONTAINER)
        mgr = _make_manager(mod, providers={IsolationLevel.CONTAINER: provider})
        dead = _ready_env(env_id="cua-ws-a")
        mgr._environments["cua-ws-a"] = dead
        mgr._workspace_env_map["ws-a"] = "cua-ws-a"

        async def _boom(container_name: str) -> bool:
            raise RuntimeError("daemon probe exploded")

        mgr.ensure_container_alive = _boom  # type: ignore[method-assign]
        mgr._find_existing_container = _async_return(None)  # type: ignore[method-assign]

        result = _run(
            mgr.get_or_create_environment(
                task_id="t1", task_type=TaskType.ATOMIC, workspace="/proj/ws-a"
            )
        )
        assert result is not dead
        assert result.env_id == "cua-ws-a"
        assert provider.created, "探针异常应按失活走重建，不得当存活复用"
        assert mgr._environments["cua-ws-a"] is result

    def test_host_env_reuse_skips_daemon_probe(self) -> None:
        """host 级登记复用不触 daemon 探活（按容器名探必 NotFound，免误杀每轮重建）。"""
        mod = _load_manager()
        host_provider = FakeProvider(IsolationLevel.HOST)
        mgr = _make_manager(mod, providers={IsolationLevel.HOST: host_provider})
        host_env = _ready_env(level=IsolationLevel.HOST, env_id="host-1")
        mgr._environments["host-1"] = host_env
        mgr._workspace_env_map["ws-a"] = "host-1"
        probed: list[str] = []

        async def _find(name: str) -> Any:
            probed.append(name)
            return None

        mgr._find_existing_container = _find  # type: ignore[method-assign]

        result = _run(
            mgr.get_or_create_environment(
                task_id="t1", task_type=TaskType.ATOMIC, workspace="/proj/ws-a"
            )
        )
        assert result is host_env
        assert probed == [], "host 级环境无 daemon 资源，复用不应探活"

    def test_parent_env_dead_falls_to_create_new(self) -> None:
        """父级登记 READY 但 daemon 失联 → 清父级登记，为子 workspace 新建。"""
        mod = _load_manager()
        provider = FakeProvider(IsolationLevel.CONTAINER)
        mgr = _make_manager(mod, providers={IsolationLevel.CONTAINER: provider})
        parent = _ready_env(env_id="cua-parent")
        mgr._environments["cua-parent"] = parent
        mgr._workspace_env_map["parent-ws"] = "cua-parent"
        mgr._find_existing_container = _async_return(None)  # type: ignore[method-assign]

        result = _run(
            mgr.get_or_create_environment(
                task_id="child",
                task_type=TaskType.ATOMIC,
                parent_env_id="cua-parent",
                workspace="/proj/child",
            )
        )
        assert result is not parent
        assert result.env_id == "cua-child"  # 子 workspace 同名新建
        assert provider.created, "父级失联应为子任务新建环境"
        # 父级死登记（环境表 + workspace 映射指向）已清
        assert "cua-parent" not in mgr._environments
        assert "parent-ws" not in mgr._workspace_env_map
        assert mgr._workspace_env_map["child"] == "cua-child"


class _ConflictProvider(FakeProvider):
    """create 恒返回 daemon 撞名 Conflict 的 ERROR 占位环境。"""

    _CONFLICT_MSG = (
        'Error response from daemon: Conflict. The container name "/cua-ws-a" '
        'is already in use by container "96b0eb5c2c4b". You have to remove '
        "(or rename) that container to be able to reuse that name."
    )

    async def create_environment(
        self, context: IsolationContext, container_name: str | None = None
    ) -> IsolationEnvironment:
        env = IsolationEnvironment(
            env_id=f"docker-{context.task_id}",
            level=self.level,
            provider_type="fake",
            status=EnvironmentStatus.ERROR.value,
            context=context,
            provider_info={"error": self._CONFLICT_MSG},
        )
        self.created.append(env)
        return env


class TestCreateNameConflictRecovery:
    """create 撞 daemon Conflict = 容器实际存在 → 复查复用，不当创建失败。"""

    def test_撞名后复查复用已有容器(self) -> None:
        mod = _load_manager()
        provider = _ConflictProvider(IsolationLevel.CONTAINER)
        mgr = _make_manager(mod, providers={IsolationLevel.CONTAINER: provider})
        found = _ready_env(env_id="cua-ws-a")
        calls = {"n": 0}

        async def _find(name: str) -> Any:
            calls["n"] += 1
            # 首次（create 前的常规查找）超时式未命中；复查命中
            return None if calls["n"] == 1 else found

        mgr._find_existing_container = _find  # type: ignore[method-assign]

        result = _run(
            mgr.get_or_create_environment(
                task_id="t1", task_type=TaskType.ATOMIC, workspace="/proj/ws-a"
            )
        )
        assert result is found
        assert calls["n"] == 2
        # 复查命中后注册进双面映射，ERROR 占位环境不落数据
        assert mgr._workspace_env_map["ws-a"] == "cua-ws-a"
        assert provider._environments.get("cua-ws-a") is found
        assert "docker-t1" not in mgr._environments

    def test_撞名但复查仍未命中_保留错误环境(self) -> None:
        mod = _load_manager()
        provider = _ConflictProvider(IsolationLevel.CONTAINER)
        mgr = _make_manager(mod, providers={IsolationLevel.CONTAINER: provider})
        mgr._find_existing_container = _async_return(None)  # type: ignore[method-assign]

        result = _run(
            mgr.get_or_create_environment(
                task_id="t1", task_type=TaskType.ATOMIC, workspace="/proj/ws-a"
            )
        )
        assert result.status == EnvironmentStatus.ERROR.value

    def test_撞名探测器区分签名(self) -> None:
        mod = _load_manager()
        conflict = (
            'Error response from daemon: Conflict. The container name "/cua-x" '
            'is already in use by container "96b0".'
        )
        assert mod.IsolationManager._is_container_name_conflict(conflict)
        assert not mod.IsolationManager._is_container_name_conflict(
            "工作空间路径不存在: /proj/ws-a"
        )
        assert not mod.IsolationManager._is_container_name_conflict("")

    def test_ensure_container_alive_透传查找结果(self) -> None:
        mod = _load_manager()
        mgr = _make_manager(mod)
        mgr._find_existing_container = _async_return(_ready_env())  # type: ignore[method-assign]
        assert _run(mgr.ensure_container_alive("cua-ws-a")) is True
        mgr._find_existing_container = _async_return(None)  # type: ignore[method-assign]
        assert _run(mgr.ensure_container_alive("cua-gone")) is False


def _async_return(value: Any) -> Any:
    async def _fake(*args: Any, **kwargs: Any) -> Any:
        return value

    return _fake


# ═══════════════════════════════════════════════════════════
# execute_in_isolation
# ═══════════════════════════════════════════════════════════


class TestExecuteInIsolation:
    def test_execute_success(self) -> None:
        mod = _load_manager()
        provider = FakeProvider(IsolationLevel.CONTAINER)
        mgr = _make_manager(mod, providers={IsolationLevel.CONTAINER: provider})
        result = _run(
            mgr.execute_in_isolation(
                task_id="t1",
                task_type=TaskType.ATOMIC,
                operation={"type": "command", "cmd": "ls"},
                workspace="/proj/ws-a",
            )
        )
        assert result.success
        assert provider.executed == [("cua-ws-a", {"type": "command", "cmd": "ls"})]

    def test_build_failure_counts_then_circuit_breaks(self) -> None:
        """连续 3 次建环境失败 → 抛 IsolationUnrecoverableError 熔断。"""
        mod = _load_manager()
        provider = FakeProvider(IsolationLevel.CONTAINER)
        provider._create_error = RuntimeError("docker daemon down")
        mgr = _make_manager(mod, providers={IsolationLevel.CONTAINER: provider})
        kwargs = {
            "task_id": "t1",
            "task_type": TaskType.ATOMIC,
            "operation": {"type": "command"},
            "workspace": "/proj/ws-a",
        }
        r1 = _run(mgr.execute_in_isolation(**kwargs))
        assert not r1.success
        assert r1.metadata["isolation_unavailable"] is True
        assert r1.metadata["fail_count"] == 1
        r2 = _run(mgr.execute_in_isolation(**kwargs))
        assert r2.metadata["fail_count"] == 2
        with pytest.raises(sys.modules["decider"].IsolationUnrecoverableError):
            _run(mgr.execute_in_isolation(**kwargs))

    def test_namespace_desync_self_heal(self) -> None:
        """exec 报 setns 错误 → destroy + 重建 + 重试成功。"""
        mod = _load_manager()
        provider = FakeProvider(IsolationLevel.CONTAINER)
        provider._exec_results = [
            ExecutionResult(success=False, output=None, error="error executing setns: bad"),
        ]
        mgr = _make_manager(mod, providers={IsolationLevel.CONTAINER: provider})
        result = _run(
            mgr.execute_in_isolation(
                task_id="t1",
                task_type=TaskType.ATOMIC,
                operation={"type": "command", "cmd": "ls"},
                workspace="/proj/ws-a",
            )
        )
        assert result.success
        assert result.metadata.get("namespace_desync_recovered") is True
        assert provider.destroyed, "应销毁旧容器"
        assert len(provider.executed) == 2  # 原执行 + 重试执行

    def test_namespace_desync_unremovable(self) -> None:
        """destroy 失败（runc 卡死删不掉）→ 不重建，返回明确错误。"""
        mod = _load_manager()
        provider = FakeProvider(IsolationLevel.CONTAINER)
        provider._exec_results = [
            ExecutionResult(success=False, output=None, error="oci runtime exec failed: setns"),
        ]
        provider._destroy_result = False
        mgr = _make_manager(mod, providers={IsolationLevel.CONTAINER: provider})
        result = _run(
            mgr.execute_in_isolation(
                task_id="t1",
                task_type=TaskType.ATOMIC,
                operation={"type": "command"},
                workspace="/proj/ws-a",
            )
        )
        assert not result.success
        assert result.metadata.get("namespace_desync_unremovable") is True

    def test_namespace_desync_retry_fails(self) -> None:
        """重试仍失败 → 返回原失败结果。"""
        mod = _load_manager()
        provider = FakeProvider(IsolationLevel.CONTAINER)
        provider._exec_results = [
            ExecutionResult(success=False, output=None, error="error executing setns"),
            ExecutionResult(success=False, output=None, error="still broken"),
        ]
        mgr = _make_manager(mod, providers={IsolationLevel.CONTAINER: provider})
        result = _run(
            mgr.execute_in_isolation(
                task_id="t1",
                task_type=TaskType.ATOMIC,
                operation={"type": "command"},
                workspace="/proj/ws-a",
            )
        )
        assert not result.success
        assert result.metadata.get("namespace_desync_recovered") is None

    def test_io_error_self_heal(self) -> None:
        """9p/drvfs EIO → 非 WSL 模式跳过宿主修复，走重建容器路径。"""
        mod = _load_manager()
        provider = FakeProvider(IsolationLevel.CONTAINER)
        provider._exec_results = [
            ExecutionResult(success=False, output=None, error="Input/output error"),
        ]
        mgr = _make_manager(mod, providers={IsolationLevel.CONTAINER: provider})
        result = _run(
            mgr.execute_in_isolation(
                task_id="t1",
                task_type=TaskType.ATOMIC,
                operation={"type": "command"},
                workspace="/proj/ws-a",
            )
        )
        assert result.success
        assert result.metadata.get("io_error_recovered") is True

    def test_exec_exception_wrapped(self) -> None:
        """provider.execute 抛异常 → 失败结果而非上抛。"""
        mod = _load_manager()
        provider = FakeProvider(IsolationLevel.CONTAINER)

        async def _boom(env_id, operation):
            raise RuntimeError("exec crashed")

        provider.execute_in_environment = _boom  # type: ignore[method-assign]
        mgr = _make_manager(mod, providers={IsolationLevel.CONTAINER: provider})
        result = _run(
            mgr.execute_in_isolation(
                task_id="t1",
                task_type=TaskType.ATOMIC,
                operation={"type": "command"},
                workspace="/proj/ws-a",
            )
        )
        assert not result.success
        assert "exec crashed" in result.error

    def test_error_marker_detectors(self) -> None:
        mod = _load_manager()
        setns = ExecutionResult(success=False, output=None, error="error executing setns")
        assert mod.IsolationManager._is_namespace_desync_result(setns) is True
        assert mod.IsolationManager._is_namespace_desync_result(
            ExecutionResult(success=False, output={"stderr": b"oci runtime exec failed"}, error=None)
        ) is True
        assert mod.IsolationManager._is_namespace_desync_result(
            ExecutionResult(success=False, output=None, error="command not found")
        ) is False
        eio = ExecutionResult(success=False, output=None, error="Input/output error")
        assert mod.IsolationManager._is_io_error_result(eio) is True
        assert mod.IsolationManager._is_io_error_result(
            ExecutionResult(success=False, output={"stderr": "input/output error"}, error=None)
        ) is True
        assert mod.IsolationManager._is_io_error_result(
            ExecutionResult(success=True, output=None, error=None)
        ) is False


# ═══════════════════════════════════════════════════════════
# destroy 路径
# ═══════════════════════════════════════════════════════════


class TestDestroy:
    def test_destroy_environment_idempotent(self) -> None:
        mod = _load_manager()
        mgr = _make_manager(mod)
        assert _run(mgr.destroy_environment("missing")) is True  # 幂等

    def test_destroy_environment_success_cleans_maps(self) -> None:
        mod = _load_manager()
        provider = FakeProvider(IsolationLevel.CONTAINER)
        mgr = _make_manager(mod, providers={IsolationLevel.CONTAINER: provider})
        mgr._environments["cua-a"] = _ready_env(env_id="cua-a")
        mgr._workspace_env_map["ws-a"] = "cua-a"
        mgr._reuse_map["x"] = "cua-a"

        assert _run(mgr.destroy_environment("cua-a")) is True
        assert "cua-a" not in mgr._environments
        assert mgr._workspace_env_map == {}
        assert mgr._reuse_map == {}
        assert provider.destroyed == [("cua-a", True)]

    def test_destroy_environment_provider_failure_keeps_mapping(self) -> None:
        mod = _load_manager()
        provider = FakeProvider(IsolationLevel.CONTAINER)
        provider._destroy_result = False
        mgr = _make_manager(mod, providers={IsolationLevel.CONTAINER: provider})
        mgr._environments["cua-a"] = _ready_env(env_id="cua-a")
        mgr._workspace_env_map["ws-a"] = "cua-a"

        assert _run(mgr.destroy_environment("cua-a")) is False
        assert "cua-a" in mgr._environments  # 保留记录

    def test_destroy_by_task_id_no_workspace_info(self) -> None:
        mod = _load_manager()
        mgr = _make_manager(mod)
        _run(mgr.destroy_by_task_id("t1"))  # 无 state 读面 → 跳过

    def test_destroy_by_task_id_via_env_map(self) -> None:
        mod = _load_manager()
        provider = FakeProvider(IsolationLevel.CONTAINER)
        mgr = _make_manager(mod, providers={IsolationLevel.CONTAINER: provider})
        mgr._environments["cua-ws-a"] = _ready_env(env_id="cua-ws-a")
        mgr._workspace_env_map["ws-a"] = "cua-ws-a"
        mgr.set_state_rows_provider(_state_rows_provider(_task_row("t1", "/proj/ws-a")))
        _run(mgr.destroy_by_task_id("t1"))
        assert provider.destroyed == [("cua-ws-a", True)]

    def test_destroy_by_task_id_by_container_name(self) -> None:
        mod = _load_manager()
        mgr = _make_manager(mod)
        mgr.set_state_rows_provider(_state_rows_provider(_task_row("t1", "/proj/ws-a")))
        destroyed: list[str] = []

        async def fake_destroy_by_name(ws_key: str) -> None:
            destroyed.append(ws_key)

        mgr._destroy_container_by_name = fake_destroy_by_name  # type: ignore[method-assign]
        _run(mgr.destroy_by_task_id("t1"))
        assert destroyed == ["ws-a"]

    def test_destroy_if_workspace_idle(self) -> None:
        mod = _load_manager()
        mgr = _make_manager(mod)
        mgr.set_task_repository(_active_repo("ws-a"))
        # 有活跃任务 → 保留
        _run(mgr.destroy_if_workspace_idle("t1"))
        assert not mgr._providers[IsolationLevel.CONTAINER].destroyed

        # 无活跃任务（该任务终态）→ 销毁（ws 坐标经 state 行解析）
        mgr2 = _make_manager(mod)
        mgr2.set_task_repository(
            FakeTaskRepo(
                {
                    "t2": types.SimpleNamespace(
                        status=sys.modules["tasks.types"].TaskStatus.COMPLETED,
                        metadata={"ws_meta": {"path": "/proj/zzz-ws"}},
                    )
                }
            )
        )
        mgr2.set_state_rows_provider(_state_rows_provider(_task_row("t2", "/proj/zzz-ws")))
        mgr2._workspace_env_map["zzz-ws"] = "cua-zzz"
        mgr2._environments["cua-zzz"] = _ready_env(env_id="cua-zzz")
        _run(mgr2.destroy_if_workspace_idle("t2"))
        assert mgr2._providers[IsolationLevel.CONTAINER].destroyed == [("cua-zzz", True)]

    def test_destroy_if_workspace_idle_load_failure_skips(self) -> None:
        mod = _load_manager()
        mgr = _make_manager(mod)
        # 无公共任务快照接口的仓储 = 加载失败 → 跳过，不销毁
        mgr.set_task_repository(types.SimpleNamespace(get=lambda tid: None))
        mgr.set_state_rows_provider(_state_rows_provider(_task_row("t1", "/proj/ws-a")))
        _run(mgr.destroy_if_workspace_idle("t1"))

    def test_resolve_workspace_key_walks_parent_chain(self) -> None:
        """子任务行无坐标 → 沿 lineage.parent_pipeline_id 向上找祖先坐标。"""
        mod = _load_manager()
        mgr = _make_manager(mod)
        mgr.set_state_rows_provider(
            _state_rows_provider(
                _task_row("leaf"),
                _task_row("mid"),
                _task_row("root", "/proj/ws-root"),
                {"pipeline_id": "mid", "lineage.parent_pipeline_id": "root"},
                {"pipeline_id": "leaf", "lineage.parent_pipeline_id": "mid"},
            )
        )
        assert _run(mgr._resolve_workspace_key("leaf")) == "ws-root"

    def test_resolve_workspace_key_json_string_ws_meta(self) -> None:
        """task.ws_meta 跨边界 JSON 字符串形态 → 还原后解析（state_fields 契约）。"""
        import json

        mod = _load_manager()
        mgr = _make_manager(mod)
        row = _task_row("t1")
        row["task.ws_meta"] = json.dumps({"path": "/proj/ws-json"})
        mgr.set_state_rows_provider(_state_rows_provider(row))
        assert _run(mgr._resolve_workspace_key("t1")) == "ws-json"

    def test_resolve_workspace_key_corrupt_ws_meta_walks_to_parent(self) -> None:
        """腐败 ws_meta（非法 JSON）按无坐标处理 → 沿血缘向上找祖先。"""
        mod = _load_manager()
        mgr = _make_manager(mod)
        child = _task_row("leaf")
        child["task.ws_meta"] = "{corrupted"
        mgr.set_state_rows_provider(
            _state_rows_provider(
                child,
                _task_row("root", "/proj/ws-root"),
                {"pipeline_id": "leaf", "lineage.parent_pipeline_id": "root"},
            )
        )
        assert _run(mgr._resolve_workspace_key("leaf")) == "ws-root"

    def test_resolve_workspace_key_provider_error_returns_none(self) -> None:
        """state 读面故障 → 留痕返回 None（跳过销毁，不炸调用面）。"""
        mod = _load_manager()
        mgr = _make_manager(mod)

        async def _boom() -> list[dict[str, Any]]:
            raise RuntimeError("state 通道故障")

        mgr.set_state_rows_provider(_boom)
        assert _run(mgr._resolve_workspace_key("t1")) is None

    def test_resolve_workspace_key_no_provider(self) -> None:
        mod = _load_manager()
        mgr = _make_manager(mod)
        assert _run(mgr._resolve_workspace_key("t1")) is None


# ═══════════════════════════════════════════════════════════
# 活跃 workspace 加载 / provider 可用性 / 健康检查
# ═══════════════════════════════════════════════════════════


class TestWorkspaceKeys:
    def test_load_active_workspace_keys(self) -> None:
        mod = _load_manager()
        mgr = _make_manager(mod)
        mgr.set_task_repository(_active_repo("ws-a", "ws-b"))
        keys = _run(mgr._load_active_workspace_keys())
        assert keys == {"ws-a", "ws-b"}  # 终态任务不计入

    def test_load_active_workspace_keys_no_repo(self) -> None:
        mod = _load_manager()
        mgr = _make_manager(mod)
        assert _run(mgr._load_active_workspace_keys()) is None

    def test_load_active_workspace_keys_no_snapshot_interface(self) -> None:
        """仓储无公共任务快照接口（snapshot_tasks/list_all 均缺席）→ fail-closed None。"""
        mod = _load_manager()
        mgr = _make_manager(mod)
        mgr.set_task_repository(types.SimpleNamespace(get=lambda tid: None))
        assert _run(mgr._load_active_workspace_keys()) is None

    def test_check_providers_availability(self) -> None:
        mod = _load_manager()
        host = FakeProvider(IsolationLevel.HOST, available=True)
        container = FakeProvider(IsolationLevel.CONTAINER, available=False)
        mgr = _make_manager(mod, providers={IsolationLevel.HOST: host, IsolationLevel.CONTAINER: container})
        available = _run(mgr._check_providers_availability())
        assert available == {IsolationLevel.HOST: True, IsolationLevel.CONTAINER: False}


class TestHealthCheck:
    def test_healthy_env_passthrough(self) -> None:
        mod = _load_manager()
        provider = FakeProvider(IsolationLevel.CONTAINER)
        mgr = _make_manager(mod, providers={IsolationLevel.CONTAINER: provider})
        env = _ready_env(env_id="cua-a")
        result = _run(mgr._ensure_env_healthy_or_rebuild(env, rebuild_kwargs={}))
        assert result is env

    def test_unhealthy_env_rebuilds(self) -> None:
        mod = _load_manager()
        provider = FakeProvider(IsolationLevel.CONTAINER)
        provider._status_override = EnvironmentStatus.ERROR
        mgr = _make_manager(mod, providers={IsolationLevel.CONTAINER: provider})
        env = _ready_env(env_id="cua-a")
        mgr._environments["cua-a"] = env
        result = _run(
            mgr._ensure_env_healthy_or_rebuild(
                env, rebuild_kwargs={"task_id": "t1", "task_type": TaskType.ATOMIC, "workspace": "/proj/ws-a"}
            )
        )
        assert result is not env
        assert provider.destroyed == [("cua-a", False)]

    def test_health_check_missing_provider(self) -> None:
        mod = _load_manager()
        mgr = _make_manager(mod, providers={})
        env = _ready_env(env_id="cua-a")
        assert _run(mgr._ensure_env_healthy_or_rebuild(env, rebuild_kwargs={})) is env

    def test_health_check_error_returns_env(self) -> None:
        mod = _load_manager()
        provider = FakeProvider(IsolationLevel.CONTAINER)

        async def _boom(env_id):
            raise RuntimeError("inspect failed")

        provider.get_environment_status = _boom  # type: ignore[method-assign]
        mgr = _make_manager(mod, providers={IsolationLevel.CONTAINER: provider})
        env = _ready_env(env_id="cua-a")
        assert _run(mgr._ensure_env_healthy_or_rebuild(env, rebuild_kwargs={})) is env


# ═══════════════════════════════════════════════════════════
# start/stop / docker 容器恢复与停止
# ═══════════════════════════════════════════════════════════


class TestStartStop:
    def test_start_marks_running(self, monkeypatch) -> None:
        mod = _load_manager()
        mgr = _make_manager(mod)
        monkeypatch.setattr(mgr, "_should_prune", lambda: False)
        monkeypatch.setattr(mgr, "_resume_containers", _async_return(None))
        _run(mgr.start())
        assert mgr._running is True
        _run(mgr.start())  # 幂等

    def test_stop(self, monkeypatch) -> None:
        mod = _load_manager()
        mgr = _make_manager(mod)
        monkeypatch.setattr(mgr, "_stop_containers", _async_return(None))
        _run(mgr.stop())
        assert mgr._running is False

    def test_should_prune_mark_file(self, tmp_path: Path, monkeypatch) -> None:
        mod = _load_manager()
        mgr = _make_manager(mod)
        mark = tmp_path / ".docker_prune_last"
        monkeypatch.setattr(mod.IsolationManager, "_PRUNE_MARK_FILE", str(mark))
        # 无标记 → 应清理
        assert mgr._should_prune() is True
        # 写入后 → 距上次不足 24h，跳过
        mgr._mark_prune_done()
        assert mgr._should_prune() is False
        # 非法内容 → 视为应清理
        mark.write_text("garbage", encoding="utf-8")
        assert mgr._should_prune() is True

    def test_resume_containers(self, monkeypatch) -> None:
        import docker

        mod = _load_manager()
        mgr = _make_manager(mod)
        mgr.set_task_repository(_active_repo("ws-a"))
        ws_dir = Path(__import__("tempfile").mkdtemp()) / "ws-a"
        containers = [
            FakeContainer("cua-ws-a", status="exited", mounts=[{"Destination": "/workspace", "Type": "bind", "Source": str(ws_dir)}]),
            FakeContainer("cua-stale-ws", status="running"),
            FakeContainer("not-ours", status="running"),
        ]
        monkeypatch.setattr(docker, "from_env", lambda timeout=10: FakeDockerClient(containers))
        _run(mgr._resume_containers())
        assert containers[0].started is True  # 活跃 workspace 容器被恢复
        assert containers[1].stopped and containers[1].removed  # 无活跃任务 → 销毁
        assert containers[2].started is False  # 非 cua-* 前缀忽略
        # 工作空间目录被创建
        assert ws_dir.exists()

    def test_resume_containers_no_active_keys(self, monkeypatch) -> None:
        """活跃 workspace 加载失败 → 仅尝试恢复，不销毁。"""
        import docker

        mod = _load_manager()
        mgr = _make_manager(mod)
        containers = [FakeContainer("cua-orphan", status="exited")]
        monkeypatch.setattr(docker, "from_env", lambda timeout=10: FakeDockerClient(containers))
        _run(mgr._resume_containers())
        assert containers[0].stopped is False and containers[0].removed is False

    def test_stop_containers(self, monkeypatch) -> None:
        import docker

        mod = _load_manager()
        mgr = _make_manager(mod)
        mgr.set_task_repository(_active_repo("ws-a"))
        containers = [
            FakeContainer("cua-ws-a", status="running"),
            FakeContainer("cua-inactive", status="running"),
            FakeContainer("cua-stopped", status="exited"),
        ]
        monkeypatch.setattr(docker, "from_env", lambda timeout=10: FakeDockerClient(containers))
        _run(mgr._stop_containers())
        assert containers[0].stopped is True
        assert containers[1].stopped is False  # 非活跃
        assert containers[2].stopped is False  # 非 running

    def test_destroy_container_by_name_sync_not_found(self, monkeypatch) -> None:
        import docker

        mod = _load_manager()
        mgr = _make_manager(mod)
        monkeypatch.setattr(docker, "from_env", lambda timeout=10: FakeDockerClient([]))
        mgr._destroy_container_by_name_sync("cua-ghost")  # NotFound → 静默

    def test_destroy_container_by_name_sync_success(self, monkeypatch) -> None:
        import docker

        mod = _load_manager()
        mgr = _make_manager(mod)
        container = FakeContainer("cua-ws-a", status="running")
        monkeypatch.setattr(docker, "from_env", lambda timeout=10: FakeDockerClient([container]))
        mgr._destroy_container_by_name_sync("cua-ws-a")
        assert container.stopped and container.removed

    def test_run_docker_sync(self) -> None:
        mod = _load_manager()
        mgr = _make_manager(mod)
        assert _run(mgr._run_docker_sync(lambda: 42, timeout=5, op_name="t")) == 42

    def test_run_docker_sync_timeout(self) -> None:
        mod = _load_manager()
        mgr = _make_manager(mod)

        import time

        def slow():
            time.sleep(1.0)
            return 1

        assert _run(mgr._run_docker_sync(slow, timeout=0.05, op_name="slow")) is None


# ═══════════════════════════════════════════════════════════
# 宿主挂载修复（EIO 自愈的 WSL 分支）
# ═══════════════════════════════════════════════════════════


class TestRepairHostMount:
    def test_no_workspace_returns_false(self) -> None:
        mod = _load_manager()
        mgr = _make_manager(mod)
        assert _run(mgr._repair_host_mount(None, "env-1")) is False

    def test_shape_check_windows_drive_rejected_and_wsl_shape_admitted(
        self, monkeypatch: Any
    ) -> None:
        """workspace 是 WSL 侧路径、按 POSIX 语法解析（PurePosixPath）：
        盘符打头（C:/...）不满足 /mnt/<x>/ 形态 → False 零派发；
        /mnt/<x>/... 形态通过守卫派发 wsl.exe（打桩防真实子进程副作用）。"""
        mod = _load_manager()
        mgr = _make_manager(mod)
        assert _run(mgr._repair_host_mount("C:/proj/ws-a", "env-1")) is False

        class _OkCompleted:
            returncode = 0
            stderr = b""

        seen: list[Any] = []
        def _fake_run(args: Any, **kw: Any) -> _OkCompleted:
            seen.append(args)
            return _OkCompleted()

        monkeypatch.setattr(
            subprocess,
            "run",
            _fake_run,
            raising=True,
        )
        assert _run(mgr._repair_host_mount("/mnt/d/proj/ws-a", "env-1")) is True
        assert seen, "WSL 形态必须派发修复命令"


# ═══════════════════════════════════════════════════════════
# 全局单例
# ═══════════════════════════════════════════════════════════


class TestSingleton:
    def test_get_isolation_manager_singleton(self) -> None:
        mod = _load_manager()
        mgr1 = _run(mod.get_isolation_manager())
        mgr2 = _run(mod.get_isolation_manager())
        assert mgr1 is mgr2
        assert mgr1._providers  # 默认提供者已创建
        _run(mod.stop_isolation_manager())


# ═══════════════════════════════════════════════════════════
# 顶层配置加载 / 提供者创建
# ═══════════════════════════════════════════════════════════


class TestProviderConfig:
    def test_extract_providers_config_dir_nested(self) -> None:
        """get_config() 全量配置（目录嵌套结构）→ providers 提取。"""
        mod = _load_manager()
        full = {
            "isolation": {
                "isolation_config": {
                    "providers": {"cua": {"limits": {"memory": "2g", "cpus": "2.0"}}}
                },
                "isolation_policy": {"default_level": "isolated"},
            },
            "agents": {"main": {"model": "x"}},
        }
        out = mod.extract_providers_config(full)
        assert out == {"providers": {"cua": {"limits": {"memory": "2g", "cpus": "2.0"}}}}

    def test_extract_providers_config_flat_fallback(self) -> None:
        """平铺结构（isolation_config.providers 顶层）兼容。"""
        mod = _load_manager()
        full = {"isolation_config": {"providers": {"host": {"enabled": False}}}}
        assert mod.extract_providers_config(full) == {"providers": {"host": {"enabled": False}}}

    def test_extract_providers_config_missing_returns_empty(self) -> None:
        """无 providers（空/缺键/非 dict）→ 空 provider 配置（自适应兜底）。"""
        mod = _load_manager()
        assert mod.extract_providers_config({}) == {}
        assert mod.extract_providers_config({"isolation": {}}) == {}
        assert mod.extract_providers_config({"isolation": {"isolation_config": {"a": 1}}}) == {}

    def test_providers_config_override_injected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """providers_config_override 注入 → 创建 provider 用显式 limits。"""
        monkeypatch.setattr("sys.argv", ["x"])  # noqa: PLW2101 - 防 get_resource_profile 相关副作用
        mod = _load_manager()
        mgr = mod.IsolationManager(
            providers_config_override={
                "providers": {
                    # 2026-11-13 裁定：Docker 仅显式 opt-in（enabled 必带）；
                    # 原生后端显式关（nt 上 wsl_native 缺省启用会占 CONTAINER 槽）
                    "cua": {"enabled": True, "limits": {"memory": "3g", "cpus": "3.0"}},
                    "linux_native": {"enabled": False},
                    "wsl_native": {"enabled": False},
                }
            },
        )
        docker = mgr._providers[IsolationLevel.CONTAINER]
        assert docker._config["memory_limit"] == "3g"
        assert docker._config["cpu_limit"] == "3.0"

    def test_create_providers_default_host_and_docker(self) -> None:
        mod = _load_manager()
        providers = mod._create_providers_from_config({})
        assert IsolationLevel.HOST in providers
        assert IsolationLevel.CONTAINER in providers

    def test_mxc_enabled_takes_container_slot_before_wsl(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """注册表真值：mxc_native 显式启用时先占 CONTAINER 槽位（Windows）。"""
        mod = _load_manager()
        monkeypatch.setattr(mod.os, "name", "nt")
        providers = mod._create_providers_from_config({"mxc_native": {"enabled": True}})
        assert isinstance(providers[IsolationLevel.CONTAINER], mod.MxcNativeProvider)

    def test_mxc_disabled_falls_back_to_wsl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """默认关闭：槽位竞争回落到 wsl_native（Windows 缺省启用）。"""
        mod = _load_manager()
        monkeypatch.setattr(mod.os, "name", "nt")
        providers = mod._create_providers_from_config({"mxc_native": {"enabled": False}})
        assert isinstance(providers[IsolationLevel.CONTAINER], mod.WslNativeProvider)

    def test_mxc_platform_gate_non_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """平台门：非 Windows 即使启用也不注册（不引入 MXC 依赖）。"""
        mod = _load_manager()
        monkeypatch.setattr(mod.os, "name", "posix")
        monkeypatch.setattr(mod, "_is_linux_platform", lambda: False)
        providers = mod._create_providers_from_config(
            {"mxc_native": {"enabled": True}, "linux_native": {"enabled": False}}
        )
        if IsolationLevel.CONTAINER in providers:
            assert not isinstance(providers[IsolationLevel.CONTAINER], mod.MxcNativeProvider)

    def test_env_name_suffix_scopes_mxc_profile(self) -> None:
        """manager 环境名后缀钩子：profile 作用域同时作用于 ws_key/容器名。"""
        mod = _load_manager()
        mgr = object.__new__(mod.IsolationManager)
        mgr._providers = {}
        assert mgr._env_name_suffix({"execution_profile": "native-win"}) == ""
        mgr._providers = {IsolationLevel.CONTAINER: mod.MxcNativeProvider({"executor": ""})}
        assert mgr._env_name_suffix({"execution_profile": "linux-bash"}) == ""
        assert mgr._env_name_suffix({"execution_profile": "native-win"}) == "--native-win"

    def test_create_providers_all_disabled(self) -> None:
        mod = _load_manager()
        providers = mod._create_providers_from_config(
            {
                "host": {"enabled": False},
                "docker": {"enabled": False},
                # linux_native 默认启用（Linux 默认后端），「全关」须显式 opt-out；
                # wsl_native 在 nt 缺省启用（2026-11-13 裁定），同样须显式关
                "linux_native": {"enabled": False},
                "wsl_native": {"enabled": False},
            }
        )
        assert providers == {}

    def test_create_providers_host_only(self) -> None:
        mod = _load_manager()
        providers = mod._create_providers_from_config(
            {
                "docker": {"enabled": False},
                "linux_native": {"enabled": False},
                "wsl_native": {"enabled": False},
            }
        )
        assert list(providers) == [IsolationLevel.HOST]

    def test_create_providers_cua_fallback(self) -> None:
        """docker 键缺失时回退 cua 键（老配置兼容）。"""
        mod = _load_manager()
        providers = mod._create_providers_from_config(
            {
                "host": {"enabled": False},
                "cua": {"enabled": True},
                "wsl_native": {"enabled": False},
            }
        )
        assert IsolationLevel.CONTAINER in providers

    def test_create_providers_explicit_limits_override_profile(self) -> None:
        """yaml 显式 limits 优先于 hardware profile（显式配置 > 自适应）。"""
        mod = _load_manager()
        profile = {
            "container_memory": "256m",
            "container_cpus": "0.5",
            "memory_swap": "256m",
            "pids_limit": 64,
        }
        providers = mod._create_providers_from_config(
            {
                "host": {"enabled": False},
                "docker": {"enabled": True, "limits": {"memory": "1g", "cpus": "2.0"}},
                "linux_native": {"enabled": False},
                "wsl_native": {"enabled": False},
            },
            profile=profile,
        )
        docker = providers[IsolationLevel.CONTAINER]
        assert docker._config["memory_limit"] == "1g"
        assert docker._config["cpu_limit"] == "2.0"
        assert docker._config["pids_limit"] == 100  # 显式未给 pids 用默认

    def test_create_providers_profile_used_when_no_limits(self) -> None:
        """无显式 limits 时回落到 hardware profile 自适应配额。"""
        mod = _load_manager()
        profile = {
            "container_memory": "256m",
            "container_cpus": "0.5",
            "memory_swap": "256m",
            "pids_limit": 64,
        }
        providers = mod._create_providers_from_config(
            {
                "host": {"enabled": False},
                "docker": {"enabled": True},
                "linux_native": {"enabled": False},
                "wsl_native": {"enabled": False},
            },
            profile=profile,
        )
        docker = providers[IsolationLevel.CONTAINER]
        assert docker._config["memory_limit"] == "256m"
        assert docker._config["cpu_limit"] == "0.5"
        assert docker._config["pids_limit"] == 64

    def test_create_providers_profile_partial_keeps_defaults(self) -> None:
        """无显式 limits 且 profile 只给部分键时其余用默认值。"""
        mod = _load_manager()
        providers = mod._create_providers_from_config(
            {
                "host": {"enabled": False},
                "docker": {"enabled": True},
                "linux_native": {"enabled": False},
                "wsl_native": {"enabled": False},
            },
            profile={"container_memory": "384m"},
        )
        docker = providers[IsolationLevel.CONTAINER]
        assert docker._config["memory_limit"] == "384m"
        assert docker._config["cpu_limit"] == "1.0"  # 配置默认
