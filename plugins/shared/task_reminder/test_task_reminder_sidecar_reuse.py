# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @vision: V3 可嵌入 | @ci: python-coverage
"""task_reminder sidecar 实例复用契约测试（per-pipeline 状态隔离）。

同 sidecar 进程内一个 TaskReminder 实例被多个 agent 管道并发/连续复用：
per-pipeline 预算与评估者模式一律从当次 ``state`` 读取，禁止实例字段固化。
2026-11-12 评估提醒误判实证：``evaluation_mode`` 曾写实例字段，被同宿任务
管道的 execute 覆盖 → 评估者收到执行者提醒（task_evaluate 不在其工具面）→
3 次提醒耗尽。本测试锁死：路由键 = 管道字段 ``evaluation.metric_id``。
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
_SHARED = _DIR.parents[0]  # plugins/shared/

for _d in [_DIR, _SHARED]:
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

from _libs.pipeline_base.plugin import PluginContext  # noqa: E402


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


def _appended_reminder(result: Any) -> dict[str, Any]:
    """提取提醒注入的消息本体（引擎契约：messages 更新只认 {"_ops":[…]} 声明，
    全量数组被 finalize 零兼容丢弃；缺 seq 的 set = append）。"""
    updates = result.state_updates["messages"]
    assert isinstance(updates, dict), (
        f"messages 更新必须是 ops 声明形态，实际: {type(updates).__name__}"
    )
    assert "_ops" in updates, f"messages 更新缺 _ops 键，实际键: {sorted(updates)}"
    ops = updates["_ops"]
    assert ops, "提醒 _ops 不应为空"
    last = ops[-1]
    assert last["op"] == "set", "提醒 op 应为 set（缺 seq = append）"
    assert "seq" not in last, "append 形态不应携带 seq（seq 由引擎分配）"
    return last["msg"]


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
        assert res_a.state_updates.get("ended") is True, "Agent A 提醒已达注入上限应 end"

        state_b = _base_state(evaluate_reminder_count=7)
        res_b = _run(plugin.execute(_ctx(state_b)))
        assert res_b.state_updates.get("ended") is not True, (
            f"Agent B 应继续注入提醒（残留上限会提前 end），实际 {res_b.state_updates}"
        )
        assert res_b.state_updates.get("_has_new_llm_input") is True
        assert res_b.state_updates["evaluate_reminder_count"] == 8

    def test_evaluation_mode_does_not_leak_across_agents(self) -> None:
        """Agent A 管道带 evaluation.metric_id；Agent B 同实例无该字段 →
        B 的纯文本输出必须走执行者文案，不可能残留评估者模式。"""
        mod = _load_plugin_module()
        plugin = mod.TaskReminder()

        state_a = _base_state(
            **{"evaluation.metric_id": "semantic_check"},
            raw_result="",
            raw_tool_calls=[{"function": {"name": "bash"}}],
            eval_tool_only_count=6,
        )
        res_a = _run(plugin.execute(_ctx(state_a)))
        appended_a = _appended_reminder(res_a).get("content", "")
        assert "评估" in appended_a, "Agent A 处于评估者模式，注入评估强制提醒"

        state_b = _base_state()
        res_b = _run(plugin.execute(_ctx(state_b)))
        appended_b = _appended_reminder(res_b)["content"]
        assert "【系统提醒" in appended_b, (
            f"Agent B 无管道字段时应使用执行者文案，实际残留了评估者模式: {appended_b[:30]}"
        )

    def test_evaluation_mode_is_pure_state_route(self) -> None:
        """路由函数只读当次 state，不持有实例级模式；交叠管道互不影响。"""
        mod = _load_plugin_module()
        plugin = mod.TaskReminder()
        eval_state = _base_state(**{"evaluation.metric_id": "semantic_check"})
        task_state = _base_state()
        assert plugin._is_evaluation_mode(eval_state) is True
        assert plugin._is_evaluation_mode(task_state) is False
        assert not hasattr(plugin, "_evaluation_mode")

    def test_eval_json_detected_after_task_pipeline_on_same_instance(self) -> None:
        """事故复现锁：同实例先跑任务管道的 execute，再跑评估管道——评估管道
        raw_result 里的合法 evaluation_result JSON 必须被检测并收束，不得被
        前一管道的执行者模式覆盖成提醒。"""
        mod = _load_plugin_module()
        plugin = mod.TaskReminder()

        # 任务管道（executor 模式）先执行：纯文本无完成证据 → 注入系统提醒
        task_state = _base_state(raw_result="任务阶段性输出")
        task_res = _run(plugin.execute(_ctx(task_state)))
        assert "【系统提醒" in _appended_reminder(task_res)["content"]

        # 评估管道随后执行：同一实例，必须按 evaluation.metric_id 走评估者分支
        eval_state = _base_state(
            **{"evaluation.metric_id": "semantic_check"},
            raw_result=(
                "评估完成。\n```json\n"
                '{"evaluation_result": {"passed": true, "score": 92, "feedback": "达标"}}'
                "\n```"
            ),
        )
        eval_res = _run(plugin.execute(_ctx(eval_state)))
        assert eval_res.state_updates.get("ended") is True
        assert eval_res.state_updates["evaluation.detected_result"]["passed"] is True
        assert "evaluate_reminder_count" not in eval_res.state_updates
