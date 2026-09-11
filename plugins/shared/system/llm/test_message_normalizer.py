# @feature: FP-T07 llm api | @ci: python-coverage
"""llm_service _message_normalizer 单元测试（自 llm_core 测试域等价迁移）。

覆盖：

- repair_json_string：markdown 包裹提取、嵌套对象提取、尾逗号/单引号修复、
  截断修复状态机（字符串内截断闭合 / 仅缺右括号 / 回退最后完整字段）、
  注释剔除、彻底无法修复返回 None、转义边界；
- _is_valid_tool_call_id：call_<hex> 标准格式判定；
- standardize_tool_calls_in_messages：旧结构 → OpenAI 格式、非标准 id remap
  并同步 tool 消息、无改写返回空列表；
- normalize_messages_for_provider：配对 fail-closed（失配 tool result 整轮
  丢弃，绝不 positional 改写）、非 minimax 直通、MiniMax Phase 1/2/3。

加载：importlib 唯一模块名装载（与本目录其余测试同款，防裸名跨插件串扰）。
被测实现无状态全量扫描，用例间无需缓存清理。
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

_MOD_NAME = "llm_service_message_normalizer_under_test"


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
    return _load_module()


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


class TestRepairJsonString:
    """JSON 修复管线：每一级策略命中后必须产出可解析的 JSON。"""

    def test_valid_json_returned_stripped(self, mod) -> None:
        assert mod.repair_json_string('  {"a": 1}  ') == '{"a": 1}'

    def test_empty_or_non_string_returns_none(self, mod) -> None:
        assert mod.repair_json_string("") is None
        assert mod.repair_json_string(None) is None  # type: ignore[arg-type]
        assert mod.repair_json_string(123) is None  # type: ignore[arg-type]

    def test_markdown_code_fence_unwrapped(self, mod) -> None:
        assert mod.repair_json_string('```json\n{"a": 1}\n```') == '{"a": 1}'

    def test_embedded_object_extracted_from_surrounding_text(self, mod) -> None:
        # 中转站在 JSON 前后加说明文字；必须提取第一个完整 {...}
        assert mod.repair_json_string('prefix {"a": {"b": 2}} suffix') == '{"a": {"b": 2}}'

    def test_escaped_quote_inside_extracted_object(self, mod) -> None:
        """提取对象时正确处理字符串内转义引号（不提前闭合字符串）。"""
        fixed = mod.repair_json_string('prefix {"a": "x\\"y", "b": 1} suffix')
        assert fixed == '{"a": "x\\"y", "b": 1}'
        assert json.loads(fixed) == {"a": 'x"y', "b": 1}

    def test_trailing_commas_removed(self, mod) -> None:
        fixed = mod.repair_json_string('{"a": [1, 2,],}')
        assert fixed == '{"a": [1, 2]}'
        json.loads(fixed)

    def test_single_quotes_replaced(self, mod) -> None:
        fixed = mod.repair_json_string("{'a': 1}")
        assert fixed == '{"a": 1}'
        json.loads(fixed)

    def test_single_quote_replacement_failure_falls_through(self, mod) -> None:
        """单引号替换后仍不可解析 → 继续后续尝试，最终 None（不误报修复成功）。"""
        assert mod.repair_json_string("{'a': 1, 'b': }") is None

    def test_truncated_string_value_closed(self, mod) -> None:
        # 截断发生在字符串值内部：闭合引号 + 补右括号，保留半截字段
        fixed = mod.repair_json_string('{"a": 1, "b": "hello wor')
        assert fixed == '{"a": 1, "b": "hello wor"}'
        assert json.loads(fixed)["a"] == 1

    def test_missing_closing_braces_only(self, mod) -> None:
        fixed = mod.repair_json_string('{"a": {"b": [1, 2]')
        assert fixed == '{"a": {"b": [1, 2]}}'
        assert json.loads(fixed)["a"]["b"] == [1, 2]

    def test_truncation_falls_back_to_last_complete_field(self, mod) -> None:
        # 值未写完（非字符串内）：闭合括号仍不可解析 → 丢弃残缺尾部字段
        fixed = mod.repair_json_string('{"a": 1, "b": unquoted')
        assert fixed == '{"a": 1}'

    def test_truncation_escaped_backslash_closed(self, mod) -> None:
        """截断在转义反斜杠处：去掉悬空反斜杠后闭合引号+括号。"""
        fixed = mod.repair_json_string('{"a": "x\\')
        assert fixed == '{"a": "x"}'
        assert json.loads(fixed) == {"a": "x"}

    def test_truncation_invalid_escape_returns_none(self, mod) -> None:
        """字符串内转义后仍非法（\\y）且无完整字段边界 → None（不误报修复成功）。"""
        assert mod.repair_json_string('{"a": "x\\y') is None

    def test_truncation_close_braces_pop_and_last_field(self, mod) -> None:
        """_close_braces 弹栈 + 回退最后完整字段（嵌套对象截断）。"""
        fixed = mod.repair_json_string('{"a": {"b": 1}, "c": ')
        assert fixed == '{"a": {"b": 1}}'
        assert json.loads(fixed) == {"a": {"b": 1}}

    def test_truncation_step3_failure_returns_none(self, mod) -> None:
        """步骤 1/2/3 全部失败（最后完整字段也不可解析）→ None。"""
        assert mod.repair_json_string('{"a": , "b": "x\\y') is None

    def test_line_comments_removed(self, mod) -> None:
        fixed = mod.repair_json_string('{\n  // note\n  "a": 1\n}')
        assert fixed is not None
        assert json.loads(fixed) == {"a": 1}

    def test_comment_removal_failure_falls_through(self, mod) -> None:
        """注释剔除后仍不可解析 → 返回 None。"""
        assert mod.repair_json_string('{"a": // comment\n 1, "b": }') is None

    def test_unrepairable_garbage_returns_none(self, mod) -> None:
        assert mod.repair_json_string("not json at all") is None

    def test_truncated_realistic_arguments_repaired(self, mod) -> None:
        # 真实事故形态：流式截断的 tool_call arguments
        fixed = mod.repair_json_string('{"path": "/tmp/f')
        assert fixed == '{"path": "/tmp/f"}'
        assert json.loads(fixed)["path"] == "/tmp/f"


class TestIsValidToolCallId:
    """call_<hex> 标准格式判定（normalize id remap 的依据）。"""

    @pytest.mark.parametrize(
        ("tc_id", "expected"),
        [
            ("call_abc123", True),
            ("call_123", True),
            ("call_", False),  # 无 hex 部分
            ("call_ZZZ", False),  # 非 hex 字符
            ("call_function_read_1", False),  # 函数式命名（含下划线/非 hex）
            ("", False),
            (None, False),
            (123, False),  # type: ignore[arg-type]
        ],
    )
    def test_id_validity(self, mod, tc_id, expected) -> None:
        assert mod._is_valid_tool_call_id(tc_id) is expected


class TestStandardizeToolCalls:
    """tool_calls 结构标准化：旧结构转换 + 非标准 id remap 同步。"""

    def test_standard_format_untouched(self, mod) -> None:
        messages = [_assistant("call_abc123")]
        snapshot = json.loads(json.dumps(messages))
        changed = mod.standardize_tool_calls_in_messages(messages)
        assert changed == []
        assert messages == snapshot

    def test_legacy_structure_normalized(self, mod) -> None:
        messages: list[dict[str, Any]] = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"name": "read_file", "args": {"p": 1}}],
            }
        ]
        changed = mod.standardize_tool_calls_in_messages(messages)
        assert changed == [0]
        (tc,) = messages[0]["tool_calls"]
        assert tc["type"] == "function"
        assert isinstance(tc["function"], dict)
        assert tc["function"]["name"] == "read_file"
        # args 按原样搬入 arguments（不序列化）
        assert tc["function"]["arguments"] == {"p": 1}
        # 补生成的 id 必须是合法标准格式
        assert tc["id"].startswith("call_")
        assert mod._is_valid_tool_call_id(tc["id"])

    def test_legacy_arguments_key_fallback(self, mod) -> None:
        messages: list[dict[str, Any]] = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"name": "f", "arguments": '{"x": 1}'}],
            }
        ]
        changed = mod.standardize_tool_calls_in_messages(messages)
        assert changed == [0]
        assert messages[0]["tool_calls"][0]["function"]["arguments"] == '{"x": 1}'

    def test_nonstandard_id_remap_syncs_tool_message(self, mod) -> None:
        messages = [
            _assistant("call_abc123", arguments="{}"),
            _tool_result("call_abc123"),
        ]
        # 直接构造非标准 id（绕过 _assistant 的标准格式）验证 remap 同步
        messages[0]["tool_calls"][0]["id"] = "call_function_read_1"
        messages[1]["tool_call_id"] = "call_function_read_1"

        changed = mod.standardize_tool_calls_in_messages(messages)
        assert changed == [0, 1]  # assistant 与 tool 消息都被改写
        new_id = messages[0]["tool_calls"][0]["id"]
        assert mod._is_valid_tool_call_id(new_id)
        assert new_id != "call_function_read_1"
        # 配对一致：tool 消息同步指向新 id
        assert messages[1]["tool_call_id"] == new_id

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

    def test_non_list_tool_calls_ignored(self, mod) -> None:
        messages = [{"role": "assistant", "content": "", "tool_calls": "junk"}]
        changed = mod.standardize_tool_calls_in_messages(messages)
        assert changed == []
        assert messages[0]["tool_calls"] == "junk"

    def test_non_dict_entries_dropped_during_structure_fix(self, mod) -> None:
        messages: list[dict[str, Any]] = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": ["junk", {"name": "f"}],
            }
        ]
        changed = mod.standardize_tool_calls_in_messages(messages)
        assert changed == [0]
        tcs = messages[0]["tool_calls"]
        assert len(tcs) == 1  # 非 dict 条目被剔除
        assert tcs[0]["function"]["name"] == "f"

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


class TestToolCallPairingFailsClosed:
    """tool_call_id 失配一律丢弃，绝不 positional 改写（2026-08-22 裁决）。"""

    def test_unknown_id_dropped_not_rewritten(self, mod) -> None:
        """未知 id 的结果被丢弃且不被改写为待配对 id。"""
        messages = [
            _assistant("call_aaa"),
            _tool_result("call_aaa"),
            _assistant("call_bbb"),
            _tool_result("call_yyy", "B 调用的产出"),
        ]
        final = mod._validate_tool_call_pairing(
            messages, "deepseek", "pairing-fails-closed-1"
        )
        # 完好的第一轮保留；失配轮整轮清理（Phase B）——绝不 positional 改写
        assert len(final) == 2, f"失配轮应整轮丢弃，实际保留: {final}"
        assert final[0]["role"] == "assistant"
        assert "B 调用的产出" not in str(final)

    def test_unknown_id_dropped_when_other_results_still_pending(self, mod) -> None:
        """失配结果不消耗期望集合（call_b 的结果仍待配对）。"""
        messages = [
            _assistant("call_a"),
            _tool_result("call_a"),
            _assistant("call_b"),
            _tool_result("call_b"),
            _assistant("call_c"),
            _tool_result("call_zzz"),
        ]
        final = mod._validate_tool_call_pairing(
            messages, "deepseek", "pairing-fails-closed-2"
        )
        # 契约：失配结果整轮丢弃（不改写为 call_c 并入），期望集合不被消耗
        assert len(final) == 4, f"失配结果应被丢弃且不消耗期望集合，实际: {final}"
        assert all(
            m.get("role") != "tool" or m.get("tool_call_id") != "call_zzz" for m in final
        )

    def test_exact_pair_kept(self, mod) -> None:
        """精确匹配路径不受影响（防过度删除）。"""
        messages = [
            _assistant("call_a"),
            _tool_result("call_a"),
        ]
        final = mod._validate_tool_call_pairing(
            messages, "deepseek", "pairing-fails-closed-3"
        )
        assert len(final) == 2
        assert final[1]["tool_call_id"] == "call_a"

    def test_orphan_result_dropped(self, mod) -> None:
        """无前置 assistant 的孤儿结果 → 丢弃（既有语义回归护栏）。"""
        messages = [_tool_result("call_orphan")]
        final = mod._validate_tool_call_pairing(
            messages, "deepseek", "pairing-fails-closed-4"
        )
        assert final == []

    def test_assistant_without_tool_calls_clears_expectation(self, mod) -> None:
        """assistant 无 tool_calls → 期望集合清空（后续孤儿 tool 被丢）。"""
        messages = [
            _assistant("call_aaa"),
            _tool_result("call_aaa"),
            {"role": "assistant", "content": "plain"},
            _tool_result("call_orphan"),
        ]
        final = mod._validate_tool_call_pairing(
            messages, "deepseek", "pairing-replay"
        )
        assert len(final) == 3  # 孤儿 tool result 被丢
        assert all(m.get("role") != "tool" or m.get("tool_call_id") != "call_orphan" for m in final)

    def test_idempotent_full_scan(self, mod) -> None:
        """性质断言：normalize 幂等——对已清理的历史再扫描不产生二次变化。"""
        sick = [
            _tool_result("call_orphan"),
            _assistant("call_aaa"),
            _tool_result("call_aaa"),
            _assistant("call_bbb"),  # 无结果的半轮
        ]
        once = mod._validate_tool_call_pairing(sick, "deepseek", "idempotent")
        twice = mod._validate_tool_call_pairing(once, "deepseek", "idempotent")
        assert twice == once


class TestNormalizeMessagesForProvider:
    """按 provider 的消息修正：非 minimax 直通 + MiniMax 专有转换。"""

    def test_non_minimax_keeps_nonfirst_system(self, mod) -> None:
        out = mod.normalize_messages_for_provider(
            [
                {"role": "system", "content": "first"},
                {"role": "system", "content": "second"},
            ],
            provider="deepseek",
            name="fmt",
        )
        assert [m["role"] for m in out] == ["system", "system"]

    def test_non_minimax_still_applies_pairing(self, mod) -> None:
        out = mod.normalize_messages_for_provider(
            [_tool_result("call_zzz")],
            provider="deepseek",
            name="fmt",
        )
        assert out == []  # 孤儿 tool result 被配对校验丢弃

    def test_minimax_nonfirst_system_converted_to_user(self, mod) -> None:
        out = mod.normalize_messages_for_provider(
            [
                {"role": "system", "content": "first"},
                {"role": "system", "content": "second", "name": "x"},
            ],
            provider="minimax",
            name="fmt",
        )
        assert out[0]["role"] == "system"  # 首位 system 保留
        assert out[1]["role"] == "user"  # 非首位转换
        assert "name" not in out[1]  # name 字段剔除

    def test_minimax_user_name_removed(self, mod) -> None:
        out = mod.normalize_messages_for_provider(
            [{"role": "user", "name": "bob", "content": "hi"}],
            provider="minimax",
            name="fmt",
        )
        assert out[0]["role"] == "user"
        assert "name" not in out[0]
        assert out[0]["content"] == "hi"

    def test_minimax_tool_content_nul_stripped(self, mod) -> None:
        out = mod.normalize_messages_for_provider(
            [_assistant("call_abc123"), _tool_result("call_abc123", "a\x00b")],
            provider="minimax",
            name="fmt",
        )
        assert out[1]["content"] == "ab"

    def test_minimax_tool_content_truncated_at_8000(self, mod) -> None:
        out = mod.normalize_messages_for_provider(
            [_assistant("call_abc123"), _tool_result("call_abc123", "x" * 9000)],
            provider="minimax",
            name="fmt",
        )
        content = out[1]["content"]
        assert content == "x" * 8000 + "\n...[truncated]"
        assert len(content) == 8015

    def test_minimax_tool_content_under_limit_untouched(self, mod) -> None:
        out = mod.normalize_messages_for_provider(
            [_assistant("call_abc123"), _tool_result("call_abc123", "x" * 100)],
            provider="minimax",
            name="fmt",
        )
        assert out[1]["content"] == "x" * 100

    def test_minimax_arguments_non_string_reset(self, mod) -> None:
        # dict 型 arguments（上游偶发）→ 重置为 "{}"
        msg = _assistant("call_abc123")
        msg["tool_calls"][0]["function"]["arguments"] = {"a": 1}
        out = mod.normalize_messages_for_provider(
            [msg, _tool_result("call_abc123")],
            provider="minimax",
            name="fmt",
        )
        assert out[0]["tool_calls"][0]["function"]["arguments"] == "{}"

    def test_minimax_arguments_empty_string_reset(self, mod) -> None:
        msg = _assistant("call_abc123", arguments="")
        out = mod.normalize_messages_for_provider(
            [msg, _tool_result("call_abc123")],
            provider="minimax",
            name="fmt",
        )
        assert out[0]["tool_calls"][0]["function"]["arguments"] == "{}"

    def test_minimax_arguments_trailing_comma_repaired(self, mod) -> None:
        msg = _assistant("call_abc123", arguments='{"a": 1,}')
        out = mod.normalize_messages_for_provider(
            [msg, _tool_result("call_abc123")],
            provider="minimax",
            name="fmt",
        )
        fixed = out[0]["tool_calls"][0]["function"]["arguments"]
        assert json.loads(fixed) == {"a": 1}

    def test_minimax_arguments_truncated_json_repaired(self, mod) -> None:
        # 契约：流式截断的 arguments 必须可解析且保留完整字段
        msg = _assistant("call_abc123", arguments='{"path": "/tmp/f')
        out = mod.normalize_messages_for_provider(
            [msg, _tool_result("call_abc123")],
            provider="minimax",
            name="fmt",
        )
        fixed = out[0]["tool_calls"][0]["function"]["arguments"]
        assert json.loads(fixed) == {"path": "/tmp/f"}

    def test_minimax_arguments_unrepairable_reset(self, mod) -> None:
        msg = _assistant("call_abc123", arguments="definitely not json")
        out = mod.normalize_messages_for_provider(
            [msg, _tool_result("call_abc123")],
            provider="minimax",
            name="fmt",
        )
        assert out[0]["tool_calls"][0]["function"]["arguments"] == "{}"


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
        )
        assert [m["role"] for m in out] == ["assistant", "user"]
        moved = out[1]
        assert moved["content"] == "x"
        assert "name" not in moved
        assert "tool_calls" not in moved
        assert "tool_call_id" not in moved
