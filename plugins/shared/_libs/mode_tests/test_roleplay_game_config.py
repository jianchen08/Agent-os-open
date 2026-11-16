# @feature: FP-0.2.〇 RP批4-Ⅱ-核 声明聚合通道 | @vision: V1 可进化 | @ci: python-coverage
"""游戏域声明聚合通道断言（RP 批 4-Ⅱ-核，GET /game/config）。

- 会话绑定卡+pack 的 Tier 1 声明（rules / narrative_graph / actions）聚合进向
  宿主透出的载荷：session → 行 agent_id（mode_roleplay/<card_id> 卡键）→ 卡 →
  pack_ids 双根加载 → 聚合（pack 声明序后者逐键覆盖 → 卡覆盖 pack；actions 拼
  接同 id 卡胜；逐条带 target 缺省 engine——与装载侧 lint_actions 同口径）；
- 无声明 = 键缺席（宿主引擎件空转 = Tier 0 兼容态锁定：载荷零新增键）；
- 兼容态：无 Tier 1 声明卡 = /game/config 只回 {session_id, card_id}（或仅
  {session_id}），bootstrap 等既有端点零触碰（批 4-Ⅰ 全部既有测试零改动全绿）。
"""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import sys
from typing import Any

import pytest
import yaml

MODES_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_PANEL_EP = "/ext/mode_roleplay"
_CONFIG = f"{_PANEL_EP}/game/config"

pytestmark = pytest.mark.unit


