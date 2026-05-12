"""
WebSocket 性能监控模块

提供 WebSocket 连接和消息传输的性能监控功能
"""

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

try:
    from prometheus_client import Counter, Gauge, Histogram, Info

    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False

    # 创建空的占位符类
    class Counter:
        def __init__(self, *args, **kwargs):
            pass

        def inc(self, *args, **kwargs):
            pass

    class Histogram:
        def __init__(self, *args, **kwargs):
            pass

        def observe(self, *args, **kwargs):
            pass

    class Gauge:
        def __init__(self, *args, **kwargs):
            pass

        def set(self, *args, **kwargs):
            pass

        def inc(self, *args, **kwargs):
            pass

        def dec(self, *args, **kwargs):
            pass

    class Info:
        def __init__(self, *args, **kwargs):
            pass

        def info(self, *args, **kwargs):
            pass


import logging

logger = logging.getLogger(__name__)


class MetricType(str, Enum):
    """指标类型"""

    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"
    INFO = "info"


@dataclass
class ConnectionMetrics:
    """连接指标"""

    connection_id: str
    thread_id: str
    user_id: str
    connect_time: datetime
    disconnect_time: datetime | None = None

    # 消息统计
    messages_sent: int = 0
    messages_received: int = 0
    bytes_sent: int = 0
    bytes_received: int = 0

    # 延迟统计
    latencies: deque = field(default_factory=lambda: deque(maxlen=100))

    # 错误统计
    errors: list[dict[str, Any]] = field(default_factory=list)

    @property
    def duration(self) -> timedelta | None:
        """连接持续时间"""
        end_time = self.disconnect_time or datetime.now()
        return end_time - self.connect_time

    @property
    def avg_latency(self) -> float:
        """平均延迟（毫秒）"""
        if not self.latencies:
            return 0.0
        return sum(self.latencies) / len(self.latencies)

    @property
    def p99_latency(self) -> float:
        """P99延迟（毫秒）"""
        if not self.latencies:
            return 0.0
        sorted_latencies = sorted(self.latencies)
        index = int(len(sorted_latencies) * 0.99)
        return sorted_latencies[min(index, len(sorted_latencies) - 1)]


