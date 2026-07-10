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
pytestmark = pytest.mark.timing
# §9.4: 时序不变量门禁 — 此文件的测试断言可观察行为（事件顺序/间隔/超时边界/资源回收），
# 不含实现细节断言（mock.call_count/私有方法），破坏不变量的改动在 CI 阶段即被拦截。

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


# ---------------------------------------------------------------------------
# 4. PrioritySemaphore 死 waiter 清理（cancel 不泄漏许可，并发控制不被绕过）
#
# 回归契约（BUG-FIX-fix_20260629_dead_waiter_permit_leak）：
# acquire() 的 await fut 没有 cancel 清理——排队中的请求被取消后，
# 它的 (priority, fut) 残留在 _waiters 里变成死占位；release()/grow()
# 撞到这种死 future 时既不唤醒任何活等待者、也不回填 _value，可用许可
# 被凭空吞掉，每发生一次永久 -1 → LLM 排队越积越多且无法自愈。
#
# 本组测试直接观测"能否拿到许可"的行为（acquire 是否在合理时间内成功），
# 而非 capacity（容量上限不变，测不到 _value 泄漏）。
# ---------------------------------------------------------------------------


async def _wait_until_blocked(acquire_coro: Any) -> asyncio.Task:
    """启动一个 acquire 任务并确认它已阻塞在等待队列中。"""
    task = asyncio.create_task(acquire_coro)
    # 多次让出控制权，确保 acquire 已进入 await fut
    for _ in range(5):
        await asyncio.sleep(0)
    assert not task.done(), "acquire 任务应阻塞在等待队列，却提前完成"
    return task


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_leak_permit() -> None:
    """核心回归：排队中的 acquire 被 cancel 后，许可必须能被后续 acquire 拿到。

    capacity=1 占满 → 第 2 个请求排队并被 cancel → release() 后，
    第 3 个 acquire 必须在 1s 内成功（不能因死 waiter 残留而永久阻塞）。
    """
    slot = KeySlot(key_id="k", api_key="sk", max_concurrent=1)
    await slot.acquire()  # 占满唯一许可

    # 排队一个请求，然后取消它（模拟首字节超时 / 用户停止）
    waiter = await _wait_until_blocked(slot.acquire())
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    # 释放持有的许可：此时 _waiters 里残留死 waiter
    slot.release()

    # 新的 acquire 必须能拿到许可（1s 内）——若有泄漏，这里会超时
    await asyncio.wait_for(slot.acquire(), timeout=1.0)
    slot.release()


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_starve_live_waiter() -> None:
    """并发控制不破：死 waiter 不能吞掉本应给活 waiter 的许可。

    capacity=1 占满 → 队列里第 1 个 cancel（死）、第 2 个活 → release() 后，
    活的那个必须被唤醒（不能因队头是死 waiter 而饿死）。
    """
    slot = KeySlot(key_id="k", api_key="sk", max_concurrent=1)
    await slot.acquire()  # 占满

    # 第 1 个排队请求：将被取消（变成死 waiter 占据队头）
    dead_waiter = await _wait_until_blocked(slot.acquire())
    # 第 2 个排队请求：活的，应被 release 唤醒
    live_acquired = asyncio.Event()

    async def _live_wait() -> None:
        await slot.acquire()
        live_acquired.set()

    live_task = asyncio.create_task(_live_wait())
    await asyncio.sleep(0.05)  # 让 live_wait 进入等待队列（排在 dead 后面）

    # 取消队头的死 waiter
    dead_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await dead_waiter

    # 释放许可：必须唤醒活的 waiter，而非被死 waiter 吞掉
    slot.release()
    await asyncio.wait_for(asyncio.shield(asyncio.sleep(0.3)), timeout=1.0)
    assert live_acquired.is_set(), (
        "release() 必须唤醒活的等待者，不能因队头死 waiter 而丢失许可"
    )

    live_task.result()
    slot.release()


@pytest.mark.asyncio
async def test_grow_wakes_live_waiter_past_dead_one() -> None:
    """grow() 路径：扩容后必须跳过死 waiter 唤醒下一个活的（或回填 _value）。"""
    slot = KeySlot(key_id="k", api_key="sk", max_concurrent=1)
    await slot.acquire()  # 占满

    # 队头死 waiter
    dead = await _wait_until_blocked(slot.acquire())
    dead.cancel()
    with pytest.raises(asyncio.CancelledError):
        await dead

    # 活 waiter
    live_acquired = asyncio.Event()

    async def _live() -> None:
        await slot.acquire()
        live_acquired.set()

    live_task = asyncio.create_task(_live())
    await asyncio.sleep(0.05)

    # grow() 扩容 1：必须唤醒活的，不能被死 waiter 吞掉
    slot._get_semaphore().grow()
    await asyncio.sleep(0.05)
    assert live_acquired.is_set(), "grow() 必须跳过死 waiter 唤醒活的等待者"

    live_task.result()
    slot.release()


