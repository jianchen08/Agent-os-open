# @feature: FP-0.2.二 内部模块manifest | @vision: V3 可嵌入 | @ci: python-coverage
"""active_requests 指标真实化（start/end 配对）测试 —— F-MON-1。

意图（WHY）：
- active_requests 必须真实反映「当前正在进行的 LLM 请求数」，而非恒为 0。
  修复前 record_llm_request（结束语义）不自增也不自减 active_requests，
  且没有 start 配对工具暴露，导致该指标恒 0，前端无法展示实际负载。
- start/end 配对语义：start +1，end（record_llm_request）-1。
- 兼容性：只调 end 不调 start（存量调用方）不得变负，下限为 0。
- 并发正确：多次 start 累加，单次 end 减一，反映真实并发度。

测试通过 server.py 公开工具函数（@plugin.tool 装饰后原样返回，可直接 await 调用）
+ _collect_token_usage 收集器（前端 /ext/monitoring/token-usage 端点数据源）验证。
"""

from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.unit  # 0.2 TDD 分层：单元测试


# ============================================================
# fixtures
# ============================================================


@pytest.fixture
def server_module():
    """导入 server 模块（平铺 import 由 conftest 注入 sys.path 解析）。"""
    import server

    return server


@pytest.fixture
def fresh_monitor(server_module):
    """每个测试用全新 PerformanceMonitor 替换 server 模块级单例，隔离计数状态。

    active_requests 是累加状态，若跨测试共享会互相污染；本 fixture 保证每个测试
    从 active_requests=0 起步，并在结束后还原原单例。
    """
    from performance_monitor import PerformanceMonitor

    old = server_module._monitor
    server_module._monitor = PerformanceMonitor()
    yield server_module._monitor
    server_module._monitor = old


def _active(monitor) -> int:
    """读 monitor 当前 active_requests。"""
    return monitor._llm_stats["active_requests"]


# ============================================================
# 工具注册（公开契约）
# ============================================================


class TestToolRegistration:
    """新工具 monitoring.record_llm_request_start 必须作为公开工具注册。"""

    def test_start_tool_registered(self, server_module) -> None:
        """start 工具已注册到 plugin._tools。"""
        assert "monitoring.record_llm_request_start" in server_module.plugin._tools

    def test_start_tool_callable(self, server_module) -> None:
        """start 工具函数可调用。"""
        assert hasattr(server_module, "monitoring_record_llm_request_start")
        assert callable(server_module.monitoring_record_llm_request_start)


# ============================================================
# start/end 配对语义
# ============================================================


class TestStartEndPairing:
    """start/end 配对：active_requests 真实反映活跃请求数。"""

    @pytest.mark.asyncio
    async def test_start_increments_active_requests(self, server_module, fresh_monitor) -> None:
        """start 后 active_requests = 1。

        WHY：一次请求开始时上报 start，活跃数应 +1，反映有一个请求正在进行。
        """
        await server_module.monitoring_record_llm_request_start()
        assert _active(fresh_monitor) == 1

    @pytest.mark.asyncio
    async def test_start_then_end_returns_to_zero(self, server_module, fresh_monitor) -> None:
        """start 后 end，active_requests 归 0。

        WHY：请求结束后调用 record_llm_request 应配对减除 start 的 +1，活跃数归 0。
        """
        await server_module.monitoring_record_llm_request_start()
        assert _active(fresh_monitor) == 1
        await server_module.monitoring_record_llm_request(response_time=0.5, error=False)
        assert _active(fresh_monitor) == 0

    @pytest.mark.asyncio
    async def test_end_preserves_count_and_timing(self, server_module, fresh_monitor) -> None:
        """end 仍正确累计 request_count / total_response_time / error_count。

        WHY：配对减除 active_requests 不能破坏原有的计数/耗时/错误统计职责。
        """
        await server_module.monitoring_record_llm_request_start()
        await server_module.monitoring_record_llm_request(response_time=0.5, error=False)
        await server_module.monitoring_record_llm_request_start()
        await server_module.monitoring_record_llm_request(response_time=1.5, error=True)
        ls = fresh_monitor._llm_stats
        assert ls["request_count"] == 2
        assert ls["total_response_time"] == pytest.approx(2.0)
        assert ls["error_count"] == 1
        assert _active(fresh_monitor) == 0


