# @feature: FP-T07 llm api | @ci: python-coverage
"""MiniMax/思考标签解析家族（复制改造件）行为测试。

覆盖 ``plugins/shared/llm_core/llm_minimax.py``（源 =
``plugins/shared/llm/adapter_minimax.py`` @ c0fe6922fc）：

- ``<think/>`` 标签提取（标准 XML / 无 '>' 开标签 / 无标签 / 空值）；
- 正文原生工具调用恢复（invoke/parameter 子标签、裸 JSON、转义倾倒形态、
  截断无闭合、无可识别调用块不误吞）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

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

import llm_minimax  # noqa: E402


def test_extract_thinking_standard_xml() -> None:
    thinking, cleaned = llm_minimax._extract_thinking_from_content("<think>先想</think>再答")
    assert thinking == "先想"
    assert cleaned == "再答"


def test_extract_thinking_no_gt_open_tag_form() -> None:
    """MiniMax 形态：开标签无 '>'（``<think\\n...``）。"""
    thinking, cleaned = llm_minimax._extract_thinking_from_content("<think\n推理中\n</think/>正文")
    assert thinking == "推理中"
    assert cleaned == "正文"


@pytest.mark.parametrize(("content", "expected"), [("", ""), ("纯正文", "纯正文")])
def test_extract_thinking_without_tag(content: str, expected: str) -> None:
    thinking, cleaned = llm_minimax._extract_thinking_from_content(content)
    assert thinking is None
    assert cleaned == expected


def test_parse_native_tool_calls_invoke_parameter_form() -> None:
    """官方模板：<minimax:tool_call> 容器 + invoke/parameter 子标签。"""
    text = (
        '前言<minimax:tool_call><invoke name="bash_execute">'
        '<parameter name="command">ls -la</parameter>'
        '<parameter name="timeout">5</parameter>'
        "</invoke></minimax:tool_call>后记"
    )
    calls, cleaned = llm_minimax.parse_minimax_native_tool_calls(text)
    assert len(calls) == 1
    assert calls[0]["name"] == "bash_execute"
    assert json.loads(calls[0]["arguments"]) == {"command": "ls -la", "timeout": "5"}
    assert calls[0]["id"].startswith("call_")
    assert "invoke" not in cleaned
    assert "前言" in cleaned
    assert "后记" in cleaned


def test_parse_native_tool_calls_bare_json_form() -> None:
    """裸 JSON 形态：{"name": ..., "arguments": {…}}（生产 2026-09-15 实证）。"""
    body = json.dumps({"name": "file_read", "arguments": {"path": "a.txt"}})
    text = f"<tool_call>{body}</tool_call>"
    calls, cleaned = llm_minimax.parse_minimax_native_tool_calls(text)
    assert calls[0]["name"] == "file_read"
    assert json.loads(calls[0]["arguments"]) == {"path": "a.txt"}
    assert cleaned == ""


def test_parse_native_tool_calls_escaped_dump_form_and_truncated_close() -> None:
    """转义倾倒形态 + 缺闭合（截断流）：调用意图仍被解析、不丢参数。"""
    text = (
        ']<]minimax[>[<tool_call>]<]minimax[>[<invoke name="bash">'
        ']<]minimax[>[<parameter name="command">pwd'
    )
    calls, _cleaned = llm_minimax.parse_minimax_native_tool_calls(text)
    assert calls
    assert calls[0]["name"] == "bash"
    assert json.loads(calls[0]["arguments"])["command"] == "pwd"


def test_parse_native_tool_calls_ignores_unrecognised_text() -> None:
    """无可识别调用块 → ([], 原文)，不误吞正常正文。"""
    calls, cleaned = llm_minimax.parse_minimax_native_tool_calls("普通回答，没有工具调用")
    assert calls == []
    assert cleaned == "普通回答，没有工具调用"


def test_parse_native_tool_calls_container_without_valid_body_keeps_text() -> None:
    """容器在但体内既非 invoke 也非合法 JSON → 不产出调用（正文保留容器原文）。"""
    text = "<tool_call>not-json-and-no-invoke</tool_call>"
    calls, cleaned = llm_minimax.parse_minimax_native_tool_calls(text)
    assert calls == []
    assert cleaned == text


def test_parse_bare_json_rejects_non_object_and_missing_name() -> None:
    assert llm_minimax._parse_bare_json_tool_call("[1, 2]") is None
    assert llm_minimax._parse_bare_json_tool_call('{"arguments": {}}') is None
