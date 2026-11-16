# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @vision: V3 可嵌入 | @ci: none-local
"""失败归因结构化 TDD 测试（§10.2 裁定实施，工作包 B）。

裁定依据：docs/working/评估体系全景与自进化流水线_20260924.md §10.2——
裁决方置 task.status=failed 时同时结构化写失败类别与终止原因
（task.failure_class / task.failure_reason），复盘分诊只认终局归因声明，
不从错误堆反推。

覆盖 task_reminder 两个裁决点：
- 提醒耗尽（reminder_exhausted）
- 空回复耗尽（empty_response）
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_module() -> Any:
    """动态加载 plugin.py（TaskReminder），与既有行为测试同款隔离加载。"""
    mod_name = "task_reminder_plugin_wpB_test"
    module_path = _PLUGIN_DIR / "plugin.py"
    assert module_path.exists(), f"plugin.py missing at {module_path}"
    spec = importlib.util.spec_from_file_location(mod_name, module_path)
    assert spec is not None and spec.loader is not None, "Cannot load plugin.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _base_state(**overrides: Any) -> dict[str, Any]:
    """任务管道基础 state（llm_call 轮、有 task.id、纯文本、无子任务）。"""
    state: dict[str, Any] = {
        "task.id": "task-1",
        "task.status": "running",
        "core_type": "llm_call",
        "agent_level": "L3",
        "iteration": 5,
        "raw_result": "进行中汇报",
        "raw_tool_calls": [],
        "messages": [{"role": "user", "content": "做任务"}],
    }
    state.update(overrides)
    return state


def _ctx(state: dict[str, Any]) -> Any:
    mod = _load_module()
    return mod.PluginContext(state=state, config={})


class TestReminderExhaustedAttribution:
    def test_exhausted_writes_failure_class_and_reason(self) -> None:
        """提醒耗尽裁决：置 failed 同时写 failure_class=reminder_exhausted。"""
        import asyncio

        mod = _load_module()
        plugin = mod.TaskReminder(config={"max_reminders": 2})
        result = asyncio.run(plugin.execute(
            _ctx(_base_state(evaluate_reminder_count=2))
        ))
        assert result.state_updates["task.status"] == "failed"
        assert result.state_updates["task.failure_class"] == "reminder_exhausted"
        reason = result.state_updates.get("task.failure_reason")
        assert isinstance(reason, str) and reason


class TestEmptyResponseAttribution:
    def test_empty_exhausted_writes_failure_class_and_reason(self) -> None:
        """空回复耗尽裁决：置 failed 同时写 failure_class=empty_response。"""
        import asyncio

        mod = _load_module()
        plugin = mod.TaskReminder(config={"max_reminders": 10})
        result = asyncio.run(plugin.execute(
            _ctx(_base_state(llm_empty_exhausted=True, raw_result=""))
        ))
        assert result.state_updates["task.status"] == "failed"
        assert result.state_updates["task.failure_class"] == "empty_response"
        reason = result.state_updates.get("task.failure_reason")
        assert isinstance(reason, str) and reason

    def test_empty_exhausted_with_evidence_never_marks_failed(self) -> None:
        """完成证据在场：信号②先命中收束 completed，不写 failed/failure_class。"""
        import asyncio

        mod = _load_module()
        plugin = mod.TaskReminder(config={"max_reminders": 10})
        result = asyncio.run(plugin.execute(
            _ctx(_base_state(
                llm_empty_exhausted=True,
                raw_result="",
                task_evaluation_completed=True,
            ))
        ))
        # 信号②（完成证据）优先于空回复耗尽裁决——已完成态不可覆盖
        assert result.state_updates.get("task.status") == "completed"
        assert "task.failure_class" not in result.state_updates
        assert "task.failure_reason" not in result.state_updates
