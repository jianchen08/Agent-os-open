# @feature: FP-0.2.一 第三方插件协议 | @vision: V3 可嵌入 | @ci: python-test
"""管道步骤服务化 SDK 侧测试（提案 §3.4/§3.6）。

覆盖：
- @plugin.step 注册 + config["_step_method"] 命中分发
- _step_method 明示调用但未注册 → StepNotFoundError（fail-closed，文案含名称与清单）
- 未注入约定字段 → 原默认 execute 工具路径不变（存量插件回归保护）
- @plugin.pipe_hook 注册 + dispatch_pipe_hook 多 handler 顺序收集 / 无注册返回 [] /
  terminate 否决指令原样穿透
"""

from __future__ import annotations

from typing import Any

import pytest

from agentos_plugin_sdk import AgentOSPlugin, McpServer
from agentos_plugin_sdk.exceptions.step import StepNotFoundError
from agentos_plugin_sdk.step_dispatch import dispatch_pipe_hook_registry


def _make_server(plugin: AgentOSPlugin) -> McpServer:
    return McpServer(
        plugin._tools,
        plugin._resources,
        plugin._lifecycle_handlers,
        steps=plugin.steps,
        pipe_hooks=plugin.pipe_hooks,
    )


class TestStepRegistration:
    """@plugin.step 装饰器注册行为。"""

    def test_step_decorator_registers_handler(self) -> None:
        plugin = AgentOSPlugin("test")

        @plugin.step("task.remind")
        async def remind(state: dict, config: dict | None = None) -> dict:
            return {"state_updates": {"reminded": True}}

        assert "task.remind" in plugin.steps
        assert plugin.steps["task.remind"] is remind

    def test_step_decorator_returns_original_function(self) -> None:
        plugin = AgentOSPlugin("test")

        @plugin.step("task.remind")
        async def remind(state: dict, config: dict | None = None) -> dict:
            return {"state_updates": {"reminded": True}}

        # 装饰器不应替换原函数——模块级引用与注册表指向同一对象
        assert callable(remind)

    def test_step_decorator_requires_name(self) -> None:
        """name 是必填位置参数——缺省即 TypeError（与 @plugin.tool 同构）。"""
        plugin = AgentOSPlugin("test")

        with pytest.raises(TypeError):
            plugin.step()  # type: ignore[call-arg]

    def test_get_declared_steps_sorted(self) -> None:
        plugin = AgentOSPlugin("test")

        @plugin.step("z_last")
        async def z_last(state: dict, config: dict | None = None) -> dict:
            return {}

        @plugin.step("a_first")
        async def a_first(state: dict, config: dict | None = None) -> dict:
            return {}

        assert plugin.get_declared_steps() == ["a_first", "z_last"]

    def test_get_declared_steps_empty(self) -> None:
        plugin = AgentOSPlugin("test")
        assert plugin.get_declared_steps() == []


