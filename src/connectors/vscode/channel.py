"""
VSCode 消息通道

通过 HTTP 短轮询实现与 VSCode 扩展的通信。

暴露接口：
- VSCodeChannel: VSCode 消息通道
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from ..types import ConnectorContext, CursorPosition

logger = logging.getLogger(__name__)


class VSCodeChannel:
    """VSCode 消息通道。

    使用 HTTP 短轮询与 VSCode 扩展通信（简化实现，不依赖 websocket 库）。

    Attributes:
        host: VSCode 扩展 HTTP 服务地址
        port: VSCode 扩展 HTTP 服务端口
        timeout: 请求超时时间（秒）
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 9741,
        timeout: float = 5.0,
    ) -> None:
        """初始化消息通道。

        Args:
            host: VSCode 扩展 HTTP 服务地址
            port: VSCode 扩展 HTTP 服务端口
            timeout: 请求超时时间（秒）
        """
        self.host = host
        self.port = port
        self.timeout = timeout
        self._logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    @property
    def base_url(self) -> str:
        """获取基础 URL。

        Returns:
            基础 URL 字符串
        """
        return f"http://{self.host}:{self.port}"

    async def send_request(self, endpoint: str, data: dict[str, Any]) -> dict[str, Any]:
        """发送 HTTP 请求到 VSCode 扩展。

        Args:
            endpoint: API 端点路径
            data: 请求数据

        Returns:
            响应数据字典

        Raises:
            ConnectionError: 连接失败
            TimeoutError: 请求超时
        """
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        body = json.dumps(data).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                response_data = json.loads(resp.read().decode("utf-8"))
                self._logger.debug(f"请求成功: {endpoint}")
                return response_data
        except urllib.error.URLError as e:
            self._logger.error(f"请求失败: {endpoint}, 错误: {e}")
            msg = f"VSCode 扩展连接失败 ({url}): {e}"
            raise ConnectionError(msg) from e
        except TimeoutError as e:
            self._logger.error(f"请求超时: {endpoint}")
            msg = f"VSCode 扩展请求超时 ({url})"
            raise TimeoutError(msg) from e

    async def listen_for_context(self) -> ConnectorContext:
        """监听并获取 VSCode 当前上下文。

        通过轮询 /context 端点获取 VSCode 当前状态。

        Returns:
            VSCode 当前上下文
        """
        try:
            response = await self.send_request("/context", {})
            return self._parse_context(response)
        except (ConnectionError, TimeoutError):
            self._logger.warning("获取上下文失败，返回空上下文")
            return ConnectorContext()

    def _parse_context(self, data: dict[str, Any]) -> ConnectorContext:
        """解析上下文数据。

        Args:
            data: 从 VSCode 扩展获取的原始数据

        Returns:
            解析后的上下文对象
        """
        cursor: CursorPosition | None = None
        cursor_data = data.get("cursor_position")
        if cursor_data and isinstance(cursor_data, dict):
            cursor = CursorPosition(
                line=cursor_data.get("line", 0),
                column=cursor_data.get("column", 0),
            )

        return ConnectorContext(
            active_file=data.get("active_file"),
            selected_text=data.get("selected_text"),
            cursor_position=cursor,
            open_files=data.get("open_files", []),
            metadata=data.get("metadata", {}),
        )

    def is_available(self) -> bool:
        """检查 VSCode 扩展是否可用。

        Returns:
            True 表示扩展可连接
        """
        try:
            url = f"{self.base_url}/health"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status == 200
        except Exception:
            return False
