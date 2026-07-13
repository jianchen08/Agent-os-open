"""外部工具适配器基类测试。"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.external.adapter import ExternalToolAdapter
from tools.external.exceptions import ExecutionError, ExternalTimeoutError
from tools.external.interfaces import IExternalToolConnection
from tools.external.types import (
    ExternalToolCapability,
    ExternalToolConfig,
    ExternalToolState,
    ProtocolType,
    RetryPolicy,
)
from tools.types import Tool, ToolSource


class MockAdapter(ExternalToolAdapter):
    """测试用 Mock 适配器。"""

    def __init__(
        self,
        config: ExternalToolConfig,
        execute_result: dict[str, Any] | None = None,
        execute_side_effect: Exception | None = None,
    ) -> None:
        super().__init__(config)
        self._execute_result = execute_result or {"success": True, "data": "ok"}
        self._execute_side_effect = execute_side_effect
        self.execute_calls: list[tuple[str, dict[str, Any]]] = []

    def define_schemas(self) -> list[ExternalToolCapability]:
        return [
            ExternalToolCapability(
                name="test_operation",
                description="测试操作",
                input_schema={
                    "type": "object",
                    "properties": {
                        "param1": {"type": "string"},
                    },
                    "required": ["param1"],
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "success": {"type": "boolean"},
                    },
                },
            ),
            ExternalToolCapability(
                name="quick_op",
                description="快速操作",
                timeout_override=5.0,
            ),
            ExternalToolCapability(
                name="dangerous_op",
                description="危险操作",
                dangerous=True,
            ),
        ]

    async def _do_execute(
        self,
        operation: str,
        inputs: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.execute_calls.append((operation, inputs))

        if self._execute_side_effect:
            raise self._execute_side_effect

        return self._execute_result


class MockConnection:
    """测试用 Mock 连接。"""

    def __init__(self, state: ExternalToolState = ExternalToolState.CONNECTED) -> None:
        self._state = state

    def get_state(self) -> ExternalToolState:
        return self._state


@pytest.fixture
def config() -> ExternalToolConfig:
    return ExternalToolConfig(
        name="mock_tool",
        display_name="Mock Tool",
        description="测试工具",
        protocol=ProtocolType.HTTP,
        endpoint="http://localhost:8080",
        execute_timeout=10.0,
        retry_policy=RetryPolicy(max_retries=2, base_delay=0.01, max_delay=0.1),
    )


class TestExternalToolAdapter:

    def test_properties(self, config: ExternalToolConfig) -> None:
        adapter = MockAdapter(config)
        assert adapter.name == "mock_tool"
        assert adapter.config is config
        assert adapter.state == ExternalToolState.DISCONNECTED

    def test_state_with_connection(self, config: ExternalToolConfig) -> None:
        adapter = MockAdapter(config)
        adapter.connection = MockConnection(ExternalToolState.CONNECTED)
        assert adapter.state == ExternalToolState.CONNECTED

    def test_get_capabilities(self, config: ExternalToolConfig) -> None:
        adapter = MockAdapter(config)
        caps = adapter.get_capabilities()
        assert len(caps) == 3
        assert caps[0].name == "test_operation"

    def test_get_capabilities_cached(self, config: ExternalToolConfig) -> None:
        adapter = MockAdapter(config)
        caps1 = adapter.get_capabilities()
        caps2 = adapter.get_capabilities()
        assert caps1 is caps2

    def test_get_capability(self, config: ExternalToolConfig) -> None:
        adapter = MockAdapter(config)
        cap = adapter.get_capability("test_operation")
        assert cap is not None
        assert cap.name == "test_operation"

    def test_get_capability_not_found(self, config: ExternalToolConfig) -> None:
        adapter = MockAdapter(config)
        cap = adapter.get_capability("nonexistent")
        assert cap is None

    def test_validate_input_success(self, config: ExternalToolConfig) -> None:
        adapter = MockAdapter(config)
        result = adapter.validate_input("test_operation", {"param1": "value"})
        assert result["param1"] == "value"

    def test_validate_input_unknown_operation(self, config: ExternalToolConfig) -> None:
        adapter = MockAdapter(config)
        with pytest.raises(ExecutionError, match="不支持的操作"):
            adapter.validate_input("unknown_op", {})

    @pytest.mark.asyncio
    async def test_execute_success(self, config: ExternalToolConfig) -> None:
        adapter = MockAdapter(config, execute_result={"success": True, "result": 42})
        result = await adapter.execute("test_operation", {"param1": "hello"})
        assert result["success"] is True
        assert result["result"] == 42
        assert len(adapter.execute_calls) == 1

    @pytest.mark.asyncio
    async def test_execute_timeout_uses_override(self, config: ExternalToolConfig) -> None:
        adapter = MockAdapter(config)
        cap = adapter.get_capability("quick_op")
        assert cap is not None
        assert cap.timeout_override == 5.0

    @pytest.mark.asyncio
    async def test_execute_retries_on_failure(self, config: ExternalToolConfig) -> None:
        adapter = MockAdapter(
            config,
            execute_side_effect=ExecutionError(message="临时失败", tool_name="mock_tool"),
        )
        result = await adapter.execute("test_operation", {"param1": "val"})
        # 应该重试 2 次（max_retries=2）+ 1 次初始 = 3 次
        assert len(adapter.execute_calls) == 3
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_handle_error(self, config: ExternalToolConfig) -> None:
        adapter = MockAdapter(config)
        error = ExecutionError(message="出错了", tool_name="mock_tool")
        result = await adapter.handle_error("test_operation", error)
        assert result["success"] is False
        assert "出错了" in result["error"]
        assert result["operation"] == "test_operation"

    def test_to_tool(self, config: ExternalToolConfig) -> None:
        adapter = MockAdapter(config)
        tools = adapter.to_tool()
        assert len(tools) == 3
        assert tools[0].name == "mock_tool__test_operation"
        assert tools[0].source == ToolSource.HTTP
        assert tools[0].metadata["external_tool"] == "mock_tool"
        assert tools[0].metadata["operation"] == "test_operation"

    def test_calculate_delay(self, config: ExternalToolConfig) -> None:
        adapter = MockAdapter(config)
        policy = RetryPolicy(base_delay=1.0, exponential_base=2.0, jitter=False)

        delay0 = adapter._calculate_delay(policy, 0)
        delay1 = adapter._calculate_delay(policy, 1)
        delay2 = adapter._calculate_delay(policy, 2)

        assert delay0 == 1.0
        assert delay1 == 2.0
        assert delay2 == 4.0

    def test_calculate_delay_with_max(self, config: ExternalToolConfig) -> None:
        adapter = MockAdapter(config)
        policy = RetryPolicy(base_delay=10.0, exponential_base=10.0, max_delay=50.0, jitter=False)

        delay = adapter._calculate_delay(policy, 10)
        assert delay == 50.0  # 被截断到 max_delay

    def test_calculate_delay_with_jitter(self, config: ExternalToolConfig) -> None:
        adapter = MockAdapter(config)
        policy = RetryPolicy(base_delay=1.0, jitter=True)

        delay = adapter._calculate_delay(policy, 0)
        assert 0.5 <= delay <= 1.0  # 抖动在 0.5~1.0 之间
