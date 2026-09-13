# @feature: FP-0.2.二 LLM 插件 | @ci: python-coverage
"""DeepSeek 适配器缺口补测：reasoning_content 采样保留三分支与补键契约。

覆盖（对照模块缺行）：
- kwargs retention 三形态：缺省 dict / 非 dict（回退默认间隔）/ 显式 interval
- interval<=0 全清空、==1 全量保留、>1 采样（首保留后续清空，第 interval*k+1 保留）
- _ensure_rc 仅补缺失键的 assistant 消息；user 消息原样返回
- _has_tool_calls 判定（assistant+tool_calls 才算）
- 原列表不被原地修改（新列表契约）
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_LLM_DIR = Path(__file__).resolve().parents[4] / "plugins" / "shared" / "system" / "llm"
if str(_LLM_DIR) not in sys.path:
    sys.path.insert(0, str(_LLM_DIR))

from provider_adapters.deepseek import DeepSeekAdapter  # noqa: E402


def _assistant(tc: bool = True, rc: Any = "思考内容") -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant", "content": "回答"}
    if tc:
        msg["tool_calls"] = [{"id": "t1"}]
    if rc is not None:
        msg["reasoning_content"] = rc
    return msg


def test_retention_default_and_non_dict_kwargs() -> None:
    adapter = DeepSeekAdapter()
    msgs = [_assistant(), _assistant(), _assistant(), _assistant()]
    out = adapter.adapt_messages_before_send(msgs)
    # 默认 interval=3：第 1、4 个（%3==1）保留原文，2/3 清空
    assert out[0]["reasoning_content"] == "思考内容"
    assert out[1]["reasoning_content"] == ""
    assert out[2]["reasoning_content"] == ""
    assert out[3]["reasoning_content"] == "思考内容"

    # retention 非 dict → 回退默认间隔（同上采样形态）
    out2 = adapter.adapt_messages_before_send(msgs, reasoning_retention="bogus")
    assert [m["reasoning_content"] for m in out2] == ["思考内容", "", "", "思考内容"]


def test_interval_zero_clears_all_and_one_keeps_all() -> None:
    adapter = DeepSeekAdapter()
    msgs = [_assistant(), _assistant(rc=""), {"role": "user", "content": "hi"}]
    out0 = adapter.adapt_messages_before_send(msgs, reasoning_retention={"sample_interval": 0})
    assert [m["reasoning_content"] for m in out0[:2]] == ["", ""]
    # user 消息（无 tool_calls）不补 rc 键
    assert "reasoning_content" not in out0[2]

    out1 = adapter.adapt_messages_before_send(msgs, reasoning_retention={"sample_interval": 1})
    assert out1[0]["reasoning_content"] == "思考内容"
    # 缺失键的 assistant 补空串（DeepSeek thinking 400 防线）
    assert out1[1]["reasoning_content"] == ""


def test_sampling_window_and_original_untouched() -> None:
    adapter = DeepSeekAdapter()
    msgs = [_assistant() for _ in range(7)]
    out = adapter.adapt_messages_before_send(msgs, reasoning_retention={"sample_interval": 3})
    kept = [i for i, m in enumerate(out) if m["reasoning_content"] == "思考内容"]
    assert kept == [0, 3, 6], "每 3 轮保留 1 轮：下标 0/3/6"
    # 原列表不被原地修改
    assert msgs[1]["reasoning_content"] == "思考内容"


def test_missing_rc_key_padded_for_tool_call_assistant() -> None:
    adapter = DeepSeekAdapter()
    msgs = [{"role": "user", "content": "q"}, _assistant(rc=None)]
    out = adapter.adapt_messages_before_send(msgs, reasoning_retention={"sample_interval": 1})
    assert out[0] is msgs[0], "user 消息原对象透传"
    assert out[1]["reasoning_content"] == "", "assistant(tool_calls) 缺 rc 补空串（内容允许为空）"


def test_no_tool_calls_assistant_not_counted_in_sampling() -> None:
    adapter = DeepSeekAdapter()
    msgs = [_assistant(tc=False), _assistant(), _assistant()]
    out = adapter.adapt_messages_before_send(msgs, reasoning_retention={"sample_interval": 2})
    # 无 tool_calls 的 assistant 不进采样计数：第二个（首个 tc）保留
    assert out[0]["reasoning_content"] == "思考内容"
    assert out[1]["reasoning_content"] == "思考内容"
    assert out[2]["reasoning_content"] == ""
