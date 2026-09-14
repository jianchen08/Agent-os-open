# @feature: FP-0.2.〇 管道引擎 | @ci: none-local
"""manager.py 分支补测：针对覆盖率缺行的行为测试。

覆盖（对齐 plugins/shared/system/isolation/manager.py 未覆盖分支）：
1. 模块级 _prune_task_done 完成回调（取消静默返回/异常留痕不上抛）
2. __init__ 兜底分支：硬件检测失败保守 profile、config_path 归一化加载与加载失败
3. prune 限频标记读写异常吞噬、_prune_docker_images 各返回码/异常分支
4. _resume_containers / _stop_containers 的 Docker 异常吞噬与跳过分支
5. 运行态 stop() 真实停止路径
6. _find_existing_container_sync 活性探针失败重建、bind 挂载工作区目录补建
7. destroy_if_workspace_idle：加载失败 fail-closed、兄弟活跃保留、按名销毁
8. destroy_environment provider 异常保留映射、execute_in_isolation 熔断透传/无 provider
9. setns 自愈各级失败：destroy 异常/重建失败/新环境无 provider/重试异常
10. _ensure_env_healthy_or_rebuild 销毁异常、重建失败回退原环境
11. list_environments 按 task_id 过滤、_load_active_workspace_keys 任务损坏 fail-closed

隔离策略与 test_isolation_manager.py 一致：decider/providers.*/hardware_profile/
tasks.types 用 sys.modules 伪模块注入（测试后恢复）；manager.py 经 importlib 按
同一模块名动态加载（每测新建，无跨文件串扰）；docker SDK 用 fake client
（monkeypatch docker.from_env），文件系统走 tmp_path 真实读写，subprocess 用替身。
"""

from __future__ import annotations

import asyncio
import contextlib
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
    TaskType,
)

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent  # plugins/shared/system/isolation/

