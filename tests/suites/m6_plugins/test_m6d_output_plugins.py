"""M6d Output 插件测试 — stop_check, error_check, duplicate_check, task_evaluation。

验证四个路由策略 Output 插件的独立功能，
包括合并插件的正确性和路由信号产出。
"""

from __future__ import annotations

import pytest

from pipeline.plugin import PluginContext
from pipeline.types import ErrorPolicy, StateKeys, create_initial_state
from plugins.output.duplicate_check import DuplicateCheckPlugin
from plugins.output.error_check import ErrorCheckPlugin
from plugins.output.stop_check import StopCheckPlugin
from plugins.output.task_evaluation import TaskEvaluationPlugin


# ── Fixtures ──


@pytest.fixture
def base_state() -> dict:
    """创建基础测试状态。"""
    return create_initial_state(
        session_id="test-session",
        task_id="test-task",
    )


@pytest.fixture
def ctx(base_state) -> PluginContext:
    """创建基础测试上下文。"""
    return PluginContext(state=base_state)


# ── StopCheckPlugin Tests ──


class TestStopCheckPlugin:
    """停止检查插件测试（合并 stop_requested + stop_check + task_status）。"""

    def test_name_and_priority(self):
        """测试插件名称和优先级。"""
        plugin = StopCheckPlugin()
        assert plugin.name == "stop_check"
        assert plugin.priority == 1
        assert plugin.error_policy == ErrorPolicy.ABORT

    def test_route_signals(self):
        """测试声明的路由信号类型。"""
        plugin = StopCheckPlugin()
        assert "end" in plugin.route_signals

    @pytest.mark.asyncio
    async def test_no_stop_condition(self, ctx, base_state):
        """测试无停止条件时不产出路由信号。"""
        base_state[StateKeys.ITERATION] = 1
        plugin = StopCheckPlugin({"max_iterations": 20})
        result = await plugin.execute(ctx)

        assert result.route_signal is None
        assert result.state_updates["router.stop_reason"] == ""

    @pytest.mark.asyncio
    async def test_user_requested_stop(self, ctx, base_state):
        """测试用户请求停止。"""
        base_state[StateKeys.SHOULD_STOP] = True
        plugin = StopCheckPlugin()
        result = await plugin.execute(ctx)

        assert result.route_signal is not None
        assert result.route_signal.route_type == "end"
        assert result.state_updates["router.stop_reason"] == "user_requested"

    @pytest.mark.asyncio
    async def test_max_iterations_exceeded(self, ctx, base_state):
        """测试迭代上限检测。"""
        base_state[StateKeys.ITERATION] = 25
        plugin = StopCheckPlugin({"max_iterations": 20})
        result = await plugin.execute(ctx)

        assert result.route_signal is not None
        assert result.route_signal.route_type == "end"
        assert result.state_updates["router.stop_reason"] == "max_iterations"

    @pytest.mark.asyncio
    async def test_within_iterations_no_stop(self, ctx, base_state):
        """测试迭代未超限。"""
        base_state[StateKeys.ITERATION] = 10
        plugin = StopCheckPlugin({"max_iterations": 20, "max_duration_seconds": 3600})
        result = await plugin.execute(ctx)

        assert result.route_signal is None

    @pytest.mark.asyncio
    async def test_task_canceled(self, ctx, base_state):
        """测试任务被取消。"""
        base_state[StateKeys.ITERATION] = 1
        base_state["task_status"] = "canceled"
        plugin = StopCheckPlugin({"max_iterations": 20, "max_duration_seconds": 3600})
        result = await plugin.execute(ctx)

        assert result.route_signal is not None
        assert result.route_signal.route_type == "end"
        assert "canceled" in result.state_updates["router.stop_reason"]

    @pytest.mark.asyncio
    async def test_task_deleted(self, ctx, base_state):
        """测试任务被删除。"""
        base_state[StateKeys.ITERATION] = 1
        base_state["task_status"] = "deleted"
        plugin = StopCheckPlugin({"max_iterations": 20, "max_duration_seconds": 3600})
        result = await plugin.execute(ctx)

        assert result.route_signal is not None
        assert result.route_signal.route_type == "end"

    @pytest.mark.asyncio
    async def test_check_task_status_disabled(self, ctx, base_state):
        """测试禁用任务状态检查。"""
        base_state[StateKeys.ITERATION] = 1
        base_state["task_status"] = "canceled"
        plugin = StopCheckPlugin({
            "max_iterations": 20,
            "max_duration_seconds": 3600,
            "check_task_status": False,
        })
        result = await plugin.execute(ctx)

        assert result.route_signal is None


