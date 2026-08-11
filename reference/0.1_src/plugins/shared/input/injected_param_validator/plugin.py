"""注入参数校验 Input 插件。

在管道首次迭代时校验所有已注册工具的 injected_params 声明，
检查每个声明参数是否有对应的注入来源。如果发现无法注入的参数，
记录警告日志但不终止管道（防御性校验）。

State 命名空间：
    - injected_param_check_done : 校验是否已完成
"""

from __future__ import annotations

import logging
from typing import Any

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import ErrorPolicy

logger = logging.getLogger(__name__)

_KNOWN_INJECT_SOURCES: dict[str, str] = {
    "session_id": "ParamInjectPlugin",
    "user_id": "ParamInjectPlugin",
    "timestamp": "ParamInjectPlugin",
    "task_id": "TaskWorker/ToolCore 上下文注入",
    "pipeline_id": "ParamInjectPlugin",
    "dependencies": "TaskWorker 上下文注入",
    "tool_record_id": "ToolCore 内部注入",
    "parent_agent_level": "TaskWorker 上下文注入",
    "_task_service": "ToolCore._SERVICE_INJECT_MAP",
    "_tool_registry": "ToolCore._SERVICE_INJECT_MAP",
    "_session": "ToolCore._SERVICE_INJECT_MAP",
    "_memory_service": "ToolCore._SERVICE_INJECT_MAP",
    "agent_config_id": "ParamInjectPlugin",
}


class InjectedParamValidator(IInputPlugin):
    """注入参数校验插件。

    在管道首次迭代时，遍历所有工具的 injected_params 声明，
    检查每个参数是否有已知的注入来源。无法注入的参数记录
    WARNING 日志，便于排查契约断裂问题。

    优先级：5（最早执行，只执行一次）
    错误策略：SKIP（校验失败不终止管道）

    Attributes:
        _config: 插件配置字典
    """

    error_policy = ErrorPolicy.SKIP

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化注入参数校验插件。

        Args:
            config: 插件配置字典，支持以下键：
                - enabled: 是否启用校验（默认 True）
        """
        self._config = config or {}
        self._enabled = self._config.get("enabled", True)

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "injected_param_validator"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return self._config.get("priority", 5)

    async def execute(self, ctx: PluginContext) -> PluginResult:
        """校验所有工具的 injected_params 是否有已知注入来源。

        仅在首次迭代时执行一次，后续迭代直接跳过。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含校验完成标记的插件执行结果
        """
        if not self._enabled:
            return PluginResult()

        if ctx.state.get("injected_param_check_done"):
            return PluginResult()

        tool_definitions = ctx.state.get("_tool_definitions", {})
        if not tool_definitions:
            return PluginResult()

        for tool_name, tool_def in tool_definitions.items():
            self._check_tool(tool_name, tool_def)

        return PluginResult(state_updates={"injected_param_check_done": True})

    def _check_tool(self, tool_name: str, tool_def: Any) -> None:
        """检查单个工具的 injected_params 声明。

        Args:
            tool_name: 工具名称
            tool_def: 工具定义（dict 或对象）
        """
        injected_params = self._get_injected_params(tool_def)
        if not injected_params:
            return

        visible_props = self._get_visible_props(tool_def)

        for param_name in injected_params:
            if param_name in _KNOWN_INJECT_SOURCES:
                continue
            if param_name in visible_props:
                continue
            logger.warning(
                "[%s] 工具 '%s' 的 injected_params 包含 '%s'，"
                "但无已知注入来源且不在 input_schema.properties 中。"
                "运行时该参数可能永远为空。",
                self.name,
                tool_name,
                param_name,
            )

    def _get_injected_params(self, tool_def: Any) -> list[str]:
        """从工具定义中提取 injected_params 列表。

        Args:
            tool_def: 工具定义（dict 或对象）

        Returns:
            injected_params 列表
        """
        if isinstance(tool_def, dict):
            return tool_def.get("injected_params", [])
        return getattr(tool_def, "injected_params", []) or []

    def _get_visible_props(self, tool_def: Any) -> set[str]:
        """从工具定义中提取 LLM 可见的属性名集合。

        Args:
            tool_def: 工具定义（dict 或对象）

        Returns:
            属性名集合
        """
        input_schema = None
        if isinstance(tool_def, dict):
            input_schema = tool_def.get("input_schema")
        else:
            input_schema = getattr(tool_def, "input_schema", None)

        if not isinstance(input_schema, dict):
            return set()

        return set(input_schema.get("properties", {}).keys())
