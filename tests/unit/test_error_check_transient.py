"""error_check 插件临时错误处理回归测试。

BUG-FIX-fix_20260629_transient_no_recovery:
临时错误（service_down/rate_limit/network/server_error）改为独立计数
retry.transient_count，重试上限 transient_max_retries（默认 10）。
- 未到上限 -> next_llm
- 达上限 -> end + status=failed（不再 wait/waiting_recovery）
- 永久错误（auth/quota/bad_request）保持 end 行为不变

设计意图：wait 状态没有主动唤醒源，会无限挂起；改为重试 10 次后失败，
让父任务通过 child_terminal 链感知并决定如何继续。
"""
from __future__ import annotations

import pytest

import litellm

from pipeline.plugin import PluginContext
from pipeline.types import StateKeys
from plugins.output.error_check.plugin import ErrorCheckPlugin


def _make_ctx(state: dict | None = None) -> PluginContext:
    base = {"retry.count": 0, "retry.transient_count": 0}
    if state:
        base.update(state)
    return PluginContext(state=base, config={}, _services={})


def _err_ctx(
    error_msg: str,
    transient_count: int = 0,
    retry_count: int = 0,
) -> PluginContext:
    return _make_ctx({
        StateKeys.RAW_ERROR: error_msg,
        "retry.count": retry_count,
        "retry.transient_count": transient_count,
    })


class TestTransientErrorRetriesBeforeFail:
    """临时错误在 transient_max_retries 内应继续 next_llm。"""

    @pytest.mark.asyncio
    async def test_service_down_first_attempt_yields_next_llm(self) -> None:
        plugin = ErrorCheckPlugin({"transient_max_retries": 10})
        ctx = _err_ctx(
            "ServiceUnavailableError: Service temporarily unavailable",
            transient_count=0,
        )
        result = await plugin.execute(ctx)
        assert result.route_signal is not None
        assert result.route_signal.route_type == "next_llm"
        assert result.state_updates["retry.transient_count"] == 1

    @pytest.mark.asyncio
    async def test_service_down_attempt_9_still_next_llm(self) -> None:
        plugin = ErrorCheckPlugin({"transient_max_retries": 10})
        ctx = _err_ctx(
            "ServiceUnavailableError: Service temporarily unavailable",
            transient_count=9,
        )
        result = await plugin.execute(ctx)
        assert result.route_signal is not None
        assert result.route_signal.route_type == "next_llm"
        assert result.state_updates["retry.transient_count"] == 10

    @pytest.mark.asyncio
    async def test_network_timeout_below_max_yields_next_llm(self) -> None:
        plugin = ErrorCheckPlugin({"transient_max_retries": 10})
        ctx = _err_ctx(
            "ReadTimeout: Stream first chunk timeout",
            transient_count=3,
        )
        result = await plugin.execute(ctx)
        assert result.route_signal is not None
        assert result.route_signal.route_type == "next_llm"