class TestStepDispatch:
    """_step_method 约定字段分发。"""

    @pytest.mark.asyncio
    async def test_step_method_hit_dispatches_to_registered_handler(self) -> None:
        plugin = AgentOSPlugin("test")

        @plugin.step("task.remind")
        async def remind(state: dict, config: dict | None = None) -> dict:
            return {"state_updates": {"reminded": state.get("task_id"), "cfg_kept": config is not None}}

        server = _make_server(plugin)
        state = {"task_id": "t-42"}
        config = {"_step_method": "task.remind", "mode": "quiet"}
        result = await server._handle_tools_call(
            {"name": "task.remind", "arguments": {"state": state, "config": config}}
        )
        content = result.content[0].text
        assert "t-42" in content
        # config 原样到达 handler（_step_method 保留 + 自定义键可见）
        assert '"cfg_kept": true' in content

    @pytest.mark.asyncio
    async def test_step_method_state_and_config_passed_verbatim(self) -> None:
        """handler 收到 state/config 原样透传（含 _step_method 键，供内省）。"""
        plugin = AgentOSPlugin("test")
        seen: dict[str, Any] = {}

        @plugin.step("task.remind")
        async def remind(state: dict, config: dict | None = None) -> dict:
            seen["state"] = state
            seen["config"] = config
            return {}

        server = _make_server(plugin)
        state = {"task_id": "t-7"}
        config = {"_step_method": "task.remind", "extra": 1}
        await server._handle_tools_call({"name": "task.remind", "arguments": {"state": state, "config": config}})
        assert seen["state"] == {"task_id": "t-7"}
        assert seen["config"] == {"_step_method": "task.remind", "extra": 1}

    @pytest.mark.asyncio
    async def test_step_method_unregistered_raises_with_name_and_declared(self) -> None:
        """_step_method 明示而来但未注册 → fail-closed，绝不静默退回 execute。"""
        plugin = AgentOSPlugin("test")

        @plugin.step("task.remind")
        async def remind(state: dict, config: dict | None = None) -> dict:
            return {}

        async def execute(state: dict, config: dict | None = None) -> dict:
            return {"state_updates": {"executed": True}}

        plugin.register_tool("test.execute", {"type": "object"}, execute)
        server = _make_server(plugin)

        with pytest.raises(StepNotFoundError) as exc_info:
            await server._handle_tools_call(
                {
                    "name": "test.execute",
                    "arguments": {"state": {}, "config": {"_step_method": "task.gc"}},
                }
            )
        assert "task.gc" in str(exc_info.value)
        assert "task.remind" in str(exc_info.value)
        assert exc_info.value.name == "task.gc"
        assert exc_info.value.declared_steps == ["task.remind"]

    @pytest.mark.asyncio
    async def test_step_method_unregistered_with_empty_registry(self) -> None:
        plugin = AgentOSPlugin("test")
        server = _make_server(plugin)

        with pytest.raises(StepNotFoundError) as exc_info:
            await server._handle_tools_call(
                {"name": "x.execute", "arguments": {"state": {}, "config": {"_step_method": "task.gc"}}}
            )
        assert "task.gc" in str(exc_info.value)
        assert exc_info.value.declared_steps == []

    @pytest.mark.asyncio
    async def test_no_convention_key_uses_default_execute_tool(self) -> None:
        """未注入 _step_method/_pipe_hook → 走原工具分发路径（存量插件零感知）。"""
        plugin = AgentOSPlugin("test")
        called: list[str] = []

        async def execute(state: dict, config: dict | None = None) -> dict:
            called.append("execute")
            return {"state_updates": {"executed": state.get("n")}}

        plugin.register_tool(
            "test.execute",
            {"type": "object", "properties": {"state": {"type": "object"}, "config": {"type": "object"}}},
            execute,
        )
        server = _make_server(plugin)

        result = await server._handle_tools_call({"name": "test.execute", "arguments": {"state": {"n": 1}}})
        assert called == ["execute"]
        assert '"executed": 1' in result.content[0].text

    @pytest.mark.asyncio
    async def test_convention_precedence_when_tool_registered(self) -> None:
        """同一名字既有 execute 工具又注册了 step 时，约定字段优先（防误走 execute）。"""
        plugin = AgentOSPlugin("test")
        executed: list[str] = []

        async def execute(state: dict, config: dict | None = None) -> dict:
            executed.append("execute")
            return {}

        @plugin.step("task.remind")
        async def remind(state: dict, config: dict | None = None) -> dict:
            executed.append("step")
            return {"state_updates": {"reminded": True}}

        plugin.register_tool("test.execute", {"type": "object"}, execute)
        server = _make_server(plugin)

        await server._handle_tools_call(
            {"name": "test.execute", "arguments": {"state": {}, "config": {"_step_method": "task.remind"}}}
        )
        assert executed == ["step"]


