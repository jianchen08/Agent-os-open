# @feature: FP-T07 llm api | @ci: python-coverage
"""worker 事件循环生命周期契约补测（adapter.py `_direct_call_with_slot`）。

契约：``_direct_call_with_slot`` 为每次调用创建的独立 worker 事件循环，在
调用结束时必须已显式关闭（``_shutdown_worker_loop``：残留任务清理 →
shutdown_asyncgens → close）。loop 的生命周期 = 流消费生命周期——
``_worker_main`` 返回即消费结束；不关闭则 loop 自持的 self-pipe socketpair
及其上的 transport/client 被 litellm 全局缓存钉住常驻，表现为每次流式调用
泄漏一个事件循环（Windows 同 PID 自连端口对逐调用累积）。

三条区分度路径（正常流式 / 非流式 / worker 异常）都断言同一不变量：
「调用窗口内创建的所有事件循环，线程退出后均 is_closed()」——性质断言，
不钉数量。litellm.acompletion 为外部依赖，全桩；事件循环创建经
``asyncio.new_event_loop`` 记录器捕获。
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
_s = str(_PLUGIN_DIR)
while _s in sys.path:
    sys.path.remove(_s)
sys.path.insert(0, _s)

import adapter as adapter_mod  # noqa: E402  平铺 import，与生产代码一致
import key_pool as _kp_mod  # noqa: E402
import litellm as _litellm_mod  # noqa: E402
import router_factory as _rf_mod  # noqa: E402

_WORKER_THREAD_PREFIX = "litellm-acompletion-"


class _FakeSlot:
    """KeySlot 桩：仅暴露直连所需凭证字段（不驱动限流状态机）。"""

    def __init__(self, key_id: str, api_key: str, api_base: str = "") -> None:
        self.key_id = key_id
        self.api_key = api_key
        self.api_base = api_base


@pytest.fixture(autouse=True)
def _pin_flat_modules() -> Any:
    """重绑平铺裸名槽位到本文件实例（同 test_adapter_gaps.py 惯例）。"""
    saved = {name: sys.modules.get(name) for name in ("key_pool", "router_factory")}
    sys.modules["key_pool"] = _kp_mod
    sys.modules["router_factory"] = _rf_mod
    try:
        yield
    finally:
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod


@pytest.fixture
def fake_route_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    """router_factory 查表桩：model_id → provider/前缀/model_name 三函数。"""
    monkeypatch.setattr(_rf_mod, "get_provider_for_model", lambda _model_id: "apigo")
    monkeypatch.setattr(_rf_mod, "get_litellm_prefix", lambda _provider: "openai")
    monkeypatch.setattr(_rf_mod, "get_model_name_for_id", lambda _model_id: "MiniMax-M3")


@pytest.fixture
def created_loops(monkeypatch: pytest.MonkeyPatch) -> list[asyncio.AbstractEventLoop]:
    """记录调用窗口内创建的所有事件循环（性质断言的数据源）。"""
    created: list[asyncio.AbstractEventLoop] = []
    real_new = asyncio.new_event_loop

    def _recording_new() -> asyncio.AbstractEventLoop:
        loop = real_new()
        created.append(loop)
        return loop

    monkeypatch.setattr(asyncio, "new_event_loop", _recording_new)
    return created


def _worker_threads() -> set[threading.Thread]:
    """当前存活的 worker 线程集合（按线程名前缀识别）。"""
    return {t for t in threading.enumerate() if t.name.startswith(_WORKER_THREAD_PREFIX)}


def _wait_threads_exit(before: set[threading.Thread], timeout: float = 5.0) -> bool:
    """等待「本次调用新起」的 worker 线程全部退出（loop 收尾在退出前完成）。

    以调用前快照做差分：套件里更早的测试可能留下同名驻留线程，不可计入。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not (_worker_threads() - before):
            return True
        time.sleep(0.02)
    return False


def _assert_all_loops_closed(created: list[asyncio.AbstractEventLoop]) -> None:
    assert created, "调用窗口内必须创建了 worker 事件循环（否则测试没打到目标路径）"
    not_closed = [loop for loop in created if not loop.is_closed()]
    assert not not_closed, f"{len(not_closed)}/{len(created)} 个 worker loop 未关闭（泄漏）"


class _FakeStream:
    """桩流对象：__aiter__ 可迭代两个 chunk 后自然结束，aclose 可观测。"""

    def __init__(self) -> None:
        self._items: list[dict[str, Any]] = [{"seq": 0}, {"seq": 1}]
        self.aclose_calls = 0

    def __aiter__(self) -> _FakeStream:
        return self

    async def __anext__(self) -> dict[str, Any]:
        if self._items:
            return self._items.pop(0)
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.aclose_calls += 1


