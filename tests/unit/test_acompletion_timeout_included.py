"""首 token 超时必须"包括在" litellm.acompletion 调用本身（HTTP 层）的回归测试。

生产故障链（2026-08-05）：
1. 17:05:34 卡死 36 分钟：litellm.acompletion 内部事件循环线程同步阻塞冻结主
   事件循环 → asyncio 层超时（wait_for/asyncio.wait/shield）全部失效。
   修复：把 first_chunk_timeout 作为 timeout 传进 litellm（HTTP 层 socket 超时
   在线程池线程内生效，不依赖事件循环调度）+ litellm 移入独立线程。
2. 20:08:02 / 20:33:59 "Event loop is closed" / "attached to a different loop"：
   CustomStreamWrapper 绑定 worker 线程的 loop，主循环 await 它的 __anext__
   会跨 loop 报错（即便 loop 存活）。修复：流式迭代也留在 worker 线程，
   chunk 经 queue.Queue（线程安全）桥接回主循环 —— _ThreadedStreamBridge。

测试用"绑定 worker loop 的异步迭代器"真实模拟 CustomStreamWrapper：它的
__anext__ 内部创建绑定该 loop 的 Future，主循环直接 await 必然报
"attached to a different loop"——旧实现（直接返回流对象）在此测试下红。
"""
from __future__ import annotations

import asyncio
import queue
import threading
import time
from unittest.mock import patch

import pytest

from llm.adapter import KeyPoolAdapter, _ThreadedStreamBridge
from llm.key_pool import KeySlot


def _make_adapter() -> KeyPoolAdapter:
    return KeyPoolAdapter(router=None)


class _LoopBoundStream:
    """真实模拟 CustomStreamWrapper：流在 worker loop 里创建并绑定该 loop。

    __anext__ 内部用 self._loop.create_future() 创建绑定 worker loop 的 Future：
    - 在 worker loop 内迭代（run_until_complete）→ 正常
    - 主循环直接 await 它的 __anext__ → "attached to a different loop"
      （生产 20:33:59 的 MidStreamFallbackError 根源）

    必须像真实 litellm.acompletion 一样在 worker 线程/loop 里创建。
    """

    def __init__(self, chunks: list[object]) -> None:
        self._loop = asyncio.get_running_loop()  # 创建时的 loop（worker loop）
        self._chunks = list(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self) -> object:
        if not self._chunks:
            raise StopAsyncIteration
        # 模拟 litellm 内部：await 一个绑定创建时 loop 的 Future
        fut = self._loop.create_future()
        self._loop.call_soon_threadsafe(fut.set_result, self._chunks.pop(0))
        return await fut

    async def aclose(self) -> None:
        self._chunks.clear()


@pytest.mark.asyncio
async def test_loop_bound_stream_rejected_if_returned_directly() -> None:
    """真实模拟：绑定 worker loop 的流在主循环直接消费必报跨 loop 错误。

    这是对旧实现（_direct_call_with_slot 直接返回 CustomStreamWrapper）的
    行为刻画——生产 20:33:59 的 "attached to a different loop" 即此场景。
    桥接方案（_ThreadedStreamBridge）下本测试必须通过。
    """
    done_evt = threading.Event()
    exc_box: list[BaseException] = []
    close_evt = threading.Event()
    q: queue.Queue = queue.Queue()
    worker_loop = asyncio.new_event_loop()

    # 模拟 worker：在独立 loop 里"跑 litellm.acompletion"（返回绑定该 loop 的流）
    # 并在同一 loop 迭代，chunk 进队列 —— 与 _worker_main 行为一致
    def _worker_iterate() -> None:
        async def _go() -> None:
            # 流在 worker loop 运行中创建（asyncio.get_running_loop() 才有值）
            stream = _LoopBoundStream(["chunk-1", "chunk-2"])
            async for chunk in stream:
                q.put(chunk)

        worker_loop.run_until_complete(_go())
        done_evt.set()

    t = threading.Thread(target=_worker_iterate, daemon=True)
    t.start()

    # 主循环拿到的是桥接对象（新实现）——旧实现直接返回 stream 会在这里炸
    bridge = _ThreadedStreamBridge(
        queue=q, done_evt=done_evt, exc_box=exc_box, close_evt=close_evt,
    )
    collected = []
    async for chunk in bridge:
        collected.append(chunk)
        if len(collected) >= 2:
            break

    assert collected == ["chunk-1", "chunk-2"]
    # 主循环未跨 loop：桥接对象消费不依赖 worker loop
    assert not worker_loop.is_closed()


