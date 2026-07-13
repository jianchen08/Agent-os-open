"""llm_error_recovery 插件白名单行为测试。

回归核心问题：LLM API 调用层面的异常（1000 条 input 限制 / 上下文超长 /
token 超限 / rate_limit / service_down 等）绝不能被喂给大模型污染对话历史，
只有"LLM 真能通过调整操作修复"的 bad_request（工具参数 JSON 错、tool id
序列破坏）才追加恢复提示让 LLM 重试。

设计原则：白名单默认安全——未命中可修复关键词的 bad_request 一律不喂，
避免新增 API 错误类型时又把异常文本塞进 messages。
"""
from __future__ import annotations

import pytest

from pipeline.plugin import PluginContext
from pipeline.types import StateKeys
from plugins.output.llm_error_recovery.plugin import (
    LLMErrorRecoveryPlugin,
    _is_bad_request_fixable,
)


def _make_ctx(error_msg: str, error_type: str = "bad_request") -> PluginContext:
    """构造带 llm_error_info 的 ctx。"""
    return PluginContext(
        state={
            StateKeys.PIPELINE_ID: "test",
            "llm_error_info": {
                "error_msg": error_msg,
                "error_type": error_type,
                "core_type": "llm_call",
            },
            "messages": [{"role": "user", "content": "hi"}],
        },
        config={},
        _services={},
    )


def _fed_to_llm(result) -> bool:
    """插件是否把恢复提示喂给了 LLM（即是否改了 messages）。"""
    return "messages" in result.state_updates


class TestFixableWhitelist:
    """_is_bad_request_fixable 白名单判定。"""

    def test_invalid_function_arguments_is_fixable(self):
        assert _is_bad_request_fixable("invalid function arguments: bad JSON")

    def test_invalid_params_is_fixable(self):
        assert _is_bad_request_fixable("invalid params at index 0")

    def test_tool_id_not_found_is_fixable(self):
        assert _is_bad_request_fixable("tool id abc_123 not found")

    def test_1000_items_limit_not_fixable(self):
        assert not _is_bad_request_fixable(
            "Maximum of 1000 items allowed in input"
        )

    def test_context_length_not_fixable(self):
        assert not _is_bad_request_fixable("context_length_exceeded")

    def test_unknown_bad_request_not_fixable(self):
        """白名单默认安全：未知 bad_request 不视为可修复。"""
        assert not _is_bad_request_fixable("some weird api error")

    def test_case_insensitive(self):
        assert _is_bad_request_fixable("INVALID FUNCTION ARGUMENTS")


class TestApiErrorsNotFedToLLm:
    """API 调用层面的异常绝不能喂给 LLM 污染对话历史。

    这些错误 LLM 改参数也修不了，应由 error_check 走 end + 透传前端。
    """

    @pytest.mark.asyncio
    async def test_1000_items_limit_not_fed(self):
        plugin = LLMErrorRecoveryPlugin()
        ctx = _make_ctx(
            "BadRequestError: Maximum of 1000 items allowed in input",
            "bad_request",
        )
        assert not _fed_to_llm(await plugin.execute(ctx))

    @pytest.mark.asyncio
    async def test_context_overflow_not_fed(self):
        plugin = LLMErrorRecoveryPlugin()
        ctx = _make_ctx("context_length_exceeded", "bad_request")
        assert not _fed_to_llm(await plugin.execute(ctx))

    @pytest.mark.asyncio
    async def test_token_limit_not_fed(self):
        plugin = LLMErrorRecoveryPlugin()
        ctx = _make_ctx("max_tokens limit exceeded", "bad_request")
        assert not _fed_to_llm(await plugin.execute(ctx))

    @pytest.mark.asyncio
    async def test_rate_limit_not_fed(self):
        plugin = LLMErrorRecoveryPlugin()
        ctx = _make_ctx("rate limit exceeded", "rate_limit")
        assert not _fed_to_llm(await plugin.execute(ctx))

    @pytest.mark.asyncio
    async def test_service_down_not_fed(self):
        plugin = LLMErrorRecoveryPlugin()
        ctx = _make_ctx("service temporarily unavailable", "service_down")
        assert not _fed_to_llm(await plugin.execute(ctx))

    @pytest.mark.asyncio
    async def test_unknown_bad_request_not_fed(self):
        """白名单默认安全：未知 bad_request 不喂 LLM。"""
        plugin = LLMErrorRecoveryPlugin()
        ctx = _make_ctx("some weird api error", "bad_request")
        assert not _fed_to_llm(await plugin.execute(ctx))

    @pytest.mark.asyncio
    async def test_error_info_cleared_when_not_fed(self):
        """不喂 LLM 时必须清除 llm_error_info，避免下一轮重复处理。"""
        plugin = LLMErrorRecoveryPlugin()
        ctx = _make_ctx("Maximum of 1000 items allowed in input", "bad_request")
        result = await plugin.execute(ctx)
        assert result.state_updates.get("llm_error_info") is None


class TestFixableErrorsFedToLLm:
    """LLM 确实能修的 bad_request 仍喂给 LLM 重试。"""

    @pytest.mark.asyncio
    async def test_invalid_function_arguments_fed(self):
        plugin = LLMErrorRecoveryPlugin()
        ctx = _make_ctx(
            "invalid function arguments: expected JSON", "bad_request"
        )
        result = await plugin.execute(ctx)
        assert _fed_to_llm(result)
        # 喂进去的内容必须含错误信息与建议
        fed_msg = result.state_updates["messages"][-1]
        assert "invalid function arguments" in fed_msg["content"].lower()

    @pytest.mark.asyncio
    async def test_tool_id_not_found_fed(self):
        plugin = LLMErrorRecoveryPlugin()
        ctx = _make_ctx("tool id abc not found", "bad_request")
        assert _fed_to_llm(await plugin.execute(ctx))

    @pytest.mark.asyncio
    async def test_invalid_params_fed(self):
        plugin = LLMErrorRecoveryPlugin()
        ctx = _make_ctx("invalid params at index 0", "bad_request")
        assert _fed_to_llm(await plugin.execute(ctx))


class TestContextOverflowSkipped:
    """context_overflow 由压缩插件处理，recovery 不介入。"""

    @pytest.mark.asyncio
    async def test_context_overflow_type_skipped(self):
        plugin = LLMErrorRecoveryPlugin()
        ctx = _make_ctx("context window exceeds limit", "context_overflow")
        result = await plugin.execute(ctx)
        assert not _fed_to_llm(result)
        assert result.state_updates.get("llm_error_info") is None
