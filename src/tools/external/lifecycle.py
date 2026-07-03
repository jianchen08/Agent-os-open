"""外部工具生命周期管理。

暴露接口：
- ExternalToolLifecycle：管理外部工具的完整生命周期
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from tools.external.adapter import ExternalToolAdapter
from tools.external.config import ExternalToolConfigManager
from tools.external.connection import ExternalToolConnection
from tools.external.registry import ExternalToolRegistry
from tools.external.sandbox import ExternalToolSandbox
from tools.external.secrets import ExternalToolSecretManager

logger = logging.getLogger(__name__)


class ExternalToolLifecycle:
    """外部工具生命周期管理器。

    职责：
    - 管理外部工具的启动/停止/重载
    - 状态监控和告警
    - 优雅关闭（等待执行完成）
    - 协调配置管理器、注册表、连接管理器等组件
    """

    def __init__(
        self,
        config_manager: ExternalToolConfigManager,
        secret_manager: ExternalToolSecretManager,
        registry: ExternalToolRegistry | None = None,
        sandbox: ExternalToolSandbox | None = None,
    ) -> None:
        """初始化生命周期管理器。

        Args:
            config_manager: 配置管理器
            secret_manager: 密钥管理器
            registry: 外部工具注册表（可选，默认创建新实例）
            sandbox: 沙箱管理器（可选，默认创建新实例）
        """
        self._config_manager = config_manager
        self._secret_manager = secret_manager
        self._registry = registry or ExternalToolRegistry()
        self._sandbox = sandbox or ExternalToolSandbox()
        self._adapter_factory: dict[str, type[ExternalToolAdapter]] = {}
        self._running = False
        self._monitor_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._logger = logging.getLogger(__name__)

    @property
    def registry(self) -> ExternalToolRegistry:
        """获取外部工具注册表。"""
        return self._registry

    @property
    def sandbox(self) -> ExternalToolSandbox:
        """获取沙箱管理器。"""
        return self._sandbox

    @property
    def is_running(self) -> bool:
        """是否正在运行。"""
        return self._running

    def register_adapter_type(
        self,
        tool_type: str,
        adapter_cls: type[ExternalToolAdapter],
    ) -> None:
        """注册适配器类型工厂。

        Args:
            tool_type: 工具类型标识
            adapter_cls: 适配器类
        """
        self._adapter_factory[tool_type] = adapter_cls
        self._logger.info(
            "适配器类型已注册 | type=%s | class=%s",
            tool_type,
            adapter_cls.__name__,
        )

    async def start(self) -> None:
        """启动所有外部工具连接。"""
        async with self._lock:
            if self._running:
                self._logger.warning("生命周期管理器已在运行")
                return

            self._logger.info("启动外部工具生命周期管理器")

            # 1. 加载配置
            configs = self._config_manager.load_all()
            if not configs:
                self._logger.info("未找到外部工具配置")
                self._running = True
                return

            # 2. 为每个配置创建适配器和连接
            for name, config in configs.items():
                try:
                    await self._start_tool(config)
                except Exception as e:
                    self._logger.error(
                        "工具启动失败 | name=%s | error=%s",
                        name,
                        e,
                    )

            # 3. 启动监控
            self._monitor_task = asyncio.create_task(self._monitor_loop())
            self._running = True
            self._logger.info(
                "外部工具生命周期管理器已启动 | tools=%d",
                self._registry.count(),
            )

    async def stop(self) -> None:
        """优雅停止所有外部工具连接。"""
        async with self._lock:
            if not self._running:
                return

            self._logger.info("停止外部工具生命周期管理器")

            # 1. 停止监控
            if self._monitor_task and not self._monitor_task.done():
                self._monitor_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._monitor_task
                self._monitor_task = None

            # 2. 断开所有连接
            for info in self._registry.list_external_tools():
                connection = self._registry.get_connection(info.name)
                if connection:
                    try:
                        await connection.disconnect()
                    except Exception as e:
                        self._logger.error(
                            "断开连接失败 | name=%s | error=%s",
                            info.name,
                            e,
                        )

            # 3. 清理所有沙箱
            await self._sandbox.destroy_all()

            self._running = False
            self._logger.info("外部工具生命周期管理器已停止")

    async def reload(self) -> None:
        """重新加载配置并重启工具。"""
        self._logger.info("重新加载外部工具")
        await self.stop()
        await self.start()

    async def start_tool(self, tool_name: str) -> bool:
        """启动单个外部工具。

        Args:
            tool_name: 工具名称

        Returns:
            是否启动成功
        """
        config = self._config_manager.get_config(tool_name)
        if config is None:
            self._logger.error("配置不存在 | tool=%s", tool_name)
            return False

        try:
            await self._start_tool(config)
            return True
        except Exception as e:
            self._logger.error(
                "工具启动失败 | name=%s | error=%s",
                tool_name,
                e,
            )
            return False

    async def stop_tool(self, tool_name: str) -> bool:
        """停止单个外部工具。

        Args:
            tool_name: 工具名称

        Returns:
            是否停止成功
        """
        connection = self._registry.get_connection(tool_name)
        if connection:
            try:
                await connection.disconnect()
            except Exception as e:
                self._logger.error(
                    "停止工具失败 | name=%s | error=%s",
                    tool_name,
                    e,
                )
                return False

        self._registry.unregister_external_tool(tool_name)
        return True

    def get_status(self) -> dict[str, Any]:
        """获取所有外部工具的状态。"""
        tools = self._registry.list_external_tools()
        return {
            "running": self._running,
            "tool_count": len(tools),
            "tools": {
                t.name: {
                    "state": t.state.value,
                    "capabilities": len(t.capabilities),
                    "version": t.version,
                }
                for t in tools
            },
        }

    async def _start_tool(self, config: Any) -> None:
        """启动单个工具（内部方法）。

        Args:
            config: 工具配置
        """
        name = config.name
        tool_type = config.extra.get("type", "generic")

        # 1. 创建适配器
        adapter_cls = self._adapter_factory.get(tool_type)
        if adapter_cls is None:
            self._logger.warning(
                "未注册适配器类型 | type=%s | tool=%s，跳过",
                tool_type,
                name,
            )
            return

        adapter = adapter_cls(config)

        # 2. 创建连接
        connection = ExternalToolConnection(config)

        # 3. 注册到外部工具注册表
        self._registry.register_external_tool(adapter, connection)

        # 4. 连接
        try:
            await connection.connect()
            self._logger.info(
                "工具已启动 | name=%s | state=%s",
                name,
                connection.get_state().value,
            )
        except Exception as e:
            self._logger.error(
                "工具连接失败 | name=%s | error=%s",
                name,
                e,
            )
            # 注册失败不阻塞其他工具

    async def _monitor_loop(self) -> None:
        """状态监控循环。"""
        try:
            while self._running:
                await asyncio.sleep(30)

                # 批量健康检查
                health = await self._registry.health_check_all()
                unhealthy = [name for name, ok in health.items() if not ok]

                if unhealthy:
                    self._logger.warning(
                        "以下外部工具不健康: %s",
                        unhealthy,
                    )

        except asyncio.CancelledError:
            pass
        except Exception as e:
            self._logger.error("监控循环异常: %s", e, exc_info=True)
