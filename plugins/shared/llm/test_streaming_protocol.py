# @feature: FP-0.2.二 内部模块统一 manifest 化 | @vision: V3 可嵌入 | @ci: python-coverage
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

from agentos_plugin_sdk.bootstrap import bootstrap_plugin

_paths = bootstrap_plugin(__file__)  # 插件目录 + plugins/shared 根入 sys.path（单文件可跑）
if str(_paths.plugin_dir) not in sys.path:
    sys.path.insert(0, str(_paths.plugin_dir))

pytestmark = pytest.mark.unit

from streaming import DegenerationDetector, StreamTranslator, map_finish_reason  # noqa: E402

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
    """构造 adapter tool_call chunk（携带归一 delta 形态的 tool_calls 列表）。"""
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


#: 20 段各 300 字符、内容互不相同的正文（分片拼接性质用）。句面必须真互异
#: （数字/措辞逐句变化）——周期性复读句会被退化循环守卫正确判停提前收流，
#: 那是守卫用例，不是本条性质（守卫换代后按 simurg 判定口径收紧了夹具）。
_VARIED_TOPICS = ["配置", "清单", "缓存", "路由", "会话", "快照", "凭证", "审计", "网关", "租户"]
_VARIED_DELTAS = [
    "".join(
        f"片段{i:02d}处理{_VARIED_TOPICS[j]}：参数 {i * 8 + j * 3} 已记录，"
        f"结论{'一致' if (i + j) % 2 == 0 else '相左'}，"
        f"下一步转入{_VARIED_TOPICS[(j + i) % len(_VARIED_TOPICS)]}面复查第{j + 1}轮。"
        for j in range(6)
    )[:300]
    for i in range(20)
]


@pytest.mark.parametrize(
    ("chunks", "content"),
    [
        ([_text("a")], "a"),
        ([_text(chunk) for chunk in _VARIED_DELTAS], "".join(_VARIED_DELTAS)),
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
        ("interrupted", "interrupted"),
        ("content_filter", "stop"),
    ],
    ids=["none", "stop", "length", "tool_calls", "error", "interrupted", "unknown-maps-stop"],
)
def test_finish_reason_mapping(raw: str | None, expected: str) -> None:
    """finish reason 词汇收敛：未知值（如 content_filter）映射 stop。"""
    assert map_finish_reason(raw) == expected


# ──────────────────── 流式退化循环检测（simurg 引擎 + 结构豁免仲裁） ────────────────────
# 回归矩阵 = docs/working/退化检测器替换_成熟件调研_2026-11-12.md §5（实施验收基线）。
# 正例必拦：emoji 刷屏 / 时间戳模板 / 整块重写 / 13 字符连发 / 混合≥60 轮 / 短应答
# 连发（finish 终判，它确实是退化内容）。负例必放：schema 倾倒 / 代码断言块 /
# 序号步骤行 / 正常叙述 / 14 份真实事故留证（零误杀）。


_REP_FIXTURES = Path(__file__).resolve().parent / "tests" / "fixtures" / "rep_evidence"


def _feed_stream(t: StreamTranslator, text: str, chunk: int = 20, channel: str = "thinking") -> list[tuple[str, dict[str, Any]]]:
    """按固定增量把文本喂进 translator 的指定通道，返回累计事件。"""
    emit = _thinking if channel == "thinking" else _text
    out: list[tuple[str, dict[str, Any]]] = []
    for i in range(0, len(text), chunk):
        if t.stopped:
            break
        out.extend((e.event, e.payload) for e in t.translate(emit(text[i : i + chunk])))
    return out


def _fixture_names() -> list[str]:
    return sorted(p.name for p in _REP_FIXTURES.glob("ev*.txt"))


def test_degeneration_guard_trips_on_emoji_thinking_flood() -> None:
    """思考通道原地刷屏（2026-11-07 事故形态）→ corrupt、闭块、此后零事件。"""
    t = StreamTranslator()
    emitted = _feed_stream(t, "🍦" * 10 * 60)

    assert t.stopped is True
    assert t.stop_reason == "repetition"
    assert t.stop_channel == "thinking"
    # 判定轮闭掉当前块，客户端收到完整的 block_end（不留半开块）
    assert emitted[-1][0] == "block_end"
    assert emitted[-1][1]["block"]["block_type"] == "reasoning"
    # 停止后不再产出任何事件（残余 chunk 一律丢弃）
    assert t.translate(_thinking("more")) == []
    # finish 仍可正常收尾（幂等契约不变）
    assert [e.event for e in t.finish("length")] == ["finish"]


