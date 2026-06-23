"""M3 工具系统单元测试。

覆盖范围：
- ToolCore：工具注册、批量导入、执行、超时、未找到、空调用
- ToolRegistry：注册、获取、查询、LLM 格式输出
- PendingToolsOutput：有/无工具调用时的路由信号
- Tool：数据类属性
"""

from __future__ import annotations

import asyncio
from typing import Any
import pytest

from core.exceptions import ToolNotFoundError
from pipeline.plugin import PluginContext
from pipeline.types import StateKeys
from plugins.core.tool_core import ToolCore
from plugins.output.pending_tools import PendingToolsOutput
from tools.registry import ToolRegistry
from tools.types import Tool, ToolSource


def _make_tool(
    name: str = "test_tool",
    description: str = "测试工具",
    input_schema: dict[str, Any] | None = None,
    **kwargs: Any,
) -> Tool:
    """创建测试用 Tool 实例的辅助函数。

    Args:
        name: 工具名称
        description: 工具描述
        input_schema: 输入 schema，默认为空 object
        **kwargs: 其他 Tool 字段

    Returns:
        Tool 实例
    """
    return Tool(
        name=name,
        description=description,
        input_schema=input_schema or {"type": "object", "properties": {}},
        source=ToolSource.CODE,
        **kwargs,
    )


# ═══════════════════════════════════════════════════════════
# Tool
# ═══════════════════════════════════════════════════════════


class TestTool:
    """Tool 数据类测试。"""

    def test_default_values(self) -> None:
        """默认值：input_schema 为传入值，handler 为 None。"""
        tool = _make_tool(name="test", description="desc")
        assert tool.name == "test"
        assert tool.description == "desc"
        assert tool.input_schema == {"type": "object", "properties": {}}
        assert tool.handler is None

    def test_custom_values(self) -> None:
        """自定义值正常赋值。"""
        async def handler(args: dict) -> dict:
            return args

        tool = Tool(
            name="my_tool",
            description="My tool",
            input_schema={"type": "object"},
            source=ToolSource.CODE,
            handler=handler,
        )
        assert tool.handler is handler
        assert tool.input_schema == {"type": "object"}

    def test_to_llm_format(self) -> None:
        """to_llm_format 返回 OpenAI function calling 格式。"""
        tool = Tool(
            name="search",
            description="Search the web",
            input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
            source=ToolSource.CODE,
        )
        result = tool.to_llm_format()
        assert result["type"] == "function"
        assert result["function"]["name"] == "search"
        assert "q" in result["function"]["parameters"]["properties"]


# ═══════════════════════════════════════════════════════════
# ToolRegistry
# ═══════════════════════════════════════════════════════════


class TestToolRegistry:
    """ToolRegistry 注册与查询测试。"""

    def test_register_and_get(self) -> None:
        """注册后可以获取。"""
        registry = ToolRegistry()
        tool = _make_tool(name="echo", description="Echo tool")
        registry.register(tool)

        tool_def = registry.get("echo")
        assert tool_def.name == "echo"
        assert tool_def.description == "Echo tool"

    def test_get_not_found_raises(self) -> None:
        """获取不存在的工具抛出 ToolNotFoundError。"""
        registry = ToolRegistry()
        with pytest.raises(ToolNotFoundError):
            registry.get("nonexistent")

    def test_has(self) -> None:
        """has() 正确判断工具是否存在。"""
        registry = ToolRegistry()
        assert not registry.has("echo")
        tool = _make_tool(name="echo")
        registry.register(tool)
        assert registry.has("echo")

    def test_list_all(self) -> None:
        """list_all() 返回所有已注册工具。"""
        registry = ToolRegistry()
        registry.register(_make_tool(name="a"))
        registry.register(_make_tool(name="b"))
        tools = registry.list_all()
        assert len(tools) == 2
        assert {t.name for t in tools} == {"a", "b"}

    def test_get_tools_for_llm(self) -> None:
        """get_tools_for_llm() 返回 OpenAI function calling 格式。"""
        registry = ToolRegistry()
        tool = Tool(
            name="search",
            description="Search the web",
            input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
            source=ToolSource.CODE,
        )
        registry.register(tool)
        result = registry.get_tools_for_llm()
        assert len(result) == 1
        assert result[0]["type"] == "function"
        assert result[0]["function"]["name"] == "search"
        assert "q" in result[0]["function"]["parameters"]["properties"]

    def test_register_with_handler(self) -> None:
        """register_with_handler 注册工具并绑定处理函数。"""
        registry = ToolRegistry()
        func = lambda args: args  # noqa: E731
        tool = _make_tool(name="simple")
        registry.register_with_handler(tool, func)
        assert registry.has("simple")
        assert registry.get_handler("simple") is func


