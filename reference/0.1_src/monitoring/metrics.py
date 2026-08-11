"""Prometheus 指标模块。

提供消息网关核心指标的 Prometheus 格式采集和输出。

指标说明：
- MESSAGE_RECEIVED: Counter，按通道分类统计接收消息数
- MESSAGE_PROCESSED: Counter，按通道+状态分类统计处理结果
- PROCESSING_TIME: Histogram，消息处理耗时分布
- ACTIVE_SESSIONS: Gauge，当前活跃会话数
- CHANNEL_STATUS: Gauge，通道连接状态（1=正常，0=异常）
"""

from __future__ import annotations

import threading
from typing import Any

# ---------------------------------------------------------------------------
# 简易 Prometheus 指标实现（不依赖 prometheus_client）
# ---------------------------------------------------------------------------


class _SimpleCounter:
    """简易 Counter 指标。

    支持按 labels 分类的计数器。

    Example::

        c = _SimpleCounter("messages_total", "Total messages")
        c.labels(channel="feishu").inc()
    """

    def __init__(self, name: str, help_text: str, label_names: tuple[str, ...] = ()) -> None:
        """初始化 Counter。

        Args:
            name: 指标名称
            help_text: 帮助文本
            label_names: 标签名列表
        """
        self.name = name
        self.help_text = help_text
        self.label_names = label_names
        self._values: dict[tuple[str, ...], float] = {}
        self._lock = threading.Lock()

    def labels(self, **kwargs: str) -> _LabeledCounter:
        """获取带标签的计数器实例。

        Args:
            **kwargs: 标签键值对

        Returns:
            带标签的计数器操作对象
        """
        key = tuple(kwargs.get(k, "") for k in self.label_names)
        return _LabeledCounter(self, key)

    def _inc(self, key: tuple[str, ...], value: float = 1.0) -> None:
        """递增指定标签组合的值。"""
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + value

    def collect(self) -> list[str]:
        """生成 Prometheus 文本格式行。

        Returns:
            Prometheus 格式的字符串列表
        """
        lines: list[str] = []
        lines.append(f"# HELP {self.name} {self.help_text}")
        lines.append(f"# TYPE {self.name} counter")
        with self._lock:
            for key, value in sorted(self._values.items()):
                label_str = self._format_labels(key)
                suffix = f"{{{label_str}}}" if label_str else ""
                lines.append(f"{self.name}{suffix} {value}")
        return lines

    def _format_labels(self, key: tuple[str, ...]) -> str:
        """格式化标签为 Prometheus 标签字符串。"""
        pairs = []
        for i, label_name in enumerate(self.label_names):
            if i < len(key):
                pairs.append(f'{label_name}="{key[i]}"')
        return ", ".join(pairs)


class _LabeledCounter:
    """带标签的 Counter 操作代理。"""

    def __init__(self, counter: _SimpleCounter, key: tuple[str, ...]) -> None:
        self._counter = counter
        self._key = key

    def inc(self, value: float = 1.0) -> None:
        """递增计数。

        Args:
            value: 递增量，默认 1.0
        """
        self._counter._inc(self._key, value)


class _SimpleGauge:
    """简易 Gauge 指标。

    支持按 labels 分类的仪表盘。

    Example::

        g = _SimpleGauge("active_sessions", "Active sessions")
        g.set(5)
        g.labels(channel="feishu").set(1)
    """

    def __init__(self, name: str, help_text: str, label_names: tuple[str, ...] = ()) -> None:
        """初始化 Gauge。

        Args:
            name: 指标名称
            help_text: 帮助文本
            label_names: 标签名列表
        """
        self.name = name
        self.help_text = help_text
        self.label_names = label_names
        self._values: dict[tuple[str, ...], float] = {}
        self._lock = threading.Lock()

    def labels(self, **kwargs: str) -> _LabeledGauge:
        """获取带标签的仪表盘实例。"""
        key = tuple(kwargs.get(k, "") for k in self.label_names)
        return _LabeledGauge(self, key)

    def set(self, value: float) -> None:
        """设置无标签值。"""
        with self._lock:
            self._values[()] = value

    def _set(self, key: tuple[str, ...], value: float) -> None:
        """设置指定标签组合的值。"""
        with self._lock:
            self._values[key] = value

    def collect(self) -> list[str]:
        """生成 Prometheus 文本格式行。"""
        lines: list[str] = []
        lines.append(f"# HELP {self.name} {self.help_text}")
        lines.append(f"# TYPE {self.name} gauge")
        with self._lock:
            for key, value in sorted(self._values.items()):
                label_str = self._format_labels(key)
                suffix = f"{{{label_str}}}" if label_str else ""
                lines.append(f"{self.name}{suffix} {value}")
        return lines

    def _format_labels(self, key: tuple[str, ...]) -> str:
        """格式化标签。"""
        pairs = []
        for i, label_name in enumerate(self.label_names):
            if i < len(key):
                pairs.append(f'{label_name}="{key[i]}"')
        return ", ".join(pairs)


class _LabeledGauge:
    """带标签的 Gauge 操作代理。"""

    def __init__(self, gauge: _SimpleGauge, key: tuple[str, ...]) -> None:
        self._gauge = gauge
        self._key = key

    def set(self, value: float) -> None:
        """设置值。"""
        self._gauge._set(self._key, value)


