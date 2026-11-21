# @feature: FP-0.2.二 | @vision: V2 安全
# @ci: python-coverage
"""isolation_guard async handler 事件循环活性锁（合宿心跳不冻）。

事故根因（2026-11-12）：插件 SDK ``_invoke_callable`` 只对**同步** handler 做
``asyncio.to_thread`` 下放，async handler 直接在合宿宿主事件循环里执行。
``execute`` 的可用性复检段同步调用 ``_ensure_engine``（wsl_health：docker
version 5s + PowerShell 30s + WSL 等待 30s）与 ``_probe_backend``（docker/wsl
探测 3s），事件循环被饿死；宿主 watchdog 判心跳停滞 >30s 即整组
``os._exit(1)``（2026-11-12 装机内核因此异常重启）。

锁面：
- 复检段（引擎自愈 + 后端探测 + 冷却打点）整体出事件循环——观测指标：心跳
  tick 间隔不出现 ≥0.1s 空窗（阻塞量级 0.3s+0.25s，红绿区分度充足）；
- 同实例多管道并发 execute：per-instance asyncio.Lock 覆盖「检查→建立」全过程，
  入场后重查冷却（双检）——引擎只拉起一次、探测只 spawn 一次；
- 原生后端承载 CONTAINER 时跳过 docker 专属引擎自愈（既有语义零变化）；
- 冷却窗口内的复检零成本（不 spawn 子进程）。

外部依赖（wsl_health 引擎自愈 / docker 探测 / 环境服务 caller）按测试纪律替换
为假身；execute 的决策与预填逻辑全真实。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import tempfile
import threading
import time
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

_THIS_DIR = Path(__file__).resolve().parent
_SHARED_DIR = _THIS_DIR.parents[0]  # plugins/shared/
_SYSTEM_DIR = _SHARED_DIR / "system"

# 置顶注入 + 裸名逐出（同进程多插件 plugin/decider 裸名串扰防护），
# 随后按唯一模块名装载本插件 plugin.py。
for _d in (str(_SYSTEM_DIR), str(_SHARED_DIR), str(_THIS_DIR)):
    if _d in sys.path:
        sys.path.remove(_d)
    sys.path.insert(0, _d)
for _bare in ("plugin", "decider"):
    sys.modules.pop(_bare, None)

from _libs.pipeline_base.plugin import PluginContext  # noqa: E402
from _libs.pipeline_base.types import StateKeys  # noqa: E402

from agentos_plugin_sdk.isolation_types import IsolationLevel  # noqa: E402


def _load_plugin_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "isolation_guard_plugin_loop_liveness", str(_THIS_DIR / "plugin.py")
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_plugin_module()
IsolationGuard = _mod.IsolationGuard

pytestmark = pytest.mark.unit

# 真实可挂载工作空间（悬空挂载防护要求夹具路径存在；惰性建目录，落在系统临时区）
_WS = str(Path(tempfile.gettempdir()) / "agentos_ig_loop_liveness_ws_fixture")

# 心跳观测阈值：宿主 watchdog 判停滞 30s 太宽（单测不可等），取 0.1s 作
# 「事件循环未被饿死」的机械判据——假身阻塞量级 0.2-0.3s 必破此界。
_MAX_TICK_GAP = 0.1


class _Ticker:
    """心跳协程：记录 tick 间隔 + 被测段收尾时刻；事件循环被同步阻塞时
    max_gap（含末段 tick→收尾）超阈值。

    末段必须计入：阻塞期间停摆的心跳在收尾时无法补点（stop 已置位），
    只比 tick 与 tick 的间隔会把整段停滞漏判成 0。
    """

    def __init__(self, interval: float = 0.01) -> None:
        self._interval = interval
        self._ticks: list[float] = []
        self._stop: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None
        self._end: float = 0.0

    async def __aenter__(self) -> _Ticker:
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run())
        # 预热：让至少两 tick 落在被测调用之前（阻塞段前有基线 tick）
        await asyncio.sleep(self._interval * 3)
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        assert self._stop is not None
        assert self._task is not None
        self._end = time.monotonic()
        self._stop.set()
        await self._task

    async def _run(self) -> None:
        assert self._stop is not None
        while not self._stop.is_set():
            self._ticks.append(time.monotonic())
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except TimeoutError:
                continue

    @property
    def max_gap(self) -> float:
        points = [*self._ticks, self._end]
        return max(
            (b - a for a, b in zip(points, points[1:], strict=False)), default=0.0
        )


# ============================================================
# 辅助
# ============================================================


@pytest.fixture(autouse=True)
def _reset_env_caller() -> Any:
    """每用例后摘除注入的 caller 假身（模块级全局，防跨用例泄漏）。"""
    yield
    _mod.set_env_caller(None)


def _make_guard() -> Any:
    """构造守卫：docker 显式不可用 + 双原生后端显式关闭（探测分派恒走 docker），
    复检来源置为自动并预置过期冷却，decider 按测试真值 bash → 容器。"""
    guard = IsolationGuard(config={
        "docker_available": False,
        "providers": {
            "wsl_native": {"enabled": False},
            "linux_native": {"enabled": False},
        },
    })
    guard._docker_auto = True
    guard._docker_checked_at = time.monotonic() - guard._RECHECK_COOLDOWN * 10

    def _resolve(tool_name: str, category: Any = None) -> Any:
        return _policy(tool_name)

    guard._decider.resolve = MagicMock(side_effect=_resolve)

    async def _create_env(tool_name: str, args: dict[str, Any]) -> Any:
        return {"success": True, "data": {"env_id": "container-abc", "status": "ready"}}

    _mod.set_env_caller(_create_env)
    return guard


def _policy(tool_name: str) -> Any:
    mock = MagicMock()
    mock.isolation = (
        IsolationLevel.CONTAINER if tool_name == "bash" else IsolationLevel.HOST
    )
    return mock


def _install_fake_engine(monkeypatch: Any, calls: dict[str, int], delay: float) -> None:
    """wsl_health.ensure_docker_engine 假身（同步阻塞 delay 秒，计入 calls）。"""
    fake = types.ModuleType("wsl_health")

    def _ensure() -> bool:
        calls["engine"] += 1
        time.sleep(delay)
        return True

    fake.ensure_docker_engine = _ensure  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wsl_health", fake)
    monkeypatch.setattr(_mod, "_ensure_isolation_path", lambda: None)


def _install_fake_detect(
    monkeypatch: Any, calls: dict[str, int], delay: float, result: tuple[str, str]
) -> None:
    """docker 探测假身（同步阻塞 delay 秒；返回值即三态探测结果）。"""

    def _detect() -> tuple[str, str]:
        calls["probe"] += 1
        time.sleep(delay)
        return result

    monkeypatch.setattr(IsolationGuard, "_detect_docker", staticmethod(_detect))


def _task_state(tool_calls: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """L2 隔离任务调用轮状态（workspace 真实存在，绕开主 agent 分支）。"""
    if not Path(_WS).exists():
        Path(_WS).mkdir(parents=True, exist_ok=True)
    return {
        StateKeys.CORE_TYPE: "tool_execute",
        StateKeys.TASK_ID: "t-liveness",
        StateKeys.AGENT_LEVEL: "L2",
        "workspace": _WS,
        "execution_context": {"isolation": {"level": "isolated"}},
        StateKeys.RAW_TOOL_CALLS: (
            tool_calls
            if tool_calls is not None
            else [{"name": "bash", "args": {"command": "cat .probe/a.txt"}, "id": "call-1"}]
        ),
    }


def _ctx(state: dict[str, Any]) -> PluginContext:
    return PluginContext(state=state, config={})


# ============================================================
# 观测仪自检：心跳能看见同步阻塞（防测试空转假绿）
# ============================================================


async def test_ticker_detects_blocking_call() -> None:
    """仪器的区分度自证：handler 内同步 sleep 必被 max_gap 捕获。"""
    async with _Ticker() as ticker:
        time.sleep(0.3)
    assert ticker.max_gap >= 0.2


# ============================================================
# 主锁：复检段（引擎自愈 + 后端探测）不冻事件循环
# ============================================================


async def test_recheck_engine_ensure_and_probe_keep_event_loop_ticking(
    monkeypatch: Any,
) -> None:
    """引擎自愈（0.3s）+ docker 探测（0.25s）期间心跳持续打点，复检结果照常生效。"""
    guard = _make_guard()
    calls = {"engine": 0, "probe": 0}
    _install_fake_engine(monkeypatch, calls, delay=0.3)
    _install_fake_detect(monkeypatch, calls, delay=0.25, result=("available", ""))

    async with _Ticker() as ticker:
        result = await guard.execute(_ctx(_task_state()))

    assert ticker.max_gap < _MAX_TICK_GAP
    assert calls == {"engine": 1, "probe": 1}
    # 复检结果照常写回实例状态：可用性恢复 → 容器要求型工具进容器
    assert guard._docker_available is True
    assert guard._docker_probe_error is None
    assert result.state_updates["execution_contexts"][0]["provider"] == "docker"


async def test_recheck_probe_error_keeps_container_tools_fail_closed(
    monkeypatch: Any,
) -> None:
    """探测异常（可用性不可验证）→ probe_error 落回实例态，容器要求型工具拒绝。"""
    guard = _make_guard()
    calls = {"engine": 0, "probe": 0}
    _install_fake_engine(monkeypatch, calls, delay=0.0)
    _install_fake_detect(monkeypatch, calls, delay=0.0, result=("probe_error", "boom"))

    result = await guard.execute(_ctx(_task_state()))

    assert guard._docker_available is False
    assert guard._docker_probe_error == "boom"
    contexts = result.state_updates["execution_contexts"]
    assert contexts[0]["provider"] == "denied"
    assert contexts[0]["reason"] == "docker_probe_error: boom"
    assert contexts[0]["blocked"] is True
    assert len(result.state_updates["pre_decided_results"]) == 1


async def test_recheck_still_unavailable_stays_fail_closed(monkeypatch: Any) -> None:
    """复检仍不可用（明确 absent）→ 维持拒绝 + host 标记，不降级裸跑。"""
    guard = _make_guard()
    calls = {"engine": 0, "probe": 0}
    _install_fake_engine(monkeypatch, calls, delay=0.0)
    _install_fake_detect(monkeypatch, calls, delay=0.0, result=("absent", ""))

    result = await guard.execute(_ctx(_task_state()))

    assert guard._docker_available is False
    assert guard._docker_probe_error is None
    contexts = result.state_updates["execution_contexts"]
    assert contexts[0]["provider"] == "denied"
    assert contexts[0]["reason"] == "docker_unavailable_container_required"


async def test_concurrent_execute_probes_backend_once(monkeypatch: Any) -> None:
    """同实例三管道并发 execute：per-instance 锁串行复检段，引擎只拉起一次、
    探测只 spawn 一次（先到者探测期间后到者排队，入场后冷却双检早退）。"""
    guard = _make_guard()
    calls = {"engine": 0, "probe": 0}
    _install_fake_engine(monkeypatch, calls, delay=0.2)
    _install_fake_detect(monkeypatch, calls, delay=0.1, result=("absent", ""))

    await asyncio.gather(*[guard.execute(_ctx(_task_state())) for _ in range(3)])

    assert calls == {"engine": 1, "probe": 1}


async def test_native_backend_skips_docker_engine_ensure(monkeypatch: Any) -> None:
    """原生后端承载 CONTAINER（wsl_native/linux_native）→ 跳过 docker 专属引擎
    自愈（无可救引擎），探测照常执行。"""
    guard = _make_guard()
    monkeypatch.setattr(IsolationGuard, "_native_backend_active", lambda _self: True)
    calls = {"engine": 0, "probe": 0}
    _install_fake_engine(monkeypatch, calls, delay=0.0)
    _install_fake_detect(monkeypatch, calls, delay=0.0, result=("available", ""))

    await guard.execute(_ctx(_task_state()))

    assert calls == {"engine": 0, "probe": 1}
    assert guard._docker_available is True


async def test_within_cooldown_recheck_is_free(monkeypatch: Any) -> None:
    """冷却窗口内不复检：零子进程开销（探测/自愈均不触发）。"""
    guard = _make_guard()
    guard._docker_checked_at = time.monotonic()
    calls = {"engine": 0, "probe": 0}
    _install_fake_engine(monkeypatch, calls, delay=0.0)
    _install_fake_detect(monkeypatch, calls, delay=0.0, result=("available", ""))

    result = await guard.execute(_ctx(_task_state()))

    assert calls == {"engine": 0, "probe": 0}
    assert result.state_updates["execution_contexts"][0]["provider"] == "denied"


# ============================================================
# on_load：实例构造（构造期含后端探测子进程）不冻事件循环
# ============================================================


async def test_on_load_builds_instance_offloop(monkeypatch: Any) -> None:
    """on_load 的实例构造（构造期 _probe_backend → 探测子进程 3s）出事件循环：
    心跳持续打点，且构造确实跑在宿主事件循环线程之外。"""
    path = _THIS_DIR / "server.py"
    spec = importlib.util.spec_from_file_location(
        "isolation_guard_server_loop_liveness", path
    )
    assert spec is not None
    assert spec.loader is not None
    server = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = server
    spec.loader.exec_module(server)

    loop_thread = threading.get_ident()
    build_threads: list[int] = []

    def _slow_get_instance() -> Any:
        build_threads.append(threading.get_ident())
        time.sleep(0.25)
        return MagicMock()

    monkeypatch.setattr(server, "get_instance", _slow_get_instance)

    async with _Ticker() as ticker:
        await server._on_load({})

    assert ticker.max_gap < _MAX_TICK_GAP
    assert len(build_threads) == 1
    assert build_threads[0] != loop_thread
