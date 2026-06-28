"""KeyPool 信号量在流式调用期间的生命周期回归测试。

回归契约（BUG-FIX-fix_20260628_release_before_stream_consumed）：
流式路径下 _direct_call_with_slot 返回惰性 stream wrapper，真正的流式传输
发生在调用方消费该对象期间。slot.release() 必须推迟到 stream.aclose()，
而非 _do_completion 返回前——否则 max_concurrent 信号量形同虚设
（只计量"拿到 stream 对象"的毫秒级瞬间，未覆盖秒~分钟级的流式传输）。

本测试用真实 KeyPool + KeySlot + PrioritySemaphore，mock _direct_call_with_slot
返回可控的 _FakeStream，精确控制流的消费时机，断言信号量生命周期正确。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from llm.adapter import KeyPoolAdapter
from llm.key_pool import KeyPool, KeySlot


def _make_delta(*, content: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(content=content, reasoning_content=None, tool_calls=None)


def _make_chunk(*, content: str | None = None, finish: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=_make_delta(content=content), finish_reason=finish)],
        usage=None,
    )


class _FakeStream:
    """可控的异步流：按既定序列产出 chunk。

    record_consumption 记录是否被 aclose（用于断言信号量释放时机）。
    """

    def __init__(self, seq: list[tuple[float, Any]]) -> None:
        self._seq = seq
        self._idx = 0
        self.is_closed = False

    def __aiter__(self) -> _FakeStream:
        return self

    async def __anext__(self) -> Any:
        if self._idx >= len(self._seq):
            raise StopAsyncIteration
        delay, item = self._seq[self._idx]
        self._idx += 1
        if item is None:
            raise StopAsyncIteration
        if delay > 0:
            await asyncio.sleep(delay)
        return item

    async def aclose(self) -> None:
        self.is_closed = True


def _build_adapter_with_pool(stream: _FakeStream, max_concurrent: int = 1) -> tuple[KeyPoolAdapter, KeySlot]:
    """构造 KeyPoolAdapter：单 key 池，_direct_call_with_slot 固定返回 stream。

    KeySlot 是真实对象（带真实 PrioritySemaphore），信号量生命周期可被观测。
    """
    slot = KeySlot(key_id="test_key", api_key="sk-test", max_concurrent=max_concurrent)
    pool = KeyPool(slots=[slot], pool_id="test_provider")

    adapter = KeyPoolAdapter(router=None)

    # _resolve_provider 返回非空，触发 KeyPool 路径
    adapter._resolve_provider = lambda _model: "test_provider"  # type: ignore[assignment]
    # _direct_call_with_slot 返回可控流（不经真实网络）
    async def _fake_direct(slot: Any, **_kw: Any) -> Any:
        return stream
    adapter._direct_call_with_slot = _fake_direct  # type: ignore[assignment]

    # get_key_pool 返回测试池
    def _fake_get_key_pool(provider: str) -> KeyPool:
        return pool
    patcher = patch("llm.router_factory.get_key_pool", _fake_get_key_pool)
    patcher.start()

    return adapter, slot


# ---------------------------------------------------------------------------
# 1. 流消费期间信号量被占用，aclose 后才释放
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_semaphore_held_during_stream_consumption() -> None:
    """流式调用期间信号量满（max_concurrent=1），第二个并发请求必须阻塞等待。"""
    # 流产出 1 个 chunk 后有 2s 间隙（消费期间信号量应被占用）
    stream = _FakeStream([
        (0.0, _make_chunk(content="hello")),
        (0.5, _make_chunk(content="world", finish="stop")),
        (0.0, None),
    ])
    adapter, slot = _build_adapter_with_pool(stream, max_concurrent=1)
    sem = slot._get_semaphore()

    # 槽位初始全空闲
    assert sem.capacity == 1

    async def _consume() -> Any:
        return await adapter.completion(
            model="zai/glm-5.2",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
            inter_chunk_timeout=5.0,
            first_chunk_timeout=5.0,
        )

    # 启动流式调用，不等它完成
    task = asyncio.create_task(_consume())
    # 让出控制权，让 adapter acquire slot 并开始消费流
    await asyncio.sleep(0.1)

    # ★ 流消费期间：信号量应被占用（第二个 acquire 必须阻塞）
    second_acquired = asyncio.Event()

    async def _try_second_acquire() -> None:
        await slot.acquire()
        second_acquired.set()
        slot.release()

    second_task = asyncio.create_task(_try_second_acquire())
    # 给一点时间，确认第二个 acquire 确实被阻塞（事件未设置）
    await asyncio.sleep(0.1)
    assert not second_acquired.is_set(), (
        "流消费期间信号量必须被占用——第二个 acquire 不应成功（释放过早的 bug 回归）"
    )

    # 等第一个流消费完毕（自然结束 → _call_streaming finally aclose → 触发 release）
    resp = await task
    assert resp.text == "helloworld"

    # 流结束后，第二个 acquire 应能成功（信号量已释放）
    await asyncio.wait_for(second_task, timeout=2.0)
    assert second_acquired.is_set(), "流结束后信号量应释放，第二个 acquire 应成功"
    assert stream.is_closed, "流应在消费完毕后被 aclose"


# ---------------------------------------------------------------------------
# 2. 异常路径（换 key 重试）立即释放，不绑定到 aclose
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_release_on_error_not_deferred() -> None:
    """_direct_call_with_slot 抛异常时立即 release（异常路径不 defer 到 aclose）。

    直接调用 _do_completion 隔离 fallback 干扰：所有 key 失败后 _do_completion
    会抛 KeyPoolExhaustedError/最后异常，但在此之前每个失败 attempt 的 finally
    已立即释放信号量。断言：失败后信号量回到满容量（未被异常路径占用）。
    """
    import litellm

    slot = KeySlot(key_id="err_key", api_key="sk-test", max_concurrent=1)
    pool = KeyPool(slots=[slot], pool_id="test_provider")
    adapter = KeyPoolAdapter(router=None)
    adapter._resolve_provider = lambda _model: "test_provider"  # type: ignore[assignment]

    async def _failing_direct(slot: Any, **_kw: Any) -> Any:
        # 抛可恢复错误，触发换 key 重试（单 key → 耗尽 → 抛异常）
        raise litellm.APIConnectionError(message="boom", model="zai/glm-5.2", llm_provider="zai")

    adapter._direct_call_with_slot = _failing_direct  # type: ignore[assignment]
    # 让 fallback 也失败，确保 _do_completion 最终抛异常而非走真实 router
    async def _failing_route(**_kw: Any) -> Any:
        raise RuntimeError("fallback disabled in test")
    adapter._route_call = _failing_route  # type: ignore[assignment]

    def _fake_get_key_pool(provider: str) -> KeyPool:
        return pool
    with patch("llm.router_factory.get_key_pool", _fake_get_key_pool):
        with pytest.raises(Exception):
            # stream=True 但 _direct_call_with_slot 永远抛错，走异常路径
            await adapter._do_completion(
                model="zai/glm-5.2",
                messages=[{"role": "user", "content": "hi"}],
                stream=True,
            )

    sem = slot._get_semaphore()
    # 异常路径应立即释放——信号量回到满容量（未被占用/泄漏）
    assert sem.capacity == 1, "异常路径必须立即释放信号量许可"


# ---------------------------------------------------------------------------
# 3. 建连超时路径不泄漏信号量（首 chunk 超时 → aclose → release）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_leak_on_connect_timeout() -> None:
    """首 chunk 永不到达（建连超时）→ 超时后 stream 被 aclose → 信号量释放，无泄漏。"""
    import litellm

    # 流的首 chunk 永远延迟（模拟上游半死连接）
    stream = _FakeStream([(100.0, _make_chunk(content="late"))])
    adapter, slot = _build_adapter_with_pool(stream, max_concurrent=1)

    with pytest.raises(litellm.Timeout):
        await adapter.completion(
            model="zai/glm-5.2",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
            inter_chunk_timeout=100.0,
            first_chunk_timeout=0.3,   # 0.3s 超时
        )

    # 建连超时后，信号量必须已释放（通过 _open_and_first_chunk 的 aclose 触发）
    sem = slot._get_semaphore()
    assert sem.capacity == 1, (
        "建连超时必须释放信号量许可——否则高频超时会耗尽信号量（泄漏回归）"
    )
    assert stream.is_closed, "建连超时后流应被 aclose（触发绑定的 release）"
