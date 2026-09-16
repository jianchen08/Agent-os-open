"""弹幕聚合窗（细化设计 §5）。

纪律：
- 滑动窗口（缺省 25s）内 ≥ min_distinct_users（缺省 3）个独立用户才产出意图；
- 每用户每分钟贡献 ≤1 次（超限丢弃计数）；
- 重复梗加权：同文本出现 N 次 = 权重 N（投票效应）；
- 意图产出：注入的 summarizer 端口（LLM 提炼）优先，缺省回落为
  加权 top 文本 + 代表弹幕（不依赖 LLM 也能出意图）。
- 时钟注入：feed/flush 由调用方传 now（单调秒），本模块零时钟依赖。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any, Protocol


class IntentSummarizer(Protocol):
    """意图提炼端口：弹幕列表 → 意图文本（LLM 或回落实现）。"""

    async def __call__(self, danmaku: list[str]) -> str: ...


@dataclass
class AggregateResult:
    """一次窗口冲刷的产出。"""

    valid: bool
    intent: str = ""
    representatives: list[str] = field(default_factory=list)
    distinct_users: int = 0
    dropped_by_throttle: int = 0


class DanmakuAggregator:
    """滑动窗口聚合器：feed 进弹幕，flush 到点产意图。"""

    def __init__(
        self,
        window_secs: float = 25.0,
        min_distinct_users: int = 3,
        per_user_interval_secs: float = 60.0,
        summarizer: IntentSummarizer | None = None,
    ) -> None:
        self._window = window_secs
        self._min_users = min_distinct_users
        self._per_user_interval = per_user_interval_secs
        self._summarizer = summarizer
        self._items: list[tuple[float, str, str]] = []  # (now, user, text)
        self._last_seen: dict[str, float] = {}
        self.dropped_by_throttle = 0

    def feed(self, now: float, user: str, text: str) -> None:
        """收一条弹幕：节流 + 入窗 + 惰性过期。"""
        self._evict(now)
        last = self._last_seen.get(user)
        if last is not None and now - last < self._per_user_interval:
            self.dropped_by_throttle += 1
            return
        self._last_seen[user] = now
        self._items.append((now, user, text.strip()))

    def _evict(self, now: float) -> None:
        cutoff = now - self._window
        self._items = [item for item in self._items if item[0] >= cutoff]

    async def flush(self, now: float) -> AggregateResult:
        """窗口冲刷：清空窗内弹幕并产出意图（不足独立用户数 → 无效）。"""
        self._evict(now)
        items, self._items = self._items, []
        distinct = {user for _, user, _ in items}
        if len(distinct) < self._min_users:
            return AggregateResult(valid=False, distinct_users=len(distinct))
        texts = [text for _, _, text in items]
        weighted = Counter(texts)
        top_text, _top_weight = weighted.most_common(1)[0]
        representatives = [text for _, _, text in items[:3]]
        if self._summarizer is not None:
            intent = await self._summarizer(texts)
        else:
            intent = top_text
        return AggregateResult(
            valid=bool(intent.strip()),
            intent=intent.strip(),
            representatives=representatives,
            distinct_users=len(distinct),
            dropped_by_throttle=self.dropped_by_throttle,
        )

    def status(self) -> dict[str, Any]:
        """面板/台账观测。"""
        return {"window_items": len(self._items), "dropped_by_throttle": self.dropped_by_throttle}