# ============================================================
# 兼容性：只调 end 不调 start（存量调用方）
# ============================================================


class TestBackwardCompat:
    """存量调用只调 end 不调 start，active_requests 不得变负。"""

    @pytest.mark.asyncio
    async def test_end_without_start_not_negative(self, server_module, fresh_monitor) -> None:
        """只调 record_llm_request（未 start），active_requests 下限为 0。

        WHY：record_llm_request 现在会配对减除 active_requests，但存量调用方
        只调 end 不调 start，若无下限保护，active_requests 会变 -1，破坏指标语义
        并触发 detect_bottlenecks 误判。必须 floor 到 0。
        """
        await server_module.monitoring_record_llm_request(response_time=0.3, error=False)
        assert _active(fresh_monitor) == 0

    @pytest.mark.asyncio
    async def test_many_ends_without_start_stays_zero(self, server_module, fresh_monitor) -> None:
        """多次只调 end，active_requests 恒为 0（不被负数累积）。"""
        for _ in range(5):
            await server_module.monitoring_record_llm_request(response_time=0.1, error=False)
        assert _active(fresh_monitor) == 0


# ============================================================
# 并发计数正确性
# ============================================================


class TestConcurrentCounting:
    """并发 start/end：active_requests 反映真实并发度。"""

    @pytest.mark.asyncio
    async def test_multiple_starts_then_partial_ends(self, server_module, fresh_monitor) -> None:
        """3 个 start → active=3；1 个 end → active=2。

        WHY：并发场景下 active_requests 应等于「已 start 未 end」的请求数，
        是前端展示并发负载的核心信号。
        """
        for _ in range(3):
            await server_module.monitoring_record_llm_request_start()
        assert _active(fresh_monitor) == 3
        await server_module.monitoring_record_llm_request(response_time=0.2, error=False)
        assert _active(fresh_monitor) == 2

    @pytest.mark.asyncio
    async def test_concurrent_starts_under_barrier(self, server_module, fresh_monitor) -> None:
        """屏障并发：所有 start 先完成，active_requests 准确等于并发数 N。"""
        N = 10

        async def one_start():
            await server_module.monitoring_record_llm_request_start()

        barrier = asyncio.Barrier(N)

        async def gated_start():
            await barrier.wait()
            await one_start()

        await asyncio.gather(*(gated_start() for _ in range(N)))
        assert _active(fresh_monitor) == N


# ============================================================
# 前端可消费：_collect_token_usage 含 active_requests
# ============================================================


class TestCollectorExposesActiveRequests:
    """_collect_token_usage（/ext/monitoring/token-usage 数据源）须含 active_requests。"""

    @pytest.mark.asyncio
    async def test_collector_includes_active_requests(self, server_module, fresh_monitor) -> None:
        """采集器返回 active_requests，供前端展示活跃 LLM 请求数。"""
        await server_module.monitoring_record_llm_request_start()
        await server_module.monitoring_record_llm_request_start()
        usage = server_module._collect_token_usage()
        assert usage["active_requests"] == 2
        # 同时保留原有字段
        assert usage["request_count"] == 0

    @pytest.mark.asyncio
    async def test_collector_includes_error_and_total_time(
        self, server_module, fresh_monitor
    ) -> None:
        """采集器同时暴露 error_count / total_response_time，便于前端展示。"""
        await server_module.monitoring_record_llm_request_start()
        await server_module.monitoring_record_llm_request(response_time=2.0, error=True)
        usage = server_module._collect_token_usage()
        assert usage["request_count"] == 1
        assert usage["error_count"] == 1
        assert usage["total_response_time"] == pytest.approx(2.0)
        assert usage["active_requests"] == 0
