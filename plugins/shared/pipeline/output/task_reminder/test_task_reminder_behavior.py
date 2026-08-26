# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @vision: V3 可嵌入 | @ci: python-coverage
"""task_reminder 行为面测试——execute 主流程编排与静态解析器。

与 test_task_reminder_state.py（state 键契约）互补，本文件覆盖：

1. 任务状态推进（pending → running，幂等）；
2. 各跳过分支（core_type 非 llm_call / 有工具调用 / 无文本 / 会话模式）；
3. 评估模式 tool-only 计数与强制提醒（阈值 6、提醒上限、计数归零）；
4. 评估结论 JSON 检测 → end 信号；
5. task_evaluate 成功证据放行；
6. 提醒耗尽 → pending_evaluation 裁决（有评估证据则纯 end）；
7. 活跃子任务 task_service 回退（state 无标记时经服务查询，含枚举状态）；
8. executor/evaluator 两套提醒文案构建（验收标准两形态、打回原因）；
9. 静态解析器：_detect_evaluation_result_json / _has_successful_task_evaluate /
   _last_assistant_has_text。
"""
from __future__ import annotations

import enum
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parent
_SHARED = _DIR.parents[2]  # plugins/shared/

for _d in [_DIR, _SHARED]:
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

from pipeline.plugin import PluginContext  # noqa: E402

# 全车道共跑裸名 `plugin` 会被同目录/先收集目录的同名模块劫持——按显式
# 路径加载（与 test_task_reminder_state.py 同范式）。
_spec = importlib.util.spec_from_file_location(
    "task_reminder_plugin_behavior_test", str(_DIR / "plugin.py")
)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
sys.modules["task_reminder_plugin_behavior_test"] = _mod
_spec.loader.exec_module(_mod)
TaskReminder = _mod.TaskReminder  # noqa: E402


def _ctx(state: dict[str, Any], services: dict[str, Any] | None = None) -> PluginContext:
    return PluginContext(state=state, config={}, _services=services or {})


def _base_task_state(**over: Any) -> dict[str, Any]:
    """0.2 任务管道 state 基线（llm_call 轮 + 已注入 task.id）。"""
    base = {
        "core_type": "llm_call",
        "iteration": 3,
        "task.id": "task-abc",
        "task.status": "running",
        "agent_level": "L2",
        "raw_tool_calls": [],
        "raw_result": "阶段性输出",
        "messages": [],
    }
    base.update(over)
    return base


