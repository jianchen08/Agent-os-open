# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: python-coverage
"""level_guard input 插件单元测试。

行为契约（只对任务类工具做 tool_ids 硬限制，其余软放行；ADR 2026-09-28
结果预填：拦截 = 写 pre_decided_results，tool_core 命中即跳过执行）：
1. disabled 配置 → 零产出（放行）
2. 非 tool_execute 循环体（llm_call/其他）→ 零产出，不检查
3. raw_tool_calls 为空 → 零产出
4. 只有非任务类工具调用 → 软放行零产出（tool_schema 可见性兜底）
5. 任务类工具 + tool_ids 缺失：strict=True 全量预填拒绝（fail-closed，
   对齐旧决策键缺 blocked_tools = 全拦语义）/ strict=False 放行
6. 任务类工具在 tool_ids 内 → 放行零产出
7. 任务类工具不在 tool_ids 内 → 预填拒绝（有 id 按 call_id、无 id 按工具
   名兜底），metadata 携带 agent_level 归因
8. name/priority 属性契约（priority 可配置）

[来源: plugins/shared/pipeline/input/level_guard/plugin.py]
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

_SHARED_DIR = str(_PLUGIN_DIR.parents[2])  # plugins/shared/
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)

from pipeline.plugin import PluginContext, PluginResult  # noqa: E402


def _load_plugin() -> Any:
    """唯一名动态加载 plugin.py（每次新建，隔离模块级状态）。"""
    name = "_lg_plugin_ut"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, _PLUGIN_DIR / "plugin.py")
    assert spec is not None, "Cannot load plugin.py"
    assert spec.loader is not None, "Cannot load plugin.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _run(coro: Any) -> Any:
    """同步执行协程（新建事件循环，避免 pytest-asyncio 冲突）。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_ctx(state: dict[str, Any] | None = None) -> PluginContext:
    return PluginContext(state=dict(state or {}))


def _pre(result: PluginResult) -> list[dict[str, Any]]:
    """预填拒绝结果（拦截时非空，放行时空）。"""
    assert isinstance(result, PluginResult)
    assert isinstance(result.state_updates, dict)
    return result.state_updates.get("pre_decided_results", [])


