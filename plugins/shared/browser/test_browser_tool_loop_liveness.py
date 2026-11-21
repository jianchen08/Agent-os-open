# @feature: FP-0.2.二 browser 事件循环活性 | @ci: python-coverage
"""browser async 路径事件循环活性锁（合宿心跳不冻）。

事故根因（审计 A-1 / P2-10，2026-11-12）：插件 SDK ``_invoke_callable`` 只对
**同步** handler 做 ``asyncio.to_thread`` 下放，``async def`` handler 直接在合宿
宿主事件循环里执行。本插件两处同步 Bridge 调用落在 async 面内：

- ``tool.py`` ``BrowserTool.execute``（async）→ ``BridgeClient.call``
  （``bridge.yaml timeout_secs: 120``，缺省同为 120s）——Bridge 未起/页面挂起/
  playwright 冷启动卡住时单次阻塞可达 120s；
- ``server.py`` ``http_handle``（async）→ ``BridgeClient.live_ticket``
  （``timeout=min(self._timeout, 15.0)``，前端直播页轮询重复打）。

宿主 watchdog 判主循环心跳停滞 >30s 即整组 ``os._exit(1)``（``light`` 宿主
20 成员连坐），所以这两条不是"工具变慢"，是整宿主自杀。

锁面：Bridge 调用整体出事件循环——观测指标：心跳 tick 间隔不出现 ≥0.1s 空窗
（假身阻塞量级 0.3s，红绿区分度充足），并核对调用确实发生在宿主事件循环
线程之外。

外部依赖（Bridge HTTP 传输）按测试纪律替换为假身；工具/端点的参数映射、
结果归一与信封语义全真实。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

# 心跳观测阈值：宿主 watchdog 判停滞 30s 太宽（单测不可等），取 0.1s 作
# 「事件循环未被饿死」的机械判据——假身阻塞量级 0.3s 必破此界。
_MAX_TICK_GAP = 0.1

# 假身阻塞量级：>2×阈值即可区分（0.3s 同步 sleep 在事件循环内必被 max_gap 捕获）。
_STUB_DELAY = 0.3


class _Ticker:
    """心跳协程：记录 tick 间隔 + 被测段收尾时刻。

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


def _load_tool() -> Any:
    """按文件路径加载 tool.py（唯一模块名，防与其它测试的裸名 tool 串扰）。"""
    mod_name = "browser_tool_loop_liveness"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "tool.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _load_server() -> Any:
    """动态加载 server.py（唯一模块名；与 test_browser_server.py 同款防遮蔽）。"""
    mod_name = "browser_server_loop_liveness"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "server.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


# ============================================================
# 观测仪自检：心跳能看见同步阻塞（防测试空转假绿）
# ============================================================


async def test_ticker_detects_blocking_call() -> None:
    """仪器的区分度自证：in-loop 同步 sleep 必被 max_gap 捕获。"""
    async with _Ticker() as ticker:
        time.sleep(_STUB_DELAY)
    assert ticker.max_gap >= 0.2


# ============================================================
# 主锁 1：工具执行面的 Bridge 调用（A-1，120s 上限）
# ============================================================


async def test_execute_bridge_call_keeps_event_loop_ticking(monkeypatch: Any) -> None:
    """execute → BridgeClient.call（假身阻塞 0.3s）期间心跳持续打点，
    结果照常归一（snapshot_text 透传）。"""
    tool_mod = _load_tool()
    loop_thread = threading.get_ident()
    call_threads: list[int] = []

    class _SlowBridge:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def call(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            call_threads.append(threading.get_ident())
            time.sleep(_STUB_DELAY)
            return {"content": [{"type": "text", "text": "page-snapshot"}]}

    monkeypatch.setattr(tool_mod, "BridgeClient", _SlowBridge)

    async with _Ticker() as ticker:
        result = await tool_mod.BrowserTool({}).execute({"_tool_name": "browser_snapshot"})

    assert ticker.max_gap < _MAX_TICK_GAP
    assert len(call_threads) == 1
    assert call_threads[0] != loop_thread
    assert result.error is None
    assert result.output["snapshot_text"] == "page-snapshot"
    assert result.output["tool"] == "browser_snapshot"


async def test_execute_bridge_error_semantics_preserved(monkeypatch: Any) -> None:
    """下放不改变失败语义：Bridge 异常仍归为 BRIDGE_CALL_FAILED 显式失败。"""
    tool_mod = _load_tool()

    class _BrokenBridge:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def call(self, _tool_name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
            raise tool_mod.BridgeClientError("bridge down")

    monkeypatch.setattr(tool_mod, "BridgeClient", _BrokenBridge)

    result = await tool_mod.BrowserTool({}).execute({"_tool_name": "browser_snapshot"})

    assert result.error_code == "BRIDGE_CALL_FAILED"
    assert "bridge down" in (result.error or "")


# ============================================================
# 主锁 2：直播换票端点（P2-10，15s 上限）
# ============================================================


async def test_live_ticket_keeps_event_loop_ticking(monkeypatch: Any) -> None:
    """http_handle 换票 → BridgeClient.live_ticket（假身阻塞 0.3s）期间心跳持续
    打点，换票载荷照常透传（available 语义不变）。"""
    server = _load_server()
    loop_thread = threading.get_ident()
    call_threads: list[int] = []

    class _SlowBridge:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def live_ticket(self) -> dict[str, Any]:
            call_threads.append(threading.get_ident())
            time.sleep(_STUB_DELAY)
            return {"ok": True, "body": {"url": "ws://127.0.0.1:9999/live", "ticket": "t-1"}}

    monkeypatch.setattr(server, "BridgeClient", _SlowBridge)

    async with _Ticker() as ticker:
        out = await server.http_handle(path=server._LIVE_ROUTE, method="GET")

    assert ticker.max_gap < _MAX_TICK_GAP
    assert len(call_threads) == 1
    assert call_threads[0] != loop_thread
    assert out["success"] is True
    assert out["data"]["status"] == 200
