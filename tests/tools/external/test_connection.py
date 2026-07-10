"""外部工具连接管理器测试。"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.external.connection import ExternalToolConnection
from tools.external.exceptions import ConnectionError, ExternalTimeoutError
from tools.external.types import (
    AuthConfig,
    AuthType,
    ExternalToolConfig,
    ExternalToolState,
    ProtocolType,
)


@pytest.fixture
def http_config() -> ExternalToolConfig:
    return ExternalToolConfig(
        name="test_http",
        protocol=ProtocolType.HTTP,
        endpoint="http://localhost:8080",
        connect_timeout=5.0,
        read_timeout=10.0,
        heartbeat_interval=0,  # 禁用心跳
    )


@pytest.fixture
def ws_config() -> ExternalToolConfig:
    return ExternalToolConfig(
        name="test_ws",
        protocol=ProtocolType.WEBSOCKET,
        endpoint="ws://localhost:9090/ws",
        connect_timeout=5.0,
        heartbeat_interval=0,
    )


class TestExternalToolConnection:

    def test_initial_state(self, http_config: ExternalToolConfig) -> None:
        conn = ExternalToolConnection(http_config)
        assert conn.get_state() == ExternalToolState.DISCONNECTED

    @pytest.mark.asyncio
    async def test_connect_http(self, http_config: ExternalToolConfig) -> None:
        conn = ExternalToolConnection(http_config)
        with patch("tools.external.connection.aiohttp.ClientSession") as mock_session_cls:
            mock_session = AsyncMock()
            mock_session_cls.return_value = mock_session

            await conn.connect()
            assert conn.get_state() == ExternalToolState.CONNECTED

            await conn.disconnect()
            assert conn.get_state() == ExternalToolState.DISCONNECTED

    @pytest.mark.asyncio
    async def test_connect_idempotent(self, http_config: ExternalToolConfig) -> None:
        conn = ExternalToolConnection(http_config)
        with patch("tools.external.connection.aiohttp.ClientSession"):
            await conn.connect()
            state1 = conn.get_state()
            await conn.connect()  # 重复连接
            state2 = conn.get_state()
            assert state1 == state2 == ExternalToolState.CONNECTED

            await conn.disconnect()

    @pytest.mark.asyncio
    async def test_disconnect_from_disconnected(self, http_config: ExternalToolConfig) -> None:
        conn = ExternalToolConnection(http_config)
        await conn.disconnect()  # 应不报错
        assert conn.get_state() == ExternalToolState.DISCONNECTED

    @pytest.mark.asyncio
    async def test_send_request_not_connected(self, http_config: ExternalToolConfig) -> None:
        conn = ExternalToolConnection(http_config)
        with pytest.raises(ConnectionError, match="未连接"):
            await conn.send_request("test_op", {"key": "val"})

    @pytest.mark.asyncio
    async def test_health_check_not_connected(self, http_config: ExternalToolConfig) -> None:
        conn = ExternalToolConnection(http_config)
        result = await conn.health_check()
        assert result is False

    @pytest.mark.asyncio
    async def test_health_check_http(self, http_config: ExternalToolConfig) -> None:
        conn = ExternalToolConnection(http_config)
        with patch("tools.external.connection.aiohttp.ClientSession") as mock_cls:
            # session.close() 是 async，head() 返回 async context manager
            mock_session = MagicMock()
            mock_session.closed = False
            mock_session.close = AsyncMock()
            mock_cls.return_value = mock_session

            await conn.connect()

            # head() 返回 async context manager
            mock_resp = MagicMock()
            mock_resp.status = 200
            ctx_manager = MagicMock()
            ctx_manager.__aenter__ = AsyncMock(return_value=mock_resp)
            ctx_manager.__aexit__ = AsyncMock(return_value=False)
            mock_session.head.return_value = ctx_manager

            result = await conn.health_check()
            assert result is True

            await conn.disconnect()

    @pytest.mark.asyncio
    async def test_unsupported_protocol(self) -> None:
        config = ExternalToolConfig(
            name="bad_proto",
            endpoint="ftp://localhost",
        )
        # 强制设置无效协议（绕过枚举验证）
        config.protocol = "ftp"  # type: ignore
        conn = ExternalToolConnection(config)
        with pytest.raises(ConnectionError, match="不支持的协议"):
            await conn.connect()


class TestStateTransitions:

    @pytest.mark.asyncio
    async def test_state_transition_connect_disconnect(
        self, http_config: ExternalToolConfig,
    ) -> None:
        conn = ExternalToolConnection(http_config)
        assert conn.get_state() == ExternalToolState.DISCONNECTED

        with patch("tools.external.connection.aiohttp.ClientSession"):
            await conn.connect()
            assert conn.get_state() == ExternalToolState.CONNECTED

            await conn.disconnect()
            assert conn.get_state() == ExternalToolState.DISCONNECTED

    @pytest.mark.asyncio
    async def test_state_error_on_connect_failure(
        self, http_config: ExternalToolConfig,
    ) -> None:
        conn = ExternalToolConnection(http_config)
        with patch(
            "tools.external.connection.aiohttp.ClientSession",
            side_effect=Exception("网络不可达"),
        ):
            with pytest.raises(ConnectionError):
                await conn.connect()
            assert conn.get_state() == ExternalToolState.ERROR


class TestAuthHeaders:

    def test_no_auth(self) -> None:
        config = ExternalToolConfig(auth=AuthConfig(auth_type=AuthType.NONE))
        conn = ExternalToolConnection(config)
        headers = conn._build_auth_headers()
        assert "X-API-Key" not in headers
        assert "Authorization" not in headers

    def test_api_key_auth(self) -> None:
        config = ExternalToolConfig(
            auth=AuthConfig(auth_type=AuthType.API_KEY, secret_key="my_key"),
        )
        conn = ExternalToolConnection(config)
        headers = conn._build_auth_headers()
        assert "X-API-Key" in headers

    def test_bearer_auth(self) -> None:
        config = ExternalToolConfig(
            auth=AuthConfig(auth_type=AuthType.BEARER, secret_key="token_ref"),
        )
        conn = ExternalToolConnection(config)
        headers = conn._build_auth_headers()
        assert "Authorization" in headers
        assert "Bearer" in headers["Authorization"]

    def test_extra_headers(self) -> None:
        config = ExternalToolConfig(
            auth=AuthConfig(
                headers={"X-Custom": "value"},
            ),
        )
        conn = ExternalToolConnection(config)
        headers = conn._build_auth_headers()
        assert headers["X-Custom"] == "value"
