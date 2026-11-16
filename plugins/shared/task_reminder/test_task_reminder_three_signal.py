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

DB 权威终态（2026-10-19 幽灵续跑改判事故 run 5f5ec503 修复③）：写面分裂
场景（task_evaluate 经 pipeline-state 写面只落 DB，恢复 state 无完成证据）
下，本管道聚合行（task_id==pipeline_id）的 task.status 与 state 证据同权——
completed 参与信号②（当轮收束补落）与耗尽裁决复查面（不写 failed）；
reader 未注入/行缺失/行无 task.status → None，全部退化为 state-only 现状
行为（保守）。DB failed/running 不构成完成证据，裁决行为不变。
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
_SHARED = _DIR.parents[0]  # plugins/shared/

for _d in [_DIR, _SHARED]:
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

from _libs.pipeline_base.plugin import PluginContext  # noqa: E402

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


def _db_rows(status: str | None, pipeline_id: str = "task-abc") -> list[dict[str, Any]]:
    """本管道聚合行（task_id==pipeline_id）最小替身（扁平点号键，reader 契约形态）。

    ``status=None`` 模拟行在但无 task.status（评估者模式管道自身行形态）。
    """
    row: dict[str, Any] = {"pipeline_id": pipeline_id}
    if status is not None:
        row["task.status"] = status
    return [row]


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
        # 信号①轮零评判零副作用（产出基线记账键除外——包装层幂等推进）
        assert set(result.state_updates) - {_mod._RESULT_FP_KEY} == set(), (
            "信号①轮零评判零副作用，实际 %r" % (result.state_updates,)
        )
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
        assert set(result.state_updates) - {_mod._RESULT_FP_KEY} == set()
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


