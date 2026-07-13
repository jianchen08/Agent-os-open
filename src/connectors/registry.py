"""
连接器注册表

管理所有已注册的连接器实例，支持按类型查询、按优先级排序和按能力匹配。

暴露接口：
- ConnectorRegistry: 连接器注册表
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from .types import ConnectorInfo

if TYPE_CHECKING:
    from connectors.base import BaseConnector

logger = logging.getLogger(__name__)


class ConnectorRegistry:
    """连接器注册表。

    管理所有已注册的连接器，支持：
    - 注册/注销连接器
    - 按类型查询连接器
    - 获取当前活跃连接器（按优先级）
    - 根据能力匹配最佳连接器

    使用方式:
        registry = ConnectorRegistry()
        registry.register(vscode_connector)
        active = registry.get_active_connector()
    """

    def __init__(self) -> None:
        """初始化连接器注册表。"""
        self._connectors: dict[str, BaseConnector] = {}
        self._lock = threading.RLock()
        logger.debug("ConnectorRegistry 初始化完成")

    def register(self, connector: BaseConnector) -> None:
        """注册连接器。

        Args:
            connector: 要注册的连接器实例
        """
        with self._lock:
            conn_type = connector.connector_type
            if conn_type in self._connectors:
                logger.warning(f"连接器 '{conn_type}' 已存在，将被覆盖")
            self._connectors[conn_type] = connector
            logger.info(f"已注册连接器: {conn_type}")

    def unregister(self, connector_type: str) -> None:
        """注销连接器。

        Args:
            connector_type: 要注销的连接器类型

        Raises:
            KeyError: 连接器不存在时
        """
        with self._lock:
            if connector_type not in self._connectors:
                raise KeyError(f"连接器 '{connector_type}' 不存在")
            del self._connectors[connector_type]
            logger.info(f"已注销连接器: {connector_type}")

    def get_connector(self, connector_type: str) -> BaseConnector | None:
        """获取指定类型的连接器。

        Args:
            connector_type: 连接器类型

        Returns:
            连接器实例，不存在时返回 None
        """
        return self._connectors.get(connector_type)

    def get_active_connector(self) -> BaseConnector | None:
        """获取当前活跃的连接器（按优先级排序）。

        优先级规则：
        1. 仅返回已连接（is_connected=True）的连接器
        2. 优先级数值越大越优先
        3. 同优先级时按类型名字母序

        Returns:
            最优先的活跃连接器，无活跃连接器时返回 None
        """
        connected = [conn for conn in self._connectors.values() if conn.is_connected]
        if not connected:
            return None

        # 按优先级降序排序，同优先级按类型名字母序
        connected.sort(key=lambda c: (-c.get_info().priority, c.connector_type))
        return connected[0]

    def list_connectors(self) -> list[ConnectorInfo]:
        """列出所有已注册连接器的信息。

        Returns:
            连接器信息列表，按优先级降序排列
        """
        infos = [conn.get_info() for conn in self._connectors.values()]
        infos.sort(key=lambda info: (-info.priority, info.connector_type))
        return infos

    def get_best_connector_for(self, action_type: str) -> BaseConnector | None:
        """根据操作类型获取最佳连接器。

        在所有已连接且支持该操作类型的连接器中，选择优先级最高的。

        Args:
            action_type: 操作类型（如 open_file, show_diff）

        Returns:
            最佳匹配的连接器，无匹配时返回 None
        """
        candidates: list[BaseConnector] = []
        for conn in self._connectors.values():
            if not conn.is_connected:
                continue
            info = conn.get_info()
            if action_type in info.capabilities:
                candidates.append(conn)

        if not candidates:
            return None

        candidates.sort(key=lambda c: (-c.get_info().priority, c.connector_type))
        return candidates[0]

    def has(self, connector_type: str) -> bool:
        """检查连接器是否已注册。

        Args:
            connector_type: 连接器类型

        Returns:
            True 表示已注册
        """
        return connector_type in self._connectors

    def count(self) -> int:
        """获取已注册的连接器数量。

        Returns:
            连接器数量
        """
        return len(self._connectors)

    def clear(self) -> None:
        """清空所有已注册的连接器。"""
        with self._lock:
            self._connectors.clear()
            logger.info("已清空所有连接器")
