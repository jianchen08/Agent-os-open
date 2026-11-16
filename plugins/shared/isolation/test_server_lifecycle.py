# @feature: FP-0.2.五 资源治理 | @ci: none-local
"""isolation server.py 生命周期测试（D6 常驻任务回收）+ create_env 处理器契约。

覆盖：
1. on_unload 取消配置 watcher 任务并收敛等待（不取消则 while True 轮询
   在卸载后常驻事件循环，持旧 manager 引用纯泄漏）；
2. on_unload 调用 wsl_health.terminate_keepalive()（拉起者负责终止保活会话）；
3. watcher 未起时 on_unload 幂等；
4. isolation.create_env 处理器失败形态（B5 职责归位：guard 弃私有 manager
   后经本正门落地，失败必须显式 success=False，裸 error dict 会被内核 ③
   归一包成 success=true 信封谎报成功）：
   - manager 未初始化 → 显式失败；
   - 配额超限（manager 抛 RuntimeError）→ 显式失败携原因（配额/熔断在
     服务进程单实例计，guard 不再私建 manager 分裂计数）；
   - 成功响应补带 exec_backend / provider_error（消费方凭同一响应完成
     落地与 bash 传输通道注入，ERROR 占位原因可回传）；
5. B5 收尾端到端：登记 READY 但 daemon 侧容器已死 → 真实 manager 复用
   快路径验活后同名重建（信封不外返死 env_id），第二轮验活通过复用。

隔离策略：wsl_health.terminate_keepalive 用 monkeypatch 替身（wsl.exe 是
外部依赖）；watcher 用真实任务驱动（config yaml 只 stat 不写）；manager
注入假身（handler 测试不触真实 docker）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent  # plugins/shared/isolation/
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_mod() -> Any:
    """动态加载 server.py（唯一模块名，防与其它测试的裸名 server 冲突）。"""
    for _bare in ("server",):
        sys.modules.pop(_bare, None)
    mod_name = "isolation_server_lifecycle_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "server.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_MOD = _load_mod()


def test_on_unload_cancels_config_watcher_and_terminates_keepalive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """on_unload：watcher 任务 cancel 收敛（done+cancelled）+ 保活句柄终止。"""
    terminated: list[bool] = []
    monkeypatch.setattr(_MOD.wsl_health, "terminate_keepalive", lambda: terminated.append(True))

    tasks: list[asyncio.Task] = []

    async def _scenario() -> None:
        watcher = asyncio.create_task(_MOD._watch_config_reload())
        tasks.append(watcher)
        _MOD._config_watcher_task = watcher
        await asyncio.sleep(0)  # watcher 进入轮询（stat → sleep(5) 窗口）
        assert not watcher.done()
        await _MOD._on_unload({})

    asyncio.run(_scenario())

    watcher = tasks[0]
    assert watcher.done(), "卸载后 watcher 任务应已收敛"
    assert watcher.cancelled(), "watcher 应经 cancel 收敛而非自然退出"
    assert _MOD._config_watcher_task is None
    assert terminated == [True], "on_unload 应调用 terminate_keepalive"


def test_on_unload_idempotent_without_watcher(monkeypatch: pytest.MonkeyPatch) -> None:
    """watcher 未起/已清（_config_watcher_task=None）时 on_unload 幂等不抛。"""
    terminated: list[bool] = []
    monkeypatch.setattr(_MOD.wsl_health, "terminate_keepalive", lambda: terminated.append(True))

    _MOD._config_watcher_task = None
    _MOD._manager = None

    asyncio.run(_MOD._on_unload({}))

    assert _MOD._config_watcher_task is None
    assert terminated == [True]


# ── isolation.create_env 处理器契约（B5 职责归位）──────────────


def test_create_env_manager_missing_explicit_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """manager 未初始化 → 显式 success=False（裸 error dict 会被内核包成成功信封）。"""
    monkeypatch.setattr(_MOD, "_manager", None)
    out = asyncio.run(_MOD.isolation_create_env(task_id="t-1"))
    assert out["success"] is False
    assert "未初始化" in out["error"]


def test_create_env_quota_rejection_explicit_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """服务侧配额拒绝路径：manager 抛 RuntimeError（超限）→ 显式失败信封携原因。

    配额/熔断单实例语义的运行时锚：拒绝在服务进程内判定并显式外传，
    消费方（isolation_guard）转 blocked 而非吞成成功。
    """
    reason = "隔离环境数量已达上限 (3/3)，无法为任务创建新环境。"

    class _QuotaManager:
        async def get_or_create_environment(self, **kwargs: Any) -> Any:
            raise RuntimeError(reason)

    monkeypatch.setattr(_MOD, "_manager", _QuotaManager())
    out = asyncio.run(_MOD.isolation_create_env(task_id="t-1", workspace="/ws"))
    assert out["success"] is False
    assert "已达上限" in out["error"]


def test_create_env_success_carries_backend_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功响应补带 exec_backend / provider_error（docker 环境为 null / 空串）。"""
    env = SimpleNamespace(
        env_id="cua-real_ws",
        level=SimpleNamespace(value="isolated"),
        provider_type="cua",
        status="ready",
        context=SimpleNamespace(task_id="t-1"),
        provider_info={"container_id": "abc"},
    )

    class _OkManager:
        async def get_or_create_environment(self, **kwargs: Any) -> Any:
            return env

    monkeypatch.setattr(_MOD, "_manager", _OkManager())
    out = asyncio.run(_MOD.isolation_create_env(task_id="t-1", workspace="/ws"))
    assert out["env_id"] == "cua-real_ws"
    assert out["status"] == "ready"
    assert out["exec_backend"] is None
    assert out["provider_error"] == ""


