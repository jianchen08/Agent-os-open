# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @vision: V3 可嵌入 | @ci: python-coverage
"""task_reminder 提醒额度记账契约测试（恢复重置 + 失败轮豁免）。

额度语义（2026-09 长稳治理定案）：
1. 额度按 run 记账：continue/retry 恢复经 resume_pipeline 拉起新 run（引擎
   每 run 覆盖写 run_started_at）→ 额度重置——恢复任务不得继承残缺额度
   （恢复后额度尽会被耗尽裁决误杀为 failed）；
2. 系统失败轮不计额：llm_core 超时/失败轮引擎 warn+继续、state 残留上轮
   raw_*，误判有产出会白耗额度（与恢复残缺额度叠加成必死组合）——失败轮
   置续跑标志直接重试，基线冻结；
3. 复读防绕过：仅"指纹未变 + 本 run 存在 llm_core 错误"双条件才豁免，
   agent 连轮输出相同文本的正常重复轮照常计额（防复读绕过耗尽兜底）。
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

# 全车道共跑时裸名 `plugin` 会被先收集目录的同名模块劫持，
# 按 _DIR 显式路径加载（与本目录其余测试同范式）。
_spec = importlib.util.spec_from_file_location(
    "task_reminder_plugin_budget_test", str(_DIR / "plugin.py")
)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
sys.modules["task_reminder_plugin_budget_test"] = _mod
_spec.loader.exec_module(_mod)
TaskReminder = _mod.TaskReminder  # noqa: E402

_FP_KEY = _mod._RESULT_FP_KEY
_RUN_KEY = _mod._RUN_KEY
_FLAG_KEY = _mod._FAILURE_ROUND_FLAG

_RUN_A = "2026-09-04T06:44:00+00:00"
_RUN_B = "2026-09-04T09:00:00+00:00"


def _ctx(state: dict[str, Any]) -> PluginContext:
    return PluginContext(state=state, config={})


def _base_task_state(**over: Any) -> dict[str, Any]:
    """0.2 任务管道 state 基线（core_type=llm_call + 任务键 + run 锚点 +
    额度归属 run 记账完备）。"""
    base: dict[str, Any] = {
        "core_type": "llm_call",
        "iteration": 3,
        "task.id": "task-abc",
        "task.status": "running",
        "agent_level": "L2",
        "raw_tool_calls": [],
        "raw_result": "继续处理中",
        "run_started_at": _RUN_A,
        _RUN_KEY: _RUN_A,
    }
    base.update(over)
    return base


def _merge(state: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    """测试域 merge：模拟引擎把 state_updates 应用回 state（标量/列表整体覆盖）。"""
    state.update(updates)
    return state


def _execute(state: dict[str, Any], **cfg: Any) -> Any:
    return asyncio.run(TaskReminder(config=cfg).execute(_ctx(state)))


class TestFailureRoundExemption:
    """B7：llm_core 失败轮（超时等系统瓶颈）不计提醒额度。"""

    def test_failed_round_retries_without_consuming_budget(self) -> None:
        """指纹未变 + 本 run llm_core 错误 → 置续跑标志直接重试。"""
        state = _base_task_state(
            **{
                _FP_KEY: TaskReminder._result_fingerprint("继续处理中"),
                "_plugin_errors": [
                    {"plugin_id": "pipeline_llm_core", "code": "MCP_CALL_FAILED", "message": "timeout"},
                ],
                "evaluate_reminder_count": 4,
            }
        )
        result = _execute(state)
        assert result.state_updates == {"_has_new_llm_input": True}, (
            "失败轮只置续跑标志，实际 %r" % (result.state_updates,)
        )
        assert "evaluate_reminder_count" not in result.state_updates, "失败轮不得消耗额度"
        assert "messages" not in result.state_updates, "失败轮不注入提醒（残留文本非本轮产出）"
        assert _FLAG_KEY not in result.state_updates, "内部协议键不得外泄"

    def test_consecutive_failed_rounds_stay_exempt(self) -> None:
        """连续失败轮按同一冻结基线持续豁免（基线不推进）。"""
        state = _base_task_state(
            **{
                _FP_KEY: TaskReminder._result_fingerprint("继续处理中"),
                "_plugin_errors": [
                    {"plugin_id": "pipeline_llm_core", "code": "MCP_CALL_FAILED", "message": "t1"},
                ],
            }
        )
        first = _execute(state)
        _merge(state, first.state_updates)
        state["_plugin_errors"].append(
            {"plugin_id": "pipeline_llm_core", "code": "MCP_CALL_FAILED", "message": "t2"},
        )
        second = _execute(state)
        assert second.state_updates == {"_has_new_llm_input": True}, (
            "连续失败轮应持续豁免，实际 %r" % (second.state_updates,)
        )

    @pytest.mark.parametrize(
        "errors",
        [
            [],
            [{"plugin_id": "pipeline_tool_core", "code": "X", "message": "tool failed"}],
        ],
        ids=["no-errors", "unrelated-plugin-error"],
    )
    def test_repeated_text_without_llm_error_still_consumes(self, errors: list) -> None:
        """指纹未变但无 llm_core 错误 → 正常计额（防复读绕过耗尽兜底）。"""
        state = _base_task_state(
            **{
                _FP_KEY: TaskReminder._result_fingerprint("继续处理中"),
                "_plugin_errors": errors,
                "evaluate_reminder_count": 2,
            }
        )
        result = _execute(state)
        assert result.state_updates.get("evaluate_reminder_count") == 3, (
            "非失败轮的相同文本也必须计额，实际 %r" % (result.state_updates,)
        )

    def test_recovery_round_with_new_output_consumes_budget(self) -> None:
        """失败后系统恢复轮（有新产出）→ 回归正常计额。"""
        state = _base_task_state(
            **{
                _FP_KEY: TaskReminder._result_fingerprint("继续处理中"),
                "_plugin_errors": [
                    {"plugin_id": "pipeline_llm_core", "code": "MCP_CALL_FAILED", "message": "t1"},
                ],
                "evaluate_reminder_count": 2,
            }
        )
        failed = _execute(state)
        _merge(state, failed.state_updates)
        # 系统恢复：llm_core 成功产出新文本（覆盖写 raw_result）
        state["raw_result"] = "系统恢复后的新回复"
        recovered = _execute(state)
        assert recovered.state_updates.get("evaluate_reminder_count") == 3, (
            "恢复轮应正常计额，实际 %r" % (recovered.state_updates,)
        )

    def test_eval_mode_failed_round_skips_tool_only_count(self) -> None:
        """评估模式失败轮（残留工具调用 + 空 raw_result）→ 不计 tool_only。"""
        state = _base_task_state(
            **{
                "raw_result": "",
                "raw_tool_calls": [{"function": {"name": "bash"}}],
                _FP_KEY: TaskReminder._result_fingerprint(""),
                "_plugin_errors": [
                    {"plugin_id": "pipeline_llm_core", "code": "MCP_CALL_FAILED", "message": "timeout"},
                ],
                "eval_tool_only_count": 4,
            }
        )
        result = _execute(state, evaluation_mode=True)
        assert result.state_updates == {"_has_new_llm_input": True}, (
            "评估模式失败轮同样豁免，实际 %r" % (result.state_updates,)
        )
        assert "eval_tool_only_count" not in result.state_updates


class TestResumeResetsBudget:
    """B6：continue/retry 恢复（新 run）重置提醒额度。"""

    def test_exhausted_budget_resets_on_new_run(self) -> None:
        """额度已尽的任务被恢复拉起新 run → 注入 #1 而非耗尽裁决。"""
        state = _base_task_state(
            **{
                "run_started_at": _RUN_B,
                "evaluate_reminder_count": 10,
                _RUN_KEY: _RUN_A,
            }
        )
        result = _execute(state, max_reminders=10)
        updates = result.state_updates
        assert updates.get("evaluate_reminder_count") == 1, (
            "恢复轮额度应重置后从 #1 重新计，实际 %r" % (updates,)
        )
        assert updates.get(_RUN_KEY) == _RUN_B, "计额必须写归属 run（防每轮误重置）"
        assert updates.get("ended") is not True, "恢复轮不得被残缺额度误杀"
        assert updates.get("task.status") != "failed"

    def test_same_run_exhausted_budget_still_fails(self) -> None:
        """同 run 内额度耗尽 → 照常耗尽裁决（重置不适用于执行中任务）。"""
        state = _base_task_state(
            **{
                "evaluate_reminder_count": 10,
                _RUN_KEY: _RUN_A,
            }
        )
        result = _execute(state, max_reminders=10)
        assert result.state_updates.get("ended") is True, "同 run 耗尽必须收束"
        assert result.state_updates.get("task.status") == "failed"

    def test_missing_run_keys_stays_conservative(self) -> None:
        """run_started_at 缺失（旧快照/异常态）→ 保守按同 run，不误重置。"""
        state = _base_task_state()
        del state["run_started_at"]
        state["evaluate_reminder_count"] = 10
        result = _execute(state, max_reminders=10)
        assert result.state_updates.get("ended") is True, "缺 run 锚点不得重置额度"

    def test_legacy_count_without_run_owner_soft_resets(self) -> None:
        """存量计数无归属 run（修复上线前的任务）→ 按新 run 软重置——方向是
        补足额度而非误杀，且一次性（本轮起写归属键）。"""
        state = _base_task_state(**{"evaluate_reminder_count": 9})
        del state[_RUN_KEY]
        result = _execute(state, max_reminders=10)
        assert result.state_updates.get("evaluate_reminder_count") == 1
        assert result.state_updates.get(_RUN_KEY) == _RUN_A, "必须补写归属 run（防每轮重置）"

    def test_eval_tool_only_count_resets_on_new_run(self) -> None:
        """评估模式 tool_only 计数同属 run 域：新 run 从头计（不继承残缺计数）。"""
        state = _base_task_state(
            **{
                "raw_result": "",
                "raw_tool_calls": [{"function": {"name": "bash"}}],
                "run_started_at": _RUN_B,
                "eval_tool_only_count": 5,
                _RUN_KEY: _RUN_A,
            }
        )
        result = _execute(state, evaluation_mode=True)
        assert result.state_updates.get("eval_tool_only_count") == 1, (
            "新 run 的 tool_only 计数应从 1 重计，实际 %r" % (result.state_updates,)
        )
        assert "_has_new_llm_input" not in result.state_updates, "未达阈值不注入强制提醒"


