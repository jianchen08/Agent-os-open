"""外部工具适配器基类测试。"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from tools.external.adapter import ExternalToolAdapter
from tools.external.exceptions import ExecutionError, ExternalTimeoutError
from tools.external.types import (
    ExternalToolCapability,
    ExternalToolConfig,
    ExternalToolState,
    ProtocolType,
    RetryPolicy,
)
from tools.types import Tool, ToolCategory, ToolSource

from tests.test_external_tools.conftest import _StubAdapter


# ════════════════════════════════════════════
# 基本属性
# ════════════════════════════════════════════


class TestAdapterProperties:
    """适配器基本属性测试。"""

    def test_config_property(self, stub_adapter: _StubAdapter, http_config: ExternalToolConfig) -> None:
        """config 属性返回初始化时的配置。"""
        assert stub_adapter.config is http_config

    def test_name_property(self, stub_adapter: _StubAdapter) -> None:
        """name 属性返回配置中的名称。"""
        assert stub_adapter.name == "test_http_tool"

    def test_state_without_connection(self, stub_adapter: _StubAdapter) -> None:
        """无连接时状态为 DISCONNECTED。"""
        assert stub_adapter.state == ExternalToolState.DISCONNECTED

    def test_state_with_connection(self, stub_adapter: _StubAdapter, mock_connection: AsyncMock) -> None:
        """有连接时返回连接状态。"""
        stub_adapter.connection = mock_connection
        mock_connection.get_state.return_value = ExternalToolState.CONNECTED
        assert stub_adapter.state == ExternalToolState.CONNECTED

    def test_connection_setter(self, stub_adapter: _StubAdapter, mock_connection: AsyncMock) -> None:
        """connection setter 注入连接。"""
        stub_adapter.connection = mock_connection
        assert stub_adapter.connection is mock_connection


# ════════════════════════════════════════════
# 能力管理
# ════════════════════════════════════════════


class TestCapabilities:
    """能力列表管理测试。"""

    def test_get_capabilities_cached(self, stub_adapter: _StubAdapter) -> None:
        """get_capabilities 结果被缓存。"""
        caps1 = stub_adapter.get_capabilities()
        caps2 = stub_adapter.get_capabilities()
        assert caps1 is caps2

    def test_get_capabilities_returns_list(self, stub_adapter: _StubAdapter) -> None:
        """返回能力列表。"""
        caps = stub_adapter.get_capabilities()
        assert isinstance(caps, list)
        assert len(caps) == 3
        names = {c.name for c in caps}
        assert names == {"echo", "no_schema_op", "timeout_op"}

    def test_get_capability_found(self, stub_adapter: _StubAdapter) -> None:
        """按名称找到能力。"""
        cap = stub_adapter.get_capability("echo")
        assert cap is not None
        assert cap.name == "echo"

    def test_get_capability_not_found(self, stub_adapter: _StubAdapter) -> None:
        """不存在的操作返回 None。"""
        cap = stub_adapter.get_capability("nonexistent")
        assert cap is None


# ════════════════════════════════════════════
# Schema 验证
# ════════════════════════════════════════════


class TestSchemaValidation:
    """输入参数 Schema 验证测试。"""

    def test_validate_valid_input(self, stub_adapter: _StubAdapter) -> None:
        """合法输入通过验证。"""
        result = stub_adapter.validate_input("echo", {"msg": "hello"})
        assert result == {"msg": "hello"}

    def test_validate_unknown_operation(self, stub_adapter: _StubAdapter) -> None:
        """不支持的操出 ExecutionError。"""
        with pytest.raises(ExecutionError) as exc_info:
            stub_adapter.validate_input("unknown_op", {})
        assert "不支持的操作" in str(exc_info.value)

    def test_validate_empty_schema_passes(self, stub_adapter: _StubAdapter) -> None:
        """空 Schema 的操作直接通过。"""
        result = stub_adapter.validate_input("no_schema_op", {"any": "data"})
        assert result == {"any": "data"}


# ════════════════════════════════════════════
# 执行逻辑（含重试）
# ════════════════════════════════════════════


class TestExecute:
    """执行操作测试。"""

    @pytest.mark.asyncio
    async def test_execute_success(self, stub_adapter: _StubAdapter) -> None:
        """正常执行成功。"""
        result = await stub_adapter.execute("echo", {"msg": "hi"})
        assert result["success"] is True
        assert result["operation"] == "echo"

    @pytest.mark.asyncio
    async def test_execute_validates_first(self, stub_adapter: _StubAdapter) -> None:
        """执行前先进行输入验证。"""
        with pytest.raises(ExecutionError):
            await stub_adapter.execute("unknown_op", {})

    @pytest.mark.asyncio
    async def test_execute_timeout_triggers_retry(self, http_config: ExternalToolConfig) -> None:
        """超时触发重试，最终返回错误信息。"""
        adapter = _StubAdapter(http_config)
        result = await adapter.execute("timeout_op", {})
        assert result["success"] is False
        assert "error" in result

    @pytest.mark.asyncio
    async def test_execute_retries_on_connection_error(
        self, http_config: ExternalToolConfig
    ) -> None:
        """连接错误触发重试。"""

        class _FailAdapter(_StubAdapter):
            attempt = 0

            async def _do_execute(self, op: str, inputs: dict, ctx: dict | None = None) -> dict:
                _FailAdapter.attempt += 1
                if _FailAdapter.attempt <= 1:
                    from tools.external.exceptions import ConnectionError as ConnErr
                    raise ConnErr(message="conn drop")
                return {"success": True, "attempt": _FailAdapter.attempt}

        adapter = _FailAdapter(http_config)
        result = await adapter.execute("echo", {"msg": "hi"})
        assert result["success"] is True
        assert result["attempt"] == 2


# ════════════════════════════════════════════
# 错误处理
# ════════════════════════════════════════════


class TestHandleError:
    """错误处理测试。"""

    @pytest.mark.asyncio
    async def test_handle_error_returns_standard_dict(self, stub_adapter: _StubAdapter) -> None:
        """handle_error 返回标准化错误字典。"""
        err = ExecutionError(message="something went wrong", operation="echo")
        result = await stub_adapter.handle_error("echo", err)
        assert result["success"] is False
        assert "error" in result
        assert result["operation"] == "echo"
        assert result["tool_name"] == "test_http_tool"


# ════════════════════════════════════════════
# to_tool() 转换
# ════════════════════════════════════════════


class TestToTool:
    """转换为内部 Tool 对象测试。"""

    def test_to_tool_returns_list(self, stub_adapter: _StubAdapter) -> None:
        """每个能力对应一个 Tool 对象。"""
        tools = stub_adapter.to_tool()
        assert len(tools) == 3
        assert all(isinstance(t, Tool) for t in tools)

    def test_tool_name_format(self, stub_adapter: _StubAdapter) -> None:
        """Tool 名格式为 {config.name}__{cap.name}。"""
        tools = stub_adapter.to_tool()
        names = {t.name for t in tools}
        assert "test_http_tool__echo" in names
        assert "test_http_tool__no_schema_op" in names

    def test_tool_metadata(self, stub_adapter: _StubAdapter) -> None:
        """Tool 元数据包含外部工具信息。"""
        tools = stub_adapter.to_tool()
        echo_tool = next(t for t in tools if "echo" in t.name)
        assert echo_tool.metadata["external_tool"] == "test_http_tool"
        assert echo_tool.metadata["operation"] == "echo"
        assert echo_tool.source == ToolSource.HTTP
        assert echo_tool.category == ToolCategory.EXECUTION


# ════════════════════════════════════════════
# 重试延迟计算
# ════════════════════════════════════════════


class TestRetryDelay:
    """指数退避延迟计算测试。"""

    def test_exponential_backoff(self, stub_adapter: _StubAdapter) -> None:
        """延迟按指数增长。"""
        policy = RetryPolicy(
            max_retries=5,
            base_delay=1.0,
            exponential_base=2.0,
            max_delay=100.0,
            jitter=False,
        )
        d0 = stub_adapter._calculate_delay(policy, 0)
        d1 = stub_adapter._calculate_delay(policy, 1)
        d2 = stub_adapter._calculate_delay(policy, 2)
        assert d0 == 1.0
        assert d1 == 2.0
        assert d2 == 4.0

    def test_max_delay_cap(self, stub_adapter: _StubAdapter) -> None:
        """延迟不超过 max_delay。"""
        policy = RetryPolicy(
            base_delay=1.0, exponential_base=10.0, max_delay=5.0, jitter=False
        )
        d = stub_adapter._calculate_delay(policy, 100)
        assert d == 5.0

    def test_jitter_reduces_delay(self, stub_adapter: _StubAdapter) -> None:
        """jitter 使延迟在 50%-100% 之间。"""
        policy = RetryPolicy(base_delay=10.0, jitter=True, max_delay=1000.0)
        delays = [stub_adapter._calculate_delay(policy, 0) for _ in range(100)]
        assert all(5.0 <= d <= 10.0 for d in delays)


# ════════════════════════════════════════════
# define_schemas 未实现
# ════════════════════════════════════════════


class TestNotImplemented:
    """子类未实现抽象方法的报错。"""

    def test_define_schemas_raises(self, http_config: ExternalToolConfig) -> None:
        """直接实例化基类调用 define_schemas 报错。"""
        adapter = ExternalToolAdapter(http_config)
        with pytest.raises(NotImplementedError):
            adapter.define_schemas()

    @pytest.mark.asyncio
    async def test_do_execute_raises(self, http_config: ExternalToolConfig) -> None:
        """直接实例化基类调用 _do_execute 报错。"""
        adapter = ExternalToolAdapter(http_config)
        with pytest.raises(NotImplementedError):
            await adapter._do_execute("op", {})
