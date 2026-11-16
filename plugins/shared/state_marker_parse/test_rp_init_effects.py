# @feature: FP-0.2.〇 RP面板v3.1刀1 | @vision: V1 可进化 | @ci: python-coverage
"""state_marker_parse 会话首轮 init effects 测试（RP 面板 v3.1 刀1，动作驱动
基线同构——开演增量只走作者声明通道）。

契约（SOURCE_INIT 常量注释单源）：
- context.rp_init_effects 对 Validator 全套结算，结算序 init 最先（先于动作/
  标记/behaviors）——init 先落账，主结算 current_values 读到 init 后现值；
- 行级审计 source=init；幂等闸 = 审计文件会话级 idem ``{pipeline_id}:init``
  （同轮重放/重启重入只生效一次；ws 不可得 = fail-closed 跳过）；
- 键域不在 schema 声明内 = rejected + 审计（fail-closed 不落账）；
- 无 rp_init_effects = 现状零变化（兼容态对照锁定）。

mock 仅外部依赖（tool-executor 调用句柄注入），审计文件全真实 IO。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parent
_SHARED = _DIR.parents[0]

for _p in (str(_SHARED), str(_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load_plugin() -> Any:
    mod_name = "state_marker_parse_plugin_init_test"
    path = _DIR / "plugin.py"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, str(path))
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeCaller:
    """tool-executor 调用记录器（test_state_marker_parse 同款）。"""

    def __init__(self, render_text: str = "", state_values: dict[str, Any] | None = None):
        self.calls: list[tuple[str, str, dict]] = []
        self.render_text = render_text
        self.state_values = state_values or {}

    async def __call__(self, tool_name: str, plugin_id: str, args: dict) -> dict:
        self.calls.append((tool_name, plugin_id, args))
        if tool_name == "character_state.render":
            return {"data": {"text": self.render_text}}
        if tool_name == "character_state.get":
            return {"data": {"values": dict(self.state_values)}}
        return {"data": {"ok": True}}


class StatefulCaller(FakeCaller):
    """模拟 character_state 落账持久化：update 合并进 get 视图（结算序可观测）。"""

    async def __call__(self, tool_name: str, plugin_id: str, args: dict) -> dict:
        self.calls.append((tool_name, plugin_id, args))
        if tool_name == "character_state.render":
            return {"data": {"text": self.render_text}}
        if tool_name == "character_state.get":
            return {"data": {"values": dict(self.state_values)}}
        if tool_name == "character_state.update":
            self.state_values.update(args.get("changes") or {})
        return {"data": {"ok": True}}


def _state(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "agent.id": "mode_roleplay/luna",
        "pipeline_id": "pipe-1",
        "workspace": str(overrides.pop("workspace")),
        "messages": [{"role": "assistant", "content": overrides.pop("text", "纯剧情。")}],
    }
    state.update(overrides)
    return state


def _run(plugin: Any, state: dict[str, Any]) -> dict[str, Any]:
    result = asyncio.run(plugin.execute(type("Ctx", (), {"state": state})()))
    return result.state_updates


def _audit_rows(ws: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in (ws / "rp_state_audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]


_SCHEMA = {
    "npc": {"阿比": {"好感度": {"type": "int", "min": 0, "max": 100, "default": 0}}},
    "world": {"季节": {"type": "enum", "values": ["春", "夏", "秋", "冬"], "default": "春"}},
}


def test_init_effects_settle_first_via_validator(tmp_path: Path) -> None:
    """init 走 Validator 全套：相对 op 以声明 default 为基线、clamp 执法、
    accepted 经 character_state.update 落账、审计 source=init。"""
    caller = FakeCaller(render_text="状态")
    plugin = _load_plugin().StateMarkerParsePlugin(invoke_caller=caller)
    state = _state(
        workspace=tmp_path,
        **{
            "context.rp_state_schema": _SCHEMA,
            "context.rp_init_effects": {"npc.阿比.好感度": "+5", "world.季节": "=夏"},
        },
    )
    _run(plugin, state)

    update_calls = [args for t, _, args in caller.calls if t == "character_state.update"]
    assert update_calls == [{"card_id": "luna", "changes": {"npc.阿比.好感度": 5, "world.季节": "夏"}}]
    rows = [r for r in _audit_rows(tmp_path) if r["idem"] == "pipe-1:init"]
    assert {r["key"]: r["source"] for r in rows} == {
        "npc.阿比.好感度": "init",
        "world.季节": "init",
    }
    by_key = {r["key"]: r for r in rows}
    assert by_key["npc.阿比.好感度"]["to"] == 5, "相对 op 基线 = 声明 default（0+5）"
    assert by_key["world.季节"]["reason"] == "accepted"


def test_init_effects_idempotent_once_per_session(tmp_path: Path) -> None:
    """幂等闸：同会话第二轮（不同消息 seq）不重放——一次性增量不复利。"""
    plugin = _load_plugin().StateMarkerParsePlugin(invoke_caller=FakeCaller(state_values={"npc.阿比.好感度": 5}))
    base = {
        "context.rp_state_schema": _SCHEMA,
        "context.rp_init_effects": {"npc.阿比.好感度": "+5"},
    }
    _run(plugin, _state(workspace=tmp_path, **base))
    _run(plugin, _state(workspace=tmp_path, text="第二回合剧情。", **{
        **base,
        "messages": [{"role": "assistant", "content": "第二回合剧情。", "seq": 3}],
    }))
    assert len(_audit_rows(tmp_path)) == 1, "第二轮同 idem 幂等跳过（不双记不双结算）"


def test_init_effects_before_main_settlement_same_key(tmp_path: Path) -> None:
    """结算序 init 最先：同键动作效果叠加于 init 落账后现值（init +5 → 动作 +10）。"""
    caller = StatefulCaller()
    plugin = _load_plugin().StateMarkerParsePlugin(invoke_caller=caller)
    state = _state(
        workspace=tmp_path,
        messages=[
            {"role": "user", "content": "挥剑迎敌"},
            {"role": "assistant", "content": "剑光如虹。"},
        ],
        **{
            "context.rp_state_schema": _SCHEMA,
            "context.rp_init_effects": {"npc.阿比.好感度": "+5"},
            "context.rp_actions": [{"id": "挥剑迎敌", "effect": {"npc.阿比.好感度": "+10"}}],
        },
    )
    _run(plugin, state)
    update_calls = [args for t, _, args in caller.calls if t == "character_state.update"]
    assert len(update_calls) == 2, "init 与主结算各落账一次（先后两批）"
    assert update_calls[0]["changes"] == {"npc.阿比.好感度": 5}, "init 先落"
    assert update_calls[1]["changes"] == {"npc.阿比.好感度": 15}, "动作基线 = init 落账后现值（+10 叠加）"


def test_init_effects_out_of_domain_keys_rejected_failclosed(tmp_path: Path) -> None:
    """键域不在 schema 声明内 = rejected + 审计（fail-closed 不落账不发明）。"""
    caller = FakeCaller()
    plugin = _load_plugin().StateMarkerParsePlugin(invoke_caller=caller)
    state = _state(
        workspace=tmp_path,
        **{
            "context.rp_state_schema": _SCHEMA,
            "context.rp_init_effects": {"npc.幽灵.好感度": "+5"},
        },
    )
    _run(plugin, state)
    assert not any(t == "character_state.update" for t, _, _ in caller.calls), "全拒绝零落账"
    rows = [r for r in _audit_rows(tmp_path) if r["idem"] == "pipe-1:init"]
    assert len(rows) == 1, "rejected 全流水单行"
    assert rows[0]["key"] == "npc.幽灵.好感度"
    assert rows[0]["source"] == "init"
    assert rows[0]["reason"] == "unknown_key"


def test_init_effects_no_workspace_failclosed_skip(tmp_path: Path) -> None:
    """ws 不可得 = fail-closed 跳过（一次性增量防重放复利放大），零落账零审计。"""
    caller = FakeCaller(render_text="状态")
    plugin = _load_plugin().StateMarkerParsePlugin(invoke_caller=caller)
    state = _state(workspace=tmp_path)
    del state["workspace"]
    state["context.rp_state_schema"] = _SCHEMA
    state["context.rp_init_effects"] = {"npc.阿比.好感度": "+5"}
    updates = _run(plugin, state)
    assert not any(t == "character_state.update" for t, _, _ in caller.calls)
    assert not (tmp_path / "rp_state_audit.jsonl").exists()
    assert updates["context.character_state_text"] == "状态", "render 回写不受 init 跳过影响"


def test_init_effects_absent_zero_change_compat(tmp_path: Path) -> None:
    """兼容态对照：无 rp_init_effects = 无 update/无审计/无 init 幂等行。"""
    caller = FakeCaller(render_text="状态")
    plugin = _load_plugin().StateMarkerParsePlugin(invoke_caller=caller)
    _run(plugin, _state(workspace=tmp_path, **{"context.rp_state_schema": _SCHEMA}))
    assert not any(t == "character_state.update" for t, _, _ in caller.calls)
    assert not (tmp_path / "rp_state_audit.jsonl").exists(), "兼容态零审计新行"


def test_init_effects_none_schema_compat_passthrough(tmp_path: Path) -> None:
    """无声明卡（schema 缺席）= Validator 兼容态逐字透传落账 + 审计 source=init。"""
    caller = FakeCaller()
    plugin = _load_plugin().StateMarkerParsePlugin(invoke_caller=caller)
    state = _state(workspace=tmp_path, **{"context.rp_init_effects": {"自由键": 7}})
    _run(plugin, state)
    update_calls = [args for t, _, args in caller.calls if t == "character_state.update"]
    assert update_calls == [{"card_id": "luna", "changes": {"自由键": 7}}]
    rows = [r for r in _audit_rows(tmp_path) if r["idem"] == "pipe-1:init"]
    assert rows, "兼容态审计照记"
    assert rows[0]["source"] == "init"
    assert rows[0]["to"] == 7


def test_init_effects_settlement_failure_does_not_block_round(tmp_path: Path) -> None:
    """init 结算异常（如 character_state.get 服务失败）= warning 跳过不拖垮本轮
    （render 回写照常，主结算不受牵连）。"""

    class FailingGetCaller(FakeCaller):
        async def __call__(self, tool_name: str, plugin_id: str, args: dict) -> dict:
            if tool_name == "character_state.get":
                raise RuntimeError("get 服务失败")
            return await super().__call__(tool_name, plugin_id, args)

    caller = FailingGetCaller(render_text="仍回写")
    plugin = _load_plugin().StateMarkerParsePlugin(invoke_caller=caller)
    state = _state(
        workspace=tmp_path,
        **{
            "context.rp_state_schema": _SCHEMA,
            "context.rp_init_effects": {"npc.阿比.好感度": "+5"},
        },
    )
    updates = _run(plugin, state)
    assert not any(t == "character_state.update" for t, _, _ in caller.calls), "init 零落账"
    assert updates["context.character_state_text"] == "仍回写", "render 不被 init 失败拖垮"
