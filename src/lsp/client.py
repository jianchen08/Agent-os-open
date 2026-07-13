"""
LSP 客户端

暴露接口：
- LSPClient：LSPClient类
"""

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from src.lsp.types import (
    CompletionItem,
    Diagnostic,
    Location,
    LSPRequest,
    LSPResponse,
    LSPServerInfo,
    Position,
)

logger = logging.getLogger(__name__)


class LSPClient:
    """
    LSP 客户端

    提供基本的 LSP 功能：
    - Definition: 跳转到定义
    - References: 查找引用
    - Diagnostics: 代码诊断
    - Completion: 代码补全
    """

    def __init__(self, server_info: LSPServerInfo):
        """初始化 LSP 客户端"""
        self.server_info = server_info
        self.process: asyncio.subprocess.Process | None = None
        self.request_id = 0
        self.initialized = False

    @staticmethod
    def is_server_installed(command: str) -> bool:
        """检查 LSP 服务器命令是否在 PATH 中可用"""
        return shutil.which(command) is not None

    async def start(self) -> bool:
        """启动 LSP 服务器"""
        try:
            # 检查服务器是否已安装
            if not self.is_server_installed(self.server_info.command):
                logger.error(
                    f"LSP 服务器未安装: {self.server_info.name} (命令 '{self.server_info.command}' 不在 PATH 中)"
                )
                return False

            # 启动 LSP 服务器进程
            self.process = await asyncio.create_subprocess_exec(
                self.server_info.command,
                *self.server_info.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.server_info.env,
            )

            # 初始化 LSP 会话
            await self._initialize()
            self.initialized = True
            logger.info(f"LSP 服务器已启动: {self.server_info.name}")
            return True

        except Exception as e:
            logger.error(f"启动 LSP 服务器失败: {e}")
            if self.process:
                self.process.terminate()
                await self.process.wait()
                self.process = None
            return False

    async def stop(self):
        """停止 LSP 服务器"""
        if self.initialized:
            await self._shutdown()
            self.initialized = False

        if self.process:
            self.process.terminate()
            await self.process.wait()
            self.process = None
            logger.info("LSP 服务器已停止")

    async def _initialize(self):
        """初始化 LSP 会话"""
        request = LSPRequest(
            id=self._next_id(),
            method="initialize",
            params={
                "processId": os.getpid(),
                "rootUri": Path.cwd().as_uri(),
                "capabilities": {
                    "textDocument": {
                        "definition": {"dynamicRegistration": True},
                        "references": {"dynamicRegistration": True},
                        "diagnostic": {"dynamicRegistration": True},
                        "completion": {
                            "dynamicRegistration": True,
                            "completionItem": {
                                "snippetSupport": True,
                                "documentationFormat": ["markdown", "plaintext"],
                            },
                        },
                    },
                },
            },
        )

        response = await self._send_request(request)
        if response.error:
            raise Exception(f"初始化失败: {response.error}")

        # 发送 initialized 通知
        await self._send_notification("initialized", {})

    async def _shutdown(self):
        """关闭 LSP 会话"""
        request = LSPRequest(
            id=self._next_id(),
            method="shutdown",
            params={},
        )
        await self._send_request(request)

        # 发送 exit 通知
        await self._send_notification("exit", {})

    async def go_to_definition(
        self,
        uri: str,
        position: Position,
    ) -> list[Location]:
        """跳转到定义"""
        request = LSPRequest(
            id=self._next_id(),
            method="textDocument/definition",
            params={
                "textDocument": {"uri": uri},
                "position": position.dict(),
            },
        )

        response = await self._send_request(request)
        if response.error:
            raise Exception(f"获取定义失败: {response.error}")

        # 解析结果
        result = response.result
        if not result:
            return []

        # 单个 Location
        if isinstance(result, dict):
            return [Location(**result)]

        # Location[]
        return [Location(**item) for item in result]

    async def find_references(
        self,
        uri: str,
        position: Position,
        context: dict[str, Any] | None = None,
    ) -> list[Location]:
        """查找引用"""
        request = LSPRequest(
            id=self._next_id(),
            method="textDocument/references",
            params={
                "textDocument": {"uri": uri},
                "position": position.dict(),
                "context": context or {"includeDeclaration": True},
            },
        )

        response = await self._send_request(request)
        if response.error:
            raise Exception(f"查找引用失败: {response.error}")

        result = response.result
        if not result:
            return []

        return [Location(**item) for item in result]

    async def get_diagnostics(
        self,
        uri: str,
    ) -> list[Diagnostic]:
        """获取诊断信息"""
        # 诊断通常通过推送通知获取，这里简化实现
        # 实际应该监听 textDocument/publishDiagnostics 通知
        request = LSPRequest(
            id=self._next_id(),
            method="textDocument/diagnostic",
            params={
                "textDocument": {"uri": uri},
            },
        )

        response = await self._send_request(request)
        if response.error:
            # 如果不支持 diagnostic 方法，返回空列表
            return []

        result = response.result
        if not result or not result.get("items"):
            return []

        return [Diagnostic(**item) for item in result["items"]]

    async def get_completion(
        self,
        uri: str,
        position: Position,
        context: dict[str, Any] | None = None,
    ) -> list[CompletionItem]:
        """获取代码补全"""
        request = LSPRequest(
            id=self._next_id(),
            method="textDocument/completion",
            params={
                "textDocument": {"uri": uri},
                "position": position.dict(),
                "context": context,
            },
        )

        response = await self._send_request(request)
        if response.error:
            raise Exception(f"获取补全失败: {response.error}")

        result = response.result
        if not result:
            return []

        # CompletionList
        if isinstance(result, dict) and "items" in result:
            return [CompletionItem(**item) for item in result["items"]]

        # CompletionItem[]
        if isinstance(result, list):
            return [CompletionItem(**item) for item in result]

        # 单个 CompletionItem
        return [CompletionItem(**result)]

    async def open_document(self, uri: str, language_id: str, version: int, text: str):
        """打开文档"""
        await self._send_notification(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": language_id,
                    "version": version,
                    "text": text,
                }
            },
        )

    async def change_document(
        self,
        uri: str,
        version: int,
        changes: list[dict[str, Any]],
    ):
        """修改文档"""
        await self._send_notification(
            "textDocument/didChange",
            {
                "textDocument": {"uri": uri, "version": version},
                "contentChanges": changes,
            },
        )

    async def _send_request(self, request: LSPRequest) -> LSPResponse:
        """发送 LSP 请求"""
        if not self.process or not self.process.stdin:
            raise Exception("LSP 服务器未连接")

        # 序列化请求
        request_str = json.dumps(request.dict(), ensure_ascii=False)
        message = f"Content-Length: {len(request_str.encode('utf-8'))}\r\n\r\n{request_str}"

        # 发送请求
        self.process.stdin.write(message.encode("utf-8"))
        await self.process.stdin.drain()

        # 读取响应
        response_str = await self._read_message()
        response_data = json.loads(response_str)

        return LSPResponse(**response_data)

    async def _send_notification(self, method: str, params: dict[str, Any]):
        """发送 LSP 通知"""
        if not self.process or not self.process.stdin:
            raise Exception("LSP 服务器未连接")

        notification = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }

        notification_str = json.dumps(notification, ensure_ascii=False)
        message = f"Content-Length: {len(notification_str.encode('utf-8'))}\r\n\r\n{notification_str}"

        self.process.stdin.write(message.encode("utf-8"))
        await self.process.stdin.drain()

    async def _read_message(self) -> str:
        """读取 LSP 消息"""
        if not self.process or not self.process.stdout:
            raise Exception("LSP 服务器未连接")

        # 读取 headers
        headers = {}
        while True:
            line = await self.process.stdout.readline()
            if not line:
                raise Exception("连接已关闭")

            line = line.decode("utf-8").strip()
            if not line:
                break

            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip()] = value.strip()

        # 读取 body
        content_length = int(headers.get("Content-Length", 0))
        if content_length == 0:
            return ""

        body = await self.process.stdout.read(content_length)
        return body.decode("utf-8")

    def _next_id(self) -> int:
        """获取下一个请求 ID"""
        self.request_id += 1
        return self.request_id