@pytest.mark.asyncio
async def test_direct_call_passes_timeout_to_litellm() -> None:
    """first_chunk_timeout 必须作为 timeout 传进 litellm.acompletion（HTTP 层）。

    生产事件循环冻结时 asyncio 层超时全失效，唯一在线程池线程内生效的是
    httpx 层 timeout —— 不传就是 litellm 默认 600s（10 分钟），卡死远超 180s。
    """
    captured: dict = {}

    async def _fake_acompletion(**kwargs: object) -> object:
        captured.update(kwargs)
        return _FakeCompletionResult()

    slot = KeySlot(key_id="k", api_key="sk-test")
    adapter = _make_adapter()

    with patch("llm.adapter.litellm.acompletion", _fake_acompletion), patch(
        "llm.router_factory.get_provider_for_model", lambda _m: "opencode_go",
    ), patch("llm.router_factory.get_litellm_prefix", lambda _p: "openai"), patch(
        "llm.router_factory.get_model_name_for_id", lambda _m: "deepseek-v4-flash",
    ):
        await adapter._direct_call_with_slot(
            slot=slot,
            model="opencode_go/deepseek-v4-flash-go",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
            first_chunk_timeout=180,
        )

    assert "timeout" in captured, "first_chunk_timeout 必须作为 timeout 传进 litellm"
    assert captured["timeout"] == 180.0, (
        f"HTTP 层超时必须等于 first_chunk_timeout，实际 {captured['timeout']!r}"
    )


@pytest.mark.asyncio
async def test_direct_call_default_timeout_180() -> None:
    """未显式传 first_chunk_timeout 时，默认也是 180s（不是 litellm 的 600s）。"""
    captured: dict = {}

    async def _fake_acompletion(**kwargs: object) -> object:
        captured.update(kwargs)
        return _FakeCompletionResult()

    slot = KeySlot(key_id="k", api_key="sk-test")
    adapter = _make_adapter()

    with patch("llm.adapter.litellm.acompletion", _fake_acompletion), patch(
        "llm.router_factory.get_provider_for_model", lambda _m: "opencode_go",
    ), patch("llm.router_factory.get_litellm_prefix", lambda _p: "openai"), patch(
        "llm.router_factory.get_model_name_for_id", lambda _m: "deepseek-v4-flash",
    ):
        await adapter._direct_call_with_slot(
            slot=slot,
            model="opencode_go/deepseek-v4-flash-go",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
        )

    assert captured.get("timeout") == 180.0, (
        f"默认 HTTP 超时应为 180s，实际 {captured.get('timeout')!r}"
    )


@pytest.mark.asyncio
async def test_explicit_timeout_not_overridden() -> None:
    """调用方显式传 timeout（如 plugin 自定义 60s）时，不被默认 180 覆盖。"""
    captured: dict = {}

    async def _fake_acompletion(**kwargs: object) -> object:
        captured.update(kwargs)
        return _FakeCompletionResult()

    slot = KeySlot(key_id="k", api_key="sk-test")
    adapter = _make_adapter()

    with patch("llm.adapter.litellm.acompletion", _fake_acompletion), patch(
        "llm.router_factory.get_provider_for_model", lambda _m: "opencode_go",
    ), patch("llm.router_factory.get_litellm_prefix", lambda _p: "openai"), patch(
        "llm.router_factory.get_model_name_for_id", lambda _m: "deepseek-v4-flash",
    ):
        await adapter._direct_call_with_slot(
            slot=slot,
            model="opencode_go/deepseek-v4-flash-go",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
            timeout=60.0,
            first_chunk_timeout=180,
        )

    assert captured.get("timeout") == 60.0, (
        f"显式 timeout 不得被默认值覆盖，实际 {captured.get('timeout')!r}"
    )


