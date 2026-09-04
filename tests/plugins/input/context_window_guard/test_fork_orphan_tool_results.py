"""_build_fork_messages 孤儿 tool result 摘除测试。

契约：fork 消息队列发往压缩 LLM（与执行请求同一 MiniMax 严格校验），
任何 tool 消息的 tool_call_id 必须能在其前方、且中间未被 user/system
打断的 assistant(tool_calls) 中找到。state 历史遗留的孤儿（cancelled/
failed run 留下，或更早压缩块吞掉 assistant 后残留）不进 fork 载荷，
否则上游 400（tool result's tool id not found 2013）。

摘除语义与执行路径 llm_core normalizer Phase A 一致：
- assistant(tool_calls) → 期待集合 = 其 id 集；
- assistant 无 tool_calls / user / system → 期待集合清空；
- tool 的 tool_call_id 不在期待集合内 → 摘除。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


def _load_cw_plugin() -> ModuleType:
    """唯一名动态加载 plugin.py（防兄弟插件 sys.modules 缓存串扰）。"""
    path = (
        Path(__file__).resolve().parents[4]
        / "plugins" / "shared" / "pipeline" / "input" / "context_window_guard"
        / "plugin.py"
    )
    name = "_cw_guard_plugin_fork_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


plugin = _load_cw_plugin()

pytestmark = pytest.mark.unit


def _assistant(content: str = "doing", tc_ids: list[str] | None = None) -> dict[str, Any]:
    if tc_ids is None:
        return {"role": "assistant", "content": content}
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [
            {"id": tid, "type": "function", "function": {"name": "godot_x", "arguments": "{}"}}
            for tid in tc_ids
        ],
    }


def _tool(tc_id: str, content: str = "result") -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": tc_id, "name": "godot_x", "content": content}


def _build_fork(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compressor = plugin.ContextCompressor()
    return compressor._build_fork_messages(messages)


class TestForkStripsOrphanToolResults:
    def test_leading_orphan_tool_result_dropped(self) -> None:
        """压缩块吞掉 assistant 后残留的头部孤儿（2026-09-03 真实形态）。"""
        messages = [
            {"role": "user", "content": "hi"},
            _tool("call_orphan", "format: png"),
            _assistant("ok"),
        ]
        fork = _build_fork(messages)
        assert not any(
            m.get("role") == "tool" and m.get("tool_call_id") == "call_orphan" for m in fork
        )

    def test_mismatched_tool_call_id_dropped(self) -> None:
        messages = [
            _assistant("call fn", tc_ids=["call_a"]),
            _tool("call_b"),
        ]
        fork = _build_fork(messages)
        assert not any(m.get("role") == "tool" for m in fork)

    def test_paired_tool_results_preserved(self) -> None:
        """防误伤：合法配对按原顺序全保留。"""
        messages = [
            {"role": "user", "content": "go"},
            _assistant("call", tc_ids=["call_a", "call_b"]),
            _tool("call_a"),
            _tool("call_b"),
            _assistant("done"),
        ]
        fork = _build_fork(messages)
        tools = [m for m in fork if m.get("role") == "tool"]
        assert [t["tool_call_id"] for t in tools] == ["call_a", "call_b"]

    def test_user_message_breaks_pairing(self) -> None:
        """user 打断后到达的 tool result 视同孤儿（与执行路径语义一致）。"""
        messages = [
            _assistant("call", tc_ids=["call_a"]),
            {"role": "user", "content": "interrupt"},
            _tool("call_a"),
        ]
        fork = _build_fork(messages)
        assert not any(m.get("role") == "tool" for m in fork)

    def test_trailing_instruction_not_stripped(self) -> None:
        """防误伤：末尾压缩指令（user）原样保留。"""
        messages = [_assistant("call", tc_ids=["call_a"]), _tool("call_a")]
        fork = _build_fork(messages)
        assert fork[-1]["role"] == "user"
        assert fork[-1]["content"] == plugin.ContextCompressor.COMPACTION_INSTRUCTION

    def test_property_every_tool_result_paired(self) -> None:
        """性质断言：输出中每条 tool 消息的 id 都在当前期待集合内。"""
        seq = [
            _assistant("a", tc_ids=["c1"]),
            _tool("c1"),
            {"role": "user", "content": "u"},
            _tool("c2"),  # user 打断后的孤儿
            _assistant("b", tc_ids=["c3"]),
            _tool("c3"),
            _tool("c9"),  # id 不匹配孤儿
        ]
        fork = _build_fork(seq)
        expecting: set[str] = set()
        for m in fork:
            if m.get("role") == "assistant" and m.get("tool_calls"):
                expecting = {tc["id"] for tc in m["tool_calls"]}
            elif m.get("role") == "tool":
                assert m["tool_call_id"] in expecting, m
                expecting.discard(m["tool_call_id"])
            else:
                expecting = set()

    def test_original_messages_not_mutated(self) -> None:
        """摘除只作用于 fork 副本，入参列表不动（fork 复制契约）。"""
        messages = [
            _assistant("call", tc_ids=["call_a"]),
            _tool("call_a"),
            _tool("call_orphan"),
        ]
        snapshot = [dict(m) for m in messages]
        _build_fork(messages)
        assert messages == snapshot