class _SimpleHistogram:
    """简易 Histogram 指标。

    提供分桶计数和总和/计数统计。

    Example::

        h = _SimpleHistogram("processing_seconds", "Processing time")
        h.labels(channel="feishu").observe(0.15)
    """

    # 默认桶边界（秒）
    DEFAULT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

    def __init__(
        self,
        name: str,
        help_text: str,
        label_names: tuple[str, ...] = (),
        buckets: tuple[float, ...] | None = None,
    ) -> None:
        """初始化 Histogram。

        Args:
            name: 指标名称
            help_text: 帮助文本
            label_names: 标签名列表
            buckets: 桶边界列表
        """
        self.name = name
        self.help_text = help_text
        self.label_names = label_names
        self._buckets = buckets or self.DEFAULT_BUCKETS
        self._data: dict[tuple[str, ...], dict[str, Any]] = {}
        self._lock = threading.Lock()

    def labels(self, **kwargs: str) -> _LabeledHistogram:
        """获取带标签的直方图实例。"""
        key = tuple(kwargs.get(k, "") for k in self.label_names)
        return _LabeledHistogram(self, key)

    def _observe(self, key: tuple[str, ...], value: float) -> None:
        """记录观测值。"""
        with self._lock:
            if key not in self._data:
                bucket_counts = {f"le={b}": 0 for b in self._buckets}
                bucket_counts["le=+Inf"] = 0
                self._data[key] = {"buckets": bucket_counts, "sum": 0.0, "count": 0}
            data = self._data[key]
            data["sum"] += value
            data["count"] += 1
            for b in self._buckets:
                if value <= b:
                    data["buckets"][f"le={b}"] += 1
            data["buckets"]["le=+Inf"] += 1

    def collect(self) -> list[str]:
        """生成 Prometheus 文本格式行。"""
        lines: list[str] = []
        lines.append(f"# HELP {self.name} {self.help_text}")
        lines.append(f"# TYPE {self.name} histogram")
        label_prefix = self.name
        with self._lock:
            for key, data in sorted(self._data.items()):
                label_str = self._format_labels(key)
                base = f",{label_str}" if label_str else ""
                for bucket_key, count in data["buckets"].items():
                    le_val = bucket_key.split("=")[1]
                    lines.append(f'{label_prefix}_bucket{{le="{le_val}"{base}}} {count}')
                suffix = f"{{{label_str}}}" if label_str else ""
                lines.append(f"{label_prefix}_sum{suffix} {data['sum']}")
                lines.append(f"{label_prefix}_count{suffix} {data['count']}")
        # 即使没有数据也输出 _bucket +Inf 行
        if not self._data:
            lines.append(f'{label_prefix}_bucket{{le="+Inf"}} 0')
            lines.append(f"{label_prefix}_sum 0")
            lines.append(f"{label_prefix}_count 0")
        return lines

    def _format_labels(self, key: tuple[str, ...]) -> str:
        """格式化标签。"""
        pairs = []
        for i, label_name in enumerate(self.label_names):
            if i < len(key):
                pairs.append(f'{label_name}="{key[i]}"')
        return ", ".join(pairs)


class _LabeledHistogram:
    """带标签的 Histogram 操作代理。"""

    def __init__(self, histogram: _SimpleHistogram, key: tuple[str, ...]) -> None:
        self._histogram = histogram
        self._key = key

    def observe(self, value: float) -> None:
        """记录一个观测值。

        Args:
            value: 观测值（秒）
        """
        self._histogram._observe(self._key, value)


# ---------------------------------------------------------------------------
# 全局指标实例
# ---------------------------------------------------------------------------

MESSAGE_RECEIVED = _SimpleCounter(
    name="message_received_total",
    help_text="Total number of messages received by channel",
    label_names=("channel",),
)

MESSAGE_PROCESSED = _SimpleCounter(
    name="message_processed_total",
    help_text="Total number of messages processed by channel and status",
    label_names=("channel", "status"),
)

PROCESSING_TIME = _SimpleHistogram(
    name="processing_seconds",
    help_text="Message processing time in seconds",
    label_names=("channel",),
)

ACTIVE_SESSIONS = _SimpleGauge(
    name="active_sessions",
    help_text="Current number of active sessions",
)

CHANNEL_STATUS = _SimpleGauge(
    name="channel_status",
    help_text="Channel connection status (1=connected, 0=disconnected)",
    label_names=("channel",),
)

# 所有指标注册表
_METRICS_REGISTRY: list[_SimpleCounter | _SimpleGauge | _SimpleHistogram] = [
    MESSAGE_RECEIVED,
    MESSAGE_PROCESSED,
    PROCESSING_TIME,
    ACTIVE_SESSIONS,
    CHANNEL_STATUS,
]


def get_metrics() -> str:
    """生成 Prometheus 格式的指标文本。

    遍历所有已注册指标，输出标准 Prometheus exposition format。

    Returns:
        Prometheus 文本格式的指标字符串
    """
    lines: list[str] = []
    for metric in _METRICS_REGISTRY:
        lines.extend(metric.collect())
        lines.append("")  # 指标间空行
    return "\n".join(lines).strip() + "\n"