# ═══════════════════════════════════════════════════════════
# ToolCore
# ═══════════════════════════════════════════════════════════


class TestToolCore:
    """ToolCore 插件测试。"""

    def _make_ctx(self, **overrides: Any) -> PluginContext:
        """创建带默认状态的 PluginContext。"""
        from pipeline.types import create_initial_state

        state = create_initial_state(**overrides)
        return PluginContext(state=state)

    def test_name_and_priority(self) -> None:
        """基本属性检查。"""
        core = ToolCore()
        assert core.name == "tool_core"
        assert core.priority == 50

    def test_register_tool(self) -> None:
        """手动注册工具。"""
        core = ToolCore()
        func = lambda args: args  # noqa: E731
        core.register_tool("echo", func)
        assert core._get_tool("echo") is func

    def test_register_tools_from_registry(self) -> None:
        """从 ToolRegistry 批量导入。"""
        registry = ToolRegistry()
        func_a = lambda args: {"result": "a"}  # noqa: E731
        func_b = lambda args: {"result": "b"}  # noqa: E731
        registry.register_with_handler(_make_tool(name="tool_a"), func_a)
        registry.register_with_handler(_make_tool(name="tool_b"), func_b)

        core = ToolCore()
        core.register_tools_from_registry(registry)

        assert core._get_tool("tool_a") is func_a
        assert core._get_tool("tool_b") is func_b

    @pytest.mark.asyncio
    async def test_execute_no_tool_calls(self) -> None:
        """无工具调用时返回提示信息。"""
        core = ToolCore()
        ctx = self._make_ctx()
        result = await core.execute(ctx)
        assert result[StateKeys.RAW_RESULT] == "No tool calls to execute"
        assert result[StateKeys.RAW_TOOL_CALLS] == []

    @pytest.mark.asyncio
    async def test_execute_sync_tool(self) -> None:
        """执行同步工具。"""
        core = ToolCore()
        core.register_tool("echo", lambda args: {"echo": args})

        ctx = self._make_ctx(
            raw_tool_calls=[{"name": "echo", "args": {"msg": "hi"}}]
        )
        result = await core.execute(ctx)

        assert result[StateKeys.RAW_ERROR] is None
        assert result[StateKeys.RAW_TOOL_CALLS] == []
        tool_results = result[StateKeys.TOOL_RESULTS]
        assert len(tool_results) == 1
        assert tool_results[0]["success"] is True
        assert tool_results[0]["tool_name"] == "echo"

    @pytest.mark.asyncio
    async def test_execute_async_tool(self) -> None:
        """执行异步工具。"""
        core = ToolCore()

        async def async_echo(args: dict) -> dict:
            return {"echo": args}

        core.register_tool("async_echo", async_echo)

        ctx = self._make_ctx(
            raw_tool_calls=[{"name": "async_echo", "args": {"msg": "hello"}}]
        )
        result = await core.execute(ctx)

        tool_results = result[StateKeys.TOOL_RESULTS]
        assert len(tool_results) == 1
        assert tool_results[0]["success"] is True

    @pytest.mark.asyncio
    async def test_execute_tool_not_found(self) -> None:
        """工具未找到时返回错误。"""
        core = ToolCore()
        ctx = self._make_ctx(
            raw_tool_calls=[{"name": "missing_tool", "args": {}}]
        )
        result = await core.execute(ctx)

        tool_results = result[StateKeys.TOOL_RESULTS]
        assert len(tool_results) == 1
        assert tool_results[0]["success"] is False
        assert "not found" in tool_results[0]["error"]

    @pytest.mark.asyncio
    async def test_execute_tool_timeout(self) -> None:
        """工具执行超时。"""
        core = ToolCore(config={"timeout": 0.1})

        async def slow_tool(args: dict) -> dict:
            await asyncio.sleep(10)
            return {"done": True}

        core.register_tool("slow", slow_tool)

        ctx = self._make_ctx(
            raw_tool_calls=[{"name": "slow", "args": {}}]
        )
        result = await core.execute(ctx)

        tool_results = result[StateKeys.TOOL_RESULTS]
        assert len(tool_results) == 1
        assert tool_results[0]["success"] is False
        assert "timed out" in tool_results[0]["error"]

    @pytest.mark.asyncio
    async def test_execute_tool_exception(self) -> None:
        """工具执行抛异常时捕获。"""
        core = ToolCore()

        def bad_tool(args: dict) -> dict:
            raise ValueError("boom")

        core.register_tool("bad", bad_tool)

        ctx = self._make_ctx(
            raw_tool_calls=[{"name": "bad", "args": {}}]
        )
        result = await core.execute(ctx)

        tool_results = result[StateKeys.TOOL_RESULTS]
        assert len(tool_results) == 1
        assert tool_results[0]["success"] is False
        assert "boom" in tool_results[0]["error"]

    @pytest.mark.asyncio
    async def test_execute_multiple_tools(self) -> None:
        """执行多个工具调用。"""
        core = ToolCore()
        core.register_tool("a", lambda args: {"a": True})
        core.register_tool("b", lambda args: {"b": True})

        ctx = self._make_ctx(
            raw_tool_calls=[
                {"name": "a", "args": {}},
                {"name": "b", "args": {}},
            ]
        )
        result = await core.execute(ctx)

        tool_results = result[StateKeys.TOOL_RESULTS]
        assert len(tool_results) == 2
        assert all(r["success"] for r in tool_results)

    @pytest.mark.asyncio
    async def test_task_evaluate_completed_sets_flag(self) -> None:
        """BUG-FIX-fix_20260615_eval_pipeline_not_end:
        task_evaluate 评估通过（metadata.result='completed'）时，
        ToolCore 应设置 task_evaluation_completed=True，使 child_task_guard
        插件当轮发出 end 信号终止管道。

        问题根因: 原代码读 tool_data.get('overall_passed')，但 slim 归一化后
          overall_passed 落在 data['output'] 子层，顶层取不到，标志永远为 False。
        修复方案: 与 stop_check 契约统一，读 metadata.result=='completed'。
        """
        from tools.types import create_success_result

        core = ToolCore()
        core.register_tool("task_evaluate", lambda args: create_success_result(
            data={"task_id": "t1", "overall_passed": True, "metrics": []},
            metadata={"action": "auto_complete", "result": "completed",
                      "message": "评估通过，任务已完成"},
        ))

        ctx = self._make_ctx(
            raw_tool_calls=[{"name": "task_evaluate", "args": {}}]
        )
        result = await core.execute(ctx)

        assert result["task_evaluation_completed"] is True, (
            "评估通过后未设置 task_evaluation_completed，child_task_guard 无法"
            "当轮终止管道"
        )

    @pytest.mark.asyncio
    async def test_task_evaluate_retry_not_sets_flag(self) -> None:
        """评估未通过（metadata.result='retry'）时不应设置完成标志。"""
        from tools.types import create_success_result

        core = ToolCore()
        core.register_tool("task_evaluate", lambda args: create_success_result(
            data={"task_id": "t1", "overall_passed": False, "metrics": []},
            metadata={"action": "auto_complete", "result": "retry",
                      "message": "指标未达标，需修复"},
        ))

        ctx = self._make_ctx(
            raw_tool_calls=[{"name": "task_evaluate", "args": {}}]
        )
        result = await core.execute(ctx)

        assert "task_evaluation_completed" not in result, \
            "评估未通过不应设置 task_evaluation_completed"


