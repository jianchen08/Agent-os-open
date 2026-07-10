"""工具相关提示统一走 tool_result — 单元测试。

验证两条契约：
1. level_guard 对只读探查工具（enhanced_search/file_read/list_directory）
   豁免 tool_ids 检查，所有层级 Agent 都能调用（修复 "L2 调 enhanced_search
   被拦" 的 bug）。
2. tool_call_guard 检测到重复工具调用时，提示作为 tool_result 返回
   （带 tool_call_id 的 role=tool 消息），不再注入 role=system 消息
   打断 assistant(tool_calls)→tool 序列。
"""

from __future__ import annotations

from typing import Any

import pytest

from pipeline.plugin import PluginContext
from pipeline.types import StateKeys


# ═══════════════════════════════════════════════════════════════
# level_guard：只读探查工具豁免
# ═══════════════════════════════════════════════════════════════


def _make_level_guard() -> Any:
    from plugins.input.level_guard.plugin import LevelGuardPlugin
    return LevelGuardPlugin()


def _make_level_guard_ctx(
    tool_calls: list[dict[str, Any]],
    tool_ids: list[str] | None,
    agent_level: str = "L2",
) -> PluginContext:
    state: dict[str, Any] = {
        StateKeys.CORE_TYPE: "tool_execute",
        StateKeys.RAW_TOOL_CALLS: tool_calls,
        StateKeys.AGENT_LEVEL: agent_level,
    }
    if tool_ids is not None:
        state["tool_ids"] = tool_ids
    return PluginContext(state=state, _services={})


class TestLevelGuardReadonlyProbeExempt:
    """改动：只读探查工具豁免 tool_ids 检查。"""

    @pytest.mark.asyncio
    async def test_enhanced_search_allowed_without_tool_ids_entry(self) -> None:
        """L2 agent 的 tool_ids 不含 enhanced_search，但应放行（只读工具豁免）。

        这是用户报的 bug 的核心回归：L2 调 enhanced_search 不再被拦。
        """
        plugin = _make_level_guard()
        ctx = _make_level_guard_ctx(
            [{"name": "enhanced_search", "args": {"query": "test"}}],
            tool_ids=["task_submit", "file_read"],  # 不含 enhanced_search
            agent_level="L2",
        )

        result = await plugin.execute(ctx)
        decision = result.state_updates.get("security.level_decision", {})

        assert decision.get("allowed") is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool_name", ["enhanced_search", "file_read", "read_file", "list_directory"])
    async def test_all_readonly_tools_exempt(self, tool_name: str) -> None:
        """所有只读探查工具在任意层级都放行。"""
        plugin = _make_level_guard()
        ctx = _make_level_guard_ctx(
            [{"name": tool_name, "args": {}}],
            tool_ids=[],  # 空白名单
            agent_level="L3",
        )

        result = await plugin.execute(ctx)
        decision = result.state_updates.get("security.level_decision", {})

        assert decision.get("allowed") is True

    @pytest.mark.asyncio
    async def test_write_tool_still_blocked_without_tool_ids(self) -> None:
        """写工具（如 file_write）不在只读豁免集，仍受 tool_ids 约束。"""
        plugin = _make_level_guard()
        ctx = _make_level_guard_ctx(
            [{"name": "file_write", "args": {"path": "/tmp/x"}}],
            tool_ids=["task_submit"],  # 不含 file_write
            agent_level="L2",
        )

        result = await plugin.execute(ctx)
        decision = result.state_updates.get("security.level_decision", {})

        assert decision.get("allowed") is False
        assert "file_write" in decision.get("reason", "")

    @pytest.mark.asyncio
    async def test_mixed_readonly_and_write_only_reports_write(self) -> None:
        """混合调用：只读工具放行，写工具被拦 → 整体 blocked，但只报写工具。"""
        plugin = _make_level_guard()
        ctx = _make_level_guard_ctx(
            [
                {"name": "enhanced_search", "args": {}},
                {"name": "delete_file", "args": {}},
            ],
            tool_ids=["task_submit"],
            agent_level="L2",
        )

        result = await plugin.execute(ctx)
        decision = result.state_updates.get("security.level_decision", {})

        assert decision.get("allowed") is False
        blocked = decision.get("blocked_tools", [])
        assert "delete_file" in blocked
        assert "enhanced_search" not in blocked  # 只读工具不被报为 blocked


