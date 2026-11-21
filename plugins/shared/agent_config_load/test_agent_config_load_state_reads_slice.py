# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci: python-coverage
"""agent_config_load `state.reads` 切片投喂回归（execution_context 读面）。

漏声明形态（2026-11-12 修复）：`execution_context` 未进 manifest `state.reads`
→ 切片后恒读不到 → `_rp_init_effects`（开演 init effects 快照）与
`_wizard_extra_ids`（开演向导 pack 增量）恒走「无 execution_context」缺省：
用户面板选的 init effects 不生效、向导追加的 pack 永不被加载，两处都静默。

本用例按 manifest 声明切片投喂（与生产 invoker 同形），断言两读点在切片下
真实可见；声明回退即红。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parent
_SHARED = _DIR.parents[0]  # plugins/shared/

for _d in (str(_DIR), str(_SHARED)):
    if _d not in sys.path:
        sys.path.insert(0, _d)

from _libs.testing.state_slice import (  # noqa: E402
    manifest_reads,
    slice_feed,
    visible_keys,
)


def _load_plugin_module() -> Any:
    mod_name = "agent_config_load_state_reads_slice_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _DIR / "plugin.py")
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


_MOD = _load_plugin_module()


MANIFEST_READS = manifest_reads(_DIR)


def _fed(state: dict[str, Any]) -> dict[str, Any]:
    return slice_feed(state, MANIFEST_READS)


def _state(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "pipeline_id": "p-1",
        "agent.id": "mode_roleplay/rp_agent",
        "task.id": "p-1",
        "execution_context": {
            "rp_init": "context.scene=雨夜,context.time=23:00",
            "wizard_pack_ids": "pack-a,pack-b",
        },
    }
    state.update(overrides)
    return state


class TestDeclaration:
    def test_execution_context_declared(self) -> None:
        assert "execution_context" in visible_keys(MANIFEST_READS), (
            "execution_context 未声明 → 切片后 rp_init / 向导 pack 读点恒空"
        )


class TestExecutionContextReadersUnderSliceFeed:
    def test_rp_init_effects_parsed_under_slice_feed(self) -> None:
        fed = _fed(_state())
        assert fed["execution_context"]["rp_init"], "切片必须携带 execution_context"

        effects = _MOD._rp_init_effects(fed)

        assert effects == {"context.scene": "雨夜", "context.time": "23:00"}

    def test_wizard_pack_ids_parsed_under_slice_feed(self) -> None:
        fed = _fed(_state())

        ids = _MOD._wizard_extra_ids(fed, _MOD.WIZARD_PACK_IDS_KEY)

        assert ids == ["pack-a", "pack-b"]

    def test_absent_execution_context_stays_empty(self) -> None:
        """无 execution_context 的管道（普通任务/会话）仍走零并入缺省。"""
        state = _state()
        state.pop("execution_context")
        fed = _fed(state)

        assert _MOD._rp_init_effects(fed) is None
        assert _MOD._wizard_extra_ids(fed, _MOD.WIZARD_PACK_IDS_KEY) == []