def _pre_by_name(entries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """无 call_id 条目按工具名索引（幻觉调用名字兜底）。"""
    return {e["tool_name"]: e for e in entries if e.get("call_id") is None}


# ── 属性契约 ────────────────────────────────────────────────


def test_name_is_level_guard() -> None:
    mod = _load_plugin()
    assert mod.LevelGuardPlugin().name == "level_guard"


def test_priority_default_and_override() -> None:
    mod = _load_plugin()
    assert mod.LevelGuardPlugin().priority == 20
    assert mod.LevelGuardPlugin(config={"priority": 5}).priority == 5


# ── 短路分支（零产出）──────────────────────────────────────


def test_disabled_short_circuits_even_with_blocked_tool() -> None:
    mod = _load_plugin()
    plugin = mod.LevelGuardPlugin(config={"enabled": False, "strict": True})
    ctx = _make_ctx(
        {
            "core_type": "tool_execute",
            "raw_tool_calls": [{"name": "task_submit", "arguments": {}}],
        }
    )
    assert _run(plugin.execute(ctx)).state_updates == {}


@pytest.mark.parametrize("core_type", ["llm_call", "tool_response", ""])
def test_non_tool_execute_phase_never_checked(core_type: str) -> None:
    mod = _load_plugin()
    plugin = mod.LevelGuardPlugin()
    state: dict[str, Any] = {"raw_tool_calls": [{"name": "task_submit", "arguments": {}}]}
    if core_type:
        state["core_type"] = core_type
    assert _run(plugin.execute(_make_ctx(state))).state_updates == {}


def test_empty_tool_calls_passes() -> None:
    mod = _load_plugin()
    plugin = mod.LevelGuardPlugin()
    ctx = _make_ctx({"core_type": "tool_execute", "raw_tool_calls": []})
    assert _run(plugin.execute(ctx)).state_updates == {}


# ── 非任务类工具软放行 ──────────────────────────────────────


@pytest.mark.parametrize(
    "tool_names",
    [
        ["file_write"],
        ["bash_execute", "enhanced_search"],
        ["memory_inject", "human_interaction", "task_manage_extra"],
    ],
)
def test_non_task_tools_soft_gated_regardless_of_tool_ids(tool_names: list[str]) -> None:
    mod = _load_plugin()
    plugin = mod.LevelGuardPlugin()  # 无 tool_ids 也放行——软限制由 tool_schema 可见性兜底
    calls = [{"name": name, "arguments": {}} for name in tool_names]
    ctx = _make_ctx({"core_type": "tool_execute", "raw_tool_calls": calls})
    assert _run(plugin.execute(ctx)).state_updates == {}


def test_mixed_calls_blocks_only_task_tools() -> None:
    mod = _load_plugin()
    plugin = mod.LevelGuardPlugin()
    calls = [
        {"name": "file_write", "arguments": {}},
        {"name": "task_submit", "id": "call_ts", "arguments": {}},
        {"name": "bash_execute", "arguments": {}},
    ]
    ctx = _make_ctx(
        {
            "core_type": "tool_execute",
            "raw_tool_calls": calls,
            "tool_ids": ["file_write", "bash_execute"],
        }
    )
    entries = _pre(_run(plugin.execute(ctx)))
    # 只拦任务类工具（有 id 按 call_id 预填），非任务类不误报
    assert len(entries) == 1
    assert entries[0]["call_id"] == "call_ts"
    assert entries[0]["tool_name"] == "task_submit"
    assert entries[0]["success"] is False
    assert "权限策略拦截" in entries[0]["error"]
    assert entries[0]["metadata"]["decided_by"] == "level_guard"


# ── tool_ids 缺失：strict 语义 ──────────────────────────────


def test_missing_tool_ids_strict_blocks_all_calls() -> None:
    mod = _load_plugin()
    plugin = mod.LevelGuardPlugin()  # strict 默认 True
    ctx = _make_ctx(
        {
            "core_type": "tool_execute",
            "raw_tool_calls": [
                {"name": "task_evaluate", "id": "c1", "arguments": {}},
                {"name": "file_write", "id": "c2", "arguments": {}},
            ],
            "agent_level": "L2",
        }
    )
    entries = _pre(_run(plugin.execute(ctx)))
    # fail-closed 全量预填（对齐旧 blocked_tools 缺失 = 全拦）
    assert {e["call_id"] for e in entries} == {"c1", "c2"}
    assert all(e["success"] is False for e in entries)
    assert "tool_ids not found in state" in entries[0]["error"]
    assert "L2" in entries[0]["error"]


def test_missing_tool_ids_non_strict_passes() -> None:
    mod = _load_plugin()
    plugin = mod.LevelGuardPlugin(config={"strict": False})
    ctx = _make_ctx(
        {
            "core_type": "tool_execute",
            "raw_tool_calls": [{"name": "task_submit", "arguments": {}}],
        }
    )
    assert _run(plugin.execute(ctx)).state_updates == {}


# ── tool_ids 授权判定 ───────────────────────────────────────


@pytest.mark.parametrize(
    ("tool_name", "tool_ids"),
    [
        ("task_submit", ["task_submit"]),
        ("task_manage", ["task_submit", "task_manage", "task_evaluate"]),
        ("task_evaluate", ["task_evaluate", "enhanced_search"]),
    ],
)
def test_authorized_task_tool_passes(tool_name: str, tool_ids: list[str]) -> None:
    mod = _load_plugin()
    plugin = mod.LevelGuardPlugin()
    ctx = _make_ctx(
        {
            "core_type": "tool_execute",
            "raw_tool_calls": [{"name": tool_name, "arguments": {}}],
            "tool_ids": tool_ids,
        }
    )
    assert _run(plugin.execute(ctx)).state_updates == {}


def test_unauthorized_task_tool_blocked_with_context() -> None:
    mod = _load_plugin()
    plugin = mod.LevelGuardPlugin()
    ctx = _make_ctx(
        {
            "core_type": "tool_execute",
            "raw_tool_calls": [{"name": "task_submit", "arguments": {}}],
            "tool_ids": ["file_write"],
            "agent_level": "L3",
        }
    )
    entries = _pre(_run(plugin.execute(ctx)))
    by_name = _pre_by_name(entries)
    # 无 id 调用走名字兜底条目
    assert set(by_name) == {"task_submit"}
    entry = by_name["task_submit"]
    assert entry["success"] is False
    assert entry["metadata"]["agent_level"] == "L3"
    assert "L3" in entry["error"]


def test_multiple_unauthorized_tools_all_prefilled() -> None:
    mod = _load_plugin()
    plugin = mod.LevelGuardPlugin()
    calls = [
        {"name": "task_submit", "id": "c_sub", "arguments": {}},
        {"name": "task_manage", "id": "c_mgr", "arguments": {}},
    ]
    ctx = _make_ctx(
        {
            "core_type": "tool_execute",
            "raw_tool_calls": calls,
            "tool_ids": ["task_evaluate"],  # 只授权一个，其余两个均拦截
        }
    )
    entries = _pre(_run(plugin.execute(ctx)))
    assert sorted(e["call_id"] for e in entries) == ["c_mgr", "c_sub"]
    assert {e["tool_name"] for e in entries} == {"task_submit", "task_manage"}


def test_unknown_agent_level_reported_in_block() -> None:
    mod = _load_plugin()
    plugin = mod.LevelGuardPlugin()
    ctx = _make_ctx(
        {
            "core_type": "tool_execute",
            "raw_tool_calls": [{"name": "task_manage", "arguments": {}}],
            "tool_ids": [],
        }
    )
    entries = _pre(_run(plugin.execute(ctx)))
    entry = _pre_by_name(entries)["task_manage"]
    assert entry["metadata"]["agent_level"] == "unknown"


# ── 多 guard 合并（merge_pre_decided 组合面）────────────────


def test_prefill_merges_with_existing_entries() -> None:
    """state 已有其它 guard 的预定结果 → 合并保留，不整键覆盖。"""
    mod = _load_plugin()
    plugin = mod.LevelGuardPlugin()
    existing = [
        {"call_id": "c_other", "tool_name": "bash_execute",
         "success": False, "error": "isolation 拒", "data": None,
         "metadata": None, "duration_ms": 0.0},
    ]
    ctx = _make_ctx(
        {
            "core_type": "tool_execute",
            "raw_tool_calls": [{"name": "task_submit", "id": "c_ts", "arguments": {}}],
            "tool_ids": [],
            "pre_decided_results": existing,
        }
    )
    entries = _pre(_run(plugin.execute(ctx)))
    assert {e["call_id"] for e in entries} == {"c_other", "c_ts"}
