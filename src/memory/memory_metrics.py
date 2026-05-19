"""记忆系统监控指标模块。

为记忆系统提供轻量级、线程安全的运行指标收集能力，
包括检索延迟分位数、命中率百分比和存储容量追踪。

核心设计原则：
- 零外部依赖，仅使用 stdlib
- 线程安全（threading.Lock）
- 有界存储（deque maxlen），防止内存泄漏
- 可选集成：不影响核心记忆功能

公共接口：
- MemoryMetrics: 指标收集器门面类
- MemoryMetrics.record_retrieval(): 记录检索操作
- MemoryMetrics.record_storage_change(): 记录存储变更
- MemoryMetrics.get_metrics(): 获取所有指标快照
- MemoryMetrics.reset(): 重置所有指标

使用示例::

    metrics = MemoryMetrics()
    metrics.record_retrieval(latency_seconds=0.035, hit=True)
    metrics.record_storage_change(delta_entries=1, delta_bytes=256)
    snapshot = metrics.get_metrics()
    print(snapshot["retrieval_latency"]["p95"])
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Any


# ---------------------------------------------------------------------------
# 内部追踪器（以 _ 前缀标记，不对外暴露）
# ---------------------------------------------------------------------------


class _LatencyTracker:
    """检索延迟追踪器。

    使用有界 deque 存储延迟采样值，支持计算
    P50/P95/P99 分位数以及 avg/min/max/count。

    Attributes:
        _samples: 延迟采样值的有界队列
        _lock: 线程安全锁
    """

    def __init__(self, max_samples: int = 10000) -> None:
        """初始化延迟追踪器。

        Args:
            max_samples: 最大采样数量，超过后丢弃最旧的采样
        """
        self._samples: deque[float] = deque(maxlen=max_samples)
        self._lock = threading.Lock()

    def record(self, latency_seconds: float) -> None:
        """记录一次延迟采样。

        Args:
            latency_seconds: 本次检索操作的耗时（秒）
        """
        with self._lock:
            self._samples.append(latency_seconds)

    def get_snapshot(self) -> dict[str, Any]:
        """获取延迟统计快照。

        Returns:
            包含 p50/p95/p99/avg/min/max/count 的字典。
            无数据时所有统计值为 None，count 为 0。
        """
        with self._lock:
            samples = list(self._samples)

        count = len(samples)
        if count == 0:
            return {
                "p50": None,
                "p95": None,
                "p99": None,
                "avg": None,
                "min": None,
                "max": None,
                "count": 0,
            }

        sorted_samples = sorted(samples)
        return {
            "p50": self._percentile(sorted_samples, 50),
            "p95": self._percentile(sorted_samples, 95),
            "p99": self._percentile(sorted_samples, 99),
            "avg": sum(samples) / count,
            "min": sorted_samples[0],
            "max": sorted_samples[-1],
            "count": count,
        }

    def reset(self) -> None:
        """清空所有采样数据。"""
        with self._lock:
            self._samples.clear()

    @staticmethod
    def _percentile(sorted_data: list[float], percent: float) -> float:
        """计算已排序列表的百分位数。

        使用 "nearest rank" 方法，与多数监控系统一致。

        Args:
            sorted_data: 已排序的数据列表
            percent: 百分位（0-100）

        Returns:
            对应百分位的值
        """
        if not sorted_data:
            return 0.0
        # nearest rank: index = ceil(percent/100 * count) - 1
        count = len(sorted_data)
        index = max(0, min(count - 1, int(count * percent / 100 + 0.5) - 1))
        return sorted_data[index]


class _HitRateTracker:
    """命中率追踪器。

    线程安全地统计检索命中次数和总查询次数，
    计算命中率百分比。

    Attributes:
        _hits: 命中次数
        _total: 总查询次数
        _lock: 线程安全锁
    """

    def __init__(self) -> None:
        """初始化命中率追踪器。"""
        self._hits: int = 0
        self._total: int = 0
        self._lock = threading.Lock()

    def record(self, hit: bool) -> None:
        """记录一次检索的命中情况。

        Args:
            hit: 是否命中
        """
        with self._lock:
            self._total += 1
            if hit:
                self._hits += 1

    def get_snapshot(self) -> dict[str, Any]:
        """获取命中率统计快照。

        Returns:
            包含 rate(百分比)/hits/total 的字典。
            无查询时 rate 为 0.0。
        """
        with self._lock:
            hits = self._hits
            total = self._total

        rate = (hits / total * 100.0) if total > 0 else 0.0
        return {
            "rate": rate,
            "hits": hits,
            "total": total,
        }

    def reset(self) -> None:
        """清空所有统计数据。"""
        with self._lock:
            self._hits = 0
            self._total = 0


class _StorageTracker:
    """存储容量追踪器。

    线程安全地追踪记忆条目数和占用空间大小（字节）。
    值不会低于零，防止因统计误差导致负数。

    Attributes:
        _entry_count: 记忆条目数
        _total_bytes: 占用空间（字节）
        _lock: 线程安全锁
    """

    def __init__(self) -> None:
        """初始化存储容量追踪器。"""
        self._entry_count: int = 0
        self._total_bytes: int = 0
        self._lock = threading.Lock()

    def record_change(self, delta_entries: int, delta_bytes: int) -> None:
        """记录存储容量变更。

        Args:
            delta_entries: 条目数变更量（正数新增，负数删除）
            delta_bytes: 字节数变更量（正数新增，负数删除）
        """
        with self._lock:
            self._entry_count = max(0, self._entry_count + delta_entries)
            self._total_bytes = max(0, self._total_bytes + delta_bytes)

    def get_snapshot(self) -> dict[str, Any]:
        """获取存储容量快照。

        Returns:
            包含 entry_count/total_bytes 的字典。
        """
        with self._lock:
            return {
                "entry_count": self._entry_count,
                "total_bytes": self._total_bytes,
            }

    def reset(self) -> None:
        """清空所有统计数据。"""
        with self._lock:
            self._entry_count = 0
            self._total_bytes = 0


# ---------------------------------------------------------------------------
# 公共接口
# ---------------------------------------------------------------------------


class MemoryMetrics:
    """记忆系统监控指标收集器。

    门面类，统一管理三类指标的收集和暴露：
    1. 检索延迟：P50/P95/P99 分位数
    2. 命中率：命中次数 / 总查询次数 × 100%
    3. 存储容量：条目数和字节数

    使用示例::

        metrics = MemoryMetrics()
        metrics.record_retrieval(latency_seconds=0.035, hit=True)
        metrics.record_storage_change(delta_entries=1, delta_bytes=256)
        snapshot = metrics.get_metrics()

    Args:
        max_latency_samples: 延迟采样的最大存储数量，默认 10000
    """

    def __init__(self, *, max_latency_samples: int = 10000) -> None:
        """初始化记忆监控指标收集器。

        Args:
            max_latency_samples: 延迟采样的最大存储数量，超过后丢弃旧值
        """
        self._latency = _LatencyTracker(max_samples=max_latency_samples)
        self._hit_rate = _HitRateTracker()
        self._storage = _StorageTracker()

    def record_retrieval(
        self, *, latency_seconds: float, hit: bool
    ) -> None:
        """记录一次检索操作。

        同时更新检索延迟和命中率两个指标。

        Args:
            latency_seconds: 本次检索耗时（秒）
            hit: 是否检索命中
        """
        self._latency.record(latency_seconds)
        self._hit_rate.record(hit)

    def record_storage_change(
        self, *, delta_entries: int, delta_bytes: int
    ) -> None:
        """记录存储容量变更。

        Args:
            delta_entries: 条目数变更量（正数新增，负数删除）
            delta_bytes: 字节数变更量（正数新增，负数删除）
        """
        self._storage.record_change(delta_entries, delta_bytes)

    def get_metrics(self) -> dict[str, dict[str, Any]]:
        """获取所有指标的快照。

        返回的字典是独立副本，修改不影响内部状态。
        快照结构::

            {
                "retrieval_latency": {
                    "p50": float | None,
                    "p95": float | None,
                    "p99": float | None,
                    "avg": float | None,
                    "min": float | None,
                    "max": float | None,
                    "count": int,
                },
                "hit_rate": {
                    "rate": float,  # 百分比 0.0-100.0
                    "hits": int,
                    "total": int,
                },
                "storage": {
                    "entry_count": int,
                    "total_bytes": int,
                },
            }

        Returns:
            包含所有指标分区的字典，每个分区的值也是独立副本
        """
        return {
            "retrieval_latency": dict(self._latency.get_snapshot()),
            "hit_rate": dict(self._hit_rate.get_snapshot()),
            "storage": dict(self._storage.get_snapshot()),
        }

    def reset(self) -> None:
        """重置所有指标到初始状态。

        适用于测试环境或定期重置场景。
        """
        self._latency.reset()
        self._hit_rate.reset()
        self._storage.reset()