def test_create_env_error_env_carries_provider_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ERROR 占位环境（创建失败）：status=error + provider_error 携真实原因
    （消费方转 blocked 时留原因日志，假 env_id 不外传为成功）。"""
    env = SimpleNamespace(
        env_id="docker-session",
        level=SimpleNamespace(value="isolated"),
        provider_type="cua",
        status="error",
        context=SimpleNamespace(task_id="session"),
        provider_info={"error": "Invalid container name (cua-修仙游戏)"},
    )

    class _ErrManager:
        async def get_or_create_environment(self, **kwargs: Any) -> Any:
            return env

    monkeypatch.setattr(_MOD, "_manager", _ErrManager())
    out = asyncio.run(_MOD.isolation_create_env(task_id="session"))
    assert out["status"] == "error"
    assert out["provider_error"] == "Invalid container name (cua-修仙游戏)"


# ── B5 收尾端到端：死登记不外返（manager 复用快路径每轮验活）────────


def test_create_env_dead_registration_rebuilds_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """登记 READY 但 daemon 侧容器已死 → create_env 信封不得外返死登记。

    真实 manager + 假 CONTAINER provider（假 daemon：创建前查无、创建后按名
    可寻；daemon dict 初始为空 = daemon 重启已清容器）走 isolation.create_env
    正门：第一轮验活发现死登记 → 清登记同名重建（信封无 success=False，
    env_id 为同名环境）；第二轮验活通过 → 复用不重复建。快路径免验活的旧
    契约下第一轮直接外返死 env_id（bash 每轮 "No such container"）。

    manager 取 sys.modules 里 server.py 装载时绑定的同源实例（server.py 导入
    前已把本目录置于 sys.path[0]，裸名 manager 必为本插件实现模块）。
    """
    import manager as manager_mod

    from agentos_plugin_sdk.isolation_types import (
        IsolationContext,
        IsolationEnvironment,
        IsolationLevel,
        TaskType,
    )

    stale = IsolationEnvironment(
        env_id="cua-e2e-ws",
        level=IsolationLevel.CONTAINER,
        provider_type="cua",
        status="ready",
        context=IsolationContext(
            task_id="before-restart",
            task_type=TaskType.ATOMIC,
            isolation_level=IsolationLevel.CONTAINER,
        ),
    )
    daemon: dict[str, IsolationEnvironment] = {}
    created: list[str] = []

    class _FakeContainerProvider:
        """假 docker provider：create 即在假 daemon 注册同名容器。"""

        async def is_available(self) -> tuple[bool, str]:
            return True, ""

        async def create_environment(
            self, context: Any, container_name: str | None = None
        ) -> Any:
            env = IsolationEnvironment(
                env_id=container_name or "cua-orphan",
                level=IsolationLevel.CONTAINER,
                provider_type="cua",
                status="ready",
                context=context,
                provider_info={"container_id": "beef"},
            )
            created.append(env.env_id)
            daemon[env.env_id] = env
            return env

    mgr = manager_mod.IsolationManager(
        providers={IsolationLevel.CONTAINER: _FakeContainerProvider()}
    )
    mgr._resource_profile = {"max_environments": 8, "tier": "test"}

    async def find_by_name(name: str) -> Any:
        return daemon.get(name)

    mgr._find_existing_container = find_by_name  # type: ignore[method-assign]
    # daemon 重启前的内存登记仍在，daemon 侧容器已被清（dict 为空）
    mgr._environments[stale.env_id] = stale
    mgr._workspace_env_map["e2e-ws"] = stale.env_id
    monkeypatch.setattr(_MOD, "_manager", mgr)

    kwargs: dict[str, Any] = {
        "task_id": "t-e2e",
        "task_type": "atomic",
        "operation_type": "code_execution",
        "workspace": "/proj/e2e-ws",
        "isolation_level": "isolated",
    }
    out1 = asyncio.run(_MOD.isolation_create_env(**kwargs))
    assert out1.get("success") is not False
    assert out1["env_id"] == "cua-e2e-ws"  # 同名重建，env_id 稳定
    assert out1["status"] == "ready"
    assert created == ["cua-e2e-ws"], "死登记应触发同名重建"

    out2 = asyncio.run(_MOD.isolation_create_env(**kwargs))
    assert out2["env_id"] == "cua-e2e-ws"
    assert created == ["cua-e2e-ws"], "第二轮验活通过应复用，不重复建"
