"""基础设施集成测试。

测试 ResourceManager + ErrorPolicy + StatsCollector 协同工作。
"""

from __future__ import annotations

import asyncio

import pytest

from infrastructure.error_policy import apply_error_policy
from infrastructure.resource import ResourceManager, ResourceQuota
from infrastructure.stats import StatsCollector
from pipeline.types import ErrorPolicy


class TestErrorPolicyIntegration:
    """ErrorPolicy 框架级处理测试。"""

    def test_abort_policy(self) -> None:
        """ABORT 策略应设置 skip_remaining=True。"""
        error = RuntimeError("test error")
        result = apply_error_policy(ErrorPolicy.ABORT, error, "test_plugin")
        assert result.skip_remaining is True
        assert result.error is error

    def test_skip_policy(self) -> None:
        """SKIP 策略不应跳过剩余插件。"""
        error = RuntimeError("test error")
        result = apply_error_policy(ErrorPolicy.SKIP, error, "test_plugin")
        assert result.skip_remaining is False
        assert result.error is error

    def test_fallback_policy(self) -> None:
        """FALLBACK 策略应使用 fallback_state。"""
        error = RuntimeError("test error")
        fallback = {"result": "default_value"}
        result = apply_error_policy(
            ErrorPolicy.FALLBACK, error, "test_plugin", fallback_state=fallback
        )
        assert result.state_updates == fallback
        assert result.error is error

    def test_retry_policy_exhausted(self) -> None:
        """RETRY 策略（重试耗尽后）应等同于 ABORT。"""
        error = RuntimeError("test error")
        result = apply_error_policy(ErrorPolicy.RETRY, error, "test_plugin")
        assert result.skip_remaining is True
        assert result.error is error


class TestStatsCollector:
    """统计信息收集器测试。"""

    def test_record_and_get(self) -> None:
        """record 和 get 应正确存取。"""
        stats = StatsCollector()
        stats.record("key1", 42)
        assert stats.get("key1") == 42

    def test_increment(self) -> None:
        """increment 应正确递增。"""
        stats = StatsCollector()
        stats.increment("count")
        stats.increment("count")
        stats.increment("count", delta=5)
        assert stats.get("count") == 7

    def test_get_default(self) -> None:
        """不存在的键应返回默认值。"""
        stats = StatsCollector()
        assert stats.get("missing") is None
        assert stats.get("missing", 0) == 0

    def test_snapshot(self) -> None:
        """snapshot 应返回浅拷贝。"""
        stats = StatsCollector()
        stats.record("a", 1)
        stats.record("b", 2)
        snap = stats.snapshot()
        assert snap == {"a": 1, "b": 2}
        # 修改快照不影响原始数据
        snap["a"] = 99
        assert stats.get("a") == 1
