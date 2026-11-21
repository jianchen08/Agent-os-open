# @feature: FP-0.2.二 内部模块统一 manifest 化 | @ci: python-coverage
"""历史退化内容折叠：出站前把合成重复片段压成占位（2026-11-10 事故根修）。

断行为：assistant 的合成重复片段被折叠成占位、tool 结果与正常正文不动、
端到端经 normalize_messages_for_provider 生效。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

pytestmark = pytest.mark.unit

from _message_normalizer import (  # noqa: E402
    collapse_degenerate_history,
    normalize_messages_for_provider,
)

_PERIOD_13 = "本轮一条链完成收紧与提交："


def _normal_long_text() -> str:
    return "".join(
        f"第 {i} 步：检查插件声明面的 output_schema 与 render 意图，"
        f"确认工具白名单与状态键口径一致，随后落盘并回归。\n"
        for i in range(200)
    )


def test_collapses_periodic_assistant_content() -> None:
    """同一短句连发 → 折叠为占位（原来会每轮回放给模型）。"""
    messages: list[dict[str, Any]] = [
        {"role": "assistant", "content": _PERIOD_13 * 2000},
    ]

    collapsed = collapse_degenerate_history(messages, name="t", pipeline_id="p1")

    assert collapsed > 20000
    assert "已折叠重复输出" in messages[0]["content"]
    assert _PERIOD_13 not in messages[0]["content"][:50]


def test_collapses_reasoning_and_keeps_tool_results() -> None:
    """思考通道同样折叠；tool 结果属工具事实，不动。"""
    emoji = "🍦" * 20000
    messages: list[dict[str, Any]] = [
        {"role": "assistant", "content": "正常回复", "reasoning_content": emoji},
        {"role": "tool", "content": emoji},
    ]

    collapse_degenerate_history(messages, name="t")

    assert "已折叠重复输出" in messages[0]["reasoning_content"]
    assert messages[1]["content"] == emoji  # tool 原样


def test_leaves_normal_content_untouched() -> None:
    """正常长文本不动（返回同一对象，不复制）。"""
    text = _normal_long_text()
    messages: list[dict[str, Any]] = [{"role": "assistant", "content": text}]

    assert collapse_degenerate_history(messages, name="t") == 0
    assert messages[0]["content"] is text


def test_normalize_messages_collapses_before_outbound() -> None:
    """端到端：出站规范化流程里就完成折叠（任何 provider 都生效）。"""
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "继续"},
        {"role": "assistant", "content": _PERIOD_13 * 2000},
        {"role": "user", "content": "继续"},
    ]

    normalized = normalize_messages_for_provider(messages, provider="openai", name="t")

    assert "已折叠重复输出" in normalized[1]["content"]
