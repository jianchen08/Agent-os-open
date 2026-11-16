# @feature: FP-0.2.〇 管道引擎(状态统一标记机制) | @vision: V1 可进化 | @ci: python-coverage
"""状态标记解析步测试——共享解析面 + 插件分发闭环（2026-09-28 设计）。

覆盖：
- parse 纯函数 ≥7 组正负例（正常/多标记取末/坏 JSON/非 dict/无标记/未闭合/
  span 区间正确/空文本）；
- 插件 execute 集成（fake tool-executor caller 注入）：卡键标记落账参数/
  render 回写/无标记零 state_updates 但 render 照写/非卡键零动作降级/未知账本
  键丢弃/update 服务失败不拖垮 render/state 载荷非 dict 拒绝；
- 动作驱动基线（2026-10-13 ADR）：标记 state 字段默认关闭（解析忽略 + 审计
  source=state_field_ignored，不进 Validator 不落账、对用户不可见）；卡
  engine.allow_state_proposals: true 显式开启后恢复 LLM 提议合法来源
  （source=llm，Validator 全套执法）；choices/actions 作者声明清单全等选取
  → 声明效果（engine 落点与 behaviors 同键同轮合并经 Validator，game 落点
  转发宿主 delta 不进 story 账本）。

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

_SHARED = Path(__file__).resolve().parents[1]
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
    assert spec is not None
    assert spec.loader is not None
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
    assert text[s:e].startswith("<state>")
    assert text[s:e].endswith("</state>")


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

    def __init__(
        self,
        render_text: str = "",
        fail_tools: set[str] | None = None,
        state_values: dict[str, Any] | None = None,
    ):
        self.calls: list[tuple[str, str, dict]] = []
        self.render_text = render_text
        self.fail_tools = fail_tools or set()
        self.state_values = state_values or {}

    async def __call__(self, tool_name: str, plugin_id: str, args: dict) -> dict:
        self.calls.append((tool_name, plugin_id, args))
        if tool_name in self.fail_tools:
            raise RuntimeError(f"{tool_name} 服务失败")
        if tool_name == "character_state.render":
            return {"data": {"text": self.render_text}}
        if tool_name == "character_state.get":
            return {"data": {"values": dict(self.state_values)}}
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


# 回退开关显式开启（作者声明 engine.allow_state_proposals: true）的引擎声明。
_ALLOWED_ENGINE = {"allow_state_proposals": True}


def _allow_state(state: dict) -> dict:
    """state 字段合法来源形态：卡显式开启 allow_state_proposals。"""
    state["context.rp_engine"] = dict(_ALLOWED_ENGINE)
    return state


def test_execute_card_key_marker_dispatch_and_render() -> None:
    """默认关（作者未开启回退）：state 字段解析忽略（不落账、不进 Validator、
    不上卡），memory 通道照常——对用户不可见。"""
    mod = _load_plugin()
    caller = FakeCaller(render_text="好感度 42")
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)
    text = '她笑了笑。\n\n<state>{"state": {"好感度": 42}, "memory": "北境出身"}</state>'

    updates = _run(plugin, _state(text))

    # state 字段被忽略：无 character_state.update（好感度 42 不落账）
    assert not any(t == "character_state.update" for t, _, _ in caller.calls)
    # memory 通道零变化
    assert ("memory", "memory", {"action": "store", "content": "北境出身"}) in caller.calls
    # render 回写（下轮动态变量注入源）不受 state 忽略影响
    assert updates["context.character_state_text"] == "好感度 42"
    # 载荷只含 memory（state 字段对用户不可见）；span 照常承载隐藏区间
    payload = updates["context.state_updates"]
    assert payload["entries"] == {"memory": {"stored": True}}
    assert "rejected" not in payload
    assert text[payload["span"][0]:payload["span"][1]].startswith("<state>")
    assert payload["ts"]


def test_execute_state_field_allowed_settles_story_ledger() -> None:
    """回退开启（allow_state_proposals: true）：state 字段恢复合法来源，
    兼容态逐字落账 + memory 照常——既有 LLM 提议闭环原样。"""
    mod = _load_plugin()
    caller = FakeCaller(render_text="好感度 42")
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)
    text = '她笑了笑。\n\n<state>{"state": {"好感度": 42}, "memory": "北境出身"}</state>'

    updates = _run(plugin, _allow_state(_state(text)))

    # 落账两键（update 服务 + memory 工具 store）
    assert ("character_state.update", "character_state",
            {"card_id": "luna", "changes": {"好感度": 42}}) in caller.calls
    assert ("memory", "memory", {"action": "store", "content": "北境出身"}) in caller.calls
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
    """默认关：state 字段忽略后连同未知键一并不可见（零载荷）；回退开启形态
    保留白名单丢弃断言（未知键丢弃 + state 照常）。"""
    mod = _load_plugin()
    caller = FakeCaller()
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)

    updates = _run(plugin, _state('<state>{"mystery": 1, "state": {"b": 2}}</state>'))

    assert "context.state_updates" not in updates, "state 忽略 + 无他键 = 零载荷"
    assert not any(t == "character_state.update" for t, _, _ in caller.calls)

    allowed_caller = FakeCaller()
    allowed_updates = _run(
        mod.StateMarkerParsePlugin(invoke_caller=allowed_caller),
        _allow_state(_state('<state>{"mystery": 1, "state": {"b": 2}}</state>')),
    )
    assert allowed_updates["context.state_updates"]["entries"] == {"state": {"b": 2}}
    assert not any(t == "mystery" for t, _, _ in allowed_caller.calls), "白名单外键丢弃"


def test_execute_update_failure_does_not_block_render() -> None:
    mod = _load_plugin()
    caller = FakeCaller(render_text="仍回写", fail_tools={"character_state.update"})
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)

    updates = _run(plugin, _allow_state(_state('<state>{"state": {"a": 1}}</state>')))

    assert "context.state_updates" not in updates, "全部键失败 = 零渲染语义不写载荷"
    assert updates["context.character_state_text"] == "仍回写", "render 不被拖垮"


def test_execute_state_payload_non_dict_rejected() -> None:
    """state 载荷非 dict：默认关 = 与 dict 同款忽略+审计（不报错中断回合）；
    回退开启 = 维持拒绝（warning 忽略、不落账）。"""
    mod = _load_plugin()
    caller = FakeCaller()
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)

    updates = _run(plugin, _state('<state>{"state": "不是对象"}</state>'))

    assert "context.state_updates" not in updates, "忽略且无他键 = 零渲染语义"
    assert not any(t == "character_state.update" for t, _, _ in caller.calls)

    allowed_updates = _run(
        mod.StateMarkerParsePlugin(invoke_caller=FakeCaller()),
        _allow_state(_state('<state>{"state": "不是对象"}</state>')),
    )
    assert "context.state_updates" not in allowed_updates, "回退开启仍拒非 dict 载荷"


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


# ── RP 批 2-Ⅰ（2a/2b）：schema 态校验 + 审计（state 消费 context.rp_state_*） ──

_INT_WIDE = {"type": "int", "min": -100, "max": 100}


def _schema_state(text: str, schema: Any, workspace: str | None = None) -> dict:
    state = _state(text)
    state["context.rp_state_schema"] = schema
    if workspace is not None:
        state["workspace"] = workspace
        state["pipeline_id"] = "pipe-1"
        state["messages"][0]["seq"] = 7
    return state


def test_schema_mode_clamps_and_rejects_unknown_key() -> None:
    """回退开启 + schema 态：clamp 后值落账（update 只收 accepted）；schema 外
    键 rejected 不落账。"""
    mod = _load_plugin()
    caller = FakeCaller(render_text="好感度 100")
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)
    text = '<state>{"state": {"好感度": 500, "魔力": 9}}</state>'

    updates = _run(plugin, _allow_state(_schema_state(text, {"好感度": _INT_WIDE})))

    assert ("character_state.update", "character_state",
            {"card_id": "luna", "changes": {"好感度": 100}}) in caller.calls
    payload = updates["context.state_updates"]
    assert payload["entries"]["state"] == {"好感度": 100}
    rejected = payload["rejected"]
    assert rejected == [
        {
            "key": "魔力",
            "op": None,
            "from": None,
            "to": 9,
            "reason": "unknown_key",
            "layer": "hard",
        }
    ]


@pytest.mark.parametrize(
    ("state_payload", "summary_fragment"),
    [
        ('{"好感度": 500}', "好感度"),  # schema 内键越界值：不 clamp 不落账（原值进摘要）
        ('{"魔力": 9}', "魔力"),  # schema 外键：不裁决不落账
        ('{"好感度": "+5"}', "+5"),  # 相对增量：不结算
    ],
)
def test_state_field_default_off_ignored_and_audited(
    tmp_path: Any, state_payload: str, summary_fragment: str
) -> None:
    """默认关三形态：schema 在场也不进 Validator——state 字段解析忽略，唯一
    痕迹 = 审计流水 source=state_field_ignored（原样载荷摘要），对用户不可见
    （零载荷零落账），render/账本零扰动。"""
    import json as _json

    mod = _load_plugin()
    caller = FakeCaller(render_text="好感度 50", state_values={"好感度": 50})
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)
    text = f'<state>{{"state": {state_payload}}}</state>'
    state = _schema_state(text, {"好感度": _INT_WIDE}, workspace=str(tmp_path))

    updates = _run(plugin, state)

    assert not any(t == "character_state.update" for t, _, _ in caller.calls), (
        "state 字段不落账（Validator 未消费）"
    )
    assert "context.state_updates" not in updates, "对用户不可见（零载荷）"
    assert updates["context.character_state_text"], "render 照常（账本生命周期不受影响）"
    rows = [_json.loads(line) for line in
            (tmp_path / "rp_state_audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1, "state 字段整体一行审计（非逐键）"
    row = rows[0]
    assert row["source"] == "state_field_ignored"
    assert row["reason"] == "state_field_ignored"
    assert row["key"] == "state"
    assert row["op"] is None
    assert row["from"] is None
    assert summary_fragment in str(row["to"]), "审计带原样载荷摘要"
    assert row["idem"] == "pipe-1:7", "并入 story 批幂等空间"


def test_schema_mode_relative_uses_ledger_current() -> None:
    mod = _load_plugin()
    caller = FakeCaller(render_text="好感度 55", state_values={"好感度": 50})
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)
    text = '<state>{"state": {"好感度": "+5"}}</state>'

    updates = _run(plugin, _allow_state(_schema_state(text, {"好感度": _INT_WIDE})))

    assert ("character_state.update", "character_state",
            {"card_id": "luna", "changes": {"好感度": 55}}) in caller.calls
    assert updates["context.state_updates"]["entries"]["state"] == {"好感度": 55}


def test_schema_mode_all_rejected_still_renders_payload() -> None:
    """全 rejected：无 accepted 落账，但 rejected 列表随载荷出（前端可展开）。"""
    mod = _load_plugin()
    caller = FakeCaller(render_text="好感度 0")
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)
    text = '<state>{"state": {"未知键": 1}}</state>'

    updates = _run(plugin, _allow_state(_schema_state(text, {"好感度": _INT_WIDE})))

    assert not any(t == "character_state.update" for t, _, _ in caller.calls)
    assert "context.character_state_text" in updates, "render 不被拖垮"
    assert updates["context.state_updates"]["entries"] == {"state": {}}, "无 accepted 落账"
    assert updates["context.state_updates"]["rejected"][0]["reason"] == "unknown_key"


def test_audit_appended_to_session_workspace(tmp_path: Any) -> None:
    """默认关：state 字段审计一行（state_field_ignored + 摘要）append
    <ws>/rp_state_audit.jsonl（真实磁盘 IO）。"""
    import json as _json

    mod = _load_plugin()
    caller = FakeCaller(state_values={"好感度": 50})
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)
    text = '<state>{"state": {"好感度": "+5", "魔力": 1}}</state>'

    _run(plugin, _schema_state(text, {"好感度": _INT_WIDE}, workspace=str(tmp_path)))

    audit_path = tmp_path / "rp_state_audit.jsonl"
    assert audit_path.is_file(), "审计文件落在会话工作空间根"
    rows = [_json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1, "state 字段整体一行"
    assert rows[0]["source"] == "state_field_ignored"
    assert rows[0]["idem"] == "pipe-1:7", "幂等键 = pipeline_id:message seq"
    assert "+5" in str(rows[0]["to"]), "原样载荷摘要含相对增量"
    assert "魔力" in str(rows[0]["to"]), "原样载荷摘要含全键"


def test_audit_allowed_state_rows_source_llm(tmp_path: Any) -> None:
    """回退开启：state 字段恢复合法来源——审计行 source=llm，from/to 链完整
    （accepted + rejected 全流水照旧）。"""
    import json as _json

    mod = _load_plugin()
    caller = FakeCaller(state_values={"好感度": 50})
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)
    text = '<state>{"state": {"好感度": "+5", "魔力": 1}}</state>'

    _run(
        plugin,
        _allow_state(_schema_state(text, {"好感度": _INT_WIDE}, workspace=str(tmp_path))),
    )

    rows = [_json.loads(line) for line in
            (tmp_path / "rp_state_audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2, "accepted + rejected 全流水"
    assert {row["key"] for row in rows} == {"好感度", "魔力"}
    assert all(row["source"] == "llm" for row in rows), "回退开启来源=llm"
    assert rows[0]["idem"] == "pipe-1:7"
    accepted_row = next(row for row in rows if row["key"] == "好感度")
    assert (accepted_row["from"], accepted_row["to"]) == (50, 55)
    rejected_row = next(row for row in rows if row["key"] == "魔力")
    assert rejected_row["reason"] == "unknown_key"


def test_audit_replay_same_turn_no_double_append(tmp_path: Any) -> None:
    """审计追加幂等：同轮重放（同 pipeline_id + 同消息 seq）不双记。"""
    mod = _load_plugin()
    text = '<state>{"state": {"好感度": 42}}</state>'

    for _ in range(2):
        plugin = mod.StateMarkerParsePlugin(invoke_caller=FakeCaller())
        _run(plugin, _schema_state(text, {"好感度": _INT_WIDE}, workspace=str(tmp_path)))

    lines = (tmp_path / "rp_state_audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1, "同 idem 重放不双记"


def test_audit_missing_workspace_degrades_not_blocks(tmp_path: Any) -> None:
    """ws 不可得：跳过审计 + 不阻断——落账/render 照常（降级契约）。"""
    mod = _load_plugin()
    caller = FakeCaller(render_text="好感度 42")
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)
    text = '<state>{"state": {"好感度": 42}}</state>'

    updates = _run(plugin, _allow_state(_schema_state(text, {"好感度": _INT_WIDE})))

    assert ("character_state.update", "character_state",
            {"card_id": "luna", "changes": {"好感度": 42}}) in caller.calls
    assert updates["context.character_state_text"]  # render 照常
    assert not (tmp_path / "rp_state_audit.jsonl").exists()


def test_compat_ledger_result_equal_with_and_without_schema() -> None:
    """兼容态对照（最重）：同一输入有/无 schema 的账本结果逐字段相等
    （回退开启形态——state 字段在场）。"""
    text = '<state>{"state": {"好感度": 42}, "memory": "北境"}</state>'

    plain_caller = FakeCaller()
    plain = _run(
        _load_plugin().StateMarkerParsePlugin(invoke_caller=plain_caller),
        _allow_state(_state(text)),
    )
    schema_caller = FakeCaller()
    schema = _run(
        _load_plugin().StateMarkerParsePlugin(invoke_caller=schema_caller),
        _allow_state(_schema_state(text, {"好感度": _INT_WIDE})),
    )

    assert plain["context.state_updates"]["entries"] == (
        schema["context.state_updates"]["entries"]
    ), "账本载荷结果与 schema 无关（声明面零侵入现状）"
    assert plain_caller.calls == schema_caller.calls, "落账调用序列逐项相等"
    assert "rejected" not in schema["context.state_updates"], "全 accepted 时无 rejected 键"


def test_compat_no_schema_payload_has_no_rejected_key() -> None:
    """无声明卡（出厂卡形态，回退开启）：载荷零新增键——逐字透传锁定。"""
    mod = _load_plugin()
    caller = FakeCaller()
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)
    text = '<state>{"state": {"好感度": "任意类型不检查"}}</state>'

    updates = _run(plugin, _allow_state(_state(text)))

    payload = updates["context.state_updates"]
    assert "rejected" not in payload
    assert payload["entries"]["state"] == {"好感度": "任意类型不检查"}, "逐字透传"


# ── 降级分支：现值读取失败两态 / 审计坏行与 IO 失败 ──────────────────────


def test_schema_mode_get_failure_fails_closed(tmp_path: Any) -> None:
    """schema 态现值拿不到 = 校验前提缺失，fail-closed 整键跳过（零落账）。"""
    mod = _load_plugin()
    caller = FakeCaller(render_text="仍回写", fail_tools={"character_state.get"})
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)
    text = '<state>{"state": {"好感度": 42}}</state>'

    updates = _run(
        plugin,
        _allow_state(_schema_state(text, {"好感度": _INT_WIDE}, workspace=str(tmp_path))),
    )

    assert not any(t == "character_state.update" for t, _, _ in caller.calls)
    assert "context.state_updates" not in updates, "state 键跳过不写载荷"
    assert updates["context.character_state_text"] == "仍回写", "render 不被拖垮"
    assert not (tmp_path / "rp_state_audit.jsonl").exists(), "零落账零审计"


def test_compat_mode_get_failure_degrades_to_verbatim() -> None:
    """兼容态现值仅审计参考：get 失败降级空表，落账照常逐字透传。"""
    caller = FakeCaller(fail_tools={"character_state.get"})
    plugin = _load_plugin().StateMarkerParsePlugin(invoke_caller=caller)
    text = '<state>{"state": {"好感度": 42}}</state>'

    _run(plugin, _allow_state(_state(text)))

    assert ("character_state.update", "character_state",
            {"card_id": "luna", "changes": {"好感度": 42}}) in caller.calls


def test_audit_bad_line_skipped_in_idem_scan(tmp_path: Any) -> None:
    """审计文件含坏行：幂等扫描跳过坏行继续判（追加照常）。"""
    audit = tmp_path / "rp_state_audit.jsonl"
    audit.write_text("{这不是JSON}\n", encoding="utf-8")
    text = '<state>{"state": {"好感度": 42}}</state>'

    _run(
        _load_plugin().StateMarkerParsePlugin(invoke_caller=FakeCaller()),
        _schema_state(text, {"好感度": _INT_WIDE}, workspace=str(tmp_path)),
    )

    lines = audit.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2, "坏行不阻断追加（保留 + 新一行）"


def test_audit_io_failure_degrades_not_blocks(tmp_path: Any) -> None:
    """审计 IO 失败（ws 指向文件而非目录）= warning 跳过，落账照常。"""
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("占用路径", encoding="utf-8")
    mod = _load_plugin()
    caller = FakeCaller(render_text="好感度 42")
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)
    text = '<state>{"state": {"好感度": 42}}</state>'

    updates = _run(
        plugin,
        _allow_state(_schema_state(text, {"好感度": _INT_WIDE}, workspace=str(blocker))),
    )

    assert ("character_state.update", "character_state",
            {"card_id": "luna", "changes": {"好感度": 42}}) in caller.calls
    assert updates["context.character_state_text"] == "好感度 42"


# ── RP 批 2-Ⅱ（2c）：behaviors 声明层——trigger 命中代理变更进同一结算 ──────

_ANGER_TEXT = "怒目而视"
_ANGER = {"怒目": {"trigger": _ANGER_TEXT, "effect": {"好感度": "-3"}}}


def _behavior_state(
    text: str | None,
    schema: Any = None,
    behaviors: Any = None,
    workspace: str | None = None,
    agent_id: str | None = "mode_roleplay/luna",
) -> dict:
    state = _state(text, agent_id=agent_id)
    if schema is not None:
        state["context.rp_state_schema"] = schema
    if behaviors is not None:
        state["context.rp_state_behaviors"] = behaviors
    if workspace is not None:
        state["workspace"] = workspace
        state["pipeline_id"] = "pipe-1"
        assert isinstance(state["messages"][0], dict)
        state["messages"][0]["seq"] = 7
    return state


def _read_audit(tmp_path: Any) -> list[dict[str, Any]]:
    import json as _json

    audit = tmp_path / "rp_state_audit.jsonl"
    assert audit.is_file(), "审计文件落在会话工作空间根"
    return [_json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]


def test_behavior_hit_without_marker_settles_and_audits(tmp_path: Any) -> None:
    """无标记轮 behavior 命中：代理变更照常结算落账 + 审计 source=behavior；
    载荷仍随标记形态（无标记 = 不写 context.state_updates，零渲染语义不变）。"""
    mod = _load_plugin()
    caller = FakeCaller(state_values={"好感度": 50})
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)

    updates = _run(
        plugin,
        _behavior_state(
            f"她{_ANGER_TEXT}，一言不发。",
            schema={"好感度": _INT_WIDE},
            behaviors=_ANGER,
            workspace=str(tmp_path),
        ),
    )

    assert ("character_state.update", "character_state",
            {"card_id": "luna", "changes": {"好感度": 47}}) in caller.calls
    assert "context.state_updates" not in updates, "无标记轮零渲染语义保持"
    rows = _read_audit(tmp_path)
    assert len(rows) == 1
    assert rows[0]["source"] == "behavior"
    assert rows[0]["behavior"] == "怒目"
    assert rows[0]["from"] == 50
    assert rows[0]["to"] == 47
    assert rows[0]["idem"] == "pipe-1:7", "behavior 变更并入同轮同一幂等批次"


def test_behavior_and_marker_same_key_relative_stack(tmp_path: Any) -> None:
    """回退开启：标记（llm）+5 与 behavior -3 同键——结算序 llm 先、behaviors 后
    依次叠加（50→55→52），审计两行各带来源（llm / behavior），from 链显示前序值。"""
    mod = _load_plugin()
    caller = FakeCaller(state_values={"好感度": 50})
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)
    text = f'她{_ANGER_TEXT}。\n\n<state>{{"state": {{"好感度": "+5"}}}}</state>'

    state = _behavior_state(
        text,
        schema={"好感度": _INT_WIDE},
        behaviors=_ANGER,
        workspace=str(tmp_path),
    )
    updates = _run(plugin, _allow_state(state))

    assert ("character_state.update", "character_state",
            {"card_id": "luna", "changes": {"好感度": 52}}) in caller.calls
    payload = updates["context.state_updates"]
    assert payload["entries"]["state"] == {"好感度": 52}
    rows = _read_audit(tmp_path)
    assert len(rows) == 2
    assert (rows[0]["source"], rows[0]["from"], rows[0]["to"]) == ("llm", 50, 55)
    assert (rows[1]["source"], rows[1]["behavior"], rows[1]["from"], rows[1]["to"]) == (
        "behavior", "怒目", 55, 52,
    )


def test_behavior_and_marker_same_key_default_off_behavior_only(tmp_path: Any) -> None:
    """默认关：同轮标记（被忽略）+ behavior 命中 = 只有声明效果结算（50→47），
    审计两行各归其位（state_field_ignored / behavior），llm 增量不进结算链。"""
    mod = _load_plugin()
    caller = FakeCaller(state_values={"好感度": 50})
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)
    text = f'她{_ANGER_TEXT}。\n\n<state>{{"state": {{"好感度": "=90"}}}}</state>'

    _run(
        plugin,
        _behavior_state(
            text,
            schema={"好感度": _INT_WIDE},
            behaviors=_ANGER,
            workspace=str(tmp_path),
        ),
    )

    assert ("character_state.update", "character_state",
            {"card_id": "luna", "changes": {"好感度": 47}}) in caller.calls, (
        "被忽略的标记绝对赋值（=90）不影响结算链"
    )
    rows = _read_audit(tmp_path)
    assert [(row["source"], row["from"]) for row in rows] == [
        ("state_field_ignored", None),
        ("behavior", 50),
    ], "忽略行 from 不承载（未进结算），to 为载荷摘要"
    assert '"=90"' in str(rows[0]["to"]), "原样载荷摘要随行"


@pytest.mark.parametrize(
    ("marker_expr", "effect", "expected"),
    [
        ("+5", "=30", 30),  # behavior 绝对覆盖标记相对
        ("=50", "+3", 53),  # behavior 相对以标记绝对结果为基线
    ],
)
def test_behavior_absolute_override_forms(
    marker_expr: str, effect: str, expected: int
) -> None:
    """同键合并两形态：相对叠加 + 绝对覆盖（契约锁定「缺省取最后」的结算序）；
    回退开启形态（标记 llm 源在场）。"""
    mod = _load_plugin()
    caller = FakeCaller(state_values={"好感度": 50})
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)
    text = f'她{_ANGER_TEXT}。<state>{{"state": {{"好感度": "{marker_expr}"}}}}</state>'

    state = _behavior_state(
        text,
        schema={"好感度": _INT_WIDE},
        behaviors={"怒目": {"trigger": _ANGER_TEXT, "effect": {"好感度": effect}}},
    )
    updates = _run(plugin, _allow_state(state))

    assert ("character_state.update", "character_state",
            {"card_id": "luna", "changes": {"好感度": expected}}) in caller.calls
    assert updates["context.state_updates"]["entries"]["state"] == {"好感度": expected}


def test_behavior_matches_stripped_text_not_marker_segment() -> None:
    """标记剥离后正文才参与匹配：trigger 词只出现在标记段内 = 不触发；
    同 behaviors 换正文命中词 = 触发（对照）。回退开启形态（标记自身变更在场）。"""
    mod = _load_plugin()
    behaviors = {"密语": {"trigger": "state", "effect": {"好感度": "-1"}}}
    marker_only = '<state>{"state": {"好感度": 1}}</state>'

    caller = FakeCaller()
    _run(
        mod.StateMarkerParsePlugin(invoke_caller=caller),
        _allow_state(
            _behavior_state(marker_only, schema={"好感度": _INT_WIDE}, behaviors=behaviors)
        ),
    )
    marker_only_updates = [
        args["changes"] for t, _, args in caller.calls if t == "character_state.update"
    ]
    assert marker_only_updates == [{"好感度": 1}], (
        "标记段内文字（含标签与 JSON）不参与 trigger 匹配——只有标记自身变更落账"
    )

    caller2 = FakeCaller(state_values={"好感度": 10})
    _run(
        mod.StateMarkerParsePlugin(invoke_caller=caller2),
        _allow_state(
            _behavior_state(
                "前段 state 剧情文字。<state>{\"state\": {\"好感度\": 1}}</state>",
                schema={"好感度": _INT_WIDE},
                behaviors=behaviors,
            )
        ),
    )
    assert ("character_state.update", "character_state",
            {"card_id": "luna", "changes": {"好感度": 0}}) in caller2.calls, (
        "剥离后正文的命中词照常触发（标记先置 1、behavior -1 后 = 0）"
    )


def test_behavior_multi_same_key_settles_in_declaration_order() -> None:
    """多 behavior 同键按声明序结算（+90 先 -3 后：140 clamp 100 → 97；
    反序声明：-3 先 +90 后：47 → 137 clamp 100）——序敏感锁定。"""
    mod = _load_plugin()
    schema = {"好感度": _INT_WIDE}
    forward = {
        "涨": {"trigger": _ANGER_TEXT, "effect": {"好感度": "+90"}},
        "落": {"trigger": _ANGER_TEXT, "effect": {"好感度": "-3"}},
    }
    caller = FakeCaller(state_values={"好感度": 50})
    _run(
        mod.StateMarkerParsePlugin(invoke_caller=caller),
        _behavior_state(f"她{_ANGER_TEXT}", schema=schema, behaviors=forward),
    )
    assert ("character_state.update", "character_state",
            {"card_id": "luna", "changes": {"好感度": 97}}) in caller.calls

    backward = {
        "落": {"trigger": _ANGER_TEXT, "effect": {"好感度": "-3"}},
        "涨": {"trigger": _ANGER_TEXT, "effect": {"好感度": "+90"}},
    }
    caller2 = FakeCaller(state_values={"好感度": 50})
    _run(
        mod.StateMarkerParsePlugin(invoke_caller=caller2),
        _behavior_state(f"她{_ANGER_TEXT}", schema=schema, behaviors=backward),
    )
    assert ("character_state.update", "character_state",
            {"card_id": "luna", "changes": {"好感度": 100}}) in caller2.calls


def test_behavior_miss_and_bad_regex_fail_soft() -> None:
    """trigger 不命中 = 零变更；坏 trigger regex = 该 behavior 跳过不阻断
    （其余 behavior 照常生效）；声明形态非法（非映射/缺 trigger/effect 非映射）
    同跳过。"""
    mod = _load_plugin()

    miss_caller = FakeCaller()
    _run(
        mod.StateMarkerParsePlugin(invoke_caller=miss_caller),
        _behavior_state("风平浪静的一段话。", schema={"好感度": _INT_WIDE}, behaviors=_ANGER),
    )
    assert not any(t == "character_state.update" for t, _, _ in miss_caller.calls)

    malformed = {
        "非映射": "垃圾声明",  # decl 非 dict
        "缺trigger": {"effect": {"好感度": "-1"}},  # trigger 缺失
        "坏effect": {"trigger": _ANGER_TEXT, "effect": "+1"},  # effect 非映射
        "好": {"trigger": _ANGER_TEXT, "effect": {"好感度": "-3"}},
    }
    ok_caller = FakeCaller(state_values={"好感度": 50})
    _run(
        mod.StateMarkerParsePlugin(invoke_caller=ok_caller),
        _behavior_state(f"她{_ANGER_TEXT}", schema={"好感度": _INT_WIDE}, behaviors=malformed),
    )
    assert ("character_state.update", "character_state",
            {"card_id": "luna", "changes": {"好感度": 47}}) in ok_caller.calls, (
        "形态非法只跳过自身，合法 behavior 不受牵连"
    )

    mixed = {
        "坏": {"trigger": "([坏", "effect": {"好感度": "-99"}},
        "好": {"trigger": _ANGER_TEXT, "effect": {"好感度": "-3"}},
    }
    ok_caller2 = FakeCaller(state_values={"好感度": 50})
    _run(
        mod.StateMarkerParsePlugin(invoke_caller=ok_caller2),
        _behavior_state(f"她{_ANGER_TEXT}", schema={"好感度": _INT_WIDE}, behaviors=mixed),
    )
    assert ("character_state.update", "character_state",
            {"card_id": "luna", "changes": {"好感度": 47}}) in ok_caller2.calls, (
        "坏 regex 只跳过自身，其余 behavior 不受牵连"
    )


def test_behavior_schema_mode_unknown_effect_key_rejected_in_payload() -> None:
    """schema 态 behavior effect 打在 schema 外键 = rejected（执法面与标记一致），
    载荷 rejected 行带 behavior 来源。"""
    mod = _load_plugin()
    caller = FakeCaller(state_values={})
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)

    updates = _run(
        plugin,
        _behavior_state(
            f"她{_ANGER_TEXT}。<state>{{\"state\": {{\"好感度\": 1}}}}</state>",
            schema={"好感度": _INT_WIDE},
            behaviors={"怒目": {"trigger": _ANGER_TEXT, "effect": {"魔力": 5}}},
        ),
    )

    rejected = updates["context.state_updates"]["rejected"]
    assert [r["key"] for r in rejected if r.get("source") == "behavior"] == ["魔力"]
    behavior_row = next(r for r in rejected if r.get("source") == "behavior")
    assert behavior_row["behavior"] == "怒目"
    assert behavior_row["reason"] == "unknown_key"
    for tool, _, args in caller.calls:
        if tool == "character_state.update":
            assert "魔力" not in args["changes"], "schema 外键不落账"


def test_behavior_compat_mode_passthrough_verbatim() -> None:
    """兼容态（无 schema 卡）：behavior effect 逐字透传落账（不解释 op）。"""
    mod = _load_plugin()
    caller = FakeCaller()
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)

    _run(
        plugin,
        _behavior_state(f"她{_ANGER_TEXT}", behaviors={"怒目": {"trigger": _ANGER_TEXT, "effect": {"好感度": "任意"}}}),
    )

    assert ("character_state.update", "character_state",
            {"card_id": "luna", "changes": {"好感度": "任意"}}) in caller.calls


def test_behavior_non_card_key_settlement_skipped(tmp_path: Any) -> None:
    """非卡键会话：behavior 代理变更不落账（归属卡未定），零审计零载荷。"""
    mod = _load_plugin()
    caller = FakeCaller()
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)

    updates = _run(
        plugin,
        _behavior_state(
            f"她{_ANGER_TEXT}",
            schema={"好感度": _INT_WIDE},
            behaviors=_ANGER,
            workspace=str(tmp_path),
            agent_id="agentos",
        ),
    )

    assert not any(t == "character_state.update" for t, _, _ in caller.calls)
    assert "context.state_updates" not in updates
    assert not (tmp_path / "rp_state_audit.jsonl").exists()


def test_behavior_audit_replay_same_turn_no_double_append(tmp_path: Any) -> None:
    """审计幂等沿用 2-Ⅰ idem 键：behavior 变更并入同批，重放不双记。"""
    mod = _load_plugin()
    state = _behavior_state(
        f"她{_ANGER_TEXT}",
        schema={"好感度": _INT_WIDE},
        behaviors=_ANGER,
        workspace=str(tmp_path),
    )
    for _ in range(2):
        plugin = mod.StateMarkerParsePlugin(invoke_caller=FakeCaller(state_values={"好感度": 50}))
        _run(plugin, state)

    lines = (tmp_path / "rp_state_audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1, "同 idem 重放整批跳过"


def test_no_behaviors_declaration_zero_change() -> None:
    """无 behaviors 声明卡（出厂卡形态）：不带 context.rp_state_behaviors =
    匹配零动作（回退开启形态下标记流照常对照——零变化源于无 behaviors 声明，
    非 state 字段忽略）。"""
    mod = _load_plugin()
    caller = FakeCaller()
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)
    text = '<state>{"state": {"好感度": 42}}</state>'

    state = _allow_state(_behavior_state(text, schema={"好感度": _INT_WIDE}))
    updates = _run(plugin, state)

    assert ("character_state.update", "character_state",
            {"card_id": "luna", "changes": {"好感度": 42}}) in caller.calls
    assert updates["context.state_updates"]["entries"]["state"] == {"好感度": 42}
    assert "rejected" not in updates["context.state_updates"]


# ── RP 批 3：cheat 旁路接线 + 审计来源 + 徽标载荷 ─────────────────────────


def _cheat_state(
    text: str,
    schema: Any,
    workspace: str,
    *,
    cheating: bool = True,
    engine: Any = None,
) -> dict:
    state = _schema_state(text, schema, workspace=str(workspace))
    if cheating:
        state["meta.cheating"] = True
    if engine is not None:
        state["context.rp_engine"] = engine
    return state


def test_cheating_round_bypasses_validation_and_audits_source_cheat(tmp_path: Any) -> None:
    """四断言之二·旁路：作弊轮普通键逐字透传（不 clamp），审计全流水 source=cheat。"""
    import json as _json

    mod = _load_plugin()
    caller = FakeCaller(state_values={"好感度": 50})
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)
    text = '<state>{"state": {"好感度": 999, "魔力": 1}}</state>'

    _run(
        plugin,
        _cheat_state(
            text, {"好感度": _INT_WIDE}, str(tmp_path),
            engine={"allow_state_proposals": True},
        ),
    )

    assert ("character_state.update", "character_state",
            {"card_id": "luna", "changes": {"好感度": 999, "魔力": 1}}) in caller.calls, (
        "越界值不 clamp、schema 外键不拒——旁路透传"
    )
    rows = [_json.loads(line) for line in
            (tmp_path / "rp_state_audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2
    assert all(row["source"] == "cheat" for row in rows), "作弊轮缺省来源=cheat"
    assert all(row["reason"] == "cheat_bypass" for row in rows)


def test_cheating_round_keep_state_machine_still_enforces(tmp_path: Any) -> None:
    """四断言之二·例外：keep_state_machine 键域作弊期间仍全校验（rejected 照旧）。"""
    mod = _load_plugin()
    caller = FakeCaller(state_values={"好感度": 50})
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)
    text = '<state>{"state": {"好感度": "暴涨", "魔力": 1}}</state>'
    engine = {
        "allow_state_proposals": True,
        "allow_cheat": True,
        "cheat_overrides": {"keep_state_machine": ["npc.*.好感度"]},
    }
    schema = {"npc": {"苏清婉": {"好感度": _INT_WIDE}}, "world": {"魔力": {"type": "int"}}}

    updates = _run(
        plugin,
        _cheat_state(text, schema, str(tmp_path), engine=engine),
    )

    assert ("character_state.update", "character_state",
            {"card_id": "luna", "changes": {"魔力": 1}}) in caller.calls, "非保留键旁路落账"
    assert updates["context.state_updates"]["rejected"][0]["key"] == "好感度"
    assert updates["context.state_updates"]["rejected"][0]["reason"] == "type_mismatch"

    import json as _json
    rows = [_json.loads(line) for line in
            (tmp_path / "rp_state_audit.jsonl").read_text(encoding="utf-8").splitlines()]
    by_key = {row["key"]: row for row in rows}
    assert by_key["好感度"]["reason"] == "type_mismatch"
    assert by_key["好感度"]["source"] == "cheat", "保留键域审计仍标记作弊轮"
    assert by_key["魔力"]["reason"] == "cheat_bypass"


@pytest.mark.parametrize(
    "engine",
    [
        {"allow_state_proposals": True, "cheat_overrides": {"keep_state_machine": True}},
        {
            "allow_state_proposals": True,
            "cheat_overrides": {"keep_state_machine": {"坏形态": 1}},
        },
        {
            "allow_state_proposals": True,
            "cheat_overrides": {"keep_state_machine": [123]},
        },
    ],
)
def test_cheating_keep_unparseable_fails_closed(tmp_path: Any, engine: Any) -> None:
    """keep 声明不可解析 = fail-closed 全保留执法（作者保留项不被静默丢弃）。"""
    mod = _load_plugin()
    caller = FakeCaller(state_values={"好感度": 50})
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)
    text = '<state>{"state": {"好感度": "暴涨"}}</state>'

    updates = _run(
        plugin,
        _cheat_state(text, {"好感度": _INT_WIDE}, str(tmp_path), engine=engine),
    )

    rejected = updates.get("context.state_updates", {}).get("rejected", [])
    assert any(row["key"] == "好感度" for row in rejected), "全键域保留 = 类型错照旧被拒"


def test_cheating_bad_engine_shape_treated_as_absent(tmp_path: Any) -> None:
    """context.rp_engine 非映射（lint 面已警告的运行时兜底）= 回退开关按缺席
    处理（默认关）：state 字段忽略+审计，作弊轮不炸不旁路。"""
    import json as _json

    mod = _load_plugin()
    caller = FakeCaller(state_values={"好感度": 50})
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)
    text = '<state>{"state": {"好感度": 999}}</state>'

    _run(
        plugin,
        _cheat_state(text, {"好感度": _INT_WIDE}, str(tmp_path), engine="不是映射"),
    )

    assert not any(t == "character_state.update" for t, _, _ in caller.calls), (
        "engine 非映射 = 开关缺席默认关，state 字段不落账"
    )
    rows = [_json.loads(line) for line in
            (tmp_path / "rp_state_audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [row["source"] for row in rows] == ["state_field_ignored"], (
        "行级来源不被作弊轮缺省 cheat 覆盖"
    )


def test_cheating_round_behavior_rows_keep_behavior_source(tmp_path: Any) -> None:
    """作弊轮 behavior 行自带 source=behavior：行级归因不被缺省 cheat 覆盖。"""
    mod = _load_plugin()
    caller = FakeCaller(state_values={"好感度": 50})
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)

    state = _cheat_state(
        f"她{_ANGER_TEXT}。",
        {"好感度": _INT_WIDE},
        str(tmp_path),
        engine={"cheat_overrides": {"keep_state_machine": True}},
    )
    state["context.rp_state_behaviors"] = _ANGER
    _run(plugin, state)

    import json as _json
    rows = [_json.loads(line) for line in
            (tmp_path / "rp_state_audit.jsonl").read_text(encoding="utf-8").splitlines()]
    behavior_rows = [row for row in rows if row.get("source") == "behavior"]
    assert behavior_rows, "behavior 行保留 behavior 归因"
    assert all(row.get("behavior") == "怒目" for row in behavior_rows)


def test_badge_payload_meta_cheating_rides_entries(tmp_path: Any) -> None:
    """四断言之四·徽标载荷：作弊轮 entries.meta.cheating=true；off 后不再写入。"""
    mod = _load_plugin()
    plugin = mod.StateMarkerParsePlugin(invoke_caller=FakeCaller())
    text = '<state>{"state": {"好感度": 42}}</state>'
    engine = {"allow_state_proposals": True}

    on_payload = _run(
        plugin,
        _cheat_state(text, {"好感度": _INT_WIDE}, str(tmp_path), engine=engine),
    )["context.state_updates"]
    assert on_payload["entries"].get("meta") == {"cheating": True}, "徽标载荷随 entries 透传"

    off_payload = _run(
        plugin,
        _cheat_state(
            text, {"好感度": _INT_WIDE}, str(tmp_path), cheating=False, engine=engine,
        ),
    )["context.state_updates"]
    assert "meta" not in off_payload["entries"], "/cheat off 后徽标消失"


def test_non_cheating_round_audit_source_by_switch(tmp_path: Any) -> None:
    """非作弊轮审计来源随回退开关：开启 = llm（合法来源）；关闭 = 仅
    state_field_ignored 忽略行；两形态都无 cheat_bypass。"""
    import json as _json

    mod = _load_plugin()
    text = '<state>{"state": {"好感度": "+5"}}</state>'

    allowed_plugin = mod.StateMarkerParsePlugin(
        invoke_caller=FakeCaller(state_values={"好感度": 50})
    )
    state = _allow_state(_schema_state(text, {"好感度": _INT_WIDE}, workspace=str(tmp_path)))
    _run(allowed_plugin, state)

    rows = [_json.loads(line) for line in
            (tmp_path / "rp_state_audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [row["source"] for row in rows] == ["llm"], "回退开启 = llm 来源"
    assert all(row["reason"] != "cheat_bypass" for row in rows)

    off_path = tmp_path / "off"
    off_path.mkdir()
    off_plugin = mod.StateMarkerParsePlugin(invoke_caller=FakeCaller(state_values={"好感度": 50}))
    _run(
        off_plugin,
        _schema_state(text, {"好感度": _INT_WIDE}, workspace=str(off_path)),
    )
    off_rows = [_json.loads(line) for line in
                (off_path / "rp_state_audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [row["source"] for row in off_rows] == ["state_field_ignored"], "关闭 = 仅忽略行"


# ── RP 批 4-Ⅰ：game 域分域（game.* 键 delta 转发宿主，不入 story 账本）──────────


def _ws_state(state: dict[str, Any], tmp_path: Any) -> dict[str, Any]:
    """story 审计同款工作空间装配（ws + pipeline_id + 消息 seq）。"""
    state["workspace"] = str(tmp_path)
    state["pipeline_id"] = "pipe-1"
    state["messages"][0]["seq"] = 7
    return state


def test_game_keys_delta_forwarded_not_in_story_ledger(tmp_path: Any) -> None:
    """game.* 键分流：不入 character_state 账本，产出 game_host_delta + 推送 +
    game_delta 审计（与 story 批分 idem 空间）；story 键照常落账。"""
    mod = _load_plugin()
    caller = FakeCaller()
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)
    text = '<state>{"state": {"game.hp": "+5", "好感度": 42}}</state>'

    updates = _run(plugin, _ws_state(_allow_state(_state(text)), tmp_path))

    assert ("character_state.update", "character_state",
            {"card_id": "luna", "changes": {"好感度": 42}}) in caller.calls, "story 键照常落账"
    delta = updates["game_host_delta"]
    assert delta["turn_seq"] == 7
    assert delta["deltas"] == [{"key": "game.hp", "value": "+5"}], "raw 值转发（宿主权威，不解释 op）"
    assert delta["ts"]
    assert ("mode.game.push_delta", "mode_roleplay", {
        "session_id": "pipe-1", "turn_seq": 7, "deltas": [{"key": "game.hp", "value": "+5"}],
    }) in caller.calls, "跨插件服务推送宿主（显式 plugin_id）"

    rows = _read_audit(tmp_path)
    game_rows = [row for row in rows if row["idem"] == "pipe-1:7:game"]
    story_rows = [row for row in rows if row["idem"] == "pipe-1:7"]
    assert [row["key"] for row in game_rows] == ["game.hp"]
    assert game_rows[0]["source"] == "game_delta"
    assert game_rows[0]["reason"] == "game_delta"
    assert game_rows[0]["to"] == "+5"
    assert [row["key"] for row in story_rows] == ["好感度"], "story 批 idem 空间不受染"
    assert updates["context.state_updates"]["entries"]["state"] == {"好感度": 42}, \
        "story 状态卡不出现 game 键"


def test_state_field_game_keys_default_off_not_forwarded(tmp_path: Any) -> None:
    """默认关：state 字段中的 game.* 键同随字段忽略——不转发宿主、不进 story
    账本，唯一痕迹 = state_field_ignored 审计行。"""
    mod = _load_plugin()
    caller = FakeCaller()
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)
    text = '<state>{"state": {"game.hp": "+5", "好感度": 42}}</state>'

    updates = _run(plugin, _ws_state(_state(text), tmp_path))

    assert "game_host_delta" not in updates, "game.* 键随字段忽略不转发"
    assert not any(t == "mode.game.push_delta" for t, _, _ in caller.calls)
    assert not any(t == "character_state.update" for t, _, _ in caller.calls)
    rows = _read_audit(tmp_path)
    assert [row["source"] for row in rows] == ["state_field_ignored"], "唯一痕迹审计行"
    assert all(not row["idem"].endswith(":game") for row in rows), "无 game 审计批"


def test_game_behavior_effect_forwarded_with_attribution(tmp_path: Any) -> None:
    """behaviors effect 的 game.* 键走 delta 通道：来源 behavior 随行（delta 行与审计行）。"""
    mod = _load_plugin()
    caller = FakeCaller()
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)
    state = _state("他怒吼一声，挥剑砍杀。")
    state["context.rp_state_behaviors"] = {
        "挥砍": {"trigger": "挥剑", "effect": {"game.hp": "-3", "好感度": "-1"}},
    }

    updates = _run(plugin, _ws_state(state, tmp_path))

    assert ("character_state.update", "character_state",
            {"card_id": "luna", "changes": {"好感度": "-1"}}) in caller.calls
    assert updates["game_host_delta"]["deltas"] == [
        {"key": "game.hp", "value": "-3", "source": "behavior", "behavior": "挥砍"}
    ]
    row = next(row for row in _read_audit(tmp_path) if row["key"] == "game.hp")
    assert row["source"] == "behavior", "行级来源覆盖缺省 game_delta（归因不丢）"
    assert row["idem"] == "pipe-1:7:game"


def test_game_delta_too_large_rejected_and_audited(tmp_path: Any) -> None:
    """薄 sanity·超长（上限常量）：拒绝+审计不转发；不 clamp 不截断（宿主权威）。"""
    mod = _load_plugin()
    caller = FakeCaller()
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)
    oversized = "囍" * (mod.GAME_DELTA_MAX_CHARS + 8)
    text = f'<state>{{"state": {{"game.note": "{oversized}"}}}}</state>'

    updates = _run(plugin, _ws_state(_allow_state(_state(text)), tmp_path))

    assert "game_host_delta" not in updates, "全被拒 = 零转发零截断"
    assert not any(t == "mode.game.push_delta" for t, _, _ in caller.calls)
    row = next(row for row in _read_audit(tmp_path) if row["key"] == "game.note")
    assert row["reason"] == "game_delta_too_large"
    assert row["source"] == "game_delta"
    assert row["to"] == oversized, "拒绝记录提议原值（不 clamp）"


def test_game_delta_non_serializable_rejected_and_audited(tmp_path: Any) -> None:
    """薄 sanity·非 JSON 值（YAML 装载的 behaviors 可携带 date 等）：拒绝+审计。"""
    from datetime import date as _date

    mod = _load_plugin()
    caller = FakeCaller()
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)
    state = _state("叙事正文。")
    state["context.rp_state_behaviors"] = {
        "坏效果": {"trigger": "叙事", "effect": {"game.hp": _date(2020, 1, 1)}},
    }

    updates = _run(plugin, _ws_state(state, tmp_path))

    assert "game_host_delta" not in updates
    assert not any(t == "mode.game.push_delta" for t, _, _ in caller.calls)
    row = next(row for row in _read_audit(tmp_path) if row["key"] == "game.hp")
    assert row["reason"] == "game_delta_not_serializable"


def test_game_delta_round_cap_rejects_overflow(tmp_path: Any) -> None:
    """单轮条数上限：超出部分拒绝+审计（game_delta_overflow），前段照常转发。"""
    mod = _load_plugin()
    caller = FakeCaller()
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)
    cap = int(mod.GAME_DELTA_ROUND_CAP)
    keys = {f"game.k{i}": i for i in range(cap + 3)}
    import json as _json

    text = f'<state>{{"state": {_json.dumps(keys, ensure_ascii=False)}}}</state>'

    updates = _run(plugin, _ws_state(_allow_state(_state(text)), tmp_path))

    forwarded = updates["game_host_delta"]["deltas"]
    assert len(forwarded) == cap
    assert forwarded[0]["key"] == "game.k0"
    assert forwarded[-1]["key"] == f"game.k{cap - 1}"
    rows = _read_audit(tmp_path)
    overflow = [row for row in rows if row["reason"] == "game_delta_overflow"]
    assert {row["key"] for row in overflow} == {f"game.k{cap}", f"game.k{cap + 1}", f"game.k{cap + 2}"}


def test_game_only_non_card_session_still_forwards(tmp_path: Any) -> None:
    """非卡键会话仅 game.* 变更：不触 story 落账（无卡键零异常），delta 照常转发。"""
    mod = _load_plugin()
    caller = FakeCaller()
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)
    text = '<state>{"state": {"game.hp": 5}}</state>'

    updates = _run(plugin, _ws_state(_allow_state(_state(text, agent_id="agentos")), tmp_path))

    assert not any(t == "character_state.update" for t, _, _ in caller.calls)
    assert updates["game_host_delta"]["deltas"] == [{"key": "game.hp", "value": 5}]


def test_game_delta_push_failure_degrades_not_blocks(tmp_path: Any) -> None:
    """推送宿主失败：warning 降级不阻断——state 键与审计留痕照常。"""
    mod = _load_plugin()
    caller = FakeCaller(fail_tools={"mode.game.push_delta"})
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)
    text = '<state>{"state": {"game.hp": 5}}</state>'

    updates = _run(plugin, _ws_state(_allow_state(_state(text)), tmp_path))

    assert updates["game_host_delta"]["deltas"], "state 键留痕"
    game_rows = [row for row in _read_audit(tmp_path) if row["idem"] == "pipe-1:7:game"]
    assert game_rows, "审计留痕"


def test_game_delta_non_mode_agent_no_push_degrade_trace(tmp_path: Any) -> None:
    """非模式命名空间 agent 键：推送目标按 agent 键派生，无宿主可推——不推送，
    state 键+审计留痕照常（与推送失败同款降级）。"""
    mod = _load_plugin()
    caller = FakeCaller()
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)
    text = '<state>{"state": {"game.hp": 5}}</state>'

    updates = _run(
        plugin, _ws_state(_allow_state(_state(text, agent_id="agentos")), tmp_path)
    )

    assert not any(t == "mode.game.push_delta" for t, _, _ in caller.calls), \
        "非模式命名空间无宿主可推"
    assert updates["game_host_delta"]["deltas"] == [{"key": "game.hp", "value": 5}], \
        "state 键留痕照常"
    rows = [row for row in _read_audit(tmp_path) if row["idem"] == "pipe-1:7:game"]
    assert [row["key"] for row in rows] == ["game.hp"], "审计留痕照常"


def test_game_delta_derived_host_missing_service_degrades(tmp_path: Any) -> None:
    """派生宿主无该服务（invoke 抛错）：目标插件 ID 按 agent 键命名空间派生，
    推送失败与既有降级同款——不阻断、state 键+审计留痕照常。"""
    mod = _load_plugin()
    caller = FakeCaller(fail_tools={"mode.game.push_delta"})
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)
    text = '<state>{"state": {"game.hp": 5}}</state>'

    updates = _run(
        plugin,
        _ws_state(_allow_state(_state(text, agent_id="mode_gamehost/hero")), tmp_path),
    )

    pushed = [(t, p) for t, p, _ in caller.calls if t == "mode.game.push_delta"]
    assert pushed == [("mode.game.push_delta", "mode_gamehost")], \
        "目标插件 ID = mode_<agent 键模式段>"
    assert updates["game_host_delta"]["deltas"], "state 键留痕"
    rows = [row for row in _read_audit(tmp_path) if row["idem"] == "pipe-1:7:game"]
    assert [row["key"] for row in rows] == ["game.hp"], "审计留痕"


def test_no_game_keys_zero_delta_channel(tmp_path: Any) -> None:
    """Tier 0 兼容态锁定：无 game.* 键 = 无 game_host_delta、无推送、无 game 审计批。"""
    mod = _load_plugin()
    caller = FakeCaller()
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)
    text = '<state>{"state": {"好感度": 42}, "memory": "北境"}</state>'

    updates = _run(plugin, _ws_state(_state(text), tmp_path))

    assert "game_host_delta" not in updates, "无 game.* 键零新增 state 键"
    assert not any(t == "mode.game.push_delta" for t, _, _ in caller.calls)
    rows = _read_audit(tmp_path)
    assert rows, "story 审计批在场（对照）"
    assert all(not row["idem"].endswith(":game") for row in rows), "无 game 审计批"


def test_game_delta_channel_failure_degrades_not_blocks(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """分域通道自身异常（审计单源崩溃）：外层降级不拖垮 story 侧、零 state 键。"""
    mod = _load_plugin()
    caller = FakeCaller()
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)

    def _boom(*_args: Any, **_kwargs: Any) -> bool:
        raise RuntimeError("审计单源崩溃")

    # 豁免注释（评审 B）：下方 monkeypatch 内部符号注入故障是对「mock 仅用于
    # 外部依赖」红线的**申报豁免**——审计单源 IO 崩溃路径无法经测试环境外部
    # 依赖稳定触发（真实磁盘故障不可注入），故障注入是验证「分域失败降级不
    # 拖垮 story 侧」契约的唯一手段，非以 mock 截断内部依赖走捷径。
    monkeypatch.setattr(mod, "append_audit_rows", _boom)
    text = '<state>{"state": {"game.hp": 5, "好感度": 1}}</state>'

    updates = _run(plugin, _ws_state(_allow_state(_state(text)), tmp_path))

    assert ("character_state.update", "character_state",
            {"card_id": "luna", "changes": {"好感度": 1}}) in caller.calls, "story 侧不受拖垮"
    assert "game_host_delta" not in updates, "分域失败零 state 键写入"


# ── 动作驱动基线（2026-10-13 ADR）：choices/actions 清单选取 → 声明效果 ──────
#
# 选取面 = 本轮玩家输入（尾条 user 消息 trim 后与声明 id/name 全等——与游戏页
# choices 点击「文本入输入区+自动发送」既有约定同构）；全等之外的形态（子串/
# 叙述提及）不触发（fail-closed 防误触）。engine 落点声明效果与 behaviors 同键
# 同轮合并经 Validator（动作先、behaviors 后，缺省取最后）；game 落点转发宿主
# delta 通道，不进 story 账本。动作通道独立于 state 字段回退开关。

_PRAISE = [
    {"id": "praise", "name": "夸奖她", "target": "engine", "effect": {"好感度": "+5"}},
]


def _action_state(
    actions: Any,
    *,
    user_text: str | None = "夸奖她",
    narration: str = "她被夸得耳根微红。",
    schema: Any = None,
    behaviors: Any = None,
    workspace: str | None = None,
    agent_id: str | None = "mode_roleplay/luna",
) -> dict:
    state = _state(narration, agent_id=agent_id)
    if actions is not None:
        state["context.rp_actions"] = actions
    if user_text is not None:
        assert isinstance(state["messages"], list)
        state["messages"].insert(0, {"role": "user", "content": user_text})
    if schema is not None:
        state["context.rp_state_schema"] = schema
    if behaviors is not None:
        state["context.rp_state_behaviors"] = behaviors
    if workspace is not None:
        state["workspace"] = workspace
        state["pipeline_id"] = "pipe-1"
        assert isinstance(state["messages"][-1], dict)
        state["messages"][-1]["seq"] = 7
    return state


@pytest.mark.parametrize(
    ("effect", "current", "expected", "reason"),
    [
        ("+5", 50, 55, "accepted"),  # 相对增量以账本现值为基线
        ("+90", 50, 100, "clamped"),  # clamp 上限（Validator 双保险对动作效果同执法）
    ],
)
def test_action_selection_engine_effect_settles_via_validator(
    tmp_path: Any, effect: str, current: int, expected: int, reason: str
) -> None:
    """engine 落点：玩家全等选取声明动作 → 声明效果经 Validator 结算落账，
    审计 source=action（动作名随行）。"""
    import json as _json

    mod = _load_plugin()
    caller = FakeCaller(state_values={"好感度": current})
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)

    _run(
        plugin,
        _action_state(
            [{"id": "praise", "name": "夸奖她", "target": "engine", "effect": {"好感度": effect}}],
            schema={"好感度": _INT_WIDE},
            workspace=str(tmp_path),
        ),
    )

    assert ("character_state.update", "character_state",
            {"card_id": "luna", "changes": {"好感度": expected}}) in caller.calls
    rows = [_json.loads(line) for line in
            (tmp_path / "rp_state_audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["source"] == "action"
    assert rows[0]["action"] == "praise"
    assert (rows[0]["from"], rows[0]["to"], rows[0]["reason"]) == (current, expected, reason)


def test_action_matches_id_and_name_selectable_key() -> None:
    """选取键 = id 或 name（作者声明任一可选取）；未声明可选取键 = 不可选取。"""
    mod = _load_plugin()
    schema = {"好感度": _INT_WIDE}

    id_only_caller = FakeCaller(state_values={"好感度": 10})
    _run(
        mod.StateMarkerParsePlugin(invoke_caller=id_only_caller),
        _action_state(
            [{"id": "praise", "target": "engine", "effect": {"好感度": "+5"}}],
            user_text="praise",
            schema=schema,
        ),
    )
    assert ("character_state.update", "character_state",
            {"card_id": "luna", "changes": {"好感度": 15}}) in id_only_caller.calls, "id 全等选取"

    name_only_caller = FakeCaller(state_values={"好感度": 10})
    _run(
        mod.StateMarkerParsePlugin(invoke_caller=name_only_caller),
        _action_state(
            [{"name": "夸奖她", "target": "engine", "effect": {"好感度": "+5"}}],
            user_text="  夸奖她  ",
            schema=schema,
        ),
    )
    assert ("character_state.update", "character_state",
            {"card_id": "luna", "changes": {"好感度": 15}}) in name_only_caller.calls, (
        "name 全等选取（trim 后）"
    )

    no_key_caller = FakeCaller(state_values={"好感度": 10})
    _run(
        mod.StateMarkerParsePlugin(invoke_caller=no_key_caller),
        _action_state(
            [{"target": "engine", "effect": {"好感度": "+5"}}],
            user_text="夸奖她",
            schema=schema,
        ),
    )
    assert not any(
        t == "character_state.update" for t, _, _ in no_key_caller.calls
    ), "无可选取键声明 = 不可选取（fail-closed）"


@pytest.mark.parametrize(
    ("action_effect", "behavior_effect", "expected"),
    [
        ("+5", "=30", 30),  # behaviors 后者绝对覆盖动作相对
        ("=50", "+3", 53),  # behaviors 后者相对以动作绝对结果为基线
    ],
)
def test_action_and_behavior_same_key_merge_last_wins(
    tmp_path: Any, action_effect: str, behavior_effect: str, expected: int
) -> None:
    """动作效果与 behaviors 效果同键同轮合并：结算序动作先、behaviors 后
    （「缺省取最后」= 序列后者胜绝对/叠加相对），同经 Validator。"""
    mod = _load_plugin()
    caller = FakeCaller(state_values={"好感度": 50})
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)

    _run(
        plugin,
        _action_state(
            [{"id": "praise", "target": "engine", "effect": {"好感度": action_effect}}],
            user_text="praise",
            schema={"好感度": _INT_WIDE},
            behaviors={"怒目": {"trigger": "耳根", "effect": {"好感度": behavior_effect}}},
            workspace=str(tmp_path),
        ),
    )

    assert ("character_state.update", "character_state",
            {"card_id": "luna", "changes": {"好感度": expected}}) in caller.calls
    rows = _read_audit(tmp_path)
    assert [row["source"] for row in rows] == ["action", "behavior"], (
        "结算序 = 动作先、behaviors 后（from 链按序）"
    )
    assert rows[1]["from"] == rows[0]["to"], "前序已定值为后续基线"


@pytest.mark.parametrize(
    ("user_text", "label"),
    [
        ("帮我夸奖她一下", "子串不触发"),
        ("夸奖", "部分词不触发"),
        (None, "无玩家输入不触发"),
    ],
)
def test_action_no_exact_match_no_effect(
    tmp_path: Any, user_text: str | None, label: str
) -> None:
    """非全等选取（子串/部分词/无输入）= 零动作（fail-closed 防叙述误触）。"""
    mod = _load_plugin()
    caller = FakeCaller(state_values={"好感度": 50})
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)

    updates = _run(
        plugin,
        _action_state(
            _PRAISE, user_text=user_text,
            schema={"好感度": _INT_WIDE}, workspace=str(tmp_path),
        ),
    )

    assert not any(t == "character_state.update" for t, _, _ in caller.calls), label
    assert "context.state_updates" not in updates, "无标记轮零渲染语义保持"
    assert not (tmp_path / "rp_state_audit.jsonl").exists(), "零结算零审计"


def test_action_engine_without_effect_fail_closed() -> None:
    """engine 落点缺声明效果 = 该动作跳过（fail-closed 不发明空效果），不炸。"""
    mod = _load_plugin()
    caller = FakeCaller(state_values={"好感度": 50})
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)

    updates = _run(
        plugin,
        _action_state(
            [{"id": "praise", "target": "engine"}],
            schema={"好感度": _INT_WIDE},
        ),
    )

    assert not any(t == "character_state.update" for t, _, _ in caller.calls)
    assert "context.state_updates" not in updates


def test_action_game_target_forwarded_host_not_story_ledger(tmp_path: Any) -> None:
    """game 落点（actions_for_game 下发宿主的 Tier 0 形态）：选取后走宿主 delta
    通道（薄 sanity + 审计 + 推送），不进 story 账本、不经 Validator。"""
    mod = _load_plugin()
    caller = FakeCaller()
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)
    actions = [
        {"id": "attack", "target": "game", "params": {"damage": 3}},
    ]

    updates = _run(
        plugin,
        _action_state(
            actions,
            user_text="attack",
            schema={"好感度": _INT_WIDE},
            workspace=str(tmp_path),
        ),
    )

    assert not any(t == "character_state.update" for t, _, _ in caller.calls), (
        "game 落点不进 story 账本"
    )
    assert updates["game_host_delta"]["deltas"] == [
        {"key": "attack", "value": {"damage": 3}, "source": "action", "action": "attack"}
    ], "声明 params 原样转发宿主（归因随行）"
    assert ("mode.game.push_delta", "mode_roleplay", {
        "session_id": "pipe-1", "turn_seq": 7,
        "deltas": [{"key": "attack", "value": {"damage": 3}, "source": "action", "action": "attack"}],
    }) in caller.calls, "经宿主待发队列下发（宿主可拒绝，结果回传对账）"
    row = next(row for row in _read_audit(tmp_path) if row["key"] == "attack")
    assert row["source"] == "action"
    assert row["reason"] == "game_delta"
    assert row["idem"] == "pipe-1:7:game", "game 批分幂等空间"


def test_action_channel_independent_of_state_field_switch(tmp_path: Any) -> None:
    """动作通道独立于 state 字段回退开关：未声明 allow_state_proposals 的卡，
    动作选取照常生效（动作驱动基线是默认形态，非回退件）。"""
    mod = _load_plugin()
    caller = FakeCaller(state_values={"好感度": 50})
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)

    updates = _run(
        plugin,
        _action_state(_PRAISE, schema={"好感度": _INT_WIDE}, workspace=str(tmp_path)),
    )

    assert ("character_state.update", "character_state",
            {"card_id": "luna", "changes": {"好感度": 55}}) in caller.calls, (
        "回退开关缺席（默认关）不影响动作通道"
    )
    assert "context.state_updates" not in updates, (
        "无标记轮零渲染语义保持（动作结算不依赖标记在场，与 behaviors 契约一致）"
    )


# ── 缺口批次一+二：拒绝回灌 + 近况事件并入下轮动态注入源（§7.5 #2/#5/#6）──────
# 下轮 LLM 动态注入源 = context.character_state_text（卡 dynamic_vars 占位符
# {{state:context.character_state_text}} 每轮取新鲜值）；两节任一为空即不出现，
# 纯文本轮该出口与现状逐字相等（兼容态锁定）。


def test_rejections_feedback_into_character_state_text(tmp_path: Any) -> None:
    """拒绝原因回灌：本轮 rejected 摘要（layer/reason/键）一行一条并入
    context.character_state_text——硬拒（unknown_key）与软拒（delta 超限）同轮
    各占一行，分层可辨（LLM 下轮知"为什么不行"而非反复撞墙）。"""
    mod = _load_plugin()
    caller = FakeCaller(render_text="状态文本", state_values={"好感度": 50})
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)
    text = '<state>{"state": {"魔力": 9, "好感度": "+999"}}</state>'
    state = _allow_state(_schema_state(text, {"好感度": _INT_WIDE}, workspace=str(tmp_path)))
    state["context.rp_state_hard_rules"] = {"好感度": {"max_delta_per_turn": 10}}

    updates = _run(plugin, state)

    state_text = updates["context.character_state_text"]
    assert state_text.startswith("状态文本"), "render 文本在前（注入源主体不变）"
    assert "上轮被拒变更：" in state_text
    assert "- [hard|unknown_key] 魔力" in state_text, "契约级拒绝带 hard 层"
    assert "- [soft|delta_exceeded] 好感度" in state_text, "值域超限带 soft 层（可申诉）"
    assert state_text.index("上轮被拒变更：") < state_text.index("近期事件："), (
        "拒绝节先于事件节（stable 次序）"
    )


def test_events_feedback_into_character_state_text(tmp_path: Any) -> None:
    """事件粒度：近 K 条事件（写面补的 actor/action/summary）并入注入源——
    本轮审计落盘后即读到（含本轮变更）。"""
    mod = _load_plugin()
    caller = FakeCaller(render_text="状态文本", state_values={"好感度": 50})
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)
    text = '<state>{"state": {"好感度": "+5"}}</state>'
    state = _allow_state(_schema_state(text, {"好感度": _INT_WIDE}, workspace=str(tmp_path)))

    updates = _run(plugin, state)

    state_text = updates["context.character_state_text"]
    assert "近期事件：" in state_text
    assert "- narrator increase 好感度: 50→55" in state_text, "事件一行一条可读"
    assert "上轮被拒变更：" not in state_text, "无拒绝轮拒绝节不出现（空节不出现）"


def test_feedback_windows_and_truncation(tmp_path: Any) -> None:
    """截断保护双面：拒绝超 8 行取最近 8 条（最早滚出）；超长键行截断带省略号。"""
    import json as _json

    mod = _load_plugin()
    caller = FakeCaller(render_text="状态文本")
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)
    long_key = "超长键" + "囍" * 150
    payload = {f"魔力{i}": i for i in range(10)}
    payload[long_key] = 1
    text = f'<state>{{"state": {_json.dumps(payload, ensure_ascii=False)}}}</state>'
    state = _allow_state(_schema_state(text, {"好感度": _INT_WIDE}, workspace=str(tmp_path)))

    updates = _run(plugin, state)

    state_text = updates["context.character_state_text"]
    assert "- [hard|unknown_key] 魔力9" in state_text, "最近一条在窗口内"
    assert "- [hard|unknown_key] 魔力2" not in state_text, "最早三条滚出 8 行窗口"
    assert "- [hard|unknown_key] 魔力3" in state_text, "窗口恰保留最近 8 条"
    truncated_line = next(
        line for line in state_text.splitlines() if "unknown_key" in line and "囍" in line
    )
    assert truncated_line.endswith("…"), "超长行截断标记在场"
    assert len(truncated_line) <= 200, "行级有界"


def test_feedback_absent_rounds_keep_render_verbatim() -> None:
    """兼容态锁定：无拒绝 + ws 不可得（无事件可读）= character_state_text 与
    现状逐字相等（该出口的前端消费面零扰动）。"""
    mod = _load_plugin()
    caller = FakeCaller(render_text="好感度 42", state_values={"好感度": 50})
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)
    text = '<state>{"state": {"好感度": "+5"}}</state>'

    updates = _run(plugin, _allow_state(_schema_state(text, {"好感度": _INT_WIDE})))

    assert updates["context.character_state_text"] == "好感度 42", "纯 render 文本"


def test_rejections_feedback_without_workspace_still_feeds_back() -> None:
    """ws 不可得：拒绝节照常回灌（内存即得），仅事件节缺席（降级不缺真）。"""
    mod = _load_plugin()
    caller = FakeCaller(render_text="状态文本", state_values={"好感度": 50})
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)
    text = '<state>{"state": {"魔力": 9}}</state>'

    updates = _run(plugin, _allow_state(_schema_state(text, {"好感度": _INT_WIDE})))

    state_text = updates["context.character_state_text"]
    assert "- [hard|unknown_key] 魔力" in state_text
    assert "近期事件：" not in state_text, "无 ws 无事件面（诚实缺席）"
