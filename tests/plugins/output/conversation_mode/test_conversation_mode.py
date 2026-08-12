"""ConversationModeDetector 单元测试——对话模式激活/循环/结束三态机。

覆盖：未激活时从 tool_results 检测激活信号、已激活无工具调用产生 wait、
已激活有工具调用清除状态、_extract_conversation_flag 多层字段提取。
"""

from __future__ import annotations

from typing import Any

import pytest

from pipeline.plugin import PluginContext
from pipeline.types import StateKeys

pytestmark = pytest.mark.unit


# ============================================================
# 辅助
# ============================================================


def _ctx(state: dict[str, Any]) -> PluginContext:
    return PluginContext(state=state, config={})


def _tool_result_with_conv(where: str = "data") -> dict[str, Any]:
    """构造 success=True 且带 conversation_mode=True 的 tool_result。"""
    if where == "data":
        return {
            "success": True,
            "data": {"conversation_mode": True},
        }
    if where == "data.output":
        return {
            "success": True,
            "data": {"output": {"conversation_mode": True}},
        }
    if where == "data.data":
        return {
            "success": True,
            "data": {"data": {"conversation_mode": True}},
        }
    return {"success": True, "data": {}}


# ============================================================
# 配置与基本属性
# ============================================================


class TestConfig:
    def test_属性(self) -> None:
        from plugin import ConversationModeDetector

        d = ConversationModeDetector()
        assert d.name == "conversation_mode"
        assert d.priority == 5
        assert d.route_signals == ["wait"]

    def test_error_policy为SKIP(self) -> None:
        from plugin import ConversationModeDetector
        from pipeline.types import ErrorPolicy

        assert ConversationModeDetector.error_policy == ErrorPolicy.SKIP


# ============================================================
# _extract_conversation_flag
# ============================================================


class TestExtractFlag:
    def test_顶层conversation_mode为True(self) -> None:
        from plugin import ConversationModeDetector

        d = ConversationModeDetector()
        assert d._extract_conversation_flag({"conversation_mode": True}) is True

    def test_output子字段conversation_mode(self) -> None:
        from plugin import ConversationModeDetector

        d = ConversationModeDetector()
        assert (
            d._extract_conversation_flag({"output": {"conversation_mode": True}})
            is True
        )

    def test_data子字段conversation_mode(self) -> None:
        from plugin import ConversationModeDetector

        d = ConversationModeDetector()
        assert (
            d._extract_conversation_flag({"data": {"conversation_mode": True}})
            is True
        )

    def test_无标志返回False(self) -> None:
        from plugin import ConversationModeDetector

        d = ConversationModeDetector()
        assert d._extract_conversation_flag({"other": 1}) is False
        assert d._extract_conversation_flag({}) is False

    def test_标志为False不激活(self) -> None:
        from plugin import ConversationModeDetector

        d = ConversationModeDetector()
        assert d._extract_conversation_flag({"conversation_mode": False}) is False


# ============================================================
# 激活检测（未激活态）
# ============================================================


