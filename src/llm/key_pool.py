"""Key Pool — 多 key 聚合 + 滑动窗口限流 + 配额追踪。

每个 API key 对应一个 KeySlot，独立追踪：
- RPM（滑动窗口，本地主动防御）
- 并发数（信号量）
- Token 配额（累加 usage）
- 429 冷却期（被动兜底 + 校准）

KeyPool 从 provider 的多个 key 中选"最空闲"的 key，
聚合吞吐量，某个 key 限额到了自动切换。
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time as _time
from dataclasses import dataclass, field

from llm.exceptions import KeyPoolExhaustedError

logger = logging.getLogger(__name__)

# ---- Agent 层级优先级 ----

_current_agent_priority: contextvars.ContextVar[int] = contextvars.ContextVar(
    "agent_priority", default=99,
)

_LEVEL_PRIORITY_MAP: dict[str, int] = {
    "L1": 1,
    "L2": 2,
    "L3": 3,
}


def set_agent_priority(agent_level: str | None) -> None:
    """设置当前协程的 Agent 层级优先级。

    Args:
        agent_level: Agent 层级标识（L1/L2/L3），未知层级默认为 99。
    """
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
    """优先级信号量 — 高优先级请求优先获取许可，支持动态缩容/扩容。

    弹性并发：收到限流/服务不可用时调用 shrink() 缩小容量（降级），
    成功调用累积后调用 grow() 恢复容量（回升）。这是真正实现
    adapter.py docstring 承诺的「自适应并发 1-3」。
    """

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
        queue_desc = ",".join(
            f"({p},{_priority_label(p)})" for p, _ in self._waiters
        )
        logger.info(
            "[PrioritySemaphore] 排队等待 | level=%s priority=%d | waiters=%d | queue=[%s]",
            _priority_label(priority), priority, len(self._waiters), queue_desc,
        )

        # BUG-FIX-fix_20260629_dead_waiter_permit_leak:
        # 排队中的请求被取消（首字节超时 / 用户停止 / 任务取消）时，CancelledError
        # 会让 asyncio 自动取消正在 await 的 fut，但 (priority, fut) 仍残留在
        # _waiters 中成为死占位。后续 release()/grow() 撞到这种 fut.done()=True
        # 的死 waiter 时既不唤醒任何活等待者、也不回填 _value，可用许可被凭空
        # 丢弃，每发生一次永久 -1 → LLM 排队越积越多且无法自愈。
        # 修复：cancel 时用 is 身份比较移除自己的占位（不误删同优先级的其他 future）。
        try:
            await fut
        except asyncio.CancelledError:
            self._waiters = [(p, f) for p, f in self._waiters if f is not fut]
            raise

    def _pop_next_live_waiter(self) -> tuple[int, asyncio.Future] | None:
        """取出最高优先级的活等待者，跳过并丢弃已取消的死 waiter。

        被取消的 future（done()=True）本不该出现在队列里——acquire() 的自清理
        已处理正常路径；此方法是对"future 异常完成"的防御深度，确保
        release()/grow() 永远把许可交给真正在等的活 waiter，绝不凭空丢弃。
        """
        while self._waiters:
            w_priority, fut = self._waiters.pop(0)
            if not fut.done():
                return w_priority, fut
        return None

    def release(self) -> None:
        """释放一个许可。唤醒最高优先级的活等待者，无活等待者时回填 _value。"""
        if self._value >= self._capacity:
            # 容量已满：超额释放无效，防止信号量超发
            return
        live = self._pop_next_live_waiter()
        if live is not None:
            w_priority, fut = live
            fut.set_result(None)
            logger.info(
                "[PrioritySemaphore] 唤醒等待者 | level=%s priority=%d | remaining_waiters=%d",
                _priority_label(w_priority), w_priority, len(self._waiters),
            )
        else:
            self._value += 1

    def shrink(self) -> int:
        """缩小容量 1（弹性降级）。返回缩容后的新容量。

        若有正在等待的请求因容量缩小而无法满足，取消并让其重新排队选 key。
        """
        if self._capacity <= 1:
            return self._capacity
        self._capacity -= 1
        # _value 不能超过新容量
        self._value = min(self._value, self._capacity)
        logger.info(
            "[PrioritySemaphore] 缩容 → capacity=%d (value=%d, waiters=%d)",
            self._capacity, self._value, len(self._waiters),
        )
        return self._capacity

    def grow(self) -> int:
        """扩大容量 1（弹性回升）。返回扩容后的新容量。"""
        self._capacity += 1
        # 扩容后可立即满足一个活等待者；跳过死 waiter，避免扩容带来的许可被吞。
        live = self._pop_next_live_waiter()
        if live is not None:
            _w_priority, fut = live
            fut.set_result(None)
        else:
            self._value += 1
        logger.info(
            "[PrioritySemaphore] 扩容 → capacity=%d (value=%d, waiters=%d)",
            self._capacity, self._value, len(self._waiters),
        )
        return self._capacity


@dataclass
class KeySlot:
    """单个 API key 的状态追踪。

    Attributes:
        key_id: 标识符（如 zhipu_key_1）
        api_key: 实际的 API key 字符串
        api_base: 可选的 API base URL
        max_concurrent: 最大并发数（信号量初始容量）。限流主参数为 rpm，
            max_concurrent 通常设大值（如 1000）使其形同不限。
        rpm_limit: 每分钟请求数上限。限流唯一主参数，配置规范要求非 0；
            漏配为 0 时 rpm_remaining 兜底返回 9999（避免死锁），但生产配置必须显式给值。
            429/SERVICE_DOWN 后弹性降级、成功后回升，均围绕 rpm_limit 波动。
        token_quota: Token 配额上限（0 表示不限）
    """

    key_id: str
    api_key: str
    api_base: str = ""
    max_concurrent: int = 2
    rpm_limit: int = 0
    token_quota: int = 0

    # 运行时状态
    _semaphore: PrioritySemaphore | None = field(default=None, repr=False)
    _request_timestamps: list[float] = field(default_factory=list, repr=False)
    _tokens_used: int = 0
    _cooling_until: float = 0.0
    _consecutive_down: int = 0  # 连续 SERVICE_DOWN 次数（指数退避用）
    # 当前生效的 rpm（429 弹性降级/成功回升围绕 rpm_limit 波动）。
    # 初始 = rpm_limit；rpm_limit<=0（漏配/不限）时保持 0，rpm_remaining 兜底返回 9999。
    _rpm_effective: int = field(default=0, repr=False)

    def __post_init__(self) -> None:
        self._rpm_effective = self.rpm_limit

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
        """当前窗口内剩余可用请求数（按生效 rpm 计算）。"""
        if self._rpm_effective <= 0:
            # 漏配/不限 rpm 的兜底：放行，避免 key 因缺配置而死锁。
            return 9999
        now = _time.monotonic()
        self._evict_old(now)
        return max(0, self._rpm_effective - len(self._request_timestamps))

    @property
    def token_remaining(self) -> int:
        """剩余 token 配额。"""
        if self.token_quota <= 0:
            return 9999
        return max(0, self.token_quota - self._tokens_used)

    @property
    def is_exhausted(self) -> bool:
        """key 是否完全不可用（冷却中 or RPM 满 or 配额耗尽）。"""
        return (
            self.is_cooling
            or self.rpm_remaining <= 0
            or self.token_remaining <= 0
        )

    def score(self) -> float:
        """选 key 时的评分，越高越优先选。

        综合考虑：RPM 余量、Token 余量、是否冷却。
        """
        if self.is_cooling:
            return -1.0
        rpm_ratio = self.rpm_remaining / max(self.rpm_limit, 1)
        token_ratio = self.token_remaining / max(self.token_quota, 1)
        return rpm_ratio * 0.6 + token_ratio * 0.4

    def record_request(self) -> float:
        """记录一次请求，返回时间戳供调用方在请求取消时归还名额。

        返回值必须由调用方保存；若请求最终未真正打到上游（排队中被取消、
        重试等），调用 revoke_request(ts) 归还这个名额，避免虚占 rpm 窗口。
        """
        now = _time.monotonic()
        self._evict_old(now)
        self._request_timestamps.append(now)
        return now

    def revoke_request(self, ts: float) -> None:
        """归还一个 rpm 名额（移除 record_request 返回的时间戳）。

        用于请求未真正打上游（排队中被 cancel）时，撤销虚占的 rpm 名额。
        ts 不在列表中时静默忽略（可能已被 _evict_old 清除）。
        """
        try:
            self._request_timestamps.remove(ts)
        except ValueError:
            # 时间戳可能已被 60s 窗口滚动清除，无需处理
            pass

    def record_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        """记录一次请求的 token 消耗。"""
        self._tokens_used += prompt_tokens + completion_tokens

    def handle_error(self, info: "ErrorInfo") -> None:
        """按统一错误类型应用策略（取代旧的 on_rate_limit 万能方法）。

        策略表：
        - RATE_LIMIT: 冷却 + RPM 降级 1 级
        - QUOTA_EXHAUSTED: 长冷却 3600s
        - AUTH_FAILED: 长冷却 300s
        - SERVICE_DOWN: 连续次数计数，第 2 次起置递增短冷却（让 select() 暂时绕开），
          累计 3 次后 RPM 降级
        - SERVER_ERROR: 冷却 + 换 key
        - NETWORK: 仅换 key，不冷却（不是 key 的错）
        - BAD_REQUEST / UNKNOWN: 不处理（由调用方决定 raise）

        Args:
            info: error_classifier.classify_error 的返回值
        """
        from llm.error_classifier import ErrorKind  # noqa: PLC0415

        kind = info.kind
        retry_after = info.retry_after

        if kind == ErrorKind.RATE_LIMIT:
            cool = retry_after if retry_after and retry_after > 0 else 5.0
            self._cooling_until = _time.monotonic() + cool
            self._reduce_rpm()
            logger.info(
                "[KeySlot] %s RATE_LIMIT 冷却 %.1fs + RPM 降级", self.key_id, cool,
            )
        elif kind == ErrorKind.QUOTA_EXHAUSTED:
            self._cooling_until = _time.monotonic() + 3600.0
            logger.warning(
                "[KeySlot] %s QUOTA_EXHAUSTED 冷却 3600s", self.key_id,
            )
        elif kind == ErrorKind.AUTH_FAILED:
            self._cooling_until = _time.monotonic() + 300.0
            logger.warning(
                "[KeySlot] %s AUTH_FAILED 冷却 300s", self.key_id,
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
                    self.key_id, n, cool,
                )
            # 累计 3 次确认非偶发，RPM 降级（与 RATE_LIMIT 一致：少打不稳的上游）
            if n >= 3:
                self._reduce_rpm()
                logger.warning(
                    "[KeySlot] %s SERVICE_DOWN 连续 %d 次，RPM 降级",
                    self.key_id, n,
                )
            else:
                logger.info(
                    "[KeySlot] %s SERVICE_DOWN 第 %d 次（adapter 退避重试）",
                    self.key_id, n,
                )
        elif kind == ErrorKind.SERVER_ERROR:
            self._cooling_until = _time.monotonic() + 5.0
            logger.info("[KeySlot] %s SERVER_ERROR 冷却 5s", self.key_id)
        # NETWORK / BAD_REQUEST / UNKNOWN：不在此处理

    def _reduce_rpm(self) -> None:
        """限流降级：收紧生效 RPM 1 级（最低到 1），从源头减少打上游的频率。

        取代旧的 _reduce_concurrency（降 max_concurrent）。新限流策略以 RPM 为
        唯一主参数——429 是频率超限，应降频率（rpm）而非瞬时并发。
        rpm_limit<=0（漏配/不限）时不降级，避免把"不限"误降成有限。
        """
        if self._rpm_effective <= 1:
            return
        self._rpm_effective -= 1
        logger.info(
            "[KeySlot] %s RPM 降级 → %d (原 %d)",
            self.key_id, self._rpm_effective, self.rpm_limit,
        )

    def on_success(self) -> None:
        """成功调用：恢复连续失败计数 + RPM 回升 1 级（封顶到 rpm_limit）。"""
        self._consecutive_down = 0
        if self._rpm_effective < self.rpm_limit:
            self._rpm_effective += 1
            logger.info(
                "[KeySlot] %s RPM 回升 → %d", self.key_id, self._rpm_effective,
            )

    def _evict_old(self, now: float) -> None:
        """清除 60 秒前的请求时间戳。"""
        cutoff = now - 60.0
        self._request_timestamps = [
            t for t in self._request_timestamps if t > cutoff
        ]

    async def acquire(self) -> None:
        """获取并发许可。"""
        await self._get_semaphore().acquire()

    def release(self) -> None:
        """释放并发许可。"""
        self._get_semaphore().release()


class KeyPool:
    """多 key 池 — 从一组 KeySlot 中选最优 key。

    线程安全：每个 key 有独立的信号量，选 key 是无锁的纯计算。
    """

    def __init__(self, slots: list[KeySlot], pool_id: str = "") -> None:
        self.pool_id = pool_id
        self._slots = slots

    @property
    def slots(self) -> list[KeySlot]:
        return self._slots

    def select(self) -> KeySlot | None:
        """按 slots 声明顺序选第一个可用 key（主备模式）。

        优先级：slots 列表顺序即 key 优先级（由 llm.yaml 的 keys 段声明）。
        始终选第一个非耗尽的 key；只有它冷却/限流/配额耗尽时才回退到下一个。
        这样配置中的第一个 key 是主 key，其余为热备——主 key 恢复后自动切回。

        Returns:
            第一个可用 KeySlot；全部不可用返回 None。
        """
        available = [s for s in self._slots if not s.is_exhausted]
        if available:
            return available[0]

        # 所有 key 都 exhausted（冷却/RPM 满/配额耗尽）→ 返回 None。
        # acquire_slot 会据此走 sleep 分支：冷却中的等冷却到期，RPM 满的等
        # 窗口滚动（60s 内最早时间戳过期释放名额）。
        #
        # BUG-FIX-rpm_bypass_in_select:
        # 原实现有一段回退分支：所有 key exhausted 后，仍返回第一个"未冷却"的 key，
        # 意图是让 acquire_slot 去 await slot.acquire() 排队。但这是错的——
        # max_concurrent 排队依赖 acquire()，而 RPM 满时信号量通常有空位，
        # acquire 立即放行，RPM 限流被完全绕过（实测 rpm=2 时 5 个并发全放行）。
        # RPM 耗尽是频率维度的事，必须在 select 阶段挡下、由 sleep 等窗口滚动。
        # 删除该回退分支后，max_concurrent 排队不受影响（满载的 key 不被判
        # is_exhausted，正常在 available 分支被选中，请求在 acquire() 上排队）。
        logger.warning(
            "[KeyPool] %s 所有 key 均不可用 (cooling/exhausted)",
            self.pool_id,
        )
        return None

    async def acquire_slot(self, timeout: float = 60.0) -> KeySlot:
        """选 key 并获取并发许可，阻塞直到有 key 可用或超时。

        Args:
            timeout: 最大等待秒数。超时抛出 KeyPoolExhaustedError，
                     避免所有 key 不可用时无限等待导致 drain_loop 卡死。

        Raises:
            KeyPoolExhaustedError: 所有 key 不可用且等待超时。
        """
        deadline = _time.monotonic() + timeout
        while True:
            slot = self.select()
            if slot is not None:
                # record_request() 必须在 await acquire() 之前且中间无 await，
                # 防止并发请求同时通过 select() 的 rpm 检查（TOCTOU 竞态）。
                # 但若 acquire 排队期间被取消（未真正打上游），必须归还 rpm 名额，
                # 否则被 cancel 的请求虚占 60s 窗口导致正常请求被误判为 rpm 耗尽。
                req_ts = slot.record_request()
                try:
                    await slot.acquire()
                except asyncio.CancelledError:
                    slot.revoke_request(req_ts)
                    raise
                return slot
            if _time.monotonic() >= deadline:
                unavailable = self.get_unavailable_slots()
                logger.error(
                    "[KeyPool] %s 所有 key 不可用，等待 %.0fs 超时；诊断: %s",
                    self.pool_id, timeout, unavailable,
                )
                raise KeyPoolExhaustedError(self.pool_id, timeout, unavailable)
            # 所有 key 都满了，等最短冷却时间
            cool_slots = [s for s in self._slots if s.is_cooling]
            if cool_slots:
                earliest = min(s._cooling_until for s in cool_slots)
                wait = max(0.1, earliest - _time.monotonic())
                logger.debug(
                    "[KeyPool] %s 所有 key 忙，等待 %.1fs",
                    self.pool_id, wait,
                )
                await asyncio.sleep(wait)
            else:
                # 没有 cooling 的但都 exhausted，等一小段时间重试
                await asyncio.sleep(1.0)

    def get_unavailable_slots(self) -> list[str]:
        """返回当前不可用 key 的诊断信息（脱敏 key 前缀）。

        供 KeyPoolExhaustedError 携带，以及调用方在超时后程序化排查。
        暴露公共接口而非让调用方读 self._slots 私有属性。
        """
        return [
            f"{s.api_key[:8]}...(cooling={s.is_cooling}, "
            f"rpm_left={s.rpm_remaining}, token_left={s.token_remaining})"
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
