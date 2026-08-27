# @feature: FP-T07 llm api | @ci: python-coverage
"""llm_core _message_normalizer 边界路径补测——JSON 修复状态机/MiniMax Phase 2 重定位。

契约：配对 fail-closed（tool_call_id 精确匹配，绝不 positional 改写）与
MiniMax 专有修正（非首位 system→user、arguments JSON 修复/重置）由既有
test_llm_core_pairing.py / test_message_normalizer_format.py 覆盖；本文件补
term-missing 缺口：

- ``repair_json_string``：转义引号提取、单引号替换失败、注释剔除失败、
  截断修复状态机（字符串内转义闭合 / 回退最后完整字段 / 彻底失败）；
- ``_repair_truncation``：转义处理、_close_braces 转义/弹栈、步骤 1/3 失败；
- ``_normalize_tool_calls_in_messages``：混合结构修正保留标准 tc、非 dict 条目
  跳过 id remap；
- ``_validate_tool_call_pairing``：重放区 assistant 无 tool_calls / user 消息
  清空期望集合、新增区 assistant 无 tool_calls；
- MiniMax Phase 2：完整轮后 user 消息停止收集、失配 tool 提前停止、
  无 id assistant 的 intruder 重定位（user 原样 / 非 user 转 user）。

加载：importlib 唯一模块名装载（与既有 normalizer 测试同款）；配对缓存按
(provider, name, pipeline_id) 键控，fixture 前后清空隔离。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent

_MOD_NAME = "message_normalizer_edges_under_test"


def _load_module() -> Any:
    """加载 _message_normalizer.py（唯一模块名，进程内缓存）。"""
    if _MOD_NAME in sys.modules:
        return sys.modules[_MOD_NAME]
    module_path = _PLUGIN_DIR / "_message_normalizer.py"
    assert module_path.exists(), f"module missing at {module_path}"
    spec = importlib.util.spec_from_file_location(_MOD_NAME, module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MOD_NAME] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mod() -> Any:
    """被测模块；前后清空配对缓存，避免模块级缓存跨用例串扰。"""
    m = _load_module()
    m._pairing_validated_len.clear()
    yield m
    m._pairing_validated_len.clear()


def _assistant(call_ids: str | list[str], arguments: str = "{}") -> dict[str, Any]:
    ids = [call_ids] if isinstance(call_ids, str) else call_ids
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": cid, "type": "function", "function": {"name": "f", "arguments": arguments}}
            for cid in ids
        ],
    }


def _tool_result(call_id: str, content: str = "ok") -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


# ─────────────────── repair_json_string 边界 ───────────────────


class TestRepairJsonStringEdges:
    def test_escaped_quote_inside_extracted_object(self, mod) -> None:
        """提取对象时正确处理字符串内转义引号（不提前闭合字符串）。"""
        fixed = mod.repair_json_string('prefix {"a": "x\\"y", "b": 1} suffix')
        assert fixed == '{"a": "x\\"y", "b": 1}'
        assert json.loads(fixed) == {"a": 'x"y', "b": 1}

    def test_single_quote_replacement_failure_falls_through(self, mod) -> None:
        """单引号替换后仍不可解析 → 继续后续尝试，最终 None（不误报修复成功）。"""
        assert mod.repair_json_string("{'a': 1, 'b': }") is None

    def test_comment_removal_failure_falls_through(self, mod) -> None:
        """注释剔除后仍不可解析 → 返回 None。"""
        assert mod.repair_json_string('{"a": // comment\n 1, "b": }') is None

    def test_truncation_escaped_backslash_closed(self, mod) -> None:
        """截断在转义反斜杠处：去掉悬空反斜杠后闭合引号+括号。"""
        fixed = mod.repair_json_string('{"a": "x\\')
        assert fixed == '{"a": "x"}'
        assert json.loads(fixed) == {"a": "x"}

    def test_truncation_invalid_escape_returns_none(self, mod) -> None:
        """字符串内转义后仍非法（\\y）且无完整字段边界 → None（不误报修复成功）。"""
        assert mod.repair_json_string('{"a": "x\\y') is None

    def test_truncation_falls_back_to_last_complete_field(self, mod) -> None:
        """步骤 1/2 失败 → 回退最后完整字段（丢弃残缺尾部字段）。"""
        fixed = mod.repair_json_string('{"a": 1, "b": "x\\y')
        assert fixed == '{"a": 1}'
        assert json.loads(fixed) == {"a": 1}

    def test_truncation_close_braces_pop_and_last_field(self, mod) -> None:
        """_close_braces 弹栈 + 回退最后完整字段（嵌套对象截断）。"""
        fixed = mod.repair_json_string('{"a": {"b": 1}, "c": ')
        assert fixed == '{"a": {"b": 1}}'
        assert json.loads(fixed) == {"a": {"b": 1}}

    def test_truncation_step3_failure_returns_none(self, mod) -> None:
        """步骤 1/2/3 全部失败（最后完整字段也不可解析）→ None。"""
        assert mod.repair_json_string('{"a": , "b": "x\\y') is None


# ─────────────────── _normalize_tool_calls_in_messages 边界 ───────────────────


class TestNormalizeToolCallsEdges:
    def test_mixed_structure_fix_keeps_standard_tc(self, mod) -> None:
        """混合 tool_calls：标准 tc（type=function+function dict）原样保留。"""
        messages: list[dict[str, Any]] = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"name": "legacy", "args": "{}"},
                    {"id": "call_abc123", "type": "function", "function": {"name": "f", "arguments": "{}"}},
                ],
            }
        ]
        changed = mod.standardize_tool_calls_in_messages(messages)
        assert changed == [0]
        tcs = messages[0]["tool_calls"]
        assert len(tcs) == 2
        assert tcs[1] == {"id": "call_abc123", "type": "function", "function": {"name": "f", "arguments": "{}"}}
        assert tcs[0]["type"] == "function"  # legacy 被转换

    def test_non_dict_entry_skipped_in_id_remap(self, mod) -> None:
        """tool_calls 含非 dict 条目：结构检查跳过、id remap 循环跳过（不崩溃）。"""
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    "junk",
                    {"id": "call_abc123", "type": "function", "function": {"name": "f", "arguments": "{}"}},
                ],
            }
        ]
        changed = mod.standardize_tool_calls_in_messages(messages)
        assert changed == []
        assert messages[0]["tool_calls"][0] == "junk"  # 原样保留


# ─────────────────── _validate_tool_call_pairing 重放区边界 ───────────────────


class TestPairingReplayEdges:
    def test_replay_region_assistant_without_tool_calls_clears_expectation(self, mod) -> None:
        """重放区 assistant 无 tool_calls → 期望集合清空（后续 tool 视为孤儿）。"""
        first = [
            _assistant("call_aaa"),
            _tool_result("call_aaa"),
            {"role": "assistant", "content": "plain"},
            {"role": "user", "content": "q"},
        ]
        mod._validate_tool_call_pairing(
            first, "deepseek", "replay", pipeline_id="t-replay-clear"
        )
        # 增量：新增 assistant(tool_calls)+tool 配对；重放区覆盖 390/397 分支
        second = first + [_assistant("call_bbb"), _tool_result("call_bbb")]
        final = mod._validate_tool_call_pairing(
            second, "deepseek", "replay", pipeline_id="t-replay-clear"
        )
        assert len(final) == 6
        assert final[-1]["tool_call_id"] == "call_bbb"

    def test_new_messages_assistant_without_tool_calls_clears_expectation(self, mod) -> None:
        """新增区 assistant 无 tool_calls → 期望集合清空（后续孤儿 tool 被丢）。"""
        messages = [
            _assistant("call_aaa"),
            _tool_result("call_aaa"),
            {"role": "assistant", "content": "plain"},
            _tool_result("call_orphan"),
        ]
        final = mod._validate_tool_call_pairing(
            messages, "deepseek", "replay", pipeline_id="t-new-clear"
        )
        assert len(final) == 3  # 孤儿 tool result 被丢
        assert all(m.get("role") != "tool" or m.get("tool_call_id") != "call_orphan" for m in final)


# ─────────────────── MiniMax Phase 2 重定位 ───────────────────


class TestMiniMaxPhase2Relocation:
    def test_complete_round_followed_by_user_stops_collection(self, mod) -> None:
        """完整配对轮后 user 消息 → 停止收集（tool_group 已收齐，不偷后续消息）。"""
        out = mod.normalize_messages_for_provider(
            [
                _assistant("call_abc123"),
                _tool_result("call_abc123"),
                {"role": "user", "content": "next"},
            ],
            provider="minimax",
            name="reloc",
            pipeline_id="t-mx-complete",
        )
        assert [m["role"] for m in out] == ["assistant", "tool", "user"]

    def test_unmatched_tool_dropped_before_phase2(self, mod) -> None:
        """tool 结果先失配后匹配 → Phase A 已丢弃失配结果（fail-closed，不 positional 改写）。"""
        out = mod.normalize_messages_for_provider(
            [
                _assistant("call_aaa"),
                _tool_result("call_xxx"),
                _tool_result("call_aaa"),
            ],
            provider="minimax",
            name="reloc",
            pipeline_id="t-mx-unmatched",
        )
        # 失配 tool 在配对校验阶段被丢，Phase 2 只看到完整配对
        assert [m["role"] for m in out] == ["assistant", "tool"]
        assert out[1]["tool_call_id"] == "call_aaa"

    def test_intruder_user_relocated_as_is(self, mod) -> None:
        """无 id assistant 后 user 消息 → intruder 原样保留（role 已是 user）。"""
        no_id_tc = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"type": "function", "function": {"name": "f", "arguments": "{}"}}
            ],
        }
        out = mod.normalize_messages_for_provider(
            [no_id_tc, {"role": "user", "content": "intruder"}],
            provider="minimax",
            name="reloc",
            pipeline_id="t-mx-intruder-user",
        )
        assert [m["role"] for m in out] == ["assistant", "user"]
        assert out[1]["content"] == "intruder"

    def test_intruder_function_role_converted_to_user(self, mod) -> None:
        """无 id assistant 后 function 角色消息 → 转 user 并清不兼容字段。"""
        no_id_tc = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"type": "function", "function": {"name": "f", "arguments": "{}"}}
            ],
        }
        out = mod.normalize_messages_for_provider(
            [
                no_id_tc,
                {"role": "function", "name": "f", "content": "x", "tool_call_id": "c1"},
            ],
            provider="minimax",
            name="reloc",
            pipeline_id="t-mx-intruder-fn",
        )
        assert [m["role"] for m in out] == ["assistant", "user"]
        moved = out[1]
        assert moved["content"] == "x"
        assert "name" not in moved
        assert "tool_calls" not in moved
        assert "tool_call_id" not in moved
