# @feature: FP-0.2.〇 RP面板v3.1刀1 | @vision: V1 可进化 | @ci: python-coverage
"""RP 面板 v3.1 刀1 后端能力缝测试：pack 呈现面声明载荷 / 自定义玩法 combos /
sessions 玩法归属 / 自包含 ui.page 通道 / game/config 增透 / 出厂两包 fixture。

契约（任务书为准）：
- GET data/packs 条目增 ui（icon/style_tags/lineage/theme_css/theme_css_text/page
  归一载荷，键缺席式）+ has_page 布尔（恒在）+ init_ui（开演设置页载荷，键缺
  席式），一并返回 combos；bootstrap 聚合键同源同改；
- 归一剔除：lineage 非法剔除（面板按缺省档 full 折叠）、theme_css/page 文件缺
  失剔除 + warning、routes/trait_pools 坏条目剔除；
- POST data/combos/{save,delete}：<user_config>/rp_combos/*.json，name/card_id
  必填、card_id 须在卡库、同 id 覆盖 created_at 保留；
- sessions 行增 pack_ids（agent_id 卡归属 ∪ 行内 wizard_pack_ids）+ state_summary
  （行内键不在=键缺席）+ ?pack= 过滤；
- GET game/page：声明序第一个 ui.page 有效包装配 {session_id, page_html}（单源
  装配标记被替换、theme_css 内联页头）；无声明 = page_html null 非 404；
- GET game/config 增 ui_page 布尔与 init_ui 声明本体（键缺席式兼容态）；
- 兼容态：出厂三卡零改动、无声明包路径行为逐字段等价。

用户空间隔离走 AGENTOS_USER_CONFIG_DIR（面板数据驱动测试同款真实解析链）。
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

MODES_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_EP = "/ext/mode_roleplay"
_PACKS_ROUTE = f"{_EP}/data/packs"
_SESSIONS_ROUTE = f"{_EP}/data/sessions"
_PAGE_ROUTE = f"{_EP}/game/page"
_CONFIG_ROUTE = f"{_EP}/game/config"
_PACKS_DIR = Path(MODES_DIR) / "mode_roleplay" / "packs"
_MANIFEST = Path(MODES_DIR) / "mode_roleplay" / "plugin.json"

pytestmark = pytest.mark.unit


def _load_server() -> Any:
    """按唯一模块名装载 mode_roleplay server.py（防与其它模式测试模块互覆）。"""
    path = os.path.join(MODES_DIR, "mode_roleplay", "server.py")
    spec = importlib.util.spec_from_file_location("mode_packdecl_roleplay", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def rp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """server 模块 + 用户空间隔离 + pipeline-state provider 注入。"""
    user_config = tmp_path / "user_config"
    monkeypatch.setenv("AGENTOS_USER_CONFIG_DIR", str(user_config))
    module = _load_server()
    module.reset_providers()
    module._reset_game_channels()
    module.user_config = user_config

    async def _state_rows(**_kwargs: Any) -> list[dict[str, Any]]:
        return list(module._DECL_TEST_ROWS)

    module._DECL_TEST_ROWS = [
        {"pipeline_id": "pipe-1", "thread_id": "thread-1", "mode": "roleplay",
         "agent_id": "mode_roleplay/card_hero", "workspace": str(tmp_path / "ws")}
    ]
    module._set_provider("pipeline-state", _state_rows)
    return module


def _call(module: Any, path: str, method: str = "GET", raw_body: str = "",
          query: dict[str, str] | None = None) -> dict[str, Any]:
    return asyncio.run(module.http_handle(path=path, method=method, raw_body=raw_body, query=query or {}))


def _body_json(result: dict[str, Any]) -> dict[str, Any]:
    assert result["success"] is True
    data = result["data"]
    assert data["body_encoding"] == "base64"
    return json.loads(base64.b64decode(data["body"]).decode("utf-8"))


def _user_pack(module: Any, pack_id: str, data: dict[str, Any], files: dict[str, str] | None = None) -> None:
    """用户层包落盘（<user_config>/packs/<id>.yaml + 伴随文件）。"""
    directory = Path(str(module._user_packs_dir()))
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{pack_id}.yaml").write_text(
        yaml.safe_dump(data, allow_unicode=True), encoding="utf-8"
    )
    for name, text in (files or {}).items():
        (directory / name).write_text(text, encoding="utf-8")


def _user_card(module: Any, card_id: str, data: dict[str, Any]) -> None:
    directory = Path(str(module._user_cards_dir()))
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{card_id}.yaml").write_text(
        yaml.safe_dump(data, allow_unicode=True), encoding="utf-8"
    )


# ── packs 清点：ui / has_page / init_ui / combos ──────────────────────────────


def test_packs_entry_has_page_boolean_always_present(rp: Any) -> None:
    """has_page 布尔恒在：出厂星露谷（ui.page+文件实存）=true、修仙（无 page）=false。"""
    by_id = {p["id"]: p for p in _body_json(_call(rp, _PACKS_ROUTE))["packs"]}
    assert by_id["stardew_social"]["has_page"] is True
    assert by_id["xianxia_combat"]["has_page"] is False


def test_factory_packs_have_bound_fixed_cast_cards() -> None:
    """玩法内定角色（2026-10-17 用户裁定）：出厂两包各带绑定卡（卡顶层 pack_ids
    引用包）= 开演免选卡的内定执行身份——星露谷世界卡（世界叙述者，固定卡司
    阿比/罗宾在包 variables/behaviors）、修仙对手卡洛青衍；卡文件实存。"""
    agents_dir = Path(MODES_DIR) / "mode_roleplay" / "agents"
    by_id: dict[str, dict] = {}
    for path in agents_dir.glob("card_*.yaml"):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        by_id[str(data.get("config_id") or path.stem)] = data
    assert by_id["card_stardew_world"]["pack_ids"] == ["stardew_social"]
    assert by_id["card_stardew_world"]["avatar"] == "🌱"
    assert by_id["card_qingyan"]["pack_ids"] == ["xianxia_combat"]
    # 世界卡执行身份=世界叙述者（不替玩家行动）
    sp = by_id["card_stardew_world"]["system_prompt"]
    assert "世界叙述者" in sp
    assert "不替玩家行动" in sp


def test_stardew_page_fixed_cast_no_card_select() -> None:
    """星露谷自包含页内定免选（2026-10-17 用户裁定）：无选卡件（pg-setup-card
    退役），内定身份=包绑定卡（pack_ids 过滤自动锁定），无绑定卡=显式报错不
    静默 + 开演钮禁用。"""
    html = (_PACKS_DIR / "stardew_page.html").read_text(encoding="utf-8")
    assert "pg-setup-card" not in html, "选卡下拉退役（内定免选）"
    assert "本玩法固定卡司" in html, "内定注记（免选可辨）"
    assert "pack_ids" in html, "内定身份锁定（pack_ids 过滤在场）"
    assert "boundCards[0].id" in html, "内定身份锁定（自动归位首卡）"
    assert "向导开演" in html, "无绑定卡指引组合玩法（显式不静默）"


# ── 玩法包标杆补齐（2026-10-17 标杆流程 §四；真值 docs/working/RP玩法包标杆流程_20261017.md）──


def test_factory_world_lorebooks_bound_to_executor_cards() -> None:
    """世界正典位（组成原则「包 = 卡+规则+世界书+初始状态」之世界书位）：两包
    世界书出厂实存 + 执行身份卡 lorebook_ids 绑定（material 逐轮关键词命中注入）。"""
    books_dir = Path(MODES_DIR) / "mode_roleplay" / "lorebooks"
    for book in ("stardew_valley.yaml", "qinglan_sect.yaml"):
        data = yaml.safe_load((books_dir / book).read_text(encoding="utf-8"))
        assert isinstance(data.get("entries"), list), f"{book} entries 须为列表"
        assert data["entries"], f"{book} 条目非空"
    agents_dir = Path(MODES_DIR) / "mode_roleplay" / "agents"

    def _card(card_id: str) -> dict:
        return yaml.safe_load((agents_dir / f"{card_id}.yaml").read_text(encoding="utf-8"))

    assert _card("card_stardew_world")["lorebook_ids"] == ["stardew_valley"]
    assert _card("card_qingyan")["lorebook_ids"] == ["qinglan_sect"]


def test_xianxia_narrative_graph_declared_and_aggregated(rp: Any) -> None:
    """固定剧情流程（标杆件 #8 Tier 1 载体）：修仙包 narrative_graph 形态自洽
    （节点/触发器 id 闭合）+ game/config 聚合后以 graph 键透出（宿主 4b 消费面），
    choices/rules 同面在透。"""
    pack = yaml.safe_load((_PACKS_DIR / "xianxia_combat.yaml").read_text(encoding="utf-8"))
    graph = pack["graph"]  # 包侧键名 = rp_packs 契约名（卡侧 narrative_graph 的包内名）
    node_ids = {n["id"] for n in graph["nodes"]}
    assert len(node_ids) == len(graph["nodes"]) >= 2, "节点非空且 id 唯一"
    for d in (dd for n in graph["nodes"] for dd in (n.get("decisions") or [])):
        assert d["next"] in node_ids, "decision next 闭合"
    for t in graph.get("triggers", []):
        assert t["jump_to"] in node_ids, "trigger jump_to 闭合"
    _user_card(rp, "card_hero", {"name": "剑修", "pack_ids": ["xianxia_combat"]})
    out = _body_json(_call(rp, _CONFIG_ROUTE, query={"session_id": "pipe-1"}))
    agg_nodes = out["narrative_graph"]["nodes"]
    assert {n["id"] for n in agg_nodes} == node_ids, "聚合后 narrative_graph.nodes 与包声明同源"
    assert len(out["choices"]) == 4, "choices 透出（宿主 4e 消费面）"
    assert out["rules"], "rules 透出（提示词承载/宿主双消费面）"


def test_factory_cast_cards_for_npc_surface() -> None:
    """卡司卡（标杆件 #6 卡司位）：阿比/罗宾出厂实存——tags 含 npc（NPC 深聊区
    与场景批消费面）、不绑 pack_ids（pack_ids=执行身份绑定面，唯一性归世界卡，
    防内定锁定多解）；青衍卡补 npc 标签（深聊区可列）。"""
    agents_dir = Path(MODES_DIR) / "mode_roleplay" / "agents"

    def _card(card_id: str) -> dict:
        return yaml.safe_load((agents_dir / f"{card_id}.yaml").read_text(encoding="utf-8"))

    for cast_id in ("card_abby", "card_robin"):
        cast = _card(cast_id)
        assert "npc" in cast["tags"], f"{cast_id} npc 标签（深聊区过滤面）"
        assert not cast.get("pack_ids"), f"{cast_id} 不绑包（执行身份唯一性）"
        assert cast["system_prompt"], f"{cast_id} system_prompt 在场"
        assert cast["first_mes"], f"{cast_id} first_mes 在场"
    assert "npc" in _card("card_qingyan")["tags"], "青衍 npc 标签（深聊区可列）"


def test_stardew_page_consumes_choices() -> None:
    """选项面闭环（标杆件 #4）：星露谷自包含页消费 game/config choices——按钮卡
    容器/委托（点击=填输入+发送）+ 主题样式，声明必有消费位。"""
    html = (_PACKS_DIR / "stardew_page.html").read_text(encoding="utf-8")
    for token in (
        'id="pg-choices"',
        "renderChoices(out.choices",
        'data-ci=',
        "useChoice(",
    ):
        assert token in html, f"choices 消费锚缺席: {token}"
    css = (_PACKS_DIR / "stardew_theme.css").read_text(encoding="utf-8")
    assert ".pg-choices .sd-choice" in css, "choices 按钮样式（主题 css 消费位）"


def test_packs_factory_ui_and_init_ui_normalized(rp: Any) -> None:
    """出厂两包 ui/init_ui 归一载荷与包内 yaml 同源：谱系档/图标/标签/主题/页
    文件、路线与词条（含 effects 透传）。"""
    by_id = {p["id"]: p for p in _body_json(_call(rp, _PACKS_ROUTE))["packs"]}

    stardew = by_id["stardew_social"]
    assert stardew["ui"]["icon"] == "🌱"
    assert stardew["ui"]["lineage"] == "full"
    assert stardew["ui"]["style_tags"] == ["像素", "好感三档"]
    assert stardew["ui"]["theme_css"] == "stardew_theme.css"
    assert stardew["ui"]["page"] == "stardew_page.html"
    assert "像素主题" in stardew["ui"]["theme_css_text"], "附加缝：包内主题文本内联"
    init = stardew["init_ui"]
    assert init["show_variables"] is True
    assert [r["id"] for r in init["routes"]] == ["inherit_farm", "escape_city"]
    traits = init["trait_pools"][0]
    assert traits["id"] == "stardew_traits"
    assert [o["id"] for o in traits["options"]] == ["angler", "farmer", "miner", "social_star"]
    red = traits["options"][3]
    assert red["effects"] == {"npc.阿比.好感度": "+5", "npc.罗宾.好感度": "+5"}

    xianxia = by_id["xianxia_combat"]
    assert xianxia["ui"]["icon"] == "⚔️"
    assert xianxia["ui"]["lineage"] == "full"
    assert "水墨主题" in xianxia["ui"]["theme_css_text"]
    assert "page" not in xianxia["ui"], "修仙无 ui.page 声明"
    assert [r["id"] for r in xianxia["init_ui"]["routes"]] == ["lone_cultivator", "sect_disciple"]
    assert any(o.get("effects") == {"player.气血": "+10"}
               for o in xianxia["init_ui"]["trait_pools"][0]["options"]), "effects 示范条目"


def test_packs_no_declaration_keys_absent_compat(rp: Any) -> None:
    """兼容态：无 ui/init_ui 声明的用户包条目键缺席（零新增键），has_page=false。"""
    _user_pack(rp, "plain_pack", {"name": "朴素包", "variables": {}})
    item = next(p for p in _body_json(_call(rp, _PACKS_ROUTE))["packs"] if p["id"] == "plain_pack")
    assert item["has_page"] is False
    assert "ui" not in item
    assert "init_ui" not in item


def test_packs_invalid_lineage_dropped_missing_theme_file_warned(rp: Any, caplog: pytest.LogCaptureFixture) -> None:
    """归一剔除：lineage 非法剔除（has_page/键缺席照常）、theme_css 文件缺失剔除
    + warning。"""
    _user_pack(rp, "broken_pack", {
        "name": "声明坏件包",
        "ui": {"icon": "🎲", "lineage": "epic", "theme_css": "不存在.css"},
    })
    item = next(p for p in _body_json(_call(rp, _PACKS_ROUTE))["packs"] if p["id"] == "broken_pack")
    assert "lineage" not in item["ui"], "非法 lineage 剔除（面板按缺省档 full 折叠）"
    assert "theme_css" not in item["ui"], "文件缺失声明剔除"
    assert "theme_css_text" not in item["ui"]
    assert item["has_page"] is False
    warnings_text = " ".join(r.getMessage() for r in caplog.records)
    assert "theme_css" in warnings_text, "剔除 warning 留痕"


def test_packs_init_ui_bad_entries_dropped(rp: Any) -> None:
    """init_ui 坏条目剔除：缺 label 的 route 与空 options 的池不透出，合法条目保留。"""
    _user_pack(rp, "half_broken", {
        "init_ui": {
            "title": "开新的一局",
            "routes": [
                {"id": "ok", "label": "合法路线"},
                {"id": "no_label"},
                {"id": "ok", "label": "重复 id"},
            ],
            "trait_pools": [
                {"id": "empty", "label": "空池"},
                {"id": "pool", "label": "词条池", "options": [
                    {"id": "t1", "label": "词条一"},
                    "坏条目",
                ]},
            ],
        },
    })
    item = next(p for p in _body_json(_call(rp, _PACKS_ROUTE))["packs"] if p["id"] == "half_broken")
    assert item["init_ui"]["routes"] == [{"id": "ok", "label": "合法路线"}]
    assert item["init_ui"]["trait_pools"] == [
        {"id": "pool", "label": "词条池", "options": [{"id": "t1", "label": "词条一"}]}
    ]


def test_packs_user_page_file_required_for_has_page(rp: Any) -> None:
    """ui.page 声明但文件缺失 = 剔除 + has_page=false（大厅不路由直达游戏页）。"""
    _user_pack(rp, "pageless", {"ui": {"icon": "📜", "page": "缺失.html"}})
    item = next(p for p in _body_json(_call(rp, _PACKS_ROUTE))["packs"] if p["id"] == "pageless")
    assert item["has_page"] is False
    assert "page" not in item["ui"]


def test_bootstrap_packs_share_listing_shape(rp: Any) -> None:
    """bootstrap 聚合键与 data/packs 同源（has_page/ui/init_ui 一处改两处生效）。"""
    bootstrap = _body_json(_call(rp, f"{_EP}/data/bootstrap"))
    standalone = _body_json(_call(rp, _PACKS_ROUTE))
    assert bootstrap["packs"] == standalone["packs"]


# ── combos：读面 + save/delete ────────────────────────────────────────────────


def test_combos_crud_cycle_and_overwrite_keeps_created_at(rp: Any) -> None:
    _user_card(rp, "card_hero", {"name": "侠客"})
    saved = _body_json(_call(rp, f"{_EP}/data/combos/save", "POST", json.dumps({
        "name": "我的修仙局", "card_id": "card_hero",
        "route_id": "lone_cultivator", "trait_ids": ["iron_body"],
        "lorebook_ids": [], "pack_ids": ["xianxia_combat"],
    })))
    combo_id = saved["combo_id"]
    assert combo_id, "id 缺省按 name 派生"

    again = _body_json(_call(rp, f"{_EP}/data/combos/save", "POST", json.dumps({
        "id": combo_id, "name": "我的修仙局·改", "card_id": "card_hero",
    })))
    assert again["combo_id"] == combo_id
    assert again["created_at"] == saved["created_at"], "同 id 覆盖 created_at 保留"

    listing = _body_json(_call(rp, _PACKS_ROUTE))
    assert len(listing["combos"]) == 1
    combo = listing["combos"][0]
    assert combo["id"] == combo_id
    assert combo["name"] == "我的修仙局·改"
    assert combo["card_id"] == "card_hero"
    assert combo["origin"] == "user"
    assert combo["trait_ids"] == [], "同 id 覆盖 = 载荷整体替换（非字段级合并）"

    deleted = _body_json(_call(rp, f"{_EP}/data/combos/delete", "POST", json.dumps({"id": combo_id})))
    assert deleted == {"combo_id": combo_id, "deleted": True}
    assert _body_json(_call(rp, _PACKS_ROUTE))["combos"] == []


def test_combos_profile_extra_optional_roundtrip(rp: Any) -> None:
    """profile_extra（刀2 可选承载：面板组合③自由文本初始状态段）——带值落盘
    读取回传；不带值键缺席（只增不破）；同 id 覆盖空值 = 键消失（整体替换）。"""
    _user_card(rp, "card_hero", {"name": "侠客"})
    saved = _body_json(_call(rp, f"{_EP}/data/combos/save", "POST", json.dumps({
        "name": "带初始状态", "card_id": "card_hero",
        "profile_extra": "开场重伤；灵力封印",
    }, ensure_ascii=False)))
    listing = _body_json(_call(rp, _PACKS_ROUTE))["combos"]
    assert listing[0]["profile_extra"] == "开场重伤；灵力封印"

    _body_json(_call(rp, f"{_EP}/data/combos/save", "POST", json.dumps({
        "id": saved["combo_id"], "name": "带初始状态", "card_id": "card_hero",
    }, ensure_ascii=False)))
    listing = _body_json(_call(rp, _PACKS_ROUTE))["combos"]
    assert "profile_extra" not in listing[0], "空值覆盖 = 键缺席（载荷整体替换）"


def test_combos_missing_dir_and_corrupt_file_fail_soft(rp: Any, caplog: pytest.LogCaptureFixture) -> None:
    """fail-soft：目录缺失 = 空表；损坏件跳过 + warning 不炸清点端点。"""
    assert _body_json(_call(rp, _PACKS_ROUTE))["combos"] == []
    directory = Path(str(rp._user_combos_dir()))
    directory.mkdir(parents=True)
    (directory / "bad.json").write_text("{坏 json", encoding="utf-8")
    (directory / "good.json").write_text(
        json.dumps({"name": "好件", "card_id": "任意", "origin": "user", "id": "good"}, ensure_ascii=False),
        encoding="utf-8",
    )
    combos = _body_json(_call(rp, _PACKS_ROUTE))["combos"]
    assert [c["id"] for c in combos] == ["good"], "损坏件跳过"
    assert any("bad.json" in r.getMessage() for r in caplog.records)


def test_combos_save_validation_fail_closed(rp: Any) -> None:
    """name/card_id 必填、card_id 须在卡库、非法 id 拒绝——fail-closed。"""
    _user_card(rp, "card_hero", {"name": "侠客"})
    cases = [
        ({"card_id": "card_hero"}, "缺少 name"),
        ({"name": "无名卡组合"}, "缺少 card_id"),
        ({"name": "挂空卡", "card_id": "no_such_card"}, "未知角色卡"),
        ({"name": "坏 id", "card_id": "card_hero", "id": "越权/id"}, "非法 id"),
    ]
    for body, fragment in cases:
        out = _body_json(_call(rp, f"{_EP}/data/combos/save", "POST", json.dumps(body, ensure_ascii=False)))
        assert "error" in out, f"{body} → {out}"
        assert fragment in out["error"], f"{body} → {out}"


def test_combos_delete_unknown_id_error(rp: Any) -> None:
    out = _body_json(_call(rp, f"{_EP}/data/combos/delete", "POST", json.dumps({"id": "ghost"})))
    assert "error" in out
    assert "ghost" in out["error"]


# ── sessions：pack_ids / state_summary / ?pack= 过滤 ──────────────────────────


def test_sessions_pack_ids_from_card_and_wizard_key(rp: Any) -> None:
    """pack_ids = 行 agent_id 卡归属 ∪ 行内 wizard_pack_ids 持久键（去重保序）。"""
    _user_card(rp, "card_hero", {"name": "侠客", "pack_ids": ["xianxia_combat"]})
    rp._DECL_TEST_ROWS = [
        {"pipeline_id": "pipe-1", "mode": "roleplay", "agent_id": "mode_roleplay/card_hero",
         "wizard_pack_ids": "stardew_social,xianxia_combat"},
        {"pipeline_id": "pipe-2", "mode": "roleplay", "agent_id": "mode_roleplay/ghost"},
        {"pipeline_id": "pipe-3", "mode": "roleplay", "wizard_pack_ids": ["stardew_social"]},
    ]
    sessions = {s["pipeline_id"]: s for s in _body_json(_call(rp, _SESSIONS_ROUTE))["sessions"]}
    assert sessions["pipe-1"]["pack_ids"] == ["xianxia_combat", "stardew_social"]
    assert sessions["pipe-2"]["pack_ids"] == [], "不可得 = 空数组如实"
    assert sessions["pipe-3"]["pack_ids"] == ["stardew_social"], "list 形态同收"


def test_sessions_state_summary_present_and_absent(rp: Any) -> None:
    """state_summary：行内持久 state 键在 → 摘要 dict；不在 → 键缺席（禁止发明）。"""
    rp._DECL_TEST_ROWS = [
        {"pipeline_id": "pipe-1", "mode": "roleplay", "agent_id": "mode_roleplay/card_hero",
         "context.character_state_text": "气血 85 · 灵力 40"},
        {"pipeline_id": "pipe-2", "mode": "roleplay", "agent_id": "mode_roleplay/card_hero"},
    ]
    sessions = {s["pipeline_id"]: s for s in _body_json(_call(rp, _SESSIONS_ROUTE))["sessions"]}
    assert sessions["pipe-1"]["state_summary"] == {"character_state_text": "气血 85 · 灵力 40"}
    assert "state_summary" not in sessions["pipe-2"]


def test_sessions_pack_filter(rp: Any) -> None:
    """/sessions?pack=<id>：pack_ids 命中即留（「继续上局」按玩法归属）。"""
    _user_card(rp, "card_hero", {"name": "侠客", "pack_ids": ["xianxia_combat"]})
    rp._DECL_TEST_ROWS = [
        {"pipeline_id": "pipe-1", "mode": "roleplay", "agent_id": "mode_roleplay/card_hero"},
        {"pipeline_id": "pipe-2", "mode": "roleplay", "wizard_pack_ids": "stardew_social"},
    ]
    filtered = _body_json(_call(rp, _SESSIONS_ROUTE, query={"pack": "xianxia_combat"}))["sessions"]
    assert [s["pipeline_id"] for s in filtered] == ["pipe-1"]
    unfiltered = _body_json(_call(rp, _SESSIONS_ROUTE))["sessions"]
    assert len(unfiltered) == 2, "无过滤参数 = 全量（现状零变化）"


# ── game/config：ui_page + init_ui 透出 ──────────────────────────────────────


def test_game_config_ui_page_and_init_ui_for_stardew_session(rp: Any) -> None:
    """星露谷会话：ui_page=true + init_ui 声明本体透出（本刀只透出不消费）。"""
    _user_card(rp, "card_hero", {"name": "农场主", "pack_ids": ["stardew_social"]})
    out = _body_json(_call(rp, _CONFIG_ROUTE, query={"session_id": "pipe-1"}))
    assert out["ui_page"] is True
    assert out["init_ui"]["routes"][0]["id"] == "inherit_farm"
    assert "trait_pools" in out["init_ui"]


def test_game_config_no_page_pack_ui_page_key_absent(rp: Any) -> None:
    """兼容态：无 ui.page 声明 = ui_page 键缺席（Tier 0 载荷零新增键）。"""
    _user_card(rp, "card_hero", {"name": "剑修", "pack_ids": ["xianxia_combat"]})
    out = _body_json(_call(rp, _CONFIG_ROUTE, query={"session_id": "pipe-1"}))
    assert "ui_page" not in out
    assert "init_ui" in out, "init_ui 透出与 ui.page 无关（声明本体独立透出面）"


def test_game_config_first_pack_with_page_wins(rp: Any) -> None:
    """多包声明序：第一个 ui.page 有效包定 ui_page（后续包不覆盖判定）。"""
    _user_pack(rp, "second_page", {"ui": {"page": "缺失.html"}})  # 文件缺失=无效
    _user_card(rp, "card_hero", {"name": "双包", "pack_ids": ["stardew_social", "second_page"]})
    out = _body_json(_call(rp, _CONFIG_ROUTE, query={"session_id": "pipe-1"}))
    assert out["ui_page"] is True


# ── game/page：装配载荷通道 ───────────────────────────────────────────────────


def test_game_page_assembles_stardew_pack_page(rp: Any) -> None:
    """命中 ui.page 有效包：page.html 装配（单源标记被替换）+ theme_css 内联页头。"""
    _user_card(rp, "card_hero", {"name": "农场主", "pack_ids": ["stardew_social"]})
    out = _body_json(_call(rp, _PAGE_ROUTE, query={"session_id": "pipe-1"}))
    assert out["session_id"] == "pipe-1"
    html = out["page_html"]
    assert html is not None
    assert "/*@agentos-include:rp-sanitize*/" not in html, "装配标记已被单源块原位替换"
    assert "function rpSanitizeHtml" in html, "rp-sanitize 单源注入"
    assert "function agentosFetch" in html, "rp-bridge 单源注入"
    assert html.index("<style>") < html.index("<body"), "theme_css <style> 内联页头"
    assert "像素主题" in html, "内联的是包内 stardew_theme.css 文本"


def test_game_page_no_declaration_returns_null_not_404(rp: Any) -> None:
    """无任何包声明有效 page = {session_id, page_html: null} 非 404（回落通用渲染）。"""
    _user_card(rp, "card_hero", {"name": "剑修", "pack_ids": ["xianxia_combat"]})
    out = _body_json(_call(rp, _PAGE_ROUTE, query={"session_id": "pipe-1"}))
    assert out == {"session_id": "pipe-1", "page_html": None}
    no_card = _body_json(_call(rp, _PAGE_ROUTE, query={"session_id": "pipe-1"}))
    assert no_card["page_html"] is None


def test_game_page_missing_session_failclosed(rp: Any) -> None:
    out = _body_json(_call(rp, _PAGE_ROUTE))
    assert "error" in out
    assert "缺少 session_id" in out["error"]
    bad = _body_json(_call(rp, _PAGE_ROUTE, query={"session_id": "越权/../id"}))
    assert "error" in bad
    assert "非法 session_id" in bad["error"]


# ── game/page pack 通道（刀4：会话未开的 setup 装配面）────────────────────────


def test_game_page_by_pack_serves_stardew_page(rp: Any) -> None:
    """pack 通道：会话未开按包 id 直取装配（双根用户层接管同口径），单源标记
    替换 + theme_css 内联与 session 通道同机制；响应形状 {pack_id, page_html}。"""
    out = _body_json(_call(rp, _PAGE_ROUTE, query={"pack": "stardew_social"}))
    assert out["pack_id"] == "stardew_social"
    html = out["page_html"]
    assert html is not None
    assert "/*@agentos-include:rp-sanitize*/" not in html
    assert "function rpSanitizeHtml" in html
    assert "function agentosFetch" in html
    assert html.index("<style>") < html.index("<body"), "theme_css 内联页头"
    assert "像素主题" in html


def test_game_page_by_pack_user_layer_takeover(rp: Any) -> None:
    """pack 通道双根：用户层同 id 包接管（页与主题按用户包目录装配）。"""
    _user_pack(rp, "stardew_social", {
        "name": "用户层星露谷",
        "ui": {"page": "page.html", "theme_css": "t.css"},
    }, files={
        "t.css": "/* 用户层主题 */",
        "page.html": "<html><head></head><body>\n"
                     "/*@agentos-include:rp-sanitize*/\n"
                     "/*@agentos-include:rp-bridge*/\n"
                     "<p>用户层页</p></body></html>",
    })
    out = _body_json(_call(rp, _PAGE_ROUTE, query={"pack": "stardew_social"}))
    assert out["page_html"] is not None
    assert "用户层页" in out["page_html"]
    assert "用户层主题" in out["page_html"]


def test_game_page_by_pack_unknown_or_pageless_null(rp: Any) -> None:
    """pack 通道兼容态：未知包 / 无 page 声明包（修仙）= page_html null 非 404。"""
    unknown = _body_json(_call(rp, _PAGE_ROUTE, query={"pack": "no_such_pack"}))
    assert unknown == {"pack_id": "no_such_pack", "page_html": None}
    pageless = _body_json(_call(rp, _PAGE_ROUTE, query={"pack": "xianxia_combat"}))
    assert pageless == {"pack_id": "xianxia_combat", "page_html": None}


def test_game_page_by_pack_invalid_id_400(rp: Any) -> None:
    bad = _body_json(_call(rp, _PAGE_ROUTE, query={"pack": "越权/../pack"}))
    assert "error" in bad
    assert "非法 pack" in bad["error"]


def test_game_page_by_pack_broken_page_falls_back_soft(rp: Any, caplog: pytest.LogCaptureFixture) -> None:
    """pack 通道装配失败 = fail-soft null + warning（与 session 通道同回落契约）。"""
    _user_pack(rp, "broken_pack_page", {"ui": {"page": "p.html"}}, files={"p.html": "<html><body>缺标记</body></html>"})
    out = _body_json(_call(rp, _PAGE_ROUTE, query={"pack": "broken_pack_page"}))
    assert out == {"pack_id": "broken_pack_page", "page_html": None}
    assert any("pack 通道" in r.getMessage() for r in caplog.records)


def test_game_page_optional_core_assembly_compat(rp: Any) -> None:
    """rp-core 可选装配（刀4）：无标记页继续原样装配（前刀用户包页兼容零变化）；
    带标记页注入面板 rp-panel-core 单源块（rp_init 组装函数可供页内 setup）。"""
    _user_pack(rp, "legacy_page", {
        "name": "旧页包",
        "ui": {"page": "p.html"},
    }, files={
        "p.html": "<html><body>\n"
                  "/*@agentos-include:rp-sanitize*/\n"
                  "/*@agentos-include:rp-bridge*/\n"
                  "<p>旧页无 rp-core 标记</p></body></html>",
    })
    _user_card(rp, "card_legacy", {"name": "旧卡", "pack_ids": ["legacy_page"]})
    rp._DECL_TEST_ROWS[0]["agent_id"] = "mode_roleplay/card_legacy"
    legacy = _body_json(_call(rp, _PAGE_ROUTE, query={"session_id": "pipe-1"}))
    assert "旧页无 rp-core 标记" in legacy["page_html"]
    assert "function rpInitSnapshot" not in legacy["page_html"], "无标记=不注入（兼容态）"

    _user_pack(rp, "core_page", {
        "name": "新页包",
        "ui": {"page": "c.html"},
    }, files={
        "c.html": "<html><body>\n"
                  "/*@agentos-include:rp-sanitize*/\n"
                  "/*@agentos-include:rp-bridge*/\n"
                  "/*@agentos-include:rp-core*/\n"
                  "<p>新页带 rp-core 标记</p></body></html>",
    })
    _user_card(rp, "card_core", {"name": "新卡", "pack_ids": ["core_page"]})
    module = rp
    module._DECL_TEST_ROWS[0]["agent_id"] = "mode_roleplay/card_core"
    modern = _body_json(_call(rp, _PAGE_ROUTE, query={"session_id": "pipe-1"}))
    html = modern["page_html"]
    assert "新页带 rp-core 标记" in html
    assert "function rpInitSnapshot" in html, "rp-core 单源块注入"
    assert "rp-panel-core:single-source-begin" not in html, "注入的是块内容非标记"


def test_game_page_user_pack_takeover_with_theme(rp: Any, tmp_path: Path) -> None:
    """用户层接管根解析：page.html 与 theme.css 按用户包目录读取。"""
    _user_pack(rp, "homebrew_page", {
        "name": "自建页包",
        "ui": {"icon": "🧩", "theme_css": "my.css", "page": "my.html"},
    }, files={
        "my.css": "/* 自建主题 */ .x { color: red; }",
        "my.html": "<html><head><title>t</title></head><body>\n"
                   "/*@agentos-include:rp-sanitize*/\n"
                   "/*@agentos-include:rp-bridge*/\n"
                   "<p>自建玩法页</p></body></html>",
    })
    _user_card(rp, "card_hero", {"name": "自建", "pack_ids": ["homebrew_page"]})
    out = _body_json(_call(rp, _PAGE_ROUTE, query={"session_id": "pipe-1"}))
    html = out["page_html"]
    assert "自建玩法页" in html
    assert "自建主题" in html, "theme_css 按用户包目录内联"
    assert "function agentosFetch" in html


def test_game_page_broken_author_page_falls_back_soft(rp: Any, caplog: pytest.LogCaptureFixture) -> None:
    """作者页缺装配标记 = 装配失败 fail-soft 回落 page_html null + warning（面板不断链）。"""
    _user_pack(rp, "broken_page", {
        "ui": {"page": "bad.html"},
    }, files={"bad.html": "<html><body>缺装配标记</body></html>"})
    _user_card(rp, "card_hero", {"name": "坏页", "pack_ids": ["broken_page"]})
    out = _body_json(_call(rp, _PAGE_ROUTE, query={"session_id": "pipe-1"}))
    assert out["page_html"] is None
    assert any("装配失败" in r.getMessage() for r in caplog.records)


# ── 兼容态对照与路由注册 ───────────────────────────────────────────────────────


def test_compat_factory_cards_and_packs_unchanged_shapes(rp: Any) -> None:
    """兼容态：出厂三卡零改动；无声明包（用户 plain 包）条目形状逐字段等价
    （基础六键 + has_page，ui/init_ui 键缺席）。"""
    cards = _body_json(_call(rp, f"{_EP}/data/cards"))["cards"]
    builtin_cards = {c["id"] for c in cards if c["origin"] == "builtin"}
    assert {"card_luna", "card_kael", "card_mira"} <= builtin_cards, \
        "出厂卡库照常在列（卡资产零改写）"
    _user_pack(rp, "plain_pack", {"name": "朴素包", "variables": {"a": {"type": "int"}}})
    item = next(p for p in _body_json(_call(rp, _PACKS_ROUTE))["packs"] if p["id"] == "plain_pack")
    assert set(item) == {"id", "origin", "name", "description", "variables_count",
                         "behaviors_count", "has_page"}


# ── 玩法级管道绑定（世界内嵌谱系右移批次一，调研 §6.4 声明面开放）──────────────
# 契约：pack yaml 顶层可选键 pipeline: <config/pipelines 登记名>——消费侧
# （清点/game.config/bind）值合法性 = 登记存在性，非法剔除回落缺省 roleplay
# + warning（与 ui.lineage 剔除回落同构）；条目键缺席式（声明且合法才带键，
# 与 ui/init_ui 风格对齐）。


def _user_pipeline(module: Any, name: str) -> None:
    """登记管道夹具：<user_config>/pipelines/<name>.yaml 落盘（内核按名加载
    用户层优先同源——登记存在性检查的消费面锚）。"""
    directory = Path(str(module._user_pipelines_dir()))
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.yaml").write_text("loop_bodies: []\n", encoding="utf-8")


@pytest.mark.parametrize("declared", ["triple_chain", "chat_rules_hybrid"])
def test_pack_pipeline_registered_declaration_passthrough(rp: Any, declared: str) -> None:
    """合法登记名声明 → 清点条目透出 pipeline 键（值=声明原样）。"""
    _user_pipeline(rp, declared)
    _user_pack(rp, "chain_pack", {"name": "链包", "pipeline": declared,
                                  "variables": {"a": {"type": "int"}}})
    item = next(p for p in _body_json(_call(rp, _PACKS_ROUTE))["packs"] if p["id"] == "chain_pack")
    assert item["pipeline"] == declared


def test_pack_pipeline_absent_entry_key_free(rp: Any) -> None:
    """缺省零变化：无声明包条目无 pipeline 键（消费面按契约缺省 roleplay 折叠）。"""
    _user_pack(rp, "plain_pipe_pack", {"name": "朴素包"})
    item = next(
        p for p in _body_json(_call(rp, _PACKS_ROUTE))["packs"] if p["id"] == "plain_pipe_pack"
    )
    assert "pipeline" not in item


@pytest.mark.parametrize("bad", [123, "", "BadChain", "带 空格"])
def test_pack_pipeline_illegal_shape_dropped(rp: Any, bad: Any, caplog: Any) -> None:
    """形态非法（非字符串/空串/大写/含空白）→ 剔除声明（键缺席）+ warning。"""
    _user_pack(rp, "bad_shape_pack", {"name": "坏形包", "pipeline": bad})
    item = next(
        p for p in _body_json(_call(rp, _PACKS_ROUTE))["packs"] if p["id"] == "bad_shape_pack"
    )
    assert "pipeline" not in item
    assert any("pipeline" in r.getMessage() for r in caplog.records), caplog.text


def test_pack_pipeline_unregistered_dropped_with_warning(rp: Any, caplog: Any) -> None:
    """未登记名（形态合法但 config/pipelines 无此名）→ 剔除回落 + warning，
    与 ui.lineage 非法剔除回落缺省同构。"""
    _user_pack(rp, "ghost_pack", {"name": "幽灵链包", "pipeline": "no_such_chain"})
    item = next(
        p for p in _body_json(_call(rp, _PACKS_ROUTE))["packs"] if p["id"] == "ghost_pack"
    )
    assert "pipeline" not in item
    assert any("no_such_chain" in r.getMessage() for r in caplog.records), caplog.text


def test_game_config_pipeline_last_declared_wins_and_absent_key_free(rp: Any) -> None:
    """game/config 聚合透出 pipeline（宿主/开演消费面）：多包声明序后者胜、
    消费侧登记校验同闸；无声明 = 键缺席（兼容态零新增）。"""
    _user_pipeline(rp, "chain_a")
    _user_pipeline(rp, "chain_b")
    _user_card(rp, "card_hero", {"name": "主角", "pack_ids": ["pack_a", "pack_b"]})
    _user_pack(rp, "pack_a", {"name": "A 包", "pipeline": "chain_a"})
    _user_pack(rp, "pack_b", {"name": "B 包", "pipeline": "chain_b"})
    out = _body_json(_call(rp, _CONFIG_ROUTE, query={"session_id": "pipe-1"}))
    assert out["pipeline"] == "chain_b"
    # 声明序后者未声明 = 前者生效（逐包独立校验，非整组作废）
    _user_pack(rp, "pack_c", {"name": "C 包"})
    _user_card(rp, "card_hero2", {"name": "主角二", "pack_ids": ["pack_a", "pack_c"]})
    rows = list(rp._DECL_TEST_ROWS)
    rows.append({"pipeline_id": "pipe-2", "thread_id": "thread-2", "mode": "roleplay",
                 "agent_id": "mode_roleplay/card_hero2", "workspace": str(rp.user_config / "ws2")})
    rp._DECL_TEST_ROWS = rows
    out2 = _body_json(_call(rp, _CONFIG_ROUTE, query={"session_id": "pipe-2"}))
    assert out2["pipeline"] == "chain_a"
    # 全员无声明 = 键缺席
    _user_card(rp, "card_hero3", {"name": "主角三", "pack_ids": ["pack_c"]})
    rows.append({"pipeline_id": "pipe-3", "thread_id": "thread-3", "mode": "roleplay",
                 "agent_id": "mode_roleplay/card_hero3", "workspace": str(rp.user_config / "ws3")})
    rp._DECL_TEST_ROWS = rows
    out3 = _body_json(_call(rp, _CONFIG_ROUTE, query={"session_id": "pipe-3"}))
    assert "pipeline" not in out3


def test_factory_packs_inventory_has_no_pipeline_key(rp: Any) -> None:
    """出厂两包保持回落态（台账批次一裁定：config/pipelines 无第二条真实可用
    交互管道，不新造不绑）：清点条目零 pipeline 键——出厂行为零变化显式锚。"""
    by_id = {p["id"]: p for p in _body_json(_call(rp, _PACKS_ROUTE))["packs"]}
    assert "pipeline" not in by_id["xianxia_combat"]
    assert "pipeline" not in by_id["stardew_social"]


@pytest.mark.parametrize("relpath", [
    "webview/roleplay_panel.html",
    "webview/game_host.html",
    "packs/stardew_page.html",
])
def test_session_senders_pass_pack_pipeline(relpath: str) -> None:
    """三开演载荷发送方透传锚：携包上下文时读清点条目 pipeline 键、以
    payload.pipeline_config_id 上行（wire 键=内核 chat.send_message 参数同名）。"""
    html = (Path(MODES_DIR) / "mode_roleplay" / relpath).read_text(encoding="utf-8")
    assert ".pipeline" in html, f"{relpath}: 未读包绑定管道（清点键 pipeline）"
    assert "pipeline_config_id" in html, f"{relpath}: 未透传 pipeline_config_id"


def _function_block(html: str, func_name: str) -> str:
    """截取具名函数体（花括号配平）——静态钉按发送方分块：整文件子串会命中
    别的函数造成假绿（批次一 pipeline 钉即整文件口径，本刀收紧到函数块）。"""
    start = html.index(f"function {func_name}(")
    idx = html.index("{", start)
    depth = 0
    while True:
        ch = html[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return html[start:idx + 1]
        idx += 1


@pytest.mark.parametrize(("relpath", "func_name", "theme_expr"), [
    ("webview/roleplay_panel.html", "playCard", "c.theme"),
    ("webview/roleplay_panel.html", "comboFinish", "c.theme"),
    ("webview/game_host.html", "setupStart", "card.theme"),
    ("packs/stardew_page.html", "setupStart", "card.theme"),
])
def test_session_senders_pass_card_theme(relpath: str, func_name: str, theme_expr: str) -> None:
    """卡主题随扮演会话出生（批次四）：四处开演发送方把所选卡的 theme 载荷放进
    payload.card_theme（wire 键=完整 ThemeConfig 档，形态=卡库 /data/cards 条目
    的 theme 字段）——无主题卡走键缺席，宿主出生行为与现役一致。回执事实位
    theme_pinned 由宿主回（本刀不改服务端端点）。"""
    html = (Path(MODES_DIR) / "mode_roleplay" / relpath).read_text(encoding="utf-8")
    block = _function_block(html, func_name)
    assert "card_theme" in block, f"{relpath}:{func_name} 未透传 card_theme"
    assert f"payload.card_theme = {theme_expr};" in block, (
        f"{relpath}:{func_name} card_theme 未取所选卡 theme 载荷"
        f"（期望 `payload.card_theme = {theme_expr};`）"
    )
    assert f"if ({theme_expr})" in block, f"{relpath}:{func_name} 缺无主题卡的键缺席守卫"


def test_manifest_declares_new_routes() -> None:
    """路由面双注册：game/page GET + combos save/delete POST 全在 manifest 声明。"""
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    declared = {(ep["method"], ep["path"]) for ep in manifest["http_endpoints"]}
    assert ("GET", _PAGE_ROUTE) in declared
    assert ("POST", f"{_EP}/data/combos/save") in declared
    assert ("POST", f"{_EP}/data/combos/delete") in declared


# ── 出厂两包 fixture：声明面结构合法 + effects 键域自洽 + 包页脚本可编译 ────────


def test_factory_packs_presentation_declarations_lint_clean() -> None:
    """出厂两包 ui/init_ui 过 card_lint（域主内部单源）同源结构校验零警告。"""
    sys.path.insert(0, str(_PACKS_DIR.parent))  # card_lint 单源宿主（T6c 域主目录）
    from card_lint import lint_init_ui, lint_ui

    for path in sorted(_PACKS_DIR.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert lint_ui(data.get("ui")) == [], path.name
        assert lint_init_ui(data.get("init_ui")) == [], path.name


@pytest.mark.parametrize("pack_id", ["xianxia_combat", "stardew_social"])
def test_factory_pack_init_effects_keys_within_variables(pack_id: str) -> None:
    """effects 键域 ⊆ 包内 variables 声明叶（与 Validator fail-closed 同源自洽）。"""
    sys.path.insert(0, str(MODES_DIR))  # _libs 包路径（T6c 契约三件套单源）
    from _libs.card_contract import flatten_variables

    data = yaml.safe_load((_PACKS_DIR / f"{pack_id}.yaml").read_text(encoding="utf-8"))
    flat, _skipped = flatten_variables(data.get("variables") or {})
    for pool in (data.get("init_ui") or {}).get("trait_pools") or []:
        for option in pool.get("options") or []:
            for key in (option.get("effects") or {}):
                assert key in flat, f"{pack_id}.{option['id']}: effects 键 {key!r} 不在 variables 声明内"


def test_factory_pack_page_assets_present_and_script_compiles() -> None:
    """星露谷 ui.page/theme_css 文件实存且装配标记齐备；页内脚本 node --check 可编译。
    刀4 改钉（台账归因）：页内 setup 阶段消费 rp-core 单源（rp_init 组装），装配
    标记三件齐备。"""
    import re
    import subprocess
    import tempfile

    assert (_PACKS_DIR / "stardew_theme.css").is_file()
    assert (_PACKS_DIR / "xianxia_theme.css").is_file()
    page_path = _PACKS_DIR / "stardew_page.html"
    html = page_path.read_text(encoding="utf-8")
    assert "/*@agentos-include:rp-sanitize*/" in html
    assert "/*@agentos-include:rp-bridge*/" in html
    assert "/*@agentos-include:rp-core*/" in html, "rp-core 装配标记（页内 setup 消费 rp_init 组装单源）"
    scripts = re.findall(r"<script>([\s\S]*?)</script>", html)
    assert scripts, "页内自包含脚本在场"
    for body in scripts:
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
            fh.write(body)
            name = fh.name
        try:
            proc = subprocess.run(["node", "--check", name], capture_output=True, text=True, check=False)
        finally:
            os.unlink(name)
        assert proc.returncode == 0, f"包页脚本语法非法: {proc.stderr[:400]}"


def test_factory_page_consumes_existing_payloads_only() -> None:
    """示范页定位（刀4 改钉，台账归因：原「不承担开演表单」定位随「工作区玩法页
    全 webview 化」裁定退役——setup 阶段页内自持）：游玩面只消费既有载荷形态
    （game/config、game/state、game/turn）；setup 面只消费既有清点/卡库/开演
    通道（data/packs、data/cards、mode.session），零新 game 端点；init_ui 声明
    渲染件（路线单选/词条多选）与 rp_init 组装（rp-core 单源）在页内。"""
    html = (_PACKS_DIR / "stardew_page.html").read_text(encoding="utf-8")
    for token in (
        "/ext/mode_roleplay/game/config",
        "/ext/mode_roleplay/game/state",
        "/ext/mode_roleplay/game/turn",
        "/ext/mode_roleplay/data/packs",
        "/ext/mode_roleplay/data/cards",
        "mode.session",
        "rpInitSnapshot(rpInitPairs(",
        "wizard_pack_ids",
        "wizard_profile",
        'data-testid="play-route-opt"',
        'data-testid="play-trait-opt"',
        "__RP_PAGE_SESSION__",
        "__RP_PAGE_PACK__",
        "rp:session-bound",
    ):
        assert token in html, f"示范页缺既有面消费锚: {token}"
    assert "/ext/mode_roleplay/game/page" not in html, "零新 game 端点（不消费页装配通道自身）"
    assert "/ext/mode_roleplay/data/sessions" not in html, "继续上局归面板/宿主（页内不重复会话清单面）"


def test_factory_page_theme_scope_and_mount_hygiene() -> None:
    """包主题作用域与挂载卫生（遗留②根修，浏览器复现证据见台账§十六）：
    主题作用域根类唯一声明位 = 页面 <body>（面板/game_host 的 document 注入经
    template 解析会剥 body 标签属性，挂载方自页面原文提取补挂容器——页脚本对
    document.body 的类写入污染宿主且作用域永不成立，禁用）；setup 显隐按
    #pg-setup.pg-live 兄弟选择器（作用域类落容器后仍成立，不依赖 body 类）；
    页内轮询带代际守卫（重挂载后旧实例 interval 自清，旧会话状态不渗入新页面
    DOM、轮询请求不随重挂载次数叠加）。"""
    page = (_PACKS_DIR / "stardew_page.html").read_text(encoding="utf-8")
    css = (_PACKS_DIR / "stardew_theme.css").read_text(encoding="utf-8")
    assert 'class="rp-theme-stardew' in page, "作用域根类在页面 body（唯一声明位）"
    assert "document.body.classList" not in page, "页脚本零 body 类写入"
    assert "__RP_PG_EPOCH__" in page, "轮询代际守卫（旧 interval 自清）"
    assert "pg-setup-live" not in page, "setup 显隐不依赖 body 类（body 属性注入面必丢）"
    assert ".pg-setup.pg-live ~" in css, "setup 期收起状态/日志/输入=兄弟选择器"
    assert "pg-setup-live" not in css


# ── 防线分支补测（路径安全闸 / 读取失败 / 归一边角 / 端点面） ─────────────────


def test_pack_ui_traversal_path_treated_as_missing(rp: Any) -> None:
    """路径安全闸：越出包目录的相对路径（../../ 穿越形态）= 声明无效剔除；
    realpath 归一后仍在包内的凌乱相对路径合法（闸按 containment 不按字面）。"""
    _user_pack(rp, "traversal_pack", {
        "ui": {"theme_css": "../../escape.css", "page": "sub/../page.html"},
    }, files={"page.html": "<html></html>"})
    item = next(p for p in _body_json(_call(rp, _PACKS_ROUTE))["packs"]
                if p["id"] == "traversal_pack")
    assert "theme_css" not in item["ui"], "越界路径剔除"
    assert item["ui"]["page"] == "sub/../page.html", "包内凌乱相对路径合法"
    assert item["has_page"] is True


def test_pack_theme_css_read_failure_content_dropped(rp: Any, monkeypatch: pytest.MonkeyPatch,
                                                     caplog: pytest.LogCaptureFixture) -> None:
    """主题文件在 listing 判存后读取失败 = 内容缺省 + warning（声明保留路径键）。"""
    import builtins

    _user_pack(rp, "unreadable_theme", {
        "ui": {"icon": "🎨", "theme_css": "locked.css"},
    }, files={"locked.css": "body{}"})
    real_open = builtins.open

    def guarded_open(file, *args, **kwargs):
        if isinstance(file, (str, Path)) and str(file).endswith("locked.css"):
            raise OSError("模拟读取失败")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    item = next(p for p in _body_json(_call(rp, _PACKS_ROUTE))["packs"]
                if p["id"] == "unreadable_theme")
    assert "theme_css_text" not in item["ui"]
    assert item["ui"]["theme_css"] == "locked.css", "路径声明保留（内容缺省不剔除）"
    assert any("读取失败" in r.getMessage() for r in caplog.records)


def test_packs_init_ui_pool_shape_edges(rp: Any) -> None:
    """trait_pools 池级边角：非映射池剔除、缺 label 池剔除、重复 id 池保首个。"""
    _user_pack(rp, "pool_edges", {
        "init_ui": {"trait_pools": [
            "坏池",
            {"id": "no_label"},
            {"id": "dup", "label": "首池", "options": [{"id": "a", "label": "甲"}]},
            {"id": "dup", "label": "次池", "options": [{"id": "b", "label": "乙"}]},
        ]},
    })
    item = next(p for p in _body_json(_call(rp, _PACKS_ROUTE))["packs"]
                if p["id"] == "pool_edges")
    assert item["init_ui"]["trait_pools"] == [
        {"id": "dup", "label": "首池", "options": [{"id": "a", "label": "甲"}]}
    ]


def test_combos_save_user_root_unavailable_error(rp: Any, patch_shared: Any) -> None:
    """用户空间不可得 = 写面如实报错不静默假成功。"""
    _user_card(rp, "card_hero", {"name": "侠客"})
    patch_shared("_user_combos_dir", lambda: None)  # 消费点在 rp_* 自持绑定
    out = _body_json(_call(rp, f"{_EP}/data/combos/save", "POST", json.dumps(
        {"name": "无处落", "card_id": "card_hero"}, ensure_ascii=False)))
    assert "error" in out
    assert "无处落盘" in out["error"]


def test_combos_delete_missing_and_invalid_id(rp: Any) -> None:
    no_id = _body_json(_call(rp, f"{_EP}/data/combos/delete", "POST", json.dumps({})))
    assert "error" in no_id
    assert "缺少 id" in no_id["error"]
    bad = _body_json(_call(rp, f"{_EP}/data/combos/delete", "POST", json.dumps({"id": "越权/id"})))
    assert "error" in bad
    assert "非法 id" in bad["error"]


def test_game_page_headless_and_unclosed_head_theme_injection(rp: Any) -> None:
    """_inject_page_theme 边角：无 <head> 页（style 置顶）与 <head 未闭合页（style
    置顶 fallback）。"""
    html = "<html><body>无头页</body></html>"
    assert rp._inject_page_theme(html, "a{}").startswith("<style>"), "无 head = style 前置"
    unclosed = "<html><head"
    assert rp._inject_page_theme(unclosed, "a{}").startswith("<style>"),         "head 后无 > = 前置兜底"
    malformed = "<html><head<meta charset='utf-8'><body>x</body></html>"
    deformed = rp._inject_page_theme(malformed, "a{}")
    assert deformed.index("<style>") < deformed.index("<body"),         "畸形 head 段 = 尽力注入（首个 > 后）"
    closed = "<html><head><title>t</title></head><body>b</body></html>"
    injected = rp._inject_page_theme(closed, "a{}")
    assert injected.index("<style>") < injected.index("<title>"), "闭合 head = 紧随其后"


def test_game_page_post_method_404(rp: Any) -> None:
    out = _body_json(_call(rp, _PAGE_ROUTE, method="POST", raw_body="{}"))
    assert out == {"error": "method not allowed"}


def test_game_page_unlocatable_page_file_falls_back(rp: Any, patch_shared: Any,
                                                    caplog: pytest.LogCaptureFixture) -> None:
    """判存与取读间文件定位失效（防线分支）= OSError → page_html null + warning。"""
    _user_card(rp, "card_hero", {"name": "农场主", "pack_ids": ["stardew_social"]})
    real = rp._pack_rel_path_in_dir
    calls = {"n": 0}

    def flaky(pack_dir: str | None, rel: str) -> str | None:
        calls["n"] += 1
        if calls["n"] == 3:  # 清点两次判存（theme_css/page）后服务期取读失效
            return None
        return real(pack_dir, rel)

    patch_shared("_pack_rel_path_in_dir", flaky)  # 消费点在 rp_content 自持绑定
    out = _body_json(_call(rp, _PAGE_ROUTE, query={"session_id": "pipe-1"}))
    assert out["page_html"] is None
    assert any("装配失败" in r.getMessage() for r in caplog.records)


def test_pack_rel_path_guard_empty_inputs(rp: Any) -> None:
    """路径安全闸空入参：包目录不可得 / rel 空 = None（防线分支直测）。"""
    assert rp._pack_rel_path_in_dir(None, "page.html") is None
    assert rp._pack_rel_path_in_dir(str(_PACKS_DIR), "") is None
    assert rp._pack_rel_path_in_dir(str(_PACKS_DIR), "  ") is None


def test_game_page_non_mode_agent_and_unknown_card_null(rp: Any) -> None:
    """会话行非本模式卡键 / 卡不可得 = page_html null（200 非 404，兼容态）。"""
    rp._DECL_TEST_ROWS = [
        {"pipeline_id": "pipe-1", "mode": "roleplay", "agent_id": "agentos"},
        {"pipeline_id": "pipe-2", "mode": "roleplay", "agent_id": "mode_roleplay/ghost_card"},
    ]
    for sid in ("pipe-1", "pipe-2"):
        out = _body_json(_call(rp, _PAGE_ROUTE, query={"session_id": sid}))
        assert out == {"session_id": sid, "page_html": None}