async def test_stream_call_closes_worker_loop(
    fake_route_tables: None,
    created_loops: list[asyncio.AbstractEventLoop],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """流式路径：bridge 消费到流末尾后，worker loop 必须 closed。

    附带断言桥接契约本身：消费方收到全部 chunk（队列桥不丢数据），
    流在 worker loop 内被 aclose。
    """
    stream = _FakeStream()

    async def _fake_acompletion(**kwargs: Any) -> Any:
        return stream

    monkeypatch.setattr(_litellm_mod, "acompletion", _fake_acompletion)
    adapter = adapter_mod.KeyPoolAdapter(router=None)
    threads_before = _worker_threads()

    bridge = await adapter._direct_call_with_slot(
        slot=_FakeSlot("k", "sk-x"),
        model="minimax-m3.1",
        messages=[],
        first_chunk_timeout=5,
    )
    assert isinstance(bridge, adapter_mod._ThreadedStreamBridge)

    received = [chunk async for chunk in bridge]
    assert [c["seq"] for c in received] == [0, 1]
    await bridge.aclose()

    assert _wait_threads_exit(threads_before), "worker 线程未退出"
    assert stream.aclose_calls == 1, "流必须在 worker loop 内被 aclose"
    _assert_all_loops_closed(created_loops)


async def test_non_stream_call_closes_worker_loop(
    fake_route_tables: None,
    created_loops: list[asyncio.AbstractEventLoop],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非流式路径：结果直返（不走 bridge），worker loop 同样必须 closed。"""
    marker = {"kind": "non-stream-response"}

    async def _fake_acompletion(**kwargs: Any) -> Any:
        return marker

    monkeypatch.setattr(_litellm_mod, "acompletion", _fake_acompletion)
    adapter = adapter_mod.KeyPoolAdapter(router=None)
    threads_before = _worker_threads()

    result = await adapter._direct_call_with_slot(
        slot=_FakeSlot("k", "sk-y", api_base="https://slot.example.com"),
        model="minimax-m3.1",
        messages=[],
        first_chunk_timeout=5,
    )
    assert result is marker

    assert _wait_threads_exit(threads_before), "worker 线程未退出"
    _assert_all_loops_closed(created_loops)


async def test_worker_exception_closes_worker_loop(
    fake_route_tables: None,
    created_loops: list[asyncio.AbstractEventLoop],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """异常路径：原始异常照常透传，worker loop 在收尾中关闭（finally 保证）。"""

    class _UpstreamGatewayError(Exception):
        pass

    async def _boom(**kwargs: Any) -> Any:
        raise _UpstreamGatewayError("gateway 503")

    monkeypatch.setattr(_litellm_mod, "acompletion", _boom)
    adapter = adapter_mod.KeyPoolAdapter(router=None)
    threads_before = _worker_threads()

    with pytest.raises(_UpstreamGatewayError, match="gateway 503"):
        await adapter._direct_call_with_slot(
            slot=_FakeSlot("k", "sk-z"),
            model="minimax-m3.1",
            messages=[],
            first_chunk_timeout=5,
        )

    assert _wait_threads_exit(threads_before), "worker 线程未退出"
    _assert_all_loops_closed(created_loops)


def test_shutdown_worker_loop_swallows_drain_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """收尾自身失败（残留任务清理抛错）不外抛，loop 仍被关闭。

    选择性桩：仅当 all_tasks 收到本测试的目标 loop 时抛错，其余调用原样
    委派——全局替换 asyncio.wait 会毒化 pytest-asyncio 自身的 loop 收尾。
    """
    loop = asyncio.new_event_loop()
    real_all_tasks = asyncio.all_tasks

    def _selective_all_tasks(target: Any = None) -> Any:
        if target is loop:
            raise RuntimeError("drain exploded")
        return real_all_tasks(target)

    monkeypatch.setattr(asyncio, "all_tasks", _selective_all_tasks)
    try:
        adapter_mod._shutdown_worker_loop(loop)  # 不应抛
        assert loop.is_closed()
    finally:
        if not loop.is_closed():
            loop.close()


def test_shutdown_worker_loop_idempotent_on_closed_loop() -> None:
    """对已关闭的 loop 二次收尾：守卫跳过 close，不抛（幂等）。"""
    loop = asyncio.new_event_loop()
    loop.close()
    adapter_mod._shutdown_worker_loop(loop)
    assert loop.is_closed()
