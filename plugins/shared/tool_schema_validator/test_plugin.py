# @feature: FP-0.2.一 tool_action_stats 键契约 | @ci: python-coverage
# @feature: 幽灵续跑改判事故修复① tool_action_stats 序列化 | @ci: python-coverage
"""tool_schema_validator ``tool_action_stats`` 观测快照序列化契约测试。

回归 2026-10-19 幽灵续跑改判事故 §4.4/§5 修复①：``_ACTION_STATS`` 是
``Counter[tuple[str, str, str]]``，元组键 dict 直出 ``state_updates`` 过不了
MCP/JSON 边界（``keys must be str, int, float, bool or None, not tuple``），
整个 step 的 state_updates 每轮全丢（含 schema_validated、raw_tool_calls
校验后回写、pre_decided_results），校验门全瘫 + plugin_errors 洪泛。
快照键必须字符串化（形如 ``file_read|read|pass``）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

_DIR = Path(__file__).resolve().parent
_SHARED = _DIR.parents[0]  # plugins/shared（_libs 等平铺共享模块解析根）

for _d in [_DIR, _SHARED]:
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

from _libs.pipeline_base.plugin import PluginContext  # noqa: E402
from _libs.pipeline_base.types import StateKeys  # noqa: E402

# 裸名 `plugin` 全车道共跑时会被先收集目录的同名模块劫持，
# 按 _DIR 显式路径加载（与 workspace_lifecycle/task_reminder 系测试同范式）。
_MOD_NAME = "tool_schema_validator_plugin_test"
_spec = importlib.util.spec_from_file_location(_MOD_NAME, str(_DIR / "plugin.py"))
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_MOD_NAME] = _mod
_spec.loader.exec_module(_mod)


def _ctx() -> PluginContext:
    """最小 ctx：definitions 声明无 input_schema 的工具 → execute 走
    「无 schema 放行」路径，不触碰进程级计数器，快照内容完全由预置值
    决定（断言确定性，不混入本轮自增项）。"""
    state: dict[str, Any] = {
        StateKeys.RAW_TOOL_CALLS: [
            {"name": "schema_free_tool", "args": {}, "id": "call_1"}
        ],
        "_tool_definitions": {"schema_free_tool": {}},
    }
    return PluginContext(state=state)


def _snapshot(result: Any) -> dict[str, Any]:
    updates = result.state_updates
    assert "tool_action_stats" in updates, "tool_action_stats 快照键不得缺失"
    return updates["tool_action_stats"]


async def test_tuple_keys_serialized_to_str_and_counts_preserved() -> None:
    """预置 tuple 键统计 → 快照 JSON 可序列化、键全 str、计数保值。

    修复点回归：tuple 键过不了 MCP/JSON 边界，json.dumps 在修复前抛
    TypeError（校验门全瘫的根因炸点）。
    """
    _mod._reset_action_stats()
    _mod._ACTION_STATS[("file_read", "read", "pass")] += 3
    _mod._ACTION_STATS[("file_write", "write", "schema_fixed")] += 2

    plugin = _mod.ToolSchemaValidator()
    result = await plugin.execute(_ctx())

    stats = _snapshot(result)
    json.dumps(result.state_updates)  # 修复前此处 TypeError（tuple 键）
    assert all(isinstance(k, str) for k in stats)
    assert stats["file_read|read|pass"] == 3
    assert stats["file_write|write|schema_fixed"] == 2


async def test_empty_stats_still_emits_key() -> None:
    """空统计 → 输出含空 dict 的 tool_action_stats 键（不缺键）。"""
    _mod._reset_action_stats()

    plugin = _mod.ToolSchemaValidator()
    result = await plugin.execute(_ctx())

    assert _snapshot(result) == {}
    json.dumps(result.state_updates)
