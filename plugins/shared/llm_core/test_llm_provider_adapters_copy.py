# @feature: FP-T07 llm api | @ci: python-coverage
"""Provider 消息适配家族（复制改造件）行为测试。

覆盖 ``plugins/shared/llm_core/llm_provider_adapters/``（源 =
``plugins/shared/llm/provider_adapters/`` @ c0fe6922fc）：

- 基类三档回放（True=字段回放 / "fold"=折叠正文 / False=剥离），返回新对象；
- DeepSeek 采样保留（interval 0/1/N）与强制 rc 键在场；
- MiniMax 非首位 system 角色修复 + 剥离档；
- ``resolve_replay_reasoning`` 三层解析与 ``get_provider_adapter`` 前缀选择。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_LLM_CORE_DIR = _REPO_ROOT / "plugins" / "shared" / "llm_core"
_SYSTEM_LLM_DIR = _REPO_ROOT / "plugins" / "shared" / "llm"
for _d in (_SYSTEM_LLM_DIR, _LLM_CORE_DIR):
    if str(_d) in sys.path:
        sys.path.remove(str(_d))
sys.path.insert(0, str(_SYSTEM_LLM_DIR))
sys.path.insert(0, str(_LLM_CORE_DIR))

from llm_provider_adapters import (  # noqa: E402
    DeepSeekAdapter,
    MiniMaxAdapter,
    ProviderAdapter,
    get_provider_adapter,
    resolve_replay_reasoning,
)
from llm_provider_adapters.base import REPLAY_MODE_FOLD, ReplayMode  # noqa: E402


def _msgs() -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": "答", "reasoning_content": "想", "tool_calls": [{"id": "c1"}]},
        {"role": "user", "content": "继续"},
    ]


# ─────────────────────────── 基类三档 ───────────────────────────


def test_base_true_mode_copies_without_mutating_input() -> None:
    """True=字段回放：返回等值新列表新 dict，原 messages 不被改写。"""
    original = _msgs()
    out = ProviderAdapter().adapt_messages_before_send(original, replay_reasoning=True)
    assert out == original
    assert out is not original
    assert out[0] is not original[0]
    assert original[1]["reasoning_content"] == "想"


def test_base_fold_mode_moves_reasoning_into_think_block() -> None:
    """"fold"=折叠：rc 以 <think> 块并入正文并去掉 rc 字段（代理丢字段通道）。"""
    out = ProviderAdapter().adapt_messages_before_send(
        [{"role": "assistant", "content": "答", "reasoning_content": "想"}],
        replay_reasoning="fold",
    )
    assert "reasoning_content" not in out[0]
    assert out[0]["content"] == "<think>\n想\n</think>\n答"


def test_base_fold_mode_without_rc_or_with_list_content() -> None:
    """折叠档边界：无 rc 原样保留内容；content 为空/list 时只写 think 块。

    list 型 content（多模态块）折叠时按源契约整段替换为 think 块——折叠通道
    的正文只承载思考文本（内容块不参与字段回放）。
    """
    out = ProviderAdapter().adapt_messages_before_send(
        [
            {"role": "assistant", "content": "无思考"},
            {"role": "assistant", "content": "", "reasoning_content": "想"},
            {"role": "tool", "content": [{"type": "text", "text": "x"}], "reasoning_content": "想"},
        ],
        replay_reasoning="fold",
    )
    assert out[0]["content"] == "无思考"
    assert out[1]["content"] == "<think>\n想\n</think>"
    assert out[2]["content"] == "<think>\n想\n</think>"
    assert "reasoning_content" not in out[2]


def test_base_false_mode_strips_reasoning_content() -> None:
    """False=剥离：rc 字段移除，其余字段保留。"""
    out = ProviderAdapter().adapt_messages_before_send(_msgs(), replay_reasoning=False)
    assert all("reasoning_content" not in m for m in out)
    assert out[1]["tool_calls"] == [{"id": "c1"}]


def test_effective_replay_defaults_to_class_attribute() -> None:
    """未显式传档位 → 沿用类属性默认档（True）。"""
    assert ProviderAdapter()._effective_replay(None) is True
    assert ProviderAdapter()._effective_replay("fold") == REPLAY_MODE_FOLD


# ─────────────────────────── DeepSeek ───────────────────────────


def test_deepseek_interval_one_keeps_and_backfills_rc_key() -> None:
    """interval=1（全量保留）：assistant 缺 rc 键补空串，已有内容原样。"""
    out = DeepSeekAdapter().adapt_messages_before_send(
        [
            {"role": "assistant", "content": "a"},
            {"role": "assistant", "content": "b", "reasoning_content": "想"},
            {"role": "user", "content": "u"},
        ],
        replay_reasoning=True,
        reasoning_retention={"sample_interval": 1},
    )
    assert out[0]["reasoning_content"] == ""
    assert out[1]["reasoning_content"] == "想"
    assert "reasoning_content" not in out[2]


def test_deepseek_interval_zero_clears_tool_call_turns_only() -> None:
    """interval=0：带 tool_calls 的 assistant 清空 rc 内容但键保留。"""
    out = DeepSeekAdapter().adapt_messages_before_send(
        [
            {"role": "assistant", "content": "a", "tool_calls": [{"id": "c"}], "reasoning_content": "想"},
            {"role": "assistant", "content": "b", "reasoning_content": "想"},
        ],
        replay_reasoning=True,
        reasoning_retention={"sample_interval": 0},
    )
    assert out[0]["reasoning_content"] == ""
    assert out[1]["reasoning_content"] == "想"


def test_deepseek_sampling_every_nth_tool_turn() -> None:
    """interval=N：每 N 个 tool_calls 轮保留第 1 个，其余清空（键保留）。"""
    turns = [
        {"role": "assistant", "content": "1", "tool_calls": [{"id": "c1"}], "reasoning_content": "r1"},
        {"role": "assistant", "content": "2", "tool_calls": [{"id": "c2"}], "reasoning_content": "r2"},
        {"role": "assistant", "content": "3", "tool_calls": [{"id": "c3"}], "reasoning_content": "r3"},
    ]
    out = DeepSeekAdapter().adapt_messages_before_send(turns, replay_reasoning=True, reasoning_retention={"sample_interval": 2})
    assert [m["reasoning_content"] for m in out] == ["r1", "", "r3"]


def test_deepseek_strip_mode_blanks_rc_but_keeps_key() -> None:
    """剥离档（thinking 模式必须回传键，内容允许空串）。"""
    out = DeepSeekAdapter().adapt_messages_before_send(_msgs(), replay_reasoning=False)
    assert out[1]["reasoning_content"] == ""
    assert out[2].get("reasoning_content", "") == ""


def test_deepseek_retention_default_interval_used_when_absent() -> None:
    """未配置 retention → 默认间隔 3（前三轮 tool 轮只保留第 1 个）。"""
    out = DeepSeekAdapter().adapt_messages_before_send(
        [
            {"role": "assistant", "content": "1", "tool_calls": [{"id": "c1"}], "reasoning_content": "r1"},
            {"role": "assistant", "content": "2", "tool_calls": [{"id": "c2"}], "reasoning_content": "r2"},
        ],
        replay_reasoning=True,
    )
    assert out[0]["reasoning_content"] == "r1"
    assert out[1]["reasoning_content"] == ""


# ─────────────────────────── MiniMax ───────────────────────────


def test_minimax_moves_non_first_system_to_user() -> None:
    """MiniMax 只允许首位 system：非首位 system → user（name 一并移除）。"""
    out = MiniMaxAdapter().adapt_messages_before_send(
        [
            {"role": "system", "content": "s1"},
            {"role": "user", "content": "u"},
            {"role": "system", "content": "s2", "name": "inject"},
        ]
    )
    assert out[0]["role"] == "system"
    assert out[2]["role"] == "user"
    assert "name" not in out[2]


def test_minimax_strip_mode_removes_rc() -> None:
    out = MiniMaxAdapter().adapt_messages_before_send(_msgs(), replay_reasoning=False)
    assert all("reasoning_content" not in m for m in out)


# ─────────────────────────── 解析器 ───────────────────────────


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        (None, REPLAY_MODE_FOLD),
        ({"provider": "deepseek", "reasoning_content": "x"}, True),
        ({"provider": "zhipu_coding"}, True),
        ({"provider": "minimax"}, True),
        ({"provider": "cpa_gateway"}, REPLAY_MODE_FOLD),
        ({"provider": "anything", "replay_reasoning": False}, False),
        ({"provider": "anything", "replay_reasoning": "fold"}, REPLAY_MODE_FOLD),
    ],
)
def test_resolve_replay_reasoning_layers(entry: dict[str, Any] | None, expected: ReplayMode) -> None:
    """三层解析：模型条目显式 > provider 名单 > 未登记默认 fold。"""
    assert resolve_replay_reasoning(entry) == expected


@pytest.mark.parametrize(
    ("model", "expected_type"),
    [
        ("deepseek-v4-pro", DeepSeekAdapter),
        ("MiniMax/MiniMax-M3", MiniMaxAdapter),
        ("gw-glm-5.3-flash", ProviderAdapter),
        ("", ProviderAdapter),
    ],
)
def test_get_provider_adapter_prefix_selection(model: str, expected_type: type) -> None:
    """按 model 串子串匹配（大小写不敏感）；未命中落基类。"""
    assert isinstance(get_provider_adapter(model), expected_type)