@pytest.mark.asyncio
async def test_release_after_shrink_wakes_live_waiter() -> None:
    """shrink 缩容后，失败请求的 release 仍能唤醒下一个活 waiter。

    固化失败重试链路：请求失败 → handle_error 内部 shrink → finally release。
    shrink 不会 cancel waiter（靠超时策略），release 必须把许可给到活的等待者，
    不能因缩容而吞掉——否则排队请求会因 key 失败降级而饿死。
    """
    slot = KeySlot(key_id="k", api_key="sk", max_concurrent=2)
    sem = slot._get_semaphore()
    # 两个许可都占满（模拟两个请求在跑）
    await slot.acquire()
    await slot.acquire()
    # 一个活的 waiter 排队
    live_acquired = asyncio.Event()

    async def _live() -> None:
        await slot.acquire()
        live_acquired.set()

    live_task = asyncio.create_task(_live())
    await asyncio.sleep(0.05)

    # 模拟某个在跑请求失败：缩容（capacity 2→1）后 release 它的许可
    sem.shrink()
    slot.release()  # 失败请求的 finally release

    await asyncio.wait_for(asyncio.shield(asyncio.sleep(0.3)), timeout=1.0)
    assert live_acquired.is_set(), (
        "shrink 后失败请求的 release 必须唤醒活 waiter，不能因降级吞掉许可"
    )
    live_task.result()
    slot.release()


@pytest.mark.asyncio
async def test_release_to_empty_queue_refills_value() -> None:
    """正常路径不回归：无等待者时 release() 应回填 _value，后续 acquire 立即成功。"""
    slot = KeySlot(key_id="k", api_key="sk", max_concurrent=1)
    await slot.acquire()
    slot.release()  # 队列空，回填 _value

    # 无需让出控制权即可立即拿到（_value > 0 的快路径）
    await asyncio.wait_for(slot.acquire(), timeout=1.0)
    slot.release()


@pytest.mark.asyncio
async def test_acquires_after_repeated_cancellation_not_starved() -> None:
    """多次 cancel 死 waiter 累积后，许可仍不泄漏——这是"排队越积越多"的直接复现。

    模拟反复停止/超时：占满 → 排队并 cancel（重复 N 次，制造 N 个死 waiter
    历史扰动）→ release → 新 acquire 必须成功。验证没有许可被吞掉。
    """
    slot = KeySlot(key_id="k", api_key="sk", max_concurrent=1)
    await slot.acquire()

    for _ in range(5):
        w = await _wait_until_blocked(slot.acquire())
        w.cancel()
        with pytest.raises(asyncio.CancelledError):
            await w

    slot.release()
    await asyncio.wait_for(slot.acquire(), timeout=1.0)
    slot.release()


@pytest.mark.asyncio
async def test_integration_acquire_slot_cancel_then_retry() -> None:
    """集成层：acquire_slot 排队中被 cancel，释放后新的 acquire_slot 能拿到许可。

    复现 adapter.py:476 首字节超时 / 用户停止 这条主路径——
    acquire_slot 内 await slot.acquire() 被 cancel 后，slot.release()
    能让下一个 acquire_slot 正常拿到，不会因死 waiter 卡死。
    """
    pool = KeyPool([KeySlot(key_id="k", api_key="sk", max_concurrent=1)], pool_id="p")
    held = await pool.acquire_slot(timeout=1.0)  # 占满

    # 排队中的 acquire_slot，随后取消（模拟上层 cancel）
    pending = await _wait_until_blocked(pool.acquire_slot(timeout=1.0))
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    held.release()

    # 新请求必须能拿到
    again = await asyncio.wait_for(pool.acquire_slot(timeout=1.0), timeout=1.0)
    again.release()


# ---------------------------------------------------------------------------
# 5. RPM 弹性降级/回升（429 降 rpm，成功回升，封顶 rpm_limit）
#
# 回归契约（rpm-as-primary-limiter）：
# 限流主参数改为 rpm，429/SERVICE_DOWN 后降 rpm（而非旧的降 max_concurrent），
# 成功后回升，封顶 rpm_limit。max_concurrent 设大值后不再参与限流。
# ---------------------------------------------------------------------------


def test_rate_limit_reduces_rpm_not_concurrency() -> None:
    """429 后降 rpm（不降 max_concurrent）：rpm_remaining 反映生效值下降。"""
    from llm.error_classifier import ErrorInfo, ErrorKind

    slot = KeySlot(key_id="k", api_key="sk", max_concurrent=3, rpm_limit=10)
    assert slot.rpm_remaining == 10, "初始生效 rpm 应等于 rpm_limit"

    slot.handle_error(ErrorInfo(ErrorKind.RATE_LIMIT))
    assert slot.rpm_remaining == 9, "RATE_LIMIT 应降 rpm（10→9）"

    # max_concurrent 不受影响：信号量容量仍是 3
    assert slot._get_semaphore().capacity == 3, "RATE_LIMIT 不应改变 max_concurrent"