def test_degeneration_guard_trips_on_timestamped_template_loop() -> None:
    """模板逐轮重写形态（2026-11-09 事故）：时间戳递增的模板打转必须判停。"""
    guard = DegenerationDetector()
    text = "".join(
        f"让我梳理当前状态：\n\n时间：2026-11-09 09:{i:02d}:{i:02d} 周一早上。\n\n"
        "系统又推进了一轮（无新事件）。\n\n保持极简。\n\n（守候中）\n"
        for i in range(20)
    )
    trips = [guard.feed("thinking", text[i : i + 20]) for i in range(0, len(text), 20)]

    assert any(trips) is True
    # 判停不拖到文本末尾（vetting 实测 corrupt@1140，检查点节奏 ~400 字符）
    assert trips.index(True) * 20 < len(text) - 200


def test_degeneration_guard_trips_on_periodic_block_rewrite() -> None:
    """整块状态重写形态（2026-11-10 事故，周期 ~700 字符）：必须判停且不拖尾。"""
    guard = DegenerationDetector()
    text = "".join(
        f"让我梳理当前状态：\n\n时间：2026-11-10 16:{i:02d}:{i:02d} 周二下午。\n\n"
        "收到系统通知：插件改造任务又失败——环境类失败，上下文二成。\n\n"
        "本轮动态变量注入了派发纪律和任务清单，变更只写有变化的单行。\n\n"
        "清单未完成项：视频脚本等待批准；管家模式插件改造验收待交卷。\n\n"
        "处置分析：环境类失败按原地续跑处置，上下文很轻，恢复合理。\n\n"
        "本轮动作：调用续跑，然后输出极简状态说明。\n"
        for i in range(12)
    )
    trips = [guard.feed("thinking", text[i : i + 20]) for i in range(0, len(text), 20)]

    assert any(trips) is True
    # 判停不拖到文本末尾（vetting 实测 corrupt@1140，检查点节奏 ~400 字符）
    assert trips.index(True) * 20 < len(text) - 200


def test_degeneration_guard_trips_on_periodic_phrase_flood() -> None:
    """周期连发（2026-11-10 事故）：同一 13 字符短语连发，须在开头几百字符内判停。"""
    guard = DegenerationDetector()
    text = "本轮一条链完成收紧与提交：" * 60
    trips = [guard.feed("thinking", text[i : i + 20]) for i in range(0, len(text), 20)]

    assert any(trips) is True
    assert trips.index(True) * 20 <= 600


def test_degeneration_guard_trips_on_mixed_code_prefix_then_template_loop() -> None:
    """混合形态：干净代码前缀（结构豁免放行）+ 尾部 ≥60 轮模板打转 → corrupt。

    前缀阶段的合法代码重复必须被结构豁免放行（engine 重建续跑），尾部模板
    打转（正文叙述，豁免不适用）必须拦断。
    """
    t = StreamTranslator()
    code = "".join(
        f"    def test_case_{i}(self):\n"
        f"        response = client.post('/api/x', json={{'a': {i}}})\n"
        "        assert response.status_code == 200\n"
        "        assert response.json()['ok'] is True\n\n"
        for i in range(8)
    )
    tail = "".join(
        f"让我梳理当前状态：\n\n时间戳{i}：继续等待。\n\n系统又推进了一轮（无新事件）。\n\n保持极简。\n\n（守候中）\n"
        for i in range(60)
    )
    _feed_stream(t, code, channel="text")
    assert t.stopped is False  # 代码前缀：豁免放行，不杀
    _feed_stream(t, tail, channel="thinking")

    assert t.stopped is True
    assert t.stop_reason == "repetition"
    assert t.stop_channel == "thinking"


def test_degeneration_flags_short_degenerate_answer_at_finish() -> None:
    """短应答「重复×60」是退化内容：流中不足检查点长度，finish 终判补拦。

    旧守卫被 400 字符起判门槛挡住（漏放）；simurg 的 finish 终判按拦口径。
    """
    t = StreamTranslator()
    _feed_stream(t, "重复" * 60, channel="text")

    assert t.stopped is False  # 流中无检查点，不中杀
    assert [e.event for e in t.finish("stop")] == ["block_end", "finish"]
    assert t.stopped is True
    assert t.stop_reason == "repetition"