class _SubTaskStatus(enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"


class TestStatusAdvance:
    def test_pending_advances_to_running(self) -> None:
        import asyncio

        result = asyncio.run(TaskReminder().execute(_ctx(_base_task_state(**{"task.status": "pending"})))
        )
        assert result.state_updates == {"task.status": "running"}

    def test_non_pending_status_not_overwritten(self) -> None:
        import asyncio

        # running 状态不再推进——落入后续流程（有文本+无评估证据 → 注入提醒）
        result = asyncio.run(TaskReminder(config={"max_reminders": 3}).execute(
                _ctx(_base_task_state(**{"task.status": "running"}))
            )
        )
        assert "task.status" not in result.state_updates
        assert result.state_updates["evaluate_reminder_count"] == 1


class TestSkipBranches:
    def test_non_llm_call_core_type_skipped(self) -> None:
        import asyncio

        result = asyncio.run(TaskReminder().execute(_ctx(_base_task_state(core_type="tool_call")))
        )
        assert result.state_updates == {}
        assert result.route_signal is None

    def test_tool_calls_present_skipped(self) -> None:
        import asyncio

        result = asyncio.run(TaskReminder().execute(
                _ctx(_base_task_state(raw_tool_calls=[{"function": {"name": "bash"}}]))
            )
        )
        assert result.state_updates == {}

    def test_no_text_no_tools_skipped(self) -> None:
        import asyncio

        result = asyncio.run(TaskReminder().execute(_ctx(_base_task_state(raw_result="")))
        )
        assert result.state_updates == {}

    def test_conversation_mode_skipped(self) -> None:
        import asyncio

        result = asyncio.run(TaskReminder().execute(
                _ctx(_base_task_state(conversation_mode=True))
            )
        )
        assert result.state_updates == {}
        assert result.route_signal is None

    def test_last_assistant_message_text_counts_as_text(self) -> None:
        """评估模式下 raw_result 为空但最后 assistant 有文本 → 不进仅工具计数。"""
        import asyncio

        plugin = TaskReminder(config={"evaluation_mode": True, "max_reminders": 10})
        state = _base_task_state(
            raw_result="",
            raw_tool_calls=[{"function": {"name": "file_read"}}],
            messages=[
                {"role": "assistant", "content": "带结论文本", "tool_calls": []},
            ],
        )
        result = asyncio.run(plugin.execute(_ctx(state)))
        # 有文本 → 不计 eval_tool_only_count；随后 has_tool_calls 跳过
        assert result.state_updates == {}


class TestActiveChildrenServiceFallback:
    def _state(self) -> dict[str, Any]:
        return _base_task_state(messages=[])

    def test_active_subtask_via_service_blocks_reminder(self) -> None:
        import asyncio

        svc = SimpleNamespace(
            list_subtasks=lambda tid: [SimpleNamespace(status=_SubTaskStatus.RUNNING)]
        )
        result = asyncio.run(TaskReminder().execute(_ctx(self._state(), services={"task_service": svc}))
        )
        assert result.state_updates == {}
        assert result.route_signal is None

    def test_completed_subtasks_do_not_block(self) -> None:
        import asyncio

        svc = SimpleNamespace(
            list_subtasks=lambda tid: [SimpleNamespace(status=_SubTaskStatus.COMPLETED)]
        )
        result = asyncio.run(TaskReminder(config={"max_reminders": 3}).execute(
                _ctx(self._state(), services={"task_service": svc})
            )
        )
        assert result.state_updates.get("evaluate_reminder_count") == 1

    def test_string_status_also_recognized(self) -> None:
        import asyncio

        svc = SimpleNamespace(
            list_subtasks=lambda tid: [SimpleNamespace(status="pending")]
        )
        result = asyncio.run(TaskReminder().execute(_ctx(self._state(), services={"task_service": svc}))
        )
        assert result.state_updates == {}

    def test_no_service_no_import_path_returns_false(self) -> None:
        """服务未注册且进程内导入不可达 → 视为无活跃子任务，不抛。"""
        import asyncio

        result = asyncio.run(TaskReminder(config={"max_reminders": 3}).execute(_ctx(self._state()))
        )
        assert result.state_updates.get("evaluate_reminder_count") == 1


class TestEvaluationModeToolOnly:
    def _eval_state(self, **over: Any) -> dict[str, Any]:
        base = _base_task_state(
            raw_result="",
            raw_tool_calls=[{"function": {"name": "bash"}}],
            messages=[],
        )
        base.update(over)
        return base

    def test_below_threshold_counts_only(self) -> None:
        import asyncio

        plugin = TaskReminder(config={"evaluation_mode": True, "max_reminders": 10})
        result = asyncio.run(plugin.execute(_ctx(self._eval_state(eval_tool_only_count=2)))
        )
        assert result.state_updates == {"eval_tool_only_count": 3}
        assert result.route_signal is None

    def test_at_threshold_forces_reminder(self) -> None:
        import asyncio

        plugin = TaskReminder(config={"evaluation_mode": True, "max_reminders": 10})
        result = asyncio.run(plugin.execute(_ctx(self._eval_state(eval_tool_only_count=5)))
        )
        assert result.route_signal is not None
        assert result.route_signal.route_type == "next_llm"
        assert result.state_updates["evaluate_reminder_count"] == 1
        assert result.state_updates["eval_tool_only_count"] == 0
        appended = result.state_updates["messages"][-1]
        assert appended["role"] == "system"
        assert "evaluation_result" in appended["content"]

    def test_threshold_with_reminders_exhausted_only_counts(self) -> None:
        """提醒数已达上限时不再强制提醒，仅维持计数。"""
        import asyncio

        plugin = TaskReminder(config={"evaluation_mode": True, "max_reminders": 2})
        result = asyncio.run(plugin.execute(
                _ctx(
                    self._eval_state(
                        eval_tool_only_count=5, evaluate_reminder_count=2
                    )
                )
            )
        )
        assert result.state_updates == {"eval_tool_only_count": 6}
        assert result.route_signal is None


class TestEvaluationJsonDetection:
    def test_detected_json_sends_end(self) -> None:
        import asyncio

        plugin = TaskReminder(config={"evaluation_mode": True})
        raw = (
            "评估完成。\n```json\n"
            + json.dumps(
                {"evaluation_result": {"passed": True, "score": 88, "feedback": "达标"}}
            )
            + "\n```"
        )
        result = asyncio.run(plugin.execute(_ctx(_base_task_state(raw_result=raw)))
        )
        assert result.route_signal is not None
        assert result.route_signal.route_type == "end"
        detected = result.state_updates["evaluation.detected_result"]
        assert detected["passed"] is True
        assert detected["score"] == 88.0
        assert isinstance(detected["passed"], bool)
        assert isinstance(detected["score"], float)

    def test_plain_text_without_json_proceeds_to_reminder(self) -> None:
        import asyncio

        plugin = TaskReminder(config={"evaluation_mode": True, "max_reminders": 3})
        result = asyncio.run(plugin.execute(_ctx(_base_task_state(raw_result="还没评估完")))
        )
        assert result.state_updates.get("evaluate_reminder_count") == 1


class TestTaskEvaluateEvidence:
    def _messages_with_eval(self, success: str) -> list[dict[str, Any]]:
        return [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "call-1", "function": {"name": "task_evaluate"}}
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": success},
        ]

    def test_successful_evaluate_allows_end(self) -> None:
        import asyncio

        msgs = self._messages_with_eval('{"success": true, "data": {}}')
        result = asyncio.run(TaskReminder().execute(_ctx(_base_task_state(messages=msgs)))
        )
        assert result.state_updates == {}
        assert result.route_signal is None

    def test_failed_evaluate_still_reminds(self) -> None:
        import asyncio

        msgs = self._messages_with_eval('{"success": false}')
        result = asyncio.run(TaskReminder(config={"max_reminders": 3}).execute(
                _ctx(_base_task_state(messages=msgs))
            )
        )
        assert result.state_updates.get("evaluate_reminder_count") == 1

    def test_compact_success_literal_recognized(self) -> None:
        assert (
            TaskReminder._has_successful_task_evaluate(
                [
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {"id": "x", "function": {"name": "task_evaluate"}}
                        ],
                    },
                    {"role": "tool", "tool_call_id": "x", "content": '{"success":true}'},
                ]
            )
            is True
        )
        assert TaskReminder._has_successful_task_evaluate([]) is False
        assert (
            TaskReminder._has_successful_task_evaluate(
                [{"role": "assistant", "tool_calls": None}]
            )
            is False
        )


