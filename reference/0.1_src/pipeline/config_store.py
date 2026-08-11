"""管道配置存储。

提供 pipeline_id → PipelineConfig 的映射管理，
供跨管道路由查找目标管道配置使用。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class PipelineConfig:
    """管道配置数据类。

    描述一个管道的完整配置，供配置管理和回滚使用。

    Attributes:
        pipeline_id: 管道唯一标识
        name: 管道显示名称
        input_routes: 输入路由规则列表
        output_routes: 输出路由规则列表
        plugins: 插件配置列表
        core_plugins: 核心插件配置字典
        max_iterations: 最大迭代次数
    """

    pipeline_id: str
    name: str
    input_routes: list[dict[str, Any]] = field(default_factory=list)
    output_routes: list[dict[str, Any]] = field(default_factory=list)
    plugins: list[dict[str, Any]] = field(default_factory=list)
    core_plugins: dict[str, dict[str, Any]] = field(default_factory=dict)
    max_iterations: int = 500


class PipelineConfigStore:
    """管道配置存储。

    管理 pipeline_id → PipelineConfig 的映射，
    支持 register / get / list / remove 操作。

    Usage::

        store = PipelineConfigStore()
        store.register("research_agent", PipelineConfig(
            pipeline_id="research_agent", name="Research Agent"
        ))
        config = store.get("research_agent")
    """

    def __init__(self) -> None:
        self._configs: dict[str, PipelineConfig] = {}

    def register(self, pipeline_id: str, config: PipelineConfig) -> None:
        """注册管道配置。

        Args:
            pipeline_id: 管道唯一标识
            config: 管道配置实例
        """
        self._configs[pipeline_id] = config
        logger.info("Pipeline config registered: %s", pipeline_id)

    def get(self, pipeline_id: str) -> PipelineConfig | None:
        """获取管道配置。

        Args:
            pipeline_id: 管道唯一标识

        Returns:
            管道配置实例，不存在时返回 None
        """
        return self._configs.get(pipeline_id)

    def list_configs(self) -> list[str]:
        """列出所有已注册的配置 ID。

        Returns:
            配置 ID 列表
        """
        return list(self._configs.keys())

    def remove(self, pipeline_id: str) -> bool:
        """移除管道配置。

        Args:
            pipeline_id: 管道唯一标识

        Returns:
            是否成功移除
        """
        if pipeline_id in self._configs:
            del self._configs[pipeline_id]
            logger.info("Pipeline config removed: %s", pipeline_id)
            return True
        logger.warning("Pipeline config not found for removal: %s", pipeline_id)
        return False
