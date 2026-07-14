"""MCP JSON-RPC 服务端。

自研轻量级 JSON-RPC 2.0 over stdio 实现，不依赖外部 MCP 包。
响应 initialize / tools/list / tools/call / resources/read / notifications 请求。

[来源: docs/tasks/task_08_python_sdk.md AC-07-2]
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from lingxi_plugin_sdk.types import LifecycleEvent, ResourceDef, ToolDef


class McpServer:
    """MCP JSON-RPC 服务端。

    通过 stdin/stdout 收发 JSON-RPC 2.0 消息，与 Rust 内核 McpClient 对接。

    支持的 MCP 方法：
    - initialize: 协议握手 + 依赖注入
    - tools/list: 列出已注册工具
    - tools/call: 调用工具
    - resources/read: 读取资源
    - notifications/*: 生命周期通知（fire-and-forget）
    """

    PROTOCOL_VERSION = "2024-11-05"
    SERVER_INFO = {"name": "lingxi-plugin-sdk", "version": "0.2.0"}

    def __init__(
        self,
        tools: dict[str, ToolDef],
        resources: dict[str, ResourceDef],
        lifecycle_handlers: dict[str, Any],
        on_initialize: Any | None = None,
    ) -> None:
        self._tools = tools
        self._resources = resources
        self._lifecycle_handlers = lifecycle_handlers
        self._on_initialize = on_initialize
        self._running = False

    async def run(self) -> None:
        """启动服务端，从 stdin 读取 JSON-RPC 请求，向 stdout 写入响应。

        阻塞运行直到 stdin EOF 或收到 shutdown 信号。
        """
        self._running = True
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await asyncio.get_event_loop().connect_read_pipe(lambda: protocol, sys.stdin)

        while self._running:
            line = await reader.readline()
            if not line:
                break
            line_str = line.decode("utf-8").strip()
            if not line_str:
                continue
            try:
                msg = json.loads(line_str)
            except json.JSONDecodeError:
                continue

            await self._handle_message(msg)

    async def _handle_message(self, msg: dict[str, Any]) -> None:
        """处理单条 JSON-RPC 消息。"""
        method = msg.get("method", "")
        msg_id = msg.get("id")
        params = msg.get("params", {})

        # notification（无 id）—— fire-and-forget
        if msg_id is None:
            await self._handle_notification(method, params)
            return

        # request（有 id）—— 需要响应
        try:
            result = await self._dispatch(method, params)
            self._send_response(msg_id, result)
        except Exception as e:
            self._send_error(msg_id, -32603, str(e))

    async def _dispatch(self, method: str, params: dict[str, Any]) -> Any:
        """分发 JSON-RPC 请求到对应处理器。"""
        if method == "initialize":
            return self._handle_initialize(params)
        if method == "tools/list":
            return self._handle_tools_list()
        if method == "tools/call":
            return await self._handle_tools_call(params)
        if method == "resources/read":
            return await self._handle_resources_read(params)
        raise ValueError(f"unknown method: {method}")

    def _handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        """处理 initialize 请求——协议握手 + 接收依赖注入。"""
        if self._on_initialize is not None:
            self._on_initialize(params)
        return {
            "protocolVersion": self.PROTOCOL_VERSION,
            "serverInfo": self.SERVER_INFO,
            "capabilities": {
                "tools": {},
                "resources": {},
            },
        }

    def _handle_tools_list(self) -> dict[str, Any]:
        """处理 tools/list 请求——返回已注册工具列表。"""
        tools = []
        for td in self._tools.values():
            tools.append(
                {
                    "name": td.name,
                    "description": td.description,
                    "inputSchema": td.schema,
                }
            )
        return {"tools": tools}

    async def _handle_tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        """处理 tools/call 请求——调用指定工具并返回结果。"""
        name = params.get("name", "")
        arguments = params.get("arguments", {})

        td = self._tools.get(name)
        if td is None:
            raise ValueError(f"tool not found: {name}")

        result = td.handler(**arguments) if arguments else td.handler()
        if asyncio.iscoroutine(result):
            result = await result

        return {
            "content": [{"type": "text", "text": json.dumps(result, default=str)}],
            "isError": False,
        }

    async def _handle_resources_read(self, params: dict[str, Any]) -> dict[str, Any]:
        """处理 resources/read 请求——读取指定资源。"""
        uri = params.get("uri", "")
        rd = self._resources.get(uri)
        if rd is None:
            raise ValueError(f"resource not found: {uri}")

        result = rd.handler()
        if asyncio.iscoroutine(result):
            result = await result

        return {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": rd.mime_type,
                    "text": json.dumps(result, default=str),
                }
            ],
        }

    async def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        """处理 notification（生命周期事件）。"""
        # 映射 MCP notification 方法到生命周期事件
        event_map = {
            "notifications/on_load": LifecycleEvent.ON_LOAD,
            "notifications/on_unload": LifecycleEvent.ON_UNLOAD,
            "notifications/on_config_change": LifecycleEvent.ON_CONFIG_CHANGE,
            "notifications/on_pipeline_start": LifecycleEvent.ON_PIPELINE_START,
            "notifications/on_pipeline_end": LifecycleEvent.ON_PIPELINE_END,
            "notifications/on_error": LifecycleEvent.ON_ERROR,
            "notifications/initialized": None,  # 协议通知，忽略
        }

        event = event_map.get(method)
        if event is None:
            return

        handler = self._lifecycle_handlers.get(event.value)
        if handler is not None:
            result = handler(params)
            if asyncio.iscoroutine(result):
                await result

    def _send_response(self, msg_id: str, result: Any) -> None:
        """向 stdout 发送 JSON-RPC 成功响应。"""
        response = {"jsonrpc": "2.0", "id": msg_id, "result": result}
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()

    def _send_error(self, msg_id: str, code: int, message: str) -> None:
        """向 stdout 发送 JSON-RPC 错误响应。"""
        response = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": code, "message": message},
        }
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()

    def stop(self) -> None:
        """停止服务端。"""
        self._running = False