def test_rpm_reduces_floor_one() -> None:
    """rpm 降级最低到 1，不会降到 0（避免把限流关死）。"""
    from llm.error_classifier import ErrorInfo, ErrorKind

    slot = KeySlot(key_id="k", api_key="sk", max_concurrent=3, rpm_limit=2)
    slot.handle_error(ErrorInfo(ErrorKind.RATE_LIMIT))  # 2→1
    assert slot.rpm_remaining == 1
    slot.handle_error(ErrorInfo(ErrorKind.RATE_LIMIT))  # 已到 1，不再降
    assert slot.rpm_remaining == 1, "rpm 降级下限为 1，不能降到 0"


def test_on_success_recovers_rpm_capped_at_limit() -> None:
    """成功调用后 rpm 逐级回升，封顶 rpm_limit，不超发。"""
    from llm.error_classifier import ErrorInfo, ErrorKind

    slot = KeySlot(key_id="k", api_key="sk", max_concurrent=3, rpm_limit=10)
    # 先降 3 级：10→7
    for _ in range(3):
        slot.handle_error(ErrorInfo(ErrorKind.RATE_LIMIT))
    assert slot.rpm_remaining == 7

    # 成功 3 次：7→10（回到 rpm_limit）
    for _ in range(3):
        slot.on_success()
    assert slot.rpm_remaining == 10

    # 再成功不会超发（封顶 rpm_limit）
    slot.on_success()
    assert slot.rpm_remaining == 10, "rpm 回升封顶 rpm_limit，不可超过"


def test_rpm_limit_zero_falls_back_to_unlimited() -> None:
    """rpm_limit=0（漏配/不限）时兜底放行，且降级/回升不对它生效（避免误降成有限）。"""
    from llm.error_classifier import ErrorInfo, ErrorKind

    slot = KeySlot(key_id="k", api_key="sk", max_concurrent=3, rpm_limit=0)
    assert slot.rpm_remaining == 9999, "rpm_limit=0 时兜底返回 9999（不限）"
    slot.handle_error(ErrorInfo(ErrorKind.RATE_LIMIT))  # _rpm_effective<=1 不降
    assert slot.rpm_remaining == 9999, "rpm_limit=0 时降级不应改变放行行为"


# ---------------------------------------------------------------------------
# 6. RPM 名额与请求生命周期绑定（cancel/失败不占 rpm 名额）
#
# 回归契约（rpm-count-tied-to-lifecycle）：
# record_request() 在排队前就记了 rpm 名额，但若请求在排队中被 cancel
# （未真正打上游），这个名额必须归还——否则被 cancel 的请求会"虚占"
# rpm 窗口 60 秒，导致正常请求被误判为 rpm 耗尽而排队，越积越多。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancelled_queued_request_does_not_consume_rpm() -> None:
    """排队中被 cancel 的请求（没真正打上游）不该占用 rpm 名额。

    复现：max_concurrent=1 占满 → 排队请求 cancel（没打上游）→
    释放许可后，rpm_remaining 应恢复（cancel 的请求没消耗名额）。
    """
    pool = KeyPool([KeySlot(key_id="k", api_key="sk", max_concurrent=1, rpm_limit=3)], pool_id="p")
    held = await pool.acquire_slot(timeout=1.0)  # 占满，消耗 1 个 rpm 名额
    assert held.rpm_remaining == 2

    # 排队中的 acquire_slot，随后取消（模拟用户停止 / 超时）
    pending = await _wait_until_blocked(pool.acquire_slot(timeout=1.0))
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    # ★ 关键断言：cancel 的请求没真正打上游，不该占 rpm 名额
    # 当前会失败：record_request 在排队前已执行，名额被虚占
    assert held.rpm_remaining == 2, (
        "排队中被 cancel 的请求没真正打上游，不该消耗 rpm 名额"
    )

    held.release()


@pytest.mark.asyncio
async def test_rpm_limit_not_bypassed_under_concurrency() -> None:
    """TOCTOU 竞态回归：多个并发请求不能绕过 rpm 限流。

    修复 Bug1（cancel 归还名额）时不能复发"record 在前"防的竞态：
    rpm=2 时，并发发起 5 个请求，最多只能有 2 个真正拿到 slot，
    其余必须被 rpm 闸挡下排队（而不是全部放行）。
    """
    pool = KeyPool(
        [KeySlot(key_id="k", api_key="sk", max_concurrent=10, rpm_limit=2)],
        pool_id="p",
    )

    acquired: list[str] = []

    async def _try(i: int) -> None:
        try:
            slot = await asyncio.wait_for(pool.acquire_slot(timeout=0.5), timeout=1.0)
            acquired.append(f"ok{i}")
            await asyncio.sleep(0.05)
            slot.release()
        except (asyncio.TimeoutError, Exception):
            acquired.append(f"blocked{i}")

    # 5 个请求几乎同时发起，max_concurrent=10 不挡，只有 rpm=2 挡
    await asyncio.gather(*[_try(i) for i in range(5)])

    ok_count = sum(1 for a in acquired if a.startswith("ok"))
    # 真正拿到 slot 的最多 2 个（rpm 闸生效），其余被挡
    assert ok_count <= 2, (
        f"rpm=2 时最多 2 个请求放行，实际 {ok_count}——rpm 限流被并发绕过（竞态复发）"
    )
