"""Agent 配置注册表。

提供 Agent 配置的注册、查找和筛选功能。
支持按 config_id、层级、类型、分类、标签和工具进行查询。

典型用法::

    from agents.registry import AgentRegistry
    from agents.loader import AgentConfigLoader

    registry = AgentRegistry()
    count = registry.load_directory("config/agents/")

    # 按 ID 查找
    config = registry.get("main_agent")

    # 按层级筛选
    l1_agents = registry.find_by_level(AgentLevel.L1_MAIN)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .loader import AgentConfigLoader
from .types import AgentConfig, AgentLevel, AgentType

logger = logging.getLogger(__name__)


class AgentRegistry:
    """Agent 配置注册表，管理所有已加载的 AgentConfig 实例。

    Attributes:
        _configs: 以 config_id 为键的配置字典。
    """

    def __init__(self) -> None:
        """初始化空的 Agent 注册表。"""
        self._configs: dict[str, AgentConfig] = {}

    def register(self, config: AgentConfig) -> None:
        """注册 Agent 配置。

        如果 config_id 已存在，将覆盖旧配置并记录警告。

        Args:
            config: AgentConfig 实例。

        Raises:
            ValueError: config_id 为空。
        """
        if not config.config_id:
            raise ValueError("AgentConfig.config_id 不能为空")
        if config.config_id in self._configs:
            logger.warning("覆盖已存在的 Agent 配置: %s", config.config_id)
        self._configs[config.config_id] = config

    def get(self, config_id: str) -> AgentConfig | None:
        """按 config_id 查找 Agent 配置。

        Args:
            config_id: 配置唯一标识。

        Returns:
            AgentConfig 实例，未找到返回 None。
        """
        return self._configs.get(config_id)

    def find_by_level(self, level: AgentLevel) -> list[AgentConfig]:
        """按层级筛选 Agent 配置。

        Args:
            level: Agent 层级。

        Returns:
            匹配的 AgentConfig 列表。
        """
        return [c for c in self._configs.values() if c.level == level]

    def find_by_type(self, agent_type: AgentType) -> list[AgentConfig]:
        """按类型筛选 Agent 配置。

        Args:
            agent_type: Agent 类型。

        Returns:
            匹配的 AgentConfig 列表。
        """
        return [c for c in self._configs.values() if c.agent_type == agent_type]

    def find_by_category(self, category: str) -> list[AgentConfig]:
        """按分类筛选 Agent 配置。

        Args:
            category: Agent 分类名称。

        Returns:
            匹配的 AgentConfig 列表。
        """
        return [c for c in self._configs.values() if c.category == category]

    def find_by_tag(self, tag: str) -> list[AgentConfig]:
        """按标签筛选 Agent 配置。

        Args:
            tag: 标签名称。

        Returns:
            包含该标签的 AgentConfig 列表。
        """
        return [c for c in self._configs.values() if tag in c.tags]

    def find_by_tool(self, tool_id: str) -> list[AgentConfig]:
        """按工具筛选 Agent 配置。

        Args:
            tool_id: 工具 ID。

        Returns:
            绑定了该工具的 AgentConfig 列表。
        """
        return [c for c in self._configs.values() if tool_id in c.tool_ids]

    def list_all(self) -> list[AgentConfig]:
        """列出所有已注册的 Agent 配置。

        Returns:
            所有 AgentConfig 列表。
        """
        return list(self._configs.values())

    def load_directory(self, dir_path: str | Path) -> int:
        """从目录批量加载 Agent 配置并注册。

        Args:
            dir_path: YAML 配置目录路径。

        Returns:
            成功加载的配置数量。
        """
        configs = AgentConfigLoader.load_from_directory(dir_path)
        for config in configs:
            try:
                self.register(config)
            except ValueError as e:
                logger.warning("跳过无效配置 %s: %s", config.config_id, e)
        return len(configs)

    def unregister(self, config_id: str) -> bool:
        """注销 Agent 配置。

        Args:
            config_id: 配置唯一标识。

        Returns:
            是否成功注销（True=已移除，False=不存在）。
        """
        if config_id in self._configs:
            del self._configs[config_id]
            return True
        return False

    def count(self) -> int:
        """返回已注册的 Agent 配置数量。

        Returns:
            配置数量。
        """
        return len(self._configs)
