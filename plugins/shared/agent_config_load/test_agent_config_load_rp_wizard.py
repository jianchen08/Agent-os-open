# @feature: FP-0.2.〇 RP面板刀-Ⅰ 开演向导 | @vision: V1 可进化 | @ci: python-coverage
"""agent_config_load 开演向导 pack 增量并入测试（RP 面板刀-Ⅰ，FR-A3/FR-G4）。

契约（消费侧最小接线）：
- execution_context.wizard_pack_ids（逗号分隔 id 串，mode.session extra_context
  桥通道只收非空字符串）增量并入卡顶层 pack_ids——卡声明序在前、向导追加在
  后，去重保序；
- 键缺席/非字符串/纯空白 = 现状零变化（兼容态）；
- 向导追加包与卡声明包同键冲突：声明序后者生效（与多 pack 合并语义同源，
  rp_packs.merge_state_decl），故去重保序可由冲突键观测。

env 隔离与包内 pack staging 同 test_agent_config_load_rp_packs（find_mode_package_dir
双根解析的既定机制）。
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
    mod_name = "agent_config_load_plugin_rp_wizard_test"
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


def _run(base: Path, agent_yaml: str, wizard_packs: Any = "__absent__") -> dict[str, Any]:
    """装载执行（wizard_packs 非 __absent__ 时注入 execution_context 向导键）。"""
    _write_agent(base, agent_yaml)
    state: dict[str, Any] = {"agent.id": _AGENT_ID}
    if wizard_packs != "__absent__":
        state["execution_context"] = {"wizard_pack_ids": wizard_packs}
    mod = _load_plugin_module()
    plugin = mod.AgentConfigLoadPlugin(config={})
    result = asyncio.run(plugin.execute(_ctx(state)))
    return result.state_updates


# 双包共键冲突 + 各自独有键（去重保序与增量并入的可观测判据）
_PACK_A = (
    "variables:\n"
    "  shared: {type: int}\n"
    "  only_a: {type: bool}\n"
)
_PACK_B = (
    "variables:\n"
    "  shared: {type: string}\n"
    "  only_b: {type: int}\n"
)


def test_wizard_pack_additive_to_card_packs(factory_root: Path) -> None:
    """增量并入：卡声明 pack_a 在前，向导追加 pack_b 在后——独有键双并入，
    共键 shared 按声明序后者（向导包）生效。"""
    _write_builtin_pack(factory_root, "pack_a", _PACK_A)
    _write_builtin_pack(factory_root, "pack_b", _PACK_B)
    updates = _run(factory_root, "display_name: 卡\npack_ids:\n- pack_a\n", "pack_b")

    assert updates["context.rp_state_schema"] == {
        "shared": {"type": "string"},
        "only_a": {"type": "bool"},
        "only_b": {"type": "int"},
    }


def test_wizard_duplicate_of_card_pack_keeps_card_first_order(
    factory_root: Path,
) -> None:
    """去重保序：向导重复卡已声明的 pack_a 不改变序（shared 仍= int，若重复
    追加成 [a, b, a] 则 shared 会被 a 二次覆盖为 int——此处断言 string 才对）。"""
    _write_builtin_pack(factory_root, "pack_a", _PACK_A)
    _write_builtin_pack(factory_root, "pack_b", _PACK_B)
    updates = _run(factory_root, "pack_ids:\n- pack_a\n", "pack_b,pack_a")

    assert updates["context.rp_state_schema"]["shared"] == {"type": "string"}
    assert updates["context.rp_state_schema"]["only_a"] == {"type": "bool"}


def test_wizard_pack_only_without_card_packs(factory_root: Path) -> None:
    """卡无 pack_ids 声明、仅向导勾选：向导包独供并入（增量语义）。"""
    _write_builtin_pack(factory_root, "pack_b", _PACK_B)
    updates = _run(factory_root, "display_name: 卡\n", "pack_b")

    assert updates["context.rp_state_schema"] == {
        "shared": {"type": "string"},
        "only_b": {"type": "int"},
    }


def test_no_wizard_key_compat_zero_change(factory_root: Path) -> None:
    """兼容态：无向导键 = 卡 pack_ids 行为与批 2-Ⅱ 完全一致（pack_a 独供）。"""
    _write_builtin_pack(factory_root, "pack_a", _PACK_A)
    updates = _run(factory_root, "pack_ids:\n- pack_a\n")

    assert updates["context.rp_state_schema"] == {
        "shared": {"type": "int"},
        "only_a": {"type": "bool"},
    }


@pytest.mark.parametrize(
    "wizard_value",
    [None, "   ", 42, {"pack_b": True}],
    ids=["absent", "blank", "non-string", "non-string-object"],
)
def test_wizard_invalid_values_zero_change(
    factory_root: Path, wizard_value: Any
) -> None:
    """向导键缺席/空白/非字符串 = 空表零并入（卡 pack_ids 照常，零行为变化）。"""
    _write_builtin_pack(factory_root, "pack_a", _PACK_A)
    state_extra = {} if wizard_value is None else {"wizard_pack_ids": wizard_value}
    _write_agent(factory_root, "pack_ids:\n- pack_a\n")
    state: dict[str, Any] = {"agent.id": _AGENT_ID}
    if state_extra:
        state["execution_context"] = state_extra
    mod = _load_plugin_module()
    plugin = mod.AgentConfigLoadPlugin(config={})
    result = asyncio.run(plugin.execute(_ctx(state)))

    assert result.state_updates["context.rp_state_schema"] == {
        "shared": {"type": "int"},
        "only_a": {"type": "bool"},
    }


def test_wizard_segments_and_spaces_tolerated(factory_root: Path) -> None:
    """逗号分隔串空段/首尾空白容忍：" pack_b , ，后段空 " → 仅 pack_b 并入。

    分隔符契约 = ASCII 逗号（面板 join(',')）；段内残留全角逗号 → id 形态
    非法按缺失零并入（fail-soft warning），不阻断其余 id。
    """
    _write_builtin_pack(factory_root, "pack_b", _PACK_B)
    updates = _run(factory_root, "display_name: 卡\n", " pack_b ,, ")

    assert updates["context.rp_state_schema"]["only_b"] == {"type": "int"}


def test_wizard_illegal_id_treated_missing(factory_root: Path) -> None:
    """向导 id 形态非法（穿越形态）= 该 pack 零并入（路径安全闸同款）。"""
    _write_builtin_pack(factory_root, "pack_a", _PACK_A)
    updates = _run(factory_root, "pack_ids:\n- pack_a\n", "../escape")

    assert updates["context.rp_state_schema"] == {
        "shared": {"type": "int"},
        "only_a": {"type": "bool"},
    }


def test_wizard_extra_ids_parser_unit() -> None:
    """_wizard_extra_ids 纯函数面：去空段/去重保序/非映射 execution_context 容错。"""
    mod = _load_plugin_module()
    parse = mod._wizard_extra_ids

    assert parse({}, "wizard_pack_ids") == []
    assert parse({"execution_context": "垃圾"}, "wizard_pack_ids") == []
    assert parse({"execution_context": {"wizard_pack_ids": "  "}}, "wizard_pack_ids") == []
    assert parse({"execution_context": {"wizard_pack_ids": 42}}, "wizard_pack_ids") == []
    assert (
        parse(
            {"execution_context": {"wizard_pack_ids": " b , a , b , a ,,"}},
            "wizard_pack_ids",
        )
        == ["b", "a"]
    )
