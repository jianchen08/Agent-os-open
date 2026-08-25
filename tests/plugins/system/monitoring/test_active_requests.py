# @feature: FP-0.2.二 内部模块manifest | @vision: V3 可嵌入 | @ci: python-coverage
"""active_requests 指标 end 侧行为测试 —— F-MON-1（start 工具已移除）。

意图（WHY）：
- monitoring.record_llm_request_start 工具因 plugin.json 未声明（声明即注册，
  永不可达）已从 server.py 删除（2026-08-25 全仓扫描 P0 断链修复）。
- 保留的契约：record_llm_request（end 语义）不调 start 时 active_requests
  不得变负，下限为 0（存量调用方只调 end）。

测试通过 server.py 公开工具函数（@plugin.tool 装饰后原样返回，可直接 await 调用）
+ _collect_token_usage 收集器（前端 /ext/monitoring/token-usage 端点数据源）验证。
"""

from __future__ import annotations

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
# 兼容性：只调 end 不调 start（存量调用方）
# ============================================================


class TestBackwardCompat:
    """存量调用只调 end 不调 start，active_requests 不得变负。"""

    @pytest.mark.asyncio
    async def test_end_without_start_not_negative(self, server_module, fresh_monitor) -> None:
        """只调 record_llm_request（未 start），active_requests 下限为 0。

        WHY：record_llm_request 会配对减除 active_requests，但存量调用方
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