def test_degeneration_finish_keeps_normal_short_answer() -> None:
    """正常短应答 finish 终判不误杀（拦口径的控制组）。"""
    t = StreamTranslator()
    _feed_stream(t, "好的，任务已完成，结果已写入配置文件。", channel="text")
    t.finish("stop")

    assert t.stopped is False


def test_degeneration_guard_exempts_json_schema_dump() -> None:
    """JSON schema 倾倒（合法结构重复，2026-11-10 误判回归形态）→ 豁免放行。

    simurg 裸判定会拦；仲裁：corrupt ∧ repetition 主因 ∧ 重复单元结构密集
    → 降级 suspect 放行 + 豁免留痕。
    """
    guard = DegenerationDetector()
    schema = '{\n  "properties": {\n' + "".join(
        f'    "field_{i}": {{\n      "type": "string",\n      "description": "",\n'
        '      "title": "字段",\n      "nullable": false,\n    },\n'
        for i in range(20)
    ) + "  }\n}\n"
    trips = [guard.feed("text", schema[i : i + 20]) for i in range(0, len(schema), 20)]
    assert not any(trips)  # 全程不杀
    assert guard.finish("text") is False  # finish 终判也不杀

    # 豁免留痕：suspect 锁存 + 命中信息可查（reasons 携 repetition、豁免标记为真）
    suspects = guard.suspect_channels()
    assert "text" in suspects
    assert suspects["text"].exempted is True
    assert suspects["text"].state == "suspect"
    assert any("repetition" in r for r in suspects["text"].reasons)


def test_degeneration_guard_exempts_code_assertion_blocks() -> None:
    """代码断言块（合法结构重复）→ 豁免放行：流中与 finish 终判均不杀。"""
    guard = DegenerationDetector()
    text = "".join(
        f"    def test_case_{i}(self):\n"
        f"        response = client.post('/api/x', json={{'a': {i}}})\n"
        "        assert response.status_code == 200\n"
        "        assert response.json()['ok'] is True\n\n"
        for i in range(8)
    )
    trips = [guard.feed("text", text[i : i + 20]) for i in range(0, len(text), 20)]

    assert not any(trips)
    assert guard.finish("text") is False


def test_degeneration_guard_ignores_numbered_step_lines() -> None:
    """序号步骤行（按序号递增的合法输出）不判退化。"""
    guard = DegenerationDetector()
    text = "".join(
        f"第{i}步：检查配置项 config_{i} 的取值并记录到清单，随后继续下一项。\n"
        for i in range(80)
    )
    trips = [guard.feed("thinking", text[i : i + 20]) for i in range(0, len(text), 20)]

    assert not any(trips)
    assert guard.finish("thinking") is False


def test_degeneration_guard_exempts_thinking_numeric_dump() -> None:
    """思考通道数字倾倒/编号清单不单独判杀（2026-11-12 装机误杀现场形态）。

    现场留证（15:19~15:42 六例）：思考通道 digit_frac 0.29~0.37、正文为
    编号核验清单，simurg 裸判定按数字倾倒杀断，截断后正文/工具调用为空。
    """
    guard = DegenerationDetector()
    text = "".join(
        f"{i}. 34 29 5 34 29 5 92 13 56 78 90 12 34 56 78 90 12 34 56 78 90 12 34 56 78 90\n"
        for i in range(1, 9)
    )
    trips = [guard.feed("thinking", text[i : i + 20]) for i in range(0, 400, 20)]

    assert not any(trips)
    suspects = guard.suspect_channels()
    assert "thinking" in suspects
    assert suspects["thinking"].exempted is True
    assert any("numeric dump" in r for r in suspects["thinking"].reasons)


def test_degeneration_guard_still_kills_text_numeric_dump() -> None:
    """正文通道数字倾倒仍判杀（思考通道豁免不扩到用户可见面）。"""
    t = StreamTranslator()
    text = "".join(
        f"{i}. 34 29 5 34 29 5 92 13 56 78 90 12 34 56 78 90 12 34 56 78 90 12 34 56 78 90\n"
        for i in range(1, 9)
    )
    _feed_stream(t, text[:400], channel="text")

    assert t.stopped is True


@pytest.mark.whitebox
def test_thinking_soft_verdict_rejects_repetition_mix() -> None:
    """思考软信号豁免只放非复读统计信号；混入复读信号仍判杀。"""
    import _llm_streaming  # noqa: PLC0415 —— 白盒直探内部实现（共享根裸模块）

    assert _llm_streaming._thinking_soft_verdict(
        ["numeric dump digit_frac=0.29", "semantic drift hamming=0.53"]
    )
    assert not _llm_streaming._thinking_soft_verdict(
        ["numeric dump digit_frac=0.29", "repetition loop rate=0.64 zlib=0.27"]
    )
    assert not _llm_streaming._thinking_soft_verdict([])


