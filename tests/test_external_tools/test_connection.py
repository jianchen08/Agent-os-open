"""外部工具连接管理器测试。"""

from __future__ import annotations

import asyncio
from typing import Any
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
    RetryPolicy,
)


# ════════════════════════════════════════════
# 初始化
# ════════════════════════════════════════════


class TestConnectionInit:
    """连接管理器初始化测试。"""

    def test_initial_state(self, http_config: ExternalToolConfig) -> None:
        """初始状态为 DISCONNECTED。"""
        conn = ExternalToolConnection(http_config)
        assert conn.get_state() == ExternalToolState.DISCONNECTED
        assert conn.state == ExternalToolState.DISCONNECTED

    def test_stores_config(self, http_config: ExternalToolConfig) -> None:
        """保存配置引用。"""
        conn = ExternalToolConnection(http_config)
        assert conn._config is http_config


# ════════════════════════════════════════════
# HTTP 连接
# ════════════════════════════════════════════


class TestHTTPConnection:
    """HTTP 协议连接测试。"""

    @pytest.mark.asyncio
    async def test_connect_http_success(self, http_config: ExternalToolConfig) -> None:
        """HTTP 连接成功，状态变为 CONNECTED。"""
        conn = ExternalToolConnection(http_config)
        with patch("tools.external.connection.aiohttp.ClientSession") as mock_session_cls:
            mock_session = AsyncMock()
            mock_session.closed = False
            mock_session_cls.return_value = mock_session

            await conn.connect()
            assert conn.get_state() == ExternalToolState.CONNECTED

            await conn.disconnect()

    @pytest.mark.asyncio
    async def test_connect_already_connected(self, http_config: ExternalToolConfig) -> None:
        """已连接时跳过重复连接。"""
        conn = ExternalToolConnection(http_config)
        conn._state = ExternalToolState.CONNECTED
        with patch("tools.external.connection.aiohttp.ClientSession") as mock_cls:
            await conn.connect()
            # 不应创建新 session
            mock_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_connect_failure_sets_error(self, http_config: ExternalToolConfig) -> None:
        """连接失败状态变为 ERROR。"""
        conn = ExternalToolConnection(http_config)
        with patch("tools.external.connection.aiohttp.ClientSession", side_effect=Exception("fail")):
            with pytest.raises(ConnectionError):
                await conn.connect()
            assert conn.get_state() == ExternalToolState.ERROR


# ════════════════════════════════════════════
# WebSocket 连接
# ════════════════════════════════════════════


class TestWebSocketConnection:
    """WebSocket 协议连接测试。"""

    @pytest.mark.asyncio
    async def test_connect_ws_success(self, ws_config: ExternalToolConfig) -> None:
        """WebSocket 连接成功。"""
        conn = ExternalToolConnection(ws_config)
        mock_ws = AsyncMock()
        mock_ws.closed = False

        with patch("tools.external.connection.aiohttp.ClientSession") as mock_session_cls:
            mock_session = AsyncMock()
            mock_session.closed = False
            mock_session.ws_connect = AsyncMock(return_value=mock_ws)
            mock_session_cls.return_value = mock_session

            await conn.connect()
            assert conn.get_state() == ExternalToolState.CONNECTED

            await conn.disconnect()


# ════════════════════════════════════════════
# 断开连接
# ════════════════════════════════════════════


