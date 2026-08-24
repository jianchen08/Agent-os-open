# @feature: GAP-1 任务元数据 state 单一真值 | @vision: V3 可嵌入 | @ci: python-coverage
"""task_reminder 0.2 state 契约测试（GAP-1 接线修复）。

0.2 定案：task = pipeline state 单一真值——任务管道的 state 出生即带
``task.id`` 扁平点号键（引擎注入，== pipeline_id），task_reminder 只读
state，不再依赖跨进程 task_service / 0.1 infrastructure service_provider。
本文件覆盖：

1. task_id 从 ``state["task.id"]`` 读取（引擎出生注入的权威键）；
2. agent_level 从顶层 ``state["agent_level"]`` 读取（context_build 以实际
   Agent 层级无条件覆盖；L1 调度层永不触发）；
3. 活跃子任务判断优先读 state 的 ``submitted_task_ids``（tool_core 副作用
   自动写入，跨进程可靠），不再依赖 task_service 跨进程查询；
4. 无任务字段的会话管道直接跳过（不误触发）。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parent
_SHARED = _DIR.parents[2]  # plugins/shared/

for _d in [_DIR, _SHARED]:
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

from pipeline.plugin import PluginContext  # noqa: E402
from plugin import TaskReminder  # noqa: E402


def _ctx(state: dict[str, Any]) -> PluginContext:
    """构造最小 PluginContext（state 直传，不接 task_service）。"""
    return PluginContext(state=state, config={})


def _base_task_state(**over: Any) -> dict[str, Any]:
    """0.2 任务管道 state 基线（引擎/task 派发注入的键）。"""
    base = {
        "core_type": "llm_call",
        "iteration": 3,
        "task.id": "task-abc",
        "task.goal": "写周报",
        "task.status": "running",
        "agent_level": "L2",
        "raw_tool_calls": [],
        "raw_result": "我完成了。",
    }
    base.update(over)
    return base


class TestTaskIdFromState:
    async def test_pending_task_advances_to_running(self) -> None:
        """职责边界（2026-08-24）：任务提交后出生值 pending 由任务域插件推进
        running（内核不再回写任务状态）——任何轮次都推进，幂等。"""
        reminder = TaskReminder(config={})
        ctx = _ctx(_base_task_state(**{"task.status": "pending"}))
        result = await reminder.execute(ctx)
        assert result.state_updates == {"task.status": "running"}, (
            "pending 应推进为 running，实际 %r" % (result.state_updates,)
        )
        assert result.route_signal is None, "推进不改变路由"

    async def test_reads_dotted_task_id(self) -> None:
        """0.2 新契约：task_id 从扁平键 task.id 读取（不再读顶层 task_id）。"""
        reminder = TaskReminder(config={})
        ctx = _ctx(_base_task_state())
        result = await reminder.execute(ctx)
        # 纯文本输出 + L2 任务 → 触发提醒（证明 task_id 已识别）
        assert result.state_updates, "应注入提醒（task.id 识别成功）"
        assert "evaluate_reminder_count" in result.state_updates

    async def test_skips_session_pipeline_without_task_fields(self) -> None:
        """无 task.* 字段的会话管道 → 直接跳过（不误触发）。"""
        reminder = TaskReminder(config={})
        ctx = _ctx(
            {
                "core_type": "llm_call",
                "iteration": 2,
                "raw_tool_calls": [],
                "raw_result": "你好，有什么可以帮你？",
            }
        )
        result = await reminder.execute(ctx)
        assert not result.state_updates, "会话管道不应触发提醒"
        assert result.route_signal is None


class TestAgentLevelSkip:
    async def test_l1_dispatcher_never_triggers(self) -> None:
        """L1 调度层（agent_level=L1）纯文本输出是正常调度 → 不提醒。"""
        reminder = TaskReminder(config={})
        ctx = _ctx(_base_task_state(**{"agent_level": "L1"}))
        result = await reminder.execute(ctx)
        assert not result.state_updates, "L1 调度层不触发 reminder"
        assert result.route_signal is None

    async def test_l2_executor_triggers_on_plain_text(self) -> None:
        """L2 执行者纯文本输出且无活跃子任务 → 注入提醒。"""
        reminder = TaskReminder(config={})
        ctx = _ctx(_base_task_state(**{"agent_level": "L2"}))
        result = await reminder.execute(ctx)
        assert result.state_updates, "L2 纯文本应注入提醒"
        assert result.route_signal is not None
        assert result.route_signal.route_type == "next_llm"


class TestActiveChildrenFromState:
    async def test_submitted_task_ids_present_skips_reminder(self) -> None:
        """state 有 submitted_task_ids（tool_core 副作用写入的活跃子任务）→ 跳过。

        有子任务在跑说明当前纯文本是等待/协调行为，不该催提交评估。
        """
        reminder = TaskReminder(config={})
        ctx = _ctx(
            _base_task_state(
                **{
                    "raw_result": "已提交子任务，等待完成",
                    "submitted_task_ids": ["task-child-1"],
                }
            )
        )
        result = await reminder.execute(ctx)
        assert not result.state_updates, "有活跃子任务不触发提醒"
        assert result.route_signal is None

    async def test_no_submitted_task_ids_triggers_reminder(self) -> None:
        """无活跃子任务标记 → 正常提醒。"""
        reminder = TaskReminder(config={})
        ctx = _ctx(_base_task_state(**{"raw_result": "我完成了，但还没提交评估"}))
        result = await reminder.execute(ctx)
        assert result.state_updates, "无活跃子任务应提醒提交评估"
