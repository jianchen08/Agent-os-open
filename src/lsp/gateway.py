"""
LSP 网关服务

暴露接口：
- get_client(self, language: str) -> LSPClient | None：get_client功能
- get_supported_languages(self) -> list[str]：get_supported_languages功能
- get_ide_info(self) -> IDEInfo | None：get_ide_info功能
- LSPGateway：LSPGateway类
"""

import asyncio
import logging
from pathlib import Path

from src.lsp.client import LSPClient
from src.lsp.detector import IDEDetector, IDEInfo
from src.lsp.types import (
    CompletionItem,
    Diagnostic,
    Location,
    LSPServerInfo,
    Position,
)

logger = logging.getLogger(__name__)


# 常见语言的 LSP 服务器配置
LSP_SERVERS = {
    "python": LSPServerInfo(
        name="pylsp",
        language="python",
        command="pylsp",
        args=[],
    ),
    "javascript": LSPServerInfo(
        name="typescript-language-server",
        language="javascript",
        command="typescript-language-server",
        args=["--stdio"],
    ),
    "typescript": LSPServerInfo(
        name="typescript-language-server",
        language="typescript",
        command="typescript-language-server",
        args=["--stdio"],
    ),
    "go": LSPServerInfo(
        name="gopls",
        language="go",
        command="gopls",
        args=["serve"],
    ),
    "rust": LSPServerInfo(
        name="rust-analyzer",
        language="rust",
        command="rust-analyzer",
        args=[],
    ),
}


class LSPGateway:
    """
    LSP 网关

    管理多个语言的 LSP 客户端，提供统一的接口
    """

    def __init__(self):
        """初始化 LSP 网关"""
        self.clients: dict[str, LSPClient] = {}
        self.ide_info: IDEInfo | None = None
        self._lock = asyncio.Lock()

    async def initialize(self):
        """初始化 LSP 网关"""
        # 检测 IDE
        self.ide_info = IDEDetector.detect()
        if self.ide_info:
            logger.info(f"检测到 IDE: {self.ide_info.name} ({self.ide_info.type})")

        # 启动常用语言的 LSP 服务器
        for language, server_info in LSP_SERVERS.items():
            try:
                client = LSPClient(server_info)
                success = await client.start()
                if success:
                    self.clients[language] = client
                    logger.info(f"LSP 服务器已启动: {language}")
            except Exception as e:
                logger.warning(f"启动 {language} LSP 服务器失败: {e}")

    async def shutdown(self):
        """关闭 LSP 网关"""
        async with self._lock:
            for client in self.clients.values():
                try:
                    await client.stop()
                except Exception as e:
                    logger.error(f"停止 LSP 客户端失败: {e}")
            self.clients.clear()

    def get_client(self, language: str) -> LSPClient | None:
        """获取指定语言的 LSP 客户端"""
        return self.clients.get(language)

    async def go_to_definition(
        self,
        file_path: str,
        position: Position,
        language: str | None = None,
    ) -> list[Location]:
        """跳转到定义"""
        if not language:
            language = self._detect_language(file_path)

        client = self.get_client(language)
        if not client:
            logger.warning(f"未找到 {language} 的 LSP 客户端")
            return []

        uri = Path(file_path).as_uri()
        return await client.go_to_definition(uri, position)

    async def find_references(
        self,
        file_path: str,
        position: Position,
        language: str | None = None,
    ) -> list[Location]:
        """查找引用"""
        if not language:
            language = self._detect_language(file_path)

        client = self.get_client(language)
        if not client:
            logger.warning(f"未找到 {language} 的 LSP 客户端")
            return []

        uri = Path(file_path).as_uri()
        return await client.find_references(uri, position)

    async def get_diagnostics(
        self,
        file_path: str,
        language: str | None = None,
    ) -> list[Diagnostic]:
        """获取诊断信息"""
        if not language:
            language = self._detect_language(file_path)

        client = self.get_client(language)
        if not client:
            logger.warning(f"未找到 {language} 的 LSP 客户端")
            return []

        uri = Path(file_path).as_uri()
        return await client.get_diagnostics(uri)

    async def get_completion(
        self,
        file_path: str,
        position: Position,
        language: str | None = None,
    ) -> list[CompletionItem]:
        """获取代码补全"""
        if not language:
            language = self._detect_language(file_path)

        client = self.get_client(language)
        if not client:
            logger.warning(f"未找到 {language} 的 LSP 客户端")
            return []

        uri = Path(file_path).as_uri()
        return await client.get_completion(uri, position)

    def _detect_language(self, file_path: str) -> str:
        """根据文件扩展名检测语言"""
        ext = Path(file_path).suffix.lower()

        language_map = {
            ".py": "python",
            ".js": "javascript",
            ".ts": "typescript",
            ".jsx": "javascript",
            ".tsx": "typescript",
            ".go": "go",
            ".rs": "rust",
        }

        return language_map.get(ext, "python")

    def get_supported_languages(self) -> list[str]:
        """获取支持的语言列表"""
        return list(self.clients.keys())

    def get_ide_info(self) -> IDEInfo | None:
        """获取 IDE 信息"""
        return self.ide_info


# 全局 LSP 网关实例
_lsp_gateway: LSPGateway | None = None


async def get_lsp_gateway() -> LSPGateway:
    """获取全局 LSP 网关实例"""
    global _lsp_gateway

    if _lsp_gateway is None:
        _lsp_gateway = LSPGateway()
        await _lsp_gateway.initialize()

    return _lsp_gateway