class TestTransientErrorExhaustedFails:
    """临时错误重试上限后必须 end + status=failed（不再 wait）。"""

    @pytest.mark.asyncio
    async def test_service_down_exhausted_yields_end_failed(self) -> None:
        plugin = ErrorCheckPlugin({"transient_max_retries": 3})
        ctx = _err_ctx(
            "ServiceUnavailableError: Service temporarily unavailable",
            transient_count=3,
        )
        result = await plugin.execute(ctx)
        assert result.route_signal is not None
        assert result.route_signal.route_type == "end", (
            f"临时错误耗尽必须 end，实际: {result.route_signal.route_type}"
        )
        assert result.state_updates[StateKeys.EXECUTION_STATUS] == "failed"

    @pytest.mark.asyncio
    async def test_rate_limit_exhausted_yields_end_failed(self) -> None:
        plugin = ErrorCheckPlugin({"transient_max_retries": 2})
        ctx = _err_ctx(
            "RateLimitError: Upstream rate limit exceeded",
            transient_count=2,
        )
        result = await plugin.execute(ctx)
        assert result.route_signal is not None
        assert result.route_signal.route_type == "end"
        assert result.state_updates[StateKeys.EXECUTION_STATUS] == "failed"

    @pytest.mark.asyncio
    async def test_network_timeout_exhausted_yields_end_failed(self) -> None:
        plugin = ErrorCheckPlugin({"transient_max_retries": 2})
        ctx = _err_ctx(
            "ReadTimeout: Timeout on reading data from socket",
            transient_count=2,
        )
        result = await plugin.execute(ctx)
        assert result.route_signal is not None
        assert result.route_signal.route_type == "end"
        assert result.state_updates[StateKeys.EXECUTION_STATUS] == "failed"

    @pytest.mark.asyncio
    async def test_server_error_exhausted_yields_end_failed(self) -> None:
        plugin = ErrorCheckPlugin({"transient_max_retries": 2})
        ctx = _err_ctx(
            "InternalServerError: 500 internal",
            transient_count=2,
        )
        result = await plugin.execute(ctx)
        assert result.route_signal is not None
        assert result.route_signal.route_type == "end"
        assert result.state_updates[StateKeys.EXECUTION_STATUS] == "failed"

    @pytest.mark.asyncio
    async def test_no_route_wait_for_transient_errors(self) -> None:
        """废除 wait 路径：临时错误任何阶段都不应产生 route=wait。"""
        plugin = ErrorCheckPlugin({"transient_max_retries": 1})
        ctx = _err_ctx(
            "ServiceUnavailableError: down",
            transient_count=1,
        )
        result = await plugin.execute(ctx)
        assert result.route_signal is not None
        assert result.route_signal.route_type != "wait", (
            "wait 路径已废除，必须 end + failed"
        )


class TestPermanentErrorStillEnds:
    """永久错误（auth/quota/bad_request）保持 end 行为不变。"""

    @pytest.mark.asyncio
    async def test_auth_failed_yields_end(self) -> None:
        plugin = ErrorCheckPlugin({})
        ctx = _err_ctx(
            "AuthenticationError: invalid api key",
            retry_count=0,
        )
        result = await plugin.execute(ctx)
        assert result.route_signal is not None
        assert result.route_signal.route_type == "end"

    @pytest.mark.asyncio
    async def test_quota_exhausted_yields_end(self) -> None:
        plugin = ErrorCheckPlugin({})
        ctx = _err_ctx(
            "quota exceeded, billing limit reached",
            retry_count=0,
        )
        result = await plugin.execute(ctx)
        assert result.route_signal is not None
        assert result.route_signal.route_type == "end"


class TestRealLiteLLMExceptions:
    """用真实 litellm 异常类型验证分类正确。"""

    @pytest.mark.asyncio
    async def test_litellm_service_unavailable_exhausted_fails(self) -> None:
        plugin = ErrorCheckPlugin({"transient_max_retries": 1})
        exc = litellm.ServiceUnavailableError(
            message="Service temporarily unavailable",
            model="glm-5.2",
            llm_provider="openai",
        )
        ctx = _err_ctx(str(exc), transient_count=1)
        result = await plugin.execute(ctx)
        assert result.route_signal is not None
        assert result.route_signal.route_type == "end"
        assert result.state_updates[StateKeys.EXECUTION_STATUS] == "failed"

    @pytest.mark.asyncio
    async def test_litellm_rate_limit_below_max_retries(self) -> None:
        plugin = ErrorCheckPlugin({"transient_max_retries": 5})
        exc = litellm.RateLimitError(
            message="Upstream rate limit exceeded, please retry later",
            model="glm-5.2",
            llm_provider="openai",
        )
        ctx = _err_ctx(str(exc), transient_count=2)
        result = await plugin.execute(ctx)
        assert result.route_signal is not None
        assert result.route_signal.route_type == "next_llm"


class TestDefaultRetryLimitIs10:
    """验证 transient_max_retries 默认值 = 10。"""

    def test_default_transient_max_retries(self) -> None:
        plugin = ErrorCheckPlugin({})
        assert plugin._transient_max_retries == 10
