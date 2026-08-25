"""llm_core _message_normalizer 消息格式修正测试——JSON 修复/结构标准化/MiniMax 适配。

覆盖 test_llm_core_pairing.py（配对 fail-closed）之外的路径：

- repair_json_string：markdown 包裹提取、嵌套对象提取、尾逗号/单引号修复、
  截断修复状态机（字符串内截断闭合 / 仅缺右括号 / 回退最后完整字段）、
  注释剔除、彻底无法修复返回 None；
- _is_valid_tool_call_id：call_<hex> 标准格式判定；
- standardize_tool_calls_in_messages：旧结构 → OpenAI 格式、非标准 id remap
  并同步 tool 消息、无改写返回空列表；
- normalize_messages_for_provider：非 minimax 直通、MiniMax Phase 1
  （非首位 system→user、name 剔除、tool 内容 NUL 清理与 8000 截断、
  arguments 非法 JSON 修复/重置）；
- reset_pairing_cache 四种清缓存口径；
- 配对增量缓存：无新增直接返回、列表重建（指纹失配）强制全量扫描、
  minimax 绕过增量缓存。

加载：与 test_llm_core_pairing.py 相同的 importlib 唯一模块名装载（0.2
装配语义）；配对缓存按 (provider, name, pipeline_id) 键控，测试用唯一
pipeline_id + fixture 清缓存隔离。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = (
    Path(__file__).resolve().parents[4]
    / "plugins" / "shared" / "pipeline" / "core" / "llm_core"
)

_MOD_NAME = "message_normalizer_format_under_test"


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

    def test_trailing_commas_removed(self, mod) -> None:
        fixed = mod.repair_json_string('{"a": [1, 2,],}')
        assert fixed == '{"a": [1, 2]}'
        json.loads(fixed)

    def test_single_quotes_replaced(self, mod) -> None:
        fixed = mod.repair_json_string("{'a': 1}")
        assert fixed == '{"a": 1}'
        json.loads(fixed)

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

    def test_line_comments_removed(self, mod) -> None:
        fixed = mod.repair_json_string('{\n  // note\n  "a": 1\n}')
        assert fixed is not None
        assert json.loads(fixed) == {"a": 1}

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
        messages = [
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
        assert tc["function"]["name"] == "read_file"
        # args 按原样搬入 arguments（不序列化）
        assert tc["function"]["arguments"] == {"p": 1}
        # 补生成的 id 必须是合法标准格式
        assert tc["id"].startswith("call_")
        assert mod._is_valid_tool_call_id(tc["id"])

    def test_legacy_arguments_key_fallback(self, mod) -> None:
        messages = [
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

    def test_non_list_tool_calls_ignored(self, mod) -> None:
        messages = [{"role": "assistant", "content": "", "tool_calls": "junk"}]
        changed = mod.standardize_tool_calls_in_messages(messages)
        assert changed == []
        assert messages[0]["tool_calls"] == "junk"

    def test_non_dict_entries_dropped_during_structure_fix(self, mod) -> None:
        messages = [
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


class TestMessageFingerprint:
    """消息指纹：重建检测的数据基础。"""

    def test_string_content_truncated_at_80_chars(self, mod) -> None:
        a = {"role": "user", "content": "a" * 80 + "X"}
        b = {"role": "user", "content": "a" * 80 + "Y"}
        assert mod._message_fingerprint(a) == mod._message_fingerprint(b)

    def test_non_string_content_serialized(self, mod) -> None:
        a = {"role": "u", "content": {"k": [1, 2]}}
        b = {"role": "u", "content": {"k": [1, 3]}}
        assert mod._message_fingerprint(a) != mod._message_fingerprint(b)
        assert mod._message_fingerprint(a) == mod._message_fingerprint({"role": "u", "content": {"k": [1, 2]}})

    def test_role_and_tool_call_id_included(self, mod) -> None:
        base = {"role": "user", "content": "same"}
        tool_msg = {"role": "tool", "tool_call_id": "call_1", "content": "same"}
        assert mod._message_fingerprint(base) != mod._message_fingerprint(tool_msg)


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
            pipeline_id="t-ds-keep-sys",
        )
        assert [m["role"] for m in out] == ["system", "system"]

    def test_non_minimax_still_applies_pairing(self, mod) -> None:
        out = mod.normalize_messages_for_provider(
            [_tool_result("call_zzz")],
            provider="deepseek",
            name="fmt",
            pipeline_id="t-ds-orphan",
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
            pipeline_id="t-mx-sys",
        )
        assert out[0]["role"] == "system"  # 首位 system 保留
        assert out[1]["role"] == "user"  # 非首位转换
        assert "name" not in out[1]  # name 字段剔除

    def test_minimax_user_name_removed(self, mod) -> None:
        out = mod.normalize_messages_for_provider(
            [{"role": "user", "name": "bob", "content": "hi"}],
            provider="minimax",
            name="fmt",
            pipeline_id="t-mx-name",
        )
        assert out[0]["role"] == "user"
        assert "name" not in out[0]
        assert out[0]["content"] == "hi"

    def test_minimax_tool_content_nul_stripped(self, mod) -> None:
        out = mod.normalize_messages_for_provider(
            [_assistant("call_abc123"), _tool_result("call_abc123", "a\x00b")],
            provider="minimax",
            name="fmt",
            pipeline_id="t-mx-nul",
        )
        assert out[1]["content"] == "ab"

    def test_minimax_tool_content_truncated_at_8000(self, mod) -> None:
        out = mod.normalize_messages_for_provider(
            [_assistant("call_abc123"), _tool_result("call_abc123", "x" * 9000)],
            provider="minimax",
            name="fmt",
            pipeline_id="t-mx-trunc",
        )
        content = out[1]["content"]
        assert content == "x" * 8000 + "\n...[truncated]"
        assert len(content) == 8015

    def test_minimax_tool_content_under_limit_untouched(self, mod) -> None:
        out = mod.normalize_messages_for_provider(
            [_assistant("call_abc123"), _tool_result("call_abc123", "x" * 100)],
            provider="minimax",
            name="fmt",
            pipeline_id="t-mx-keep",
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
            pipeline_id="t-mx-dictarg",
        )
        assert out[0]["tool_calls"][0]["function"]["arguments"] == "{}"

    def test_minimax_arguments_empty_string_reset(self, mod) -> None:
        msg = _assistant("call_abc123", arguments="")
        out = mod.normalize_messages_for_provider(
            [msg, _tool_result("call_abc123")],
            provider="minimax",
            name="fmt",
            pipeline_id="t-mx-emptyarg",
        )
        assert out[0]["tool_calls"][0]["function"]["arguments"] == "{}"

    def test_minimax_arguments_trailing_comma_repaired(self, mod) -> None:
        msg = _assistant("call_abc123", arguments='{"a": 1,}')
        out = mod.normalize_messages_for_provider(
            [msg, _tool_result("call_abc123")],
            provider="minimax",
            name="fmt",
            pipeline_id="t-mx-fixarg",
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
            pipeline_id="t-mx-truncarg",
        )
        fixed = out[0]["tool_calls"][0]["function"]["arguments"]
        assert json.loads(fixed) == {"path": "/tmp/f"}

    def test_minimax_arguments_unrepairable_reset(self, mod) -> None:
        msg = _assistant("call_abc123", arguments="definitely not json")
        out = mod.normalize_messages_for_provider(
            [msg, _tool_result("call_abc123")],
            provider="minimax",
            name="fmt",
            pipeline_id="t-mx-badarg",
        )
        assert out[0]["tool_calls"][0]["function"]["arguments"] == "{}"


class TestResetPairingCache:
    """reset_pairing_cache 四种口径：全清 / provider / provider:name / 精确 key。"""

    def _seed(self, mod) -> None:
        mod._pairing_validated_len.update(
            {
                "deepseek:n1:p1": (2, "fp1"),
                "deepseek:n2:p1": (3, "fp2"),
                "minimax:n1:p1": (4, "fp3"),
            }
        )

    def test_reset_all(self, mod) -> None:
        self._seed(mod)
        mod.reset_pairing_cache()
        assert mod._pairing_validated_len == {}

    def test_reset_by_provider(self, mod) -> None:
        self._seed(mod)
        mod.reset_pairing_cache("deepseek")
        assert set(mod._pairing_validated_len) == {"minimax:n1:p1"}

    def test_reset_by_provider_name(self, mod) -> None:
        self._seed(mod)
        mod.reset_pairing_cache("deepseek", "n1")
        assert set(mod._pairing_validated_len) == {"deepseek:n2:p1", "minimax:n1:p1"}

    def test_reset_exact_key(self, mod) -> None:
        self._seed(mod)
        mod.reset_pairing_cache("deepseek", "n1", pipeline_id="p1")
        assert set(mod._pairing_validated_len) == {"deepseek:n2:p1", "minimax:n1:p1"}


class TestPairingIncrementalCache:
    """增量配对缓存语义：短路、重建检测、minimax 绕过。"""

    def test_no_new_messages_returns_same_list(self, mod) -> None:
        messages = [{"role": "user", "content": "q"}]
        mod._validate_tool_call_pairing(
            messages, "deepseek", "cache", pipeline_id="t-cache-same"
        )
        again = mod._validate_tool_call_pairing(
            messages, "deepseek", "cache", pipeline_id="t-cache-same"
        )
        assert again is messages  # 无新增 → 直接返回原列表

    def test_rebuilt_list_fingerprint_mismatch_forces_full_rescan(self, mod) -> None:
        # 第一轮建立缓存
        first = [_assistant("call_aaa"), _tool_result("call_aaa")]
        mod._validate_tool_call_pairing(
            first, "deepseek", "cache", pipeline_id="t-cache-rebuild"
        )
        # 消息列表整体重建（同数量、不同内容且含孤儿 tool result）
        rebuilt = [
            _assistant("call_aaa"),
            _tool_result("call_bbb", "orphan from another round"),
        ]
        final = mod._validate_tool_call_pairing(
            rebuilt, "deepseek", "cache", pipeline_id="t-cache-rebuild"
        )
        # 指纹失配 → 全量扫描 → 失配整轮清理（不能短路放行带病消息）
        assert len(final) == 0

    def test_shorter_list_resets_cache_for_full_rescan(self, mod) -> None:
        first = [
            _assistant("call_aaa"),
            _tool_result("call_aaa"),
            {"role": "user", "content": "q"},
        ]
        mod._validate_tool_call_pairing(
            first, "deepseek", "cache", pipeline_id="t-cache-shorter"
        )
        # 数量比缓存少（截断/重建）→ 全量扫描，孤儿 tool result 被清
        shorter = [_tool_result("call_zzz")]
        final = mod._validate_tool_call_pairing(
            shorter, "deepseek", "cache", pipeline_id="t-cache-shorter"
        )
        assert final == []

    def test_minimax_bypasses_incremental_cache(self, mod) -> None:
        # 用「缓存指纹匹配 + 消息带病」构造可区分场景：
        # 非 minimax 会短路返回带病消息；minimax 强制全量扫描清理。
        sick = [_assistant("call_aaa"), _tool_result("call_bbb")]
        key = "minimax:cache:bypass"
        mod._pairing_validated_len[key] = (
            len(sick),
            mod._message_fingerprint(sick[-1]),
        )
        final = mod._validate_tool_call_pairing(
            list(sick), "minimax", "cache", pipeline_id="bypass"
        )
        assert final == []  # 全量扫描：失配 tool + 不完整 assistant 整轮清理

    def test_deepseek_early_return_when_fingerprint_matches(self, mod) -> None:
        # 对照组：同缓存条件下的非 minimax provider 短路放行
        sick = [_assistant("call_aaa"), _tool_result("call_bbb")]
        key = "deepseek:cache:bypass"
        mod._pairing_validated_len[key] = (
            len(sick),
            mod._message_fingerprint(sick[-1]),
        )
        final = mod._validate_tool_call_pairing(
            list(sick), "deepseek", "cache", pipeline_id="bypass"
        )
        assert len(final) == 2  # 短路：原样返回（增量假设成立）