class TestMaxRemindersExhausted:
    def test_exhausted_without_evidence_marks_pending_evaluation(self) -> None:
        import asyncio

        plugin = TaskReminder(config={"max_reminders": 2})
        result = asyncio.run(plugin.execute(
                _ctx(_base_task_state(evaluate_reminder_count=2))
            )
        )
        assert result.route_signal is not None
        assert result.route_signal.route_type == "end"
        assert result.state_updates["task.status"] == "pending_evaluation"

    def test_exhausted_with_detected_result_ends_cleanly(self) -> None:
        import asyncio

        plugin = TaskReminder(config={"max_reminders": 2})
        result = asyncio.run(plugin.execute(
                _ctx(
                    _base_task_state(
                        evaluate_reminder_count=2,
                        **{"evaluation.detected_result": {"passed": True}},
                    )
                )
            )
        )
        assert result.route_signal is not None
        assert result.route_signal.route_type == "end"
        assert "task.status" not in result.state_updates


class TestReminderBuilding:
    def test_executor_reminder_lists_acceptance_criteria(self) -> None:
        import asyncio

        state = _base_task_state(
            acceptance_criteria=[
                {"description": "测试全绿", "metric_id": "m1"},
                "文档已更新",
            ],
            reject_count=1,
            reject_reason="覆盖不足",
        )
        result = asyncio.run(TaskReminder(config={"max_reminders": 5}).execute(_ctx(state))
        )
        content = result.state_updates["messages"][-1]["content"]
        assert "task_evaluate" in content
        assert "测试全绿" in content
        assert "文档已更新" in content
        assert "打回" in content
        assert "覆盖不足" in content

    def test_executor_reminder_without_criteria_still_actionable(self) -> None:
        import asyncio

        result = asyncio.run(TaskReminder(config={"max_reminders": 5}).execute(
                _ctx(_base_task_state())
            )
        )
        content = result.state_updates["messages"][-1]["content"]
        assert "auto_complete" in content
        assert "打回" not in content

    def test_evaluator_reminder_includes_metric_thresholds(self) -> None:
        import asyncio

        plugin = TaskReminder(config={"evaluation_mode": True, "max_reminders": 5})
        state = _base_task_state(
            acceptance_criteria=[{"metric_id": "coverage", "pass_threshold": 0.9}]
        )
        result = asyncio.run(plugin.execute(_ctx(state))
        )
        content = result.state_updates["messages"][-1]["content"]
        assert "【评估提醒" in content
        assert "coverage" in content
        assert "0.9" in content


