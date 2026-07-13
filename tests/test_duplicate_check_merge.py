"""duplicate_check 消息注入修复 — 不打断 assistant(tool_calls)→tool 序列。

验证改动：_inject_hint / _inject_warning 不再追加独立 system 消息，
而是合并进末尾消息 content（参照 llm_error_recovery 范本）。

修复前：往 messages 追加 role=system，在 llm_call 阶段会插到
assistant(tool_calls) 之后、tool 之前 → 断序列 → 引擎中断。
修复后：合并进末尾 tool/assistant content，序列保持完整。
"""

from __future__ import annotations

from typing import Any

import pytest

from pipeline.plugin import PluginContext
from pipeline.types import StateKeys


def _make_plugin() -> Any:
    from plugins.output.duplicate_check.plugin import DuplicateCheckPlugin
    return DuplicateCheckPlugin()


def _make_ctx(messages: list[dict[str, Any]]) -> PluginContext:
    return PluginContext(state={"messages": list(messages)}, _services={})


class TestMergeDoesNotBreakSequence:
    """合并式注入不打断 assistant(tool_calls)→tool 序列。"""

    def test_merge_into_tool_message(self) -> None:
        """末尾是 tool 消息时，提示合并进 tool content（不新增 system）。"""
        plugin = _make_plugin()
        ctx = _make_ctx([
            {"role": "assistant", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "f"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "原始结果"},
        ])
        original_len = len(ctx.state["messages"])

        plugin._inject_hint(ctx, "请勿重复调用")

        msgs = ctx.state["messages"]
        # 不新增消息（仍是 2 条）
        assert len(msgs) == original_len
        # 末尾仍是 tool（不是 system）
        assert msgs[-1]["role"] == "tool"
        # content 被合并
        assert "请勿重复调用" in msgs[-1]["content"]
        assert "原始结果" in msgs[-1]["content"]

    def test_merge_into_assistant_with_tool_calls(self) -> None:
        """末尾是 assistant(tool_calls) 时，提示合并进 assistant content。

        关键：这是改动前会断序列的场景（system 插在 assistant(tool_calls)
        之后）。修复后合并进 assistant，不新增消息。
        """
        plugin = _make_plugin()
        ctx = _make_ctx([
            {"role": "assistant", "content": "我来调用工具", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "f"}}]},
        ])
        original_len = len(ctx.state["messages"])

        plugin._inject_hint(ctx, "重复提醒")

        msgs = ctx.state["messages"]
        assert len(msgs) == original_len  # 不新增
        assert msgs[-1]["role"] == "assistant"
        assert msgs[-1]["tool_calls"]  # tool_calls 保留
        assert "重复提醒" in msgs[-1]["content"]

    def test_merge_into_system_message(self) -> None:
        """末尾是 system 时合并进 system content。"""
        plugin = _make_plugin()
        ctx = _make_ctx([{"role": "system", "content": "系统提示"}])
        plugin._inject_warning(ctx, "警告")
        assert ctx.state["messages"][-1]["role"] == "system"
        assert "警告" in ctx.state["messages"][-1]["content"]

    def test_empty_messages_appends_user(self) -> None:
        """messages 为空时追加 user（无配对问题）。"""
        plugin = _make_plugin()
        ctx = _make_ctx([])
        plugin._inject_hint(ctx, "提示")
        assert len(ctx.state["messages"]) == 1
        assert ctx.state["messages"][0]["role"] == "user"

    def test_trailing_user_appends_user(self) -> None:
        """末尾是 user 时追加 user。"""
        plugin = _make_plugin()
        ctx = _make_ctx([{"role": "user", "content": "hi"}])
        plugin._inject_hint(ctx, "提示")
        msgs = ctx.state["messages"]
        assert len(msgs) == 2
        assert msgs[-1]["role"] == "user"

    def test_no_standalone_system_after_assistant_tool_calls(self) -> None:
        """回归核心契约：assistant(tool_calls) 后绝不出现独立 system 消息。

        这是本次修复的目标——确保引擎不会再因注入 system 断序列。
        """
        plugin = _make_plugin()
        ctx = _make_ctx([
            {"role": "user", "content": "问题"},
            {"role": "assistant", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "f"}}]},
        ])

        plugin._inject_warning(ctx, "拦截警告")

        msgs = ctx.state["messages"]
        # 末尾消息不得是独立 system
        assert msgs[-1]["role"] != "system"
        # assistant(tool_calls) 仍保留
        assert any(
            m["role"] == "assistant" and m.get("tool_calls")
            for m in msgs
        )