# ═══════════════════════════════════════════════════════════════
# tool_call_guard：提示走 tool_result，不注入 system
# ═══════════════════════════════════════════════════════════════


def _make_tool_call_guard() -> Any:
    from plugins.input.tool_call_guard.plugin import ToolCallGuard
    return ToolCallGuard()


def _make_guard_ctx(
    tool_calls: list[dict[str, Any]],
    repeat_count: int = 0,
    last_signature: str = "",
    messages: list[dict[str, Any]] | None = None,
) -> PluginContext:
    state: dict[str, Any] = {
        StateKeys.CORE_TYPE: "tool_execute",
        StateKeys.RAW_TOOL_CALLS: tool_calls,
        "tool_call.repeat_count": repeat_count,
        "tool_call.last_signature": last_signature,
        "messages": messages if messages is not None else [],
    }
    return PluginContext(state=state, _services={})


class TestToolCallGuardReturnsToolResult:
    """改动：重复提示作为 tool_result 返回，不注入 system。"""

    @pytest.mark.asyncio
    async def test_repeat_injects_tool_result_not_system(self) -> None:
        """重复调用时注入的是 role=tool 消息（带 tool_call_id），不是 system。

        构造"上次签名相同"的状态，本次执行会判定为 repeat_count=1，触发软提示。
        """
        plugin = _make_tool_call_guard()
        tc = {"name": "file_read", "args": {"path": "/a"}, "id": "call_1"}
        sig = plugin._generate_signature([tc])
        # last_signature 与当前相同 → repeat_count 从 0 升到 1，触发软提示
        ctx = _make_guard_ctx([tc], repeat_count=0, last_signature=sig)

        result = await plugin.execute(ctx)

        messages = result.state_updates.get("messages", [])
        # 不应有 role=system 消息
        assert all(m.get("role") != "system" for m in messages)
        # 应有 role=tool 消息，带 tool_call_id
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        assert len(tool_msgs) >= 1
        assert tool_msgs[0]["tool_call_id"] == "call_1"
        assert "ToolCallGuard" in tool_msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_block_clears_raw_tool_calls_and_adds_tool_results(self) -> None:
        """超过软提示阈值（repeat>2）拦截时：清空 raw_tool_calls + 注入 tool 结果。"""
        plugin = _make_tool_call_guard()
        tc = {"name": "file_read", "args": {"path": "/a"}, "id": "call_2"}
        # 模拟已重复 3 次（repeat_count=2 时本次 +1 = 3，进入 block 分支）
        ctx = _make_guard_ctx([tc], repeat_count=2, last_signature="same_sig")
        # 让签名匹配，触发 repeat+1
        ctx.state["tool_call.last_signature"] = plugin._generate_signature([tc])
        ctx.state["tool_call.repeat_count"] = 2

        result = await plugin.execute(ctx)

        updates = result.state_updates
        assert updates.get(StateKeys.RAW_TOOL_CALLS) == []
        messages = updates.get("messages", [])
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["tool_call_id"] == "call_2"

    @pytest.mark.asyncio
    async def test_no_system_message_after_assistant_tool_calls(self) -> None:
        """回归核心契约：assistant(tool_calls) 后绝不出现独立 system 消息。"""
        plugin = _make_tool_call_guard()
        tc = {"name": "file_read", "args": {"path": "/a"}, "id": "call_3"}
        messages_before = [
            {"role": "user", "content": "问题"},
            {"role": "assistant", "tool_calls": [{"id": "call_3", "type": "function", "function": {"name": "file_read"}}]},
        ]
        ctx = _make_guard_ctx([tc], repeat_count=1, last_signature=plugin._generate_signature([tc]))
        ctx.state["messages"] = list(messages_before)

        result = await plugin.execute(ctx)
        messages = result.state_updates.get("messages", [])

        # 末尾不应该是 system
        assert messages[-1]["role"] != "system"
        # assistant(tool_calls) 仍保留
        assert any(m["role"] == "assistant" and m.get("tool_calls") for m in messages)