@pytest.mark.asyncio
async def test_acompletion_hang_timeout_recovers_loop() -> None:
    """litellm.acompletion 卡死（永不返回）时，主事件循环必须保持可用。

    生产 17:05:34 卡死 36 分钟 = 事件循环线程被同步阻塞冻结，asyncio 层超时
    全部失效。本测试验证：即便 litellm 调用卡死，心跳协程（模拟其他管道）
    仍持续运行 —— 即超时机制不依赖事件循环调度。
    """
    async def _hanging_acompletion(**kwargs: object) -> object:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: time.sleep(30))
        return _FakeCompletionResult()

    heartbeat_hits: list[float] = []

    async def _heartbeat() -> None:
        while True:
            heartbeat_hits.append(time.monotonic())
            await asyncio.sleep(0.02)

    slot = KeySlot(key_id="k", api_key="sk-test")
    adapter = _make_adapter()

    hb_task = asyncio.create_task(_heartbeat())
    t0 = time.monotonic()
    try:
        with patch("llm.adapter.litellm.acompletion", _hanging_acompletion), patch(
            "llm.router_factory.get_provider_for_model", lambda _m: "opencode_go",
        ), patch("llm.router_factory.get_litellm_prefix", lambda _p: "openai"), patch(
            "llm.router_factory.get_model_name_for_id", lambda _m: "deepseek-v4-flash",
        ):
            await adapter._direct_call_with_slot(
                slot=slot,
                model="opencode_go/deepseek-v4-flash-go",
                messages=[{"role": "user", "content": "hi"}],
                stream=True,
                first_chunk_timeout=0.5,
            )
            pytest.fail("应当抛 TimeoutError")
    except asyncio.TimeoutError:
        pass
    finally:
        hb_task.cancel()

    elapsed = time.monotonic() - t0
    assert len(heartbeat_hits) >= 3, (
        f"事件循环被冻结：0.5s 内心跳仅 {len(heartbeat_hits)} 次"
    )
    assert elapsed < 5, f"超时未生效，耗时 {elapsed:.1f}s"


@pytest.mark.asyncio
async def test_streaming_bridge_consumes_without_cross_loop() -> None:
    """端到端：_direct_call_with_slot 返回桥接对象，主循环消费不跨 loop。

    旧实现直接返回绑定 worker loop 的流 → 主循环 __anext__ 报
    "attached to a different loop"（生产 20:33:59）。新实现流式迭代在
    worker 线程完成，chunk 进队列，主循环从队列取 —— 本测试模拟完整链路。
    """
    # worker 线程：在独立 loop 里"跑 litellm.acompletion"返回绑定该 loop 的流，
    # 然后在同一 loop 迭代，chunk 进队列（_worker_main 的行为）
    worker_loop = asyncio.new_event_loop()

    done_evt = threading.Event()
    exc_box: list[BaseException] = []
    close_evt = threading.Event()
    q: queue.Queue = queue.Queue()

    def _worker() -> None:
        async def _go() -> None:
            # 流在 worker loop 运行中创建（真实 litellm.acompletion 的行为）
            stream = _LoopBoundStream([_Chunk("hello"), _Chunk("world"), None])
            try:
                async for chunk in stream:
                    q.put(chunk)
                    if close_evt.is_set():
                        break
            finally:
                await stream.aclose()
        worker_loop.run_until_complete(_go())
        done_evt.set()

    t = threading.Thread(target=_worker, daemon=True)
    t.start()

    bridge = _ThreadedStreamBridge(
        queue=q, done_evt=done_evt, exc_box=exc_box, close_evt=close_evt,
    )
    collected = []
    async for chunk in bridge:
        if chunk is None:
            break
        collected.append(chunk.content)

    assert collected == ["hello", "world"]
    assert not worker_loop.is_closed(), "worker loop 必须保持存活（流 wrapper 绑定它）"