class TestPipeHook:
    """@plugin.pipe_hook 注册与分发。"""

    def test_pipe_hook_decorator_registers_handler(self) -> None:
        plugin = AgentOSPlugin("test")

        @plugin.pipe_hook("stream_chunk")
        async def on_chunk(payload: dict) -> dict | None:
            return None

        assert plugin.pipe_hooks["stream_chunk"] == [on_chunk]

    def test_pipe_hook_multiple_handlers_same_event(self) -> None:
        plugin = AgentOSPlugin("test")

        @plugin.pipe_hook("stream_chunk")
        async def first(payload: dict) -> dict | None:
            return None

        @plugin.pipe_hook("stream_chunk")
        async def second(payload: dict) -> dict | None:
            return None

        assert plugin.pipe_hooks["stream_chunk"] == [first, second]

    @pytest.mark.asyncio
    async def test_dispatch_pipe_hook_no_registry_returns_empty(self) -> None:
        plugin = AgentOSPlugin("test")
        server = _make_server(plugin)
        assert await server.dispatch_pipe_hook("unknown_event", {"k": 1}) == []

    @pytest.mark.asyncio
    async def test_dispatch_pipe_hook_sequential_collects_non_null(self) -> None:
        plugin = AgentOSPlugin("test")
        order: list[str] = []

        @plugin.pipe_hook("stream_chunk")
        async def first(payload: dict) -> dict | None:
            order.append("first")
            return None  # 观察者：无输出

        @plugin.pipe_hook("stream_chunk")
        async def second(payload: dict) -> dict | None:
            order.append("second")
            return {"event": payload["event"], "count": 1}

        @plugin.pipe_hook("stream_chunk")
        async def third(payload: dict) -> dict | None:
            order.append("third")
            return {"count": 2}

        server = _make_server(plugin)
        results = await server.dispatch_pipe_hook("stream_chunk", {"event": "stream_chunk", "chunk": "x"})

        assert order == ["first", "second", "third"]
        assert results == [
            {"event": "stream_chunk", "count": 1},
            {"count": 2},
        ]

    @pytest.mark.asyncio
    async def test_dispatch_pipe_hook_terminate_decision_passes_through(self) -> None:
        """结构化否决指令原样穿透，不折叠不吞掉。"""
        plugin = AgentOSPlugin("test")

        @plugin.pipe_hook("stream_chunk")
        async def veto(payload: dict) -> dict | None:
            return {"decision": "terminate", "reason": "chunk malformed"}

        server = _make_server(plugin)
        results = await server.dispatch_pipe_hook("stream_chunk", {"event": "stream_chunk"})
        assert results == [{"decision": "terminate", "reason": "chunk malformed"}]

    @pytest.mark.asyncio
    async def test_dispatch_pipe_hook_via_execute_channel(self) -> None:
        """内核经 execute 通道注入 _pipe_hook 的调用形态。"""
        plugin = AgentOSPlugin("test")
        seen: list[dict[str, Any]] = []

        @plugin.pipe_hook("stream_chunk")
        async def on_chunk(payload: dict) -> dict | None:
            seen.append(payload)
            return {"decision": "terminate", "reason": "stop"}

        server = _make_server(plugin)
        result = await server._handle_tools_call(
            {
                "name": "test.execute",
                "arguments": {
                    "state": {},
                    "config": {
                        "_pipe_hook": {"event": "stream_chunk", "payload": {"event": "stream_chunk", "chunk": "x"}}
                    },
                },
            }
        )
        assert seen == [{"event": "stream_chunk", "chunk": "x"}]
        assert '"decision": "terminate"' in result.content[0].text
        assert '"reason": "stop"' in result.content[0].text

    @pytest.mark.asyncio
    async def test_dispatch_pipe_hook_unknown_event_via_execute_returns_empty(self) -> None:
        plugin = AgentOSPlugin("test")
        server = _make_server(plugin)
        result = await server._handle_tools_call(
            {
                "name": "test.execute",
                "arguments": {"state": {}, "config": {"_pipe_hook": {"event": "nope", "payload": {}}}},
            }
        )
        assert result.content[0].text == "[]"


class TestDispatchRegistry:
    """共享分发函数的独立行为（供非 McpServer 宿主复用）。"""

    @pytest.mark.asyncio
    async def test_registry_sync_handlers_supported(self) -> None:
        def sync_handler(payload: dict) -> dict | None:
            return {"seen": payload["n"]}

        results = await dispatch_pipe_hook_registry({"evt": [sync_handler]}, "evt", {"n": 5})
        assert results == [{"seen": 5}]

    @pytest.mark.asyncio
    async def test_registry_sequential_order_and_null_skip(self) -> None:
        async def a(payload: dict) -> dict | None:
            return None

        def b(payload: dict) -> dict | None:
            return {"b": True}

        results = await dispatch_pipe_hook_registry({"evt": [a, b]}, "evt", {})
        assert results == [{"b": True}]

    @pytest.mark.asyncio
    async def test_registry_no_handlers_returns_empty(self) -> None:
        assert await dispatch_pipe_hook_registry({}, "evt", {}) == []
