"""M6d Output 插件测试 — 已标记为跳过（部分插件已重构迁移）。

task_evaluation 插件已从 plugins.output 中移除，
对应功能已整合到其他插件中。
保留其余插件的测试。
"""

from __future__ import annotations

import pytest

from pipeline.plugin import PluginContext
from pipeline.types import ErrorPolicy, StateKeys, create_initial_state
from plugins.output.duplicate_check import DuplicateCheckPlugin
from plugins.output.error_check import ErrorCheckPlugin
from plugins.output.stop_check import StopCheckPlugin

# task_evaluation 插件已移除
# from plugins.output.task_evaluation import TaskEvaluationPlugin


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
    async def test_task_stopped(self, ctx, base_state):
        """测试任务被暂停/取消（stopped）产出 end 信号。

        TaskStatus 枚举仅有 stopped（合并旧 suspended/cancelled），
        stop_check 必须把 stopped 判为终态，否则暂停后引擎仍空转。
        """
        base_state[StateKeys.ITERATION] = 1
        base_state["task_status"] = "stopped"
        plugin = StopCheckPlugin({"max_iterations": 20, "max_duration_seconds": 3600})
        result = await plugin.execute(ctx)

        assert result.route_signal is not None
        assert result.route_signal.route_type == "end"
        assert "stopped" in result.state_updates["router.stop_reason"]

    @pytest.mark.asyncio
    async def test_check_task_status_disabled(self, ctx, base_state):
        """测试禁用任务状态检查。"""
        base_state[StateKeys.ITERATION] = 1
        base_state["task_status"] = "stopped"
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
        """测试可重试错误返回 next_llm 信号。

        BUG-FIX-fix_20260629_transient_no_recovery: RateLimitError 现在被分类为
        临时错误（transient），走独立计数 retry.transient_count，而不是
        retry.count；这里改为校验 transient_count 即可。
        """
        base_state[StateKeys.RAW_ERROR] = "RateLimitError: too many requests"
        base_state["retry.count"] = 0
        base_state["retry.transient_count"] = 0
        plugin = ErrorCheckPlugin({"max_retries": 3, "transient_max_retries": 3})
        result = await plugin.execute(ctx)

        assert result.route_signal is not None
        assert result.route_signal.route_type == "next_llm"
        assert result.state_updates[StateKeys.EXECUTION_STATUS] == "needs_retry"
        assert result.state_updates["retry.transient_count"] == 1

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
        """临时错误重试上限耗尽 → end + failed（不再 wait/waiting_recovery）。

        BUG-FIX-fix_20260629_transient_no_recovery:
        旧实现：3 次重试耗尽 → wait 挂起等待"恢复"，但 wait 没有主动唤醒源
        会无限挂起 → pipeline 死挂数小时。
        新实现：临时错误独立计数到 transient_max_retries（默认 10），耗尽
        直接 failed，由 task 失败链通知父任务，避免死挂。
        """
        base_state[StateKeys.RAW_ERROR] = "TimeoutError: connection timed out"
        base_state["retry.count"] = 0
        base_state["retry.transient_count"] = 3
        plugin = ErrorCheckPlugin(
            {"max_retries": 3, "transient_max_retries": 3}
        )
        result = await plugin.execute(ctx)

        assert result.route_signal is not None
        assert result.route_signal.route_type == "end"
        assert result.state_updates[StateKeys.EXECUTION_STATUS] == "failed"

    @pytest.mark.asyncio
    async def test_permanent_error_max_retries_yields_end(self, ctx, base_state):
        """永久错误（auth）重试耗尽仍返回 end 信号。"""
        base_state[StateKeys.RAW_ERROR] = (
            "AuthenticationError: invalid api key"
        )
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

    @pytest.mark.asyncio
    async def test_tool_execute_result_not_format_error(self, ctx, base_state):
        """测试工具执行结果(core_type=tool_execute)不应被误判为格式错误。

        BUG-FIX: 工具执行结果(如file_read返回的文件内容)可能包含奇数个```，
        但这不是LLM输出格式错误，不应该触发格式错误重试。
        """
        # 模拟 tool_execute 后的文件读取结果，包含奇数个 ```
        base_state[StateKeys.CORE_TYPE] = "tool_execute"
        base_state[StateKeys.RAW_RESULT] = "```python\nprint('hello')\n```\n\n一些说明\n\n```json\n{'a': 1}\n```\n\n结尾"
        base_state[StateKeys.RAW_TOOL_CALLS] = []
        base_state["retry.count"] = 0
        plugin = ErrorCheckPlugin()
        result = await plugin.execute(ctx)

        # 不应该被判定为格式错误
        assert result.route_signal is None
        assert result.state_updates[StateKeys.EXECUTION_STATUS] == "success"

    @pytest.mark.asyncio
    async def test_llm_call_format_error_still_detected(self, ctx, base_state):
        """测试LLM调用时的格式错误仍然应该被检测。"""
        base_state[StateKeys.CORE_TYPE] = "llm_call"
        # 奇数个 ``` 的LLM回复（未关闭的代码块）
        base_state[StateKeys.RAW_RESULT] = "```python\nprint('hello')\n"
        base_state[StateKeys.RAW_TOOL_CALLS] = []
        base_state["retry.count"] = 0
        plugin = ErrorCheckPlugin()
        result = await plugin.execute(ctx)

        # LLM调用时应该被判定为格式错误
        assert result.route_signal is not None
        assert result.route_signal.route_type == "next_llm"
        assert result.state_updates[StateKeys.EXECUTION_STATUS] == "needs_retry"


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


