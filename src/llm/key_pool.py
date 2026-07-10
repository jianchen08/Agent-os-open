"""Key Pool — 多 key 聚合 + 滑动窗口限流 + 配额追踪。"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import time as _time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from llm.exceptions import KeyPoolExhaustedError

if TYPE_CHECKING:
    from llm.error_classifier import ErrorInfo

logger = logging.getLogger(__name__)

# Agent 层级优先级

_current_agent_priority: contextvars.ContextVar[int] = contextvars.ContextVar(
    "agent_priority",
    default=99,
)

_LEVEL_PRIORITY_MAP: dict[str, int] = {
    "L1": 1,
    "L2": 2,
    "L3": 3,
}


def set_agent_priority(agent_level: str | None) -> None:
    """设置当前协程的 Agent 层级优先级。"""
    priority = _LEVEL_PRIORITY_MAP.get(agent_level or "", 99)
    _current_agent_priority.set(priority)


def get_agent_priority() -> int:
    """获取当前协程的 Agent 层级优先级。"""
    return _current_agent_priority.get()


def _priority_label(priority: int) -> str:
    """将优先级数值转为可读的层级标签。"""
    _reverse = {v: k for k, v in _LEVEL_PRIORITY_MAP.items()}
    return _reverse.get(priority, f"P{priority}")


class PrioritySemaphore:
    """优先级信号量 — 高优先级请求优先获取许可，支持动态缩容/扩容。"""

    def __init__(self, value: int = 1) -> None:
        self._value = value
        self._capacity = value  # 当前容量上限（shrink/grow 修改）
        self._waiters: list[tuple[int, asyncio.Future]] = []

    @property
    def capacity(self) -> int:
        """当前容量上限。"""
        return self._capacity

    async def acquire(self) -> None:
        """获取一个许可。高优先级（数值小）的请求优先获取。"""
        priority = get_agent_priority()

        if self._value > 0:
            self._value -= 1
            return

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._waiters.append((priority, fut))
        self._waiters.sort(key=lambda x: x[0])

        # 诊断日志：排队等待时打印优先级和等待队列状态
        queue_desc = ",".join(f"({p},{_priority_label(p)})" for p, _ in self._waiters)
        logger.info(
            "[PrioritySemaphore] 排队等待 | level=%s priority=%d | waiters=%d | queue=[%s]",
            _priority_label(priority),
            priority,
            len(self._waiters),
            queue_desc,
        )

        try:
            await fut
        except asyncio.CancelledError:
            # 被取消（首字节超时 / 用户停止）时，必须把自己的 waiter 从队列移除，
            # 否则 (priority, fut) 残留成死占位：release()/grow() 撞到死 future 时
            # 既不唤醒任何活等待者、也不回填 _value，可用许可被凭空吞掉，
            # 每发生一次永久 -1 → LLM 排队越积越多且无法自愈。
            # 注意区分两种 done：
            #   - fut.cancelled()：自己是被 cancel 的（没拿到许可）→ 仅移除，不转交
            #   - fut 有 result：自己已被 release/grow 唤醒（许可已交给我）→
            #     此刻被取消要转交给下一个活等待者，许可不能凭空消失
            if fut.cancelled():
                with contextlib.suppress(ValueError):
                    self._waiters.remove((priority, fut))
            elif fut.done():
                # 自己已被唤醒但被取消：把这份许可转交给下一个活等待者
                self._wake_next()
            raise

    def _wake_next(self) -> None:
        """唤醒队首第一个「活的、未完成」的等待者；跳过死 future。

        若没有可唤醒的活等待者，则回填 _value（许可归还池）。
        """
        while self._waiters:
            w_priority, fut = self._waiters.pop(0)
            if not fut.done():
                fut.set_result(None)
                logger.info(
                    "[PrioritySemaphore] 唤醒等待者 | level=%s priority=%d | remaining_waiters=%d",
                    _priority_label(w_priority),
                    w_priority,
                    len(self._waiters),
                )
                return
            # 死 future（被 cancel 的 waiter）：直接丢弃，继续找下一个活的
            logger.debug(
                "[PrioritySemaphore] 跳过死 waiter | remaining_waiters=%d",
                len(self._waiters),
            )
        # 没有活等待者：回填许可到池
        if self._value < self._capacity:
            self._value += 1

    def release(self) -> None:
        """释放一个许可。唤醒最高优先级的等待者。"""
        if self._waiters and self._value < self._capacity:
            # 容量未满且有等待者：交给等待者，不增加 _value
            self._wake_next()
        elif self._value < self._capacity:
            self._value += 1

    def shrink(self) -> int:
        """缩小容量 1（弹性降级）。返回缩容后的新容量。"""
        if self._capacity <= 1:
            return self._capacity
        self._capacity -= 1
        # _value 不能超过新容量
        self._value = min(self._value, self._capacity)
        logger.info(
            "[PrioritySemaphore] 缩容 → capacity=%d (value=%d, waiters=%d)",
            self._capacity,
            self._value,
            len(self._waiters),
        )
        return self._capacity

    def grow(self) -> int:
        """扩大容量 1（弹性回升）。返回扩容后的新容量。"""
        self._capacity += 1
        # 扩容后可立即满足一个等待者（跳过死 waiter）
        if self._waiters:
            self._wake_next()
        else:
            self._value += 1
        logger.info(
            "[PrioritySemaphore] 扩容 → capacity=%d (value=%d, waiters=%d)",
            self._capacity,
            self._value,
            len(self._waiters),
        )
        return self._capacity


@dataclass
class KeySlot:
    """单个 API key 的状态追踪。"""

    key_id: str
    api_key: str
    api_base: str = ""
    max_concurrent: int = 2
    rpm_limit: int = 0
    token_quota: int = 0

    # 运行时状态
    # rpm-as-primary-limiter: rpm 是限流主参数。遇 RATE_LIMIT 降 _rpm_effective（地板 1），
    # on_success 升 _rpm_effective（封顶 rpm_limit）。max_concurrent 不再因限流而变。
    _rpm_effective: int = 0
    _semaphore: PrioritySemaphore | None = field(default=None, repr=False)
    _request_timestamps: list[float] = field(default_factory=list, repr=False)
    _tokens_used: int = 0
    _cooling_until: float = 0.0
    _consecutive_down: int = 0  # 连续 SERVICE_DOWN 次数（指数退避用）

    def _get_semaphore(self) -> PrioritySemaphore:
        if self._semaphore is None:
            self._semaphore = PrioritySemaphore(self.max_concurrent)
        return self._semaphore

    def _reset_semaphore(self) -> None:
        self._semaphore = None

    @property
    def is_cooling(self) -> bool:
        return _time.monotonic() < self._cooling_until

    @property
    def rpm_remaining(self) -> int:
        """当前窗口内剩余可用请求数（按生效 rpm 上限扣减已发请求）。"""
        if self.rpm_limit <= 0:
            return 9999
        now = _time.monotonic()
        self._evict_old(now)
        effective = self._effective_rpm()
        return max(0, effective - len(self._request_timestamps))

    def _effective_rpm(self) -> int:
        """当前生效的 rpm 上限（被降级时小于 rpm_limit，最低 1；rpm_limit<=0 不限）。"""
        if self.rpm_limit <= 0:
            return 9999
        # _rpm_effective<=0 表示尚未初始化（首次），按 rpm_limit 起步
        if self._rpm_effective <= 0:
            self._rpm_effective = self.rpm_limit
        return self._rpm_effective

    @property
    def token_remaining(self) -> int:
        """剩余 token 配额。"""
        if self.token_quota <= 0:
            return 9999
        return max(0, self.token_quota - self._tokens_used)

    @property
    def is_exhausted(self) -> bool:
        """key 是否完全不可用（冷却中 or RPM 满 or 配额耗尽）。"""
        return self.is_cooling or self.rpm_remaining <= 0 or self.token_remaining <= 0

    def score(self) -> float:
        """选 key 时的评分，越高越优先选。"""
        if self.is_cooling:
            return -1.0
        rpm_ratio = self.rpm_remaining / max(self._effective_rpm(), 1)
        token_ratio = self.token_remaining / max(self.token_quota, 1)
        return rpm_ratio * 0.6 + token_ratio * 0.4

    def record_request(self) -> None:
        """记录一次请求（占一个 rpm 名额）。

        必须在 acquire() 之前调用：select() 的 rpm_remaining>0 检查与 acquire() 之间
        若不占名额，并发请求可同时通过检查绕过本地 RPM 限流，全部放行上游导致 429。
        """
        now = _time.monotonic()
        self._evict_old(now)
        self._request_timestamps.append(now)

    def release_request(self) -> None:
        """归还最近一次 record_request 占的 rpm 名额。

        用于 acquire() 排队中被 cancel（未真正打上游）的请求——这种请求
        若不归还名额，会虚占 rpm 窗口 60s，导致正常请求被误判为 rpm 耗尽而排队。
        """
        if self._request_timestamps:
            self._request_timestamps.pop()

    def record_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        """记录一次请求的 token 消耗。"""
        self._tokens_used += prompt_tokens + completion_tokens

    def handle_error(self, info: ErrorInfo) -> None:
        """按统一错误类型应用策略（取代旧的 on_rate_limit 万能方法）。"""
        from llm.error_classifier import ErrorKind  # noqa: PLC0415

        kind = info.kind
        retry_after = info.retry_after

        if kind == ErrorKind.RATE_LIMIT:
            cool = retry_after if retry_after and retry_after > 0 else 5.0
            self._cooling_until = _time.monotonic() + cool
            # rpm-as-primary-limiter: 限流主参数是 rpm，而非 max_concurrent。
            # 429 后降 _rpm_effective（地板 1），max_concurrent 不变（避免误伤并发吞吐）。
            self._reduce_rpm()
            logger.info(
                "[KeySlot] %s RATE_LIMIT 冷却 %.1fs + rpm 降级 → %d",
                self.key_id,
                cool,
                self._effective_rpm(),
            )
        elif kind == ErrorKind.QUOTA_EXHAUSTED:
            self._cooling_until = _time.monotonic() + 3600.0
            logger.warning(
                "[KeySlot] %s QUOTA_EXHAUSTED 冷却 3600s",
                self.key_id,
            )
        elif kind == ErrorKind.AUTH_FAILED:
            self._cooling_until = _time.monotonic() + 300.0
            logger.warning(
                "[KeySlot] %s AUTH_FAILED 冷却 300s",
                self.key_id,
            )
        elif kind == ErrorKind.SERVICE_DOWN:
            self._consecutive_down += 1
            n = self._consecutive_down
            # 第 1 次当偶发抖动容忍，不冷却（让 adapter 立即退避重试）。
            # 从第 2 次起置递增短冷却，让 is_cooling=True，select() 暂时绕开
            # 这个 key（否则单 key 场景下 select() 会无限选回它，陷入
            # 「选坏 key → 503 → 退避 → 又选回」死循环）。冷却时长指数退避
            # 封顶 60s，避免长冷却后忘记恢复。
            if n >= 2:
                cool = min(10.0 * (2 ** (n - 2)), 60.0)
                self._cooling_until = _time.monotonic() + cool
                logger.info(
                    "[KeySlot] %s SERVICE_DOWN 连续 %d 次，冷却 %.0fs",
                    self.key_id,
                    n,
                    cool,
                )
            # 累计 3 次确认非偶发，并发降级
            if n >= 3:
                self._reduce_concurrency()
                logger.warning(
                    "[KeySlot] %s SERVICE_DOWN 连续 %d 次，并发降级",
                    self.key_id,
                    n,
                )
            else:
                logger.info(
                    "[KeySlot] %s SERVICE_DOWN 第 %d 次（adapter 退避重试）",
                    self.key_id,
                    n,
                )
        elif kind == ErrorKind.SERVER_ERROR:
            self._cooling_until = _time.monotonic() + 5.0
            logger.info("[KeySlot] %s SERVER_ERROR 冷却 5s", self.key_id)
        # NETWORK / BAD_REQUEST / UNKNOWN：不在此处理

    def _reduce_concurrency(self) -> None:
        """弹性并发降级：信号量缩容 1 级（最低到 1）。"""
        sem = self._get_semaphore()
        new_cap = sem.shrink()
        logger.info(
            "[KeySlot] %s 并发降级 → %d (原 %d)",
            self.key_id,
            new_cap,
            self.max_concurrent,
        )

    def _reduce_rpm(self) -> None:
        """rpm 限流降级：_rpm_effective 减 1（地板 1，避免把限流关死）。

        rpm_limit<=0（不限）时不降——避免把无限误降成有限。
        """
        if self.rpm_limit <= 0:
            return
        cur = self._effective_rpm()
        if cur > 1:
            self._rpm_effective = cur - 1

    def _recover_rpm(self) -> None:
        """rpm 限流回升：_rpm_effective 加 1（封顶 rpm_limit，不超发）。"""
        if self.rpm_limit <= 0:
            return
        cur = self._effective_rpm()
        if cur < self.rpm_limit:
            self._rpm_effective = cur + 1

    def on_success(self) -> None:
        """成功调用：恢复连续失败计数 + rpm 回升 1 级（封顶 rpm_limit）。

        并发回升：仅当并发曾被 SERVICE_DOWN 降级（capacity<max_concurrent）时才回升，
        RATE_LIMIT 不动并发，故正常限流路径下 capacity==max_concurrent 不会触发 grow。
        """
        self._consecutive_down = 0
        self._recover_rpm()
        sem = self._get_semaphore()
        if sem.capacity < self.max_concurrent:
            new_cap = sem.grow()
            logger.info(
                "[KeySlot] %s 并发回升 → %d",
                self.key_id,
                new_cap,
            )

    def _evict_old(self, now: float) -> None:
        """清除 60 秒前的请求时间戳。"""
        cutoff = now - 60.0
        self._request_timestamps = [t for t in self._request_timestamps if t > cutoff]

    async def acquire(self) -> None:
        """获取并发许可。"""
        await self._get_semaphore().acquire()

    def release(self) -> None:
        """释放并发许可。"""
        self._get_semaphore().release()


class KeyPool:
    """多 key 池 — 从一组 KeySlot 中选最优 key。"""

    def __init__(self, slots: list[KeySlot], pool_id: str = "") -> None:
        self.pool_id = pool_id
        self._slots = slots

    @property
    def slots(self) -> list[KeySlot]:
        return self._slots

    def select(self) -> KeySlot | None:
        """按 slots 声明顺序选第一个可用 key（主备模式）。"""
        available = [s for s in self._slots if not s.is_exhausted]
        if available:
            return available[0]

        # 所有 key 都进入 exhausted。区分两种情况：
        #   - 并发满（is_exhausted 仅因 token/rpm 满不成立，但信号量占满）：
        #     返回未冷却的，acquire_slot 在 record_request 后等待信号量释放。
        #   - rpm 真正耗尽（rpm_remaining<=0）：不能返回——否则 acquire_slot 会
        #     再 record_request 占名额，rpm 限流形同虚设（5 并发全放行上游→429）。
        #     返回 None 让 acquire_slot 等冷却/窗口滚动。
        rpm_starved = [s for s in self._slots if not s.is_cooling and s.rpm_remaining > 0]
        if rpm_starved:
            return rpm_starved[0]

        unavailable = self.get_unavailable_slots()
        logger.warning(
            "[KeyPool] %s 所有 key 均不可用 (cooling/exhausted): %s",
            self.pool_id,
            unavailable,
        )
        return None

    async def acquire_slot(self, timeout: float = 60.0) -> KeySlot:
        """选 key 并获取并发许可，阻塞直到有 key 可用或超时。"""
        deadline = _time.monotonic() + timeout
        while True:
            slot = self.select()
            if slot is not None:
                # record_request() 必须在 await acquire() 之前，
                # 否则并发请求可同时通过 select() 的 rpm_remaining>0 检查，
                # 绕过本地 RPM 限流，全部放行到上游导致 429。
                slot.record_request()
                try:
                    await slot.acquire()
                except BaseException:
                    # acquire 排队中被 cancel（首字节超时 / 用户停止）：请求
                    # 未真正打上游，不该占 rpm 名额，归还避免虚占 60s 窗口。
                    slot.release_request()
                    raise
                return slot
            if _time.monotonic() >= deadline:
                unavailable = self.get_unavailable_slots()
                logger.error(
                    "[KeyPool] %s 所有 key 不可用，等待 %.0fs 超时；诊断: %s",
                    self.pool_id,
                    timeout,
                    unavailable,
                )
                raise KeyPoolExhaustedError(self.pool_id, timeout, unavailable)
            # 所有 key 都满了，等最短冷却时间
            cool_slots = [s for s in self._slots if s.is_cooling]
            if cool_slots:
                earliest = min(s._cooling_until for s in cool_slots)
                wait = max(0.1, earliest - _time.monotonic())
                logger.debug(
                    "[KeyPool] %s 所有 key 忙，等待 %.1fs",
                    self.pool_id,
                    wait,
                )
                await asyncio.sleep(wait)
            else:
                # 没有 cooling 的但都 exhausted，等一小段时间重试
                await asyncio.sleep(1.0)

    def get_unavailable_slots(self) -> list[str]:
        """返回当前不可用 key 的诊断信息（脱敏 key 前缀）。"""
        return [
            f"{s.api_key[:8]}...(cooling={s.is_cooling}, rpm_left={s.rpm_remaining}, token_left={s.token_remaining})"
            for s in self._slots
        ]

    def stats(self) -> dict[str, dict[str, int | float | bool]]:
        """返回各 key 的状态摘要。"""
        result = {}
        for s in self._slots:
            result[s.key_id] = {
                "rpm_remaining": s.rpm_remaining,
                "token_remaining": s.token_remaining,
                "is_cooling": s.is_cooling,
                "score": s.score(),
            }
        return result
