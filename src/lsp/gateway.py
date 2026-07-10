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
import threading
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, TypeVar

from src.lsp.client import LSPClient
from src.lsp.detector import IDEDetector, IDEInfo
from src.lsp.types import (
    CompletionItem,
    Diagnostic,
    Location,
    LSPServerInfo,
    Position,
)

T = TypeVar("T")

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

    管理多个语言的 LSP 客户端，提供统一的接口。

    Lifetime 设计：
    - LSPClient 持有绑定到某个事件循环的子进程 transport（stdin/stdout
      StreamWriter/StreamReader）。这些对象**不能跨事件循环复用**。
    - 工具执行框架（tool_core）会为每次异步工具调用新建并关闭一个事件循环，
      每个 task 管道也各自 asyncio.run 一个新循环。若 client 被缓存在这类
      "短命循环" 上，下一次调用命中的是已关闭循环的死 transport，在 Windows
      ProactorEventLoop 上表现为
      ``AttributeError: 'NoneType' object has no attribute 'send'``
      （``self._loop._proactor`` 已随循环关闭被置 None）。
    - 解法：网关自管一个**常驻专用事件循环**（daemon 线程 run_forever，永不
      关闭）。所有 loop-bound 资源（asyncio.Lock、LSPClient 子进程）都存活在
      该专用循环上；对外 async 方法用 ``run_coroutine_threadsafe`` + wrap_future
      把协程 marshal 进专用循环执行。调用方循环的生死不再影响 LSP 缓存。
    """

    def __init__(self):
        """初始化 LSP 网关"""
        self.clients: dict[str, LSPClient] = {}
        self.ide_info: IDEInfo | None = None
        self._failed_attempts: dict[str, int] = {}
        # 专用常驻事件循环（run_forever），承载所有 loop-bound 资源。
        self._loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
        self._loop_thread: threading.Thread | None = None
        # 绑定在专用循环上的锁；延迟到首次在专用循环内使用时创建
        # （见 _ensure_lock）。__init__ 不能用 run_until_complete 创建 lock：
        # get_lsp_gateway 是 async 函数，调用时已有 loop 运行，
        # Python 3.12 的 run_until_complete 会因 _check_running 检测到当前
        # 线程运行的 loop 而抛 "Cannot run the event loop while another loop is running"。
        self._lock: asyncio.Lock | None = None

    async def _ensure_lock(self) -> asyncio.Lock:
        """在专用循环内惰性创建并绑定锁。

        asyncio.Lock 构造时延迟绑定到首个使用它的运行循环；本方法总在专用
        循环内（经 _run_in_loop 调度）执行，从而把 lock 正确绑定到专用循环。
        """
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def start(self) -> None:
        """在 daemon 线程里启动专用事件循环（幂等）。"""
        if self._loop_thread is not None and self._loop_thread.is_alive():
            return
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever,
            name="lsp-gateway-loop",
            daemon=True,
        )
        self._loop_thread.start()

    def _run_in_loop(self, coro: Coroutine[Any, Any, T]) -> "asyncio.Future[T]":
        """把协程 marshal 到专用常驻循环执行，返回调用方可 await 的 Future。

        返回值是 ``asyncio.wrap_future(concurrent.futures.Future)``，可在任意
        事件循环里 await，从而把调用方循环与专用循环解耦。
        """
        return asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, self._loop))

    async def initialize(self):
        """初始化 LSP 网关（启动专用循环 + 检测 IDE，不预启动服务器）"""
        self.start()
        self.ide_info = IDEDetector.detect()
        if self.ide_info:
            logger.info(f"检测到 IDE: {self.ide_info.name} ({self.ide_info.type})")

    async def shutdown(self):
        """关闭 LSP 网关（在专用循环上停止所有客户端后清空缓存）"""
        await self._run_in_loop(self._shutdown_locked())

    async def _shutdown_locked(self):
        """在专用循环内加锁停止所有客户端。"""
        async with await self._ensure_lock():
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
        async with await self._ensure_lock():
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
        return await self._run_in_loop(self._go_to_definition(file_path, language, position))

    async def _go_to_definition(
        self,
        file_path: str,
        language: str,
        position: Position,
    ) -> list[Location]:
        """跳转到定义（在专用循环内执行）"""
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
        return await self._run_in_loop(self._find_references(file_path, language, position))

    async def _find_references(
        self,
        file_path: str,
        language: str,
        position: Position,
    ) -> list[Location]:
        """查找引用（在专用循环内执行）"""
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
        return await self._run_in_loop(self._get_diagnostics(file_path, language))

    async def _get_diagnostics(
        self,
        file_path: str,
        language: str,
    ) -> list[Diagnostic]:
        """获取诊断信息（在专用循环内执行）"""
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
        return await self._run_in_loop(self._get_completion(file_path, language, position))

    async def _get_completion(
        self,
        file_path: str,
        language: str,
        position: Position,
    ) -> list[CompletionItem]:
        """获取代码补全（在专用循环内执行）"""
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
    global _lsp_gateway  # noqa: PLW0603

    if _lsp_gateway is None:
        _lsp_gateway = LSPGateway()
        await _lsp_gateway.initialize()

    return _lsp_gateway
