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
_SHARED = _PLUGIN_DIR.parents[0]  # plugins/shared/（`_libs` 包解析根）
for _d in (str(_PLUGIN_DIR), str(_SHARED)):
    if _d not in sys.path:
        sys.path.insert(0, _d)


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


# kernel/crates/invoker/src/shared.rs STATE_SLICE_CONTROL_KEYS（恒带控制键）。
# 漂移时以 Rust 侧为真源同步本表（agent_config_load 同款镜像先例）。
_CONTROL_KEYS = {
    "pipeline_id",
    "session_id",
    "thread_id",
    "message_id",
    "run_id",
    "task_id",
    "workspace",
    "conversation_mode",
    "core_type",
    "core_plugin",
    "suspended",
    "ended",
    "next_phase",
    "agent.id",
    "model_tier",
    "agent_level",
}


def _manifest_reads() -> list[str]:
    """manifest state.reads 原始声明（切片条目 messages_tail:N 原样保留）。"""
    import json

    manifest = json.loads((_PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    reads = (manifest.get("state") or {}).get("reads") or []
    assert reads, "state.reads 已声明（非空声明 = 切片投喂前提），不得回退全量"
    return list(reads)


def _read_keys(reads: list[str]) -> set[str]:
    """声明集归一（messages_tail:N 投喂键仍是 messages）。"""
    return {"messages" if r.startswith("messages_tail:") else r for r in reads}


def _slice_feed(full_state: dict[str, Any], reads: list[str]) -> dict[str, Any]:
    """按内核切片契约组装投喂 state（控制键白名单 ∪ 声明键；缺键省略）。"""
    out: dict[str, Any] = {k: full_state[k] for k in _CONTROL_KEYS if k in full_state}
    for entry in reads:
        if entry.startswith("messages_tail:"):
            tail = int(entry.split(":", 1)[1])
            msgs = full_state.get("messages")
            if isinstance(msgs, list):
                out["messages"] = msgs[-tail:] if tail else []
        elif entry in full_state:
            out[entry] = full_state[entry]
    return out


class TestSliceFeedAttribution:
    """切片投喂下的失败归因（2026-11-11 装机事故同款回归面）。

    引擎按 manifest ``state.reads`` 切片投喂（控制键白名单 ∪ 声明键），未声明
    键插件恒读不到且静默缺省——归因写面依赖的身份键（task.id）漏声明会让
    failure_class/failure_reason 静默不写（duplicate_check 事故根因，本插件
    同域回归锁）。
    """

    def test_task_identity_declared_for_attribution(self) -> None:
        """提醒耗尽裁决以 task.id 在场为前提——必须声明，切片后才可见。"""
        keys = _read_keys(_manifest_reads())
        assert "task.id" in keys, (
            "task.id 未在 state.reads 声明 → 切片后提醒耗尽裁决视为无任务身份，"
            "failure_class/failure_reason 静默不写"
        )
        assert "task.status" in keys

    def test_exhausted_writes_nonempty_attribution_under_slice(self) -> None:
        """按声明切片投喂（与真实管道同形）→ 耗尽裁决仍落非空 class/reason。"""
        import asyncio

        fed = _slice_feed(_base_state(evaluate_reminder_count=2), _manifest_reads())
        assert "task.id" in fed, "切片后任务身份必须可见"

        mod = _load_module()
        plugin = mod.TaskReminder(config={"max_reminders": 2})
        result = asyncio.run(plugin.execute(mod.PluginContext(state=fed, config={})))

        updates = result.state_updates
        assert updates["task.status"] == "failed"
        assert updates["task.failure_class"] == "reminder_exhausted"
        reason = updates.get("task.failure_reason")
        assert isinstance(reason, str), "失败原因必须是非空字符串"
        assert reason.strip(), "失败原因不得为空"

