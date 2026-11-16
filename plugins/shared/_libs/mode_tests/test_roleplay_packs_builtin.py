# @feature: FP-0.2.〇 RP C3 整合刀 出厂玩法包 | @vision: V1 可进化 | @ci: python-coverage
"""出厂玩法包×2 断言（RP C3 整合刀：修仙战斗 + 星露谷好感，一引擎两玩法）。

- 出厂根播种两包（``<mode_pkg>/packs/``）：清点面发现（origin=builtin）+ 装载机
  双根加载（rp_packs.load_packs / merge_state_decl）零 warning；
- 一引擎两玩法：两包 Tier 0 声明（variables/behaviors）跑**同一套内置机**——
  state_marker_parse 行为匹配（声明序 re.search）→ state_contract Validator
  （路径/类型/clamp）——修仙包「剑气斩」类触发落气血下降、星露谷包「送礼」类
  触发落好感上升；每包 ≥2 组区分度输入（正常/边界 clamp/类型枚举）+ 空触发对照；
- 冒烟卡 pack_ids 双引：agent_config_load 装载机经出厂根（用户层空）一次并入两包
  schema/behaviors（测试内构造，不入厂卡库）；
- 声明自洽：behaviors effect 键全部落在包内 variables 声明（flatten 路径）、
  trigger 可编译、choices 条目 text/category/hint 齐备、rules 裁决优先级清单在场。

mock 仅外部依赖（无）；包资产/装载机/结算机全真实。
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_MODES_TESTS = Path(__file__).resolve().parent
_SHARED = _MODES_TESTS.parents[1]  # plugins/shared/
_PKG_DIR = _SHARED / "mode_roleplay"
_PACKS_DIR = _PKG_DIR / "packs"
_ACL_DIR = _SHARED / "agent_config_load"
_SMP_DIR = _SHARED / "state_marker_parse"

for _p in (str(_SHARED), str(_ACL_DIR), str(_SMP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _libs.pipeline_base.plugin import PluginContext  # noqa: E402
from rp_packs import load_packs, merge_state_decl  # noqa: E402
from state_contract import (  # noqa: E402
    REASON_ACCEPTED,
    REASON_CLAMPED,
    flatten_variables,
    validate_and_apply,
)

_XIANXIA_ID = "xianxia_combat"
_STARDEW_ID = "stardew_social"
_FACTORY_IDS = (_XIANXIA_ID, _STARDEW_ID)


def _pack_yaml(pack_id: str) -> dict[str, Any]:
    data = yaml.safe_load((_PACKS_DIR / f"{pack_id}.yaml").read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"出厂包顶层须为映射: {pack_id}"
    return data


def _load_server() -> Any:
    """按唯一模块名装载 mode_roleplay server.py（清点面复用既有模块装载风格）。"""
    path = _PKG_DIR / "server.py"
    spec = importlib.util.spec_from_file_location("mode_packs_builtin_roleplay", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _match_pack_behaviors(behaviors: dict[str, Any], text: str) -> list[tuple[str, Any, dict[str, Any]]]:
    """行为匹配机（state_marker_parse 同源）：声明序 re.search → 变更对。"""
    mod_name = "state_marker_parse_plugin_packs_test"
    path = _SMP_DIR / "plugin.py"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod.StateMarkerParsePlugin._match_behaviors(
        {"context.rp_state_behaviors": behaviors}, text
    )


def _settle(
    pack_id: str, text: str, current: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """单包结算回路：行为匹配 → Validator。返回 (accepted, rejected, audit)。"""
    data = _pack_yaml(pack_id)
    changes = _match_pack_behaviors(data["behaviors"], text)
    result = validate_and_apply(current, changes, data["variables"])
    return result.accepted, result.rejected, result.audit


def _audit_of(audit: list[dict[str, Any]], behavior: str) -> dict[str, Any]:
    rows = [row for row in audit if row.get("behavior") == behavior]
    assert rows, f"审计缺行为行: {behavior}"
    return rows[0]


# ── 出厂根播种：清点面发现 + 装载机双根加载 ────────────────────────────────


def test_factory_packs_discovered_in_listing(tmp_path: Any) -> None:
    """清点面（bootstrap packs 数据源）：出厂根发现两包，origin=builtin 带计数。"""
    server = _load_server()
    listing = server._load_packs_listing(user_dir=str(tmp_path / "empty_user_packs"))
    by_id = {p["id"]: p for p in listing}

    for pack_id in _FACTORY_IDS:
        entry = by_id[pack_id]
        assert entry["origin"] == "builtin"
        assert entry["name"], "出厂包带作者标注名"
        assert entry["name"] != pack_id
        assert entry["description"]
        assert entry["variables_count"] > 0
        assert entry["behaviors_count"] > 0


def test_factory_packs_load_via_loading_machine(tmp_path: Any) -> None:
    """装载机双根加载两包零 warning；Tier 0 合并两包并存（同构不同值同面）。"""
    packs, warnings = load_packs(
        _FACTORY_IDS, str(_PKG_DIR), str(tmp_path / "no_user_packs")
    )
    assert len(packs) == 2
    assert warnings == [], "出厂包声明面必须自洽零 warning"

    variables, behaviors, decl_warnings = merge_state_decl(None, None, packs)
    # v3.1 刀1 起两包声明 ui、2026-10-17 标杆补齐起修仙包声明 graph（呈现/引擎
    # 面，消费在模式插件 server 侧）——三延迟键 warning 已随批 4-Ⅲ 消费启用
    # 解除（rp_packs 静默忽略），出厂包声明面保持零 warning。
    assert decl_warnings == [], "出厂包延迟键 warning 已解除（零 warning）"
    assert variables is not None
    assert behaviors is not None
    # 两包键域并存且组键不打架（浅合并整体替换语义下，两包顶层组键互不重叠）：
    # 修仙 player/combat 与星露谷 npc/world
    assert "player" in variables
    assert "combat" in variables
    assert "npc" in variables
    assert "日期" in variables["world"]
    assert "回合" in variables["combat"]
    assert "剑气斩" in behaviors
    assert "阿比收礼" in behaviors


# ── 声明自洽（card_author 玩法包四件套自查的机械化）────────────────────────


@pytest.mark.parametrize("pack_id", _FACTORY_IDS)
def test_pack_declaration_self_consistent(pack_id: str) -> None:
    """effect 键全部落在包内 variables 声明；trigger 可编译；choices/rules 形态齐备。"""
    data = _pack_yaml(pack_id)
    flat, _skipped = flatten_variables(data["variables"])
    flat_paths = set(flat)

    behaviors = data["behaviors"]
    assert isinstance(behaviors, dict)
    assert behaviors
    for name, decl in behaviors.items():
        assert isinstance(decl, dict), f"声明形态: {name}"
        assert decl.get("trigger"), f"trigger 缺席: {name}"
        re.compile(decl["trigger"])  # 可编译（不可编译即红）
        effect = decl["effect"]
        assert isinstance(effect, dict), f"effect 形态: {name}"
        assert effect, f"effect 缺席: {name}"
        for key in effect:
            assert key in flat_paths, f"effect 键未声明: {name} -> {key}"

    choices = data["choices"]
    assert isinstance(choices, list)
    assert choices
    for entry in choices:
        assert isinstance(entry, dict)
        assert str(entry.get("text") or "").strip(), "choices 条目 text 缺席"
        assert str(entry.get("category") or "").strip(), "choices 条目 category 缺席"
        assert str(entry.get("hint") or "").strip(), "choices 条目 hint 缺席"

    rules = data["rules"]
    assert isinstance(rules, dict)
    assert rules
    priority = rules.get("裁决优先级")
    assert isinstance(priority, list), "裁决优先级清单形态"
    assert len(priority) >= 3, "裁决优先级清单缺席"
    assert "引擎事实" in priority[0], "首条裁决优先级 = 引擎事实最高"


# ── 一引擎两玩法：修仙包（触发 → 气血下降；正常/clamp/枚举/空对照）──────────


def test_xianxia_strike_damages_hp_normal_and_clamped() -> None:
    """剑气斩命中：正常轮 100→82；濒死轮 clamp 触底 0（reason=clamped）。"""
    accepted, _, audit = _settle(_XIANXIA_ID, "长剑出鞘，一道剑气斩破空而至", {"player.气血": 100})
    assert accepted == {"player.气血": 82}
    row = _audit_of(audit, "剑气斩")
    assert (row["source"], row["op"], row["from"], row["to"], row["reason"]) == (
        "behavior", "-", 100, 82, REASON_ACCEPTED,
    )

    clamped, _, audit2 = _settle(_XIANXIA_ID, "剑气斩落在他护体之上", {"player.气血": 10})
    assert clamped == {"player.气血": 0}
    assert _audit_of(audit2, "剑气斩")["reason"] == REASON_CLAMPED
    # 性质断言：结果恒在声明域内且伤害单调不增
    assert 0 <= clamped["player.气血"] <= 100


def test_xianxia_qi_deviation_enum_and_baseline_default() -> None:
    """走火入魔：伤势枚举绝对赋值（=重伤）类型校验过 Validator；灵力缺现值取
    声明 default（100）为基线相对扣减。"""
    accepted, rejected, audit = _settle(_XIANXIA_ID, "他强行催动真气，竟走火入魔", {})
    assert accepted == {"player.灵力": 85, "player.伤势": "重伤"}
    assert rejected == []
    assert _audit_of(audit, "走火入魔")["reason"] == REASON_ACCEPTED


def test_xianxia_meditation_advances_turn_and_no_hit_yields_nothing() -> None:
    """调息回气：灵力回复 + 回合计数推进；空触发对照 = 零变更零落账。"""
    accepted, _, audit = _settle(_XIANXIA_ID, "他盘坐调息，气息渐渐平稳", {"player.灵力": 40})
    assert accepted == {"player.灵力": 50, "combat.回合": 2}
    assert _audit_of(audit, "调息回气")["from"] == 40

    accepted2, _, audit2 = _settle(_XIANXIA_ID, "山风拂过山门，石阶无人", {"player.气血": 100})
    assert accepted2 == {}
    assert audit2 == []


# ── 一引擎两玩法：星露谷包（触发 → 好感上升；default/clamp/隔离/空对照）──────


def test_stardew_gift_raises_affection_default_and_clamped() -> None:
    """送礼命中：缺现值取 default 0 基线 → 8；临顶轮 clamp 100。"""
    accepted, _, audit = _settle(_STARDEW_ID, "你把向日葵送给阿比，她眯眼笑了", {})
    assert accepted == {"npc.阿比.好感度": 8}
    row = _audit_of(audit, "阿比收礼")
    # 缺现值取声明 default（0）为结算基线；审计 from 记账本原样（缺席 = None）
    assert (row["source"], row["op"], row["to"]) == ("behavior", "+", 8)
    assert row["from"] is None

    clamped, _, audit2 = _settle(
        _STARDEW_ID, "生日这天你把最爱的话送给了阿比，阿比收下", {"npc.阿比.好感度": 95}
    )
    assert clamped == {"npc.阿比.好感度": 100}
    assert _audit_of(audit2, "阿比收礼")["reason"] == REASON_CLAMPED


def test_stardew_talk_and_isolation_between_npcs() -> None:
    """交谈小增；送阿比不动罗宾（键域隔离）；空触发对照零变更。"""
    accepted, _, audit = _settle(
        _STARDEW_ID, "傍晚你与阿比聊天，聊起矿石", {"npc.阿比.好感度": 40}
    )
    assert accepted == {"npc.阿比.好感度": 42}
    assert _audit_of(audit, "阿比交谈")["reason"] == REASON_ACCEPTED

    gift_only, _, _ = _settle(_STARDEW_ID, "你把防风草送给罗宾", {})
    assert gift_only == {"npc.罗宾.好感度": 8}, "送礼只动收礼人键域"

    nothing, _, audit3 = _settle(_STARDEW_ID, "镇长在广场宣布丰收节开幕", {})
    assert nothing == {}
    assert audit3 == []


def test_stardew_sleep_advances_date_with_clamp() -> None:
    """入睡过夜：日期推进；月末 clamp 封顶（max=28，reason=clamped）。"""
    accepted, _, _ = _settle(_STARDEW_ID, "你熄灯入睡", {})
    assert accepted == {"world.日期": 2}

    clamped, _, audit2 = _settle(_STARDEW_ID, "这夜你早早睡下", {"world.日期": 28})
    assert clamped == {"world.日期": 28}
    assert _audit_of(audit2, "入睡过夜")["reason"] == REASON_CLAMPED


# ── 一引擎两玩法收官：两包同轮一次结算（同一台机器，两种玩法结果并存）────────


def test_both_packs_settle_in_one_machine_pass() -> None:
    """合并两包声明后一轮正文同时命中修仙与星露谷行为：一次 Validator 结算产出
    两种玩法结果（气血下降 + 好感上升 + 日期推进），审计各归其包其行为。"""
    packs, warnings = load_packs(
        _FACTORY_IDS, str(_PKG_DIR), str(_PACKS_DIR.parent.parent / "nonexistent")
    )
    assert warnings == []
    variables, behaviors, _ = merge_state_decl(None, None, packs)
    assert variables is not None
    assert behaviors is not None

    text = "激战中一记剑气斩破空而来；战罢你把草药送给阿比，回屋入睡。"
    changes = _match_pack_behaviors(behaviors, text)
    result = validate_and_apply({"player.气血": 50}, changes, variables)

    assert result.accepted == {
        "player.气血": 32,
        "npc.阿比.好感度": 8,
        "world.日期": 2,
    }
    behavior_names = {row.get("behavior") for row in result.audit}
    assert {"剑气斩", "阿比收礼", "入睡过夜"} <= behavior_names
    assert all(row["source"] == "behavior" for row in result.audit)


# ── 冒烟卡 pack_ids 双引：装载机经出厂根一次并入两包（测试内构造，不入卡库）──


@pytest.fixture
def factory_root(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Path:
    """用户根/config 根钉到 tmp（与 rp_packs 装载测试同契约）——包根回落出厂。"""
    factory = tmp_path / "factory"
    factory.mkdir()
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "user-root"))
    monkeypatch.delenv("AGENTOS_USER_CONFIG_DIR", raising=False)
    monkeypatch.delenv("AGENTOS_USER_PLUGINS_DIR", raising=False)
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(factory))
    return tmp_path


def _load_acl_plugin() -> Any:
    mod_name = "agent_config_load_plugin_packs_smoke_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _ACL_DIR / "plugin.py")
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_smoke_card_pack_ids_loads_both_packs(factory_root: Path) -> None:
    """冒烟卡 pack_ids 引两包：出厂根加载 → context.rp_state_schema/behaviors
    两包声明并存（用户层无副本，纯出厂根双根回落位）。"""
    agents = factory_root / "factory" / "agents" / "mode_roleplay"
    agents.mkdir(parents=True)
    (agents / "card_smoke.yaml").write_text(
        "display_name: 冒烟卡\npack_ids:\n- xianxia_combat\n- stardew_social\n",
        encoding="utf-8",
    )
    mod = _load_acl_plugin()
    plugin = mod.AgentConfigLoadPlugin(config={})
    result = asyncio.run(
        plugin.execute(PluginContext(state={"agent.id": "mode_roleplay/card_smoke"}, config={}))
    )
    updates = result.state_updates

    schema = updates["context.rp_state_schema"]
    assert "player" in schema
    assert "npc" in schema
    assert schema["combat"]["回合"]["type"] == "int"
    assert schema["npc"]["阿比"]["好感度"]["max"] == 100
    behaviors = updates["context.rp_state_behaviors"]
    assert "剑气斩" in behaviors
    assert "阿比收礼" in behaviors
