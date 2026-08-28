# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @vision: V3 可嵌入 | @ci: python-coverage
"""task_reminder 收束闸门测试：连续工具全失败的强制收束（ADR 2026-08-28 三信号判据配套）。

背景（08-28 实证）：父会话被 task_failed 通知唤醒后 LLM 反复调用不可用工具，
每轮必败、run 永不收束。闸门契约：

- 计数只在「本轮有工具调用」的信号①轮次上进行，看上一轮 state.tool_results
  结构化结果（不解析渲染文本）；连续 N 轮（默认 3，tool_fail_streak_limit
  可配置）全失败 → 注入一次"工具不可用，直接文本总结"强制收束轮；
- 强制收束轮 LLM 仍以工具调用作答（无文本收束）→ end；任务管道改写
  task.status=failed 前复查信号②，已完成态不可覆盖；会话管道只收束不落终态；
- 任一工具成功 → episode 结束，计数与注入标志复位；
- 阈值内的仅工具调用轮次保持信号①现状（路由工具执行，不评判）。
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
    "task_reminder_plugin_tool_fail_gate_test", str(_DIR / "plugin.py")
)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
sys.modules["task_reminder_plugin_tool_fail_gate_test"] = _mod
_spec.loader.exec_module(_mod)
TaskReminder = _mod.TaskReminder  # noqa: E402


def _ctx(state: dict[str, Any]) -> PluginContext:
    return PluginContext(state=state, config={})


def _tool_call_state(**over: Any) -> dict[str, Any]:
    """信号①轮基线：LLM 本轮发出工具调用。"""
    base = {
        "core_type": "llm_call",
        "iteration": 5,
        "task.id": "task-abc",
        "task.status": "running",
        "agent_level": "L2",
        "raw_tool_calls": [{"function": {"name": "task_manage"}}],
        "raw_result": "",
        "messages": [],
    }
    base.update(over)
    return base


def _failed_results(tool: str = "task_manage") -> list[dict[str, Any]]:
    return [{"tool_name": tool, "success": False, "error": "tool not found"}]


def _is_iso_datetime(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


class TestStreakAccounting:
    """阈值内：计数递增，保持信号①现状（路由工具执行，不评判）。"""

    def test_first_failing_round_counts_one(self) -> None:
        import asyncio

        result = asyncio.run(
            TaskReminder().execute(
                _ctx(_tool_call_state(tool_results=_failed_results()))
            )
        )
        assert result.route_signal is None, "阈值内不得改变路由"
        assert result.state_updates.get("tool_fail_streak") == 1

    @pytest.mark.parametrize(
        ("prior", "expected"),
        [(0, 1), (1, 2), (2, 3)],
    )
    def test_streak_monotonic_accumulation(self, prior: int, expected: int) -> None:
        """性质：连续全失败轮计数单调 +1（参数化：0/1/2 三档）。"""
        import asyncio

        result = asyncio.run(
            TaskReminder().execute(
                _ctx(
                    _tool_call_state(
                        tool_fail_streak=prior,
                        tool_results=_failed_results(),
                    )
                )
            )
        )
        got = result.state_updates.get("tool_fail_streak")
        assert got == expected or got == 0  # 达阈值轮复位为 0 并注入（见下组测试）
        if got != 0:
            assert got == prior + 1

    def test_first_round_without_prior_results_keeps_signal_one_silent(self) -> None:
        """运行首个工具调用轮（无上一轮结果）→ 无计数写入，信号①现状零输出。"""
        import asyncio

        result = asyncio.run(TaskReminder().execute(_ctx(_tool_call_state())))
        assert result.state_updates == {}
        assert result.route_signal is None

    def test_success_resets_episode(self) -> None:
        """任一工具成功 → episode 结束：计数与注入标志复位。"""
        import asyncio

        result = asyncio.run(
            TaskReminder().execute(
                _ctx(
                    _tool_call_state(
                        tool_fail_streak=2,
                        tool_fail_gate_injected=True,
                        tool_results=[
                            {"tool_name": "file_read", "success": True, "data": {}},
                            {"tool_name": "bash", "success": False, "error": "x"},
                        ],
                    )
                )
            )
        )
        assert result.state_updates.get("tool_fail_streak") == 0
        assert result.state_updates.get("tool_fail_gate_injected") is False


class TestToolResultsShapeContract:
    """tool_results 形态契约：跨边界 JSON 字符串还原 + 非法形态不误判。

    state 结构化字段跨引擎内存 → 持久层 TEXT → 消费端边界（state_fields 同款
    契约意识）：JSON 字符串形态必须还原后判定；损坏 JSON/非列表形态不得静默
    当成"全部失败"去推动闸门。
    """

    def test_json_string_failed_results_are_restored_and_counted(self) -> None:
        """JSON 数组字符串（DB TEXT 形态）→ 还原为失败列表参与计数。"""
        import asyncio
        import json as _json

        result = asyncio.run(
            TaskReminder().execute(
                _ctx(
                    _tool_call_state(
                        tool_results=_json.dumps(_failed_results())
                    )
                )
            )
        )
        assert result.state_updates.get("tool_fail_streak") == 1

    def test_corrupted_json_string_is_not_treated_as_all_failed(self) -> None:
        """损坏 JSON 字符串 → 不构成失败证据（信号①现状零输出）。"""
        import asyncio

        result = asyncio.run(
            TaskReminder().execute(
                _ctx(_tool_call_state(tool_results='{"broken'))
            )
        )
        assert result.state_updates == {}
        assert result.route_signal is None

    @pytest.mark.parametrize(
        "bad_shape",
        [123, {"success": False}, None],
        ids=["int", "dict", "null"],
    )
    def test_non_list_shapes_are_not_failure_evidence(self, bad_shape: Any) -> None:
        """非列表形态（含缺失）→ 不构成失败证据（参数化：3 组区分度输入）。"""
        import asyncio

        result = asyncio.run(
            TaskReminder().execute(
                _ctx(_tool_call_state(tool_results=bad_shape))
            )
        )
        assert result.state_updates == {}
        assert result.route_signal is None


class TestForcedClosureRound:
    """达阈值：注入一次"工具不可用，直接文本总结"强制收束轮。"""

    def test_default_limit_three_injects_closure(self) -> None:
        import asyncio

        result = asyncio.run(
            TaskReminder().execute(
                _ctx(
                    _tool_call_state(
                        tool_fail_streak=2, tool_results=_failed_results()
                    )
                )
            )
        )
        assert result.route_signal is not None
        assert result.route_signal.route_type == "next_llm"
        updates = result.state_updates
        # 续跑标志 + 清空本轮工具调用（DSL 工具路由优先，不清会被派去执行工具）
        assert updates.get("_has_new_llm_input") is True
        assert updates.get("raw_tool_calls") == []
        # 注入标志置位、计数复位
        assert updates.get("tool_fail_gate_injected") is True
        assert updates.get("tool_fail_streak") == 0
        # 强制收束提醒已注入对话
        injected = updates["messages"][-1]
        assert injected["role"] == "system"
        assert "工具" in injected["content"]
        assert "文本" in injected["content"]

    def test_limit_is_configurable(self) -> None:
        """可配置：tool_fail_streak_limit=1 时首轮全失败即注入（与默认 3 对照）。"""
        import asyncio

        result = asyncio.run(
            TaskReminder(config={"tool_fail_streak_limit": 1}).execute(
                _ctx(_tool_call_state(tool_results=_failed_results()))
            )
        )
        assert result.route_signal is not None
        assert result.route_signal.route_type == "next_llm"
        assert result.state_updates.get("tool_fail_gate_injected") is True

    def test_injection_clears_streak_for_post_closure_accounting(self) -> None:
        """性质：注入后计数复位——后续 end 裁决不依赖累计值，只看注入标志。"""
        import asyncio

        result = asyncio.run(
            TaskReminder().execute(
                _ctx(
                    _tool_call_state(
                        tool_fail_streak=5, tool_results=_failed_results()
                    )
                )
            )
        )
        assert result.state_updates.get("tool_fail_streak") == 0
        assert result.state_updates.get("tool_fail_gate_injected") is True

    def test_gate_runs_without_task_id_wake_round(self) -> None:
        """唤醒轮（无 task.id）同样受闸门保护——循环断裂不依赖任务身份。"""
        import asyncio

        state = _tool_call_state()
        del state["task.id"]
        state["tool_fail_streak"] = 2
        state["tool_results"] = _failed_results()
        result = asyncio.run(TaskReminder().execute(_ctx(state)))
        assert result.route_signal is not None
        assert result.route_signal.route_type == "next_llm"
        assert result.state_updates.get("tool_fail_gate_injected") is True


class TestPostClosureDefiance:
    """强制收束轮后仍调用工具（无文本收束）→ end。"""

    def test_task_pipeline_marked_failed(self) -> None:
        import asyncio

        result = asyncio.run(
            TaskReminder().execute(
                _ctx(
                    _tool_call_state(
                        tool_fail_gate_injected=True,
                        tool_results=_failed_results(),
                    )
                )
            )
        )
        assert result.route_signal is not None
        assert result.route_signal.route_type == "end"
        updates = result.state_updates
        assert updates.get("_has_new_llm_input") is False
        assert updates.get("raw_tool_calls") == []
        assert updates.get("task.status") == "failed"
        assert _is_iso_datetime(updates.get("task.ended_at"))

    def test_completion_evidence_never_overwritten(self) -> None:
        """复查信号②：完成证据在场 → 按②收束补落 completed，绝不写 failed。"""
        import asyncio

        result = asyncio.run(
            TaskReminder().execute(
                _ctx(
                    _tool_call_state(
                        tool_fail_gate_injected=True,
                        tool_results=_failed_results(),
                        task_evaluation_completed=True,
                    )
                )
            )
        )
        assert result.route_signal is not None
        assert result.route_signal.route_type == "end"
        assert result.state_updates.get("task.status") == "completed"
        assert _is_iso_datetime(result.state_updates.get("task.ended_at"))

    def test_session_pipeline_ends_without_task_status(self) -> None:
        """会话管道（无 task.id）→ 只收束 end，不落任务终态。"""
        import asyncio

        state = _tool_call_state()
        del state["task.id"]
        state["tool_fail_gate_injected"] = True
        state["tool_results"] = _failed_results()
        result = asyncio.run(TaskReminder().execute(_ctx(state)))
        assert result.route_signal is not None
        assert result.route_signal.route_type == "end"
        assert "task.status" not in result.state_updates
        assert "task.ended_at" not in result.state_updates


class TestGateDoesNotFightEvalMode:
    """评估模式仅工具轮有自己的阈值机制（6 轮强制提醒），闸门不与其抢裁决。"""

    def test_eval_mode_tool_only_round_counts_normally(self) -> None:
        import asyncio

        plugin = TaskReminder(config={"evaluation_mode": True})
        state = _tool_call_state(
            eval_tool_only_count=1,
            tool_fail_streak=2,  # 已达默认闸门阈值-1，但评估模式计数优先
            tool_results=_failed_results(),
        )
        result = asyncio.run(plugin.execute(_ctx(state)))
        assert result.state_updates == {"eval_tool_only_count": 2}
        assert result.route_signal is None


class TestTextRoundUntouched:
    """纯文本轮与闸门互不干扰（计数保持、不注入、不收束）。"""

    def test_text_round_with_failing_history_falls_through(self) -> None:
        import asyncio

        result = asyncio.run(
            TaskReminder(config={"max_reminders": 3}).execute(
                _ctx(
                    _tool_call_state(
                        raw_tool_calls=[],
                        raw_result="我遇到了一些工具问题，先总结进展。",
                        tool_fail_streak=1,
                        tool_results=_failed_results(),
                    )
                )
            )
        )
        # 落入正常提醒级联（闸门只在工具调用轮计数）
        assert result.state_updates.get("evaluate_reminder_count") == 1
        assert "tool_fail_streak" not in result.state_updates