def _load_server() -> Any:
    """按唯一模块名装载 mode_roleplay server.py（防与其它模式测试模块互覆）。"""
    path = os.path.join(MODES_DIR, "mode_roleplay", "server.py")
    spec = importlib.util.spec_from_file_location("mode_game_config_roleplay", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_yaml(path: str, data: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, allow_unicode=True)


@pytest.fixture
def rp(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """server 模块 + 用户空间指向 tmp + pipeline-state provider 注入（会话行）。"""
    user_root = tmp_path / "user_config"
    monkeypatch.setenv("AGENTOS_USER_CONFIG_DIR", str(user_root))
    module = _load_server()
    module.reset_providers()

    async def _state_rows(**_kwargs: Any) -> list[dict[str, Any]]:
        return list(module._CONFIG_TEST_ROWS)  # 测试注入的会话行

    module._set_provider("pipeline-state", _state_rows)
    module._CONFIG_TEST_ROWS = [
        {"pipeline_id": "pipe-1", "thread_id": "thread-1", "mode": "roleplay",
         "agent_id": "mode_roleplay/card_hero", "workspace": str(tmp_path / "ws")}
    ]
    return module


def _get(module: Any, query: dict[str, str] | None = None) -> dict[str, Any]:
    return asyncio.run(
        module.http_handle(path=_CONFIG, method="GET", query=query)
    )


def _payload(result: dict[str, Any]) -> dict[str, Any]:
    assert result["success"] is True
    data = result["data"]
    assert data["body_encoding"] == "base64"
    return json.loads(base64.b64decode(data["body"]).decode("utf-8"))


def _status_of(result: dict[str, Any]) -> int:
    return int(result["data"]["status"])


def _user_card(module: Any, card_id: str, data: dict[str, Any]) -> None:
    """写用户层卡（用户层同 id 接管出厂，读面经 _load_cards 合并）。"""
    _write_yaml(
        os.path.join(str(module._user_cards_dir()), f"{card_id}.yaml"), data
    )


# ── 声明透出 ──────────────────────────────────────────────────────────────────


def test_config_exposes_card_tier1_declarations(rp: Any) -> None:
    """卡 extensions 的 rules/narrative_graph/actions 聚合透出；actions 逐条带 target。"""
    _user_card(rp, "card_hero", {
        "name": "侠客",
        "extensions": {
            "rules": {"combat": {"formula": "atk * (1 + rand(-0.1,0.1)) - def * 0.5"}},
            "narrative_graph": {"nodes": [{"id": "intro"}]},
            "actions": [
                {"id": "hit_npc", "handler": "damage"},
                {"id": "send_to_game", "target": "game"},
            ],
        },
    })

    out = _payload(_get(rp, {"session_id": "pipe-1"}))

    assert out["session_id"] == "pipe-1"
    assert out["card_id"] == "card_hero"
    assert out["rules"]["combat"]["formula"].startswith("atk")
    assert out["narrative_graph"]["nodes"] == [{"id": "intro"}]
    actions = {a["id"]: a for a in out["actions"]}
    assert actions["hit_npc"]["target"] == "engine", "缺省 target 补 engine（2-Ⅱ 同口径）"
    assert actions["send_to_game"]["target"] == "game", "显式 target 原样保留"


def test_config_pack_contributions_merged_card_wins(rp: Any) -> None:
    """pack 的 rules/graph/actions 并入（声明序后者覆盖，卡覆盖 pack）；graph=卡侧
    narrative_graph 的包内键名（rp_packs _DEFERRED_PACK_KEYS 契约名）。"""
    _write_yaml(os.path.join(str(rp._user_packs_dir()), "xianxia.yaml"), {
        "variables": {"hp": {"type": "int"}},
        "rules": {"combat": {"formula": "pack 版"}, "random_events": {"pool": []}},
        "graph": {"nodes": [{"id": "pack_node"}]},
        "actions": [{"id": "buy_pill", "target": "engine", "handler": "buy"}],
    })
    _user_card(rp, "card_hero", {
        "name": "侠客",
        "pack_ids": ["xianxia"],
        "extensions": {
            "rules": {"combat": {"formula": "卡版公式"}},
            "actions": [{"id": "hit_npc", "handler": "damage"}],
        },
    })

    out = _payload(_get(rp, {"session_id": "pipe-1"}))

    assert out["rules"]["combat"]["formula"] == "卡版公式", "卡覆盖 pack 同键"
    assert out["rules"]["random_events"] == {"pool": []}, "pack 独有键并入"
    assert out["narrative_graph"]["nodes"] == [{"id": "pack_node"}], "pack graph → narrative_graph"
    ids = [a["id"] for a in out["actions"]]
    assert ids == ["buy_pill", "hit_npc"], "pack actions 前置、卡 actions 追加"


def test_config_actions_dedupe_card_wins(rp: Any) -> None:
    """pack 与卡同 id action：卡版胜（后者覆盖），不双列。"""
    _write_yaml(os.path.join(str(rp._user_packs_dir()), "pkg.yaml"), {
        "actions": [{"id": "give", "handler": "give", "params": {"item": "包产物"}}],
    })
    _user_card(rp, "card_hero", {
        "pack_ids": ["pkg"],
        "extensions": {"actions": [{"id": "give", "handler": "give", "params": {"item": "卡产物"}}]},
    })

    out = _payload(_get(rp, {"session_id": "pipe-1"}))

    assert out["actions"] == [{"id": "give", "handler": "give", "params": {"item": "卡产物"}, "target": "engine"}]


def test_config_multi_pack_later_overrides_earlier(rp: Any) -> None:
    """多 pack 声明序后者逐键覆盖（与 rp_packs Tier 0 合并语义同构）。"""
    _write_yaml(os.path.join(str(rp._user_packs_dir()), "base.yaml"), {
        "rules": {"combat": {"formula": "base"}},
    })
    _write_yaml(os.path.join(str(rp._user_packs_dir()), "patch.yaml"), {
        "rules": {"combat": {"formula": "patch"}},
    })
    _user_card(rp, "card_hero", {"pack_ids": ["base", "patch"]})

    out = _payload(_get(rp, {"session_id": "pipe-1"}))

    assert out["rules"]["combat"]["formula"] == "patch"


# ── 兼容态：无声明 = 键缺席（Tier 0 锁定）──────────────────────────────────────


def test_config_no_declarations_keys_absent(rp: Any) -> None:
    """无 Tier 1 声明卡 = 载荷只回 {session_id, card_id}（键缺席，引擎件空转）。"""
    _user_card(rp, "card_hero", {"name": "朴素卡"})

    out = _payload(_get(rp, {"session_id": "pipe-1"}))

    assert out == {"session_id": "pipe-1", "card_id": "card_hero"}


def test_config_unknown_session_keys_absent(rp: Any) -> None:
    """未知会话 = 只回 {session_id}（无卡键）。"""
    out = _payload(_get(rp, {"session_id": "ghost"}))
    assert out == {"session_id": "ghost"}


def test_config_card_unresolvable_keys_absent(rp: Any) -> None:
    """agent_id 指向不存在的卡 = 只回 {session_id}（不假爆不 fallback）。"""
    rp._CONFIG_TEST_ROWS[0]["agent_id"] = "mode_roleplay/card_ghost"
    out = _payload(_get(rp, {"session_id": "pipe-1"}))
    assert out == {"session_id": "pipe-1"}


def test_config_non_mode_agent_id_keys_absent(rp: Any) -> None:
    """agent_id 非本模式卡键（无 mode_roleplay/ 前缀）= 只回 {session_id}。"""
    rp._CONFIG_TEST_ROWS[0]["agent_id"] = "executor/general_agent"
    out = _payload(_get(rp, {"session_id": "pipe-1"}))
    assert out == {"session_id": "pipe-1"}


def test_config_provider_down_degrades_keys_absent(rp: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """provider 未注入/失败（读面降级空行）= 只回 {session_id}，不崩。"""
    rp.reset_providers()
    out = _payload(_get(rp, {"session_id": "pipe-1"}))
    assert out == {"session_id": "pipe-1"}


@pytest.mark.parametrize(
    ("query", "fragment"),
    [
        (None, "session_id"),
        ({"session_id": ""}, "session_id"),
        ({"session_id": "坏 id/斜杠"}, "非法 session_id"),
    ],
)
def test_config_failclosed_invalid_session(rp: Any, query: dict[str, str], fragment: str) -> None:
    """缺参/形态非法 = 400 可读错误（与通道端点同闸）。"""
    result = _get(rp, query)

    assert _status_of(result) == 400
    assert fragment in _payload(result)["error"]


def test_config_post_method_404(rp: Any) -> None:
    result = asyncio.run(
        rp.http_handle(path=_CONFIG, method="POST", raw_body="{}")
    )
    assert _status_of(result) == 404


# ── fail-soft 读面：坏 pack 资产跳过不阻断 ────────────────────────────────────


def test_config_missing_pack_id_skipped_card_still_aggregated(rp: Any) -> None:
    """pack 两根皆缺 = 该 pack 跳过；卡内声明照常聚合。"""
    _user_card(rp, "card_hero", {
        "pack_ids": ["不存在的包"],
        "extensions": {"rules": {"combat": {"formula": "卡版"}}},
    })

    out = _payload(_get(rp, {"session_id": "pipe-1"}))

    assert out["rules"]["combat"]["formula"] == "卡版"
    assert "narrative_graph" not in out
    assert "actions" not in out


def test_config_broken_pack_yaml_skipped(rp: Any) -> None:
    """用户层 pack yaml 损坏（解析失败/顶层非映射）= 跳过不阻断（读面 fail-soft）。"""
    broken_dir = str(rp._user_packs_dir())
    os.makedirs(broken_dir, exist_ok=True)
    with open(os.path.join(broken_dir, "broken.yaml"), "w", encoding="utf-8") as fh:
        fh.write("{{{ 这不是 yaml")
    _write_yaml(os.path.join(broken_dir, "nonmap.yaml"), ["顶层是列表"])
    _user_card(rp, "card_hero", {
        "pack_ids": ["broken", "nonmap"],
        "extensions": {"actions": [{"id": "hit_npc"}]},
    })

    out = _payload(_get(rp, {"session_id": "pipe-1"}))

    assert [a["id"] for a in out["actions"]] == ["hit_npc"]
    assert "rules" not in out
    assert "narrative_graph" not in out


def test_config_actions_malformed_entries_dropped(rp: Any) -> None:
    """非映射条目/无 id 条目剔除（宿主白名单以 id 为键）；id 空白剔除。"""
    _user_card(rp, "card_hero", {
        "extensions": {
            "actions": [
                "字符串条目",
                {"handler": "give"},
                {"id": "  ", "handler": "give"},
                {"id": "ok_action", "handler": "give"},
            ],
        },
    })

    out = _payload(_get(rp, {"session_id": "pipe-1"}))

    assert out["actions"] == [{"id": "ok_action", "handler": "give", "target": "engine"}]


def test_config_builtin_pack_used_when_user_missing(rp: Any, tmp_path: Any) -> None:
    """用户层无此 pack → 回落包内 packs/（出厂根只读消费，双根同构）。"""
    builtin_pack = os.path.join(MODES_DIR, "mode_roleplay", "packs", "builtin_probe.yaml")
    _write_yaml(builtin_pack, {"rules": {"combat": {"formula": "出厂"}}})
    try:
        _user_card(rp, "card_hero", {"pack_ids": ["builtin_probe"]})
        out = _payload(_get(rp, {"session_id": "pipe-1"}))
        assert out["rules"]["combat"]["formula"] == "出厂"
    finally:
        os.remove(builtin_pack)


def test_manifest_declares_game_config_route() -> None:
    """manifest 面：/game/config GET 端点声明在场（零新增插件，进既有 http.handle）。"""
    manifest_path = os.path.join(MODES_DIR, "mode_roleplay", "plugin.json")
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    routes = {r["path"]: (r["method"], r["auth"]) for r in manifest["http_endpoints"]}
    method, auth = routes[_CONFIG]
    assert method == "GET"
    assert auth == "user"


# ── template/ui 声明透出（RP 批 4-Ⅲ：_DEFERRED_PACK_KEYS 契约键启用消费）────────


def test_config_exposes_template_and_ui_declarations(rp: Any) -> None:
    """卡 extensions 的 template/ui 聚合透出；仅声明 template/ui 时 rules/graph/
    actions 键缺席（键缺席式聚合——无某面声明零新增该键）。"""
    _user_card(rp, "card_hero", {
        "name": "侠客",
        "extensions": {
            "template": {
                "village": {"text": "村口 {{gold}}"},
                "well": {"trigger": "state.hp < 30", "text": "井边"},
            },
            "ui": {
                "status_bar_template": "金币 {{gold}}",
                "bg_image": "https://example.com/bg.png",
                "bgm": "/media/theme.mp3",
            },
        },
    })

    out = _payload(_get(rp, {"session_id": "pipe-1"}))

    assert out["template"] == {
        "village": {"text": "村口 {{gold}}"},
        "well": {"trigger": "state.hp < 30", "text": "井边"},
    }
    assert out["ui"]["status_bar_template"] == "金币 {{gold}}"
    assert out["ui"]["bgm"] == "/media/theme.mp3"
    assert "rules" not in out
    assert "narrative_graph" not in out
    assert "actions" not in out


def test_config_pack_template_ui_merged_card_wins(rp: Any) -> None:
    """pack 的 template/ui（_DEFERRED_PACK_KEYS 契约名）并入：声明序后者逐键覆盖 →
    卡逐键覆盖 pack（与 rules/graph 同构浅合并）。"""
    _write_yaml(os.path.join(str(rp._user_packs_dir()), "pack_a.yaml"), {
        "template": {"village": {"text": "pack 版村口"}, "shop": {"text": "pack 商店"}},
        "ui": {"bgm": "/pack.mp3", "bg_image": "https://pack.example/bg.png"},
    })
    _write_yaml(os.path.join(str(rp._user_packs_dir()), "pack_b.yaml"), {
        "template": {"shop": {"text": "后者商店"}},
    })
    _user_card(rp, "card_hero", {
        "name": "侠客",
        "pack_ids": ["pack_a", "pack_b"],
        "extensions": {
            "template": {"village": {"text": "卡版村口"}},
            "ui": {"bgm": "/card.mp3"},
        },
    })

    out = _payload(_get(rp, {"session_id": "pipe-1"}))

    assert out["template"]["village"] == {"text": "卡版村口"}, "卡覆盖 pack 同键"
    assert out["template"]["shop"] == {"text": "后者商店"}, "多 pack 声明序后者覆盖"
    assert out["ui"]["bgm"] == "/card.mp3", "卡覆盖 pack ui 同键"
    assert out["ui"]["bg_image"] == "https://pack.example/bg.png", "pack 独有 ui 键并入"


def test_config_template_ui_malformed_ignored_keys_absent(rp: Any) -> None:
    """template/ui 非映射（声明畸形）= 忽略不进载荷（读面 fail-soft，不崩）。"""
    _write_yaml(os.path.join(str(rp._user_packs_dir()), "pack_bad.yaml"), {
        "template": ["非映射形态"],
        "ui": "非映射形态",
    })
    _user_card(rp, "card_hero", {
        "name": "侠客",
        "pack_ids": ["pack_bad"],
        "extensions": {"template": 42, "ui": ["也是非映射"]},
    })

    out = _payload(_get(rp, {"session_id": "pipe-1"}))

    assert out == {"session_id": "pipe-1", "card_id": "card_hero"}


# ── choices 增透（RP 面板 v3.1 刀6：对话流 choices 卡数据通道，只增键）──────────


def test_config_exposes_pack_choices(rp: Any) -> None:
    """pack choices 聚合透出：text/category/hint 归一载荷（text 剥首尾空白，
    category/hint 缺席 = 键缺席；对话流 chips 数据源）。"""
    _write_yaml(os.path.join(str(rp._user_packs_dir()), "ch_pack.yaml"), {
        "choices": [
            {"text": " 出剑迎敌 ", "category": "攻击",
             "hint": "叙述命中「剑气斩」类词面时由行为触发结算"},
            {"text": "调息回气"},
        ],
    })
    _user_card(rp, "card_hero", {"name": "侠客", "pack_ids": ["ch_pack"]})

    out = _payload(_get(rp, {"session_id": "pipe-1"}))

    assert out["choices"] == [
        {"text": "出剑迎敌", "category": "攻击",
         "hint": "叙述命中「剑气斩」类词面时由行为触发结算"},
        {"text": "调息回气"},
    ]


def test_config_choices_multi_pack_later_nonempty_wins(rp: Any) -> None:
    """多 pack 声明序后者非空清单整体覆盖（pack yaml 头「同键合并=后到覆盖」
    同构）；后者空清单/全坏条目 = 不覆盖前者（映射面 sink.update({}) 无操作
    同构——空贡献不清场）。"""
    _write_yaml(os.path.join(str(rp._user_packs_dir()), "ch_base.yaml"), {
        "choices": [{"text": "基础选项", "category": "基础"}],
    })
    _write_yaml(os.path.join(str(rp._user_packs_dir()), "ch_patch.yaml"), {
        "choices": [{"text": "补丁选项"}],
    })
    _write_yaml(os.path.join(str(rp._user_packs_dir()), "ch_empty.yaml"), {
        "choices": [],
    })
    _user_card(rp, "card_hero", {"name": "侠客",
                                 "pack_ids": ["ch_base", "ch_patch", "ch_empty"]})

    out = _payload(_get(rp, {"session_id": "pipe-1"}))

    assert out["choices"] == [{"text": "补丁选项"}]


def test_config_choices_malformed_entries_dropped(rp: Any) -> None:
    """非映射条目/text 非字符串或空白剔除（归一剔除面）；全坏 = 键缺席。"""
    _write_yaml(os.path.join(str(rp._user_packs_dir()), "ch_bad.yaml"), {
        "choices": [
            "字符串条目",
            {"category": "无正文"},
            {"text": "   "},
            {"text": 42, "category": "攻击"},
            {"text": "可用选项", "category": 7, "hint": {"nested": True}},
        ],
    })
    _user_card(rp, "card_hero", {"name": "侠客", "pack_ids": ["ch_bad"]})

    out = _payload(_get(rp, {"session_id": "pipe-1"}))

    assert out["choices"] == [{"text": "可用选项"}]

    _write_yaml(os.path.join(str(rp._user_packs_dir()), "ch_all_bad.yaml"), {
        "choices": [{"category": "无正文"}, "再一条"],
    })
    _user_card(rp, "card_hero", {"name": "侠客", "pack_ids": ["ch_all_bad"]})
    out = _payload(_get(rp, {"session_id": "pipe-1"}))
    assert "choices" not in out, "全坏条目 = 无声明同型（键缺席）"


def test_config_choices_no_declaration_keys_absent(rp: Any) -> None:
    """无 choices 声明包 = 键缺席（兼容态对照：既有 /game/config 载荷零新增键，
    test_config_no_declarations_keys_absent 精确等值断言同门执法）。"""
    _write_yaml(os.path.join(str(rp._user_packs_dir()), "plain_pack.yaml"), {
        "variables": {"hp": {"type": "int"}},
    })
    _user_card(rp, "card_hero", {"name": "侠客", "pack_ids": ["plain_pack"]})

    out = _payload(_get(rp, {"session_id": "pipe-1"}))

    assert "choices" not in out
