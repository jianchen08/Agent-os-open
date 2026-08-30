# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @vision: V3 可嵌入 | @ci: python-coverage
"""task_reminder 三信号收束判据测试（ADR 2026-08-28-task-closure-three-signal-gate）。

收束判据 = 三信号按序短路，全部读 state / 对话结构，不解析渲染文本形态：
① 本轮有工具调用 → 路由工具执行，不评判；
② state 完成证据（task.status == completed，或 task_evaluate 结构化工具结果
   的当轮投影 task_evaluation_completed）→ 当轮收束，补落终态，提醒不注入；
③ 存在未回子任务挂号键 → 本轮收束等待唤醒，不催评估；
④ 三信号皆否 → 注入提醒；耗尽裁决改写终态前必须复查②，已完成态不可覆盖。

背景（同日管道 e02ad39a8c5a/a3fddc00da33 实证）：证据识别只认 messages 里
role=tool 的 JSON，而真实路径 LLM 面被 result_format YAML 化——成功任务被
提醒耗尽改判 failed。本文件锁定「证据以 state 为准」的新契约。
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
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

# 全车道共跑时裸名 `plugin` 会被先收集目录的同名模块劫持，
# 按 _DIR 显式路径加载（与 test_task_reminder_state.py 同范式）。
_spec = importlib.util.spec_from_file_location(
    "task_reminder_plugin_three_signal_test", str(_DIR / "plugin.py")
)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
sys.modules["task_reminder_plugin_three_signal_test"] = _mod
_spec.loader.exec_module(_mod)
TaskReminder = _mod.TaskReminder  # noqa: E402


def _ctx(state: dict[str, Any]) -> PluginContext:
    """构造最小 PluginContext（state 直传，不接 task_service）。"""
    return PluginContext(state=state, config={})


def _base_task_state(**over: Any) -> dict[str, Any]:
    """0.2 任务管道 state 基线（llm_call 轮 + 引擎注入 task.id + 纯文本轮）。"""
    base = {
        "core_type": "llm_call",
        "iteration": 3,
        "task.id": "task-abc",
        "task.status": "running",
        "agent_level": "L2",
        "raw_tool_calls": [],
        "raw_result": "阶段性文本输出",
        "messages": [],
    }
    base.update(over)
    return base


def _is_iso_datetime(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


class TestSignalOneToolCallsRouteToTools:
    """信号①：本轮有工具调用 → 路由工具执行，不评判（ADR 原语义）。

    工具重复调用/死循环防护不在本插件：由 duplicate_check 按
    「同工具+相同参数」签名承接（ADR 2026-08-30-retire-tool-fail-streak-gate）。
    """

    @pytest.mark.parametrize(
        "tool_results",
        [
            [],
            [{"tool_name": "bash", "success": False, "error": "boom"}],
            [{"tool_name": "bash", "success": True, "data": {}}],
        ],
        ids=["no-results", "failed-results", "ok-results"],
    )
    def test_tool_call_round_routes_to_tools(self, tool_results: list) -> None:
        """有工具调用的轮次零评判放行（参数化：无结果/全失败/全成功）。

        上一轮工具成败不影响本轮路由——工具轮不做任何收束评判，亦不注入
        提醒、不消耗提醒配额；混合轮（文本+工具调用）同样按①放行。
        """
        import asyncio

        reminder = TaskReminder()
        state = _base_task_state(
            raw_tool_calls=[{"function": {"name": "bash"}}],
            raw_result="",
            tool_results=tool_results,
        )
        result = asyncio.run(reminder.execute(_ctx(state)))
        assert result.state_updates == {}, "信号①轮零评判零副作用"
        assert "ended" not in result.state_updates and "suspended" not in result.state_updates

    def test_mixed_text_and_tool_call_round_routes_to_tools(self) -> None:
        """混合轮（文本+工具调用）按信号①放行：不进提醒级联、不耗配额。"""
        import asyncio

        reminder = TaskReminder(config={"max_reminders": 3})
        state = _base_task_state(
            raw_tool_calls=[{"function": {"name": "task_submit"}}],
            raw_result="参数已修正，重新派发。",
        )
        result = asyncio.run(reminder.execute(_ctx(state)))
        assert result.state_updates == {}
        assert "evaluate_reminder_count" not in result.state_updates
        assert "messages" not in result.state_updates


class TestSignalTwoCompletionEvidence:
    """信号②：state 完成证据 → 当轮收束，提醒不注入。"""

    def test_completed_status_ends_round_without_reminder(self) -> None:
        """task.status == completed（写面已落）→ 当轮 end，不注入提醒。"""
        import asyncio

        reminder = TaskReminder()
        result = asyncio.run(
            reminder.execute(_ctx(_base_task_state(**{"task.status": "completed"})))
        )
        assert result.state_updates.get("ended") is True
        # 性质：收束轮零提醒注入（messages 不增、计数不动）
        assert "messages" not in result.state_updates
        assert "evaluate_reminder_count" not in result.state_updates
        assert result.state_updates.get("_has_new_llm_input") is False

    def test_structured_flag_with_stale_status_completes_task(self) -> None:
        """真实路径（08-28 实证）：task_evaluate 成功的写面写在注册表/DB，
        在飞 state 当轮仍 running——tool_core 从结构化工具结果派生的
        task_evaluation_completed 即同一裁决的当轮证据 → 补落 completed 并 end。"""
        import asyncio

        reminder = TaskReminder()
        result = asyncio.run(
            reminder.execute(_ctx(_base_task_state(task_evaluation_completed=True)))
        )
        assert result.state_updates.get("ended") is True
        # 补落终态（与耗尽裁决写 failed 同通路对称）
        assert result.state_updates.get("task.status") == "completed"
        assert _is_iso_datetime(result.state_updates.get("task.ended_at"))
        assert "messages" not in result.state_updates

    def test_completion_rewrite_idempotent_when_already_completed(self) -> None:
        """性质：终态已落时补落幂等——不重复写 task.status/ended_at。"""
        import asyncio

        reminder = TaskReminder()
        result = asyncio.run(
            reminder.execute(
                _ctx(
                    _base_task_state(
                        **{"task.status": "completed", "task_evaluation_completed": True}
                    )
                )
            )
        )
        assert result.state_updates.get("ended") is True
        assert "task.status" not in result.state_updates
        assert "task.ended_at" not in result.state_updates

    def test_running_without_evidence_still_reminds(self) -> None:
        """对照组：running + 无任何完成证据 → 不触发②，照常进入提醒级联。"""
        import asyncio

        reminder = TaskReminder(config={"max_reminders": 3})
        result = asyncio.run(reminder.execute(_ctx(_base_task_state())))
        assert result.state_updates.get("evaluate_reminder_count") == 1
        assert result.state_updates.get("_has_new_llm_input") is True


class TestExhaustionNeverOverwritesCompletion:
    """④耗尽裁决改写终态前必须复查②：已完成态不可覆盖为 failed（08-28 事故面）。"""

    @pytest.mark.parametrize(
        "evidence,expect_failed",
        [
            ({"task.status": "completed"}, False),
            ({"task_evaluation_completed": True}, False),
        ],
        ids=["status-completed", "structured-flag"],
    )
    def test_exhausted_with_completion_evidence_never_fails(
        self, evidence: dict[str, Any], expect_failed: bool
    ) -> None:
        """提醒耗尽 + state 完成证据 → ②按序短路先收束，绝不落 failed。

        status 已落：②幂等 end，无终态改写；structured-flag：②补落 completed。
        （两种路径 task.status 都不是 failed——"完成被覆盖为 failed"不可达。）
        """
        import asyncio

        reminder = TaskReminder(config={"max_reminders": 2})
        result = asyncio.run(
            reminder.execute(_ctx(_base_task_state(evaluate_reminder_count=2, **evidence)))
        )
        assert result.state_updates.get("ended") is True
        assert (result.state_updates.get("task.status") == "failed") is expect_failed
        if not expect_failed and "task.status" in result.state_updates:
            # 补落路径：写的一定是 completed，且带 ISO ended_at
            assert result.state_updates["task.status"] == "completed"
            assert _is_iso_datetime(result.state_updates.get("task.ended_at"))

    def test_exhausted_without_evidence_marks_failed(self) -> None:
        """对照组：耗尽且零证据 → failed 裁决保持（漏评任务不放行）。"""
        import asyncio

        reminder = TaskReminder(config={"max_reminders": 2})
        result = asyncio.run(
            reminder.execute(_ctx(_base_task_state(evaluate_reminder_count=2)))
        )
        assert result.state_updates.get("ended") is True
        assert result.state_updates.get("task.status") == "failed"
        assert _is_iso_datetime(result.state_updates.get("task.ended_at"))


class TestSignalThreePendingSubtaskRegistration:
    """信号③：存在未回子任务挂号键 → 本轮收束等待唤醒，不催评估。

    挂号键 = task_submit 写入提交者管道的 ``task.subtasks_pending.<task_id>``
    （值 = 提交时间戳）；子任务终态事件经 task_service 写 null 清除。
    """

    def test_pending_registration_key_ends_round_waiting(self) -> None:
        import asyncio

        reminder = TaskReminder()
        result = asyncio.run(
            reminder.execute(
                _ctx(
                    _base_task_state(
                        raw_result="已提交子任务，等待完成回执。",
                        **{"task.subtasks_pending.task-child-1": "2026-08-28T10:00:00+00:00"},
                    )
                )
            )
        )
        assert result.state_updates.get("suspended") is True
                # 收束轮零提醒注入（不催评估）
        assert "messages" not in result.state_updates
        assert "evaluate_reminder_count" not in result.state_updates
        assert result.state_updates.get("_has_new_llm_input") is False

    def test_cleared_registration_null_value_does_not_wait(self) -> None:
        """子任务已回执（键写 null 清除）→ 视为无挂号，照常进入提醒级联。"""
        import asyncio

        reminder = TaskReminder(config={"max_reminders": 3})
        result = asyncio.run(
            reminder.execute(
                _ctx(
                    _base_task_state(
                        **{"task.subtasks_pending.task-child-1": None}
                    )
                )
            )
        )
        assert result.state_updates.get("evaluate_reminder_count") == 1

    def test_mixed_keys_any_truthy_registration_waits(self) -> None:
        """性质：多笔挂号中任一未回（真值）即等待；全部回执（null）才继续。"""
        import asyncio

        reminder = TaskReminder()

        mixed = _base_task_state(
            **{
                "task.subtasks_pending.task-child-1": None,
                "task.subtasks_pending.task-child-2": "2026-08-28T10:30:00+00:00",
            }
        )
        waiting = asyncio.run(reminder.execute(_ctx(mixed)))
        assert waiting.state_updates.get("suspended") is True

        all_cleared = _base_task_state(
            **{
                "task.subtasks_pending.task-child-1": None,
                "task.subtasks_pending.task-child-2": None,
            }
        )
        reminded = asyncio.run(
            TaskReminder(config={"max_reminders": 3}).execute(_ctx(all_cleared))
        )
        assert reminded.state_updates.get("evaluate_reminder_count") == 1

    def test_signal_two_short_circuits_before_registration_wait(self) -> None:
        """按序短路：②完成证据与③挂号同场时②先收束（补落 completed）。"""
        import asyncio

        reminder = TaskReminder()
        result = asyncio.run(
            reminder.execute(
                _ctx(
                    _base_task_state(
                        task_evaluation_completed=True,
                        **{"task.subtasks_pending.task-child-1": "2026-08-28T10:00:00+00:00"},
                    )
                )
            )
        )
        assert result.state_updates.get("ended") is True
        assert result.state_updates.get("task.status") == "completed"


class TestSecondaryEvidenceDemotedButKept:
    """messages JSON 文本检测降为次级证据保留（文本形态契约脆弱，ADR 被否项①）。"""

    def _messages_with_eval(self, content: str) -> list[dict[str, Any]]:
        return [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "function": {"name": "task_evaluate"}}],
            },
            {"role": "tool", "tool_call_id": "c1", "content": content},
        ]

    def test_secondary_json_evidence_still_allows_end(self) -> None:
        """次级证据（messages role=tool JSON success=true）在场 → 放行结束。"""
        import asyncio

        reminder = TaskReminder()
        result = asyncio.run(
            reminder.execute(
                _ctx(_base_task_state(messages=self._messages_with_eval('{"success": true}')))
            )
        )
        assert result.state_updates.get("_has_new_llm_input") is False

    def test_secondary_evidence_with_completed_state_secondary_not_needed(self) -> None:
        """性质：主证据（②）优先于次级证据——两者同场时仍走②收束（补落幂等）。"""
        import asyncio

        reminder = TaskReminder()
        result = asyncio.run(
            reminder.execute(
                _ctx(
                    _base_task_state(
                        **{"task.status": "completed"},
                        messages=self._messages_with_eval('{"success": true}'),
                    )
                )
            )
        )
        assert result.state_updates.get("ended") is True
        assert "task.status" not in result.state_updates