class TestDisconnect:
    """断开连接测试。"""

    @pytest.mark.asyncio
    async def test_disconnect_sets_disconnected(self, http_config: ExternalToolConfig) -> None:
        """断开后状态变为 DISCONNECTED。"""
        conn = ExternalToolConnection(http_config)
        conn._state = ExternalToolState.CONNECTED
        conn._session = AsyncMock()
        conn._session.closed = False
        conn._session.close = AsyncMock()

        await conn.disconnect()
        assert conn.get_state() == ExternalToolState.DISCONNECTED
        assert conn._session is None

    @pytest.mark.asyncio
    async def test_disconnect_cancels_heartbeat(self, ws_config: ExternalToolConfig) -> None:
        """断开连接清理心跳任务引用。"""
        conn = ExternalToolConnection(ws_config)
        conn._state = ExternalToolState.CONNECTED
        conn._heartbeat_task = asyncio.create_task(asyncio.sleep(100))

        await conn.disconnect()
        assert conn._heartbeat_task is None

    @pytest.mark.asyncio
    async def test_disconnect_cancels_reconnect(self, http_config: ExternalToolConfig) -> None:
        """断开连接清理重连任务引用。"""
        conn = ExternalToolConnection(http_config)
        conn._state = ExternalToolState.CONNECTED
        conn._reconnect_task = asyncio.create_task(asyncio.sleep(100))

        await conn.disconnect()
        assert conn._reconnect_task is None


# ════════════════════════════════════════════
# 发送请求
# ════════════════════════════════════════════


class TestSendRequest:
    """发送请求测试。"""

    @pytest.mark.asyncio
    async def test_send_request_not_connected(self, http_config: ExternalToolConfig) -> None:
        """未连接时发送请求抛出 ConnectionError。"""
        conn = ExternalToolConnection(http_config)
        with pytest.raises(ConnectionError):
            await conn.send_request("op", {})

    @pytest.mark.asyncio
    async def test_send_http_request_success(self, http_config: ExternalToolConfig) -> None:
        """HTTP 请求成功。"""
        conn = ExternalToolConnection(http_config)
        conn._state = ExternalToolState.CONNECTED

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"success": True})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.post = MagicMock(return_value=mock_resp)
        conn._session = mock_session

        result = await conn.send_request("test_op", {"key": "val"})
        assert result == {"success": True}

    @pytest.mark.asyncio
    async def test_send_http_request_error_status(self, http_config: ExternalToolConfig) -> None:
        """HTTP 错误状态码抛出 ConnectionError。"""
        conn = ExternalToolConnection(http_config)
        conn._state = ExternalToolState.CONNECTED

        mock_resp = AsyncMock()
        mock_resp.status = 500
        mock_resp.text = AsyncMock(return_value="Internal Server Error")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.post = MagicMock(return_value=mock_resp)
        conn._session = mock_session

        with pytest.raises(ConnectionError):
            await conn.send_request("test_op", {})

    @pytest.mark.asyncio
    async def test_send_request_session_not_initialized(self, http_config: ExternalToolConfig) -> None:
        """Session 未初始化时抛出 ConnectionError。"""
        conn = ExternalToolConnection(http_config)
        conn._state = ExternalToolState.CONNECTED
        conn._session = None

        with pytest.raises(ConnectionError) as exc_info:
            await conn._send_http("op", {}, 5.0)
        assert "未初始化" in str(exc_info.value)


# ════════════════════════════════════════════
# 健康检查
# ════════════════════════════════════════════


class TestHealthCheck:
    """健康检查测试。"""

    @pytest.mark.asyncio
    async def test_health_check_not_connected(self, http_config: ExternalToolConfig) -> None:
        """未连接时健康检查返回 False。"""
        conn = ExternalToolConnection(http_config)
        result = await conn.health_check()
        assert result is False

    @pytest.mark.asyncio
    async def test_health_check_http_success(self, http_config: ExternalToolConfig) -> None:
        """HTTP 健康检查成功。"""
        conn = ExternalToolConnection(http_config)
        conn._state = ExternalToolState.CONNECTED

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.head = MagicMock(return_value=mock_resp)
        conn._session = mock_session

        result = await conn.health_check()
        assert result is True

    @pytest.mark.asyncio
    async def test_health_check_http_no_session(self, http_config: ExternalToolConfig) -> None:
        """HTTP Session 为 None 时返回 False。"""
        conn = ExternalToolConnection(http_config)
        conn._state = ExternalToolState.CONNECTED
        conn._session = None
        result = await conn.health_check()
        assert result is False


# ════════════════════════════════════════════
# 自动重连
# ════════════════════════════════════════════


