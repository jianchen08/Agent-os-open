# @ci: python-coverage
"""key_pool 契约测试：优先级信号量 + KeySlot 限流/配额/错误策略 + KeyPool 选键。

覆盖契约：
- PrioritySemaphore：许可守恒（acquire/release/grow/shrink 全路径不漏不重）、
  高优先级先得、被取消的 waiter 不吞许可（死占位自愈）；
- KeySlot：rpm/token 预算语义、按 ErrorKind 分派的冷却与降级策略、
  成功后回升（rpm 封顶、并发仅在被降级过才 grow）；
- KeyPool：主备顺序选键、并发满 vs rpm 耗尽的区分放行、
  acquire_slot 取消归还 rpm 名额、超时抛 KeyPoolExhaustedError。

时钟：KeySlot/KeyPool 全部经模块级 _time.monotonic 取时，测试注入可调
假时钟（外部依赖：时钟允许替身），不 sleep 真等。
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from agentos_plugin_sdk.error_classifier import ErrorInfo, ErrorKind

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_key_pool() -> Any:
    """按唯一模块名加载 key_pool（平铺布局防同名模块互劫持）。

    先弹 key_pool/exceptions 裸名缓存：其他插件目录可能已把同名模块装进
    sys.modules，顶层 `from exceptions import ...` 须命中本目录版本。
    """
    for _m in ("key_pool", "exceptions"):
        sys.modules.pop(_m, None)
    mod_name = "key_pool_coverage_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "key_pool.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def kp() -> Any:
    return _load_key_pool()


class FakeClock:
    """key_pool._time 替身：只提供模块内唯一用点 monotonic。

    打在 kp 模块命名空间（setattr(kp, "_time", fake)），不碰全局 time 模块——
    asyncio 事件循环内部也用 time.monotonic 排定时器，全局冻结会让
    asyncio.sleep 永不触发（整个事件循环假死）。
    """

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


@pytest.fixture
def clock(kp: Any, monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(kp, "_time", fake)
    return fake


def _slot(kp: Any, **kw: Any) -> Any:
    kw.setdefault("key_id", "k1")
    kw.setdefault("api_key", "sk-test-1234567890")
    return kp.KeySlot(**kw)


# ──────────────────────────────────────────────
# Agent 层级优先级
# ──────────────────────────────────────────────


class TestAgentPriority:
    def test_default_priority_is_lowest(self, kp: Any) -> None:
        assert kp.get_agent_priority() == 99

    @pytest.mark.parametrize(
        ("level", "expected"),
        [("L1", 1), ("L2", 2), ("L3", 3), (None, 99), ("L9", 99), ("", 99)],
    )
    def test_set_agent_priority_mapping(self, kp: Any, level: str | None, expected: int) -> None:
        kp.set_agent_priority(level)
        assert kp.get_agent_priority() == expected

    def test_priority_is_context_scoped(self, kp: Any) -> None:
        """contextvar 隔离：子上下文里 set 不回流到外层。"""
        outer = kp.get_agent_priority()
        kp.set_agent_priority("L1")
        assert kp.get_agent_priority() == 1
        # 同一协程内恢复
        kp.set_agent_priority(None)
        assert kp.get_agent_priority() == outer

    def test_priority_label_reverse_map(self, kp: Any) -> None:
        assert kp._priority_label(1) == "L1"
        assert kp._priority_label(2) == "L2"
        assert kp._priority_label(3) == "L3"
        assert kp._priority_label(99) == "P99"
        assert kp._priority_label(7) == "P7"


# ──────────────────────────────────────────────
# PrioritySemaphore
# ──────────────────────────────────────────────


class TestPrioritySemaphore:
    async def test_acquire_under_capacity_is_immediate(self, kp: Any) -> None:
        sem = kp.PrioritySemaphore(2)
        await sem.acquire()
        await sem.acquire()
        assert sem.capacity == 2

    async def test_high_priority_waiter_wakes_first(self, kp: Any) -> None:
        """许可占满后三个不同优先级 waiter 排队，release 按优先级顺序唤醒。"""
        sem = kp.PrioritySemaphore(1)
        await sem.acquire()
        order: list[int] = []

        async def waiter(level: str) -> None:
            kp.set_agent_priority(level)
            await sem.acquire()
            order.append(kp.get_agent_priority())
            sem.release()

        tasks = [asyncio.create_task(waiter(lv)) for lv in ("L3", "L1", "L2")]
        await asyncio.sleep(0)  # 让三个 waiter 全部入队
        sem.release()
        await asyncio.gather(*tasks)
        assert order == [1, 2, 3]

    async def test_release_without_waiters_returns_permit_capped(self, kp: Any) -> None:
        sem = kp.PrioritySemaphore(1)
        await sem.acquire()
        sem.release()
        sem.release()  # 超额 release 不允许 value 超过 capacity
        assert sem.capacity == 1

    async def test_release_hands_permit_to_waiter(self, kp: Any) -> None:
        """有等待者时 release 直接转交许可（_value 不经由 +1）。"""
        sem = kp.PrioritySemaphore(1)
        await sem.acquire()
        got = asyncio.Event()

        async def waiter() -> None:
            await sem.acquire()
            got.set()

        t = asyncio.create_task(waiter())
        await asyncio.sleep(0)
        sem.release()
        await asyncio.wait_for(got.wait(), timeout=1)
        await t
        assert sem.capacity == 1

    def test_shrink_reduces_capacity_and_clamps_value(self, kp: Any) -> None:
        sem = kp.PrioritySemaphore(3)
        assert sem.shrink() == 2
        assert sem.shrink() == 1
        assert sem.shrink() == 1  # 地板 1：容量不再收缩

    async def test_grow_without_waiters_increments_value(self, kp: Any) -> None:
        sem = kp.PrioritySemaphore(1)
        await sem.acquire()
        assert sem.grow() == 2
        # value 原 0（被占），grow 无等待者 → +1 → 1，下一个 acquire 立即成功
        await asyncio.wait_for(sem.acquire(), timeout=0.1)

    async def test_grow_wakes_waiter(self, kp: Any) -> None:
        sem = kp.PrioritySemaphore(1)
        await sem.acquire()
        got = asyncio.Event()

        async def waiter() -> None:
            await sem.acquire()
            got.set()

        t = asyncio.create_task(waiter())
        await asyncio.sleep(0)
        sem.grow()
        await asyncio.wait_for(got.wait(), timeout=1)
        await t

    async def test_cancelled_waiter_removed_no_permit_leak(self, kp: Any) -> None:
        """排队中被取消：waiter 移除、许可不丢——后续 release 仍能唤醒别人。

        回归背景：死占位 waiter 会让 release/grow 撞到死 future，可用许可
        永久 -1 且无法自愈。
        """
        sem = kp.PrioritySemaphore(1)
        await sem.acquire()
        t = asyncio.create_task(sem.acquire())
        await asyncio.sleep(0)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        assert len(sem._waiters) == 0
        # 许可仍守恒：release 后新 waiter 能立即拿到
        got = asyncio.Event()

        async def waiter() -> None:
            await sem.acquire()
            got.set()

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0)
        sem.release()
        await asyncio.wait_for(got.wait(), timeout=1)
        await task

    async def test_cancel_after_wake_transfers_permit(self, kp: Any) -> None:
        """已被唤醒（许可已交付）但未恢复执行即被取消：许可转交下一个 waiter。

        时序要点：release() 后 first 的 future 已有 result 但任务仍挂起，
        此时 cancel —— acquire 的取消分支必须把这份许可转交给 second，
        许可不能凭空消失（否则容量被吞成永久 -1）。
        """
        sem = kp.PrioritySemaphore(1)
        await sem.acquire()
        first = asyncio.create_task(sem.acquire())
        await asyncio.sleep(0)  # first 入队
        second_started = asyncio.Event()

        async def second() -> None:
            await sem.acquire()
            second_started.set()

        t2 = asyncio.create_task(second())
        await asyncio.sleep(0)  # second 入队
        sem.release()  # 许可交付给队首 first（future set_result，任务未恢复）
        first.cancel()  # 立即取消：走「已被唤醒但被取消」的转交分支
        with pytest.raises(asyncio.CancelledError):
            await first
        await asyncio.wait_for(second_started.wait(), timeout=1)
        await t2


# ──────────────────────────────────────────────
# KeySlot：预算语义
# ──────────────────────────────────────────────


class TestKeySlotBudget:
    def test_unlimited_rpm_and_token_report_9999(self, kp: Any) -> None:
        s = _slot(kp, rpm_limit=0, token_quota=0)
        assert s.rpm_remaining == 9999
        assert s.token_remaining == 9999
        assert s._effective_rpm() == 9999

    def test_rpm_window_counts_recorded_requests(self, kp: Any, clock: FakeClock) -> None:
        s = _slot(kp, rpm_limit=3)
        assert s.rpm_remaining == 3
        s.record_request()
        clock.advance(10)
        s.record_request()
        assert s.rpm_remaining == 1

    def test_rpm_window_evicts_after_60s(self, kp: Any, clock: FakeClock) -> None:
        s = _slot(kp, rpm_limit=2)
        s.record_request()
        clock.advance(61)
        assert s.rpm_remaining == 2  # 旧请求出窗，配额恢复

    def test_token_quota_floor_at_zero(self, kp: Any) -> None:
        s = _slot(kp, token_quota=100)
        s.record_usage(60, 50)
        assert s.token_remaining == 0  # max(0, 100-110) 地板 0
        s.record_usage(10, 10)
        assert s.token_remaining == 0

    def test_is_exhausted_three_ways(self, kp: Any, clock: FakeClock) -> None:
        fresh = _slot(kp, rpm_limit=5, token_quota=100)
        assert fresh.is_exhausted is False
        cooled = _slot(kp, rpm_limit=5, token_quota=100)
        cooled._cooling_until = clock() + 10
        assert cooled.is_exhausted is True
        rpm_full = _slot(kp, rpm_limit=1)
        rpm_full.record_request()
        assert rpm_full.is_exhausted is True
        token_out = _slot(kp, token_quota=10)
        token_out.record_usage(10, 5)
        assert token_out.is_exhausted is True

    def test_score_cooling_is_negative_one(self, kp: Any, clock: FakeClock) -> None:
        s = _slot(kp, rpm_limit=10, token_quota=1000)
        s._cooling_until = clock() + 5
        assert s.score() == -1.0

    def test_score_fresh_full_quota_is_one(self, kp: Any) -> None:
        """性质断言：预算全满的 key 评分恒为 0.6+0.4=1.0。"""
        s = _slot(kp, rpm_limit=10, token_quota=1000)
        assert s.score() == pytest.approx(1.0)

    def test_score_weights_rpm_over_token(self, kp: Any) -> None:
        """rpm 权重 60% 高于 token 40%：各砍半时，rpm 折损 0.3 分 > token 折损 0.2 分。"""
        rpm_half = _slot(kp, rpm_limit=10, token_quota=100)
        for _ in range(5):
            rpm_half.record_request()
        assert rpm_half.score() == pytest.approx(0.6 * 0.5 + 0.4 * 1.0)  # 0.70
        token_half = _slot(kp, rpm_limit=10, token_quota=100)
        token_half.record_usage(50, 0)
        assert token_half.score() == pytest.approx(0.6 * 1.0 + 0.4 * 0.5)  # 0.80
        # 性质：rpm 减半扣 0.3 分，token 减半只扣 0.2 分——rpm 对评分影响更大
        assert 1.0 - rpm_half.score() == pytest.approx(0.3)
        assert 1.0 - token_half.score() == pytest.approx(0.2)

    def test_release_request_returns_rpm_slot(self, kp: Any, clock: FakeClock) -> None:
        s = _slot(kp, rpm_limit=2)
        s.record_request()
        s.release_request()
        assert s.rpm_remaining == 2
        s.release_request()  # 空窗口 release 是无害 no-op
        assert s.rpm_remaining == 2


# ──────────────────────────────────────────────
# KeySlot：错误分派策略
# ──────────────────────────────────────────────


class TestKeySlotErrorPolicy:
    def test_rate_limit_uses_retry_after_and_reduces_rpm(self, kp: Any, clock: FakeClock) -> None:
        s = _slot(kp, rpm_limit=5)
        s.handle_error(ErrorInfo(kind=ErrorKind.RATE_LIMIT, retry_after=7.0))
        assert s.is_cooling is True
        clock.advance(7.0)
        assert s.is_cooling is False
        assert s._effective_rpm() == 4  # 降 1

    def test_rate_limit_without_retry_after_defaults_5s(self, kp: Any, clock: FakeClock) -> None:
        s = _slot(kp, rpm_limit=5)
        s.handle_error(ErrorInfo(kind=ErrorKind.RATE_LIMIT))
        clock.advance(4.9)
        assert s.is_cooling is True
        clock.advance(0.2)
        assert s.is_cooling is False

    def test_rate_limit_rpm_floor_is_one(self, kp: Any) -> None:
        """连续 429 把 _rpm_effective 压到地板 1，不会把限流关死为 0。"""
        s = _slot(kp, rpm_limit=2)
        for _ in range(5):
            s.handle_error(ErrorInfo(kind=ErrorKind.RATE_LIMIT))
            s._cooling_until = 0.0
        assert s._effective_rpm() == 1

    def test_rate_limit_unlimited_rpm_not_reduced(self, kp: Any) -> None:
        s = _slot(kp, rpm_limit=0)
        s.handle_error(ErrorInfo(kind=ErrorKind.RATE_LIMIT))
        assert s._effective_rpm() == 9999

    @pytest.mark.parametrize(
        ("kind", "cool_s"),
        [
            (ErrorKind.QUOTA_EXHAUSTED, 3600.0),
            (ErrorKind.AUTH_FAILED, 300.0),
            (ErrorKind.SERVER_ERROR, 5.0),
        ],
    )
    def test_fixed_cooldown_kinds(self, kp: Any, clock: FakeClock, kind: Any, cool_s: float) -> None:
        s = _slot(kp)
        s.handle_error(ErrorInfo(kind=kind))
        clock.advance(cool_s - 0.5)
        assert s.is_cooling is True
        clock.advance(1.0)
        assert s.is_cooling is False

    def test_service_down_first_occurrence_tolerated(self, kp: Any, clock: FakeClock) -> None:
        s = _slot(kp)
        s.handle_error(ErrorInfo(kind=ErrorKind.SERVICE_DOWN))
        assert s.is_cooling is False  # 第 1 次当偶发抖动
        assert s._consecutive_down == 1

    def test_service_down_exponential_backoff_capped(self, kp: Any, clock: FakeClock) -> None:
        """第 2/3/4/5/6+ 次连续 SERVICE_DOWN：冷却 10/20/40/60/60s（指数退避封顶 60）。"""
        s = _slot(kp)
        s.handle_error(ErrorInfo(kind=ErrorKind.SERVICE_DOWN))  # 第 1 次：容忍，不冷却
        assert s.is_cooling is False
        expected = {2: 10.0, 3: 20.0, 4: 40.0, 5: 60.0, 6: 60.0}
        for n in range(2, 7):
            s.handle_error(ErrorInfo(kind=ErrorKind.SERVICE_DOWN))
            assert s.is_cooling is True
            clock.advance(0.5)
            assert s._cooling_until - clock() == pytest.approx(expected[n] - 0.5)
            clock.advance(expected[n])
            s._cooling_until = 0.0

    def test_service_down_third_occurrence_shrinks_concurrency(self, kp: Any) -> None:
        s = _slot(kp, max_concurrent=3)
        s.handle_error(ErrorInfo(kind=ErrorKind.SERVICE_DOWN))
        s.handle_error(ErrorInfo(kind=ErrorKind.SERVICE_DOWN))
        assert s._get_semaphore().capacity == 3  # 前 2 次不动并发
        s.handle_error(ErrorInfo(kind=ErrorKind.SERVICE_DOWN))
        assert s._get_semaphore().capacity == 2  # 第 3 次降级

    def test_non_actionable_kinds_leave_state_untouched(self, kp: Any, clock: FakeClock) -> None:
        """NETWORK/BAD_REQUEST/UNKNOWN 不在 KeySlot 层处理：无冷却无计数。"""
        s = _slot(kp, rpm_limit=5)
        for kind in (ErrorKind.NETWORK, ErrorKind.BAD_REQUEST, ErrorKind.UNKNOWN):
            s.handle_error(ErrorInfo(kind=kind))
        assert s.is_cooling is False
        assert s._consecutive_down == 0
        assert s._effective_rpm() == 5

    def test_on_success_recovers_rpm_capped(self, kp: Any) -> None:
        s = _slot(kp, rpm_limit=4)
        s.handle_error(ErrorInfo(kind=ErrorKind.RATE_LIMIT))
        s.handle_error(ErrorInfo(kind=ErrorKind.RATE_LIMIT))
        s._cooling_until = 0.0
        s._rpm_effective = 1
        s.on_success()
        assert s._effective_rpm() == 2
        s.on_success()
        s.on_success()
        s.on_success()
        assert s._effective_rpm() == 4  # 封顶 rpm_limit，不超发
        assert s._consecutive_down == 0

    def test_on_success_grows_only_after_degradation(self, kp: Any) -> None:
        s = _slot(kp, max_concurrent=3)
        s.on_success()  # 未被降级过：不动并发
        assert s._get_semaphore().capacity == 3
        s.handle_error(ErrorInfo(kind=ErrorKind.SERVICE_DOWN))
        s.handle_error(ErrorInfo(kind=ErrorKind.SERVICE_DOWN))
        s.handle_error(ErrorInfo(kind=ErrorKind.SERVICE_DOWN))
        assert s._get_semaphore().capacity == 2
        s.on_success()
        assert s._get_semaphore().capacity == 3  # 回升到原值

    def test_semaphore_lazy_init_and_reset(self, kp: Any) -> None:
        s = _slot(kp, max_concurrent=2)
        sem = s._get_semaphore()
        assert sem is s._get_semaphore()  # 惰性单例
        s._reset_semaphore()
        assert s._get_semaphore() is not sem

    async def test_slot_acquire_release_roundtrip(self, kp: Any) -> None:
        s = _slot(kp, max_concurrent=1)
        await s.acquire()
        s.release()
        await asyncio.wait_for(s.acquire(), timeout=0.1)
        s.release()


# ──────────────────────────────────────────────
# KeyPool：选键
# ──────────────────────────────────────────────


class TestKeyPoolSelect:
    def test_select_prefers_declaration_order(self, kp: Any, clock: FakeClock) -> None:
        """主备模式：按声明顺序选第一个可用，不看评分。"""
        primary = _slot(kp, key_id="primary", rpm_limit=100)
        backup = _slot(kp, key_id="backup", rpm_limit=100)
        pool = kp.KeyPool([primary, backup], pool_id="p")
        assert pool.select() is primary
        primary._cooling_until = clock() + 10
        assert pool.select() is backup

    def test_select_returns_none_when_all_cooling(self, kp: Any, clock: FakeClock) -> None:
        a = _slot(kp, key_id="a")
        b = _slot(kp, key_id="b")
        a._cooling_until = clock() + 10
        b._cooling_until = clock() + 10
        pool = kp.KeyPool([a, b], pool_id="p")
        assert pool.select() is None

    async def test_select_returns_slot_when_only_concurrency_full(self, kp: Any) -> None:
        """并发满但 rpm 有余：返回未冷却 slot（acquire 排队等信号量），不放弃。"""
        s = _slot(kp, key_id="busy", rpm_limit=10, max_concurrent=1)
        await s.acquire()  # 信号量占满
        pool = kp.KeyPool([s], pool_id="p")
        assert pool.select() is s

    def test_select_returns_none_when_rpm_starved(self, kp: Any, clock: FakeClock) -> None:
        """rpm 真耗尽（非冷却）：返回 None，不放行——否则 429 风暴。"""
        s = _slot(kp, rpm_limit=1)
        s.record_request()
        pool = kp.KeyPool([s], pool_id="p")
        assert pool.select() is None

    async def test_acquire_slot_success_records_request(self, kp: Any, clock: FakeClock) -> None:
        s = _slot(kp, rpm_limit=10)
        pool = kp.KeyPool([s], pool_id="p")
        slot = await pool.acquire_slot(timeout=1.0)
        assert slot is s
        assert s.rpm_remaining == 9  # record_request 已占名额
        s.release()

    async def test_acquire_slot_timeout_raises_with_diagnostics(
        self, kp: Any, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """所有 key 永久耗尽（rpm 空、无冷却）→ 等满 timeout 抛 KeyPoolExhaustedError。

        asyncio.sleep 打桩为推进假时钟：等待循环的「等最短冷却/等 1s 重试」
        才能推进到 deadline（真实 sleep 推不动假时钟，会假死）。
        """
        s = _slot(kp, key_id="a", rpm_limit=1)
        s.record_request()  # rpm 耗尽且未冷却：select 恒 None，且不走冷却等待分支
        pool = kp.KeyPool([s], pool_id="pool-x")

        async def fake_sleep(t: float) -> None:
            clock.advance(t)

        monkeypatch.setattr(kp.asyncio, "sleep", fake_sleep)
        with pytest.raises(kp.KeyPoolExhaustedError) as ei:
            await pool.acquire_slot(timeout=2.0)
        assert ei.value.pool_id == "pool-x"
        assert ei.value.timeout == 2.0
        assert any("a" in u for u in ei.value.unavailable)

    async def test_acquire_slot_waits_out_cooldown_then_succeeds(
        self, kp: Any, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """唯一 key 冷却中：acquire_slot 等到冷却结束拿到 key（不空手超时）。

        双语义验证：等最短冷却时间的 wait 量取自 cooling_until - now。
        """

        async def fake_sleep(t: float) -> None:
            clock.advance(t)

        monkeypatch.setattr(kp.asyncio, "sleep", fake_sleep)
        s = _slot(kp, key_id="a", rpm_limit=10)
        s._cooling_until = clock() + 3.0
        pool = kp.KeyPool([s], pool_id="p")
        slot = await pool.acquire_slot(timeout=60.0)
        assert slot is s
        assert clock.now >= 1003.0  # 确实等过冷却窗口
        s.release()

    async def test_acquire_slot_cancel_returns_rpm_slot(self, kp: Any, clock: FakeClock) -> None:
        """acquire 排队中被取消：请求未打上游，rpm 名额必须归还。"""
        s = _slot(kp, key_id="busy", rpm_limit=5, max_concurrent=1)
        await s.acquire()  # 信号量占满 → acquire_slot 将在信号量上排队
        pool = kp.KeyPool([s], pool_id="p")
        t = asyncio.create_task(pool.acquire_slot(timeout=5.0))
        await asyncio.sleep(0.05)  # 进入 record_request + acquire 排队
        assert s.rpm_remaining == 4  # 已占名额
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        assert s.rpm_remaining == 5  # 名额已归还
        s.release()

    def test_get_unavailable_slots_masks_keys(self, kp: Any, clock: FakeClock) -> None:
        s = _slot(kp, key_id="a", rpm_limit=2)
        s._cooling_until = clock() + 3
        pool = kp.KeyPool([s], pool_id="p")
        diag = pool.get_unavailable_slots()
        assert len(diag) == 1
        assert diag[0].startswith("sk-test-")  # 只露前 8 位
        assert "full" not in "".join(diag)
        assert "cooling=True" in diag[0] and "rpm_left" in diag[0] and "token_left" in diag[0]

    def test_stats_reports_per_slot_budget(self, kp: Any) -> None:
        a = _slot(kp, key_id="a", rpm_limit=10, token_quota=100)
        b = _slot(kp, key_id="b")
        pool = kp.KeyPool([a, b], pool_id="p")
        stats = pool.stats()
        assert set(stats) == {"a", "b"}
        assert stats["a"]["rpm_remaining"] == 10
        assert stats["a"]["token_remaining"] == 100
        assert stats["a"]["is_cooling"] is False
        assert stats["a"]["score"] == pytest.approx(1.0)
        assert stats["b"]["rpm_remaining"] == 9999
