# @feature: FP-0.2.〇 TB参赛适配 | @ci: none-local
"""isolation manager 外部容器绑定通道测试（Terminal-Bench 参赛适配）。

覆盖（manager.py 外部通道新增面）：
1. bind_external_env 校验：空 container / cua- 前缀拒绝 / inspect 失败显式
   success=False 且不登记 / 成功绑定双键（ws_key + task_id）登记；
2. get_or_create_environment 拦截：命中绑定走外部收养（不建 cua- 容器、
   不查通用路径）、幂等复用（provider.create 仅一次）、task_id 键命中；
3. 销毁分流：destroy_environment 对外部环境走绑定 provider（解登记不删容器，
   零 docker 调用）；provider 缺失时仅清登记返回 True；
4. 自愈跳过：外部环境非就绪不重建（_ensure_env_healthy_or_rebuild）、
   setns 特征错误不触发 destroy+recreate 自愈（错误如实回传）；
5. unbind：双键解除、幂等。

隔离策略：manager 顶层 import 的 decider/providers.*/hardware_profile 全部用
sys.modules 伪模块注入（与 test_isolation_manager.py 同款约定）；外部 provider
桩为功能型 Fake（记录调用、可配结果），不 mock 被测对象（manager 本体真实）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
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

_PLUGIN_DIR = Path(__file__).resolve().parent

_STUB_KEYS = (
    "decider",
    "providers",
    "providers.base",
    "providers.docker_provider",
    "providers.external_docker_provider",
    "providers.host_provider",
    "providers.wsl_native_provider",
    "providers.linux_native_provider",
    "hardware_profile",
    "workspace",
    "tasks",
    "tasks.types",
)


class FakeExternalProvider:
    """功能型外部 provider 桩：记录调用、结果可配（类级实例登记供断言）。"""

    instances: list[FakeExternalProvider] = []
    # 类级预配置队列：manager 内部构造的下一个实例继承（bind 失败场景预置用）
    next_inspect_results: list[tuple[int, str, str, str]] = []

    def __init__(self, container_ref: str, workdir: str = "/workspace", config: Any = None) -> None:
        self.container_ref = container_ref
        self.workdir = workdir
        self._environments: dict[str, IsolationEnvironment] = {}
        # inspect 队列：(rc, id, name, err)；构造时继承类级预配置
        self.inspect_results: list[tuple[int, str, str, str]] = list(
            type(self).next_inspect_results
        )
        type(self).next_inspect_results = []
        # status 队列：EnvironmentStatus；空 → READY
        self.status_results: list[EnvironmentStatus] = []
        # exec 队列：ExecutionResult；空 → 成功
        self.exec_results: list[ExecutionResult] = []
        self.created: list[IsolationContext] = []
        self.destroyed: list[str] = []
        self.executed: list[tuple[str, dict]] = []
        FakeExternalProvider.instances.append(self)

    async def inspect_target(self) -> tuple[int, str, str, str]:
        # 契约与真 provider 一致：name 已剥 docker .Name 前导斜杠
        if self.inspect_results:
            return self.inspect_results.pop(0)
        return 0, "cid-" + self.container_ref, self.container_ref, ""

    async def create_environment(
        self, context: IsolationContext, container_name: str | None = None
    ) -> IsolationEnvironment:
        rc, cid, name, err = await self.inspect_target()
        self.created.append(context)
        if rc != 0:
            env = IsolationEnvironment(
                env_id=f"external-{self.container_ref}",
                level=IsolationLevel.CONTAINER,
                provider_type="external_docker",
                status=EnvironmentStatus.ERROR.value,
                context=context,
                provider_info={"error": f"外部容器不可达: {err}"},
            )
        else:
            env = IsolationEnvironment(
                env_id=name or self.container_ref,
                level=IsolationLevel.CONTAINER,
                provider_type="external_docker",
                status=EnvironmentStatus.READY.value,
                context=context,
                provider_info={
                    "container_id": cid,
                    "container_name": name or self.container_ref,
                    "workdir": self.workdir,
                    "external": True,
                },
            )
        self._environments[env.env_id] = env
        return env

    async def destroy_environment(self, env_id: str, success: bool = True) -> bool:
        self.destroyed.append(env_id)
        self._environments.pop(env_id, None)
        return True

    async def get_environment_status(self, env_id: str) -> EnvironmentStatus:
        if self.status_results:
            return self.status_results.pop(0)
        return EnvironmentStatus.READY

    async def execute_in_environment(self, env_id: str, operation: dict) -> ExecutionResult:
        self.executed.append((env_id, operation))
        if self.exec_results:
            return self.exec_results.pop(0)
        return ExecutionResult(success=True, output={"stdout": "ok"})


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
            self.created: list[Any] = []

        async def create_environment(self, context: Any, container_name: Any = None) -> Any:
            self.created.append(context)
            raise AssertionError("外部绑定拦截失效：自建 DockerProvider 被调用")

        @classmethod
        def _is_namespace_desync_error(cls, err: str | bytes | None) -> bool:
            if not err:
                return False
            if isinstance(err, bytes):
                err = err.decode("utf-8", errors="replace")
            return any(m in err.lower() for m in cls._NAMESPACE_DESYNC_MARKERS)

        @classmethod
        def _is_io_error(cls, err: str | bytes | None) -> bool:
            if not err:
                return False
            if isinstance(err, bytes):
                err = err.decode("utf-8", errors="replace")
            return any(m in err.lower() for m in cls._IO_ERROR_MARKERS)

        def _is_wsl_docker(self) -> bool:
            return False

    docker_mod.DockerProvider = DockerProvider
    sys.modules["providers.docker_provider"] = docker_mod

    external_mod = types.ModuleType("providers.external_docker_provider")
    external_mod.ExternalDockerProvider = FakeExternalProvider
    external_mod.PROVIDER_TYPE = "external_docker"
    sys.modules["providers.external_docker_provider"] = external_mod

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

    linux_mod = types.ModuleType("providers.linux_native_provider")

    class LinuxNativeProvider:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._config = kwargs.get("config", {})

    linux_mod.LinuxNativeProvider = LinuxNativeProvider
    sys.modules["providers.linux_native_provider"] = linux_mod

    # workspace（R7 锚目录）：get_workspace_base_dir 经 _HOLDER 可控，
    # 默认给 tmp 无关路径（仅锚落盘测试显式设置）
    workspace_mod = types.ModuleType("workspace")
    workspace_mod.get_workspace_base_dir = lambda: _ANCHOR_HOLDER["base"]
    sys.modules["workspace"] = workspace_mod

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

    class TaskStatus(str, Enum):  # noqa: UP042 — 与 test_isolation_manager.py 桩同形
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
    saved = {k: sys.modules.get(k) for k in _STUB_KEYS}
    FakeExternalProvider.instances = []
    _ANCHOR_HOLDER["base"] = Path(os.getcwd()) / "unused-anchor-base"
    _install_stubs()
    yield
    for key, value in saved.items():
        if value is None:
            sys.modules.pop(key, None)
        else:
            sys.modules[key] = value


def _load_manager() -> Any:
    mod_name = "isolation_manager_binding_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "manager.py")
    assert spec is not None
    assert spec.loader is not None
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


def _make_manager() -> Any:
    manager_cls = _load_manager().IsolationManager
    return manager_cls(
        providers={},  # 无 level provider：任何自建路径都会显式失败（拦截失效即测试红）
        providers_config_override={"providers": {}},
    )


_ANCHOR_HOLDER: dict = {"base": None}
_WS = "D:\\eval\\ws-tb-1"


# ═══════════════════════════════════════════════════════════
# bind 校验
# ═══════════════════════════════════════════════════════════


class TestBindValidation:
    def test_bind_rejects_empty_and_cua_prefix(self) -> None:
        mgr = _make_manager()
        res = _run(mgr.bind_external_env("t-1", _WS, ""))
        assert res["success"] is False
        res = _run(mgr.bind_external_env("t-1", _WS, "cua-lookalike"))
        assert res["success"] is False
        assert "cua-" in res["error"]
        assert mgr._external_bindings == {}

    def test_bind_missing_container_explicit_fail_no_registration(self) -> None:
        mgr = _make_manager()
        # 预置 manager 内部构造实例的 inspect 结果：容器不存在
        FakeExternalProvider.next_inspect_results = [(1, "", "", "No such container: ghost-box")]
        res = _run(mgr.bind_external_env("t-1", _WS, "ghost-box"))
        assert res["success"] is False
        assert "外部容器不可达" in res["error"]
        # 不留任何绑定与 provider 缓存（无效绑定不会让后续请求误入外部通道）
        assert mgr._external_bindings == {}
        assert mgr._external_providers == {}

    def test_bind_success_registers_both_keys(self) -> None:
        mgr = _make_manager()
        res = _run(mgr.bind_external_env("task-A", _WS, "tb-box", workdir="/app"))
        assert res["success"] is True
        assert res["container_name"] == "/tb-box".lstrip("/")
        ws_key = "ws-tb-1"
        assert ws_key in mgr._external_bindings
        assert "task-A" in mgr._external_bindings
        assert mgr._external_bindings[ws_key]["workdir"] == "/app"
        # inspect 走了一次（bind 即验证）
        assert len(FakeExternalProvider.instances) >= 1

    def test_bind_requires_task_and_workspace(self) -> None:
        mgr = _make_manager()
        assert _run(mgr.bind_external_env("", _WS, "tb-box"))["success"] is False
        assert _run(mgr.bind_external_env("t-1", "", "tb-box"))["success"] is False

    def test_bind_materializes_kernel_side_anchor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """跨宿主编排（R7）：编排器宿主 workspace 内核侧不存在 → 落锚回传规范路径。

        真内核实证根因：Harbor（Ubuntu 侧）trial workspace（编排器相对
        POSIX 路径）在 Windows 内核侧不存在——guard 悬空挂载预检在绑定查找
        **之前**拒绝，bash 全线 container_create_failed。外部容器无挂载源，
        workspace 仅承担绑定键 + 预检存在性：内核侧落空锚目录（basename
        不变 → 绑定键两端一致）并把规范路径回传。
        """
        import workspace as _workspace_mod

        monkeypatch.setattr(_workspace_mod, "get_workspace_base_dir", lambda: tmp_path / "wsbase")
        mgr = _make_manager()
        orchestrator_ws = (
            "jobs-agentos/2026-11-16__13-58-26/trial_x/agent/agentos-workspaces"
            "/tb-834dd9ebd2c8"
        )
        res = _run(mgr.bind_external_env("task-R7", orchestrator_ws, "tb-r7-box"))
        assert res["success"] is True
        canonical = res["workspace"]
        # 锚已落（内核侧真实存在，guard 预检可过）且 basename 不变（键一致）
        assert Path(canonical).is_dir()
        assert Path(canonical).name == "tb-834dd9ebd2c8"
        assert Path(canonical).is_relative_to(tmp_path / "wsbase")
        assert mgr._external_bindings["tb-834dd9ebd2c8"]["workspace"] == canonical
        # guard 实际调用形态：经规范 workspace + 主会话 task_id 走 get_or_create
        env = _run(
            mgr.get_or_create_environment(
                task_id="session",
                task_type=TaskType.ATOMIC,
                workspace=canonical,
            )
        )
        assert env.provider_type == "external_docker"
        assert env.env_id == "tb-r7-box"

    def test_bind_existing_workspace_passthrough(self, tmp_path: Path) -> None:
        """内核侧已存在的 workspace（B2 同宿主形态）→ 原样回传不落锚。"""
        existing = tmp_path / "exists-ws"
        existing.mkdir()
        mgr = _make_manager()
        res = _run(mgr.bind_external_env("task-B2", str(existing), "tb-box"))
        assert res["workspace"] == str(existing)
        assert not (tmp_path / "wsbase").exists()  # 未落锚


# ═══════════════════════════════════════════════════════════
# get_or_create_environment 拦截
# ═══════════════════════════════════════════════════════════


class TestGetOrCreateIntercept:
    def _bound_manager(self) -> Any:
        mgr = _make_manager()
        res = _run(mgr.bind_external_env("task-A", _WS, "tb-box", workdir="/app"))
        assert res["success"] is True
        return mgr

    def test_intercept_adopts_external_env_no_cua_creation(self) -> None:
        mgr = self._bound_manager()
        env = _run(
            mgr.get_or_create_environment(
                task_id="task-A",
                task_type=TaskType.ATOMIC,
                operation_type=OperationType.CODE_EXECUTION,
                workspace=_WS,
            )
        )
        assert env.provider_type == "external_docker"
        assert env.status == EnvironmentStatus.READY.value
        assert env.env_id == "tb-box"
        assert env.provider_info["workdir"] == "/app"
        # 注册进映射（销毁面按 ws_key 找得到）
        assert mgr._workspace_env_map["ws-tb-1"] == "tb-box"
        assert mgr._environments["tb-box"] is env

    def test_intercept_idempotent_reuse_one_create(self) -> None:
        mgr = self._bound_manager()
        env1 = _run(
            mgr.get_or_create_environment(
                task_id="task-A",
                task_type=TaskType.ATOMIC,
                workspace=_WS,
            )
        )
        env2 = _run(
            mgr.get_or_create_environment(
                task_id="task-A",
                task_type=TaskType.ATOMIC,
                workspace=_WS,
            )
        )
        assert env1 is env2
        # 收养只发生一次（第二轮走 READY 验活复用）
        adopt_calls = [p for p in FakeExternalProvider.instances if p.created]
        assert len(adopt_calls) == 1
        assert len(adopt_calls[0].created) == 1

    def test_intercept_by_task_id_key(self) -> None:
        mgr = self._bound_manager()
        # workspace 不同但 task_id 命中绑定 → 仍走外部通道
        env = _run(
            mgr.get_or_create_environment(
                task_id="task-A",
                task_type=TaskType.ATOMIC,
                workspace="D:\\eval\\other-ws",
            )
        )
        assert env.provider_type == "external_docker"

    def test_no_binding_normal_path_unaffected(self) -> None:
        # 无绑定时：CONTAINER 槽位为空 → 显式失败（找不到提供者），
        # 证明默认行为未被外部通道改动
        mgr = _make_manager()
        with pytest.raises(Exception, match="提供者"):
            _run(
                mgr.get_or_create_environment(
                    task_id="task-B",
                    task_type=TaskType.ATOMIC,
                    workspace="D:\\eval\\plain",
                    isolation_level=IsolationLevel.CONTAINER,
                )
            )


# ═══════════════════════════════════════════════════════════
# 销毁分流
# ═══════════════════════════════════════════════════════════


class TestDestroyRouting:
    def test_destroy_external_unregisters_only(self) -> None:
        mgr = _make_manager()
        _run(mgr.bind_external_env("task-A", _WS, "tb-box"))
        env = _run(
            mgr.get_or_create_environment(
                task_id="task-A", task_type=TaskType.ATOMIC, workspace=_WS
            )
        )
        provider = FakeExternalProvider.instances[-1]
        ok = _run(mgr.destroy_environment(env.env_id))
        assert ok is True
        assert provider.destroyed == ["tb-box"]
        assert env.env_id not in mgr._environments
        assert mgr._workspace_env_map == {}

    def test_destroy_by_task_id_routes_external(self) -> None:
        mgr = _make_manager()
        _run(mgr.bind_external_env("task-A", _WS, "tb-box"))
        _run(
            mgr.get_or_create_environment(
                task_id="task-A", task_type=TaskType.ATOMIC, workspace=_WS
            )
        )
        provider = FakeExternalProvider.instances[-1]
        # 注入 pipeline_state 聚合行（销毁面 task.ws_meta 坐标通道）
        async def _rows() -> list[dict]:
            return [{"pipeline_id": "task-A", "task.ws_meta": {"path": _WS}}]

        mgr.set_state_rows_provider(_rows)
        _run(mgr.destroy_by_task_id("task-A"))
        assert provider.destroyed == ["tb-box"]

    def test_destroy_external_missing_provider_clears_registration(self) -> None:
        mgr = _make_manager()
        _run(mgr.bind_external_env("task-A", _WS, "tb-box"))
        env = _run(
            mgr.get_or_create_environment(
                task_id="task-A", task_type=TaskType.ATOMIC, workspace=_WS
            )
        )
        # 模拟 provider 缓存丢失（如 manager 重建）：只清登记，绝不 docker rm
        mgr._external_providers.clear()
        ok = _run(mgr.destroy_environment(env.env_id))
        assert ok is True
        assert env.env_id not in mgr._environments


# ═══════════════════════════════════════════════════════════
# 自愈跳过
# ═══════════════════════════════════════════════════════════


class TestSelfHealSkip:
    def test_unready_external_env_not_rebuilt(self) -> None:
        mgr = _make_manager()
        _run(mgr.bind_external_env("task-A", _WS, "tb-box"))
        env = _run(
            mgr.get_or_create_environment(
                task_id="task-A", task_type=TaskType.ATOMIC, workspace=_WS
            )
        )
        provider = FakeExternalProvider.instances[-1]
        provider.status_results = [EnvironmentStatus.STOPPED]
        env2 = _run(mgr._ensure_env_healthy_or_rebuild(env, rebuild_kwargs={}))
        assert env2 is env  # 原样返回：不销毁、不重建
        assert provider.destroyed == []
        assert len(provider.created) == 1

    def test_setns_error_no_rebuild_error_returned(self) -> None:
        mgr = _make_manager()
        _run(mgr.bind_external_env("task-A", _WS, "tb-box"))
        provider = FakeExternalProvider.instances[-1]
        provider.exec_results = [
            ExecutionResult(
                success=False,
                output={"stderr": "OCI runtime exec failed: ... setns process: exit status 1"},
                error="OCI runtime exec failed",
            )
        ]
        result = _run(
            mgr.execute_in_isolation(
                task_id="task-A",
                task_type=TaskType.ATOMIC,
                operation={"type": "command", "command": "ls"},
                workspace=_WS,
            )
        )
        assert result.success is False
        # setns 特征在 output.stderr（error 只携首行）；命中即说明自愈判定入口看过它
        assert "setns" in str(result.output)
        # 自愈未触发：无销毁、无重建（exec 只发生一次）
        assert provider.destroyed == []
        assert len(provider.created) == 1
        assert len(provider.executed) == 1

    def test_normal_exec_success_via_external(self) -> None:
        mgr = _make_manager()
        _run(mgr.bind_external_env("task-A", _WS, "tb-box"))
        provider = FakeExternalProvider.instances[-1]
        result = _run(
            mgr.execute_in_isolation(
                task_id="task-A",
                task_type=TaskType.ATOMIC,
                operation={"type": "command", "command": "echo hi"},
                workspace=_WS,
            )
        )
        assert result.success is True
        assert result.output == {"stdout": "ok"}
        assert len(provider.executed) == 1


# ═══════════════════════════════════════════════════════════
# unbind
# ═══════════════════════════════════════════════════════════


class TestUnbind:
    def test_unbind_removes_both_keys_idempotent(self) -> None:
        mgr = _make_manager()
        _run(mgr.bind_external_env("task-A", _WS, "tb-box"))
        removed = mgr.unbind_external_env(task_id="task-A", workspace=_WS)
        assert removed >= 1
        assert mgr._external_bindings == {}
        # 幂等：再解绑返回 0
        assert mgr.unbind_external_env(task_id="task-A", workspace=_WS) == 0

    def test_unbind_after_destroy_path_clear(self) -> None:
        mgr = _make_manager()
        _run(mgr.bind_external_env("task-A", _WS, "tb-box"))
        env = _run(
            mgr.get_or_create_environment(
                task_id="task-A", task_type=TaskType.ATOMIC, workspace=_WS
            )
        )
        _run(mgr.destroy_environment(env.env_id))
        # 销毁后解绑：绑定键仍在（销毁只清环境映射），解绑收干净
        removed = mgr.unbind_external_env(task_id="task-A", workspace=_WS)
        assert removed >= 1
        assert mgr._external_bindings == {}


# ═══════════════════════════════════════════════════════════
# R15：绑定注册表持久化（跨实例/重启路由恒定）
# ═══════════════════════════════════════════════════════════


class TestBindingPersistence:
    def test_bind_persists_to_data_dir(self, tmp_path: Path) -> None:
        """bind 即落盘：数据目录出现注册表文件且含记录（双键重建源）。"""
        store = tmp_path / "data" / "isolation" / "external_bindings.json"
        assert not store.exists()
        mgr = _make_manager()
        res = _run(mgr.bind_external_env("task-A", _WS, "tb-box", workdir="/app"))
        assert res["success"] is True
        assert store.is_file(), "bind 后注册表文件必须落盘"
        import json

        payload = json.loads(store.read_text(encoding="utf-8"))
        rec = payload["records"]["ws-tb-1"]
        assert rec["task_id"] == "task-A"
        assert rec["container_name"] == "tb-box"
        assert rec["workdir"] == "/app"

    def test_lookup_self_heals_from_disk_after_instance_loss(
        self, tmp_path: Path
    ) -> None:
        """R15 核心场景：登记实例消失（新实例内存空）→ 查询经持久化自愈命中。

        修前缺陷复现形态：同 workspace 键 50 次 bound=True / 7 次 bound=False
        间歇（sidecar 多实例/重建后内存注册表丢失，fs 族工具静默回落宿主
        路径语义）——持久化 + 查询未命中自愈重载后路由恒定。
        """
        # 实例 A：登记
        mgr_a = _make_manager()
        assert _run(mgr_a.bind_external_env("task-A", _WS, "tb-box"))["success"] is True
        # 实例 B：全新 manager（内存空，模拟 sidecar 重建/另一 light 实例）
        mgr_b = _make_manager()
        assert mgr_b._external_bindings == {}
        res = mgr_b.lookup_external_binding(task_id="", workspace=_WS)
        assert res == {
            "bound": True,
            "container_name": "tb-box",
            "workdir": "/workspace",
        }, "未命中必须经持久化文件自愈（路由恒定）"
        # 自愈载入后 task_id 键同样可查
        assert mgr_b.lookup_external_binding(task_id="task-A", workspace="")["bound"] is True

    def test_unbind_updates_disk(self, tmp_path: Path) -> None:
        store = tmp_path / "data" / "isolation" / "external_bindings.json"
        mgr = _make_manager()
        _run(mgr.bind_external_env("task-A", _WS, "tb-box"))
        assert store.is_file()
        mgr.unbind_external_env(task_id="task-A", workspace=_WS)
        import json

        payload = json.loads(store.read_text(encoding="utf-8"))
        assert payload["records"] == {}, "解绑后持久化面必须同步收敛"
        # 新实例查询：文件已清 → bound=False（真未绑定语义）
        fresh = _make_manager()
        assert fresh.lookup_external_binding(task_id="", workspace=_WS)["bound"] is False

    def test_corrupt_store_is_tolerated(self, tmp_path: Path) -> None:
        """损坏持久化文件：查询优雅回 bound=False（不崩、内存面保留）。"""
        store = tmp_path / "data" / "isolation" / "external_bindings.json"
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text("{broken json", encoding="utf-8")
        mgr = _make_manager()
        res = mgr.lookup_external_binding(task_id="", workspace=_WS)
        assert res["bound"] is False

    def test_memory_hit_does_not_require_disk(self, tmp_path: Path) -> None:
        """内存命中（本实例登记的绑定）不依赖文件——直查即中。"""
        mgr = _make_manager()
        _run(mgr.bind_external_env("task-A", _WS, "tb-box"))
        # 手动删文件模拟落盘失败残留：内存面不受影响
        store = tmp_path / "data" / "isolation" / "external_bindings.json"
        store.unlink()
        res = mgr.lookup_external_binding(task_id="", workspace=_WS)
        assert res["bound"] is True
