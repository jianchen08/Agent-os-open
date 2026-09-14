# @feature: FP-0.2.二 内部模块 manifest | @ci: python-coverage
"""llm 簇缺口补测批：_message_normalizer / MiniMaxAdapter 的未覆盖分支。

覆盖面（按行为分组）：
- ``_minimax_phase_standardize``（507 行）：tool_calls 元素 ``function`` 非 dict
  时跳过 arguments 修正（结构性防御，逐元素跳过而非整条 assistant 短路）；
- ``_minimax_phase_relocate``（588、591、595）：tool_call_id 不匹配时按
  「已有匹配 tool 结果」/「尚无匹配结果」两态分别停止收集（不偷后续 assistant
  的 tool 结果）；非 tool 消息在已有 tool_group 时停止（新一轮对话起点）；
- ``_minimax_final_system_guard``（641、647-649）：前序阶段遗漏的非首位 system
  经终极安全网就地改 user 并摘 name（返回修正条数、幂等）；
- ``MiniMaxAdapter.adapt_messages_before_send``（36-37）：非首位 system → user
  且摘 name（首位 system / 非 system 角色不动，reasoning_content 一律剥离，
  返回新列表不改写输入）。

不可达/残留说明（逐条）：
- 无——本文件所有目标行均可达且已覆盖；无防御分支留白。

外部依赖：无（消息归一化与 provider 适配为纯数据变换，不触网）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
_s = str(_PLUGIN_DIR)
while _s in sys.path:
    sys.path.remove(_s)
sys.path.insert(0, _s)


def _load(filename: str, mod_name: str) -> Any:
    """按唯一模块名加载平铺模块（防裸名跨插件串扰，同目录既有惯例）。"""
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / filename)
    assert spec is not None and spec.loader is not None, f"cannot load {filename}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def norm() -> Any:
    return _load("_message_normalizer.py", "llm_gaps_message_normalizer_under_test")


# ─────────────────── _minimax_phase_standardize：function 非 dict ───────────────────


def test_non_dict_function_skips_arguments_fix(norm: Any) -> None:
    """tool_call.function 非 dict → 该 tool_call 的 arguments 修正整体跳过。

    行为契约：_minimax_phase_standardize 只修正可解析结构的 tool_call；
    结构非法者原样保留（由上游 standardize 阶段负责结构重建），不得抛异常。
    """
    tc = {"id": "call_abcd", "type": "function", "function": ["not", "a", "dict"]}
    msg = {"role": "assistant", "content": "", "tool_calls": [tc]}

    out, _converted = norm._minimax_phase_standardize([msg], "gap")

    assert out[0]["tool_calls"][0]["function"] == ["not", "a", "dict"]


def test_non_dict_function_skipped_while_sibling_fixed(norm: Any) -> None:
    """同一 assistant 内：结构非法的 tool_call 跳过、合法兄弟照常修正。

    区分度输入（≥2 组同行为）：本条把非法 tool_call 放在合法者之前，
    上一条为独占非法；两条共同锁定「跳过是逐元素、非整条 assistant 短路」。
    """
    bad = {"id": "call_1", "type": "function", "function": None}
    good = {"id": "call_2", "type": "function", "function": {"name": "f", "arguments": 12345}}
    msg = {"role": "assistant", "content": "", "tool_calls": [bad, good]}

    out, _converted = norm._minimax_phase_standardize([msg], "gap")

    assert out[0]["tool_calls"][0]["function"] is None
    assert out[0]["tool_calls"][1]["function"]["arguments"] == "{}"  # 非 str → 重置


# ─────────────────── _minimax_phase_relocate：停止收集三态 ───────────────────


def _assistant(call_ids: list[str]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": cid, "type": "function", "function": {"name": "f", "arguments": "{}"}}
            for cid in call_ids
        ],
    }


def _tool(call_id: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "content": "ok"}


def test_relocate_stops_at_unmatched_tool_when_group_nonempty(norm: Any) -> None:
    """已有匹配 tool 结果后遇不匹配 tool 消息 → 停止收集（不偷后续 assistant 的）。

    直接驱动 Phase 2（前置 Phase A 会先摘掉失配 tool，此处锁定重定位层自身的
    分组边界契约）：assistant(want=a) + tool(a) + tool(b) → 第二组停止收集。
    """
    converted = [_assistant(["call_aa"]), _tool("call_aa"), _tool("call_bb")]

    result, relocated = norm._minimax_phase_relocate(converted)

    # 三个消息全部保留、顺序不变（b 的 tool 属后续轮次，不并入当前组）
    assert [m.get("tool_call_id") or m["role"] for m in result] == [
        "assistant",
        "call_aa",
        "call_bb",
    ]
    assert relocated == 0


def test_relocate_stops_at_unmatched_tool_when_group_empty(norm: Any) -> None:
    """尚无匹配 tool 结果时遇不匹配 tool → 停止收集（提前消费的后续 tool 结果）。

    与上一条区分点：tool_group 为空（不匹配者出现在匹配者之前），
    走 592-595 的 else 分支而非 588-591 的 elif 分支。
    """
    converted = [_assistant(["call_aa"]), _tool("call_bb"), _tool("call_aa")]

    result, relocated = norm._minimax_phase_relocate(converted)

    assert [m.get("tool_call_id") or m["role"] for m in result] == [
        "assistant",
        "call_bb",
        "call_aa",
    ]
    assert relocated == 0


def test_relocate_stops_at_non_tool_after_group_collected(norm: Any) -> None:
    """已收到 tool 结果后遇非 tool 消息（新对话轮）→ 停止收集，不把后续当 intruder。

    走 596-598 的 ``elif tool_group`` 分支：user 消息是新一轮对话起点，
    既不能当非法插入者重排，也不能继续向后吞消息。
    """
    converted = [_assistant(["call_aa"]), _tool("call_aa"), {"role": "user", "content": "next"}]

    result, relocated = norm._minimax_phase_relocate(converted)

    assert [m["role"] for m in result] == ["assistant", "tool", "user"]
    assert relocated == 0  # user 未被当作非法插入者重定位


# ─────────────────── _minimax_final_system_guard：终极安全网 ───────────────────


def test_final_system_guard_converts_nonfirst_system_and_strips_name(norm: Any) -> None:
    """非首位 system → user 就地改写、摘 name，返回修正条数（≥2 组输入断言）。"""
    result = [
        {"role": "system", "content": "first", "name": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "system", "content": "injected", "name": "reminder"},
    ]

    fixed = norm._minimax_final_system_guard(result, "gap")

    assert fixed == 1
    assert result[2]["role"] == "user"
    assert "name" not in result[2]
    assert result[2]["content"] == "injected"  # 内容不动
    assert result[0]["role"] == "system"  # 首位不动
    assert result[0]["name"] == "sys"


def test_final_system_guard_counts_multiple_and_leaves_others(norm: Any) -> None:
    """多条非首位 system 一并改写；已 user/tool 消息不动（幂等性佐证）。"""
    result = [
        {"role": "system", "content": "first"},
        {"role": "system", "content": "a", "name": "n1"},
        {"role": "tool", "tool_call_id": "call_1", "content": "t"},
        {"role": "system", "content": "b"},
    ]

    fixed = norm._minimax_final_system_guard(result, "gap")

    assert fixed == 2
    assert [m["role"] for m in result] == ["system", "user", "tool", "user"]
    # 幂等：再跑一次无修正（无残留 system 非首位）
    assert norm._minimax_final_system_guard(result, "gap") == 0


def test_final_system_guard_on_empty_list_returns_zero(norm: Any) -> None:
    """空列表边界：零修正、不抛异常（性质断言）。"""
    assert norm._minimax_final_system_guard([], "gap") == 0


# ─────────────────── MiniMaxAdapter：非首位 system 角色修复 ───────────────────


def test_minimax_adapter_nonfirst_system_to_user_strips_name() -> None:
    """MiniMax 不允许非首位 system：转 user 并摘 name；reasoning_content 一律剥离。"""
    from provider_adapters.minimax import MiniMaxAdapter

    adapter = MiniMaxAdapter()
    out = adapter.adapt_messages_before_send(
        [
            {"role": "system", "content": "sys", "reasoning_content": "r0"},
            {"role": "user", "content": "hi"},
            {"role": "system", "content": "late", "name": "reminder"},
        ]
    )

    assert out[0]["role"] == "system"  # 首位 system 保留
    assert "reasoning_content" not in out[0]  # rc 剥离与角色修复正交
    assert out[2]["role"] == "user"
    assert "name" not in out[2]
    assert out[2]["content"] == "late"


def test_minimax_adapter_leaves_non_system_and_returns_new_list() -> None:
    """非 system 消息不动；返回新列表、输入不被改写（纯函数契约，≥2 组输入）。"""
    from provider_adapters.minimax import MiniMaxAdapter

    adapter = MiniMaxAdapter()
    original = [
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "a", "reasoning_content": "rc"},
        {"role": "tool", "tool_call_id": "call_1", "content": "t"},
    ]

    out = adapter.adapt_messages_before_send(original)

    assert [m["role"] for m in out] == ["user", "assistant", "tool"]
    assert "reasoning_content" not in out[1]
    # 输入未被就地改写（新列表 + 新 dict）
    assert "reasoning_content" in original[1]
    assert out is not original
