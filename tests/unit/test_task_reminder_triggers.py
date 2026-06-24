"""task_reminder 插件触发条件回归测试。

BUG-FIX-fix_20260624_task_reminder_loop:
验证三条核心规则，修复"9 分钟 1690 次空转 LLM 调用"的死循环：
1. L1 调度层永不触发 reminder
2. 有活跃下级任务时不触发（在等子任务，不该催当前任务）
3. LLM 正在调工具时不触发（有进展）；只在纯文本输出时才触发

设计意图（用户原话）：
"reminder 应该只在任务没有下级且调用工具的时候触发，还有 L1 不应该触发"
（注："调用工具"指 reminder 仅在 LLM 该调工具却没调时催促，正在调时不催）
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from pipeline.plugin import PluginContext
from pipeline.types import StateKeys
from plugins.output.task_reminder.plugin import TaskReminder


def _make_state(**overrides) -> dict:
    """构造符合 task_reminder 检查的 state。"""
    base = {
        "iteration": 1,
        "core_type": "llm_call",
        "task_id": "task_001",
        "agent_level": "L3",  # 默认叶子执行者
        "raw_result": "我正在分析测试结果...",  # 纯文本
        "raw_tool_calls": [],  # 没调工具
        "evaluate_reminder_count": 0,
    }
    base.update(overrides)
    return base


def _make_ctx(state: dict, task_service: MagicMock | None = None) -> PluginContext:
    """构造 PluginContext，注入 mock task_service。"""
    services: dict = {}
    if task_service is not None:
        services["task_service"] = task_service
        state["task_service"] = task_service
    return PluginContext(state=state, config={}, _services=services)


def _mock_task_service(*, has_subtasks: bool = False, task_exists: bool = True) -> MagicMock:
    """构造 mock task_service。

    Args:
        has_subtasks: 是否有活跃子任务
        task_exists: 任务是否存在
    """
    svc = MagicMock()
    if task_exists:
        svc.get_task.return_value = MagicMock()
    else:
        svc.get_task.return_value = None

    if has_subtasks:
        # 有活跃子任务：status 用真实字符串（safe_enum_value 对 str 直接返回）
        from tasks.types import TaskStatus
        sub = MagicMock()
        sub.status = TaskStatus.RUNNING
        svc.list_subtasks.return_value = [sub]
    else:
        svc.list_subtasks.return_value = []
    return svc


class TestL1NeverTriggers:
    """规则 1：L1 调度层永不触发 reminder。"""

    @pytest.mark.asyncio
    async def test_l1_agent_skipped_even_with_text_output(self) -> None:
        """L1 即使输出纯文本、没调工具、没下级，也不触发 reminder。"""
        plugin = TaskReminder({"max_reminders": 5})
        svc = _mock_task_service(has_subtasks=False)
        ctx = _make_ctx(_make_state(agent_level="L1"), svc)

        result = await plugin.execute(ctx)

        assert result.route_signal is None, "L1 不应触发任何路由信号"

    @pytest.mark.asyncio
    async def test_l1_skipped_regardless_of_reminder_count(self) -> None:
        """L1 即便 reminder_count=0（没催过）也不触发。"""
        plugin = TaskReminder({"max_reminders": 5})
        svc = _mock_task_service(has_subtasks=False)
        ctx = _make_ctx(
            _make_state(agent_level="L1", evaluate_reminder_count=0), svc,
        )
        result = await plugin.execute(ctx)
        assert result.route_signal is None

    @pytest.mark.asyncio
    async def test_l2_l3_still_eligible(self) -> None:
        """L2/L3 叶子任务，纯文本输出，无下级 → 应触发 reminder。"""
        for level in ("L2", "L3"):
            plugin = TaskReminder({"max_reminders": 5})
            svc = _mock_task_service(has_subtasks=False)
            ctx = _make_ctx(_make_state(agent_level=level), svc)
            result = await plugin.execute(ctx)
            assert result.route_signal is not None
            assert result.route_signal.route_type == "next_llm", (
                f"{level} 叶子任务纯文本应触发 next_llm"
            )


class TestHasActiveChildrenSkips:
    """规则 2：有活跃下级任务时不触发。"""

    @pytest.mark.asyncio
    async def test_active_children_prevents_reminder(self) -> None:
        """L3 任务有活跃子任务（在等子任务完成）→ 不触发。"""
        plugin = TaskReminder({"max_reminders": 5})
        svc = _mock_task_service(has_subtasks=True)
        ctx = _make_ctx(_make_state(agent_level="L3"), svc)

        result = await plugin.execute(ctx)

        assert result.route_signal is None, "有活跃子任务时不应催当前任务"

    @pytest.mark.asyncio
    async def test_no_children_allows_reminder(self) -> None:
        """L3 叶子任务无子任务，纯文本输出 → 触发。"""
        plugin = TaskReminder({"max_reminders": 5})
        svc = _mock_task_service(has_subtasks=False)
        ctx = _make_ctx(_make_state(agent_level="L3"), svc)

        result = await plugin.execute(ctx)

        assert result.route_signal is not None
        assert result.route_signal.route_type == "next_llm"


class TestToolCallsInProgressSkips:
    """规则 3：LLM 正在调工具时不触发（有进展不催）。"""

    @pytest.mark.asyncio
    async def test_has_tool_calls_skips_reminder(self) -> None:
        """LLM 这一轮调了工具 → 不触发 reminder。"""
        plugin = TaskReminder({"max_reminders": 5})
        svc = _mock_task_service(has_subtasks=False)
        ctx = _make_ctx(
            _make_state(
                agent_level="L3",
                raw_tool_calls=[{"id": "c1", "name": "bash_execute"}],
                raw_result="",  # 工具调用时通常无文本
            ),
            svc,
        )

        result = await plugin.execute(ctx)

        assert result.route_signal is None, "正在调工具时不该催"

    @pytest.mark.asyncio
    async def test_text_only_triggers_reminder(self) -> None:
        """LLM 只输出纯文本没调工具 → 触发（核心场景）。"""
        plugin = TaskReminder({"max_reminders": 5})
        svc = _mock_task_service(has_subtasks=False)
        ctx = _make_ctx(
            _make_state(
                agent_level="L3",
                raw_tool_calls=[],
                raw_result="我已经看完了所有测试文件",
            ),
            svc,
        )

        result = await plugin.execute(ctx)

        assert result.route_signal is not None
        assert result.route_signal.route_type == "next_llm"


class TestReminderCountLimit:
    """reminder 达上限仍发 end（防死循环兜底不变）。"""

    @pytest.mark.asyncio
    async def test_max_reminders_sends_end(self) -> None:
        """L3 叶子任务，纯文本，但 reminder 已达上限 → 发 end。"""
        plugin = TaskReminder({"max_reminders": 3})
        svc = _mock_task_service(has_subtasks=False)
        ctx = _make_ctx(
            _make_state(agent_level="L3", evaluate_reminder_count=3), svc,
        )

        result = await plugin.execute(ctx)

        assert result.route_signal is not None
        assert result.route_signal.route_type == "end"


class TestNoInfiniteLoopRegression:
    """回归：修复前的死循环场景现在不再空转。"""

    @pytest.mark.asyncio
    async def test_l3_text_output_then_tool_calls_no_loop(self) -> None:
        """模拟日志中的死循环：L3 先纯文本（应催1次），然后调工具（不催）。

        修复前：每次纯文本都催，催完上下文变长 LLM 更倾向纯文本 → 死循环。
        修复后：L3 叶子纯文本催 1 次；一旦 LLM 调工具就停止催。
        """
        plugin = TaskReminder({"max_reminders": 5})
        svc = _mock_task_service(has_subtasks=False)

        # 第1轮：纯文本 → 催一次
        ctx1 = _make_ctx(_make_state(agent_level="L3", evaluate_reminder_count=0), svc)
        r1 = await plugin.execute(ctx1)
        assert r1.route_signal is not None
        assert r1.route_signal.route_type == "next_llm"
        assert r1.state_updates.get("evaluate_reminder_count") == 1

        # 第2轮：LLM 开始调工具 → 不催（有进展）
        ctx2 = _make_ctx(
            _make_state(
                agent_level="L3",
                evaluate_reminder_count=1,
                raw_tool_calls=[{"id": "c1", "name": "bash_execute"}],
                raw_result="",
            ),
            svc,
        )
        r2 = await plugin.execute(ctx2)
        assert r2.route_signal is None, "LLM 开始调工具后不该再催"

    @pytest.mark.asyncio
    async def test_empty_output_with_history_text_no_reminder(self) -> None:
        """本轮 LLM 输出为空（流式截断/调用失败）+ 历史有旧 assistant 文本 → 不触发。

        BUG-FIX-fix_20260625_reminder_on_empty_output:
        日志 af11896959d1 iter=5 现场：raw_result=None, raw_tool_calls=[]，
        但 messages 历史里有旧的 assistant 文本。旧逻辑用 _last_assistant_has_text
        回退把历史文本当成本轮输出 → 误触发 reminder → 死循环。
        修复后：本轮空输出不该被 reminder 当成"光说不练"。
        """
        plugin = TaskReminder({"max_reminders": 8})
        svc = _mock_task_service(has_subtasks=False)
        ctx = _make_ctx(
            _make_state(
                agent_level="L3",
                raw_tool_calls=[],       # 本轮没调工具
                raw_result=None,          # 本轮无输出（流式截断）
                evaluate_reminder_count=0,
                # messages 里有历史 assistant 文本（模拟真实场景）
                messages=[
                    {"role": "user", "content": "运行 E2E 测试"},
                    {"role": "assistant", "content": "我已经分析完测试结果..."},
                ],
            ),
            svc,
        )

        result = await plugin.execute(ctx)

        assert result.route_signal is None, (
            "本轮 LLM 输出为空时，不该用历史旧文本伪装成有输出而触发 reminder"
        )