class TestDbAuthoritativeCompletion:
    """DB 权威终态（2026-10-19 幽灵续跑改判事故修复③）。

    写面分裂：task_evaluate 把 task.status=completed 经 pipeline-state 写面只落
    DB，恢复轮 state 无任何完成证据——闸门信号②与耗尽复查面（state 视角）全部
    失明，把已完成任务改判 failed 并空转烧 token。修复：本管道聚合行
    （task_id==pipeline_id）的 task.status 经 state 读取器同权参与两处判定。
    """

    def test_db_completed_closes_round_without_reminder(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """信号②位：DB completed + state 无证据 → 当轮收束补落 completed，不催促。

        本事故幽灵轮的根治面：续跑轮第一轮即干净收束（不再 3 次催促 4 轮空转）。
        """
        import asyncio

        monkeypatch.setattr(_mod, "_get_state_reader", lambda: _db_rows("completed"))
        state = _base_task_state(pipeline_id="task-abc")  # state: running，无任何证据
        result = asyncio.run(TaskReminder().execute(_ctx(state)))
        assert result.state_updates.get("ended") is True
        # 补落 completed（写面 mid-run 落 DB，state 补写同一裁决，与耗尽写 failed 同通路对称）
        assert result.state_updates.get("task.status") == "completed"
        assert _is_iso_datetime(result.state_updates.get("task.ended_at"))
        # 收束轮零提醒注入
        assert "messages" not in result.state_updates
        assert "evaluate_reminder_count" not in result.state_updates
        assert result.state_updates.get("_has_new_llm_input") is False

    def test_db_completed_total_budget_exhaustion_never_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """本事故 replay：state 无证据 + DB completed → 耗尽路径不写 failed。

        总预算触顶（提醒循环跨 run 累计继承）先于信号②裁决：DB completed 在场
        复查面放行——不覆盖终态（既不写 failed 也不重复补落），保持现状 end 本轮。
        """
        import asyncio

        monkeypatch.setattr(_mod, "_get_state_reader", lambda: _db_rows("completed"))
        state = _base_task_state(
            pipeline_id="task-abc",
            evaluate_reminder_total=15,  # ≥ 缺省任务级总预算（评估不收敛触顶）
        )
        result = asyncio.run(TaskReminder().execute(_ctx(state)))
        assert result.state_updates.get("ended") is True
        assert "task.status" not in result.state_updates
        assert "task.ended_at" not in result.state_updates
        assert result.state_updates.get("_has_new_llm_input") is False

    def test_reader_none_keeps_failed_verdict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """回归保护：reader 未注入 → 全部退化为现状，耗尽照旧写 failed。"""
        import asyncio

        monkeypatch.setattr(_mod, "_get_state_reader", lambda: None)
        state = _base_task_state(evaluate_reminder_total=15)
        result = asyncio.run(TaskReminder().execute(_ctx(state)))
        assert result.state_updates.get("task.status") == "failed"
        assert result.state_updates.get("ended") is True

    def test_db_running_keeps_failed_verdict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """防误伤：DB running 不构成完成证据，耗尽失败裁决照旧。"""
        import asyncio

        monkeypatch.setattr(_mod, "_get_state_reader", lambda: _db_rows("running"))
        state = _base_task_state(evaluate_reminder_total=15)
        result = asyncio.run(TaskReminder().execute(_ctx(state)))
        assert result.state_updates.get("task.status") == "failed"

    def test_db_failed_status_keeps_failed_verdict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """边界：DB failed 不当完成证据（失败重试轮行为与现状一致）。"""
        import asyncio

        monkeypatch.setattr(_mod, "_get_state_reader", lambda: _db_rows("failed"))
        state = _base_task_state(evaluate_reminder_total=15)
        result = asyncio.run(TaskReminder().execute(_ctx(state)))
        assert result.state_updates.get("task.status") == "failed"

    def test_db_row_missing_degrades_to_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """诚实降级：聚合行缺失（非本管道行）→ None → 现状 failed 裁决。"""
        import asyncio

        monkeypatch.setattr(
            _mod,
            "_get_state_reader",
            lambda: [{"pipeline_id": "other-pipe", "task.status": "completed"}],
        )
        state = _base_task_state(evaluate_reminder_total=15)
        result = asyncio.run(TaskReminder().execute(_ctx(state)))
        assert result.state_updates.get("task.status") == "failed"

    def test_db_row_without_status_degrades_to_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """诚实降级：行在但无 task.status（评估者模式管道自身行形态）→ None → 现状。"""
        import asyncio

        monkeypatch.setattr(_mod, "_get_state_reader", lambda: _db_rows(None))
        state = _base_task_state(evaluate_reminder_total=15)
        result = asyncio.run(TaskReminder().execute(_ctx(state)))
        assert result.state_updates.get("task.status") == "failed"


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
    """messages 通过证据检测降为次级收束回退（文本形态契约脆弱，ADR 被否项①）。

    信号②（state 投影键 / DB 权威终态）缺席时，messages 里「评估通过」证据
    （result=completed）当轮收束补落终态——不再走旧「放行不收束」形态
    （D2 路由下 = 无监督空转循环）；非 completed 裁决不构成证据。
    """

    def _messages_with_eval(self, content: str) -> list[dict[str, Any]]:
        return [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "function": {"name": "task_evaluate"}}],
            },
            {"role": "tool", "tool_call_id": "c1", "content": content},
        ]

    def _envelope_completed(self) -> list[dict[str, Any]]:
        """tool_core 正路信封形态：success + result=completed（content 是 YAML data）。"""
        return [
            {
                "role": "tool",
                "tool_call_id": "c1",
                "content": "metrics: []\noverall_passed: true\n",
                "tool_result": {
                    "call_id": "c1",
                    "tool_name": "task_evaluate",
                    "success": True,
                    "error": None,
                    "data": {"overall_passed": True},
                    "metadata": {"action": "auto_complete", "result": "completed"},
                },
            }
        ]

    def test_secondary_pass_evidence_closes_round(self) -> None:
        """次级通过证据（content JSON completed）在场 → 当轮收束补落 completed。"""
        import asyncio

        reminder = TaskReminder()
        result = asyncio.run(
            reminder.execute(
                _ctx(
                    _base_task_state(
                        messages=self._messages_with_eval(
                            '{"success": true, "metadata": {"result": "completed"}}'
                        )
                    )
                )
            )
        )
        assert result.state_updates.get("ended") is True
        assert result.state_updates.get("task.status") == "completed"
        # 收束轮零提醒注入
        assert "messages" not in result.state_updates
        assert "evaluate_reminder_count" not in result.state_updates

    def test_envelope_pass_evidence_closes_round(self) -> None:
        """信封形态通过证据（tool_core 正路，无需 id 配对）→ 当轮收束补落。"""
        import asyncio

        reminder = TaskReminder()
        result = asyncio.run(
            reminder.execute(_ctx(_base_task_state(messages=self._envelope_completed())))
        )
        assert result.state_updates.get("ended") is True
        assert result.state_updates.get("task.status") == "completed"

    def test_retry_evidence_not_pass_keeps_reminding(self) -> None:
        """对照：评估未通过（result=retry）不构成通过证据 → 提醒级联照常。"""
        import asyncio

        reminder = TaskReminder(config={"max_reminders": 3})
        result = asyncio.run(
            reminder.execute(
                _ctx(
                    _base_task_state(
                        messages=self._messages_with_eval(
                            '{"success": true, "metadata": {"result": "retry"}}'
                        )
                    )
                )
            )
        )
        assert result.state_updates.get("ended") is None
        assert result.state_updates.get("evaluate_reminder_count") == 1

    def test_secondary_evidence_with_completed_state_secondary_not_needed(self) -> None:
        """性质：主证据（②）优先于次级证据——两者同场时仍走②收束（补落幂等）。"""
        import asyncio

        reminder = TaskReminder()
        result = asyncio.run(
            reminder.execute(
                _ctx(
                    _base_task_state(
                        **{"task.status": "completed"},
                        messages=self._messages_with_eval(
                            '{"success": true, "metadata": {"result": "completed"}}'
                        ),
                    )
                )
            )
        )
        assert result.state_updates.get("ended") is True
        assert "task.status" not in result.state_updates


