"""外部工具连接管理器。

暴露接口：
- ExternalToolConnection：基于 HTTP/WebSocket 的连接管理实现
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

import aiohttp

from tools.external.exceptions import ConnectionError, ExternalTimeoutError
from tools.external.interfaces import IExternalToolConnection
from tools.external.types import (
    AuthType,
    ExternalToolConfig,
    ExternalToolState,
    ProtocolType,
)

logger = logging.getLogger(__name__)


class ExternalToolConnection(IExternalToolConnection):
    """外部工具连接管理器。

    支持 HTTP 和 WebSocket 双协议，提供：
    - 自动重连（指数退避）
    - 连接池管理
    - 心跳保活
    - 健康检查
    - 连接状态机管理
    """

    def __init__(self, config: ExternalToolConfig) -> None:
        """初始化连接管理器。

        Args:
            config: 外部工具配置
        """
        self._config = config
        self._state = ExternalToolState.DISCONNECTED
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._last_health_check: float = 0.0
        self._logger = logging.getLogger(f"{__name__}.{config.name}")

    @property
    def state(self) -> ExternalToolState:
        """获取当前状态。"""
        return self._state

    def get_state(self) -> ExternalToolState:
        """获取当前连接状态（接口实现）。"""
        return self._state

    def _set_state(self, new_state: ExternalToolState) -> None:
        """安全更新状态。"""
        old_state = self._state
        self._state = new_state
        if old_state != new_state:
            self._logger.info(
                "状态变更 | tool=%s | %s → %s",
                self._config.name,
                old_state.value,
                new_state.value,
            )

    async def connect(self) -> None:
        """建立连接。"""
        async with self._lock:
            if self._state == ExternalToolState.CONNECTED:
                self._logger.debug("已连接，跳过 | tool=%s", self._config.name)
                return

            self._set_state(ExternalToolState.CONNECTING)

            try:
                if self._config.protocol == ProtocolType.HTTP:
                    await self._connect_http()
                elif self._config.protocol == ProtocolType.WEBSOCKET:
                    await self._connect_websocket()
                else:
                    raise ConnectionError(
                        message=f"不支持的协议: {self._config.protocol}",
                        tool_name=self._config.name,
                        endpoint=self._config.endpoint,
                    )

                self._set_state(ExternalToolState.CONNECTED)
                self._last_health_check = time.monotonic()

                # 启动心跳
                if self._config.heartbeat_interval > 0:
                    self._heartbeat_task = asyncio.create_task(
                        self._heartbeat_loop(),
                    )

                self._logger.info(
                    "连接成功 | tool=%s | protocol=%s | endpoint=%s",
                    self._config.name,
                    self._config.protocol.value,
                    self._config.endpoint,
                )

            except Exception as e:
                self._set_state(ExternalToolState.ERROR)
                raise ConnectionError(
                    message=f"连接失败: {e}",
                    tool_name=self._config.name,
                    endpoint=self._config.endpoint,
                    cause=e,
                ) from e

    async def disconnect(self) -> None:
        """断开连接。"""
        async with self._lock:
            # 取消心跳任务
            if self._heartbeat_task and not self._heartbeat_task.done():
                self._heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._heartbeat_task
                self._heartbeat_task = None

            # 取消重连任务
            if self._reconnect_task and not self._reconnect_task.done():
                self._reconnect_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._reconnect_task
                self._reconnect_task = None

            # 关闭 WebSocket
            if self._ws and not self._ws.closed:
                await self._ws.close()
                self._ws = None

            # 关闭 HTTP Session
            if self._session and not self._session.closed:
                await self._session.close()
                self._session = None

            self._set_state(ExternalToolState.DISCONNECTED)
            self._logger.info("已断开连接 | tool=%s", self._config.name)

    async def health_check(self) -> bool:
        """执行健康检查。

        Returns:
            连接是否健康
        """
        if self._state != ExternalToolState.CONNECTED:
            return False

        try:
            if self._config.protocol == ProtocolType.HTTP:
                return await self._health_check_http()
            if self._config.protocol == ProtocolType.WEBSOCKET:
                return await self._health_check_websocket()
            return False
        except Exception as e:
            self._logger.warning(
                "健康检查失败 | tool=%s | error=%s",
                self._config.name,
                e,
            )
            return False

    async def send_request(
        self,
        operation: str,
        payload: dict[str, Any],
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """发送请求到外部工具。

        Args:
            operation: 操作名称
            payload: 请求参数
            timeout: 超时时间

        Returns:
            响应数据

        Raises:
            ConnectionError: 连接异常
            ExternalTimeoutError: 超时
        """
        if self._state != ExternalToolState.CONNECTED:
            raise ConnectionError(
                message="未连接",
                tool_name=self._config.name,
                endpoint=self._config.endpoint,
            )

        effective_timeout = timeout or self._config.read_timeout

        try:
            if self._config.protocol == ProtocolType.HTTP:
                return await self._send_http(operation, payload, effective_timeout)
            if self._config.protocol == ProtocolType.WEBSOCKET:
                return await self._send_websocket(operation, payload, effective_timeout)
            raise ConnectionError(
                message=f"不支持的协议: {self._config.protocol}",
                tool_name=self._config.name,
            )
        except asyncio.TimeoutError as e:
            raise ExternalTimeoutError(
                message=f"请求超时 ({effective_timeout}s) | op={operation}",
                tool_name=self._config.name,
                timeout_seconds=effective_timeout,
                operation=operation,
            ) from e

    # ---- HTTP 协议实现 ----

    async def _connect_http(self) -> None:
        """建立 HTTP 连接（创建 Session）。"""
        connector = aiohttp.TCPConnector(
            limit=self._config.max_connections,
        )
        timeout = aiohttp.ClientTimeout(
            connect=self._config.connect_timeout,
            sock_read=self._config.read_timeout,
        )
        self._session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers=self._build_auth_headers(),
        )

    async def _health_check_http(self) -> bool:
        """HTTP 健康检查（HEAD 或 GET /health）。"""
        if self._session is None or self._session.closed:
            return False

        try:
            health_url = f"{self._config.endpoint}/health"
            async with self._session.head(health_url) as resp:
                self._last_health_check = time.monotonic()
                return resp.status < 500
        except Exception:
            return False

    async def _send_http(
        self,
        operation: str,
        payload: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        """发送 HTTP 请求。"""
        if self._session is None or self._session.closed:
            raise ConnectionError(
                message="HTTP Session 未初始化",
                tool_name=self._config.name,
            )

        url = f"{self._config.endpoint}/{operation}"
        async with asyncio.timeout(timeout):
            async with self._session.post(url, json=payload) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    raise ConnectionError(
                        message=f"HTTP {resp.status}: {text[:200]}",
                        tool_name=self._config.name,
                        endpoint=url,
                    )
                return await resp.json()

    # ---- WebSocket 协议实现 ----

    async def _connect_websocket(self) -> None:
        """建立 WebSocket 连接。"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers=self._build_auth_headers(),
            )

        try:
            self._ws = await self._session.ws_connect(
                self._config.endpoint,
                heartbeat=self._config.heartbeat_interval,
            )
        except Exception as e:
            raise ConnectionError(
                message=f"WebSocket 连接失败: {e}",
                tool_name=self._config.name,
                endpoint=self._config.endpoint,
                cause=e,
            ) from e

    async def _health_check_websocket(self) -> bool:
        """WebSocket 健康检查。"""
        if self._ws is None or self._ws.closed:
            return False
        self._last_health_check = time.monotonic()
        return True

    async def _send_websocket(
        self,
        operation: str,
        payload: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        """发送 WebSocket 请求。"""
        if self._ws is None or self._ws.closed:
            raise ConnectionError(
                message="WebSocket 未连接",
                tool_name=self._config.name,
            )

        message = {"operation": operation, "payload": payload}
        await self._ws.send_json(message)

        async with asyncio.timeout(timeout):
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = msg.json()
                    if data.get("operation") == operation:
                        return data
                elif msg.type in (
                    aiohttp.WSMsgType.ERROR,
                    aiohttp.WSMsgType.CLOSED,
                ):
                    raise ConnectionError(
                        message="WebSocket 连接异常关闭",
                        tool_name=self._config.name,
                    )

        raise ExternalTimeoutError(
            message=f"WebSocket 响应超时 | op={operation}",
            tool_name=self._config.name,
            timeout_seconds=timeout,
            operation=operation,
        )

    # ---- 心跳保活 ----

    async def _heartbeat_loop(self) -> None:
        """心跳循环。"""
        try:
            while self._state == ExternalToolState.CONNECTED:
                await asyncio.sleep(self._config.heartbeat_interval)
                is_healthy = await self.health_check()
                if not is_healthy:
                    self._logger.warning(
                        "心跳检测失败 | tool=%s | 触发重连",
                        self._config.name,
                    )
                    await self._reconnect()
                    return
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self._logger.error(
                "心跳循环异常 | tool=%s | error=%s",
                self._config.name,
                e,
            )

    async def _reconnect(self) -> None:
        """自动重连（指数退避）。"""
        self._set_state(ExternalToolState.RECONNECTING)
        policy = self._config.retry_policy

        for attempt in range(policy.max_retries):
            try:
                # 先断开旧连接
                if self._ws and not self._ws.closed:
                    await self._ws.close()
                    self._ws = None

                # 重新连接
                if self._config.protocol == ProtocolType.HTTP:
                    await self._connect_http()
                elif self._config.protocol == ProtocolType.WEBSOCKET:
                    await self._connect_websocket()

                self._set_state(ExternalToolState.CONNECTED)
                self._logger.info(
                    "重连成功 | tool=%s | attempt=%d",
                    self._config.name,
                    attempt + 1,
                )
                return

            except Exception as e:
                self._logger.warning(
                    "重连失败 | tool=%s | attempt=%d/%d | error=%s",
                    self._config.name,
                    attempt + 1,
                    policy.max_retries,
                    e,
                )

                delay = min(
                    policy.base_delay * (policy.exponential_base**attempt),
                    policy.max_delay,
                )
                await asyncio.sleep(delay)

        self._set_state(ExternalToolState.ERROR)
        self._logger.error(
            "重连全部失败 | tool=%s",
            self._config.name,
        )

    # ---- 辅助方法 ----

    def _build_auth_headers(self) -> dict[str, str]:
        """构建认证头。

        注意：实际密钥值从 ISecretManager 获取，此处仅构建框架。
        密钥引用键名存在 config.auth.secret_key 中。
        """
        headers: dict[str, str] = {}
        auth = self._config.auth

        if auth.auth_type == AuthType.API_KEY:
            headers["X-API-Key"] = f"{{secret:{auth.secret_key}}}"
        elif auth.auth_type == AuthType.BEARER:
            headers["Authorization"] = f"Bearer {{secret:{auth.secret_key}}}"

        headers.update(auth.headers)
        return headers
