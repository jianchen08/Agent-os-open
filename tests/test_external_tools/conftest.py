"""外部工具连接机制测试 - 公共 fixtures。"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from tools.external.adapter import ExternalToolAdapter
from tools.external.connection import ExternalToolConnection
from tools.external.interfaces import IExternalToolConnection
from tools.external.types import (
    AuthConfig,
    AuthType,
    ExternalToolCapability,
    ExternalToolConfig,
    ExternalToolState,
    ProtocolType,
    RetryPolicy,
    SandboxResourceLimits,
)


# ── 通用配置 fixtures ──


@pytest.fixture
def http_config() -> ExternalToolConfig:
    """HTTP 协议的测试配置。"""
    return ExternalToolConfig(
        name="test_http_tool",
        display_name="测试 HTTP 工具",
        description="测试用 HTTP 外部工具",
        protocol=ProtocolType.HTTP,
        endpoint="http://localhost:8080",
        connect_timeout=5.0,
        read_timeout=10.0,
        execute_timeout=15.0,
        retry_policy=RetryPolicy(max_retries=2, base_delay=0.01, max_delay=0.1),
        heartbeat_interval=0,
    )


@pytest.fixture
def ws_config() -> ExternalToolConfig:
    """WebSocket 协议的测试配置。"""
    return ExternalToolConfig(
        name="test_ws_tool",
        display_name="测试 WS 工具",
        description="测试用 WebSocket 外部工具",
        protocol=ProtocolType.WEBSOCKET,
        endpoint="ws://localhost:9090",
        connect_timeout=5.0,
        read_timeout=10.0,
        execute_timeout=15.0,
        retry_policy=RetryPolicy(max_retries=2, base_delay=0.01, max_delay=0.1),
        heartbeat_interval=0.5,
    )


@pytest.fixture
def sample_capability() -> ExternalToolCapability:
    """示例能力描述。"""
    return ExternalToolCapability(
        name="test_op",
        description="测试操作",
        input_schema={
            "type": "object",
            "properties": {
                "param1": {"type": "string", "description": "参数1"},
                "param2": {"type": "integer", "description": "参数2"},
            },
            "required": ["param1"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "result": {"type": "string"},
            },
        },
    )


@pytest.fixture
def mock_connection() -> AsyncMock:
    """模拟的 IExternalToolConnection。"""
    conn = AsyncMock(spec=IExternalToolConnection)
    conn.get_state.return_value = ExternalToolState.CONNECTED
    conn.send_request.return_value = {"success": True, "data": "mock"}
    conn.health_check.return_value = True
    return conn


# ── 具体适配器（用于测试基类逻辑）──


class _StubAdapter(ExternalToolAdapter):
    """用于测试的适配器桩。"""

    def __init__(self, config: ExternalToolConfig, **kwargs: Any) -> None:
        super().__init__(config, **kwargs)
        self._executed: list[dict[str, Any]] = []

    def define_schemas(self) -> list[ExternalToolCapability]:
        return [
            ExternalToolCapability(
                name="echo",
                description="回显输入",
                input_schema={
                    "type": "object",
                    "properties": {"msg": {"type": "string"}},
                    "required": ["msg"],
                },
                output_schema={"type": "object", "properties": {"msg": {"type": "string"}}},
            ),
            ExternalToolCapability(
                name="no_schema_op",
                description="无 Schema 操作",
                input_schema={},
            ),
            ExternalToolCapability(
                name="timeout_op",
                description="超时操作",
                input_schema={},
                timeout_override=0.1,
            ),
        ]

    async def _do_execute(
        self,
        operation: str,
        inputs: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._executed.append({"op": operation, "inputs": inputs})
        if operation == "timeout_op":
            await asyncio.sleep(10)
        return {"success": True, "operation": operation, "inputs": inputs}


@pytest.fixture
def stub_adapter(http_config: ExternalToolConfig) -> _StubAdapter:
    """测试用适配器桩。"""
    return _StubAdapter(http_config)
