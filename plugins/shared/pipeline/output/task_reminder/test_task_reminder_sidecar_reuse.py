# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @vision: V3 可嵌入 | @ci: python-coverage
"""task_reminder sidecar 实例复用契约测试（M2 状态复位）。

同 sidecar 进程内一个 TaskReminder 实例被多个 agent 管道连续复用：
前一 agent 经 state/plugin_configs 注入的 max_reminders / evaluation_mode
不得残留到下一 agent 的执行——每次 execute 先回构造默认再应用覆盖。
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
_SHARED = _DIR.parents[2]  # plugins/shared/

for _d in [_DIR, _SHARED]:
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

from pipeline.plugin import PluginContext  # noqa: E402


def _load_plugin_module() -> Any:
    mod_name = "task_reminder_plugin_sidecar_test"
    module_path = _DIR / "plugin.py"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, str(module_path))
    assert spec is not None, "Cannot load plugin.py"
    assert spec.loader is not None, "Cannot load plugin.py"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _ctx(state: dict[str, Any]) -> PluginContext:
    return PluginContext(state=dict(state), config={})


def _base_state(**over: Any) -> dict[str, Any]:
    base = {
        "core_type": "llm_call",
        "iteration": 3,
        "task.id": "task-x",
        "task.status": "running",
        "agent_level": "L2",
        "raw_tool_calls": [],
        "raw_result": "阶段性输出",
        "messages": [],
    }
    base.update(over)
    return base


class TestRuntimeConfigResetPerAgent:
    def test_max_reminders_does_not_leak_across_agents(self) -> None:
        """Agent A 注入 max_reminders=3 并耗尽；Agent B（无覆盖、同实例）
        reminder_count=7 必须仍按构造默认 10 走提醒注入，而非残留的 3 直接收束。"""
        mod = _load_plugin_module()
        plugin = mod.TaskReminder(config={"max_reminders": 10})

        state_a = _base_state(max_reminders=3, evaluate_reminder_count=3)
        res_a = _run(plugin.execute(_ctx(state_a)))
        assert res_a.route_signal is not None, "Agent A 提醒已达注入上限应 end"
        assert res_a.route_signal.route_type == "end"

        state_b = _base_state(evaluate_reminder_count=7)
        res_b = _run(plugin.execute(_ctx(state_b)))
        assert res_b.route_signal is not None, "Agent B 未达默认上限应收束为 next_llm 续跑"
        assert res_b.route_signal.route_type == "next_llm", (
            f"Agent B 应继续注入提醒（残留上限会提前 end），实际 {res_b.route_signal.reason}"
        )
        assert res_b.state_updates["evaluate_reminder_count"] == 8

    def test_evaluation_mode_does_not_leak_across_agents(self) -> None:
        """Agent A 经 plugin_configs 切入评估者模式；Agent B（无 plugin_configs、同实例）
        的纯文本输出必须走执行者文案（系统提醒），而不是残留的评估者文案。"""
        mod = _load_plugin_module()
        plugin = mod.TaskReminder(config={"evaluation_mode": False})

        state_a = _base_state(
            raw_result="",
            raw_tool_calls=[{"function": {"name": "bash"}}],
            plugin_configs={"task_reminder": {"evaluation_mode": True}},
            eval_tool_only_count=6,
        )
        res_a = _run(plugin.execute(_ctx(state_a)))
        appended_a = res_a.state_updates.get("messages", [])[-1].get("content", "")
        assert "评估" in appended_a, "Agent A 处于评估者模式，注入评估强制提醒"

        state_b = _base_state()
        res_b = _run(plugin.execute(_ctx(state_b)))
        appended_b = res_b.state_updates["messages"][-1]["content"]
        assert "【系统提醒" in appended_b, (
            f"Agent B 无配置覆盖时应使用执行者文案，实际残留了评估者模式: {appended_b[:30]}"
        )
