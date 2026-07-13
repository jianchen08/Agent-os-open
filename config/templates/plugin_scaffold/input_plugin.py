"""{plugin_name} 插件 — {one_line_description}。

{detailed_description}

State 读写:
    读取: {read_state_keys}
    写入: {write_state_keys}

配置项:
    enabled (bool): 是否启用，默认 True
    {other_config_docs}
"""

from __future__ import annotations

import logging
from typing import Any

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import ErrorPolicy

logger = logging.getLogger(__name__)


class {PluginClass}(IInputPlugin):
    """{one_line_description}。

    {detailed_description}

    Attributes:
        error_policy: 错误处理策略
    """

    error_policy: ErrorPolicy = ErrorPolicy.ABORT

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化插件。

        Args:
            config: 插件配置字典，从 YAML 加载
        """
        self._config = config or {{}}
        self._enabled = self._config.get("enabled", True)
        # TODO: 从 self._config 读取其他配置项

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "{plugin_name}"

    @property
    def priority(self) -> int:
        """插件执行优先级，数值越小越先执行。"""
        return {priority_value}

    async def execute(self, ctx: PluginContext) -> PluginResult:
        """执行插件逻辑。

        Args:
            ctx: 插件执行上下文

        Returns:
            插件执行结果
        """
        if not self._enabled:
            return PluginResult()

        try:
            # TODO: 实现插件核心逻辑
            state_updates: dict[str, Any] = {{}}

            logger.debug("Plugin %s executed successfully", self.name)
            return PluginResult(state_updates=state_updates)

        except Exception as e:
            logger.error("Plugin %s execution failed: %s", self.name, e)
            return PluginResult(error=e)
