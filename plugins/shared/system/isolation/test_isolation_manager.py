# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: none-local
"""isolation 插件（隔离环境管理器）单元测试。

覆盖（对齐 plugins/shared/system/isolation/manager.py）：
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
import sys
import types
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from plugins.shared.system.isolation.isolation_types import (
        EnvironmentStatus,
        ExecutionResult,
        IsolationContext,
        IsolationEnvironment,
        IsolationLevel,
        OperationType,
        TaskType,
    )

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent  # plugins/shared/system/isolation/

# ── 真实 isolation_types（纯 dataclass/枚举，无外部依赖） ──
_isolation_types_mod = importlib.util.spec_from_file_location(
    "isolation_types", _PLUGIN_DIR / "isolation_types.py"
)
assert _isolation_types_mod is not None and _isolation_types_mod.loader is not None
ISOLATION_TYPES = importlib.util.module_from_spec(_isolation_types_mod)
sys.modules["isolation_types"] = ISOLATION_TYPES
_isolation_types_mod.loader.exec_module(ISOLATION_TYPES)

if not TYPE_CHECKING:
    # 运行期：仍从动态加载的 isolation_types 取真实类（mypy 走上方静态导入）。
    EnvironmentStatus = ISOLATION_TYPES.EnvironmentStatus
    ExecutionResult = ISOLATION_TYPES.ExecutionResult
    IsolationContext = ISOLATION_TYPES.IsolationContext
    IsolationEnvironment = ISOLATION_TYPES.IsolationEnvironment
    IsolationLevel = ISOLATION_TYPES.IsolationLevel
    OperationType = ISOLATION_TYPES.OperationType
    TaskType = ISOLATION_TYPES.TaskType

_STUB_KEYS = (
    "decider",
    "providers",
    "providers.base",
    "providers.docker_provider",
    "providers.host_provider",
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
    """同时支持 _load_active_workspace_keys（_storage._tasks）与
    _resolve_workspace_key（.get(task_id)）的伪任务仓储。"""

    def __init__(self, tasks: dict[str, Any]) -> None:
        self._tasks = tasks
        self._storage = types.SimpleNamespace(_tasks=tasks)

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


# ═══════════════════════════════════════════════════════════
# 命名 / 基础查询
# ═══════════════════════════════════════════════════════════


class TestNaming:
    def test_workspace_to_container_name(self) -> None:
        mod = _load_manager()
        assert mod.IsolationManager._workspace_to_container_name("/proj/ws-a", "t1") == "cua-ws-a"
        assert mod.IsolationManager._workspace_to_container_name(None, "t1") == "cua-t1"
        assert mod.IsolationManager._workspace_to_container_name("", "t1") == "cua-t1"

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
        mgr._environments["cua-ws-a"] = env
        mgr._workspace_env_map["ws-a"] = "cua-ws-a"

        result = _run(
            mgr.get_or_create_environment(
                task_id="t1", task_type=TaskType.ATOMIC, workspace="/proj/ws-a"
            )
        )
        assert result is env
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
        _run(mgr.destroy_by_task_id("t1"))  # 无 repo → 跳过

    def test_destroy_by_task_id_via_env_map(self) -> None:
        mod = _load_manager()
        provider = FakeProvider(IsolationLevel.CONTAINER)
        mgr = _make_manager(mod, providers={IsolationLevel.CONTAINER: provider})
        mgr._environments["cua-ws-a"] = _ready_env(env_id="cua-ws-a")
        mgr._workspace_env_map["ws-a"] = "cua-ws-a"
        mgr.set_task_repository(
            types.SimpleNamespace(
                get=lambda tid: types.SimpleNamespace(
                    metadata={"ws_meta": {"path": "/proj/ws-a"}}, parent_task_id=None
                )
            )
        )
        _run(mgr.destroy_by_task_id("t1"))
        assert provider.destroyed == [("cua-ws-a", True)]

    def test_destroy_by_task_id_by_container_name(self) -> None:
        mod = _load_manager()
        mgr = _make_manager(mod)
        mgr.set_task_repository(
            types.SimpleNamespace(
                get=lambda tid: types.SimpleNamespace(
                    metadata={"ws_meta": {"path": "/proj/ws-a"}}, parent_task_id=None
                )
            )
        )
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

        # 无活跃任务（该任务终态）→ 销毁
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
        mgr2._workspace_env_map["zzz-ws"] = "cua-zzz"
        mgr2._environments["cua-zzz"] = _ready_env(env_id="cua-zzz")
        _run(mgr2.destroy_if_workspace_idle("t2"))
        assert mgr2._providers[IsolationLevel.CONTAINER].destroyed == [("cua-zzz", True)]

    def test_destroy_if_workspace_idle_load_failure_skips(self) -> None:
        mod = _load_manager()
        mgr = _make_manager(mod)
        mgr.set_task_repository(types.SimpleNamespace(_storage=types.SimpleNamespace(_tasks=None)))
        _run(mgr.destroy_if_workspace_idle("t1"))  # 加载失败 → 跳过，不销毁

    def test_resolve_workspace_key_walks_parent_chain(self) -> None:
        mod = _load_manager()
        mgr = _make_manager(mod)
        tasks = {
            "leaf": types.SimpleNamespace(metadata={}, parent_task_id="mid"),
            "mid": types.SimpleNamespace(metadata={}, parent_task_id="root"),
            "root": types.SimpleNamespace(metadata={"ws_meta": {"path": "/proj/ws-root"}}, parent_task_id=None),
        }
        mgr.set_task_repository(types.SimpleNamespace(get=lambda tid: tasks.get(tid)))
        assert mgr._resolve_workspace_key("leaf") == "ws-root"

    def test_resolve_workspace_key_no_repo(self) -> None:
        mod = _load_manager()
        mgr = _make_manager(mod)
        assert mgr._resolve_workspace_key("t1") is None


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

    def test_load_active_workspace_keys_missing_tasks(self) -> None:
        mod = _load_manager()
        mgr = _make_manager(mod)
        mgr.set_task_repository(types.SimpleNamespace(_storage=types.SimpleNamespace(_tasks=None)))
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

    def test_shape_check_rejects_windows_paths(self) -> None:
        """Windows 下 PurePath 语义：/mnt/... 与 C:/... 均无法通过
        /mnt/<x>/ 形态检查 → 返回 False，不执行 wsl.exe（该分支仅 Linux 可达）。"""
        mod = _load_manager()
        mgr = _make_manager(mod)
        assert _run(mgr._repair_host_mount("C:/proj/ws-a", "env-1")) is False
        assert _run(mgr._repair_host_mount("/mnt/d/proj/ws-a", "env-1")) is False


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
    def test_load_provider_config_fallback_empty(self) -> None:
        """config_center 不可用 → 返回空配置（不 panic）。"""
        mod = _load_manager()
        assert mod._load_provider_config() == {}

    def test_load_provider_config_from_center(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """config_center 返回 providers 段 → 透传。"""
        fake_center = types.ModuleType("config")
        fake_center.config_center = types.ModuleType("config.config_center")

        class _FakeConfigCenter:
            def get(self, _key: str) -> dict[str, Any]:
                return {"providers": {"host": {"enabled": False}}}

        fake_center.config_center.get_config_center = lambda: _FakeConfigCenter()
        monkeypatch.setitem(sys.modules, "config", fake_center)
        monkeypatch.setitem(sys.modules, "config.config_center", fake_center.config_center)
        mod = _load_manager()
        assert mod._load_provider_config() == {"host": {"enabled": False}}

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
            providers_config_override={"providers": {"cua": {"limits": {"memory": "3g", "cpus": "3.0"}}}}
        )
        docker = mgr._providers[IsolationLevel.CONTAINER]
        assert docker._config["memory_limit"] == "3g"
        assert docker._config["cpu_limit"] == "3.0"

    def test_create_providers_default_host_and_docker(self) -> None:
        mod = _load_manager()
        providers = mod._create_providers_from_config({})
        assert IsolationLevel.HOST in providers
        assert IsolationLevel.CONTAINER in providers

    def test_create_providers_all_disabled(self) -> None:
        mod = _load_manager()
        providers = mod._create_providers_from_config(
            {"host": {"enabled": False}, "docker": {"enabled": False}}
        )
        assert providers == {}

    def test_create_providers_host_only(self) -> None:
        mod = _load_manager()
        providers = mod._create_providers_from_config({"docker": {"enabled": False}})
        assert list(providers) == [IsolationLevel.HOST]

    def test_create_providers_cua_fallback(self) -> None:
        """docker 键缺失时回退 cua 键（老配置兼容）。"""
        mod = _load_manager()
        providers = mod._create_providers_from_config(
            {"host": {"enabled": False}, "cua": {"enabled": True}}
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
            {"host": {"enabled": False}, "docker": {"limits": {"memory": "1g", "cpus": "2.0"}}},
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
            {"host": {"enabled": False}, "docker": {}},
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
            {"host": {"enabled": False}, "docker": {}},
            profile={"container_memory": "384m"},
        )
        docker = providers[IsolationLevel.CONTAINER]
        assert docker._config["memory_limit"] == "384m"
        assert docker._config["cpu_limit"] == "1.0"  # 配置默认
