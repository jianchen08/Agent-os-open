"""并发控制器测试。

测试 ConcurrencyController 的信号量获取/释放、并发限制、available 属性、无效级别。
"""

from __future__ import annotations

import asyncio

import pytest

from infrastructure.concurrency import ConcurrencyController


class TestConcurrencyAcquireRelease:
    """获取和释放信号量。"""

    @pytest.mark.asyncio
    async def test_acquire_release(self) -> None:
        """获取信号量后应正确释放。"""
        controller = ConcurrencyController(config={"agent_max": 2})
        assert controller.available["agent"] == 2

        async with controller.acquire("agent"):
            assert controller.available["agent"] == 1

        assert controller.available["agent"] == 2

    @pytest.mark.asyncio
    async def test_acquire_all_levels(self) -> None:
        """所有级别都应能正常获取和释放。"""
        controller = ConcurrencyController()
        defaults = {"provider": 3, "model": 5, "agent": 10}
        for level in ("provider", "model", "agent"):
            async with controller.acquire(level):
                assert controller.available[level] == defaults[level] - 1


class TestConcurrencyLimit:
    """超出并发限制时等待。"""

    @pytest.mark.asyncio
    async def test_concurrency_limit(self) -> None:
        """超出限制时新请求应等待。"""
        controller = ConcurrencyController(config={"agent_max": 1})
        results: list[str] = []
        barrier = asyncio.Event()

        async def worker(name: str) -> None:
            async with controller.acquire("agent"):
                results.append(f"{name}_start")
                await barrier.wait()
                results.append(f"{name}_end")

        # 启动两个 worker，第一个应先获取信号量
        task1 = asyncio.create_task(worker("a"))
        await asyncio.sleep(0.01)  # 让 task1 获取信号量

        task2 = asyncio.create_task(worker("b"))
        await asyncio.sleep(0.01)  # task2 应该在等待

        # 此时只有 task1 开始了
        assert results == ["a_start"]

        # 释放 barrier，让 task1 完成
        barrier.set()
        await asyncio.gather(task1, task2)

        # 两个都完成了
        assert "a_end" in results
        assert "b_start" in results
        assert "b_end" in results


class TestConcurrencyAvailable:
    """available 属性应正确反映信号量状态。"""

    @pytest.mark.asyncio
    async def test_available_default(self) -> None:
        """默认配置下各级别可用数应正确。"""
        controller = ConcurrencyController()
        avail = controller.available
        assert avail["provider"] == 3
        assert avail["model"] == 5
        assert avail["agent"] == 10

    @pytest.mark.asyncio
    async def test_available_custom(self) -> None:
        """自定义配置下可用数应正确。"""
        controller = ConcurrencyController(config={"provider_max": 1, "model_max": 2, "agent_max": 5})
        avail = controller.available
        assert avail["provider"] == 1
        assert avail["model"] == 2
        assert avail["agent"] == 5


class TestConcurrencyInvalidLevel:
    """无效级别应抛出异常。"""

    @pytest.mark.asyncio
    async def test_invalid_level(self) -> None:
        """不存在的级别应抛出 ValueError。"""
        controller = ConcurrencyController()
        with pytest.raises(ValueError, match="Unknown concurrency level"):
            async with controller.acquire("invalid"):
                pass
