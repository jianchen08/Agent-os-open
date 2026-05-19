"""{plugin_name} 核心插件 — {one_line_description}。

{detailed_description}

State 读写:
    读取: {read_state_keys}
    写入: {write_state_keys}

配置项:
    {config_docs}
"""

from __future__ import annotations

import logging
from typing import Any

from pipeline.plugin import ICorePlugin, PluginContext
from pipeline.types import ErrorPolicy

logger = logging.getLogger(__name__)


class {PluginClass}(ICorePlugin):
    """{one_line_description}。

    {detailed_description}

    Attributes:
        error_policy: 错误处理策略
        fallback_state: 错误策略为 FALLBACK 时的默认状态更新
    """

    error_policy: ErrorPolicy = ErrorPolicy.ABORT
    fallback_state: dict[str, Any] = {{}}

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化核心插件。

        Args:
            config: 插件配置字典，从 YAML 加载
        """
        self._config = config or {{}}
        # TODO: 从 self._config 读取配置项

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "{plugin_name}"

    @property
    def priority(self) -> int:
        """插件执行优先级，数值越小越先执行。"""
        return 0

    async def execute(self, ctx: PluginContext) -> dict[str, Any]:
        """执行核心插件逻辑。

        Args:
            ctx: 插件执行上下文

        Returns:
            核心执行结果字典，将合并到管道状态中
        """
        try:
            # TODO: 实现核心逻辑
            result: dict[str, Any] = {{}}

            logger.debug("Core plugin %s executed successfully", self.name)
            return result

        except Exception as e:
            logger.error("Core plugin %s execution failed: %s", self.name, e)
            raise
