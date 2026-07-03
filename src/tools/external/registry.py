"""外部工具注册表。

暴露接口：
- ExternalToolRegistry：管理外部工具的注册、发现和健康检查
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from tools.external.adapter import ExternalToolAdapter
from tools.external.exceptions import ConfigError
from tools.external.interfaces import IExternalToolConnection
from tools.external.types import (
    ExternalToolInfo,
    ExternalToolState,
)
from tools.types import Tool

logger = logging.getLogger(__name__)


class ExternalToolRegistry:
    """外部工具注册表。

    职责：
    - 管理外部工具适配器的注册和注销
    - 将外部工具能力转换为内部 Tool 对象
    - 提供按能力查询、批量健康检查等功能
    - 与现有 ToolRegistry 无缝集成（通过 register_to_tool_registry）
    """

    def __init__(self) -> None:
        """初始化外部工具注册表。"""
        self._adapters: dict[str, ExternalToolAdapter] = {}
        self._connections: dict[str, IExternalToolConnection] = {}
        self._tool_map: dict[str, str] = {}  # 内部工具名 → 外部工具名
        self._lock = asyncio.Lock()
        self._logger = logging.getLogger(__name__)

    def register_external_tool(
        self,
        adapter: ExternalToolAdapter,
        connection: IExternalToolConnection | None = None,
    ) -> str:
        """注册外部工具。

        Args:
            adapter: 适配器实例
            connection: 连接管理器（可选）

        Returns:
            工具名称

        Raises:
            ConfigError: 工具已存在
        """
        name = adapter.name

        if name in self._adapters:
            raise ConfigError(
                message=f"外部工具已注册: {name}",
                tool_name=name,
            )

        self._adapters[name] = adapter

        if connection is not None:
            self._connections[name] = connection
            adapter.connection = connection

        # 构建内部工具名映射
        for tool in adapter.to_tool():
            self._tool_map[tool.name] = name

        self._logger.info(
            "外部工具已注册 | name=%s | capabilities=%d",
            name,
            len(adapter.get_capabilities()),
        )
        return name

    def unregister_external_tool(self, name: str) -> None:
        """注销外部工具。

        Args:
            name: 工具名称
        """
        adapter = self._adapters.pop(name, None)
        if adapter is None:
            self._logger.warning("外部工具不存在 | name=%s", name)
            return

        self._connections.pop(name, None)

        # 清理工具映射
        keys_to_remove = [k for k, v in self._tool_map.items() if v == name]
        for key in keys_to_remove:
            del self._tool_map[key]

        self._logger.info("外部工具已注销 | name=%s", name)

    def get_adapter(self, name: str) -> ExternalToolAdapter | None:
        """获取适配器。

        Args:
            name: 工具名称

        Returns:
            适配器实例，不存在返回 None
        """
        return self._adapters.get(name)

    def get_connection(self, name: str) -> IExternalToolConnection | None:
        """获取连接管理器。

        Args:
            name: 工具名称

        Returns:
            连接管理器，不存在返回 None
        """
        return self._connections.get(name)

    def get_external_tool_name(self, internal_tool_name: str) -> str | None:
        """根据内部工具名获取外部工具名。

        Args:
            internal_tool_name: 内部工具名（格式: external_name__operation）

        Returns:
            外部工具名
        """
        return self._tool_map.get(internal_tool_name)

    def list_external_tools(self) -> list[ExternalToolInfo]:
        """列出所有外部工具信息。"""
        infos: list[ExternalToolInfo] = []
        for name, adapter in self._adapters.items():
            connection = self._connections.get(name)
            state = connection.get_state() if connection else ExternalToolState.DISCONNECTED

            info = ExternalToolInfo(
                name=name,
                version=adapter.config.extra.get("version", "1.0.0"),
                display_name=adapter.config.display_name,
                description=adapter.config.description,
                capabilities=adapter.get_capabilities(),
                state=state,
                config=adapter.config,
            )
            infos.append(info)
        return infos

    def list_all_internal_tools(self) -> list[Tool]:
        """列出所有外部工具转换后的内部 Tool 对象。"""
        tools: list[Tool] = []
        for adapter in self._adapters.values():
            tools.extend(adapter.to_tool())
        return tools

    def get_tools_by_capability(self, capability_name: str) -> list[Tool]:
        """按能力名查询内部 Tool 对象。

        Args:
            capability_name: 能力名称

        Returns:
            匹配的 Tool 列表
        """
        tools: list[Tool] = []
        for adapter in self._adapters.values():
            for tool in adapter.to_tool():
                op = tool.metadata.get("operation", "")
                if op == capability_name:
                    tools.append(tool)
        return tools

    async def health_check_all(self) -> dict[str, bool]:
        """对所有已连接的工具执行健康检查。

        Returns:
            工具名 → 是否健康的字典
        """
        results: dict[str, bool] = {}
        for name, connection in self._connections.items():
            try:
                results[name] = await connection.health_check()
            except Exception:
                results[name] = False
        return results

    def discover_tools(self, capability: str | None = None) -> list[ExternalToolInfo]:
        """发现可用的外部工具。

        Args:
            capability: 可选的能力过滤

        Returns:
            工具信息列表
        """
        tools = self.list_external_tools()

        if capability:
            tools = [t for t in tools if any(c.name == capability for c in t.capabilities)]

        return tools

    async def register_to_tool_registry(
        self,
        tool_registry: Any,
    ) -> list[str]:
        """将所有外部工具注册到系统内部 ToolRegistry。

        Args:
            tool_registry: 系统内部 ToolRegistry 实例

        Returns:
            注册的工具名列表
        """
        registered: list[str] = []

        for adapter in self._adapters.values():
            for tool in adapter.to_tool():
                try:
                    # 创建执行 handler
                    handler = self._create_handler(adapter, tool)

                    tool_registry.register_with_handler(
                        tool=tool,
                        handler=handler,
                        overwrite=True,
                    )
                    registered.append(tool.name)

                    self._logger.info(
                        "内部工具已注册 | name=%s | source=external",
                        tool.name,
                    )
                except Exception as e:
                    self._logger.error(
                        "内部工具注册失败 | name=%s | error=%s",
                        tool.name,
                        e,
                    )

        return registered

    def _create_handler(
        self,
        adapter: ExternalToolAdapter,
        tool: Tool,
    ) -> Any:
        """为外部工具创建执行 handler。

        Args:
            adapter: 外部工具适配器
            tool: 内部 Tool 对象

        Returns:
            异步 handler 函数
        """
        operation = tool.metadata.get("operation", "")

        async def handler(inputs: dict[str, Any]) -> dict[str, Any]:
            return await adapter.execute(operation, inputs)

        return handler

    def count(self) -> int:
        """获取已注册的外部工具数量。"""
        return len(self._adapters)

    def clear(self) -> None:
        """清空注册表。"""
        self._adapters.clear()
        self._connections.clear()
        self._tool_map.clear()