class WebSocketMetrics:
    """
    WebSocket 性能指标收集器

    收集和管理 WebSocket 连接的各种性能指标
    """

    def __init__(self, enable_prometheus: bool = True):
        """
        初始化指标收集器

        Args:
            enable_prometheus: 是否启用 Prometheus 指标导出
        """
        self.enable_prometheus = enable_prometheus and PROMETHEUS_AVAILABLE

        # 连接指标存储
        self._connection_metrics: dict[str, ConnectionMetrics] = {}
        self._historical_metrics: list[ConnectionMetrics] = []

        # 全局统计
        self._start_time = datetime.now()
        self._total_connections = 0
        self._active_connections = 0
        self._total_messages = 0
        self._total_bytes = 0
        self._error_count = 0

        # 按线程统计
        self._thread_stats: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"connections": 0, "messages": 0, "bytes": 0, "errors": 0}
        )

        # 按用户统计
        self._user_stats: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"connections": 0, "messages": 0, "bytes": 0, "errors": 0}
        )

        # 初始化 Prometheus 指标
        if self.enable_prometheus:
            self._init_prometheus_metrics()

    def _init_prometheus_metrics(self):
        """初始化 Prometheus 指标"""
        try:
            # 连接指标
            self.ws_connections_total = Gauge(
                "websocket_connections_total", "Total active WebSocket connections"
            )

            self.ws_connections_created = Counter(
                "websocket_connections_created_total",
                "Total WebSocket connections created",
                ["thread_id", "user_id"],
            )

            self.ws_connection_duration = Histogram(
                "websocket_connection_duration_seconds",
                "WebSocket connection duration in seconds",
                buckets=[1, 5, 10, 30, 60, 300, 600, 1800, 3600],
            )

            # 消息指标
            self.ws_messages_sent = Counter(
                "websocket_messages_sent_total",
                "Total WebSocket messages sent",
                ["thread_id", "message_type"],
            )

            self.ws_messages_received = Counter(
                "websocket_messages_received_total",
                "Total WebSocket messages received",
                ["thread_id", "message_type"],
            )

            self.ws_message_size = Histogram(
                "websocket_message_size_bytes",
                "WebSocket message size in bytes",
                ["direction"],  # 'sent' or 'received'
                buckets=[100, 500, 1000, 5000, 10000, 50000, 100000],
            )

            # 延迟指标
            self.ws_message_latency = Histogram(
                "websocket_message_latency_seconds",
                "WebSocket message latency in seconds",
                buckets=[0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0],
            )

            # 错误指标
            self.ws_errors_total = Counter(
                "websocket_errors_total",
                "Total WebSocket errors",
                ["error_type", "thread_id"],
            )

            # 系统信息
            self.ws_info = Info("websocket_info", "WebSocket service information")
            self.ws_info.info(
                {"version": "1.0.0", "start_time": self._start_time.isoformat()}
            )

            logger.info("[WebSocketMetrics] Prometheus 指标初始化完成")

        except Exception as e:
            logger.error(f"[WebSocketMetrics] Prometheus 指标初始化失败: {e}")
            self.enable_prometheus = False

    def record_connection_start(
        self, connection_id: str, thread_id: str, user_id: str
    ) -> None:
        """
        记录连接开始

        Args:
            connection_id: 连接 ID
            thread_id: 线程 ID
            user_id: 用户 ID
        """
        metrics = ConnectionMetrics(
            connection_id=connection_id,
            thread_id=thread_id,
            user_id=user_id,
            connect_time=datetime.now(),
        )

        self._connection_metrics[connection_id] = metrics

        # 更新全局统计
        self._total_connections += 1
        self._active_connections += 1

        # 更新线程统计
        self._thread_stats[thread_id]["connections"] += 1

        # 更新用户统计
        self._user_stats[user_id]["connections"] += 1

        # 更新 Prometheus 指标
        if self.enable_prometheus:
            self.ws_connections_total.set(self._active_connections)
            self.ws_connections_created.labels(
                thread_id=thread_id, user_id=user_id
            ).inc()

        logger.debug(
            f"[WebSocketMetrics] 记录连接开始: {connection_id}, "
            f"线程: {thread_id}, 用户: {user_id}"
        )

    def record_connection_end(self, connection_id: str) -> None:
        """
        记录连接结束

        Args:
            connection_id: 连接 ID
        """
        if connection_id not in self._connection_metrics:
            logger.warning(f"[WebSocketMetrics] 连接 {connection_id} 不存在")
            return

        metrics = self._connection_metrics[connection_id]
        metrics.disconnect_time = datetime.now()

        # 移动到历史记录
        self._historical_metrics.append(metrics)
        del self._connection_metrics[connection_id]

        # 更新全局统计
        self._active_connections -= 1

        # 更新 Prometheus 指标
        if self.enable_prometheus:
            self.ws_connections_total.set(self._active_connections)
            if metrics.duration:
                self.ws_connection_duration.observe(metrics.duration.total_seconds())

        logger.debug(
            f"[WebSocketMetrics] 记录连接结束: {connection_id}, "
            f"持续时间: {metrics.duration}"
        )

    def record_message_sent(
        self,
        connection_id: str,
        message_type: str,
        message_size: int,
        latency_ms: float | None = None,
    ) -> None:
        """
        记录消息发送

        Args:
            connection_id: 连接 ID
            message_type: 消息类型
            message_size: 消息大小（字节）
            latency_ms: 消息延迟（毫秒）
        """
        if connection_id in self._connection_metrics:
            metrics = self._connection_metrics[connection_id]
            metrics.messages_sent += 1
            metrics.bytes_sent += message_size

            if latency_ms is not None:
                metrics.latencies.append(latency_ms)

            # 更新线程统计
            self._thread_stats[metrics.thread_id]["messages"] += 1
            self._thread_stats[metrics.thread_id]["bytes"] += message_size

            # 更新用户统计
            self._user_stats[metrics.user_id]["messages"] += 1
            self._user_stats[metrics.user_id]["bytes"] += message_size

        # 更新全局统计
        self._total_messages += 1
        self._total_bytes += message_size

        # 更新 Prometheus 指标
        if self.enable_prometheus:
            thread_id = (
                self._connection_metrics.get(connection_id, {}).thread_id or "unknown"
            )
            self.ws_messages_sent.labels(
                thread_id=thread_id, message_type=message_type
            ).inc()
            self.ws_message_size.labels(direction="sent").observe(message_size)

            if latency_ms is not None:
                self.ws_message_latency.observe(latency_ms / 1000.0)

    def record_message_received(
        self, connection_id: str, message_type: str, message_size: int
    ) -> None:
        """
        记录消息接收

        Args:
            connection_id: 连接 ID
            message_type: 消息类型
            message_size: 消息大小（字节）
        """
        if connection_id in self._connection_metrics:
            metrics = self._connection_metrics[connection_id]
            metrics.messages_received += 1
            metrics.bytes_received += message_size

        # 更新 Prometheus 指标
        if self.enable_prometheus:
            thread_id = (
                self._connection_metrics.get(connection_id, {}).thread_id or "unknown"
            )
            self.ws_messages_received.labels(
                thread_id=thread_id, message_type=message_type
            ).inc()
            self.ws_message_size.labels(direction="received").observe(message_size)

    def record_error(
        self,
        connection_id: str,
        error_type: str,
        error_message: str,
        context: dict[str, Any] | None = None,
    ) -> None:
        """
        记录错误

        Args:
            connection_id: 连接 ID
            error_type: 错误类型
            error_message: 错误消息
            context: 错误上下文
        """
        error_info = {
            "type": error_type,
            "message": error_message,
            "timestamp": datetime.now(),
            "context": context or {},
        }

        if connection_id in self._connection_metrics:
            metrics = self._connection_metrics[connection_id]
            metrics.errors.append(error_info)

            # 更新线程统计
            self._thread_stats[metrics.thread_id]["errors"] += 1

            # 更新用户统计
            self._user_stats[metrics.user_id]["errors"] += 1

        # 更新全局统计
        self._error_count += 1

        # 更新 Prometheus 指标
        if self.enable_prometheus:
            thread_id = (
                self._connection_metrics.get(connection_id, {}).thread_id or "unknown"
            )
            self.ws_errors_total.labels(
                error_type=error_type, thread_id=thread_id
            ).inc()

        logger.warning(
            f"[WebSocketMetrics] 记录错误: {connection_id}, "
            f"类型: {error_type}, 消息: {error_message}"
        )

    def get_global_stats(self) -> dict[str, Any]:
        """获取全局统计信息"""
        uptime = datetime.now() - self._start_time

        # 计算平均延迟
        all_latencies = []
        for metrics in self._connection_metrics.values():
            all_latencies.extend(metrics.latencies)

        avg_latency = sum(all_latencies) / len(all_latencies) if all_latencies else 0
        p99_latency = 0
        if all_latencies:
            sorted_latencies = sorted(all_latencies)
            index = int(len(sorted_latencies) * 0.99)
            p99_latency = sorted_latencies[min(index, len(sorted_latencies) - 1)]

        return {
            "uptime_seconds": uptime.total_seconds(),
            "total_connections": self._total_connections,
            "active_connections": self._active_connections,
            "total_messages": self._total_messages,
            "total_bytes": self._total_bytes,
            "error_count": self._error_count,
            "avg_latency_ms": avg_latency,
            "p99_latency_ms": p99_latency,
            "threads_count": len(self._thread_stats),
            "users_count": len(self._user_stats),
        }

    def get_thread_stats(self, thread_id: str) -> dict[str, Any]:
        """获取线程统计信息"""
        return dict(self._thread_stats.get(thread_id, {}))

    def get_user_stats(self, user_id: str) -> dict[str, Any]:
        """获取用户统计信息"""
        return dict(self._user_stats.get(user_id, {}))

    def get_connection_stats(self, connection_id: str) -> dict[str, Any] | None:
        """获取连接统计信息"""
        if connection_id not in self._connection_metrics:
            return None

        metrics = self._connection_metrics[connection_id]
        return {
            "connection_id": metrics.connection_id,
            "thread_id": metrics.thread_id,
            "user_id": metrics.user_id,
            "connect_time": metrics.connect_time.isoformat(),
            "duration_seconds": (
                metrics.duration.total_seconds() if metrics.duration else None
            ),
            "messages_sent": metrics.messages_sent,
            "messages_received": metrics.messages_received,
            "bytes_sent": metrics.bytes_sent,
            "bytes_received": metrics.bytes_received,
            "avg_latency_ms": metrics.avg_latency,
            "p99_latency_ms": metrics.p99_latency,
            "error_count": len(metrics.errors),
        }

    def get_top_threads(self, limit: int = 10) -> list[dict[str, Any]]:
        """获取消息量最多的线程"""
        sorted_threads = sorted(
            self._thread_stats.items(), key=lambda x: x[1]["messages"], reverse=True
        )

        return [
            {"thread_id": thread_id, **stats}
            for thread_id, stats in sorted_threads[:limit]
        ]

    def get_top_users(self, limit: int = 10) -> list[dict[str, Any]]:
        """获取消息量最多的用户"""
        sorted_users = sorted(
            self._user_stats.items(), key=lambda x: x[1]["messages"], reverse=True
        )

        return [
            {"user_id": user_id, **stats} for user_id, stats in sorted_users[:limit]
        ]

    def export_metrics(self) -> dict[str, Any]:
        """导出所有指标"""
        return {
            "global_stats": self.get_global_stats(),
            "active_connections": [
                self.get_connection_stats(conn_id)
                for conn_id in self._connection_metrics.keys()
            ],
            "top_threads": self.get_top_threads(),
            "top_users": self.get_top_users(),
            "prometheus_enabled": self.enable_prometheus,
        }

    def reset_stats(self) -> None:
        """重置统计信息"""
        self._connection_metrics.clear()
        self._historical_metrics.clear()
        self._thread_stats.clear()
        self._user_stats.clear()

        self._start_time = datetime.now()
        self._total_connections = 0
        self._active_connections = 0
        self._total_messages = 0
        self._total_bytes = 0
        self._error_count = 0

        logger.info("[WebSocketMetrics] 统计信息已重置")


# 全局指标实例
_global_metrics: WebSocketMetrics | None = None


def get_websocket_metrics() -> WebSocketMetrics:
    """获取全局 WebSocket 指标实例"""
    global _global_metrics
    if _global_metrics is None:
        _global_metrics = WebSocketMetrics()
    return _global_metrics


def init_websocket_metrics(enable_prometheus: bool = True) -> WebSocketMetrics:
    """
    初始化全局 WebSocket 指标实例

    Args:
        enable_prometheus: 是否启用 Prometheus 指标导出

    Returns:
        WebSocket 指标实例
    """
    global _global_metrics
    _global_metrics = WebSocketMetrics(enable_prometheus=enable_prometheus)
    return _global_metrics
