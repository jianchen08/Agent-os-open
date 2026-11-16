# @feature: FP-0.2.〇 RP面板v3.1刀1 | @vision: V1 可进化 | @ci: python-coverage
"""agent_config_load RP 面板 v3.1 刀1 装载侧测试：rp_init 快照解析 + pack
呈现面声明 warning 面。

契约（RP_INIT_KEY 常量注释单源）：
- execution_context.rp_init（k=v 对逗号串，rp_axes 同构）解析归一为
  context.rp_init_effects；段内按第一个 = 切分（v 可含等号，如绝对 op
  ``=重伤``）；值内 ``\\,``→逗号、``\\\\``→反斜杠反转义，其余 ``\\x`` 保留
  原字；坏段（缺 k / 缺 =）warning 跳过；
- 键缺席/非字符串/纯空白/全坏段 = 不写键（兼容态零变化）；
- pack ui/init_ui 声明不入装载 state（零 context.* 新键），只做 card 质检服务
  （card.lint_pack_ui，真实 card_lint 后端替身）同源结构 warning 面（caplog 可观测）。

env 隔离与包内 pack staging 同 test_agent_config_load_rp_wizard。
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
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
    mod_name = "agent_config_load_plugin_rp_panel_v31_test"
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


def _run_with_state(state: dict[str, Any]) -> dict[str, Any]:
    mod = _load_plugin_module()
    plugin = mod.AgentConfigLoadPlugin(config={})
    install_card_service_fake(plugin)  # lint 面走真实 card_lint 后端替身（T6c）
    result = asyncio.run(plugin.execute(_ctx(state)))
    return result.state_updates


# ── rp_init 快照解析 → context.rp_init_effects ───────────────────────────────


def test_rp_init_parse_basic_pairs_preserve_order() -> None:
    updates = _run_with_state({
        "agent.id": "agentos",
        "execution_context": {"rp_init": "npc.阿比.好感度=+5,player.伤势==重伤"},
    })
    effects = updates["context.rp_init_effects"]
    assert list(effects.items()) == [
        ("npc.阿比.好感度", "+5"),
        ("player.伤势", "=重伤"),
    ], "段内按第一个 = 切分（绝对 op 整串归 v）；声明序保持"


def test_rp_init_parse_value_escapes() -> None:
    """值内转义：``\\,``→逗号、``\\\\``→反斜杠、其余 ``\\x`` 保留原字。"""
    updates = _run_with_state({
        "agent.id": "agentos",
        "execution_context": {"rp_init": r"world.铭文=见\,知微,world.路=山\水"},
    })
    effects = updates["context.rp_init_effects"]
    assert effects["world.铭文"] == "见,知微"
    assert effects["world.路"] == r"山\水"


def test_rp_init_bad_segments_skipped_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    """缺 = 段 / 空 k 段 warning 跳过，合法段照常归一。"""
    with caplog.at_level(logging.WARNING, logger="agent_config_load"):
        updates = _run_with_state({
            "agent.id": "agentos",
            "execution_context": {"rp_init": "没有等号,=空键, player.气血=+10 ,"},
        })
    assert updates["context.rp_init_effects"] == {"player.气血": "+10"}
    assert any("rp_init" in r.message for r in caplog.records)


@pytest.mark.parametrize(
    "raw",
    [None, "", "   ", 42],
)
def test_rp_init_absent_or_non_string_no_key(raw: Any) -> None:
    """键缺席/空白/非字符串 = 不写键（兼容态零行为变化）。"""
    ec = None if raw is None else {"rp_init": raw}
    state: dict[str, Any] = {"agent.id": "agentos"}
    if ec is not None:
        state["execution_context"] = ec
    updates = _run_with_state(state)
    assert "context.rp_init_effects" not in updates


def test_rp_init_all_bad_segments_no_key(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="agent_config_load"):
        updates = _run_with_state({
            "agent.id": "agentos",
            "execution_context": {"rp_init": "没有等号,,=空键"},
        })
    assert "context.rp_init_effects" not in updates


def test_rp_init_whitespace_and_dedup_last_wins() -> None:
    """段两侧空白容忍；同键多次出现后者胜（dict 归一语义，面板聚合端不产重键）。"""
    updates = _run_with_state({
        "agent.id": "agentos",
        "execution_context": {"rp_init": " player.气血=+5 , player.气血=-2 "},
    })
    assert updates["context.rp_init_effects"] == {"player.气血": "-2"}


# ── pack 呈现面声明（ui/init_ui）：不入 state，card 质检服务 warning 面 ──────

_PACK_WITH_PRESENTATION = (
    "name: 自建包\n"
    "variables:\n"
    "  键: {type: int}\n"
    "ui:\n"
    "  icon: \"🎲\"\n"
    "  lineage: 传说\n"
    "init_ui:\n"
    "  routes:\n"
    "    - {id: '', label: 缺 id}\n"
)


def test_pack_presentation_declarations_not_in_state_but_lint_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """ui/init_ui 零 context.* 并入；lineage 非法 / route 缺 id 走 lint warning 面。"""
    factory = tmp_path / "factory"
    factory.mkdir()
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "user-root"))
    monkeypatch.delenv("AGENTOS_USER_CONFIG_DIR", raising=False)
    monkeypatch.delenv("AGENTOS_USER_PLUGINS_DIR", raising=False)
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(factory))
    agents = factory / "agents" / "mode_roleplay"
    agents.mkdir(parents=True)
    (agents / "card_rp.yaml").write_text(
        "display_name: 卡\npack_ids:\n- pack_ui\n", encoding="utf-8"
    )
    pkg = tmp_path / "user-root" / "plugins" / "modes" / "mode_roleplay"
    (pkg / "packs").mkdir(parents=True)
    (pkg / "plugin.json").write_text("{}", encoding="utf-8")
    (pkg / "packs" / "pack_ui.yaml").write_text(_PACK_WITH_PRESENTATION, encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="agent_config_load"):
        updates = _run_with_state({"agent.id": "mode_roleplay/card_rp"})

    assert updates["context.rp_state_schema"] == {"键": {"type": "int"}}, "Tier 0 面照常并入"
    assert not any(k.startswith("context.rp_init") or k == "context.rp_pack_ui" for k in updates), \
        "呈现面声明零 state 新键"
    messages = [r.message for r in caplog.records]
    assert any("lineage" in m and "传说" in m for m in messages), "lineage 非法 lint warning"
    assert any("routes" in m and "id" in m for m in messages), "route 缺 id lint warning"


def test_pack_presentation_valid_declaration_zero_lint_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """合法呈现面声明零 warning（warning 面只收作者面错误）。"""
    factory = tmp_path / "factory"
    factory.mkdir()
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "user-root"))
    monkeypatch.delenv("AGENTOS_USER_CONFIG_DIR", raising=False)
    monkeypatch.delenv("AGENTOS_USER_PLUGINS_DIR", raising=False)
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(factory))
    agents = factory / "agents" / "mode_roleplay"
    agents.mkdir(parents=True)
    (agents / "card_rp.yaml").write_text(
        "display_name: 卡\npack_ids:\n- pack_ui\n", encoding="utf-8"
    )
    pkg = tmp_path / "user-root" / "plugins" / "modes" / "mode_roleplay"
    (pkg / "packs").mkdir(parents=True)
    (pkg / "plugin.json").write_text("{}", encoding="utf-8")
    (pkg / "packs" / "pack_ui.yaml").write_text(
        "name: 自建包\n"
        "ui: {icon: \"⚔️\", style_tags: [水墨], lineage: full}\n"
        "init_ui:\n"
        "  routes:\n"
        "    - {id: lone, label: 散修逆袭, profile: 你是散修}\n",
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="agent_config_load"):
        _run_with_state({"agent.id": "mode_roleplay/card_rp"})

    messages = [r.message for r in caplog.records]
    assert not any("lint_ui" in m or "ui." in m or "init_ui" in m for m in messages), \
        f"合法声明零 lint warning（实际：{messages}）"


def test_rp_init_trailing_backslash_preserved_both_sides() -> None:
    """尾部悬挂反斜杠按字面保留（切段与反转义两侧同一宽容语义）。"""
    mod = _load_plugin_module()
    tail = "b\\,c" + "\\"
    assert mod._split_escaped("a,b\\,c" + "\\", ",") == ["a", tail]
    snapshot = "world.路=山\\,水\\,湖" + "\\"
    updates = _run_with_state({
        "agent.id": "agentos",
        "execution_context": {"rp_init": snapshot},
    })
    assert updates["context.rp_init_effects"] == {"world.路": "山,水,湖" + "\\"}
    assert mod._unescape_commas("尾\\玄") == "尾\\玄"
    assert mod._unescape_commas("挂" + "\\") == "挂" + "\\"
