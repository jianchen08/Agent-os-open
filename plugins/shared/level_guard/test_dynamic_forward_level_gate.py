# @feature: FP-0.2.二 工具面声明与动态定义集 | @vision: V2 全能闭环 | @ci: python-coverage
"""level_guard 动态转发壳判定键展开测试。

动态工具不在 `tool_ids` 白名单里，只经薄转发壳 `call_tool` 调用。若层级闸只按
`call_tool` 这个名字判定，动态引入 `task_submit` 就等于绕开 L1→L2→L3 委托链。
本文件锁的契约：

- 判定键展开为**目标工具名**：转发壳调用 `task_submit` 且目标不在 `tool_ids`
  → 预填拒绝（与直接调用同一结论）。
- 展开只认 `call_tool`（白名单式）：任意工具伪造 `tool` 字段不参与判定。
- 目标在 `tool_ids` 内 → 放行（展开不导致误拦）。
- 条目名保留原调用名 `call_tool`（tool_core 按调用整形），目标名进 metadata。
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
_SHARED_DIR = str(_PLUGIN_DIR.parent)

for _p in (str(_PLUGIN_DIR), _SHARED_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _libs.pipeline_base.plugin import PluginContext, PluginResult  # noqa: E402


def _load_plugin() -> Any:
    name = "_lg_dynamic_expand_ut"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, _PLUGIN_DIR / "plugin.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _ctx(state: dict[str, Any]) -> PluginContext:
    return PluginContext(state=dict(state))


def _pre(result: PluginResult) -> list[dict[str, Any]]:
    assert isinstance(result, PluginResult)
    return result.state_updates.get("pre_decided_results", [])


def _forward(target: str, call_id: str = "c-dyn") -> dict[str, Any]:
    return {"id": call_id, "name": "call_tool", "args": {"tool": target, "arguments": {"task_id": "t1"}}}


class TestEffectiveToolName:
    def test_expands_forward_shell(self) -> None:
        mod = _load_plugin()
        assert mod._effective_tool_name(_forward("task_submit")) == "task_submit"

    def test_non_forward_untouched(self) -> None:
        mod = _load_plugin()
        assert mod._effective_tool_name({"name": "file_read", "args": {}}) == "file_read"

    def test_other_tool_cannot_forge_target(self) -> None:
        """伪造 tool 字段的其他工具不展开（白名单式）。"""
        mod = _load_plugin()
        forged = {"name": "memory", "args": {"tool": "task_submit"}}
        assert mod._effective_tool_name(forged) == "memory"

    def test_missing_target_falls_back(self) -> None:
        mod = _load_plugin()
        assert mod._effective_tool_name({"name": "call_tool", "args": {"arguments": {}}}) == "call_tool"


class TestLevelGate:
    def test_forward_to_task_tool_blocked_when_not_whitelisted(self) -> None:
        """动态引入 task_submit 且未被 tool_ids 授权 → 层级闸拦截。"""
        mod = _load_plugin()
        plugin = mod.LevelGuardPlugin(config={"strict": True})
        ctx = _ctx({"raw_tool_calls": [_forward("task_submit")], "tool_ids": ["file_read"], "agent_level": "L2"})
        entries = _pre(_run(plugin.execute(ctx)))
        assert len(entries) == 1
        assert entries[0]["tool_name"] == "call_tool"  # 条目名保留原调用名
        assert entries[0]["call_id"] == "c-dyn"
        assert entries[0]["metadata"]["target_tool"] == "task_submit"
        assert "task_submit" in entries[0]["error"]

    def test_forward_to_task_tool_allowed_when_whitelisted(self) -> None:
        """目标在 tool_ids 内 → 放行（展开不造成误拦）。"""
        mod = _load_plugin()
        plugin = mod.LevelGuardPlugin(config={"strict": True})
        ctx = _ctx({"raw_tool_calls": [_forward("task_submit")], "tool_ids": ["task_submit"], "agent_level": "L1"})
        assert _pre(_run(plugin.execute(ctx))) == []

    def test_forward_to_non_task_tool_passes(self) -> None:
        """目标非任务类工具 → 层级闸不管（沿用软放行语义）。"""
        mod = _load_plugin()
        plugin = mod.LevelGuardPlugin(config={"strict": True})
        ctx = _ctx({"raw_tool_calls": [_forward("file_read")], "tool_ids": [], "agent_level": "L2"})
        assert _pre(_run(plugin.execute(ctx))) == []

    def test_direct_task_tool_still_blocked(self) -> None:
        """回归：直接调用 task_submit 未授权 → 仍拦截（展开不改变原语义）。"""
        mod = _load_plugin()
        plugin = mod.LevelGuardPlugin(config={"strict": True})
        ctx = _ctx(
            {
                "raw_tool_calls": [{"id": "c-direct", "name": "task_submit", "args": {}}],
                "tool_ids": ["file_read"],
                "agent_level": "L2",
            }
        )
        entries = _pre(_run(plugin.execute(ctx)))
        assert len(entries) == 1
        assert entries[0]["tool_name"] == "task_submit"
        assert entries[0]["metadata"]["target_tool"] == "task_submit"