class TestActivation:
    @pytest.mark.asyncio
    async def test_tool_results带激活信号则激活并wait(self) -> None:
        from plugin import ConversationModeDetector

        d = ConversationModeDetector()
        result = await d.execute(
            _ctx(
                {
                    StateKeys.CONVERSATION_MODE: False,
                    StateKeys.TOOL_RESULTS: [_tool_result_with_conv("data")],
                }
            )
        )
        assert result.state_updates[StateKeys.CONVERSATION_MODE] is True
        assert result.state_updates[StateKeys.CONVERSATION_ROUND] == 1
        assert result.route_signal is not None
        assert result.route_signal.route_type == "wait"
        assert result.skip_remaining is True

    @pytest.mark.asyncio
    async def test_多层嵌套的激活信号也能检测(self) -> None:
        from plugin import ConversationModeDetector

        d = ConversationModeDetector()
        for where in ("data", "data.output", "data.data"):
            result = await d.execute(
                _ctx(
                    {
                        StateKeys.CONVERSATION_MODE: False,
                        StateKeys.TOOL_RESULTS: [_tool_result_with_conv(where)],
                    }
                )
            )
            assert result.state_updates.get(StateKeys.CONVERSATION_MODE) is True, where

    @pytest.mark.asyncio
    async def test_tool_result_success非True不激活(self) -> None:
        from plugin import ConversationModeDetector

        d = ConversationModeDetector()
        result = await d.execute(
            _ctx(
                {
                    StateKeys.CONVERSATION_MODE: False,
                    StateKeys.TOOL_RESULTS: [
                        {"success": False, "data": {"conversation_mode": True}}
                    ],
                }
            )
        )
        assert result.state_updates == {}

    @pytest.mark.asyncio
    async def test_无tool_results返回空结果(self) -> None:
        from plugin import ConversationModeDetector

        d = ConversationModeDetector()
        result = await d.execute(_ctx({StateKeys.CONVERSATION_MODE: False}))
        assert result.state_updates == {}
        assert result.route_signal is None

    @pytest.mark.asyncio
    async def test_tool_results无激活信号返回空结果(self) -> None:
        from plugin import ConversationModeDetector

        d = ConversationModeDetector()
        result = await d.execute(
            _ctx(
                {
                    StateKeys.CONVERSATION_MODE: False,
                    StateKeys.TOOL_RESULTS: [
                        {"success": True, "data": {"other": 1}}
                    ],
                }
            )
        )
        assert result.state_updates == {}


# ============================================================
# 已激活态：对话循环 / 对话结束
# ============================================================


class TestActiveConversation:
    @pytest.mark.asyncio
    async def test_已激活无工具调用产生wait并递增round(self) -> None:
        from plugin import ConversationModeDetector

        d = ConversationModeDetector()
        result = await d.execute(
            _ctx(
                {
                    StateKeys.CONVERSATION_MODE: True,
                    StateKeys.CONVERSATION_ROUND: 2,
                    StateKeys.RAW_TOOL_CALLS: [],  # 纯文本回复
                }
            )
        )
        assert result.state_updates[StateKeys.CONVERSATION_ROUND] == 3
        assert result.route_signal.route_type == "wait"
        assert "round 3" in result.route_signal.reason
        assert result.skip_remaining is True

    @pytest.mark.asyncio
    async def test_已激活无round默认从1开始(self) -> None:
        from plugin import ConversationModeDetector

        d = ConversationModeDetector()
        result = await d.execute(
            _ctx(
                {
                    StateKeys.CONVERSATION_MODE: True,
                    StateKeys.RAW_TOOL_CALLS: [],
                }
            )
        )
        # 无 CONVERSATION_ROUND → state.get 返回 0 → +1 = 1
        assert result.state_updates[StateKeys.CONVERSATION_ROUND] == 1

    @pytest.mark.asyncio
    async def test_已激活有工具调用则清除对话模式(self) -> None:
        from plugin import ConversationModeDetector

        d = ConversationModeDetector()
        result = await d.execute(
            _ctx(
                {
                    StateKeys.CONVERSATION_MODE: True,
                    StateKeys.CONVERSATION_ROUND: 5,
                    StateKeys.RAW_TOOL_CALLS: [{"name": "file_write"}],
                }
            )
        )
        assert result.state_updates[StateKeys.CONVERSATION_MODE] is False
        assert result.state_updates[StateKeys.CONVERSATION_ROUND] == 0
        # 不产生 wait（让 next_tool 路由接管）
        assert result.route_signal is None
        assert result.skip_remaining is False

    @pytest.mark.asyncio
    async def test_激活优先级高于tool_results检测(self) -> None:
        """已激活态下，即使 tool_results 又带激活信号，也走激活态分支。"""
        from plugin import ConversationModeDetector

        d = ConversationModeDetector()
        result = await d.execute(
            _ctx(
                {
                    StateKeys.CONVERSATION_MODE: True,
                    StateKeys.RAW_TOOL_CALLS: [],
                    StateKeys.TOOL_RESULTS: [_tool_result_with_conv("data")],
                }
            )
        )
        # 走的是 _handle_active_conversation，不是激活分支（round 递增而非重置为 1）
        assert StateKeys.CONVERSATION_MODE not in result.state_updates or \
            result.state_updates.get(StateKeys.CONVERSATION_MODE) is not False
        assert result.route_signal.route_type == "wait"
