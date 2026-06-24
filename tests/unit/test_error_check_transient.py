"""error_check 插件临时错误处理回归测试。

BUG-FIX-fix_20260624_transient_no_end:
验证核心修复点：临时错误（service_down/rate_limit/network/server_error）
在重试耗尽后产 wait 信号挂起等待，而不是 end 终止管道。
永久错误（auth/quota/bad_request）保持 end 行为不变。

设计意图："错误就等下再调用，不导致后面调用的停止和失败，总超时由 idle 负责"
"""
from __future__ import annotations

import asyncio

import pytest

import litellm

from pipeline.plugin import PluginContext
from pipeline.types import StateKeys
from plugins.output.error_check.plugin import ErrorCheckPlugin


def _make_ctx(state: dict | None = None) -> PluginContext:
    """构造带基础 state 的 PluginContext。"""
    base = {"retry.count": 0}
    if state:
        base.update(state)
    return PluginContext(state=base, config={}, _services={})


def _err_ctx(error_msg: str, retry_count: int = 0) -> PluginContext:
    """构造带 raw_error 的 ctx（使用 StateKeys.RAW_ERROR 正确 key）。"""
    return _make_ctx({
        StateKeys.RAW_ERROR: error_msg,
        "retry.count": retry_count,
    })


class TestTransientErrorSuspends:
    """临时错误重试耗尽应产 wait 信号挂起，不 end。"""

    @pytest.mark.asyncio
    async def test_service_down_exhausted_yields_wait(self) -> None:
        """service_down 重试耗尽 → wait（不 end）。"""
        plugin = ErrorCheckPlugin({"max_retries": 2})
        ctx = _err_ctx(
            "ServiceUnavailableError: Service temporarily unavailable",
            retry_count=2,
        )
        result = await plugin.execute(ctx)

        assert result.route_signal is not None
        assert result.route_signal.route_type == "wait", (
            f"临时错误耗尽应 wait 挂起，实际: {result.route_signal.route_type}"
        )
        assert result.state_updates[StateKeys.EXECUTION_STATUS] == "waiting_recovery"

    @pytest.mark.asyncio
    async def test_rate_limit_exhausted_yields_wait(self) -> None:
        """rate_limit 重试耗尽 → wait。"""
        plugin = ErrorCheckPlugin({"max_retries": 1})
        ctx = _err_ctx(
            "RateLimitError: Upstream rate limit exceeded",
            retry_count=1,
        )
        result = await plugin.execute(ctx)
        assert result.route_signal is not None
        assert result.route_signal.route_type == "wait"

    @pytest.mark.asyncio
    async def test_network_timeout_exhausted_yields_wait(self) -> None:
        """network 超时重试耗尽 → wait。"""
        plugin = ErrorCheckPlugin({"max_retries": 1})
        ctx = _err_ctx(
            "ReadTimeout: Timeout on reading data from socket",
            retry_count=1,
        )
        result = await plugin.execute(ctx)
        assert result.route_signal is not None
        assert result.route_signal.route_type == "wait"

    @pytest.mark.asyncio
    async def test_server_error_exhausted_yields_wait(self) -> None:
        """500 server_error 重试耗尽 → wait。"""
        plugin = ErrorCheckPlugin({"max_retries": 1})
        ctx = _err_ctx(
            "InternalServerError: 500 internal",
            retry_count=1,
        )
        result = await plugin.execute(ctx)
        assert result.route_signal is not None
        assert result.route_signal.route_type == "wait"


class TestTransientErrorRetriesBeforeWait:
    """临时错误在重试次数内应继续 next_llm 重试。"""

    @pytest.mark.asyncio
    async def test_service_down_first_attempt_yields_next_llm(self) -> None:
        """service_down 首次 → next_llm 重试（不是立即 wait）。"""
        plugin = ErrorCheckPlugin({"max_retries": 3})
        ctx = _err_ctx(
            "ServiceUnavailableError: Service temporarily unavailable",
            retry_count=0,
        )
        result = await plugin.execute(ctx)
        assert result.route_signal is not None
        assert result.route_signal.route_type == "next_llm"
        assert result.state_updates["retry.count"] == 1

    @pytest.mark.asyncio
    async def test_service_down_second_attempt_still_next_llm(self) -> None:
        """service_down 第 2 次（未达 max=3）→ next_llm。"""
        plugin = ErrorCheckPlugin({"max_retries": 3})
        ctx = _err_ctx(
            "ServiceUnavailableError: Service temporarily unavailable",
            retry_count=2,
        )
        result = await plugin.execute(ctx)
        assert result.route_signal is not None
        assert result.route_signal.route_type == "next_llm"


class TestPermanentErrorStillEnds:
    """永久错误（auth/quota/bad_request）保持 end 行为不变。"""

    @pytest.mark.asyncio
    async def test_auth_failed_yields_end(self) -> None:
        """auth_failed → end（不可恢复）。"""
        plugin = ErrorCheckPlugin({"max_retries": 3})
        ctx = _err_ctx(
            "AuthenticationError: invalid api key",
            retry_count=0,
        )
        result = await plugin.execute(ctx)
        assert result.route_signal is not None
        assert result.route_signal.route_type == "end"

    @pytest.mark.asyncio
    async def test_quota_exhausted_yields_end(self) -> None:
        """quota 耗尽 → end。"""
        plugin = ErrorCheckPlugin({"max_retries": 3})
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
    async def test_litellm_service_unavailable_suspends(self) -> None:
        """真实 litellm.ServiceUnavailableError → wait。"""
        plugin = ErrorCheckPlugin({"max_retries": 1})
        exc = litellm.ServiceUnavailableError(
            message="Service temporarily unavailable",
            model="glm-5.2",
            llm_provider="openai",
        )
        ctx = _err_ctx(str(exc), retry_count=1)
        result = await plugin.execute(ctx)
        assert result.route_signal is not None
        assert result.route_signal.route_type == "wait"

    @pytest.mark.asyncio
    async def test_litellm_rate_limit_suspends(self) -> None:
        """真实 litellm.RateLimitError → wait。"""
        plugin = ErrorCheckPlugin({"max_retries": 1})
        exc = litellm.RateLimitError(
            message="Upstream rate limit exceeded, please retry later",
            model="glm-5.2",
            llm_provider="openai",
        )
        ctx = _err_ctx(str(exc), retry_count=1)
        result = await plugin.execute(ctx)
        assert result.route_signal is not None
        assert result.route_signal.route_type == "wait"
