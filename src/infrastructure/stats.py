"""统计信息收集器 — 合并 scheduler + concurrency 的统计。

精简原则：
- 用简单的 dict 存储，不做分类聚合
- 提供 record/increment/get/snapshot 四个操作
- 不做持久化，纯内存
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class StatsCollector:
    """统计信息收集器。

    合并调度器和并发控制器的统计信息，
    提供简单的记录、递增、查询和快照能力。

    Attributes:
        _stats: 统计项键值对
    """

    _stats: dict[str, Any] = field(default_factory=dict)

    def record(self, key: str, value: Any) -> None:
        """记录统计项。

        Args:
            key: 统计项键名
            value: 统计项值
        """
        self._stats[key] = value

    def increment(self, key: str, delta: int = 1) -> None:
        """递增统计项。

        若统计项不存在，从 0 开始递增。

        Args:
            key: 统计项键名
            delta: 递增量，默认 1
        """
        self._stats[key] = self._stats.get(key, 0) + delta

    def get(self, key: str, default: Any = None) -> Any:
        """获取统计项。

        Args:
            key: 统计项键名
            default: 键不存在时的默认值

        Returns:
            统计项值或默认值
        """
        return self._stats.get(key, default)

    def snapshot(self) -> dict[str, Any]:
        """获取统计快照。

        Returns:
            统计项的浅拷贝字典
        """
        return dict(self._stats)
