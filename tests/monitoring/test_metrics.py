"""Prometheus 指标模块测试。

覆盖场景：
- 各指标定义存在且类型正确
- 指标可通过标签分类
- get_metrics() 返回 Prometheus 格式文本
- 指标值可递增/设置
"""

from __future__ import annotations

import re

from monitoring.metrics import (
    ACTIVE_SESSIONS,
    CHANNEL_STATUS,
    MESSAGE_PROCESSED,
    MESSAGE_RECEIVED,
    PROCESSING_TIME,
    get_metrics,
)


class TestMetricDefinitions:
    """指标定义测试。"""

    def test_message_received_exists(self) -> None:
        """MESSAGE_RECEIVED 指标存在。"""
        assert MESSAGE_RECEIVED is not None

    def test_message_processed_exists(self) -> None:
        """MESSAGE_PROCESSED 指标存在。"""
        assert MESSAGE_PROCESSED is not None

    def test_processing_time_exists(self) -> None:
        """PROCESSING_TIME 指标存在。"""
        assert PROCESSING_TIME is not None

    def test_active_sessions_exists(self) -> None:
        """ACTIVE_SESSIONS 指标存在。"""
        assert ACTIVE_SESSIONS is not None

    def test_channel_status_exists(self) -> None:
        """CHANNEL_STATUS 指标存在。"""
        assert CHANNEL_STATUS is not None


class TestMetricOperations:
    """指标操作测试。"""

    def test_message_received_inc(self) -> None:
        """MESSAGE_RECEIVED 可按通道递增。"""
        before = get_metrics()
        MESSAGE_RECEIVED.labels(channel="feishu").inc()
        after = get_metrics()
        assert len(after) > len(before) or after != before

    def test_message_processed_inc(self) -> None:
        """MESSAGE_PROCESSED 可按通道和状态递增。"""
        MESSAGE_PROCESSED.labels(channel="dingtalk", status="success").inc()
        text = get_metrics()
        assert isinstance(text, str)
        assert len(text) > 0

    def test_active_sessions_set(self) -> None:
        """ACTIVE_SESSIONS 可设置值。"""
        ACTIVE_SESSIONS.set(5)
        text = get_metrics()
        assert isinstance(text, str)

    def test_channel_status_set(self) -> None:
        """CHANNEL_STATUS 可按通道设置状态。"""
        CHANNEL_STATUS.labels(channel="feishu").set(1)
        CHANNEL_STATUS.labels(channel="wecom").set(0)
        text = get_metrics()
        assert isinstance(text, str)

    def test_processing_time_observe(self) -> None:
        """PROCESSING_TIME 可观测耗时。"""
        PROCESSING_TIME.labels(channel="qq").observe(0.15)
        text = get_metrics()
        assert isinstance(text, str)


class TestGetMetrics:
    """get_metrics 输出格式测试。"""

    def test_returns_string(self) -> None:
        """get_metrics 返回字符串。"""
        result = get_metrics()
        assert isinstance(result, str)

    def test_contains_help_comments(self) -> None:
        """输出包含 Prometheus HELP 注释。"""
        result = get_metrics()
        assert "# HELP" in result

    def test_contains_type_comments(self) -> None:
        """输出包含 Prometheus TYPE 注释。"""
        result = get_metrics()
        assert "# TYPE" in result

    def test_contains_metric_names(self) -> None:
        """输出包含所有指标名。"""
        result = get_metrics()
        # 至少应包含核心指标名（可能有前缀）
        assert "message_received" in result
        assert "message_processed" in result
        assert "processing_seconds" in result
        assert "active_sessions" in result
        assert "channel_status" in result

    def test_prometheus_format_line_pattern(self) -> None:
        """输出行符合 Prometheus 文本格式。"""
        result = get_metrics()
        lines = [l for l in result.strip().split("\n") if l and not l.startswith("#")]
        for line in lines:
            # 每行非注释行应为 metric_name{labels} value
            assert re.match(r'^[a-z_]+(\{[^}]*\})?\s+[\d\.e\+]+$', line), f"Invalid line: {line}"
