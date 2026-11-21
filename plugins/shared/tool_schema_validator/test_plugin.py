# @feature: FP-0.2.一 tool_action_stats 键契约 | @ci: python-coverage
# @feature: 幽灵续跑改判事故修复① tool_action_stats 序列化 | @ci: python-coverage
"""tool_schema_validator ``tool_action_stats`` 观测快照契约测试。

两类契约：

1. 序列化（回归 2026-10-19 幽灵续跑改判事故 §4.4/§5 修复①）：快照键必须
   字符串化（形如 ``file_read|read|pass``）——tuple 键 dict 直出
   ``state_updates`` 过不了 MCP/JSON 边界，整个 step 的 state_updates 每轮
   全丢（校验门全瘫）。
2. 管道隔离（2026-11-12 跨管道泄漏实证）：计数基线取自本管道 state 的
   上一轮快照，不得用进程级全局计数器——否则并行/交错执行的管道会在各自
   快照里看到对方的键（实证：另一管道的 todo 原文作为 Counter 键出现）。
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

# 两条区分度的声明面：file_read 只要求 action（合法调用即 pass）；
# file_write 要求 action+path（缺 path 不可修复 → prefill_rejected）。
_READ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"action": {"type": "string"}},
    "required": ["action"],
}
_WRITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"action": {"type": "string"}, "path": {"type": "string"}},
    "required": ["action", "path"],
}


def _ctx(
    pipeline_id: str,
    calls: list[dict[str, Any]],
    definitions: dict[str, Any] | None = None,
) -> PluginContext:
    """最小 ctx：按管道 state 携带定义面与（可能存在的）上轮统计快照。"""
    state: dict[str, Any] = {
        "pipeline_id": pipeline_id,
        StateKeys.RAW_TOOL_CALLS: calls,
        "_tool_definitions": definitions if definitions is not None else {},
    }
    return PluginContext(state=state)


def _snapshot(result: Any) -> dict[str, Any]:
    updates = result.state_updates
    assert "tool_action_stats" in updates, "tool_action_stats 快照键不得缺失"
    return updates["tool_action_stats"]


async def _run_round(
    plugin: Any, ctx: PluginContext, calls: list[dict[str, Any]]
) -> dict[str, Any]:
    """执行一轮并把 state_updates 并回 state（模拟管道跨轮状态合并）。"""
    ctx.state[StateKeys.RAW_TOOL_CALLS] = calls
    result = await plugin.execute(ctx)
    ctx.state.update(result.state_updates)
    return _snapshot(result)


def _read_call(call_id: str) -> dict[str, Any]:
    return {"name": "file_read", "args": {"action": "read"}, "id": call_id}


def _write_call(call_id: str, *, with_path: bool) -> dict[str, Any]:
    args: dict[str, Any] = {"action": "write"}
    if with_path:
        args["path"] = "a.txt"
    return {"name": "file_write", "args": args, "id": call_id}


async def test_previous_snapshot_serialized_and_counts_preserved() -> None:
    """上轮快照作基线：键全 str、计数保值、JSON 可序列化。

    修复点回归：tuple 键过不了 MCP/JSON 边界，json.dumps 在修复前抛
    TypeError（校验门全瘫的根因炸点）。
    """
    ctx = _ctx("pipe_snapshot", [{"name": "schema_free_tool", "args": {}, "id": "call_1"}])
    ctx.state["_tool_definitions"] = {"schema_free_tool": {}}
    ctx.state["tool_action_stats"] = {
        "file_read|read|pass": 3,
        "file_write|write|schema_fixed": 2,
    }

    plugin = _mod.ToolSchemaValidator()
    result = await plugin.execute(ctx)

    stats = _snapshot(result)
    json.dumps(result.state_updates)  # 修复前此处 TypeError（tuple 键）
    assert all(isinstance(k, str) for k in stats)
    assert stats == {
        "file_read|read|pass": 3,
        "file_write|write|schema_fixed": 2,
    }, "无 schema 工具不产生增量，快照应与基线一致"


async def test_empty_stats_still_emits_key() -> None:
    """空统计 → 输出含空 dict 的 tool_action_stats 键（不缺键）。"""
    ctx = _ctx("pipe_empty", [{"name": "schema_free_tool", "args": {}, "id": "call_1"}])
    ctx.state["_tool_definitions"] = {"schema_free_tool": {}}

    plugin = _mod.ToolSchemaValidator()
    result = await plugin.execute(ctx)

    assert _snapshot(result) == {}
    json.dumps(result.state_updates)


async def test_malformed_previous_snapshot_is_discarded() -> None:
    """畸形基线（非 str 键/非正整数计数）丢弃，不炸校验步骤。"""
    ctx = _ctx("pipe_malformed", [{"name": "schema_free_tool", "args": {}, "id": "call_1"}])
    ctx.state["_tool_definitions"] = {"schema_free_tool": {}}
    ctx.state["tool_action_stats"] = {
        "file_read|read|pass": 2,
        "bad": "3",
        "neg": -1,
        "bool": True,
        "": 5,
        7: 4,
    }

    plugin = _mod.ToolSchemaValidator()
    result = await plugin.execute(ctx)

    assert _snapshot(result) == {"file_read|read|pass": 2}


async def test_stats_isolated_between_pipelines_interleaved() -> None:
    """两个 pipeline_id 交错调用：各自快照只含自己的键，互不泄漏。

    回归 2026-11-12：模块级 Counter 让另一管道的键（如 todo 原文）混进本
    管道快照。两条管道的工具面刻意不同（file_read/file_write + 不同
    outcome），泄漏一旦存在键面即穿透。
    """
    plugin = _mod.ToolSchemaValidator()
    ctx_a = _ctx(
        "pipe_a",
        [],
        {"file_read": {"input_schema": _READ_SCHEMA}},
    )
    ctx_b = _ctx(
        "pipe_b",
        [],
        {"file_write": {"input_schema": _WRITE_SCHEMA}},
    )

    # 交错：A 两轮 → B 一轮 → A 一轮；B 内两种 outcome（pass / rejected）
    stats_a1 = await _run_round(plugin, ctx_a, [_read_call("a1"), _read_call("a2")])
    stats_b = await _run_round(
        plugin,
        ctx_b,
        [_write_call("b1", with_path=True), _write_call("b2", with_path=False)],
    )
    stats_a2 = await _run_round(plugin, ctx_a, [_read_call("a3")])

    assert stats_a1 == {"file_read|read|pass": 2}, "首轮 A 统计只含本轮增量"
    assert stats_b == {
        "file_write|write|pass": 1,
        "file_write|write|prefill_rejected": 1,
    }, "B 快照不得携带 A 的键"
    assert stats_a2 == {"file_read|read|pass": 3}, "A 跨轮累加，基线来自自身 state"
    assert not any(key.startswith("file_write") for key in stats_a2), "A 不得看到 B 的键"
    assert not any(key.startswith("file_read") for key in stats_b), "B 不得看到 A 的键"


async def test_sequential_pipelines_do_not_leak() -> None:
    """连续（非交错）执行同样隔离：后跑的管道看不到先跑管道的键。"""
    plugin = _mod.ToolSchemaValidator()
    ctx_a = _ctx("pipe_seq_a", [], {"file_read": {"input_schema": _READ_SCHEMA}})
    ctx_b = _ctx("pipe_seq_b", [], {"file_write": {"input_schema": _WRITE_SCHEMA}})

    await _run_round(plugin, ctx_a, [_read_call("a1")])
    stats_b = await _run_round(plugin, ctx_b, [_write_call("b1", with_path=True)])

    assert stats_b == {"file_write|write|pass": 1}
    assert not any(key.startswith("file_read") for key in stats_b)