def test_degeneration_guard_exempts_thinking_structural_artifacts() -> None:
    """思考通道技术英文/符号密集内容不因 structural artifacts 判杀。

    现场留证（2026-11-12 17:58:08，1fe3b0bec40a）：371 字符凭据/端口排查英文，
    原文无任何重复，被 ``structural artifacts density=1.3`` 杀断 → 连续 10 次
    零产出 → llm_fail_loop。符号密度是技术说明的固有形态，不是退化。
    """
    guard = DegenerationDetector()
    text = (
        "Key blocker: /mnt/c unavailable in this container → can't read 装机 DB snapshot "
        "nor .env for admin password. But REST kernel at 127.0.0.1:9101 responds (401) — "
        "so kernel IS reachable! Just need credentials. The .env in repo root has "
        "AGENTOS_ADMIN_PASSWORD — but it's dev .env, may differ from 装机 .env. The task "
        "says 口令恒读 %APPDATA%/agentos/.env — which is inaccessible.\nWait"
    )
    trips = [guard.feed("thinking", text[i : i + 20]) for i in range(0, len(text), 20)]

    assert not any(trips)
    assert guard.finish("thinking") is False
    suspects = guard.suspect_channels()
    assert "thinking" in suspects
    assert suspects["thinking"].exempted is True
    assert any("structural artifacts" in r for r in suspects["thinking"].reasons)


def test_degeneration_guard_still_kills_text_structural_artifacts() -> None:
    """正文通道同形态内容仍判杀（思考通道豁免不外溢到用户可见面）。"""
    t = StreamTranslator()
    text = (
        "Key blocker: /mnt/c unavailable in this container → can't read 装机 DB snapshot "
        "nor .env for admin password. But REST kernel at 127.0.0.1:9101 responds (401) — "
        "so kernel IS reachable! Just need credentials. The .env in repo root has "
        "AGENTOS_ADMIN_PASSWORD — but it's dev .env, may differ from 装机 .env. The task "
        "says 口令恒读 %APPDATA%/agentos/.env — which is inaccessible.\nWait"
    )
    _feed_stream(t, text, channel="text")

    assert t.stopped is True


@pytest.mark.whitebox
def test_thinking_soft_verdict_accepts_structural_and_surprise() -> None:
    """白盒：structural artifacts / surprise collapse 单独出现属软信号。"""
    import _llm_streaming  # noqa: PLC0415 —— 白盒直探内部实现（共享根裸模块）

    assert _llm_streaming._thinking_soft_verdict(["structural artifacts density=1.3"])
    assert _llm_streaming._thinking_soft_verdict(
        ["surprise collapse low_frac=0.96", "semantic drift hamming=0.56"]
    )
    assert not _llm_streaming._thinking_soft_verdict(
        ["surprise collapse low_frac=0.96", "repetition loop rate=0.97 zlib=0.13"]
    )


def test_degeneration_guard_keeps_normal_long_text() -> None:
    """正常长输出（内容各异的叙述）不误判。"""
    t = StreamTranslator()
    text = "".join(
        f"第{i}步：检查配置项 config_{i} 的取值并记录到清单，随后继续下一项。\n"
        for i in range(80)
    )
    _feed_stream(t, text, channel="text")

    assert t.stopped is False
    assert t.stop_reason is None


def test_degeneration_guard_channels_are_independent() -> None:
    """正文与思考分通道独立计度：正文正常推进不会掩盖思考通道的退化。"""
    guard = DegenerationDetector()
    assert all(not guard.feed("text", f"不同的正文片段第{i}段。\n") for i in range(80))
    trips = [guard.feed("thinking", "🍦" * 10) for _ in range(60)]
    assert trips[-1] is True


def test_degeneration_detector_ignores_empty_feed() -> None:
    """空增量不参与判定（防御：adapter 不发空增量），不计字符。"""
    guard = DegenerationDetector()
    assert guard.feed("text", "") is False
    assert guard.seen("text") == 0


@pytest.mark.whitebox
def test_structural_ratio_empty_text_defensive_branch() -> None:
    """白盒：结构比空文本分支为 0（防御路径，公共面经单元导出不可达空串）。"""
    import _llm_streaming  # noqa: PLC0415 —— 白盒直探内部实现（共享根裸模块）

    assert _llm_streaming._structural_ratio("") == 0.0


