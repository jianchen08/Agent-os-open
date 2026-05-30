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

# LSP 服务器安装提示
INSTALL_HINTS: dict[str, str] = {
    "python": "pip install python-lsp-server",
    "javascript": "npm install -g typescript-language-server typescript",
    "typescript": "npm install -g typescript-language-server typescript",
    "go": "go install golang.org/x/tools/gopls@latest",
    "rust": "rustup component add rust-analyzer 或从 https://github.com/rust-lang/rust-analyzer/releases 下载",
}


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
        self._failed_attempts: dict[str, int] = {}

    async def initialize(self):
        """初始化 LSP 网关（仅检测 IDE，不预启动服务器）"""
        self.ide_info = IDEDetector.detect()
        if self.ide_info:
            logger.info(f"检测到 IDE: {self.ide_info.name} ({self.ide_info.type})")

    async def shutdown(self):
        """关闭 LSP 网关"""
        async with self._lock:
            for client in self.clients.values():
                try:
                    await client.stop()
                except Exception as e:
                    logger.error(f"停止 LSP 客户端失败: {e}")
            self.clients.clear()

    async def ensure_client(self, language: str) -> LSPClient | None:
        """带锁的懒启动，包含重试逻辑"""
        # 已有客户端且已初始化，直接返回
        if language in self.clients and self.clients[language].initialized:
            return self.clients[language]

        # 失败次数超过2次，不再重试
        if self._failed_attempts.get(language, 0) >= 2:
            logger.warning(f"LSP {language} 服务器已失败超过2次，跳过启动")
            return None

        # 带锁启动
        async with self._lock:
            # double-check
            if language in self.clients and self.clients[language].initialized:
                return self.clients[language]

            client = await self._start_client(language)
            if client:
                return client

            self._failed_attempts[language] = self._failed_attempts.get(language, 0) + 1
            return None

    async def _start_client(self, language: str) -> LSPClient | None:
        """启动单个 LSP 客户端"""
        server_info = LSP_SERVERS.get(language)
        if not server_info:
            logger.warning(f"不支持的语言: {language}")
            return None

        try:
            client = LSPClient(server_info)
            success = await client.start()
            if success:
                self.clients[language] = client
                logger.info(f"LSP 服务器已启动: {language}")
                return client
        except Exception as e:
            logger.warning(f"启动 {language} LSP 服务器失败: {e}")

        return None

    def get_install_hint(self, language: str) -> str:
        """获取安装提示"""
        return INSTALL_HINTS.get(language, f"请安装 {language} 语言的 LSP 服务器")

    def get_client(self, language: str) -> LSPClient | None:
        """获取指定语言的 LSP 客户端（同步方法，仅返回已启动的客户端）"""
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

        client = await self.ensure_client(language)
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

        client = await self.ensure_client(language)
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

        client = await self.ensure_client(language)
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

        client = await self.ensure_client(language)
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
        return list(LSP_SERVERS.keys())

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
