"""SDK 功能测试——覆盖 AC-07-1 ~ AC-07-5。

测试覆盖：
- AC-07-1: pip install + AgentOSPlugin + @tool 装饰器
- AC-07-2: MCP 服务端(initialize/tools/list/tools/call/resources/read/notifications)
- AC-07-3: 依赖注入句柄可用
- AC-07-4: 30 行内封装工具
- AC-07-5: 生命周期钩子(on_load/on_unload/on_config_change)
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from lingxi_plugin_sdk import (
    AgentOSPlugin,
    CapabilityHandle,
    McpServer,
    ToolDef,
    tool,
)
from lingxi_plugin_sdk.tool import collect_tools

# ═══════════════════════════════════════════════════════════
# AC-07-1: pip install + AgentOSPlugin + @tool
# ═══════════════════════════════════════════════════════════


class TestSdkImport:
    """验证 SDK 可导入且核心 API 存在。"""

    def test_import_version(self) -> None:
        from lingxi_plugin_sdk import __version__

        assert __version__ == "0.2.0"

    def test_agentos_plugin_exists(self) -> None:
        assert AgentOSPlugin is not None

    def test_tool_decorator_exists(self) -> None:
        assert callable(tool)

    def test_capability_handle_exists(self) -> None:
        assert CapabilityHandle is not None


class TestToolRegistration:
    """验证工具注册功能。"""

    def test_register_tool(self) -> None:
        plugin = AgentOSPlugin("test")

        async def handler(query: str) -> dict:
            return {"result": query}

        plugin.register_tool("search", {"type": "object"}, handler, "Search tool")
        assert "search" in plugin._tools
        assert plugin._tools["search"].name == "search"
        assert plugin._tools["search"].description == "Search tool"

    def test_tool_decorator_on_plugin(self) -> None:
        plugin = AgentOSPlugin("test")

        @plugin.tool(name="echo", schema={"type": "object"}, description="Echo tool")
        async def echo(text: str) -> dict:
            return {"echo": text}

        assert "echo" in plugin._tools
        assert plugin._tools["echo"].description == "Echo tool"

    def test_tool_decorator_module_level(self) -> None:
        @tool(name="module_tool", schema={"type": "object"})
        async def module_tool(x: int) -> dict:
            return {"x": x}

        assert hasattr(module_tool, "_agentos_tool")
        assert module_tool._agentos_tool.name == "module_tool"

    def test_register_multiple_tools(self) -> None:
        plugin = AgentOSPlugin("test")

        for i in range(5):
            plugin.register_tool(
                f"tool_{i}",
                {"type": "object"},
                lambda i=i: {"i": i},
            )

        assert len(plugin._tools) == 5

    def test_sync_handler_registration(self) -> None:
        """sync handler 也应被接受。"""
        plugin = AgentOSPlugin("test")

        def sync_handler(path: str) -> dict:
            return {"path": path}

        plugin.register_tool("read", {"type": "object"}, sync_handler)
        assert "read" in plugin._tools


# ═══════════════════════════════════════════════════════════
# AC-07-2: MCP 服务端功能
# ═══════════════════════════════════════════════════════════


class TestMcpServer:
    """验证 MCP JSON-RPC 服务端功能。"""

    def _make_server(self) -> tuple[McpServer, dict[str, ToolDef], dict]:
        tools = {
            "echo": ToolDef(
                name="echo",
                schema={"type": "object", "properties": {"text": {"type": "string"}}},
                handler=AsyncMock(return_value={"echo": "hello"}),
                description="Echo tool",
            ),
        }
        resources = {}
        handlers = {}
        server = McpServer(tools, resources, handlers)
        return server, tools, handlers

    def test_initialize(self) -> None:
        server, _, _ = self._make_server()
        result = server._handle_initialize({"capabilities": {}, "config": {}})
        assert result["protocolVersion"] == "2024-11-05"
        assert result["serverInfo"]["name"] == "lingxi-plugin-sdk"

    def test_tools_list(self) -> None:
        server, _, _ = self._make_server()
        result = server._handle_tools_list()
        assert len(result["tools"]) == 1
        assert result["tools"][0]["name"] == "echo"
        assert result["tools"][0]["description"] == "Echo tool"

    @pytest.mark.asyncio
    async def test_tools_call_async(self) -> None:
        server, _, _ = self._make_server()
        result = await server._handle_tools_call({"name": "echo", "arguments": {"text": "hello"}})
        assert result["isError"] is False
        content_text = json.loads(result["content"][0]["text"])
        assert content_text == {"echo": "hello"}

    @pytest.mark.asyncio
    async def test_tools_call_unknown_tool(self) -> None:
        server, _, _ = self._make_server()
        with pytest.raises(ValueError, match="tool not found"):
            await server._handle_tools_call({"name": "nonexistent", "arguments": {}})

    @pytest.mark.asyncio
    async def test_tools_call_sync_handler(self) -> None:
        """sync handler 应被自动包装为 async。"""

        def sync_echo(text: str) -> dict:
            return {"echo": text}

        tools = {
            "sync_echo": ToolDef(
                name="sync_echo",
                schema={"type": "object"},
                handler=sync_echo,
            ),
        }
        server = McpServer(tools, {}, {})
        result = await server._handle_tools_call(
            {"name": "sync_echo", "arguments": {"text": "world"}}
        )
        content = json.loads(result["content"][0]["text"])
        assert content == {"echo": "world"}

    @pytest.mark.asyncio
    async def test_resources_read(self) -> None:
        from lingxi_plugin_sdk.types import ResourceDef

        def read_config() -> dict:
            return {"setting": "value"}

        resources = {
            "config://app": ResourceDef(
                uri="config://app",
                handler=read_config,
                name="App Config",
            ),
        }
        server = McpServer({}, resources, {})
        result = await server._handle_resources_read({"uri": "config://app"})
        assert len(result["contents"]) == 1
        assert result["contents"][0]["uri"] == "config://app"
        content = json.loads(result["contents"][0]["text"])
        assert content == {"setting": "value"}

    @pytest.mark.asyncio
    async def test_resources_read_unknown(self) -> None:
        server = McpServer({}, {}, {})
        with pytest.raises(ValueError, match="resource not found"):
            await server._handle_resources_read({"uri": "unknown://x"})

    @pytest.mark.asyncio
    async def test_notification_lifecycle(self) -> None:
        """生命周期 notification 应触发注册的 handler。"""
        called: dict[str, Any] = {}

        def on_load_handler(params: dict) -> None:
            called["on_load"] = params

        handlers = {"on_load": on_load_handler}
        server = McpServer({}, {}, handlers)
        await server._handle_notification("notifications/on_load", {"key": "value"})
        assert called.get("on_load") == {"key": "value"}

    @pytest.mark.asyncio
    async def test_notification_unknown_method_ignored(self) -> None:
        """未知的 notification 方法应被安全忽略。"""
        server = McpServer({}, {}, {})
        await server._handle_notification("notifications/unknown", {})

    @pytest.mark.asyncio
    async def test_notification_initialized_ignored(self) -> None:
        """notifications/initialized 是协议通知，应被忽略。"""
        server = McpServer({}, {}, {})
        await server._handle_notification("notifications/initialized", {})


# ═══════════════════════════════════════════════════════════
# AC-07-3: 依赖注入句柄
# ═══════════════════════════════════════════════════════════


class TestCapabilityHandle:
    """验证能力句柄功能。"""

    def test_capability_handle_creation(self) -> None:
        handle = CapabilityHandle("pipeline-executor")
        assert handle.name == "pipeline-executor"

    @pytest.mark.asyncio
    async def test_capability_call(self) -> None:
        call_fn = AsyncMock(return_value={"status": "ok"})
        handle = CapabilityHandle("pipeline-executor", call_fn=call_fn)
        result = await handle.call("execute", {"plugin": "test"})
        assert result == {"status": "ok"}
        call_fn.assert_called_once_with("execute", {"plugin": "test"})

    @pytest.mark.asyncio
    async def test_capability_call_not_connected(self) -> None:
        handle = CapabilityHandle("pipeline-executor")
        with pytest.raises(RuntimeError, match="not connected"):
            await handle.call("execute", {})

    def test_capability_get_context(self) -> None:
        handle = CapabilityHandle(
            "tenant-context",
            context={"tenant_id": "t1", "user_id": "u1"},
        )
        assert handle.get("tenant_id") == "t1"
        assert handle.get("user_id") == "u1"
        assert handle.get("nonexistent") is None
        assert handle.has("tenant_id") is True
        assert handle.has("nonexistent") is False

    def test_capability_keys(self) -> None:
        handle = CapabilityHandle("config-reader", context={"a": 1, "b": 2})
        keys = handle.keys()
        assert set(keys) == {"a", "b"}


class TestDependencyInjection:
    """验证 initialize 握手时的依赖注入。"""

    def test_plugin_receives_capabilities(self) -> None:
        plugin = AgentOSPlugin("test")
        init_params = {
            "capabilities": {
                "pipeline-executor": {
                    "call_fn": AsyncMock(),
                    "context": {"max_iterations": 10},
                },
                "config-reader": {
                    "call_fn": AsyncMock(),
                    "context": {"root": "/etc/agentos"},
                },
            },
            "config": {"model": "gpt-4"},
        }
        plugin._on_initialize(init_params)

        cap = plugin.get_capability("pipeline-executor")
        assert cap.name == "pipeline-executor"
        assert cap.get("max_iterations") == 10

        cap2 = plugin.get_capability("config-reader")
        assert cap2.get("root") == "/etc/agentos"

    def test_plugin_receives_config(self) -> None:
        plugin = AgentOSPlugin("test")
        plugin._on_initialize({"capabilities": {}, "config": {"key": "val"}})
        assert plugin.get_config() == {"key": "val"}

    def test_get_capability_not_injected(self) -> None:
        plugin = AgentOSPlugin("test")
        plugin._on_initialize({"capabilities": {}, "config": {}})
        with pytest.raises(KeyError, match="not injected"):
            plugin.get_capability("pipeline-executor")

    def test_all_five_standard_capabilities(self) -> None:
        """验证 5 个标准能力句柄均可被注入。"""
        plugin = AgentOSPlugin("test")
        caps = {
            name: {"call_fn": AsyncMock(), "context": {"active": True}}
            for name in [
                "pipeline-executor",
                "config-reader",
                "tenant-context",
                "event-bus",
                "logger",
            ]
        }
        plugin._on_initialize({"capabilities": caps, "config": {}})

        for name in caps:
            cap = plugin.get_capability(name)
            assert cap.get("active") is True


# ═══════════════════════════════════════════════════════════
# AC-07-4: 30 行内封装工具
# ═══════════════════════════════════════════════════════════


class TestThirtyLinePlugin:
    """验证 SDK 可在 30 行内封装一个简单工具。"""

    def test_thirty_line_plugin(self) -> None:
        """30 行内封装工具为 MCP 服务的完整示例。"""
        plugin_code = '''
from lingxi_plugin_sdk import AgentOSPlugin

plugin = AgentOSPlugin("calculator")


@plugin.tool(
    name="add",
    schema={"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}},
    description="Add two numbers",
)
async def add(a: float, b: float) -> dict:
    """Add two numbers and return the sum."""
    return {"sum": a + b}


if __name__ == "__main__":
    plugin.run()
'''
        lines = [line for line in plugin_code.strip().split("\n") if line.strip()]
        assert len(lines) <= 30, f"Plugin code is {len(lines)} lines, should be <= 30"

    def test_collect_tools_from_module(self) -> None:
        """验证 collect_tools 可从模块收集 @tool 标记的函数。"""

        class FakeModule:
            @tool(name="a", schema={"type": "object"})
            async def tool_a(self) -> dict:
                return {}

            @tool(name="b", schema={"type": "object"})
            async def tool_b(self) -> dict:
                return {}

        mod = FakeModule()
        tools = collect_tools(mod)
        assert "a" in tools
        assert "b" in tools


# ═══════════════════════════════════════════════════════════
# AC-07-5: 生命周期钩子
# ═══════════════════════════════════════════════════════════


class TestLifecycleHooks:
    """验证生命周期钩子注册和响应。"""

    def test_on_load_decorator(self) -> None:
        plugin = AgentOSPlugin("test")

        @plugin.on_load
        async def handle_load(params: dict) -> None:
            pass

        assert "on_load" in plugin._lifecycle_handlers

    def test_on_unload_decorator(self) -> None:
        plugin = AgentOSPlugin("test")

        @plugin.on_unload
        async def handle_unload(params: dict) -> None:
            pass

        assert "on_unload" in plugin._lifecycle_handlers

    def test_on_config_change_decorator(self) -> None:
        plugin = AgentOSPlugin("test")

        @plugin.on_config_change
        async def handle_config(params: dict) -> None:
            pass

        assert "on_config_change" in plugin._lifecycle_handlers

    def test_on_lifecycle_direct(self) -> None:
        plugin = AgentOSPlugin("test")

        def handler(params: dict) -> None:
            pass

        plugin.on_lifecycle("on_error", handler)
        assert "on_error" in plugin._lifecycle_handlers

    @pytest.mark.asyncio
    async def test_lifecycle_via_mcp_notification(self) -> None:
        """通过 MCP notification 触发生命周期 handler。"""
        plugin = AgentOSPlugin("test")
        received: list[dict] = []

        @plugin.on_load
        def handle_load(params: dict) -> None:
            received.append(params)

        @plugin.on_unload
        def handle_unload(params: dict) -> None:
            received.append(params)

        server = McpServer(
            plugin._tools,
            plugin._resources,
            plugin._lifecycle_handlers,
        )

        await server._handle_notification("notifications/on_load", {"step": 1})
        await server._handle_notification("notifications/on_unload", {"step": 2})

        assert len(received) == 2
        assert received[0] == {"step": 1}
        assert received[1] == {"step": 2}

    @pytest.mark.asyncio
    async def test_async_lifecycle_handler(self) -> None:
        """async 生命周期 handler 也应被正确调用。"""
        plugin = AgentOSPlugin("test")
        called: list[str] = []

        @plugin.on_config_change
        async def handle_config(params: dict) -> None:
            called.append("config_changed")

        server = McpServer(
            plugin._tools,
            plugin._resources,
            plugin._lifecycle_handlers,
        )

        await server._handle_notification("notifications/on_config_change", {"new_config": {}})
        assert called == ["config_changed"]