@pytest.mark.parametrize("name", _fixture_names(), ids=lambda n: n[:14])
def test_real_incident_evidence_corpus_not_killed(name: str) -> None:
    """真实事故留证语料（2026-11-12 装机 12bb 误判现场，14 份）零误杀。

    出处 = 装机内核日志 kernel.log.2026-11-12 的「退化循环截断留证」块
    （旧守卫全部误杀；vetting 基线：simurg + 豁免仲裁 0/14）。文件名编码
    通道与原流总长（如 ev06_thinking_2358 = 思考通道、原流 2358 字符），
    正文为留证尾部原文。喂流 + finish 终判双口径均不得判退化。
    """
    names = _fixture_names()
    assert len(names) == 14, f"真实留证夹具应为 14 份，实际 {len(names)}"
    text = (_REP_FIXTURES / name).read_text(encoding="utf-8")
    assert len(text) >= 1000, f"夹具 {name} 内容异常（留证尾部应 ≥1000 字符）"

    guard = DegenerationDetector()
    trips = [guard.feed("thinking", text[i : i + 20]) for i in range(0, len(text), 20)]

    assert not any(trips), f"误杀真实留证 {name}（流中）"
    assert guard.finish("thinking") is False, f"误杀真实留证 {name}（finish 终判）"


def test_translator_repetition_evidence_carries_reasons_and_onset() -> None:
    """截断现场可取证：通道 + 字符数 + 尾部原文 + 判定原因 + 环起点（供给落日志方）。"""
    translator = StreamTranslator()
    accepted: list[str] = []

    for _ in range(80):
        if translator.stopped:
            break
        chunk = "🍦" * 10
        translator.translate(_thinking(chunk))
        accepted.append(chunk)

    evidence = translator.repetition_evidence(limit=200)

    assert translator.stop_reason == "repetition"
    assert evidence["channel"] == "thinking"
    assert evidence["chars"] == sum(len(c) for c in accepted)
    assert evidence["tail"] == "".join(accepted)[-200:]
    # 引擎自带可读判定原因 + 环起点（旧守卫自证缺口的修复面）
    assert evidence["reasons"], "corrupt 判定必须携带 reasons"
    assert isinstance(evidence["onset_char"], int)
    assert evidence["exempted"] is False


def test_translator_suspect_marks_without_killing() -> None:
    """suspect 态只标记不杀：流照常产出事件，suspect 通道留痕可查。"""
    t = StreamTranslator()
    text = "".join(
        f"让我梳理当前状态：\n\n时间：2026-11-09 09:{i:02d}:{i:02d} 周一早上。\n\n"
        "系统又推进了一轮（无新事件）。\n\n保持极简。\n\n（守候中）\n"
        for i in range(20)
    )
    emitted: list[tuple[str, dict[str, Any]]] = []
    for i in range(0, len(text), 20):
        if t.suspect and t.stopped:
            break
        emitted.extend((e.event, e.payload) for e in t.translate(_thinking(text[i : i + 20])))

    # suspect 先于 corrupt 出现（模板流 vetting 实证），且 suspect 态流未断
    assert t.suspect is True
    assert "thinking" in t.suspect_channels
    assert t.stopped is False or t.stop_channel == "thinking"


def test_translator_detector_injectable_and_ignores_empty_chunks() -> None:
    """检测器可注入（测试/调参口子）：注入实例被使用；空增量不参与判定。"""
    calls: list[tuple[str, str]] = []

    class _StubDetector:
        def feed(self, channel: str, text: str) -> bool:
            calls.append((channel, text))
            return bool(text) and text == "boom"

        def finish(self, channel: str) -> bool:
            return False

        def suspect_channels(self) -> dict[str, dict[str, Any]]:
            return {}

    t = StreamTranslator(degeneration_detector=_StubDetector())

    assert t.translate(_text("")) == []  # 空增量不喂检测器
    assert [(e.event, e.payload) for e in t.translate(_text("safe"))] == [
        ("block_start", {"index": 0, "block_type": "text"}),
        ("text_delta", {"index": 0, "text": "safe"}),
    ]
    assert calls == [("text", "safe")]
    events = [(e.event, e.payload) for e in t.translate(_text("boom"))]
    assert t.stopped is True
    assert events[-1][0] == "block_end"