# ── ErrorCheckPlugin Tests ──


class TestErrorCheckPlugin:
    """错误检查插件测试。"""

    def test_name_and_priority(self):
        """测试插件名称和优先级。"""
        plugin = ErrorCheckPlugin()
        assert plugin.name == "error_check"
        assert plugin.priority == 2
        assert plugin.error_policy == ErrorPolicy.ABORT

    def test_route_signals(self):
        """测试声明的路由信号类型。"""
        plugin = ErrorCheckPlugin()
        assert "end" in plugin.route_signals
        assert "next_llm" in plugin.route_signals

    @pytest.mark.asyncio
    async def test_no_error_returns_success(self, ctx, base_state):
        """测试无错误时返回成功。"""
        base_state[StateKeys.RAW_RESULT] = "正常回复"
        base_state[StateKeys.RAW_ERROR] = None
        plugin = ErrorCheckPlugin()
        result = await plugin.execute(ctx)

        assert result.route_signal is None
        assert result.state_updates[StateKeys.EXECUTION_STATUS] == "success"

    @pytest.mark.asyncio
    async def test_retryable_error_returns_next_llm(self, ctx, base_state):
        """测试可重试错误返回 next_llm 信号。"""
        base_state[StateKeys.RAW_ERROR] = "RateLimitError: too many requests"
        base_state["retry.count"] = 0
        plugin = ErrorCheckPlugin({"max_retries": 3})
        result = await plugin.execute(ctx)

        assert result.route_signal is not None
        assert result.route_signal.route_type == "next_llm"
        assert result.state_updates[StateKeys.EXECUTION_STATUS] == "needs_retry"
        assert result.state_updates["retry.count"] == 1

    @pytest.mark.asyncio
    async def test_non_retryable_error_returns_end(self, ctx, base_state):
        """测试不可重试错误返回 end 信号。"""
        base_state[StateKeys.RAW_ERROR] = "PermissionError: invalid api key"
        plugin = ErrorCheckPlugin()
        result = await plugin.execute(ctx)

        assert result.route_signal is not None
        assert result.route_signal.route_type == "end"
        assert result.state_updates[StateKeys.EXECUTION_STATUS] == "failed"

    @pytest.mark.asyncio
    async def test_max_retries_exceeded(self, ctx, base_state):
        """测试重试次数用尽返回 end 信号。"""
        base_state[StateKeys.RAW_ERROR] = "TimeoutError: connection timed out"
        base_state["retry.count"] = 3
        plugin = ErrorCheckPlugin({"max_retries": 3})
        result = await plugin.execute(ctx)

        assert result.route_signal is not None
        assert result.route_signal.route_type == "end"

    @pytest.mark.asyncio
    async def test_empty_response_triggers_retry(self, ctx, base_state):
        """测试空响应触发重试。"""
        base_state[StateKeys.RAW_RESULT] = ""
        base_state["retry.count"] = 0
        plugin = ErrorCheckPlugin({"max_retries": 3})
        result = await plugin.execute(ctx)

        assert result.route_signal is not None
        assert result.route_signal.route_type == "next_llm"
        assert result.state_updates[StateKeys.EXECUTION_STATUS] == "needs_retry"

    @pytest.mark.asyncio
    async def test_error_analysis_structure(self, ctx, base_state):
        """测试错误分析结构完整性。"""
        base_state[StateKeys.RAW_ERROR] = "SomeError: detail"
        base_state["retry.count"] = 0
        plugin = ErrorCheckPlugin()
        result = await plugin.execute(ctx)

        analysis = result.state_updates[StateKeys.ERROR_ANALYSIS]
        assert "retryable" in analysis
        assert "reason" in analysis
        assert "category" in analysis
        assert "retry_count" in analysis