# ═══════════════════════════════════════════════════════════
# PendingToolsOutput
# ═══════════════════════════════════════════════════════════


class TestPendingToolsOutput:
    """PendingToolsOutput 输出插件测试。"""

    def _make_ctx(self, **overrides: Any) -> PluginContext:
        from pipeline.types import create_initial_state

        state = create_initial_state(**overrides)
        return PluginContext(state=state)

    def test_name_and_priority(self) -> None:
        """基本属性检查。"""
        plugin = PendingToolsOutput()
        assert plugin.name == "pending_tools"
        assert plugin.priority == 6

    def test_route_signals_empty(self) -> None:
        """route_signals 返回空列表（关注所有 core_type）。"""
        plugin = PendingToolsOutput()
        assert plugin.route_signals == []

    @pytest.mark.asyncio
    async def test_no_tool_calls(self) -> None:
        """无工具调用时返回空结果（无路由信号）。"""
        plugin = PendingToolsOutput()
        ctx = self._make_ctx()
        result = await plugin.execute(ctx)
        assert result.route_signal is None

    @pytest.mark.asyncio
    async def test_with_tool_calls(self) -> None:
        """有工具调用时发出 next_tool 路由信号。"""
        plugin = PendingToolsOutput()
        ctx = self._make_ctx(
            raw_tool_calls=[{"name": "search"}, {"name": "calculate"}]
        )
        result = await plugin.execute(ctx)

        assert result.route_signal is not None
        assert result.route_signal.route_type == "next_tool"
        assert result.route_signal.target == "tool_execute"
        assert "2 tool call(s)" in result.route_signal.reason

    @pytest.mark.asyncio
    async def test_single_tool_call(self) -> None:
        """单个工具调用也正确产生信号。"""
        plugin = PendingToolsOutput()
        ctx = self._make_ctx(
            raw_tool_calls=[{"name": "echo"}]
        )
        result = await plugin.execute(ctx)

        assert result.route_signal is not None
        assert result.route_signal.route_type == "next_tool"
        assert "1 tool call(s)" in result.route_signal.reason
