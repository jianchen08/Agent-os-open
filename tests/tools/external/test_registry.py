"""外部工具注册表测试。"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from tools.external.adapter import ExternalToolAdapter
from tools.external.exceptions import ConfigError
from tools.external.interfaces import IExternalToolConnection
from tools.external.registry import ExternalToolRegistry
from tools.external.types import (
    ExternalToolCapability,
    ExternalToolConfig,
    ExternalToolState,
    ProtocolType,
)


class SimpleAdapter(ExternalToolAdapter):
    """测试用简单适配器。"""

    def __init__(self, config: ExternalToolConfig) -> None:
        super().__init__(config)

    def define_schemas(self) -> list[ExternalToolCapability]:
        return [
            ExternalToolCapability(
                name="op_a",
                description="操作 A",
                input_schema={"type": "object", "properties": {"x": {"type": "string"}}},
            ),
            ExternalToolCapability(
                name="op_b",
                description="操作 B",
            ),
        ]

    async def _do_execute(
        self,
        operation: str,
        inputs: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {"success": True, "operation": operation}


class MockConnection:
    """测试用 Mock 连接。"""

    def __init__(self, healthy: bool = True) -> None:
        self._healthy = healthy

    def get_state(self) -> ExternalToolState:
        return ExternalToolState.CONNECTED if self._healthy else ExternalToolState.ERROR

    async def health_check(self) -> bool:
        return self._healthy

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def send_request(
        self,
        operation: str,
        payload: dict[str, Any],
        timeout: float | None = None,
    ) -> dict[str, Any]:
        return {"success": True, "operation": operation}


@pytest.fixture
def registry() -> ExternalToolRegistry:
    return ExternalToolRegistry()


@pytest.fixture
def config_a() -> ExternalToolConfig:
    return ExternalToolConfig(
        name="tool_a",
        display_name="Tool A",
        protocol=ProtocolType.HTTP,
        endpoint="http://localhost:8080",
    )


@pytest.fixture
def config_b() -> ExternalToolConfig:
    return ExternalToolConfig(
        name="tool_b",
        display_name="Tool B",
        protocol=ProtocolType.WEBSOCKET,
        endpoint="ws://localhost:9090",
    )


class TestExternalToolRegistry:

    def test_register_external_tool(self, registry: ExternalToolRegistry, config_a: ExternalToolConfig) -> None:
        adapter = SimpleAdapter(config_a)
        name = registry.register_external_tool(adapter)
        assert name == "tool_a"
        assert registry.count() == 1

    def test_register_duplicate_raises(self, registry: ExternalToolRegistry, config_a: ExternalToolConfig) -> None:
        adapter1 = SimpleAdapter(config_a)
        adapter2 = SimpleAdapter(config_a)
        registry.register_external_tool(adapter1)
        with pytest.raises(ConfigError, match="已注册"):
            registry.register_external_tool(adapter2)

    def test_register_with_connection(self, registry: ExternalToolRegistry, config_a: ExternalToolConfig) -> None:
        adapter = SimpleAdapter(config_a)
        connection = MockConnection()
        registry.register_external_tool(adapter, connection)
        assert registry.get_connection("tool_a") is connection
        assert adapter.connection is connection

    def test_unregister(self, registry: ExternalToolRegistry, config_a: ExternalToolConfig) -> None:
        adapter = SimpleAdapter(config_a)
        registry.register_external_tool(adapter)
        assert registry.count() == 1

        registry.unregister_external_tool("tool_a")
        assert registry.count() == 0
        assert registry.get_adapter("tool_a") is None

    def test_unregister_nonexistent(self, registry: ExternalToolRegistry) -> None:
        # 不应报错
        registry.unregister_external_tool("nonexistent")

    def test_get_adapter(self, registry: ExternalToolRegistry, config_a: ExternalToolConfig) -> None:
        adapter = SimpleAdapter(config_a)
        registry.register_external_tool(adapter)
        assert registry.get_adapter("tool_a") is adapter

    def test_get_adapter_not_found(self, registry: ExternalToolRegistry) -> None:
        assert registry.get_adapter("nonexistent") is None

    def test_list_external_tools(self, registry: ExternalToolRegistry, config_a: ExternalToolConfig, config_b: ExternalToolConfig) -> None:
        registry.register_external_tool(SimpleAdapter(config_a))
        registry.register_external_tool(SimpleAdapter(config_b))

        infos = registry.list_external_tools()
        assert len(infos) == 2
        names = {i.name for i in infos}
        assert names == {"tool_a", "tool_b"}

    def test_list_all_internal_tools(self, registry: ExternalToolRegistry, config_a: ExternalToolConfig) -> None:
        registry.register_external_tool(SimpleAdapter(config_a))
        tools = registry.list_all_internal_tools()
        assert len(tools) == 2  # op_a, op_b
        tool_names = {t.name for t in tools}
        assert "tool_a__op_a" in tool_names
        assert "tool_a__op_b" in tool_names

    def test_get_tools_by_capability(self, registry: ExternalToolRegistry, config_a: ExternalToolConfig) -> None:
        registry.register_external_tool(SimpleAdapter(config_a))
        tools = registry.get_tools_by_capability("op_a")
        assert len(tools) == 1
        assert tools[0].name == "tool_a__op_a"

    def test_get_tools_by_capability_not_found(self, registry: ExternalToolRegistry, config_a: ExternalToolConfig) -> None:
        registry.register_external_tool(SimpleAdapter(config_a))
        tools = registry.get_tools_by_capability("nonexistent")
        assert len(tools) == 0

    @pytest.mark.asyncio
    async def test_health_check_all(self, registry: ExternalToolRegistry, config_a: ExternalToolConfig, config_b: ExternalToolConfig) -> None:
        registry.register_external_tool(
            SimpleAdapter(config_a), MockConnection(healthy=True),
        )
        registry.register_external_tool(
            SimpleAdapter(config_b), MockConnection(healthy=False),
        )

        results = await registry.health_check_all()
        assert results["tool_a"] is True
        assert results["tool_b"] is False

    def test_discover_tools(self, registry: ExternalToolRegistry, config_a: ExternalToolConfig) -> None:
        registry.register_external_tool(SimpleAdapter(config_a))
        discovered = registry.discover_tools()
        assert len(discovered) == 1

    def test_discover_tools_with_filter(self, registry: ExternalToolRegistry, config_a: ExternalToolConfig) -> None:
        registry.register_external_tool(SimpleAdapter(config_a))
        discovered = registry.discover_tools(capability="op_a")
        assert len(discovered) == 1

        discovered = registry.discover_tools(capability="nonexistent")
        assert len(discovered) == 0

    def test_get_external_tool_name(self, registry: ExternalToolRegistry, config_a: ExternalToolConfig) -> None:
        registry.register_external_tool(SimpleAdapter(config_a))
        assert registry.get_external_tool_name("tool_a__op_a") == "tool_a"
        assert registry.get_external_tool_name("tool_a__op_b") == "tool_a"
        assert registry.get_external_tool_name("nonexistent") is None

    def test_clear(self, registry: ExternalToolRegistry, config_a: ExternalToolConfig) -> None:
        registry.register_external_tool(SimpleAdapter(config_a))
        registry.clear()
        assert registry.count() == 0
        assert registry.list_all_internal_tools() == []

    @pytest.mark.asyncio
    async def test_register_to_tool_registry(self, registry: ExternalToolRegistry, config_a: ExternalToolConfig) -> None:
        """测试注册到内部 ToolRegistry。"""
        registry.register_external_tool(SimpleAdapter(config_a))

        # Mock 内部 ToolRegistry
        mock_internal = MagicMock()
        mock_internal.register_with_handler = MagicMock()

        registered = await registry.register_to_tool_registry(mock_internal)
        assert len(registered) == 2
        assert mock_internal.register_with_handler.call_count == 2

    def test_tool_info_state_without_connection(self, registry: ExternalToolRegistry, config_a: ExternalToolConfig) -> None:
        """无连接的工具应返回 DISCONNECTED 状态。"""
        registry.register_external_tool(SimpleAdapter(config_a))
        infos = registry.list_external_tools()
        assert infos[0].state == ExternalToolState.DISCONNECTED

    def test_tool_info_state_with_connection(self, registry: ExternalToolRegistry, config_a: ExternalToolConfig) -> None:
        """有连接的工具应返回连接的实际状态。"""
        registry.register_external_tool(
            SimpleAdapter(config_a), MockConnection(healthy=True),
        )
        infos = registry.list_external_tools()
        assert infos[0].state == ExternalToolState.CONNECTED
