# @feature: FP-0.2.〇 RP引擎(状态通道schema化) | @vision: V1 可进化 | @ci: python-coverage
"""agent_config_load 装载卡状态契约声明测试（RP 批 2-Ⅰ 2a）。

契约（照抄 context.regex_scripts 先例）：agent yaml（模式卡）extensions.variables →
context.rp_state_schema、extensions.rules.hard_rules → context.rp_state_hard_rules，
卡产物 verbatim 透传（装载零解释，结构合法性由消费面 fail-soft 把关）；
无声明/空 = 不写键（兼容态零行为变化——出厂卡无 variables 声明即此形态）。

env 隔离：AGENTOS_USER_ROOT / AGENTOS_CONFIG_ROOT 钉到 tmp（与双根测试同契约）。
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


def _load_plugin_module() -> Any:
    mod_name = "agent_config_load_plugin_rp_state_test"
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
    """factory 配置根钉到 tmp（用户根留空 = 直接回落 factory）。"""
    factory = tmp_path / "factory"
    factory.mkdir()
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "user-root"))
    monkeypatch.delenv("AGENTOS_USER_CONFIG_DIR", raising=False)
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(factory))
    return factory


def _write_agent(base: Path, agent_id: str, text: str) -> None:
    agents = base / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / f"{agent_id}.yaml").write_text(text, encoding="utf-8")


def _run(agent_yaml: str, factory_root: Path) -> dict[str, Any]:
    _write_agent(factory_root, "card_rp", agent_yaml)
    mod = _load_plugin_module()
    plugin = mod.AgentConfigLoadPlugin(config={})
    result = asyncio.run(plugin.execute(_ctx({"agent.id": "card_rp"})))
    return result.state_updates


def test_variables_loaded_verbatim(factory_root: Path) -> None:
    """extensions.variables → context.rp_state_schema 原样透传（装载零解释）。"""
    yaml_text = (
        "display_name: 测试卡\n"
        "extensions:\n"
        "  variables:\n"
        "    player:\n"
        "      林远:\n"
        "        灵力: {type: int, default: 10, min: 0, max: 100}\n"
        "    npc:\n"
        "      苏清婉:\n"
        "        好感度: {type: int, min: -100, max: 100}\n"
        "    world:\n"
        "      时辰: {type: enum, values: [晨, 午, 夜]}\n"
    )
    updates = _run(yaml_text, factory_root)
    assert updates["context.rp_state_schema"] == {
        "player": {"林远": {"灵力": {"type": "int", "default": 10, "min": 0, "max": 100}}},
        "npc": {"苏清婉": {"好感度": {"type": "int", "min": -100, "max": 100}}},
        "world": {"时辰": {"type": "enum", "values": ["晨", "午", "夜"]}},
    }


def test_hard_rules_loaded_verbatim(factory_root: Path) -> None:
    """extensions.rules.hard_rules → context.rp_state_hard_rules 原样透传。"""
    yaml_text = (
        "extensions:\n"
        "  variables:\n"
        "    npc:\n"
        "      苏清婉:\n"
        "        好感度: {type: int}\n"
        "  rules:\n"
        "    hard_rules:\n"
        "      npc.*.好感度: {max_delta_per_turn: 10}\n"
        "    combat: {formula: atk}\n"
    )
    updates = _run(yaml_text, factory_root)
    assert updates["context.rp_state_hard_rules"] == {"npc.*.好感度": {"max_delta_per_turn": 10}}


def test_no_declaration_writes_no_keys(factory_root: Path) -> None:
    """无声明卡（出厂卡形态）= 两键均不写 = 兼容态零行为变化。"""
    updates = _run("display_name: 朴素卡\n", factory_root)
    assert "context.rp_state_schema" not in updates
    assert "context.rp_state_hard_rules" not in updates


def test_variables_without_rules_writes_schema_only(factory_root: Path) -> None:
    """variables 有声明而 rules 缺席 = 仅 schema 键（两声明互相独立）。"""
    yaml_text = (
        "extensions:\n"
        "  variables:\n"
        "    好感度: {type: int, min: 0, max: 100}\n"
    )
    updates = _run(yaml_text, factory_root)
    assert updates["context.rp_state_schema"] == {"好感度": {"type": "int", "min": 0, "max": 100}}
    assert "context.rp_state_hard_rules" not in updates