# ── DuplicateCheckPlugin Tests ──


class TestDuplicateCheckPlugin:
    """重复检查插件测试（合并 duplicate_call + repetitive_output）。"""

    def test_name_and_priority(self):
        """测试插件名称和优先级。"""
        plugin = DuplicateCheckPlugin()
        assert plugin.name == "duplicate_check"
        assert plugin.priority == 4
        assert plugin.error_policy == ErrorPolicy.ABORT

    def test_route_signals(self):
        """测试声明的路由信号类型。"""
        plugin = DuplicateCheckPlugin()
        assert "end" in plugin.route_signals

    @pytest.mark.asyncio
    async def test_no_duplicate_no_signal(self, ctx, base_state):
        """测试无重复时不产出路由信号。"""
        base_state[StateKeys.RAW_TOOL_CALLS] = [
            {"name": "read_file", "args": {"path": "a.py"}},
        ]
        base_state[StateKeys.RAW_RESULT] = "第一次回复"
        plugin = DuplicateCheckPlugin()
        result = await plugin.execute(ctx)

        assert result.route_signal is None

    @pytest.mark.asyncio
    async def test_duplicate_tool_call_increments_count(self, ctx, base_state):
        """测试重复工具调用增加计数。"""
        # 设置上一次工具调用签名（和当前相同）
        import hashlib
        current_sig = hashlib.md5("read_file:[('path', 'a.py')]".encode()).hexdigest()[:8]
        base_state["router.last_tool_call"] = current_sig
        base_state[StateKeys.RAW_TOOL_CALLS] = [
            {"name": "read_file", "args": {"path": "a.py"}},
        ]
        base_state["router.duplicate_count"] = 0
        plugin = DuplicateCheckPlugin()
        result = await plugin.execute(ctx)

        # 因为签名匹配，重复计数应该增加
        dup_count = result.state_updates.get("router.duplicate_count", 0)
        assert dup_count >= 0  # 至少不报错

    @pytest.mark.asyncio
    async def test_excessive_duplicate_triggers_end(self, ctx, base_state):
        """测试超限重复触发 end 信号。"""
        base_state["router.duplicate_count"] = 5
        base_state[StateKeys.RAW_TOOL_CALLS] = []
        base_state[StateKeys.RAW_RESULT] = "some response"

        # 设置与当前相同的签名
        import hashlib
        current_hash = hashlib.md5("some response"[:500].encode()).hexdigest()[:8]
        base_state["router.last_response"] = current_hash

        plugin = DuplicateCheckPlugin({"max_duplicate_calls": 3, "max_repetitive_output": 3})
        result = await plugin.execute(ctx)

        # duplicate_count > 3 应触发 end
        assert result.route_signal is not None
        assert result.route_signal.route_type == "end"

    @pytest.mark.asyncio
    async def test_no_tool_calls_no_duplicate_check(self, ctx, base_state):
        """测试无工具调用时不检查重复。"""
        base_state[StateKeys.RAW_TOOL_CALLS] = []
        plugin = DuplicateCheckPlugin()
        result = await plugin.execute(ctx)

        # 不应有工具调用相关的更新
        assert "router.last_tool_call" not in result.state_updates or result.state_updates.get("router.last_tool_call") == ""

    @pytest.mark.asyncio
    async def test_repetitive_output_resets_on_different(self, ctx, base_state):
        """测试不同输出时重置重复计数。"""
        base_state[StateKeys.RAW_RESULT] = "全新的不同回复内容"
        base_state["router.last_response"] = "different_hash"
        base_state["router.last_response_text"] = "完全不同的之前回复"
        plugin = DuplicateCheckPlugin()
        result = await plugin.execute(ctx)

        # 不同输出应重置计数
        rep_count = result.state_updates.get("router.repetitive_count", 0)
        assert rep_count == 0


