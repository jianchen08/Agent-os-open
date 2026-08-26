# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: python-coverage
"""level_guard input 插件单元测试。

行为契约（只对任务类工具做 tool_ids 硬限制，其余软放行）：
1. disabled 配置 → 一律放行（"level guard disabled"）
2. 非 tool_execute 循环体（llm_call/其他）→ 放行，不检查
3. raw_tool_calls 为空 → 放行
4. 只有非任务类工具调用 → 软放行（tool_schema 可见性兜底）
5. 任务类工具 + tool_ids 缺失：strict=True 拦截 / strict=False 放行
6. 任务类工具在 tool_ids 内 → 放行
7. 任务类工具不在 tool_ids 内 → 拦截，blocked_tools 只列任务类工具，
   decision 携带 agent_level
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


def _decision(result: PluginResult) -> dict[str, Any]:
    assert isinstance(result, PluginResult)
    assert isinstance(result.state_updates, dict)
    decision = result.state_updates["security.level_decision"]
    assert isinstance(decision, dict)
    return decision


# ── 属性契约 ────────────────────────────────────────────────


def test_name_is_level_guard() -> None:
    mod = _load_plugin()
    assert mod.LevelGuardPlugin().name == "level_guard"


def test_priority_default_and_override() -> None:
    mod = _load_plugin()
    assert mod.LevelGuardPlugin().priority == 20
    assert mod.LevelGuardPlugin(config={"priority": 5}).priority == 5


# ── 短路分支 ────────────────────────────────────────────────


def test_disabled_short_circuits_allow_even_with_blocked_tool() -> None:
    mod = _load_plugin()
    plugin = mod.LevelGuardPlugin(config={"enabled": False, "strict": True})
    ctx = _make_ctx(
        {
            "core_type": "tool_execute",
            "raw_tool_calls": [{"name": "task_submit", "arguments": {}}],
        }
    )
    decision = _decision(_run(plugin.execute(ctx)))
    assert decision == {"allowed": True, "reason": "level guard disabled"}


@pytest.mark.parametrize("core_type", ["llm_call", "tool_response", ""])
def test_non_tool_execute_phase_never_checked(core_type: str) -> None:
    mod = _load_plugin()
    plugin = mod.LevelGuardPlugin()
    state: dict[str, Any] = {"raw_tool_calls": [{"name": "task_submit", "arguments": {}}]}
    if core_type:
        state["core_type"] = core_type
    result = _decision(_run(plugin.execute(_make_ctx(state))))
    assert result == {"allowed": True, "reason": "not a tool execution"}


def test_empty_tool_calls_passes() -> None:
    mod = _load_plugin()
    plugin = mod.LevelGuardPlugin()
    ctx = _make_ctx({"core_type": "tool_execute", "raw_tool_calls": []})
    result = _decision(_run(plugin.execute(ctx)))
    assert result == {"allowed": True, "reason": "no tool calls to check"}


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
    result = _decision(_run(plugin.execute(ctx)))
    assert result["allowed"] is True
    assert "no task-control tools" in result["reason"]


def test_mixed_calls_blocks_only_task_tools() -> None:
    mod = _load_plugin()
    plugin = mod.LevelGuardPlugin()
    calls = [
        {"name": "file_write", "arguments": {}},
        {"name": "task_submit", "arguments": {}},
        {"name": "bash_execute", "arguments": {}},
    ]
    ctx = _make_ctx(
        {
            "core_type": "tool_execute",
            "raw_tool_calls": calls,
            "tool_ids": ["file_write", "bash_execute"],
        }
    )
    result = _decision(_run(plugin.execute(ctx)))
    assert result["allowed"] is False
    # 只拦任务类工具，非任务类不误报
    assert result["blocked_tools"] == ["task_submit"]


# ── tool_ids 缺失：strict 语义 ──────────────────────────────


def test_missing_tool_ids_strict_blocks() -> None:
    mod = _load_plugin()
    plugin = mod.LevelGuardPlugin()  # strict 默认 True
    ctx = _make_ctx(
        {
            "core_type": "tool_execute",
            "raw_tool_calls": [{"name": "task_evaluate", "arguments": {}}],
            "agent_level": "L2",
        }
    )
    result = _decision(_run(plugin.execute(ctx)))
    assert result["allowed"] is False
    assert "tool_ids not found in state" in result["reason"]
    assert "L2" in result["reason"]


def test_missing_tool_ids_non_strict_passes() -> None:
    mod = _load_plugin()
    plugin = mod.LevelGuardPlugin(config={"strict": False})
    ctx = _make_ctx(
        {
            "core_type": "tool_execute",
            "raw_tool_calls": [{"name": "task_submit", "arguments": {}}],
        }
    )
    result = _decision(_run(plugin.execute(ctx)))
    assert result == {"allowed": True, "reason": "tool_ids missing but strict=False"}


# ── tool_ids 授权判定 ───────────────────────────────────────


@pytest.mark.parametrize(
    "tool_name, tool_ids",
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
    result = _decision(_run(plugin.execute(ctx)))
    assert result["allowed"] is True
    assert "within tool_ids authorization" in result["reason"]


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
    result = _decision(_run(plugin.execute(ctx)))
    assert result["allowed"] is False
    assert result["blocked_tools"] == ["task_submit"]
    assert result["agent_level"] == "L3"
    assert "L3" in result["reason"]


def test_multiple_unauthorized_tools_all_listed() -> None:
    mod = _load_plugin()
    plugin = mod.LevelGuardPlugin()
    calls = [
        {"name": "task_submit", "arguments": {}},
        {"name": "task_manage", "arguments": {}},
    ]
    ctx = _make_ctx(
        {
            "core_type": "tool_execute",
            "raw_tool_calls": calls,
            "tool_ids": ["task_evaluate"],  # 只授权一个，其余两个均拦截
        }
    )
    result = _decision(_run(plugin.execute(ctx)))
    assert result["allowed"] is False
    assert sorted(result["blocked_tools"]) == ["task_manage", "task_submit"]


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
    result = _decision(_run(plugin.execute(ctx)))
    assert result["allowed"] is False
    assert result["agent_level"] == "unknown"
