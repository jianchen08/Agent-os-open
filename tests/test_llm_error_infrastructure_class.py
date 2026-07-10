"""测试 LLM 错误分类与恢复提示注入策略。

背景：LLM 调用失败时，认证/权限/连接/key 耗尽/限流/配额等基础设施类错误
LLM 无法通过调整操作修复。原实现把这些错误当作 unknown 类型，
给 LLM 追加「请检查你的操作是否正确」的恢复提示，毫无意义且浪费调用。

修复：
1. 新增 infrastructure_error 分类（含 rate_limit / budget_exceeded / 上限 / 额度）
2. infrastructure_error 和 unknown 类型不追加面向 LLM 的提示
3. 仅 llm_fixable（非法工具参数）保留提示注入

测试覆盖：
1. _build_llm_error_info 的分类逻辑（infrastructure 优先级最高）
2. LLMErrorRecoveryPlugin 的 infrastructure/unknown 分支（不追加 messages）
3. llm_fixable 分支（仍追加提示）
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from pipeline.engine_chain import _build_llm_error_info
from pipeline.plugin import OutputResult, PluginContext
from plugins.output.llm_error_recovery.plugin import LLMErrorRecoveryPlugin


# ---------------------------------------------------------------------------
# 1. 分类逻辑：_build_llm_error_info
# ---------------------------------------------------------------------------

def _classify(error_msg: str) -> str:
    """辅助函数：对给定错误信息跑一遍分类，返回 error_type。"""
    state: dict[str, Any] = {}
    _build_llm_error_info(state, error_msg, "llm_call")
    return state["llm_error_info"]["error_type"]


class TestInfrastructureClassification:
    """基础设施错误识别。"""

    def test_authentication_error(self) -> None:
        assert _classify(
            "AuthenticationError: ZaiException - The api_key client option "
            "must be set either by passing api_key to the client"
        ) == "infrastructure_error"

    def test_authentication_error_lowercase(self) -> None:
        assert _classify("litellm.authenticationerror: invalid api key") == "infrastructure_error"

    def test_api_key_phrase(self) -> None:
        assert _classify("api_key is missing or invalid") == "infrastructure_error"
        assert _classify("api key not configured") == "infrastructure_error"

    def test_permission_denied(self) -> None:
        assert _classify("PermissionDeniedError: 403 Forbidden") == "infrastructure_error"

    def test_connection_error(self) -> None:
        assert _classify("APIConnectionError: connection refused") == "infrastructure_error"
        assert _classify("connection reset by peer") == "infrastructure_error"

    def test_key_pool_exhausted_chinese(self) -> None:
        assert _classify("所有 key 均失败 provider=zhipu_coding model=glm-5.1") == "infrastructure_error"


class TestPriorityOverOtherTypes:
    """infrastructure 分类优先级最高，覆盖其他可能匹配的错误。"""

    def test_auth_with_connection_keyword_still_infrastructure(self) -> None:
        # 既含 connection 又含 authentication → infrastructure（不被 connection 单独影响）
        assert _classify("authentication error: connection to api failed") == "infrastructure_error"

    def test_infrastructure_beats_unknown(self) -> None:
        # 默认会落到 unknown 的错误，只要含基础设施关键词就是 infrastructure
        assert _classify("api_key expired, please refresh") == "infrastructure_error"


class TestNonInfrastructureClassification:
    """确认非基础设施错误仍按原逻辑分类，未被误伤。"""

    def test_context_overflow_still_classified(self) -> None:
        assert _classify("context_length_exceeded: this model has a maximum") == "context_overflow"

    def test_context_window_exceeds(self) -> None:
        assert _classify("context window exceeds maximum token limit") == "context_overflow"

    def test_llm_fixable_invalid_params(self) -> None:
        assert _classify("invalid function arguments: tool_call_id required") == "llm_fixable"
        assert _classify("invalid params in tool call") == "llm_fixable"

    def test_unknown_timeout(self) -> None:
        # timeout 不含基础设施关键词，落到 unknown（既有 hint 逻辑会处理）
        assert _classify("request timed out after 30s") == "unknown"

    def test_unknown_generic(self) -> None:
        assert _classify("some random unexpected error") == "unknown"

    def test_rate_limit_now_infrastructure(self) -> None:
        # rate limit / 上限 / 额度 / 用完 现在是 infrastructure_error
        assert _classify("rate limit exceeded: 429") == "infrastructure_error"
        assert _classify("RateLimitError: ZaiException - 已达到 5 小时的使用上限") == "infrastructure_error"
        assert _classify("BudgetExceededError: 每周额度已用完") == "infrastructure_error"
        assert _classify("Insufficient Balance: 余额不足") == "infrastructure_error"


# ---------------------------------------------------------------------------
# 2. 插件分支：LLMErrorRecoveryPlugin
# ---------------------------------------------------------------------------

def _run_execute(state: dict[str, Any]) -> OutputResult:
    """辅助函数：用独立事件循环同步执行插件。

    注意：不能用 asyncio.get_event_loop().is_running() 判断，因为 Python 3.14
    在无运行 loop 时会直接抛 RuntimeError。直接 asyncio.run 即可。
    """
    plugin = LLMErrorRecoveryPlugin()
    ctx = PluginContext(state=state)
    return asyncio.run(plugin.execute(ctx))


class TestPluginInfrastructureBranch:
    """infrastructure_error 分支：不追加 messages，清除错误状态。"""

    def test_infrastructure_no_message_appended(self) -> None:
        """核心断言：认证失败时不追加面向 LLM 的提示。"""
        original_messages = [{"role": "user", "content": "原消息"}]
        state = {
            "messages": list(original_messages),
            "llm_error_info": {
                "error_msg": "AuthenticationError: api_key not set",
                "error_type": "infrastructure_error",
                "core_type": "llm_call",
            },
        }
        result = _run_execute(state)

        # 不追加任何 messages 更新
        assert "messages" not in result.state_updates
        # 错误信息被清除
        assert result.state_updates.get("llm_error_info") is None

    def test_infrastructure_does_not_mutate_input_state(self) -> None:
        """不修改输入 state（只通过 state_updates 返回）。"""
        state = {
            "messages": [{"role": "user", "content": "hi"}],
            "llm_error_info": {
                "error_msg": "connection refused",
                "error_type": "infrastructure_error",
                "core_type": "llm_call",
            },
        }
        original_msg_count = len(state["messages"])
        _run_execute(state)
        assert len(state["messages"]) == original_msg_count

    def test_infrastructure_no_error_info_returns_empty(self) -> None:
        """无错误信息时直接返回空结果。"""
        result = _run_execute({"messages": []})
        assert result.state_updates == {}


class TestPluginOtherBranchesUntouched:
    """确认其他分支行为未受影响。"""

    def test_context_overflow_still_skips_hint(self) -> None:
        state = {
            "messages": [{"role": "user", "content": "x"}],
            "llm_error_info": {
                "error_msg": "context_length_exceeded",
                "error_type": "context_overflow",
                "core_type": "llm_call",
            },
        }
        result = _run_execute(state)
        assert "messages" not in result.state_updates
        assert result.state_updates.get("llm_error_info") is None

    def test_unknown_no_longer_appends_hint(self) -> None:
        """unknown 类型不再追加提示（LLM 无法修复未知错误，注入无意义）。"""
        state = {
            "messages": [{"role": "user", "content": "x"}],
            "llm_error_info": {
                "error_msg": "some unexpected error",
                "error_type": "unknown",
                "core_type": "llm_call",
            },
        }
        result = _run_execute(state)
        # unknown 不再追加 messages
        assert "messages" not in result.state_updates
        assert result.state_updates.get("llm_error_info") is None

    def test_llm_fixable_still_appends_hint(self) -> None:
        state = {
            "messages": [{"role": "user", "content": "x"}],
            "llm_error_info": {
                "error_msg": "invalid function arguments",
                "error_type": "llm_fixable",
                "core_type": "llm_call",
            },
        }
        result = _run_execute(state)
        assert "messages" in result.state_updates


# ---------------------------------------------------------------------------
# 3. 端到端：从错误信息到插件决策
# ---------------------------------------------------------------------------

class TestEndToEndAuthError:
    """真实日志里的认证错误，端到端走通：分类 → 不追加提示。"""

    REAL_AUTH_ERROR = (
        "litellm.AuthenticationError: AuthenticationError: ZaiException - "
        "The api_key client option must be set either by passing api_key "
        "to the client or by setting the ZAI_API_KEY environment variable"
    )

    def test_real_auth_error_classified_as_infrastructure(self) -> None:
        assert _classify(self.REAL_AUTH_ERROR) == "infrastructure_error"

    def test_real_auth_error_no_hint_to_llm(self) -> None:
        # 先分类（模拟 engine_chain 调用）
        state: dict[str, Any] = {"messages": [{"role": "user", "content": "hi"}]}
        _build_llm_error_info(state, self.REAL_AUTH_ERROR, "llm_call")

        # 再交给插件处理
        result = _run_execute(state)

        # 核心断言：不会把「请检查你的操作是否正确」推给 LLM
        assert "messages" not in result.state_updates
        assert result.state_updates.get("llm_error_info") is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
