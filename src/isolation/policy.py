"""
隔离策略加载器

从 isolation_policy.yaml 加载策略配置，提供工具隔离决策。
决策优先级：tools（精确匹配）> categories（分类匹配）> default

暴露接口：
- ToolIsolationPolicy：单个工具的隔离策略数据类
- IsolationPolicyLoader：策略加载器，提供 resolve() 决策方法
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml  # noqa: F401

from isolation.types import IsolationLevel

logger = logging.getLogger(__name__)

DEFAULT_POLICY_PATH = Path(__file__).parent.parent.parent / "config" / "isolation" / "isolation_policy.yaml"


@dataclass
class ToolIsolationPolicy:
    """单个工具的隔离策略"""

    isolation: IsolationLevel = IsolationLevel.CONTAINER
    execution: str = "command_in_container"
    network: str | None = None
    checkpoint: bool = False
    approval: bool = False
    disk_quota: str | None = None


class IsolationPolicyLoader:
    """隔离策略加载器

    从 isolation_policy.yaml 加载策略，提供工具隔离决策。
    决策优先级：tools（精确匹配）> categories（分类匹配）> default
    """

    def __init__(self, config_path: str | None = None) -> None:
        """初始化策略加载器

        Args:
            config_path: 策略配置文件路径，为 None 时使用默认路径
        """
        self._config_path = Path(config_path) if config_path else DEFAULT_POLICY_PATH
        self._config: dict = {}
        self._default = ToolIsolationPolicy()
        self._tools: dict[str, ToolIsolationPolicy] = {}
        self._categories: dict[str, ToolIsolationPolicy] = {}

        self._load_config()

        # 注册 config_center watcher，实现热更新
        self._register_watcher()

    def _load_config(self) -> None:
        """从 config_center 加载策略配置。"""
        path = self._config_path
        try:
            from config.config_center import get_config_center  # noqa: PLC0415

            rel = str(path).replace("\\", "/")
            if "config/" in rel:
                rel = rel[rel.index("config/") + len("config/") :]
            self._config = get_config_center().get(rel) or {}
        except Exception:
            logger.warning(f"隔离策略配置加载失败: {path}，使用默认策略（容器隔离）")
            self._default = ToolIsolationPolicy()
            self._tools = {}
            self._categories = {}
            return

        self._default = self._parse_policy(self._config.get("default", {}))
        self._tools = {k: self._parse_policy(v) for k, v in self._config.get("tools", {}).items()}
        self._categories = {k: self._parse_policy(v) for k, v in self._config.get("categories", {}).items()}
        logger.info(f"隔离策略加载完成: {len(self._tools)} 个工具策略, {len(self._categories)} 个分类策略")

    def _register_watcher(self) -> None:
        """注册 config_center watcher，配置变更时自动 reload。"""
        try:
            from config.config_center import get_config_center  # noqa: PLC0415

            get_config_center().watch("isolation/", self._on_config_changed)
            logger.debug("[IsolationPolicyLoader] 已注册 config_center watcher")
        except Exception as e:
            logger.warning(f"[IsolationPolicyLoader] 注册 watcher 失败: {e}")

    def _on_config_changed(
        self,
        event_type: str,
        file_path: str,
        context: dict | None = None,
    ) -> None:
        """config_center 回调：检测到 isolation_policy.yaml 变更时自动 reload。

        Args:
            event_type: 事件类型（created/modified/deleted）
            file_path: 变更的配置文件路径
            context: 变更上下文（可选）
        """
        if "isolation_policy" in file_path:
            logger.info(
                "[IsolationPolicyLoader] 检测到策略配置变更(%s)，自动 reload: %s",
                event_type,
                file_path,
            )
            self._load_config()

    def resolve(self, tool_name: str, category: str | None = None) -> ToolIsolationPolicy:
        """决策工具的隔离策略

        优先级：工具名 > 分类 > 默认

        Args:
            tool_name: 工具名称
            category: 工具分类（可选）

        Returns:
            匹配到的隔离策略
        """
        if tool_name in self._tools:
            return self._tools[tool_name]
        if category and category in self._categories:
            return self._categories[category]
        return self._default

    def _parse_policy(self, data: dict) -> ToolIsolationPolicy:
        """解析单条策略配置

        Args:
            data: YAML 中单条策略的字典

        Returns:
            解析后的策略对象
        """
        if not data:
            return ToolIsolationPolicy()
        return ToolIsolationPolicy(
            isolation=IsolationLevel(data.get("isolation", "isolated")),
            execution=data.get("execution", "command_in_container"),
            network=data.get("network"),
            checkpoint=data.get("checkpoint", False),
            approval=data.get("approval", False),
            disk_quota=data.get("disk_quota"),
        )

    def get_tool_names(self) -> list[str]:
        """获取所有已配置的工具名"""
        return list(self._tools.keys())

    def get_category_names(self) -> list[str]:
        """获取所有已配置的分类名"""
        return list(self._categories.keys())

    def reload(self, config_path: str | None = None) -> None:
        """重新加载配置（热更新）

        Args:
            config_path: 可选的新配置文件路径
        """
        if config_path:
            self._config_path = Path(config_path)
        self._load_config()
