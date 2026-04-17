"""基础设施集成测试。

测试 Scheduler + ConcurrencyController + ResourceManager 协同工作。
"""

from __future__ import annotations

import asyncio

import pytest

from infrastructure.concurrency import ConcurrencyController
from infrastructure.error_policy import apply_error_policy
from infrastructure.resource import ResourceManager, ResourceQuota
from infrastructure.scheduler import Scheduler
from infrastructure.stats import StatsCollector
from pipeline.types import ErrorPolicy


class TestInfrastructureIntegration:
    """Scheduler + ConcurrencyController + ResourceManager 协同。"""

    @pytest.mark.asyncio
    async def test_full_pipeline_lifecycle(self) -> None:
        """模拟管道生命周期：调度 → 资源检查 → 并发控制 → 统计。"""
        # 设置
        scheduler = Scheduler()
        controller = ConcurrencyController(config={"agent_max": 2})
        resource_mgr = ResourceManager(quotas={
            "default": ResourceQuota(max_pipelines=5),
        })
        stats = StatsCollector()

        # 提交任务
        await scheduler.submit("task-1", priority=1)
        await scheduler.submit("task-2", priority=3)
        stats.increment("submitted")

        # 调度第一个任务
        task = await scheduler.pick_next()
        assert task == "task-1"

        # 检查资源
        assert resource_mgr.can_create("default")
        resource_mgr.register("default")
        stats.increment("active_pipelines")

        # 并发控制
        async with controller.acquire("agent"):
            stats.record("concurrency_available", controller.available["agent"])
            # 模拟工作
            await asyncio.sleep(0.01)

        # 释放资源
        resource_mgr.release("default")
        stats.increment("active_pipelines", delta=-1)

        # 验证统计
        assert stats.get("submitted") == 1
        assert stats.get("active_pipelines") == 0  # released
        assert stats.snapshot()["concurrency_available"] == 1  # agent_max=2, 占了1个

    @pytest.mark.asyncio
    async def test_resource_quota_blocks_creation(self) -> None:
        """资源配额满时应拒绝创建。"""
        resource_mgr = ResourceManager(quotas={
            "default": ResourceQuota(max_pipelines=10),
            "limited": ResourceQuota(max_pipelines=2),
        })

        assert resource_mgr.can_create("limited")
        resource_mgr.register("limited")
        assert resource_mgr.can_create("limited")
        resource_mgr.register("limited")
        assert not resource_mgr.can_create("limited")  # 已满

        resource_mgr.release("limited")
        assert resource_mgr.can_create("limited")  # 释放后可创建


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