class TestPassEvidenceScopedToCurrentAttempt:
    """通过证据按「本次尝试」作用域化（ADR 2026-10-28 D4-3）。

    事故（子任务 9746ad7e2386，2026-10-28）：返工重开后返工轮跑到第 3 次
    LLM 调用挂死超时，提醒器却在**消息历史**里翻出**上一轮**的 task_evaluate
    通过信封，把返工轮按「已完成」收束（task.status=completed + ended=true），
    面板显示与实况相反。

    锚点 = 最后一条 user 消息（返工指令经 chat.send_message 注入 → 引擎待
    输入队列 → round 边界落 role=user 消息，历史天然按尝试分段）；正常轮次
    （任务提示本身即最后一条 user 消息）行为不变；消息面无 user 消息时锚点
    缺席 = 全量扫描（现状行为）。
    """

    def _envelope_completed(self) -> list[dict[str, Any]]:
        """tool_core 正路信封形态通过证据（success + result=completed）。"""
        return [
            {
                "role": "tool",
                "tool_call_id": "c1",
                "content": "metrics: []\noverall_passed: true\n",
                "tool_result": {
                    "call_id": "c1",
                    "tool_name": "task_evaluate",
                    "success": True,
                    "error": None,
                    "data": {"overall_passed": True},
                    "metadata": {"action": "auto_complete", "result": "completed"},
                },
            }
        ]

    def _content_json_completed(self) -> list[dict[str, Any]]:
        """旧序列化形态通过证据（assistant tool_calls + tool content JSON）。"""
        return [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "function": {"name": "task_evaluate"}}],
            },
            {
                "role": "tool",
                "tool_call_id": "c1",
                "content": '{"success": true, "metadata": {"result": "completed"}}',
            },
        ]

    def test_previous_attempt_pass_evidence_does_not_close_new_attempt(self) -> None:
        """历史里有上一轮通过证据 + 其后有用户消息（返工指令）→ 不按已完成收束。"""
        import asyncio

        messages = [
            {"role": "user", "content": "任务提示"},
            *self._envelope_completed(),
            {"role": "user", "content": "返工重开指令：上一轮做错了，重做"},
        ]
        reminder = TaskReminder(config={"max_reminders": 3})
        result = asyncio.run(
            reminder.execute(_ctx(_base_task_state(messages=messages)))
        )
        # 漏判方向安全：回退到正常提醒级联（预算受限），不收束、不补落 completed
        assert result.state_updates.get("ended") is None
        assert "task.status" not in result.state_updates
        assert result.state_updates.get("evaluate_reminder_count") == 1

    def test_previous_attempt_content_json_evidence_also_scoped(self) -> None:
        """旧序列化形态的上一轮通过证据同样不得收束新一轮（两种证据形态同口径）。"""
        import asyncio

        messages = [
            {"role": "user", "content": "任务提示"},
            *self._content_json_completed(),
            {"role": "user", "content": "返工重开指令"},
        ]
        reminder = TaskReminder(config={"max_reminders": 3})
        result = asyncio.run(
            reminder.execute(_ctx(_base_task_state(messages=messages)))
        )
        assert result.state_updates.get("ended") is None
        assert result.state_updates.get("evaluate_reminder_count") == 1

    def test_pass_evidence_after_last_user_message_still_closes_round(self) -> None:
        """最后一次用户消息**之后**的新通过证据 → 仍按已完成收束（既有行为不破）。"""
        import asyncio

        messages = [
            {"role": "user", "content": "任务提示"},
            {"role": "user", "content": "返工重开指令"},
            *self._envelope_completed(),
        ]
        reminder = TaskReminder()
        result = asyncio.run(
            reminder.execute(_ctx(_base_task_state(messages=messages)))
        )
        assert result.state_updates.get("ended") is True
        assert result.state_updates.get("task.status") == "completed"

    def test_normal_round_task_prompt_is_last_user_message(self) -> None:
        """正常轮次（任务提示即最后一条 user 消息）行为不变 → 通过证据仍收束。"""
        import asyncio

        messages = [
            {"role": "user", "content": "任务提示"},
            *self._envelope_completed(),
        ]
        reminder = TaskReminder()
        result = asyncio.run(
            reminder.execute(_ctx(_base_task_state(messages=messages)))
        )
        assert result.state_updates.get("ended") is True
        assert result.state_updates.get("task.status") == "completed"

    def test_no_user_message_scans_whole_history(self) -> None:
        """消息面无 user 消息（会话管道等）→ 锚点缺席 = 全量扫描，行为不变。"""
        assert TaskReminder._has_successful_task_evaluate(
            self._envelope_completed()
        ) is True
        assert TaskReminder._has_successful_task_evaluate(
            self._content_json_completed()
        ) is True

    def test_scope_boundary_is_the_last_user_message(self) -> None:
        """锚点取**最后**一条 user 消息（不是第一条）：其后的证据仍算数。"""
        messages = [
            {"role": "user", "content": "任务提示"},
            *self._envelope_completed(),
            {"role": "user", "content": "返工重开指令"},
            *self._envelope_completed(),
        ]
        assert TaskReminder._has_successful_task_evaluate(messages) is True
        # 去掉最后一条证据 → 上一轮的证据被锚点隔开 → 不算证据
        assert TaskReminder._has_successful_task_evaluate(messages[:-1]) is False