class TestReconnect:
    """自动重连测试。"""

    @pytest.mark.asyncio
    async def test_reconnect_success(self, http_config: ExternalToolConfig) -> None:
        """重连成功，状态恢复 CONNECTED。"""
        conn = ExternalToolConnection(http_config)
        conn._state = ExternalToolState.ERROR

        with patch("tools.external.connection.aiohttp.ClientSession") as mock_cls:
            mock_session = AsyncMock()
            mock_session.closed = False
            mock_cls.return_value = mock_session

            await conn._reconnect()
            assert conn.get_state() == ExternalToolState.CONNECTED

    @pytest.mark.asyncio
    async def test_reconnect_all_fail(self, http_config: ExternalToolConfig) -> None:
        """所有重连失败后状态变为 ERROR。"""
        http_config.retry_policy = RetryPolicy(max_retries=2, base_delay=0.01, max_delay=0.05)
        conn = ExternalToolConnection(http_config)
        conn._state = ExternalToolState.ERROR

        with patch(
            "tools.external.connection.aiohttp.ClientSession",
            side_effect=Exception("nope"),
        ):
            await conn._reconnect()
            assert conn.get_state() == ExternalToolState.ERROR


# ════════════════════════════════════════════
# 认证头构建
# ════════════════════════════════════════════


class TestAuthHeaders:
    """认证头构建测试。"""

    def test_no_auth(self, http_config: ExternalToolConfig) -> None:
        """无认证时不添加额外头。"""
        conn = ExternalToolConnection(http_config)
        headers = conn._build_auth_headers()
        assert "X-API-Key" not in headers
        assert "Authorization" not in headers

    def test_api_key_auth(self) -> None:
        """API Key 认证添加 X-API-Key 头（值为模板引用）。"""
        config = ExternalToolConfig(
            auth=AuthConfig(auth_type=AuthType.API_KEY, secret_key="my_key")
        )
        conn = ExternalToolConnection(config)
        headers = conn._build_auth_headers()
        assert "X-API-Key" in headers
        assert "{secret:my_key}" in headers["X-API-Key"]

    def test_bearer_auth(self) -> None:
        """Bearer 认证添加 Authorization 头（值为模板引用）。"""
        config = ExternalToolConfig(
            auth=AuthConfig(auth_type=AuthType.BEARER, secret_key="token123")
        )
        conn = ExternalToolConnection(config)
        headers = conn._build_auth_headers()
        assert "Authorization" in headers
        assert "Bearer {secret:token123}" == headers["Authorization"]

    def test_custom_headers_merged(self) -> None:
        """自定义 headers 合并到认证头中。"""
        config = ExternalToolConfig(
            auth=AuthConfig(headers={"X-Custom": "value"})
        )
        conn = ExternalToolConnection(config)
        headers = conn._build_auth_headers()
        assert headers["X-Custom"] == "value"


# ════════════════════════════════════════════
# 状态机转换
# ════════════════════════════════════════════


class TestStateMachine:
    """连接状态机转换测试。"""

    @pytest.mark.asyncio
    async def test_state_transition_disconnected_to_connecting(self, http_config: ExternalToolConfig) -> None:
        """DISCONNECTED → CONNECTING → CONNECTED。"""
        conn = ExternalToolConnection(http_config)
        assert conn.get_state() == ExternalToolState.DISCONNECTED

        with patch("tools.external.connection.aiohttp.ClientSession") as mock_cls:
            mock_session = AsyncMock()
            mock_session.closed = False
            mock_cls.return_value = mock_session

            await conn.connect()
            assert conn.get_state() == ExternalToolState.CONNECTED

            await conn.disconnect()
            assert conn.get_state() == ExternalToolState.DISCONNECTED

    @pytest.mark.asyncio
    async def test_state_transition_error_on_failure(self, http_config: ExternalToolConfig) -> None:
        """连接失败时状态变为 ERROR。"""
        conn = ExternalToolConnection(http_config)
        with patch("tools.external.connection.aiohttp.ClientSession", side_effect=Exception("boom")):
            with pytest.raises(ConnectionError):
                await conn.connect()
            assert conn.get_state() == ExternalToolState.ERROR