class TestDetectEvaluationResultJson:
    def test_nested_wrapper_and_top_level_both_recognized(self) -> None:
        wrapped = TaskReminder._detect_evaluation_result_json(
            'prefix {"evaluation_result": {"passed": false, "score": 40}} suffix'
        )
        assert wrapped is not None
        assert wrapped["passed"] is False
        assert wrapped["score"] == 40.0
        assert wrapped["feedback"] == ""

        direct = TaskReminder._detect_evaluation_result_json(
            '{"passed": true, "score": 100, "feedback": "满分"}'
        )
        assert direct is not None
        assert direct["passed"] is True
        assert direct["feedback"] == "满分"

    def test_invalid_json_and_missing_passed_rejected(self) -> None:
        assert TaskReminder._detect_evaluation_result_json("no braces at all") is None
        assert TaskReminder._detect_evaluation_result_json('{"other": 1}') is None
        # 非法 JSON 候选被跳过，其后仍可命中合法候选
        mixed = TaskReminder._detect_evaluation_result_json(
            '{"broken": ,} {"passed": true}'
        )
        assert mixed is not None
        assert mixed["passed"] is True

    def test_last_valid_candidate_wins(self) -> None:
        text = '{"passed": false} tail {"evaluation_result": {"passed": true}}'
        got = TaskReminder._detect_evaluation_result_json(text)
        assert got is not None
        assert got["passed"] is True

    def test_suggestions_passthrough_and_score_default(self) -> None:
        got = TaskReminder._detect_evaluation_result_json(
            '{"evaluation_result": {"passed": true, "suggestions": ["a", "b"]}}'
        )
        assert got is not None
        assert got["suggestions"] == ["a", "b"]
        assert got["score"] == 0.0


class TestRuntimeConfigOverride:
    def test_state_max_reminders_overrides_constructor(self) -> None:
        import asyncio

        plugin = TaskReminder(config={"max_reminders": 10})
        result = asyncio.run(plugin.execute(
                _ctx(_base_task_state(max_reminders=1, evaluate_reminder_count=1))
            )
        )
        # 运行时上限 1 已达 → pending_evaluation 而非注入第 2 次提醒
        assert result.state_updates.get("task.status") == "pending_evaluation"

    def test_plugin_configs_evaluation_mode_switch(self) -> None:
        import asyncio

        plugin = TaskReminder(config={"evaluation_mode": False})
        state = _base_task_state(
            plugin_configs={"task_reminder": {"evaluation_mode": True}},
            raw_result="",
            raw_tool_calls=[{"function": {"name": "bash"}}],
        )
        result = asyncio.run(plugin.execute(_ctx(state)))
        assert result.state_updates == {"eval_tool_only_count": 1}

    def test_identity_properties(self) -> None:
        plugin = TaskReminder(config={"priority": 50})
        assert plugin.name == "task_reminder"
        assert plugin.priority == 50
        assert TaskReminder().priority == 35


class TestLastAssistantHasText:
    def test_variants(self) -> None:
        assert (
            TaskReminder._last_assistant_has_text(
                {"messages": [{"role": "assistant", "content": "有话"}]}
            )
            is True
        )
        assert (
            TaskReminder._last_assistant_has_text(
                {"messages": [{"role": "assistant", "content": "   "}]}
            )
            is False
        )
        assert TaskReminder._last_assistant_has_text({"messages": []}) is False
        assert (
            TaskReminder._last_assistant_has_text(
                {"messages": ["非 dict 消息", {"role": "user", "content": "x"}]}
            )
            is False
        )
