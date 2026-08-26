# @feature: FP-0.2.LLM 流式服务契约 | @vision: V3 可嵌入 | @ci: python-coverage
"""llm.complete_stream 流式协议翻译测试（DSH 8 事件形态）。

验证 ``StreamTranslator`` 把 adapter 归一化 chunk（thinking/text/tool_call/
thinking_end）翻译为块索引化事件序列：
- 块生命周期：block_start → 增量 → block_end，增量按 index 归组
- thinking/text/tool_call 三族块独立索引，类型切换先闭旧块再开新块
- tool 参数 ``arguments_delta`` 为原始 JSON 字符串增量，不中途解析
- 收尾顺序：block_end → usage → finish；finish 幂等
- finish 断流兜底（reason="error"）由 runner 在异常路径调用（见
  test_streaming_server.py），本文件只验 finish 事件形态

测试断行为不断实现：断言事件序列/载荷，不断言内部队列等私有细节。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

pytestmark = pytest.mark.unit

from streaming import StreamTranslator, map_finish_reason  # noqa: E402

# ────────────────────────────────────────────────────────────
# chunk 构造辅助（adapter on_chunk 契约形态）
# ────────────────────────────────────────────────────────────


def _text(content: str) -> dict[str, Any]:
    return {"type": "text", "content": content}


def _thinking(content: str) -> dict[str, Any]:
    return {"type": "thinking", "content": content}


def _thinking_end() -> dict[str, Any]:
    return {"type": "thinking_end", "content": ""}


def _tool_call(
    index: int,
    call_id: str | None = None,
    name: str = "",
    arguments: str = "",
) -> dict[str, Any]:
    """构造 adapter tool_call chunk（携带 litellm delta 形态的 tool_calls 列表）。"""
    tc = SimpleNamespace(
        index=index,
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )
    return {"type": "tool_call", "tool_calls": [tc]}


def _events(*chunks: dict[str, Any], reason: str = "stop", usage: dict[str, Any] | None = None) -> list[tuple[str, dict[str, Any]]]:
    """把 chunk 序列过一遍 translator 并收尾，返回 (event, payload) 列表。"""
    t = StreamTranslator()
    out: list[tuple[str, dict[str, Any]]] = []
    for c in chunks:
        out.extend((e.event, e.payload) for e in t.translate(c))
    out.extend((e.event, e.payload) for e in t.finish(reason, usage=usage))
    return out


def _types(events: list[tuple[str, dict[str, Any]]]) -> list[str]:
    return [e for e, _ in events]


# ────────────────────────────────────────────────────────────
# 文本块生命周期
# ────────────────────────────────────────────────────────────


def test_text_stream_block_lifecycle() -> None:
    """text 增量 → 单块：block_start → text_delta×N → block_end → finish。"""
    events = _events(_text("Hel"), _text("lo"))
    assert _types(events) == [
        "block_start",
        "text_delta",
        "text_delta",
        "block_end",
        "finish",
    ]
    assert events[0][1] == {"index": 0, "block_type": "text"}
    assert events[1][1] == {"index": 0, "text": "Hel"}
    assert events[2][1] == {"index": 0, "text": "lo"}
    assert events[3][1] == {"index": 0, "block": {"block_type": "text", "text": "Hello"}}
    assert events[4][1] == {"reason": "stop"}


@pytest.mark.parametrize(
    ("chunks", "content"),
    [
        ([_text("a")], "a"),
        ([_text("x" * 300) for _ in range(20)], "x" * 6000),
    ],
    ids=["single-delta", "twenty-300-char-deltas"],
)
def test_text_deltas_partition_content_property(chunks: list[dict[str, Any]], content: str) -> None:
    """性质断言：同块增量拼接 == 完整正文（两组成区分度输入）。"""
    events = _events(*chunks)
    text_deltas = [p["text"] for e, p in events if e == "text_delta"]
    assert "".join(text_deltas) == content
    # 所有 text_delta 归同一 index，且只开闭一次块
    indices = {p["index"] for e, p in events if e in ("block_start", "text_delta", "block_end") and p.get("block_type", "text") == "text"}
    assert indices == {0}
    assert events[0][0] == "block_start"
    assert events[-2][0] == "block_end"


# ────────────────────────────────────────────────────────────
# thinking 块（取代旧 thinking_start/chunk/end 三事件）
# ────────────────────────────────────────────────────────────


def test_reasoning_then_text_switches_block() -> None:
    """thinking → 思考块；thinking_end → 闭块；随后 text 开新索引块。"""
    events = _events(_thinking("Why"), _thinking("?"), _thinking_end(), _text("Ans"))
    assert _types(events) == [
        "block_start",  # reasoning 0
        "reasoning_delta",
        "reasoning_delta",
        "block_end",  # reasoning 0 闭合（thinking_end）
        "block_start",  # text 1
        "text_delta",
        "block_end",
        "finish",
    ]
    assert events[0][1] == {"index": 0, "block_type": "reasoning"}
    assert events[3][1]["index"] == 0
    assert events[3][1]["block"] == {"block_type": "reasoning", "text": "Why?"}
    assert events[4][1] == {"index": 1, "block_type": "text"}
    assert events[5][1] == {"index": 1, "text": "Ans"}


def test_text_then_reasoning_switches_block() -> None:
    """先 text 后 thinking：text 块先闭，reasoning 开新索引块（类型切换闭旧块）。"""
    events = _events(_text("A"), _thinking("T"))
    assert _types(events) == [
        "block_start",
        "text_delta",
        "block_end",
        "block_start",
        "reasoning_delta",
        "block_end",
        "finish",
    ]
    assert events[2][1]["index"] == 0
    assert events[3][1] == {"index": 1, "block_type": "reasoning"}


def test_empty_text_chunk_is_ignored() -> None:
    """空 content 的 text chunk 不产生事件（防御：adapter 不发空增量）。"""
    t = StreamTranslator()
    assert t.translate(_text("")) == []
    assert t.translate({"type": "text"}) == []  # 缺 content 键同语义
    assert t.translate(_text("x")) != []


def test_empty_thinking_chunk_is_ignored() -> None:
    """空 content 的 thinking chunk 不产生事件（不误开空块）。"""
    t = StreamTranslator()
    assert t.translate(_thinking("")) == []
    assert t.translate({"type": "thinking"}) == []
    assert t.translate(_thinking("x")) != []


def test_text_after_tool_call_closes_all_tool_blocks() -> None:
    """tool_call 后接 text：所有打开的 tool 块闭合（并行不残留），text 开新索引。"""
    events = _events(
        _tool_call(0, call_id="c0", name="f", arguments="{}"),
        _tool_call(1, call_id="c1", name="g", arguments="{}"),
        _text("after"),
    )
    assert _types(events) == [
        "block_start",
        "tool_call_delta",
        "block_start",
        "tool_call_delta",
        "block_end",
        "block_end",
        "block_start",
        "text_delta",
        "block_end",
        "finish",
    ]
    # 两个 tool 块（0/1）全部闭合（并行不残留），text 块索引 2
    ends = [p["index"] for e, p in events if e == "block_end"]
    assert ends == [1, 0, 2]
    assert events[6][1] == {"index": 2, "block_type": "text"}


def test_thinking_end_without_open_reasoning_is_noop() -> None:
    """无思考块时的 thinking_end 不产生事件（防御：adapter 只在有思考时发）。"""
    t = StreamTranslator()
    assert t.translate(_thinking_end()) == []
    assert t.translate(_text("hi")) != []


# ────────────────────────────────────────────────────────────
# tool_call 块：arguments_delta 原始 JSON 字符串增量
# ────────────────────────────────────────────────────────────


def test_tool_call_delta_raw_arguments_accumulation() -> None:
    """tool 参数增量：id/name 只在首增量携带，arguments_delta 为原始字符串增量。"""
    events = _events(
        _tool_call(0, call_id="call_1", name="get_weather", arguments='{"loc'),
        _tool_call(0, arguments='ation":"x"}'),
    )
    assert _types(events) == [
        "block_start",
        "tool_call_delta",
        "tool_call_delta",
        "block_end",
        "finish",
    ]
    assert events[0][1] == {"index": 0, "block_type": "tool_call"}
    assert events[1][1] == {
        "index": 0,
        "id": "call_1",
        "name": "get_weather",
        "arguments_delta": '{"loc',
    }
    # 第二增量不重复 id/name，arguments_delta 是原始字符串片段（未解析）
    assert events[2][1] == {"index": 0, "arguments_delta": 'ation":"x"}'}
    assert events[3][1] == {
        "index": 0,
        "block": {
            "block_type": "tool_call",
            "id": "call_1",
            "name": "get_weather",
            "arguments": '{"location":"x"}',
        },
    }


def test_parallel_tool_calls_independent_indices() -> None:
    """并行工具调用：按流 index 各开一块，增量按各自 index 归组。"""
    events = _events(
        _tool_call(0, call_id="c0", name="f", arguments='{"a":'),
        _tool_call(1, call_id="c1", name="g", arguments='{"b":'),
        _tool_call(0, arguments="1}"),
        _tool_call(1, arguments="2}"),
    )
    assert _types(events) == [
        "block_start",
        "tool_call_delta",
        "block_start",
        "tool_call_delta",
        "tool_call_delta",
        "tool_call_delta",
        "block_end",
        "block_end",
        "finish",
    ]
    # 块索引按开块顺序分配（0, 1），增量按流 index 归组
    assert events[0][1] == {"index": 0, "block_type": "tool_call"}
    assert events[2][1] == {"index": 1, "block_type": "tool_call"}
    assert events[4][1] == {"index": 0, "arguments_delta": "1}"}
    assert events[5][1] == {"index": 1, "arguments_delta": "2}"}
    ends = [p for e, p in events if e == "block_end"]
    assert {p["index"] for p in ends} == {0, 1}


def test_tool_call_after_text_closes_text_block() -> None:
    """text 后接 tool_call：text 块先闭，tool 块开新索引。"""
    events = _events(_text("ok"), _tool_call(0, call_id="c", name="f", arguments="{}"))
    assert _types(events) == [
        "block_start",
        "text_delta",
        "block_end",
        "block_start",
        "tool_call_delta",
        "block_end",
        "finish",
    ]
    assert events[2][1]["index"] == 0
    assert events[3][1] == {"index": 1, "block_type": "tool_call"}


# ────────────────────────────────────────────────────────────
# 收尾：usage → finish；finish 幂等
# ────────────────────────────────────────────────────────────


def test_usage_before_finish_maps_token_fields() -> None:
    """usage 在 finish 前发出，prompt/completion_tokens 映射为 input/output_tokens。"""
    usage = {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
        "cached_tokens": 3,
    }
    events = _events(_text("hi"), usage=usage)
    usage_event = events[-2]
    assert usage_event[0] == "usage"
    assert usage_event[1] == {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "cached_tokens": 3,
    }
    assert events[-1] == ("finish", {"reason": "stop"})


def test_usage_missing_fields_default_to_zero() -> None:
    """usage 缺字段时 input/output_tokens 默认 0（其余键 0 兜底，不出现 NaN）。"""
    events = _events(usage={"prompt_tokens": 0, "completion_tokens": 0})
    usage_payload = next(p for e, p in events if e == "usage")
    assert usage_payload == {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
    }


def test_finish_without_open_blocks_only_finish() -> None:
    """空流：无任何块，finish 直接收尾。"""
    t = StreamTranslator()
    events = t.finish("stop")
    assert [(e.event, e.payload) for e in events] == [("finish", {"reason": "stop"})]


def test_finish_idempotent() -> None:
    """finish 幂等：二次调用不产生事件（断流兜底重复触发安全）。"""
    t = StreamTranslator()
    t.translate(_text("a"))
    first = t.finish("stop")
    second = t.finish("error")
    assert first != []
    assert second == []
    assert t.finished


def test_translate_after_finish_is_noop() -> None:
    """finish 后到达的 chunk 被忽略（协议已终结）。"""
    t = StreamTranslator()
    t.finish("stop")
    assert t.translate(_text("late")) == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "stop"),
        ("stop", "stop"),
        ("length", "length"),
        ("tool_calls", "tool_calls"),
        ("error", "error"),
        ("content_filter", "stop"),
    ],
    ids=["none", "stop", "length", "tool_calls", "error", "unknown-maps-stop"],
)
def test_finish_reason_mapping(raw: str | None, expected: str) -> None:
    """finish reason 词汇收敛：未知值（如 content_filter）映射 stop。"""
    assert map_finish_reason(raw) == expected
