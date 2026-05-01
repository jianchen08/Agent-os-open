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
import logging
import time as _time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


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
    _semaphore: asyncio.Semaphore | None = field(default=None, repr=False)
    _request_timestamps: list[float] = field(default_factory=list, repr=False)
    _tokens_used: int = 0
    _cooling_until: float = 0.0
    _rpm_overflows: int = 0  # 本地计数被 429 校准的次数

    def _get_semaphore(self) -> asyncio.Semaphore:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.max_concurrent)
        return self._semaphore

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

    async def acquire_slot(self) -> KeySlot:
        """选 key 并获取并发许可，阻塞直到有 key 可用。"""
        while True:
            slot = self.select()
            if slot is not None:
                await slot.acquire()
                slot.record_request()
                return slot
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