class TestSessionPipelineWriteGates:
    """会话主管道写面门（ADR 2026-11-06 D5 N3/N4/N6）：主管道不该有 task.*
    自身字段——无 ``task.id`` 的 state（会话管道）一律不落任务自身投影写面。

    装机事故（2026-11-06，主管道 35ee45bf1da2）：会话被幽灵写 task.status
    残留后，任务域写面把它当任务管道继续放大（pending 推进/终态补落/耗尽
    裁决）——三门收口后，会话行的残留只能由 reconcile 清扫刀清理。
    """

    def test_pending_advance_gate_requires_task_id(self) -> None:
        """⑰ pending→running 推进门：无 task.id 不推进（残留不被放大成
        running——running 残留是面板伪任务行与 run-started 复位死锁形态）。"""
        assert TaskReminder._advance_pending_status({"task.status": "pending"}) is None, (
            "会话管道（无 task.id）不得推进 pending → running"
        )
        # 对照：任务管道照常推进
        advanced = TaskReminder._advance_pending_status(
            {"task.id": "task-abc", "task.status": "pending"}
        )
        assert advanced is not None
        assert advanced.state_updates == {"task.status": "running"}

    def test_completed_round_gate_requires_task_id(self) -> None:
        """⑱ completed 补落门：无 task.id 不写 task.status/task.ended_at
        （收束轮 ended 语义保留——会话轮照常收束，只是不落任务终态）。"""
        reminder = TaskReminder()
        result = reminder._completed_round_result(
            {"task.status": "running", "task_evaluation_completed": True},
            iteration=3,
            task_id="",
        )
        assert "task.status" not in result.state_updates, "会话管道不得补落 completed"
        assert "task.ended_at" not in result.state_updates
        assert result.state_updates.get("ended") is True, "收束语义保留"
        # 对照：任务管道照常补落 completed + ISO ended_at
        result2 = reminder._completed_round_result(
            {"task.status": "running", "task_evaluation_completed": True},
            iteration=3,
            task_id="task-abc",
        )
        assert result2.state_updates["task.status"] == "completed"
        assert _is_iso_datetime(result2.state_updates.get("task.ended_at"))

    def test_reminder_exhausted_gate_requires_task_id(self) -> None:
        """⑲ 提醒耗尽裁决门：无 task.id 不落 failed/failure_*（与空回复路径
        同款会话豁免——耗尽裁决是任务域写面，会话轮只收束）。"""
        reminder = TaskReminder()
        result = reminder._reminder_exhausted_result(
            {"task.status": "running"}, reminder_count=5, iteration=3, task_id=""
        )
        for key in ("task.status", "task.ended_at", "task.failure_class", "task.failure_reason"):
            assert key not in result.state_updates, f"会话管道不得写 {key}"
        assert result.state_updates.get("ended") is True, "收束语义保留"
        # 对照：任务管道照常裁决 failed + 结构化归因
        result2 = reminder._reminder_exhausted_result(
            {"task.status": "running"}, reminder_count=5, iteration=3, task_id="task-abc"
        )
        assert result2.state_updates["task.status"] == "failed"
        assert result2.state_updates["task.failure_class"] == "reminder_exhausted"
