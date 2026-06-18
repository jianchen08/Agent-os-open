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
    """优先级信号量 — 高优先级请求优先获取许可。"""

    def __init__(self, value: int = 1) -> None:
        self._value = value
        self._waiters: list[tuple[int, asyncio.Future]] = []

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

        await fut

    def release(self) -> None:
        """释放一个许可。唤醒最高优先级的等待者。"""
        if self._waiters:
            w_priority, fut = self._waiters.pop(0)
            if not fut.done():
                fut.set_result(None)
                logger.info(
                    "[PrioritySemaphore] 唤醒等待者 | level=%s priority=%d | remaining_waiters=%d",
                    _priority_label(w_priority), w_priority, len(self._waiters),
                )
        else:
            self._value += 1


@dataclass
class KeySlot:
    """单个 API key 的状态追踪。

    Attributes:
        key_id: 标识符（如 zhipu_key_1）
        api_key: 实际的 API key 字符串
        api_base: 可选的 API base URL
        max_concurrent: 最大并发数（信号量容量）
        rpm_limit: 每分钟请求数上限（0 表示不限）
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
    _rpm_overflows: int = 0  # 本地计数被 429 校准的次数

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
        """当前窗口内剩余可用请求数。"""
        if self.rpm_limit <= 0:
            return 9999
        now = _time.monotonic()
        self._evict_old(now)
        return max(0, self.rpm_limit - len(self._request_timestamps))

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

    def record_request(self) -> None:
        """记录一次请求。"""
        now = _time.monotonic()
        self._evict_old(now)
        self._request_timestamps.append(now)

    def record_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        """记录一次请求的 token 消耗。"""
        self._tokens_used += prompt_tokens + completion_tokens

    def on_rate_limit(self, retry_after: float | None = None) -> None:
        """收到 429 响应：冷却 + 校准本地计数。"""
        cool_seconds = retry_after if retry_after and retry_after > 0 else 5.0
        self._cooling_until = _time.monotonic() + cool_seconds
        # 校准：本地计数偏少，追加溢出计数
        self._rpm_overflows += 1
        if self._rpm_overflows > 3 and self.rpm_limit > 0:
            # 多次被 429，说明本地限流太松，缩紧有效 RPM
            logger.warning(
                "[KeySlot] %s 被 429 多次 (%d)，可能本地计数不准",
                self.key_id, self._rpm_overflows,
            )
        logger.info(
            "[KeySlot] %s 收到 429，冷却 %.1fs",
            self.key_id, cool_seconds,
        )

    def on_success(self) -> None:
        """成功时重置溢出计数。"""
        self._rpm_overflows = max(0, self._rpm_overflows - 1)

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
        """选最优可用 key。

        优先级：
        1. 排除冷却中 / RPM 满 / 配额耗尽的
        2. 在剩余 key 中选 score 最高的
        """
        candidates = [s for s in self._slots if not s.is_exhausted]
        if not candidates:
            # 全满了，看有没有只是 RPM 满但没冷却的（最短时间内可用的）
            rpm_blocked = [s for s in self._slots if not s.is_cooling]
            if rpm_blocked:
                candidates = rpm_blocked
            else:
                logger.warning(
                    "[KeyPool] %s 所有 key 均不可用 (cooling/exhausted)",
                    self.pool_id,
                )
                return None
        return max(candidates, key=lambda s: s.score())

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
                await slot.acquire()
                slot.record_request()
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
