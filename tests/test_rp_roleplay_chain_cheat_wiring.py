# @feature: FP-0.2.〇 RP引擎(作者权威+/cheat) | @vision: V1 可进化 | @ci: python-coverage
"""roleplay 链 /cheat 门控接线契约测试（RP 批 3，ADR 2026-10-11）。

锁三件事：
1. roleplay.yaml prepare 组写链级门控键 `_cheat_guard: true`（守卫唯一闸门）；
2. roleplay.yaml llm 组 `when: _cheat_command != True`（命令轮整组短路——llm_core
   与 spill_guard 都不跑），组内 llm_core 既有 when（工具轮跳过）原样保留；
3. autonomous.yaml 零触碰：不写门控键（任务域输 /cheat 是普通消息，零拦截契约）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_PIPELINES = Path(__file__).resolve().parents[1] / "config" / "pipelines"


def _load(name: str) -> dict[str, Any]:
    return yaml.safe_load((_PIPELINES / name).read_text(encoding="utf-8"))


def _find_step(node: Any, step_id: str) -> dict[str, Any] | None:
    if isinstance(node, dict):
        if node.get("id") == step_id:
            return node
        for value in node.values():
            found = _find_step(value, step_id)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_step(item, step_id)
            if found is not None:
                return found
    return None


def test_roleplay_prepare_carries_cheat_guard_flag() -> None:
    cfg = _load("roleplay.yaml")
    prepare = _find_step(cfg.get("loop_bodies"), "prepare")
    assert prepare is not None
    assert prepare.get("context", {}).get("_cheat_guard") is True, "链级门控键 = 守卫唯一闸门"


def test_roleplay_llm_group_short_circuits_on_cheat_round() -> None:
    cfg = _load("roleplay.yaml")
    llm = _find_step(cfg.get("loop_bodies"), "llm")
    assert llm is not None
    assert llm.get("when") == "_cheat_command != True", "命令轮整组短路（llm_core+spill_guard）"
    steps = llm.get("steps", [])
    llm_core = next(s for s in steps if "llm_core" in str(s))
    assert llm_core.get("when") == "_executed_tool_calls == []", "既有工具轮跳过条件原样保留"


def test_autonomous_chain_has_no_cheat_guard() -> None:
    """autonomous.yaml 零触碰：任务域链不写门控键——/cheat 在任务域是普通消息。"""
    text = (_PIPELINES / "autonomous.yaml").read_text(encoding="utf-8")
    assert "_cheat_guard" not in text
    assert "_cheat_command" not in text
