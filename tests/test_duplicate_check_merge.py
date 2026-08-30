"""duplicate_check 消息注入契约 — 不打断 assistant(tool_calls)→tool 序列。

_inject_hint / _inject_warning 经 updates["messages"]={"_ops":[...]} 回传消息
修改：末尾 tool/assistant(system) 按 seq modify 合并 content（不新增独立
system 消息——system 插在 assistant(tool_calls) 与 tool 之间会断序列），
末尾 user/空 追加 user。引擎按 slot ops 三落点应用（缺 seq=append /
set(seq,msg)=modify / set(seq,null)=delete）。
"""

from __future__ import annotations

from tests._pipeline_plugin_path import add_plugin_dir

add_plugin_dir("output", "duplicate_check")

from typing import Any

import pytest
from pipeline.plugin import PluginContext
from pipeline.types import StateKeys

pytestmark = pytest.mark.unit


def _make_plugin() -> Any:
    add_plugin_dir("output", "duplicate_check")
    from plugin import DuplicateCheckPlugin
    return DuplicateCheckPlugin()


def _make_ctx(messages: list[dict[str, Any]]) -> PluginContext:
    return PluginContext(state={"messages": list(messages)}, _services={})


def _single_op(updates: dict[str, Any]) -> dict[str, Any]:
    ops = updates["messages"]["_ops"]
    assert len(ops) == 1
    return ops[0]


class TestMergeDoesNotBreakSequence:
    """合并式注入不打断 assistant(tool_calls)→tool 序列。"""

    def test_merge_into_tool_message(self) -> None:
        """末尾是 tool 消息时，提示按 seq modify 合并进 tool content（不新增 system）。"""
        plugin = _make_plugin()
        ctx = _make_ctx([
            {"role": "assistant", "seq": 0, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "f"}}]},
            {"role": "tool", "seq": 1, "tool_call_id": "c1", "content": "原始结果"},
        ])
        updates: dict[str, Any] = {}

        plugin._inject_hint(ctx, updates, "请勿重复调用")

        op = _single_op(updates)
        assert op["op"] == "set" and op["seq"] == 1  # 同 seq modify，不新增消息
        assert op["msg"]["role"] == "tool"  # 末尾仍是 tool（不是 system）
        assert "请勿重复调用" in op["msg"]["content"]  # content 被合并
        assert "原始结果" in op["msg"]["content"]

    def test_merge_into_assistant_with_tool_calls(self) -> None:
        """末尾是 assistant(tool_calls) 时，提示按 seq modify 合并进 assistant content。

        契约：不新增独立 system 消息（system 插在 assistant(tool_calls) 与
        tool 之间会断序列），提示须并入 assistant content。
        """
        plugin = _make_plugin()
        ctx = _make_ctx([
            {"role": "assistant", "seq": 0, "content": "我来调用工具", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "f"}}]},
        ])
        updates: dict[str, Any] = {}

        plugin._inject_hint(ctx, updates, "重复提醒")

        op = _single_op(updates)
        assert op["seq"] == 0  # modify，不新增
        assert op["msg"]["role"] == "assistant"
        assert op["msg"]["tool_calls"]  # tool_calls 保留
        assert "重复提醒" in op["msg"]["content"]

    def test_merge_into_system_message(self) -> None:
        """末尾是 system 时按 seq modify 合并进 system content。"""
        plugin = _make_plugin()
        ctx = _make_ctx([{"role": "system", "seq": 0, "content": "系统提示"}])
        updates: dict[str, Any] = {}
        plugin._inject_warning(ctx, updates, "警告")
        op = _single_op(updates)
        assert op["seq"] == 0 and op["msg"]["role"] == "system"
        assert "警告" in op["msg"]["content"]

    def test_empty_messages_appends_user(self) -> None:
        """messages 为空时产 append op（缺 seq=append 契约），role=user。"""
        plugin = _make_plugin()
        ctx = _make_ctx([])
        updates: dict[str, Any] = {}
        plugin._inject_hint(ctx, updates, "提示")
        op = _single_op(updates)
        assert op.get("seq") is None
        assert op["msg"]["role"] == "user"

    def test_trailing_user_appends_user(self) -> None:
        """末尾是 user 时产 append op，不 modify 原消息。"""
        plugin = _make_plugin()
        ctx = _make_ctx([{"role": "user", "seq": 0, "content": "hi"}])
        updates: dict[str, Any] = {}
        plugin._inject_hint(ctx, updates, "提示")
        op = _single_op(updates)
        assert op.get("seq") is None
        assert op["msg"]["role"] == "user"

    def test_no_standalone_system_after_assistant_tool_calls(self) -> None:
        """回归核心契约：assistant(tool_calls) 后绝不新增独立 system 消息 op。

        引擎按 ops 应用后序列里不得出现插在 tool_calls 与 tool 之间的 system。
        """
        plugin = _make_plugin()
        ctx = _make_ctx([
            {"role": "user", "seq": 0, "content": "问题"},
            {"role": "assistant", "seq": 1, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "f"}}]},
        ])
        updates: dict[str, Any] = {}

        plugin._inject_warning(ctx, updates, "拦截警告")

        op = _single_op(updates)
        assert op["msg"].get("role") != "system"  # 不新增独立 system
        assert op.get("seq") == 1 or op["msg"]["role"] == "assistant"  # assistant 保留（modify 合并）
        assert "拦截警告" in op["msg"]["content"]