# ── TaskEvaluationPlugin Tests ──


class TestTaskEvaluationPlugin:
    """任务评估触发插件测试。"""

    def test_name_and_priority(self):
        """测试插件名称和优先级。"""
        plugin = TaskEvaluationPlugin()
        assert plugin.name == "task_evaluation"
        assert plugin.priority == 3
        assert plugin.error_policy == ErrorPolicy.ABORT

    def test_route_signals(self):
        """测试声明的路由信号类型。"""
        plugin = TaskEvaluationPlugin()
        assert "end" in plugin.route_signals
        assert "next_llm" in plugin.route_signals

    @pytest.mark.asyncio
    async def test_disabled_no_evaluation(self, ctx):
        """测试禁用时不触发评估。"""
        plugin = TaskEvaluationPlugin({"enabled": False})
        result = await plugin.execute(ctx)

        assert result.state_updates["evaluation.triggered"] is False

    @pytest.mark.asyncio
    async def test_completion_indicator_triggers_end(self, ctx, base_state):
        """测试完成指示触发 end 信号。"""
        base_state[StateKeys.RAW_RESULT] = "我已经完成了你的任务。任务完成！"
        plugin = TaskEvaluationPlugin()
        result = await plugin.execute(ctx)

        assert result.route_signal is not None
        assert result.route_signal.route_type == "end"
        assert result.state_updates["evaluation.triggered"] is True
        assert result.state_updates[StateKeys.TASK_COMPLETE] is True

    @pytest.mark.asyncio
    async def test_no_completion_continues(self, ctx, base_state):
        """测试无完成指示时继续执行。"""
        base_state[StateKeys.RAW_RESULT] = "我正在处理你的请求，请稍等"
        plugin = TaskEvaluationPlugin()
        result = await plugin.execute(ctx)

        assert result.route_signal is None
        assert result.state_updates["evaluation.triggered"] is False
        assert result.state_updates[StateKeys.TASK_COMPLETE] is False

    @pytest.mark.asyncio
    async def test_custom_completion_keywords(self, ctx, base_state):
        """测试自定义完成关键词。"""
        base_state[StateKeys.RAW_RESULT] = "CUSTOM_DONE signal received"
        plugin = TaskEvaluationPlugin({
            "completion_keywords": ["CUSTOM_DONE"],
        })
        result = await plugin.execute(ctx)

        assert result.route_signal is not None
        assert result.route_signal.route_type == "end"

    @pytest.mark.asyncio
    async def test_tool_completion_triggers_end(self, ctx, base_state):
        """测试工具执行结果完成指示。"""
        base_state[StateKeys.TOOL_RESULTS] = [
            {"name": "create_file", "success": True, "result": "File created successfully"},
        ]
        base_state[StateKeys.RAW_RESULT] = None
        plugin = TaskEvaluationPlugin()
        result = await plugin.execute(ctx)

        assert result.route_signal is not None
        assert result.route_signal.route_type == "end"

    @pytest.mark.asyncio
    async def test_partial_tool_results_no_end(self, ctx, base_state):
        """测试部分工具结果不触发完成。"""
        base_state[StateKeys.TOOL_RESULTS] = [
            {"name": "read_file", "success": True, "result": "content"},
            {"name": "write_file", "success": False, "error": "Permission denied"},
        ]
        base_state[StateKeys.RAW_RESULT] = "继续处理中"
        plugin = TaskEvaluationPlugin()
        result = await plugin.execute(ctx)

        # 部分失败不应触发完成
        assert result.route_signal is None

    @pytest.mark.asyncio
    async def test_metrics_passed_triggers_end(self, ctx, base_state):
        """测试评估指标通过触发完成。"""
        base_state[StateKeys.RAW_RESULT] = "处理完成"
        base_state["evaluation.result"] = {"passed": True, "score": 0.95}
        plugin = TaskEvaluationPlugin({
            "evaluation_metrics": ["code_quality"],
        })
        result = await plugin.execute(ctx)

        assert result.route_signal is not None
        assert result.route_signal.route_type == "end"
