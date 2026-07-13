"""外部工具注册表测试。"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from tools.external.adapter import ExternalToolAdapter
from tools.external.exceptions import ConfigError
from tools.external.registry import ExternalToolRegistry
from tools.external.types import (
    ExternalToolCapability,
    ExternalToolConfig,
    ExternalToolState,
    ProtocolType,
)
from tools.types import Tool

from tests.test_external_tools.conftest import _StubAdapter


# ════════════════════════════════════════════
# 注册与注销
# ════════════════════════════════════════════


class TestRegistration:
    """注册和注销测试。"""

    def test_register_returns_name(self, stub_adapter: _StubAdapter) -> None:
        """注册返回工具名称。"""
        registry = ExternalToolRegistry()
        name = registry.register_external_tool(stub_adapter)
        assert name == "test_http_tool"

    def test_register_duplicate_raises(self, stub_adapter: _StubAdapter) -> None:
        """重复注册抛出 ConfigError。"""
        registry = ExternalToolRegistry()
        registry.register_external_tool(stub_adapter)
        with pytest.raises(ConfigError) as exc_info:
            registry.register_external_tool(stub_adapter)
        assert "已注册" in str(exc_info.value)

    def test_register_with_connection(self, stub_adapter: _StubAdapter, mock_connection: AsyncMock) -> None:
        """注册时注入连接管理器。"""
        registry = ExternalToolRegistry()
        name = registry.register_external_tool(stub_adapter, connection=mock_connection)
        assert registry.get_connection(name) is mock_connection
        assert stub_adapter.connection is mock_connection

    def test_unregister_existing(self, stub_adapter: _StubAdapter) -> None:
        """注销已注册的工具。"""
        registry = ExternalToolRegistry()
        name = registry.register_external_tool(stub_adapter)
        registry.unregister_external_tool(name)
        assert registry.get_adapter(name) is None

    def test_unregister_nonexistent_no_error(self) -> None:
        """注销不存在的工具不报错。"""
        registry = ExternalToolRegistry()
        registry.unregister_external_tool("nonexistent")  # 不应抛异常

    def test_unregister_cleans_tool_map(self, stub_adapter: _StubAdapter) -> None:
        """注销后清理内部工具名映射。"""
        registry = ExternalToolRegistry()
        name = registry.register_external_tool(stub_adapter)
        tools = stub_adapter.to_tool()
        for t in tools:
            assert registry.get_external_tool_name(t.name) == name

        registry.unregister_external_tool(name)
        for t in tools:
            assert registry.get_external_tool_name(t.name) is None


# ════════════════════════════════════════════
# 查询
# ════════════════════════════════════════════


class TestQueries:
    """查询功能测试。"""

    def test_get_adapter(self, stub_adapter: _StubAdapter) -> None:
        """获取已注册的适配器。"""
        registry = ExternalToolRegistry()
        registry.register_external_tool(stub_adapter)
        assert registry.get_adapter("test_http_tool") is stub_adapter

    def test_get_adapter_not_found(self) -> None:
        """获取不存在的适配器返回 None。"""
        registry = ExternalToolRegistry()
        assert registry.get_adapter("nope") is None

    def test_get_external_tool_name(self, stub_adapter: _StubAdapter) -> None:
        """根据内部工具名查外部工具名。"""
        registry = ExternalToolRegistry()
        registry.register_external_tool(stub_adapter)
        result = registry.get_external_tool_name("test_http_tool__echo")
        assert result == "test_http_tool"

    def test_list_external_tools(self, stub_adapter: _StubAdapter) -> None:
        """列出所有外部工具信息。"""
        registry = ExternalToolRegistry()
        registry.register_external_tool(stub_adapter)
        infos = registry.list_external_tools()
        assert len(infos) == 1
        assert infos[0].name == "test_http_tool"
        assert len(infos[0].capabilities) == 3

    def test_list_all_internal_tools(self, stub_adapter: _StubAdapter) -> None:
        """列出所有转换后的内部 Tool。"""
        registry = ExternalToolRegistry()
        registry.register_external_tool(stub_adapter)
        tools = registry.list_all_internal_tools()
        assert len(tools) == 3
        assert all(isinstance(t, Tool) for t in tools)

    def test_get_tools_by_capability(self, stub_adapter: _StubAdapter) -> None:
        """按能力名查询 Tool。"""
        registry = ExternalToolRegistry()
        registry.register_external_tool(stub_adapter)
        tools = registry.get_tools_by_capability("echo")
        assert len(tools) == 1
        assert tools[0].metadata["operation"] == "echo"

    def test_get_tools_by_capability_no_match(self, stub_adapter: _StubAdapter) -> None:
        """查询不匹配的能力返回空。"""
        registry = ExternalToolRegistry()
        registry.register_external_tool(stub_adapter)
        tools = registry.get_tools_by_capability("nonexistent")
        assert tools == []

    def test_count(self, stub_adapter: _StubAdapter) -> None:
        """注册数量统计。"""
        registry = ExternalToolRegistry()
        assert registry.count() == 0
        registry.register_external_tool(stub_adapter)
        assert registry.count() == 1


# ════════════════════════════════════════════
# 发现工具
# ════════════════════════════════════════════


class TestDiscovery:
    """工具发现测试。"""

    def test_discover_all(self, stub_adapter: _StubAdapter) -> None:
        """发现所有工具。"""
        registry = ExternalToolRegistry()
        registry.register_external_tool(stub_adapter)
        tools = registry.discover_tools()
        assert len(tools) == 1

    def test_discover_by_capability(self, stub_adapter: _StubAdapter) -> None:
        """按能力过滤发现工具。"""
        registry = ExternalToolRegistry()
        registry.register_external_tool(stub_adapter)
        tools = registry.discover_tools(capability="echo")
        assert len(tools) == 1

    def test_discover_no_match(self, stub_adapter: _StubAdapter) -> None:
        """无匹配能力返回空。"""
        registry = ExternalToolRegistry()
        registry.register_external_tool(stub_adapter)
        tools = registry.discover_tools(capability="nonexistent")
        assert tools == []


# ════════════════════════════════════════════
# 健康检查
# ════════════════════════════════════════════


class TestHealthCheck:
    """批量健康检查测试。"""

    @pytest.mark.asyncio
    async def test_health_check_all(self, stub_adapter: _StubAdapter, mock_connection: AsyncMock) -> None:
        """批量健康检查返回结果字典。"""
        mock_connection.health_check.return_value = True
        registry = ExternalToolRegistry()
        registry.register_external_tool(stub_adapter, connection=mock_connection)

        results = await registry.health_check_all()
        assert results["test_http_tool"] is True

    @pytest.mark.asyncio
    async def test_health_check_all_failure(self, stub_adapter: _StubAdapter, mock_connection: AsyncMock) -> None:
        """健康检查失败返回 False。"""
        mock_connection.health_check.side_effect = Exception("boom")
        registry = ExternalToolRegistry()
        registry.register_external_tool(stub_adapter, connection=mock_connection)

        results = await registry.health_check_all()
        assert results["test_http_tool"] is False

    @pytest.mark.asyncio
    async def test_health_check_no_connections(self) -> None:
        """无连接时返回空结果。"""
        registry = ExternalToolRegistry()
        results = await registry.health_check_all()
        assert results == {}


# ════════════════════════════════════════════
# 与 ToolRegistry 集成
# ════════════════════════════════════════════


class TestToolRegistryIntegration:
    """与系统内部 ToolRegistry 集成测试。"""

    @pytest.mark.asyncio
    async def test_register_to_tool_registry(self, stub_adapter: _StubAdapter) -> None:
        """注册到内部 ToolRegistry。"""
        registry = ExternalToolRegistry()
        registry.register_external_tool(stub_adapter)

        mock_tool_registry = MagicMock()
        mock_tool_registry.register_with_handler = MagicMock()

        registered = await registry.register_to_tool_registry(mock_tool_registry)
        assert len(registered) == 3
        assert mock_tool_registry.register_with_handler.call_count == 3

    @pytest.mark.asyncio
    async def test_register_to_tool_registry_error_handling(
        self, stub_adapter: _StubAdapter
    ) -> None:
        """注册失败不阻塞其他工具。"""
        registry = ExternalToolRegistry()
        registry.register_external_tool(stub_adapter)

        mock_tool_registry = MagicMock()
        call_count = 0

        def side_effect(**kwargs: Any) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("registration failed")

        mock_tool_registry.register_with_handler = MagicMock(side_effect=side_effect)

        registered = await registry.register_to_tool_registry(mock_tool_registry)
        # 第一个失败，后续应该继续
        assert len(registered) == 2


# ════════════════════════════════════════════
# clear
# ════════════════════════════════════════════


class TestClear:
    """清空注册表测试。"""

    def test_clear(self, stub_adapter: _StubAdapter) -> None:
        """清空后所有数据清除。"""
        registry = ExternalToolRegistry()
        registry.register_external_tool(stub_adapter)
        assert registry.count() == 1

        registry.clear()
        assert registry.count() == 0
        assert registry.list_all_internal_tools() == []
        assert registry.list_external_tools() == []
