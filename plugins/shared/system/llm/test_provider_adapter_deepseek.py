# @feature: FP-T07 llm api | @ci: python-coverage
"""DeepSeek ProviderAdapter 采样保留 reasoning_content 行为测试。

契约（provider_adapters/deepseek.py 文档声明）：
- DeepSeek thinking 模式强制：assistant(tool_calls) 消息必须带
  ``reasoning_content`` 键（内容可为空串），缺失即上游 400；
- 采样保留：default_params.reasoning_retention.sample_interval 控制——
  0 = 全部清空，1 = 全量保留，N≥2 = 每 N 个 tool_calls 轮保留第 1 个
  （第 1/4/7…个），其余清空；
- 默认（未配置 / retention 非 dict）interval=3；
- 纯函数语义：返回新列表，输入 messages 不被改写。

外部依赖：无（纯消息变换，不触网）。
"""
from __future__ import annotations

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

from provider_adapters import get_provider_adapter  # noqa: E402
from provider_adapters.deepseek import DeepSeekAdapter  # noqa: E402


def _tc_assistant(rc: str | None = None) -> dict[str, Any]:
    """assistant(tool_calls) 消息；reasoning_content 缺省缺失（历史重建形态）。"""
    msg: dict[str, Any] = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "call_1", "function": {"name": "f", "arguments": "{}"}}],
    }
    if rc is not None:
        msg["reasoning_content"] = rc
    return msg


def _plain_assistant() -> dict[str, Any]:
    return {"role": "assistant", "content": "done"}


def _user() -> dict[str, Any]:
    return {"role": "user", "content": "hi"}


# ─────────────────────────── 采样分档 ───────────────────────────


@pytest.mark.parametrize(
    ("interval_cfg", "expected_rcs"),
    [
        # interval=3：第 1/4/7…个 tool_calls 轮保留，其余清空
        ({"sample_interval": 3}, ["r1", "", "", "r4", ""]),
        # interval=1：全量保留
        ({"sample_interval": 1}, ["r1", "r2", "r3", "r4", "r5"]),
        # interval=0：全部清空
        ({"sample_interval": 0}, ["", "", "", "", ""]),
        # interval=2：第 1/3/5…个保留（区分度：偶数间隔的保留相位不同）
        ({"sample_interval": 2}, ["r1", "", "r3", "", "r5"]),
    ],
)
def test_sample_interval_tiers_control_retention(
    interval_cfg: dict[str, Any], expected_rcs: list[str]
) -> None:
    """sample_interval 三档语义：0 清空 / 1 保留 / N 采样，保留相位恒为首轮。"""
    messages = [_user(), _tc_assistant("r1"), _tc_assistant("r2"), _tc_assistant("r3"),
                _tc_assistant("r4"), _tc_assistant("r5")]
    out = DeepSeekAdapter().adapt_messages_before_send(
        messages, reasoning_retention=interval_cfg
    )

    rcs = [m.get("reasoning_content") for m in out if m.get("tool_calls")]
    assert rcs == expected_rcs


def test_default_interval_is_three_when_retention_absent() -> None:
    """未传 reasoning_retention → 默认 3 轮采样（与显式 3 等效）。"""
    messages = [_tc_assistant("r1"), _tc_assistant("r2"), _tc_assistant("r3"), _tc_assistant("r4")]

    default_out = DeepSeekAdapter().adapt_messages_before_send(messages)
    explicit_out = DeepSeekAdapter().adapt_messages_before_send(
        messages, reasoning_retention={"sample_interval": 3}
    )

    assert [m["reasoning_content"] for m in default_out] == ["r1", "", "", "r4"]
    assert [m["reasoning_content"] for m in explicit_out] == [m["reasoning_content"] for m in default_out]


@pytest.mark.parametrize("non_dict", [None, "full", 7, ["1"]])
def test_non_dict_retention_falls_back_to_default(non_dict: Any) -> None:
    """retention 非 dict（坏配置形态）→ 不炸，回落默认 3 轮采样。"""
    messages = [_tc_assistant("r1"), _tc_assistant("r2"), _tc_assistant("r3"), _tc_assistant("r4")]

    out = DeepSeekAdapter().adapt_messages_before_send(messages, reasoning_retention=non_dict)

    assert [m["reasoning_content"] for m in out] == ["r1", "", "", "r4"]


# ─────────────────────────── rc 补键契约 ───────────────────────────


@pytest.mark.parametrize("interval_cfg", [{"sample_interval": 0}, {"sample_interval": 1}, {"sample_interval": 3}])
def test_tool_call_assistant_always_carries_rc_key(interval_cfg: dict[str, Any]) -> None:
    """性质：任何分档下 assistant(tool_calls) 必带 reasoning_content 键——
    缺失补空串（历史恢复/压缩重建的消息直接透传会 400）。"""
    messages = [_tc_assistant(), _user(), _tc_assistant("kept")]

    out = DeepSeekAdapter().adapt_messages_before_send(messages, reasoning_retention=interval_cfg)

    for m in out:
        if m.get("tool_calls"):
            assert "reasoning_content" in m
            assert isinstance(m["reasoning_content"], str)


def test_plain_assistant_gets_empty_rc_without_rc_loss() -> None:
    """普通 assistant（无 tool_calls）：interval<=0/1 分档补 rc 空串；已有 rc 不丢。"""
    messages = [_plain_assistant(), {"role": "assistant", "content": "x", "reasoning_content": "th"}]

    out = DeepSeekAdapter().adapt_messages_before_send(messages, reasoning_retention={"sample_interval": 1})

    assert out[0]["reasoning_content"] == ""
    assert out[1]["reasoning_content"] == "th"


def test_user_message_left_untouched() -> None:
    """user 消息不注入 rc 键（DeepSeek 仅强制 assistant 面）。"""
    messages = [_user()]

    out = DeepSeekAdapter().adapt_messages_before_send(messages, reasoning_retention={"sample_interval": 0})

    assert "reasoning_content" not in out[0]


# ─────────────────────────── 纯函数语义 ───────────────────────────


def test_input_messages_not_mutated() -> None:
    """清空/补键都落在副本上：原 messages 逐字保留（保护 state["messages"]）。"""
    messages = [_tc_assistant("r1"), _tc_assistant(), _user()]
    snapshot = [
        {k: (list(v) if isinstance(v, list) else v) for k, v in m.items()} for m in messages
    ]

    out = DeepSeekAdapter().adapt_messages_before_send(messages, reasoning_retention={"sample_interval": 0})

    assert out is not messages
    assert messages == snapshot  # 原 rc 值与键位不被改写
    assert out[0]["reasoning_content"] == ""  # 副本上已清空


def test_other_kwargs_accepted_without_effect() -> None:
    """default_params 的其余键（temperature 等）透传不炸、不影响 rc 语义。"""
    messages = [_tc_assistant("r1"), _tc_assistant("r2")]

    out = DeepSeekAdapter().adapt_messages_before_send(
        messages, reasoning_retention={"sample_interval": 1}, temperature=0.5
    )

    assert [m["reasoning_content"] for m in out] == ["r1", "r2"]


# ─────────────────────────── 注册表接线 ───────────────────────────


def test_registry_routes_deepseek_model_to_deepseek_adapter() -> None:
    """deepseek 前缀模型 → DeepSeekAdapter 单例（completion 入口的实际分派面）。"""
    adapter = get_provider_adapter("deepseek/deepseek-v4-pro")

    assert isinstance(adapter, DeepSeekAdapter)
