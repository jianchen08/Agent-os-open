"""
MCP 客户端 - JSON-RPC over stdio 通信协议

暴露接口：
- MCPClient：MCP客户端类
"""

import asyncio
import json
import logging
from typing import Any

from core.exceptions import MCPConnectionError


class MCPClient:
    """简单的 MCP 客户端实现

    支持两种子进程模式:
    - asyncio 模式（use_sync=False）：使用 asyncio.subprocess.Process，适用于 ProactorEventLoop
    - sync 模式（use_sync=True）：使用 subprocess.Popen，适用于 SelectorEventLoop 环境
      I/O 操作通过 asyncio.to_thread 包装为异步调用
    """

    def __init__(self, process: Any, name: str, use_sync: bool = False):
        self.process = process
        self.name = name
        self._request_id = 0
        self._use_sync = use_sync

    def is_alive(self) -> bool:
        """检测子进程是否存活"""
        if self.process is None:
            return False
        if self._use_sync:
            return self.process.poll() is None
        return self.process.returncode is None

    async def initialize(self) -> None:
        """初始化 MCP 连接"""
        init_request = {
            "jsonrpc": "2.0",
            "id": self._get_next_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "clientInfo": {"name": "meta-agent-system", "version": "1.0.0"},
            },
        }

        await self._send_request(init_request)
        response = await self._read_response()

        if "error" in response:
            raise MCPConnectionError(
                message=f"MCP 服务器 '{self.name}' 初始化失败: {response['error']}",
                details={"server": self.name, "error": response["error"]},
            )

    async def list_tools(self) -> Any:
        """列出可用工具"""
        request = {
            "jsonrpc": "2.0",
            "id": self._get_next_id(),
            "method": "tools/list",
            "params": {},
        }

        await self._send_request(request)
        response = await self._read_response()

        if "error" in response:
            raise MCPConnectionError(
                message=f"MCP 服务器 '{self.name}' 获取工具列表失败: {response['error']}",
                details={"server": self.name, "error": response["error"]},
            )

        class ToolsResponse:
            def __init__(self, tools_data: dict[str, Any]) -> None:
                self.tools = tools_data.get("result", {}).get("tools", [])

        return ToolsResponse(response)

    async def call_tool(self, name: str, arguments: dict[str, Any], timeout: float = 120.0) -> Any:
        """调用工具

        使用 asyncio.wait_for 对整个调用过程进行超时保护，
        防止 MCP 服务器无响应时永远阻塞。

        Args:
            name: 工具名称
            arguments: 工具参数
            timeout: 整体超时时间（秒），默认 120 秒

        Raises:
            MCPConnectionError: 超时或连接错误时抛出
        """
        request = {
            "jsonrpc": "2.0",
            "id": self._get_next_id(),
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }

        try:
            await asyncio.wait_for(self._send_request(request), timeout=timeout)
        except TimeoutError:
            raise MCPConnectionError(
                message=f"MCP 服务器 '{self.name}' 工具调用请求发送超时（{timeout}秒）",
                details={"server": self.name, "timeout": timeout},
            ) from None

        try:
            response = await asyncio.wait_for(self._read_response(timeout=timeout), timeout=timeout)
        except TimeoutError:
            raise MCPConnectionError(
                message=f"MCP 服务器 '{self.name}' 工具调用响应超时（{timeout}秒），工具: {name}",
                details={"server": self.name, "tool": name, "timeout": timeout},
            ) from None

        if "error" in response:
            raise MCPConnectionError(
                message=f"MCP 服务器 '{self.name}' 工具调用失败: {response['error']}",
                details={"server": self.name, "error": response["error"]},
            )

        return response.get("result")

    async def close(self) -> None:
        """关闭连接"""
        if not self.process:
            return
        if self._use_sync:
            if self.process.poll() is None:
                self.process.terminate()
                self.process.wait()
        elif self.process.returncode is None:
            self.process.terminate()
            await self.process.wait()

    def _get_next_id(self) -> int:
        """获取下一个请求ID"""
        self._request_id += 1
        return self._request_id

    async def _send_request(self, request: dict[str, Any]) -> None:
        """发送请求"""
        if not self.process or not self.process.stdin:
            raise MCPConnectionError(
                message=f"MCP 服务器 '{self.name}' 进程未启动，无法发送请求",
                details={"server": self.name},
            )

        message = json.dumps(request) + "\n"
        if self._use_sync:
            await asyncio.to_thread(self._sync_write, message.encode())
        else:
            self.process.stdin.write(message.encode())
            await self.process.stdin.drain()

    def _sync_write(self, data: bytes) -> None:
        """同步写入数据到子进程 stdin"""
        self.process.stdin.write(data)
        self.process.stdin.flush()

    async def _read_response(self, timeout: float = 60.0) -> dict[str, Any]:
        """读取响应

        跳过 MCP 服务器在 stdout 输出的非 JSON 日志行，只处理有效的 JSON-RPC 响应。
        每次 readline 用 asyncio.wait_for 包装，超时（默认 60s）后抛出 MCPConnectionError，
        避免 MCP 无响应时永久阻塞。
        """
        if not self.process or not self.process.stdout:
            raise MCPConnectionError(
                message=f"MCP 服务器 '{self.name}' 进程未启动，无法读取响应",
                details={"server": self.name},
            )

        max_attempts = 500
        for _ in range(max_attempts):
            try:
                if self._use_sync:
                    line = await asyncio.wait_for(
                        asyncio.to_thread(self._sync_readline),
                        timeout=timeout,
                    )
                else:
                    line = await asyncio.wait_for(
                        self.process.stdout.readline(),
                        timeout=timeout,
                    )
            except TimeoutError:
                raise MCPConnectionError(
                    message=f"MCP 服务器 '{self.name}' 响应读取超时（{timeout}秒），服务器可能无响应或已挂起",
                    details={"server": self.name, "timeout": timeout},
                ) from None

            if not line:
                raise MCPConnectionError(
                    message=f"MCP 服务器 '{self.name}' 连接已关闭",
                    details={"server": self.name},
                )

            # 强制 UTF-8 解码 + errors='replace' 兜底：
            # Windows 中文系统子进程 stdout 默认 cp936(GBK)，若子进程未自行 reconfigure，
            # 双字节汉字首字节（如 0xCA continuation byte）会让默认 UTF-8 解码抛
            # UnicodeDecodeError，使整个工具调用失败。errors='replace' 保证协议帧可解析，
            # 最坏情况下个别字节被替换为 □ 而非整次调用崩溃。
            line_str = line.decode("utf-8", errors="replace").strip() if isinstance(line, bytes) else line.strip()
            if not line_str:
                continue

            try:
                return json.loads(line_str)
            except json.JSONDecodeError:
                logging.getLogger(__name__).debug(f"[MCP] 跳过非 JSON 行: {line_str[:100]}")
                continue

        raise MCPConnectionError(
            message=f"MCP 服务器 '{self.name}' 响应超时：未找到有效的 JSON 响应",
            details={"server": self.name},
        )

    def _sync_readline(self) -> bytes:
        """同步从子进程 stdout 读取一行"""
        return self.process.stdout.readline()