class TestBudgetWithinSameRun:
    """同 run 内额度单调累加（重置只发生在恢复边界）。"""

    def test_budget_monotonically_increases_within_run(self) -> None:
        state = _base_task_state()
        reminder = TaskReminder(config={"max_reminders": 10})
        seen: list[int] = []
        for _ in range(3):
            result = asyncio.run(reminder.execute(_ctx(state)))
            _merge(state, result.state_updates)
            count = state.get("evaluate_reminder_count", 0)
            seen.append(count)
        assert seen == [1, 2, 3], "同 run 连续提醒额度应 1→2→3 单调递增"
        assert all(0 < c <= 10 for c in seen), "额度始终在 (0, max] 区间"
        assert state.get(_RUN_KEY) == _RUN_A, "额度归属 run 保持不变"

    def test_full_lifecycle_failure_then_resume(self) -> None:
        """串景：正常计额 → 失败轮豁免 → 恢复计额 → 恢复拉起重置。"""
        state = _base_task_state()
        # ① 正常计额
        _merge(state, _execute(state).state_updates)
        assert state["evaluate_reminder_count"] == 1
        # ② 失败轮：raw_result 残留 + llm_core 错误 → 不计额
        state["_plugin_errors"] = [
            {"plugin_id": "pipeline_llm_core", "code": "MCP_CALL_FAILED", "message": "timeout"},
        ]
        _merge(state, _execute(state).state_updates)
        assert state["evaluate_reminder_count"] == 1, "失败轮不得消耗额度"
        # ③ 恢复：新产出 → 正常计额
        del state["_plugin_errors"]
        state["raw_result"] = "恢复后的回复"
        _merge(state, _execute(state).state_updates)
        assert state["evaluate_reminder_count"] == 2
        # ④ continue/retry 恢复：新 run → 额度重置
        state["run_started_at"] = _RUN_B
        _merge(state, _execute(state).state_updates)
        assert state["evaluate_reminder_count"] == 1, "恢复轮额度重置后重新计 #1"
