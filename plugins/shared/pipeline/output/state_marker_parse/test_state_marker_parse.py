# @feature: FP-0.2.〇 管道引擎(状态统一标记机制) | @vision: V1 可进化 | @ci: python-coverage
"""状态标记解析步测试——共享解析面 + 插件分发闭环（2026-09-28 设计）。

覆盖：
- parse 纯函数 ≥7 组正负例（正常/多标记取末/坏 JSON/非 dict/无标记/未闭合/
  span 区间正确/空文本）；
- 插件 execute 集成（fake tool-executor caller 注入）：卡键标记落账参数/
  render 回写/无标记零 state_updates 但 render 照写/非卡键零动作降级/未知账本
  键丢弃/update 服务失败不拖垮 render/state 载荷非 dict 拒绝。

mock 仅外部依赖（tool-executor 调用句柄注入），文件/解析面全真实。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_SHARED = Path(__file__).resolve().parents[3]
for p in (str(_SHARED), str(Path(__file__).resolve().parent)):
    if p not in sys.path:
        sys.path.insert(0, p)

from state_marker import parse_state_marker, strip_state_marker  # noqa: E402


def _load_plugin() -> Any:
    mod_name = "state_marker_parse_plugin_test"
    path = Path(__file__).resolve().parent / "plugin.py"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


# ── parse 纯函数 ─────────────────────────────────────────────────────────


def test_parse_normal_single_marker() -> None:
    text = '剧情正文。\n\n<state>\n{"state": {"好感度": 42}}\n</state>'
    out = parse_state_marker(text)
    assert out is not None
    assert out["entries"] == {"state": {"好感度": 42}}
    s, e = out["span"]
    assert text[s:e].startswith("<state>") and text[s:e].endswith("</state>")


def test_parse_multiple_takes_last() -> None:
    text = '<state>{"state": {"a": 1}}</state>\n中段\n<state>{"memory": "二"}</state>'
    out = parse_state_marker(text)
    assert out is not None
    assert out["entries"] == {"memory": "二"}


@pytest.mark.parametrize(
    "text",
    [
        "纯正文无标记",
        "",
        "<state>{bad json}</state>",
        "<state>[1,2]</state>",  # 内体本身非 dict → parse 层即 None
        "<state>未闭合 {\"a\": 1",
        "前缀 <state> 噪声无 JSON",
    ],
)
def test_parse_invalid_returns_none(text: str) -> None:
    assert parse_state_marker(text) is None


def test_parse_span_covers_full_marker() -> None:
    text = "甲<state>{\"state\": {}}</state>乙"
    out = parse_state_marker(text)
    assert out is not None
    assert text[out["span"][0]:out["span"][1]] == '<state>{"state": {}}</state>'


def test_strip_removes_and_tightens() -> None:
    text = '正文一。\n\n<state>{"state": {}}</state>\n\n\n正文二。'
    assert strip_state_marker(text) == "正文一。\n\n正文二。"


# ── 插件 execute 集成（fake caller 注入） ────────────────────────────────


class FakeCaller:
    """tool-executor 调用记录器：按 (tool_name, plugin_id) 分发预置回执。"""

    def __init__(self, render_text: str = "", fail_tools: set[str] | None = None):
        self.calls: list[tuple[str, str, dict]] = []
        self.render_text = render_text
        self.fail_tools = fail_tools or set()

    async def __call__(self, tool_name: str, plugin_id: str, args: dict) -> dict:
        self.calls.append((tool_name, plugin_id, args))
        if tool_name in self.fail_tools:
            raise RuntimeError(f"{tool_name} 服务失败")
        if tool_name == "character_state.render":
            return {"data": {"text": self.render_text}}
        return {"data": {"ok": True}}


def _state(text: str | None, agent_id: str | None = "mode_roleplay/luna") -> dict:
    state: dict[str, Any] = {}
    if text is not None:
        state["messages"] = [{"role": "assistant", "content": text}]
    if agent_id:
        state["agent.id"] = agent_id
    return state


def _run(plugin: Any, state: dict) -> dict:
    result = asyncio.run(plugin.execute(type("Ctx", (), {"state": state})()))
    return result.state_updates


def test_execute_card_key_marker_dispatch_and_render() -> None:
    mod = _load_plugin()
    caller = FakeCaller(render_text="好感度 42")
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)
    text = '她笑了笑。\n\n<state>{"state": {"好感度": 42}, "memory": "北境出身"}</state>'

    updates = _run(plugin, _state(text))

    # 落账两键（update 服务 + memory 工具 store）
    assert ("character_state.update", "character_state",
            {"card_id": "luna", "changes": {"好感度": 42}}) in caller.calls
    assert ("memory", "memory", {"action": "store", "content": "北境出身"}) in caller.calls
    # render 回写（下轮动态变量注入源）
    assert updates["context.character_state_text"] == "好感度 42"
    assert ("character_state.render", "character_state", {"card_id": "luna"}) in caller.calls
    # state_updates 载荷（前端小卡 + span 隐藏）
    payload = updates["context.state_updates"]
    assert payload["entries"] == {"state": {"好感度": 42}, "memory": {"stored": True}}
    assert text[payload["span"][0]:payload["span"][1]].startswith("<state>")
    assert payload["ts"]


def test_execute_no_marker_still_renders_no_state_updates() -> None:
    mod = _load_plugin()
    caller = FakeCaller(render_text="初始状态")
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)

    updates = _run(plugin, _state("纯剧情，无标记。"))

    assert "context.state_updates" not in updates, "无标记零载荷（前端零渲染语义）"
    assert updates["context.character_state_text"] == "初始状态", "render 每轮回写"


def test_execute_non_card_key_silent_skip() -> None:
    mod = _load_plugin()
    caller = FakeCaller()
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)

    updates = _run(plugin, _state('<state>{"state": {"a": 1}}</state>', agent_id="agentos"))

    assert "context.state_updates" not in updates, "非卡键状态账本不落账"
    assert not any(t == "character_state.update" for t, _, _ in caller.calls)
    assert "context.character_state_text" not in updates, "无卡键无渲染回写"


def test_execute_unknown_ledger_key_dropped() -> None:
    mod = _load_plugin()
    caller = FakeCaller()
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)

    updates = _run(plugin, _state('<state>{"mystery": 1, "state": {"b": 2}}</state>'))

    assert updates["context.state_updates"]["entries"] == {"state": {"b": 2}}
    assert not any(t == "mystery" for t, _, _ in caller.calls)


def test_execute_update_failure_does_not_block_render() -> None:
    mod = _load_plugin()
    caller = FakeCaller(render_text="仍回写", fail_tools={"character_state.update"})
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)

    updates = _run(plugin, _state('<state>{"state": {"a": 1}}</state>'))

    assert "context.state_updates" not in updates, "全部键失败 = 零渲染语义不写载荷"
    assert updates["context.character_state_text"] == "仍回写", "render 不被拖垮"


def test_execute_state_payload_non_dict_rejected() -> None:
    mod = _load_plugin()
    caller = FakeCaller()
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)

    updates = _run(plugin, _state('<state>{"state": "不是对象"}</state>'))

    assert "context.state_updates" not in updates, "载荷拒绝且无他键 = 零渲染语义"
    assert not any(t == "character_state.update" for t, _, _ in caller.calls)


def test_execute_no_messages_zero_writes() -> None:
    mod = _load_plugin()
    caller = FakeCaller()
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)

    updates = _run(plugin, _state(None, agent_id=None))

    assert updates == {}, "无消息无卡键 = 零动作"


def test_strip_by_span_removes_middle_marker_keeps_tail() -> None:
    """区间切除：标记在中间时前后正文都保留（切尾部丢后半正文是 bug）。"""
    from state_marker import strip_by_span

    text = '前段正文。<state>{"state": {}}</state>后段正文。'
    out = parse_state_marker(text)
    assert out is not None
    assert strip_by_span(text, out["span"]) == "前段正文。后段正文。"


def test_strip_by_span_out_of_range_returns_original() -> None:
    from state_marker import strip_by_span

    text = "原文不动。"
    assert strip_by_span(text, [5, 3]) == text  # 非法区间原样返回
    assert strip_by_span(text, [0, 999]) == text
