# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: python-coverage
"""ConversationModeDetector 单元测试——对话模式激活检测。

覆盖：未激活时从 tool_results 检测激活信号、_extract_conversation_flag
多层字段提取。已激活态的对话循环判断（纯文本→wait / 工具调用→清状态）
由管道配置路由承载（autonomous.yaml post 链 next 分支），插件不再持有。
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
    async def test_tool_results带激活信号则激活并挂起(self) -> None:
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
        # 挂起经 state.suspended 表达（route_signal 全链零消费，引擎见
        # suspended 即停轮；per-run 键下轮派发自动复位）
        assert result.state_updates["suspended"] is True
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
# 已激活态：判断已搬管道配置路由，插件不再持有
# ============================================================

# autonomous.yaml post 链 next 分支承载已激活态两分支（DSL 表达式）：
# - when: "conversation_mode == True and raw_tool_calls == []" →
#   then: loop + set: {suspended: true}（挂起等待用户下一条消息）
# - when: "conversation_mode == True and raw_tool_calls != []" →
#   then: loop + set: {conversation_mode: false, conversation_round: 0}
# 表达式在引擎 condition.rs 求值（缺失键 → None → False），语义与原插件
# 布尔判断一致；DSL 求值属引擎车道（Rust 测试），此处不重复。