@pytest.mark.asyncio
async def test_streaming_bridge_consumes_without_cross_loop() -> None:
    """端到端：_direct_call_with_slot 返回桥接对象，主循环消费不跨 loop。

    旧实现直接返回绑定 worker loop 的流 → 主循环 __anext__ 报
    "attached to a different loop"（生产 20:33:59）。新实现流式迭代在
    worker 线程完成，chunk 进队列，主循环从队列取 —— 本测试模拟完整链路。
    """
    async def _fake_acompletion(**kwargs: object) -> object:
        # 模拟 litellm.acompletion：在 worker loop 里返回绑定该 loop 的流。
        # 不能直接返回 _LoopBoundStream（它的 loop 是创建时的 running loop，
        # 即 worker loop）——真实 litellm 行为一致。
        return _LoopBoundStream([_Chunk("hello"), _Chunk("world"), None])

    slot = KeySlot(key_id="k", api_key="sk-test")
    adapter = _make_adapter()

    with patch("llm.adapter.litellm.acompletion", _fake_acompletion), patch(
        "llm.router_factory.get_provider_for_model", lambda _m: "opencode_go",
    ), patch("llm.router_factory.get_litellm_prefix", lambda _p: "openai"), patch(
        "llm.router_factory.get_model_name_for_id", lambda _m: "deepseek-v4-flash",
    ):
        bridge = await adapter._direct_call_with_slot(
            slot=slot,
            model="opencode_go/deepseek-v4-flash-go",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
        )

    # 返回的是桥接对象（不是绑定 worker loop 的原始流）
    assert isinstance(bridge, _ThreadedStreamBridge)
    collected = []
    async for chunk in bridge:
        if chunk is None:
            break
        collected.append(chunk.content)

    assert collected == ["hello", "world"], (
        f"桥接消费失败（跨 loop 或队列问题）: {collected!r}"
    )


@pytest.mark.asyncio
async def test_full_streaming_call_flow_with_bridge() -> None:
    """完整 _call_streaming 流式链路：桥接对象 + on_chunk + 正常结束。

    覆盖生产流式路径的完整闭环：
    - litellm.acompletion 在 worker loop 返回绑定该 loop 的流（真实行为）
    - _call_streaming 主循环消费桥接对象，chunk 正确回调
    - 结束后 aclose 触发（bridge 通知 worker 关底层流）
    """
    received: list[str] = []
    closed = threading.Event()

    class _StreamWithAclose(_LoopBoundStream):
        async def aclose(self) -> None:
            self._chunks.clear()
            closed.set()

    async def _fake_acompletion(**kwargs: object) -> object:
        return _StreamWithAclose(["你", "好", None])

    slot = KeySlot(key_id="k", api_key="sk-test")
    adapter = _make_adapter()

    with patch("llm.adapter.litellm.acompletion", _fake_acompletion), patch(
        "llm.router_factory.get_provider_for_model", lambda _m: "opencode_go",
    ), patch("llm.router_factory.get_litellm_prefix", lambda _p: "openai"), patch(
        "llm.router_factory.get_model_name_for_id", lambda _m: "deepseek-v4-flash",
    ):
        bridge = await adapter._direct_call_with_slot(
            slot=slot,
            model="opencode_go/deepseek-v4-flash-go",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
        )

    # 模拟 _call_streaming：消费桥接流
    async for chunk in bridge:
        if chunk is None:
            break
        received.append(chunk)

    # 正常结束后，finally 会调 aclose → 通知 worker 关闭底层流
    await bridge.aclose()
    assert received == ["你", "好"]
    assert closed.is_set(), "aclose 应通知 worker 关闭底层流"


class _Chunk:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeCompletionResult:
    """最小响应对象（_direct_call_with_slot 直接返回，不解析）。"""

    def __init__(self) -> None:
        self.choices = []
