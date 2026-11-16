# @feature: FP-0.2.〇 RP引擎(状态通道schema化) | @vision: V1 可进化 | @ci: python-coverage
"""agent_config_load 装载 RP 玩法包 pack / behaviors / actions target 测试
（RP 批 2-Ⅱ 2c+ / 2c / 2d）。

契约：
- pack（FR-B6 Tier 0）：卡顶层 pack_ids 声明序双根加载（用户层
  <user_config_dir>/packs 同 id 接管 → 回落包内 <mode_pkg>/packs）；Tier 0 面
  variables/behaviors 并入装载（多 pack 后者逐键覆盖、卡覆盖 pack）；graph/
  template/ui 忽略；两根皆缺 = 零并入；无 pack_ids = 兼容态零变化；
- behaviors：卡 extensions.behaviors → context.rp_state_behaviors；
- actions target：合法值 {engine, game}、缺省 engine、非法剔除 warning（本刀
  仅装载校验，零消费方——下发随批 4/5）。

env 隔离：AGENTOS_USER_ROOT / AGENTOS_CONFIG_ROOT 钉到 tmp（与 rp_state_schema
装载测试同契约）；包内 pack 经用户插件副本根 staged（find_mode_package_dir
双根解析的既定机制，mode_keys 契约测试管辖）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parent
_SHARED = _DIR.parents[0]  # plugins/shared/

for _d in [_DIR, _SHARED]:
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

from _libs.pipeline_base.plugin import PluginContext  # noqa: E402
from _libs.testing.card_service_fake import install_card_service_fake  # noqa: E402


def _load_plugin_module() -> Any:
    mod_name = "agent_config_load_plugin_rp_packs_test"
    module_path = _DIR / "plugin.py"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, str(module_path))
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _ctx(state: dict[str, Any]) -> PluginContext:
    return PluginContext(state=dict(state), config={})


@pytest.fixture
def factory_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """factory 配置根钉到 tmp（用户根/config 根同树隔离）。"""
    factory = tmp_path / "factory"
    factory.mkdir()
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "user-root"))
    monkeypatch.delenv("AGENTOS_USER_CONFIG_DIR", raising=False)
    monkeypatch.delenv("AGENTOS_USER_PLUGINS_DIR", raising=False)
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(factory))
    return tmp_path


_AGENT_ID = "mode_roleplay/card_rp"


def _write_agent(base: Path, agent_yaml: str) -> None:
    """卡落 factory 配置根（模式命名空间路径形态，与出厂卡同构）。"""
    agents = base / "factory" / "agents" / "mode_roleplay"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "card_rp.yaml").write_text(agent_yaml, encoding="utf-8")


def _write_builtin_pack(base: Path, pack_id: str, text: str) -> None:
    """包内 pack：经用户插件副本根 staged（find_mode_package_dir 命中位的包内 packs/）。"""
    pkg = base / "user-root" / "plugins" / "modes" / "mode_roleplay"
    (pkg / "packs").mkdir(parents=True, exist_ok=True)
    (pkg / "plugin.json").write_text("{}", encoding="utf-8")
    (pkg / "packs" / f"{pack_id}.yaml").write_text(text, encoding="utf-8")


def _write_user_pack(base: Path, pack_id: str, text: str) -> None:
    """用户层 pack：<user_config_dir>/packs/<id>.yaml（lorebooks 用户层同模式）。"""
    packs = base / "user-root" / "config" / "packs"
    packs.mkdir(parents=True, exist_ok=True)
    (packs / f"{pack_id}.yaml").write_text(text, encoding="utf-8")


def _run(base: Path, agent_yaml: str) -> dict[str, Any]:
    _write_agent(base, agent_yaml)
    mod = _load_plugin_module()
    plugin = mod.AgentConfigLoadPlugin(config={})
    install_card_service_fake(plugin)  # lint/actions 归一走真实 card_lint 后端替身（T6c）
    result = asyncio.run(plugin.execute(_ctx({"agent.id": _AGENT_ID})))
    return result.state_updates


_PACK_A = (
    "variables:\n"
    "  npc:\n"
    "    苏清婉: {好感度: {type: int, min: -100, max: 100}}\n"
    "  world:\n"
    "    时辰: {type: enum, values: [晨, 午, 夜]}\n"
    "behaviors:\n"
    "  包行为:\n"
    "    trigger: 剑出鞘\n"
    "    effect: {好感度: '-1'}\n"
)


# ── pack 双根加载 ─────────────────────────────────────────────────────────


def test_builtin_pack_loaded_and_merged(factory_root: Path) -> None:
    """包内 pack：variables/behaviors 并入 context.rp_state_schema /
    context.rp_state_behaviors（卡内无声明 = pack 独供）。"""
    _write_builtin_pack(factory_root, "pack_a", _PACK_A)
    updates = _run(factory_root, "display_name: 卡\npack_ids:\n- pack_a\n")

    assert updates["context.rp_state_schema"] == {
        "npc": {"苏清婉": {"好感度": {"type": "int", "min": -100, "max": 100}}},
        "world": {"时辰": {"type": "enum", "values": ["晨", "午", "夜"]}},
    }
    assert updates["context.rp_state_behaviors"] == {
        "包行为": {"trigger": "剑出鞘", "effect": {"好感度": "-1"}},
    }


def test_user_pack_takes_over_builtin_same_id(factory_root: Path) -> None:
    """双根同 id：用户层接管（BuiltinFirst），包内容不参与。"""
    _write_builtin_pack(factory_root, "pack_x", "variables:\n  出厂键: {type: int}\n")
    _write_user_pack(factory_root, "pack_x", "variables:\n  用户键: {type: string}\n")
    updates = _run(factory_root, "pack_ids:\n- pack_x\n")

    assert updates["context.rp_state_schema"] == {"用户键": {"type": "string"}}


def test_both_roots_missing_zero_merge(factory_root: Path) -> None:
    """两根皆缺 = 该 pack 零并入；卡内自有声明不受牵连照常装载。"""
    updates = _run(
        factory_root,
        "extensions:\n  variables:\n    卡键: {type: int}\npack_ids:\n- ghost\n",
    )
    assert updates["context.rp_state_schema"] == {"卡键": {"type": "int"}}
    assert "context.rp_state_behaviors" not in updates


def test_illegal_pack_id_treated_missing(factory_root: Path) -> None:
    """id 形态非法（穿越/分隔符）= 按缺失零并入（路径安全闸）。"""
    updates = _run(factory_root, "pack_ids:\n- '../escape'\n")
    assert "context.rp_state_schema" not in updates


# ── 合并语义：多 pack 序 + 卡覆盖 pack ────────────────────────────────────


def test_card_overrides_pack_same_key(factory_root: Path) -> None:
    """卡覆盖 pack：同键（含嵌套组键 npc）卡值整体生效，pack 独有键保留。"""
    _write_builtin_pack(
        factory_root,
        "pack_a",
        "variables:\n"
        "  npc: {苏清婉: {好感度: {type: int, max: 50}}}\n"
        "  pack独有: {type: bool}\n",
    )
    updates = _run(
        factory_root,
        "pack_ids:\n- pack_a\n"
        "extensions:\n"
        "  variables:\n"
        "    npc: {苏清婉: {好感度: {type: int, max: 100}}}\n",
    )
    assert updates["context.rp_state_schema"] == {
        "npc": {"苏清婉": {"好感度": {"type": "int", "max": 100}}},
        "pack独有": {"type": "bool"},
    }


def test_card_overrides_pack_behavior_same_key(factory_root: Path) -> None:
    """behaviors 同键合并同语义：卡行为覆盖包行为，包独有行为保留。"""
    _write_builtin_pack(
        factory_root,
        "pack_a",
        "behaviors:\n  共键: {trigger: 包, effect: {a: '+1'}}\n  包独有: {trigger: x, effect: {b: '+1'}}\n",
    )
    updates = _run(
        factory_root,
        "pack_ids:\n- pack_a\n"
        "extensions:\n"
        "  behaviors:\n"
        "    共键: {trigger: 卡, effect: {c: '+1'}}\n",
    )
    assert updates["context.rp_state_behaviors"] == {
        "共键": {"trigger": "卡", "effect": {"c": "+1"}},
        "包独有": {"trigger": "x", "effect": {"b": "+1"}},
    }


def test_multi_pack_later_declaration_overrides(factory_root: Path) -> None:
    """多 pack 按声明序后者逐键覆盖（key2 取 pack_b）。"""
    _write_builtin_pack(
        factory_root,
        "pack_a",
        "variables:\n  key1: {type: int}\n  key2: {type: int}\n",
    )
    _write_builtin_pack(factory_root, "pack_b", "variables:\n  key2: {type: string}\n")
    updates = _run(factory_root, "pack_ids:\n- pack_a\n- pack_b\n")

    assert updates["context.rp_state_schema"] == {
        "key1": {"type": "int"},
        "key2": {"type": "string"},
    }


def test_pack_deferred_keys_ignored_and_tier0_merged(factory_root: Path) -> None:
    """graph/template/ui 键忽略（消费随批 4 宿主）；variables/behaviors 照常并入。"""
    _write_builtin_pack(
        factory_root,
        "pack_a",
        "variables:\n  键: {type: int}\n"
        "graph: {nodes: []}\n"
        "template: {开场: x}\n"
        "ui: {面板: y}\n",
    )
    updates = _run(factory_root, "pack_ids:\n- pack_a\n")
    assert updates["context.rp_state_schema"] == {"键": {"type": "int"}}
    assert "context.rp_actions" not in updates


# ── 兼容态（本刀最重验收） ─────────────────────────────────────────────────


def test_no_pack_ids_no_declarations_compat_zero_change(factory_root: Path) -> None:
    """无 pack_ids 无 extensions（出厂三卡形态）：三键均不写 = 现状零变化。"""
    updates = _run(factory_root, "display_name: 朴素卡\n")
    assert "context.rp_state_schema" not in updates
    assert "context.rp_state_behaviors" not in updates
    assert "context.rp_actions" not in updates


def test_card_extensions_without_new_faces_unchanged(factory_root: Path) -> None:
    """卡只有 2-Ⅰ 声明（variables/rules）无 behaviors/actions/pack：2-Ⅰ 行为等价。"""
    yaml_text = (
        "extensions:\n"
        "  variables:\n"
        "    好感度: {type: int, min: 0, max: 100}\n"
        "  rules:\n"
        "    hard_rules:\n"
        "      好感度: {max_delta_per_turn: 10}\n"
    )
    updates = _run(factory_root, yaml_text)
    assert updates["context.rp_state_schema"] == {"好感度": {"type": "int", "min": 0, "max": 100}}
    assert updates["context.rp_state_hard_rules"] == {"好感度": {"max_delta_per_turn": 10}}
    assert "context.rp_state_behaviors" not in updates
    assert "context.rp_actions" not in updates


# ── behaviors / actions 装载 ──────────────────────────────────────────────


def test_card_behaviors_loaded_verbatim(factory_root: Path) -> None:
    updates = _run(
        factory_root,
        "extensions:\n"
        "  behaviors:\n"
        "    怒目:\n"
        "      trigger: 怒目而视\n"
        "      effect: {好感度: '-3'}\n",
    )
    assert updates["context.rp_state_behaviors"] == {
        "怒目": {"trigger": "怒目而视", "effect": {"好感度": "-3"}},
    }


@pytest.mark.parametrize("target", ["engine", "game"])
def test_actions_legal_target_loaded_with_default_fill(
    factory_root: Path, target: str
) -> None:
    """target 合法两值保留；缺省 target 补 engine（显式值原样）。"""
    updates = _run(
        factory_root,
        "extensions:\n"
        "  actions:\n"
        "    - name: 显\n"
        f"      target: {target}\n"
        "    - name: 隐\n",
    )
    assert updates["context.rp_actions"] == [
        {"name": "显", "target": target},
        {"name": "隐", "target": "engine"},
    ]


def test_actions_illegal_target_dropped(factory_root: Path) -> None:
    """target 非法 = 该 action 剔除（fail-soft），合法条目保留。"""
    updates = _run(
        factory_root,
        "extensions:\n"
        "  actions:\n"
        "    - name: 好\n"
        "      target: game\n"
        "    - name: 坏\n"
        "      target: warp\n",
    )
    assert updates["context.rp_actions"] == [{"name": "好", "target": "game"}]


def test_actions_all_illegal_writes_no_key(factory_root: Path) -> None:
    """全部 action 非法 = 剔除后为空 = 不写键（载荷零新增语义）。"""
    updates = _run(
        factory_root,
        "extensions:\n  actions:\n    - name: 坏\n      target: warp\n",
    )
    assert "context.rp_actions" not in updates


# ── card 质检服务降级（T6c 服务化）：失败/无效信封 = lint 面跳过不阻断 ────────


def _run_with_card_caller(
    base: Path, agent_yaml: str, caller: Any, caplog: pytest.LogCaptureFixture
) -> dict[str, Any]:
    _write_agent(base, agent_yaml)
    mod = _load_plugin_module()
    plugin = mod.AgentConfigLoadPlugin(config={})
    plugin.set_card_service_caller(caller)
    with caplog.at_level("WARNING", logger="agent_config_load"):
        result = asyncio.run(plugin.execute(_ctx({"agent.id": _AGENT_ID})))
    return result.state_updates


@pytest.mark.parametrize("caller_name", ["raise", "error_envelope", "non_dict"])
def test_card_service_failure_degrades_load_with_notice(
    factory_root: Path,
    caplog: pytest.LogCaptureFixture,
    caller_name: str,
) -> None:
    """质检服务失败/error 信封/非 dict = lint 面跳过 + 降级提示；装载照常，
    actions 归一不可执行 = 本轮不写 rp_actions（非法条目不下发的安全方向）。"""

    async def _raise(method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("card service down")

    async def _error_envelope(method: str, params: dict[str, Any]) -> dict[str, Any]:
        return {"error": "internal"}

    async def _non_dict(method: str, params: dict[str, Any]) -> Any:
        return "not a dict"

    callers = {"raise": _raise, "error_envelope": _error_envelope, "non_dict": _non_dict}
    agent_yaml = (
        "extensions:\n"
        "  variables:\n"
        "    好感度: {type: int}\n"
        "  actions:\n"
        "    - name: 显\n"
    )
    updates = _run_with_card_caller(factory_root, agent_yaml, callers[caller_name], caplog)
    assert updates["context.rp_state_schema"] == {"好感度": {"type": "int"}}, "装载不被 lint 服务失败阻断"
    assert "context.rp_actions" not in updates, "归一不可执行 = 本轮不写 rp_actions"
    messages = [r.getMessage() for r in caplog.records]
    assert any("card 质检服务" in m and "跳过" in m for m in messages), messages


# ── 降级分支：非模式键仅用户层 / 用户层损坏回落 / 包内损坏上抛 ──────────────


def test_non_mode_key_pack_user_layer_only(factory_root: Path) -> None:
    """非模式键卡（无包根可解析）：pack 仅用户层参与，包内不可达零并入。"""
    _write_builtin_pack(factory_root, "pack_a", "variables:\n  包键: {type: int}\n")
    _write_user_pack(factory_root, "pack_u", "variables:\n  用户键: {type: int}\n")
    _write_agent(factory_root, "pack_ids:\n- pack_a\n- pack_u\n")
    mod = _load_plugin_module()
    plugin = mod.AgentConfigLoadPlugin(config={})
    result = asyncio.run(plugin.execute(_ctx({"agent.id": "card_rp"})))  # 裸键非模式形态

    assert result.state_updates["context.rp_state_schema"] == {"用户键": {"type": "int"}}


def test_builtin_pack_corrupt_raises_seed_damage(factory_root: Path) -> None:
    """包内 pack 损坏（顶层非映射）= 上抛（出厂种子损坏属包事实，不静默当缺席）。"""
    _write_builtin_pack(factory_root, "pack_bad", "- 列表顶层\n")
    _write_agent(factory_root, "pack_ids:\n- pack_bad\n")
    mod = _load_plugin_module()
    plugin = mod.AgentConfigLoadPlugin(config={})
    with pytest.raises(ValueError, match="pack 顶层须为映射"):
        asyncio.run(plugin.execute(_ctx({"agent.id": _AGENT_ID})))


def test_user_pack_corrupt_falls_back_builtin(factory_root: Path) -> None:
    """用户层 pack 损坏 = warning 回落包内（用户层可变面坏件不当缺席也不当致命）。"""
    _write_builtin_pack(factory_root, "pack_x", "variables:\n  包内键: {type: int}\n")
    _write_user_pack(factory_root, "pack_x", "- 列表顶层\n")
    updates = _run(factory_root, "pack_ids:\n- pack_x\n")
    assert updates["context.rp_state_schema"] == {"包内键": {"type": "int"}}


# ── merge_state_decl 纯函数面（防御分支直测） ─────────────────────────────


def test_merge_state_decl_non_dict_pack_entry_skipped() -> None:
    """packs 内非映射条目 = 跳过 + warning（防御直测：load_packs 产出恒映射）。"""
    from rp_packs import merge_state_decl

    variables, behaviors, warnings = merge_state_decl(
        {"卡键": {"type": "int"}}, None, ["垃圾条目"]
    )
    assert variables == {"卡键": {"type": "int"}}
    assert behaviors is None
    assert len(warnings) == 1


def test_merge_state_decl_non_dict_face_warns_and_skips() -> None:
    """pack variables/behaviors 非映射 = 该面跳过 + warning，其余面照常并入。"""
    from rp_packs import merge_state_decl

    variables, behaviors, warnings = merge_state_decl(
        None, None, [{"variables": "垃圾", "behaviors": {"行为": {"trigger": "x", "effect": {}}}}]
    )
    assert variables is None
    assert behaviors == {"行为": {"trigger": "x", "effect": {}}}
    assert len(warnings) == 1


def test_user_packs_dir_resolves_under_user_root(
    factory_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_user_packs_dir：<user_config_dir>/packs；共享根不在 path 时自插（引导对齐）。"""
    import user_space  # noqa: PLC0415 — 预热缓存，路径剥离后导入仍可达

    assert user_space is not None
    shared_resolved = str(_SHARED.resolve())
    stripped = [
        p for p in sys.path
        if p and str(Path(p).resolve()) != shared_resolved
    ]
    monkeypatch.setattr(sys, "path", stripped)
    mod = _load_plugin_module()
    resolved = mod._user_packs_dir()
    assert resolved == str(factory_root / "user-root" / "config" / "packs")


def test_user_packs_dir_unresolvable_degrades_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """user_space 解析失败（用户层不可得）= None 降级，不阻断装载。"""
    import user_space

    def _boom() -> None:
        raise RuntimeError("用户配置层解析失败")

    monkeypatch.setattr(user_space, "user_config_dir", _boom)
    mod = _load_plugin_module()
    assert mod._user_packs_dir() is None