_STUB_KEYS = (
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


# ═══════════════════════════════════════════════════════════
# 伪依赖模块（与 test_isolation_manager.py 同构）
# ═══════════════════════════════════════════════════════════


def _install_stubs() -> None:
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

    wsl_mod = types.ModuleType("providers.wsl_native_provider")

    class WslNativeProvider:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._config = kwargs.get("config", {})

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
    """每测安装伪依赖模块，测后恢复，避免污染同进程其它测试。"""
    saved = {k: sys.modules.get(k) for k in _STUB_KEYS}
    _install_stubs()
    yield
    for key, value in saved.items():
        if value is None:
            sys.modules.pop(key, None)
        else:
            sys.modules[key] = value


def _load_manager() -> Any:
    """动态加载 manager.py（与 test_isolation_manager.py 同一模块名，每测新建）。"""
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


def _async_return(value: Any) -> Any:
    async def _fake(*args: Any, **kwargs: Any) -> Any:
        return value

    return _fake


# ═══════════════════════════════════════════════════════════
# 伪 Provider / 伪 docker client / 伪任务仓储
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
        self._exec_results: list[ExecutionResult] = []
        self._destroy_result = True
        self._create_error: Exception | None = None
        self._status_override: EnvironmentStatus | None = None

    async def is_available(self) -> tuple[bool, str]:
        return self._available, ("" if self._available else "unavailable")

    async def create_environment(
        self, context: IsolationContext, container_name: str | None = None
    ) -> IsolationEnvironment:
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


class ScriptedProvider(FakeProvider):
    """按脚本执行的 provider：create/exec 依序抛错或返回（None = 正常成功）。"""

    def __init__(
        self,
        level: IsolationLevel,
        create_script: tuple[Any, ...] = (),
        exec_script: tuple[Any, ...] = (),
    ) -> None:
        super().__init__(level)
        self._create_script = list(create_script)
        self._exec_script = list(exec_script)

    async def create_environment(
        self, context: IsolationContext, container_name: str | None = None
    ) -> IsolationEnvironment:
        if self._create_script:
            item = self._create_script.pop(0)
            if isinstance(item, Exception):
                raise item
        return await super().create_environment(context, container_name=container_name)

    async def execute_in_environment(self, env_id: str, operation: dict) -> ExecutionResult:
        if self._exec_script:
            item = self._exec_script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return await super().execute_in_environment(env_id, operation)


class FakeContainer:
    """伪 docker 容器：状态/挂载/探针/异常均可配置。"""

    def __init__(
        self,
        name: str,
        status: str = "exited",
        mounts: list[dict] | None = None,
        start_error: Exception | None = None,
        stop_error: Exception | None = None,
        remove_error: Exception | None = None,
        exec_results: list[tuple[int, bytes]] | None = None,
        exec_error: Exception | None = None,
        cid: str = "cid-1",
    ) -> None:
        self.name = name
        self.status = status
        self.attrs = {"Mounts": mounts or []}
        self.id = cid
        self.started = False
        self.stopped = False
        self.removed = False
        self._start_error = start_error
        self._stop_error = stop_error
        self._remove_error = remove_error
        self._exec_results = exec_results or []
        self._exec_error = exec_error

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
        if self._remove_error:
            raise self._remove_error
        self.removed = True

    def reload(self) -> None:
        pass

    def exec_run(self, cmd: list[str], **kwargs: Any) -> tuple[int, bytes]:
        if self._exec_error is not None:
            raise self._exec_error
        if self._exec_results:
            return self._exec_results.pop(0)
        return (0, b"")


class _FakeContainerCollection:
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


class FakeTaskRepo:
    """同时支持 _load_active_workspace_keys（_storage._tasks）与
    _resolve_workspace_key（.get(task_id)）的伪任务仓储。"""

    def __init__(self, tasks: dict[str, Any]) -> None:
        self._tasks = tasks
        self._storage = types.SimpleNamespace(_tasks=tasks)

    def get(self, task_id: str) -> Any:
        return self._tasks.get(task_id)


class _SplitRepo:
    """get() 可用但 _storage._tasks 不可用的伪仓储（模拟任务存储加载失败）。"""

    def __init__(self, getmap: dict[str, Any]) -> None:
        self._storage = types.SimpleNamespace(_tasks=None)
        self._getmap = getmap

    def get(self, task_id: str) -> Any:
        return self._getmap.get(task_id)


def _make_manager(
    mod: Any,
    providers: dict[IsolationLevel, Any] | None = None,
    policy: Any | None = None,
) -> Any:
    if providers is None:
        providers = {
            IsolationLevel.HOST: FakeProvider(IsolationLevel.HOST),
            IsolationLevel.CONTAINER: FakeProvider(IsolationLevel.CONTAINER),
        }
    if policy is None:
        policy = types.SimpleNamespace(isolation=IsolationLevel.CONTAINER, approval=False)
    decider_cls = sys.modules["decider"].IsolationDecider
    return mod.IsolationManager(providers=providers, decider=decider_cls(policy))


def _ready_env(
    level: IsolationLevel = IsolationLevel.CONTAINER, env_id: str = "cua-ws-a"
) -> IsolationEnvironment:
    return IsolationEnvironment(
        env_id=env_id,
        level=level,
        provider_type="fake",
        status=EnvironmentStatus.READY.value,
        context=IsolationContext(task_id="t1", task_type=TaskType.ATOMIC, isolation_level=level),
    )


def _active_repo(*active_ws: str) -> FakeTaskRepo:
    """构造含活跃任务与一个终态任务的伪 task_repository。"""
    tasks: dict[str, Any] = {}
    for i, ws in enumerate(active_ws):
        tasks[f"task-{i}"] = types.SimpleNamespace(
            status=sys.modules["tasks.types"].TaskStatus.RUNNING,
            metadata={"ws_meta": {"path": f"/proj/{ws}"}},
        )
    tasks["task-terminal"] = types.SimpleNamespace(
        status=sys.modules["tasks.types"].TaskStatus.COMPLETED,
        metadata={"ws_meta": {"path": "/proj/done-ws"}},
    )
    return FakeTaskRepo(tasks)


_SETNS_ERROR = "error executing setns: runc desync"


# ═══════════════════════════════════════════════════════════
# _prune_task_done 完成回调
# ═══════════════════════════════════════════════════════════


class TestPruneTaskDoneCallback:
    def test_cancelled_task_returns_silently(self) -> None:
        mod = _load_manager()

        async def main() -> None:
            task = asyncio.ensure_future(asyncio.sleep(0))
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            mod._prune_task_done(task)  # 取消属正常收尾，不上抛

        _run(main())

    def test_failed_task_returns_without_raising(self) -> None:
        """prune 后台任务异常：回调自身不上抛（异常已经由 task.exception() 留痕）。"""
        mod = _load_manager()

        async def main() -> None:
            async def boom() -> None:
                raise RuntimeError("prune died")

            task = asyncio.ensure_future(boom())
            with contextlib.suppress(RuntimeError):
                await task
            mod._prune_task_done(task)

        _run(main())


# ═══════════════════════════════════════════════════════════
# __init__ 兜底分支
# ═══════════════════════════════════════════════════════════


class TestInitFallbacks:
    @pytest.mark.parametrize("mode", ["raise", "halted"])
    def test_hardware_detection_failure_uses_conservative_profile(
        self, mode: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """硬件检测失败（调用抛错/模块不可导入）→ 保守默认 profile 传导到容器配额。"""
        if mode == "raise":
            hp = types.ModuleType("hardware_profile")

            def _boom() -> dict[str, Any]:
                raise RuntimeError("sensor gone")

            hp.get_resource_profile = _boom
            monkeypatch.setitem(sys.modules, "hardware_profile", hp)
        else:
            monkeypatch.setitem(sys.modules, "hardware_profile", None)  # import 即失败
        mod = _load_manager()
        # 钉 CONTAINER 后端为 docker：仓库真身 isolation_config.yaml 已启用
        # wsl_native（占 CONTAINER 槽），不钉则真身配置渗入、断言对象漂移。
        monkeypatch.setattr(mod, "_load_provider_config", lambda: {"docker": {}})
        mgr = mod.IsolationManager()
        assert mgr._resource_profile["tier"] == "low(fallback)"
        assert mgr._resource_profile["max_environments"] == 3
        # 保守配额传导：内存 256m 而非默认 512m
        docker = mgr._providers[IsolationLevel.CONTAINER]
        assert docker._config["memory_limit"] == "256m"

    def _install_fake_center(self, monkeypatch: pytest.MonkeyPatch, seen: list[str], payload: Any) -> None:
        fake_pkg = types.ModuleType("config")
        fake_center = types.ModuleType("config.config_center")

        class _Center:
            def get(self, key: str) -> Any:
                seen.append(key)
                return payload

        fake_center.get_config_center = lambda: _Center()
        fake_pkg.config_center = fake_center
        monkeypatch.setitem(sys.modules, "config", fake_pkg)
        monkeypatch.setitem(sys.modules, "config.config_center", fake_center)

    def test_config_path_strips_prefix_and_applies_providers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """config_path 含 config/ 前缀 → 剥前缀后经 ConfigCenter 加载；host 被禁用则不建。"""
        seen: list[str] = []
        mod = _load_manager()
        self._install_fake_center(
            monkeypatch, seen, {"providers": {"host": {"enabled": False}}}
        )
        mgr = mod.IsolationManager(config_path=r"config\plugins\isolation\isolation_config.yaml")
        # Windows 反斜杠归一化 + 前缀剥离后才是 ConfigCenter 键
        assert seen == ["plugins/isolation/isolation_config.yaml"]
        assert set(mgr._providers) == {IsolationLevel.CONTAINER}

    def test_config_path_without_prefix_uses_key_as_is(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """config_path 不含 config/ 前缀 → 原样作为键；providers 为空 → 默认双提供者。"""
        seen: list[str] = []
        mod = _load_manager()
        self._install_fake_center(monkeypatch, seen, {})
        mgr = mod.IsolationManager(config_path="plugins/isolation/isolation_config.yaml")
        assert seen == ["plugins/isolation/isolation_config.yaml"]
        assert set(mgr._providers) == {IsolationLevel.HOST, IsolationLevel.CONTAINER}

    def test_config_path_load_failure_boots_with_defaults(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """config_path 加载失败 → 告警后走默认配置，管理器仍带双提供者启动。"""
        monkeypatch.setitem(sys.modules, "config.config_center", None)  # import 即失败
        mod = _load_manager()
        mgr = mod.IsolationManager(config_path="config/plugins/isolation/isolation_config.yaml")
        assert set(mgr._providers) == {IsolationLevel.HOST, IsolationLevel.CONTAINER}


# ═══════════════════════════════════════════════════════════
# prune 限频标记读写异常
# ═══════════════════════════════════════════════════════════


class TestPruneMarkFileIOErrors:
    def test_should_prune_unreadable_mark_treats_as_due(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """标记不可读（非 FileNotFoundError/ValueError）→ 视为应清理且不上抛。"""
        mod = _load_manager()
        mgr = _make_manager(mod)
        monkeypatch.setattr(mod.IsolationManager, "_PRUNE_MARK_FILE", str(tmp_path))  # 目录不可读
        assert mgr._should_prune() is True

    def test_mark_prune_done_unwritable_swallows_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """标记不可写 → 告警吞掉，不上抛。"""
        mod = _load_manager()
        mgr = _make_manager(mod)
        mark_dir = tmp_path / "mark-dir"
        mark_dir.mkdir()
        monkeypatch.setattr(mod.IsolationManager, "_PRUNE_MARK_FILE", str(mark_dir))
        mgr._mark_prune_done()  # 不上抛
        assert not (mark_dir / "x").exists()


# ═══════════════════════════════════════════════════════════
# _prune_docker_images
# ═══════════════════════════════════════════════════════════


@pytest.fixture
def _fast_prune(monkeypatch: pytest.MonkeyPatch) -> None:
    """摘掉 5s 启动延迟（时钟边界替身），避免拖慢整条测试车道。"""

    async def _no_delay(_delay: float = 0) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_delay)


class TestPruneDockerImages:
    def _point_mark_to(self, mod: Any, monkeypatch: pytest.MonkeyPatch, mark: Path) -> None:
        monkeypatch.setattr(mod.IsolationManager, "_PRUNE_MARK_FILE", str(mark))

    def test_both_prunes_succeed_writes_timestamp_mark(
        self, _fast_prune: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """image/builder prune 均 returncode=0 → 落时间戳标记（限频依据）。"""
        mod = _load_manager()
        mgr = _make_manager(mod)
        mark = tmp_path / ".docker_prune_last"
        self._point_mark_to(mod, monkeypatch, mark)
        commands: list[list[str]] = []

        def fake_run(args: list[str], **kwargs: Any) -> Any:
            commands.append(list(args))
            if args[1] == "image":
                return subprocess.CompletedProcess(args, 0, b"", b"")
            return (0, b"", b"")  # builder prune 走元组解包路径

        monkeypatch.setattr(subprocess, "run", fake_run)
        _run(mgr._prune_docker_images())
        assert commands[0][:3] == ["docker", "image", "prune"]
        assert commands[1][1:3] == ["builder", "prune"]
        assert "until=72h" in commands[1]
        assert mark.exists()
        float(mark.read_text(encoding="utf-8"))  # 内容是可解析时间戳

    @pytest.mark.parametrize("image_rc,builder_rc", [(1, 2), (127, 0)])
    def test_nonzero_returncode_still_marks_attempt(
        self,
        _fast_prune: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        image_rc: int,
        builder_rc: int,
    ) -> None:
        """prune 非零退出（docker 不可用/无需清理）→ 跳过但仍记尝试时间。"""
        mod = _load_manager()
        mgr = _make_manager(mod)
        mark = tmp_path / ".docker_prune_last"
        self._point_mark_to(mod, monkeypatch, mark)

        def fake_run(args: list[str], **kwargs: Any) -> Any:
            if args[1] == "image":
                return subprocess.CompletedProcess(args, image_rc, b"", b"Boom")
            return (builder_rc, b"", b"bad")

        monkeypatch.setattr(subprocess, "run", fake_run)
        _run(mgr._prune_docker_images())  # 不上抛
        assert mark.exists()

    @pytest.mark.parametrize("cli_error", [FileNotFoundError("no docker"), TimeoutError("hang")])
    def test_prune_cli_failure_skips_mark(
        self,
        _fast_prune: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        cli_error: Exception,
    ) -> None:
        """Docker CLI 缺失/超时 → 静默跳过，不写标记（下次启动重试）。"""
        mod = _load_manager()
        mgr = _make_manager(mod)
        mark = tmp_path / ".docker_prune_last"
        self._point_mark_to(mod, monkeypatch, mark)

        def fake_run(args: list[str], **kwargs: Any) -> Any:
            raise cli_error

        monkeypatch.setattr(subprocess, "run", fake_run)
        _run(mgr._prune_docker_images())  # 不上抛
        assert not mark.exists()

    def test_unexpected_result_shape_swallows_and_skips_mark(
        self, _fast_prune: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """builder prune 返回非元组（CompletedProcess）→ 解包 TypeError 落通用异常分支，
        静默跳过且不写标记（现状契约：cast 仅向类型检查器声明形状）。"""
        mod = _load_manager()
        mgr = _make_manager(mod)
        mark = tmp_path / ".docker_prune_last"
        self._point_mark_to(mod, monkeypatch, mark)

        def fake_run(args: list[str], **kwargs: Any) -> Any:
            return subprocess.CompletedProcess(args, 0, b"", b"")

        monkeypatch.setattr(subprocess, "run", fake_run)
        _run(mgr._prune_docker_images())  # 不上抛
        assert not mark.exists()


# ═══════════════════════════════════════════════════════════
# _resume_containers 异常分支
# ═══════════════════════════════════════════════════════════


class TestResumeContainersFailureBranches:
    @pytest.mark.parametrize("fail_on", ["stop", "remove"])
    def test_destroy_failure_swallowed(
        self, monkeypatch: pytest.MonkeyPatch, fail_on: str
    ) -> None:
        """销毁无效容器中途失败 → 告警吞掉，容器保留，其余容器不受影响。"""
        import docker

        mod = _load_manager()
        mgr = _make_manager(mod)
        mgr.set_task_repository(_active_repo("ws-a"))
        stale = FakeContainer(
            "cua-stale",
            status="running",
            stop_error=docker.errors.DockerException("busy") if fail_on == "stop" else None,
            remove_error=docker.errors.DockerException("rm fail") if fail_on == "remove" else None,
        )
        active = FakeContainer("cua-ws-a", status="running")
        monkeypatch.setattr(docker, "from_env", lambda timeout=10: FakeDockerClient([stale, active]))
        _run(mgr._resume_containers())  # 不上抛
        assert stale.removed is False
        assert active.started is False  # 活跃且 running → 不动

    def test_resume_start_failure_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """活跃 workspace 的 exited 容器 start 失败 → 告警吞掉，容器保留。"""
        import docker

        mod = _load_manager()
        mgr = _make_manager(mod)
        mgr.set_task_repository(_active_repo("ws-a"))
        dead = FakeContainer(
            "cua-ws-a", status="exited", start_error=docker.errors.DockerException("setns")
        )
        monkeypatch.setattr(docker, "from_env", lambda timeout=10: FakeDockerClient([dead]))
        _run(mgr._resume_containers())  # 不上抛
        assert dead.started is False

    def test_client_creation_failure_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """docker 客户端创建失败（daemon 不可达）→ 整个恢复流程静默跳过。"""
        import docker

        mod = _load_manager()
        mgr = _make_manager(mod)

        def _boom(timeout: int = 10) -> None:
            raise RuntimeError("no daemon")

        monkeypatch.setattr(docker, "from_env", _boom)
        _run(mgr._resume_containers())  # 不上抛


# ═══════════════════════════════════════════════════════════
# 运行态 stop() 与 _stop_containers 分支
# ═══════════════════════════════════════════════════════════


class TestStopLifecycleAndContainers:
    def test_stop_running_manager_stops_active_container(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """运行态 stop → 置位停止 + 仅停活跃 workspace 的 running 容器；重复 stop 幂等。"""
        import docker

        mod = _load_manager()
        mgr = _make_manager(mod)
        mgr._running = True
        mgr.set_task_repository(_active_repo("ws-a"))
        running = FakeContainer("cua-ws-a", status="running")
        monkeypatch.setattr(docker, "from_env", lambda timeout=10: FakeDockerClient([running]))
        _run(mgr.stop())
        assert mgr._running is False
        assert running.stopped is True
        _run(mgr.stop())  # 幂等：已停直接返回
        assert mgr._running is False

    def test_stop_containers_without_repo_stops_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """task_repository 未注入 → 活跃集合为空，任何容器都不停。"""
        import docker

        mod = _load_manager()
        mgr = _make_manager(mod)
        running = FakeContainer("cua-ws-a", status="running")
        monkeypatch.setattr(docker, "from_env", lambda timeout=10: FakeDockerClient([running]))
        _run(mgr._stop_containers())
        assert running.stopped is False

    def test_stop_containers_skips_non_prefixed_containers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """无 cua- 前缀的容器不属于管理器 → 跳过；同前缀活跃容器正常停。"""
        import docker

        mod = _load_manager()
        mgr = _make_manager(mod)
        mgr.set_task_repository(_active_repo("ws-a"))
        ours = FakeContainer("cua-ws-a", status="running")
        foreign = FakeContainer("ws-a", status="running")  # 同名但无前缀
        monkeypatch.setattr(
            docker, "from_env", lambda timeout=10: FakeDockerClient([ours, foreign])
        )
        _run(mgr._stop_containers())
        assert ours.stopped is True
        assert foreign.stopped is False

    def test_stop_containers_docker_error_swallowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """停容器抛 DockerException → 告警吞掉，client 仍释放。"""
        import docker

        mod = _load_manager()
        mgr = _make_manager(mod)
        mgr.set_task_repository(_active_repo("ws-a"))
        stuck = FakeContainer(
            "cua-ws-a", status="running", stop_error=docker.errors.DockerException("boom")
        )
        client = FakeDockerClient([stuck])
        monkeypatch.setattr(docker, "from_env", lambda timeout=10: client)
        _run(mgr._stop_containers())  # 不上抛
        assert stuck.stopped is False
        assert client.closed is True

    def test_stop_containers_client_failure_swallowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """docker 客户端创建失败 → 整个停止流程静默跳过。"""
        import docker

        mod = _load_manager()
        mgr = _make_manager(mod)

        def _boom(timeout: int = 10) -> None:
            raise RuntimeError("no daemon")

        monkeypatch.setattr(docker, "from_env", _boom)
        _run(mgr._stop_containers())  # 不上抛


# ═══════════════════════════════════════════════════════════
# _find_existing_container_sync：活性探针与挂载目录
# ═══════════════════════════════════════════════════════════


class TestFindExistingSyncEdges:
    def test_probe_nonzero_exit_treated_as_dead(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """探针 exec 非零退出 → 视为坏容器强制删除并返回 None（交上层重建）。"""
        import docker

        mod = _load_manager()
        mgr = _make_manager(mod)
        zombie = FakeContainer("cua-ws-a", status="running", exec_results=[(1, b"")])
        monkeypatch.setattr(docker, "from_env", lambda timeout=10: FakeDockerClient([zombie]))
        assert mgr._find_existing_container_sync("cua-ws-a") is None
        assert zombie.removed is True

    def test_probe_exception_treated_as_dead(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """探针 exec 抛 DockerException（setns 脱节）→ 同样删除重建。"""
        import docker

        mod = _load_manager()
        mgr = _make_manager(mod)
        zombie = FakeContainer(
            "cua-ws-a", status="running", exec_error=docker.errors.DockerException("setns failure")
        )
        monkeypatch.setattr(docker, "from_env", lambda timeout=10: FakeDockerClient([zombie]))
        assert mgr._find_existing_container_sync("cua-ws-a") is None
        assert zombie.removed is True

    def test_workspace_bind_mount_recreates_missing_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """/workspace bind 挂载的宿主目录缺失 → 补建（真实文件系统）并记入 env。"""
        import docker

        mod = _load_manager()
        mgr = _make_manager(mod)
        ws_dir = tmp_path / "recreated-ws"
        mounts = [
            {"Destination": "/data", "Type": "volume", "Source": "vol-data"},  # Destination 不符
            {"Destination": "/workspace", "Type": "volume", "Source": "vol-ws"},  # Type 不符
            {"Destination": "/workspace", "Type": "bind", "Source": str(ws_dir)},  # 命中
        ]
        healthy = FakeContainer(
            "cua-ws-a", status="running", mounts=mounts, exec_results=[(0, b"")]
        )
        monkeypatch.setattr(docker, "from_env", lambda timeout=10: FakeDockerClient([healthy]))
        env = mgr._find_existing_container_sync("cua-ws-a")
        assert ws_dir.exists()  # 缺失工作区目录被真实补建
        assert env is not None
        assert env.env_id == "cua-ws-a"
        assert env.provider_info["workspace_root"] == str(ws_dir)


# ═══════════════════════════════════════════════════════════
# destroy_if_workspace_idle 分支
# ═══════════════════════════════════════════════════════════


class TestDestroyIfWorkspaceIdleBranches:
    def test_active_keys_load_failure_fail_closed(self) -> None:
        """活跃 workspace 加载失败 → fail-closed 不销毁，容器记录原样保留。"""
        mod = _load_manager()
        provider = FakeProvider(IsolationLevel.CONTAINER)
        mgr = _make_manager(mod, providers={IsolationLevel.CONTAINER: provider})
        mgr._environments["cua-ws-a"] = _ready_env(env_id="cua-ws-a")
        mgr._workspace_env_map["ws-a"] = "cua-ws-a"
        mgr.set_task_repository(
            _SplitRepo(
                {
                    "t1": types.SimpleNamespace(
                        metadata={"ws_meta": {"path": "/proj/ws-a"}}, parent_task_id=None
                    )
                }
            )
        )
        _run(mgr.destroy_if_workspace_idle("t1"))
        assert provider.destroyed == []
        assert mgr._workspace_env_map == {"ws-a": "cua-ws-a"}

    def test_sibling_active_keeps_container(self) -> None:
        """同 workspace 仍有活跃兄弟任务 → 保留容器。"""
        mod = _load_manager()
        provider = FakeProvider(IsolationLevel.CONTAINER)
        mgr = _make_manager(mod, providers={IsolationLevel.CONTAINER: provider})
        status = sys.modules["tasks.types"].TaskStatus
        mgr.set_task_repository(
            FakeTaskRepo(
                {
                    "t-done": types.SimpleNamespace(
                        status=status.COMPLETED,
                        metadata={"ws_meta": {"path": "/proj/ws-a"}},
                        parent_task_id=None,
                    ),
                    "t-sibling": types.SimpleNamespace(
                        status=status.RUNNING,
                        metadata={"ws_meta": {"path": "/proj/ws-a"}},
                        parent_task_id=None,
                    ),
                }
            )
        )
        _run(mgr.destroy_if_workspace_idle("t-done"))
        assert provider.destroyed == []

    def test_idle_without_env_destroys_by_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """workspace 无内存映射 → 按名经 Docker API 停止并删除容器。"""
        import docker

        mod = _load_manager()
        mgr = _make_manager(mod)
        status = sys.modules["tasks.types"].TaskStatus
        mgr.set_task_repository(
            FakeTaskRepo(
                {
                    "t-done": types.SimpleNamespace(
                        status=status.COMPLETED,
                        metadata={"ws_meta": {"path": "/proj/ws-b"}},
                        parent_task_id=None,
                    )
                }
            )
        )
        container = FakeContainer("cua-ws-b", status="running")
        monkeypatch.setattr(docker, "from_env", lambda timeout=10: FakeDockerClient([container]))
        _run(mgr.destroy_if_workspace_idle("t-done"))
        assert container.stopped is True
        assert container.removed is True

    def test_idle_by_name_container_absent_is_silent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """按名销毁时容器已不存在（NotFound）→ 静默返回。"""
        import docker

        mod = _load_manager()
        mgr = _make_manager(mod)
        status = sys.modules["tasks.types"].TaskStatus
        mgr.set_task_repository(
            FakeTaskRepo(
                {
                    "t-done": types.SimpleNamespace(
                        status=status.COMPLETED,
                        metadata={"ws_meta": {"path": "/proj/ws-b"}},
                        parent_task_id=None,
                    )
                }
            )
        )
        monkeypatch.setattr(docker, "from_env", lambda timeout=10: FakeDockerClient([]))
        _run(mgr.destroy_if_workspace_idle("t-done"))  # 不上抛


# ═══════════════════════════════════════════════════════════
# destroy_environment / execute_in_isolation 边界
# ═══════════════════════════════════════════════════════════


class TestDestroyEnvironmentProviderRaises:
    def test_provider_destroy_exception_keeps_mapping(self) -> None:
        """provider 销毁抛异常 → 返回 False 且内存映射不清理（docker 里容器仍在）。"""
        mod = _load_manager()
        provider = FakeProvider(IsolationLevel.CONTAINER)

        async def _boom(env_id: str, success: bool = True) -> bool:
            raise RuntimeError("rm exploded")

        provider.destroy_environment = _boom  # type: ignore[method-assign]
        mgr = _make_manager(mod, providers={IsolationLevel.CONTAINER: provider})
        mgr._environments["cua-a"] = _ready_env(env_id="cua-a")
        mgr._workspace_env_map["ws-a"] = "cua-a"
        assert _run(mgr.destroy_environment("cua-a")) is False
        assert "cua-a" in mgr._environments
        assert mgr._workspace_env_map == {"ws-a": "cua-a"}


class TestExecuteInIsolationEdges:
    def test_unrecoverable_create_error_reraises_without_counting(self) -> None:
        """建环境抛 IsolationUnrecoverableError → 原样上抛且不计 ws 失败计数。"""
        mod = _load_manager()
        provider = FakeProvider(IsolationLevel.CONTAINER)
        provider._create_error = sys.modules["decider"].IsolationUnrecoverableError("engine down")
        mgr = _make_manager(mod, providers={IsolationLevel.CONTAINER: provider})
        mgr._find_existing_container = _async_return(None)  # type: ignore[method-assign]
        with pytest.raises(sys.modules["decider"].IsolationUnrecoverableError):
            _run(
                mgr.execute_in_isolation(
                    task_id="t1",
                    task_type=TaskType.ATOMIC,
                    operation={"type": "command"},
                    workspace="/proj/ws-a",
                )
            )
        assert mgr._ws_env_fail_counts == {}

    def test_transient_create_error_counted_as_failure(self) -> None:
        """普通异常建环境失败 → 不上抛，返回失败结果并计一次 ws 失败。"""
        mod = _load_manager()
        provider = FakeProvider(IsolationLevel.CONTAINER)
        provider._create_error = RuntimeError("docker busy")
        mgr = _make_manager(mod, providers={IsolationLevel.CONTAINER: provider})
        mgr._find_existing_container = _async_return(None)  # type: ignore[method-assign]
        result = _run(
            mgr.execute_in_isolation(
                task_id="t1",
                task_type=TaskType.ATOMIC,
                operation={"type": "command"},
                workspace="/proj/ws-a",
            )
        )
        assert result.success is False
        assert result.metadata["isolation_unavailable"] is True
        assert result.metadata["fail_count"] == 1
        assert mgr._ws_env_fail_counts["ws-a"] == 1

    def test_env_without_provider_returns_failure_result(self) -> None:
        """复用的环境在 providers 中无对应提供者 → 返回失败结果而非上抛。"""
        mod = _load_manager()
        mgr = _make_manager(mod)
        env = _ready_env(level=IsolationLevel.HOST, env_id="host-1")
        mgr._environments["host-1"] = env
        mgr._workspace_env_map["ws-a"] = "host-1"
        mgr._providers = {}
        result = _run(
            mgr.execute_in_isolation(
                task_id="t9",
                task_type=TaskType.ATOMIC,
                operation={"type": "command"},
                workspace="/proj/ws-a",
            )
        )
        assert result.success is False
        assert "找不到" in (result.error or "")
        assert "non_isolated" in (result.error or "")


# ═══════════════════════════════════════════════════════════
# setns 自愈（_rebuild_and_retry_exec）各级失败
# ═══════════════════════════════════════════════════════════


class TestSetnsSelfHealEdges:
    def test_destroy_crash_returns_unremovable(self) -> None:
        """自愈销毁阶段自身抛异常 → 视为不可删除，返回需重启 docker 的明确错误。"""
        mod = _load_manager()
        mgr = _make_manager(mod, providers={IsolationLevel.CONTAINER: FakeProvider(IsolationLevel.CONTAINER)})

        async def _explode(*args: Any, **kwargs: Any) -> bool:
            raise RuntimeError("destroy crashed")

        mgr.destroy_environment = _explode  # type: ignore[method-assign]
        env = _ready_env(env_id="cua-ws-a")
        result = _run(
            mgr._rebuild_and_retry_exec(
                env, rebuild_kwargs={}, operation={"type": "command"}, original_error=_SETNS_ERROR
            )
        )
        assert result.success is False
        assert result.metadata["namespace_desync_unremovable"] is True
        assert "restart" in (result.error or "")
        assert _SETNS_ERROR in (result.error or "")

    def test_rebuild_create_failure_returns_original_error(self) -> None:
        """重建阶段 create 失败 → 返回原失败结果并标记 rebuild_failed。"""
        mod = _load_manager()
        provider = ScriptedProvider(
            IsolationLevel.CONTAINER,
            create_script=(None, RuntimeError("daemon gone")),  # 首建成功，重建失败
            exec_script=(ExecutionResult(success=False, output=None, error=_SETNS_ERROR),),
        )
        mgr = _make_manager(mod, providers={IsolationLevel.CONTAINER: provider})
        mgr._find_existing_container = _async_return(None)  # type: ignore[method-assign]
        result = _run(
            mgr.execute_in_isolation(
                task_id="t1",
                task_type=TaskType.ATOMIC,
                operation={"type": "command"},
                workspace="/proj/ws-a",
            )
        )
        assert result.success is False
        assert result.error == _SETNS_ERROR
        assert result.metadata["namespace_desync_rebuild_failed"] is True

    def test_new_env_without_provider_returns_original_error(self) -> None:
        """重建出的环境级别无对应 provider → 返回原错误，无自愈标记。"""
        mod = _load_manager()
        provider = FakeProvider(IsolationLevel.CONTAINER)
        mgr = _make_manager(mod, providers={IsolationLevel.CONTAINER: provider})
        env = _ready_env(env_id="cua-ws-a")
        mgr._environments["cua-ws-a"] = env
        mgr._workspace_env_map["ws-a"] = "cua-ws-a"
        mgr.get_or_create_environment = _async_return(
            _ready_env(level=IsolationLevel.HOST, env_id="host-x")
        )  # type: ignore[method-assign]
        result = _run(
            mgr._rebuild_and_retry_exec(
                env, rebuild_kwargs={}, operation={"type": "command"}, original_error=_SETNS_ERROR
            )
        )
        assert result.success is False
        assert result.error == _SETNS_ERROR
        assert result.metadata == {}
        # 旧环境销毁仍发生（自愈第一步），success=False 语义保留
        assert provider.destroyed == [("cua-ws-a", False)]

    def test_retry_exec_crash_returns_original_error(self) -> None:
        """重试执行抛异常 → 返回原失败结果并标记 retry_failed。"""
        mod = _load_manager()
        provider = ScriptedProvider(
            IsolationLevel.CONTAINER,
            exec_script=(
                ExecutionResult(success=False, output=None, error=_SETNS_ERROR),
                RuntimeError("exec boom on retry"),
            ),
        )
        mgr = _make_manager(mod, providers={IsolationLevel.CONTAINER: provider})
        mgr._find_existing_container = _async_return(None)  # type: ignore[method-assign]
        result = _run(
            mgr.execute_in_isolation(
                task_id="t1",
                task_type=TaskType.ATOMIC,
                operation={"type": "command"},
                workspace="/proj/ws-a",
            )
        )
        assert result.success is False
        assert result.error == _SETNS_ERROR
        assert result.metadata["namespace_desync_retry_failed"] is True


# ═══════════════════════════════════════════════════════════
# _ensure_env_healthy_or_rebuild 边界
# ═══════════════════════════════════════════════════════════


class TestEnsureEnvHealthyEdges:
    def test_destroy_crash_still_rebuilds(self) -> None:
        """非就绪环境自愈前销毁失败 → 告警吞掉，重建照常完成。"""
        mod = _load_manager()
        provider = FakeProvider(IsolationLevel.CONTAINER)
        provider._status_override = EnvironmentStatus.ERROR
        mgr = _make_manager(mod, providers={IsolationLevel.CONTAINER: provider})

        async def _explode(*args: Any, **kwargs: Any) -> bool:
            raise RuntimeError("destroy crashed")

        mgr.destroy_environment = _explode  # type: ignore[method-assign]
        new_env = _ready_env(env_id="cua-a-2")
        mgr.get_or_create_environment = _async_return(new_env)  # type: ignore[method-assign]
        env = _ready_env(env_id="cua-a")
        result = _run(
            mgr._ensure_env_healthy_or_rebuild(
                env, rebuild_kwargs={"task_id": "t1", "task_type": TaskType.ATOMIC}
            )
        )
        assert result is new_env

    def test_rebuild_failure_returns_original_env(self) -> None:
        """非就绪环境重建失败 → 返回原环境（执行将以错误告终，不形成循环）。"""
        mod = _load_manager()
        provider = FakeProvider(IsolationLevel.CONTAINER)
        provider._status_override = EnvironmentStatus.ERROR
        mgr = _make_manager(mod, providers={IsolationLevel.CONTAINER: provider})

        async def _fail(**kwargs: Any) -> IsolationEnvironment:
            raise RuntimeError("rebuild down")

        mgr.get_or_create_environment = _fail  # type: ignore[method-assign]
        env = _ready_env(env_id="cua-a")
        mgr._environments["cua-a"] = env
        result = _run(
            mgr._ensure_env_healthy_or_rebuild(
                env, rebuild_kwargs={"task_id": "t1", "task_type": TaskType.ATOMIC}
            )
        )
        assert result is env


# ═══════════════════════════════════════════════════════════
# start() 启动期 prune 调度 / 查找与按名销毁的外层异常吞噬 / EIO 自愈 WSL 分支
# ═══════════════════════════════════════════════════════════


class TestStartPruneScheduling:
    def test_start_launches_background_prune_when_due(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """到期应清理 → start() 调度后台 prune 任务并保存引用；任务正常收尾。"""
        mod = _load_manager()
        mgr = _make_manager(mod)
        assert mgr._prune_task is None
        monkeypatch.setattr(mgr, "_should_prune", lambda: True)
        monkeypatch.setattr(mgr, "_resume_containers", _async_return(None))
        monkeypatch.setattr(mgr, "_prune_docker_images", _async_return(None))

        async def main() -> None:
            await mgr.start()
            assert mgr._prune_task is not None
            await mgr._prune_task  # 后台任务正常收尾，无异常留痕

        _run(main())
        assert mgr._running is True


class TestFindExistingOuterEdges:
    def test_sync_lookup_crash_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """查找阶段同步实现抛异常（daemon 不可达）→ 记录后返回 None，不上抛。"""
        import docker

        mod = _load_manager()
        mgr = _make_manager(mod)

        def _boom(timeout: int = 10) -> None:
            raise RuntimeError("daemon gone")

        monkeypatch.setattr(docker, "from_env", _boom)
        assert _run(mgr._find_existing_container("cua-ws-a")) is None

    def test_stopped_container_is_started_and_adopted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """created/exited 容器可正常启动 → 探针通过后采用为 READY 环境。"""
        import docker

        mod = _load_manager()
        mgr = _make_manager(mod)
        stopped = FakeContainer("cua-ws-a", status="exited", exec_results=[(0, b"")])
        monkeypatch.setattr(docker, "from_env", lambda timeout=10: FakeDockerClient([stopped]))
        env = mgr._find_existing_container_sync("cua-ws-a")
        assert env is not None
        assert env.status == EnvironmentStatus.READY.value
        assert stopped.started is True

    def test_start_failure_removes_container_for_rebuild(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """停止态容器启动失败（如挂载脏路径）→ 删除容器并返回 None（交上层新建）。"""
        import docker

        mod = _load_manager()
        mgr = _make_manager(mod)
        stuck = FakeContainer(
            "cua-ws-a", status="created", start_error=docker.errors.DockerException("dirty mount")
        )
        monkeypatch.setattr(docker, "from_env", lambda timeout=10: FakeDockerClient([stuck]))
        assert mgr._find_existing_container_sync("cua-ws-a") is None
        assert stuck.removed is True

    def test_destroy_by_name_sync_crash_swallowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """按名销毁的同步实现抛异常 → 告警吞掉，不上抛。"""
        import docker

        mod = _load_manager()
        mgr = _make_manager(mod)

        def _boom(timeout: int = 10) -> None:
            raise RuntimeError("daemon gone")

        monkeypatch.setattr(docker, "from_env", _boom)
        _run(mgr._destroy_container_by_name("ws-a"))  # 不上抛


class TestIoErrorHealWslBranch:
    def test_wsl_provider_attempts_host_repair_then_rebuilds(self) -> None:
        """WSL docker 模式命中 EIO → 先尝试修宿主挂载再重建容器；
        Windows 直跑时宿主修复走 Linux 专属分支前置检查返回 False，
        重建重试仍成功并标记 io_error_recovered（无 host_mount_repaired）。"""
        mod = _load_manager()
        docker_stub = sys.modules["providers.docker_provider"]

        # 真实 DockerProvider 类经 sys.modules 动态取（平铺防串扰），基类只能
        # 用 type() 运行时构造（mypy 不接受动态 base class 语句）
        _WSLProvider: Any = type(
            "_WSLProvider",
            (ScriptedProvider, getattr(docker_stub, "DockerProvider")),
            {
                "_is_wsl_docker": lambda self: True,
                "_resolve_mount_path": lambda self, workspace: "/mnt/d/proj/ws-a",
            },
        )

        eio = ExecutionResult(success=False, output=None, error="Input/output error")
        provider = _WSLProvider(
            IsolationLevel.CONTAINER, exec_script=(eio, ExecutionResult(success=True, output={"stdout": "ok"}, metadata={}))
        )
        mgr = _make_manager(mod, providers={IsolationLevel.CONTAINER: provider})
        mgr._find_existing_container = _async_return(None)  # type: ignore[method-assign]
        result = _run(
            mgr.execute_in_isolation(
                task_id="t1",
                task_type=TaskType.ATOMIC,
                operation={"type": "command"},
                workspace="/proj/ws-a",
            )
        )
        assert result.success is True
        assert result.metadata["io_error_recovered"] is True
        assert result.metadata.get("host_mount_repaired") is None


# ═══════════════════════════════════════════════════════════
# 查询辅助
# ═══════════════════════════════════════════════════════════


class TestQueryHelpers:
    def test_list_environments_filters_by_task_id(self) -> None:
        """list_environments(task_id=...) 只保留该任务的环境；无过滤返回全部。"""
        mod = _load_manager()
        mgr = _make_manager(mod)
        for env_id, task_id in [("a", "t1"), ("b", "t2"), ("c", "t1")]:
            env = _ready_env(env_id=env_id)
            env.context.task_id = task_id
            mgr._environments[env_id] = env
        filtered = _run(mgr.list_environments(task_id="t1"))
        assert {e.env_id for e in filtered} == {"a", "c"}
        assert len(_run(mgr.list_environments())) == 3

    def test_load_active_workspace_keys_corrupt_task_fail_closed(self) -> None:
        """任务 status 读取抛异常 → 整体 fail-closed 返回 None。"""
        mod = _load_manager()
        mgr = _make_manager(mod)

        class _CorruptTask:
            @property
            def status(self) -> Any:
                raise RuntimeError("corrupt task record")

        mgr.set_task_repository(FakeTaskRepo({"t-bad": _CorruptTask()}))
        assert _run(mgr._load_active_workspace_keys()) is None
