"""内置工具基类——插件共享层。

定义所有内置工具的抽象基类 ``BuiltinTool``，统一 ``get_tool_definition`` /
``execute`` 契约，并提供向 MCP / LLM 格式转换的默认实现。

来源：原 0.1 ``tools/builtin/base.py``。被 task / triggers_ext / task_submit
三个工具继承（``search`` 不继承），属于跨插件共享类型，故上提到 SDK 公共
依赖层，不再依赖 0.1 兼容 shim。

公共 API:
    BuiltinTool: 内置工具抽象基类
    register_builtin_tool: 注册内置工具到注册表
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from agentos_plugin_sdk.results import ToolExecutionResult
from agentos_plugin_sdk.tool_types import Tool

__all__ = ["BuiltinTool", "register_builtin_tool"]


class BuiltinTool(ABC):
    """内置工具基类。

    所有内置工具应继承此类，实现：
    - get_tool_definition(): 返回工具定义
    - execute(): 执行工具

    类属性:
        run_on_main_loop: 是否在主事件循环直接执行（默认 False=to_thread）。
            True 适用于纯异步工具（如 bash_execute），避免每次调用创建
            独立事件循环导致的跨循环问题。
    """

    # 纯异步工具设为 True，表示不需要 to_thread 隔离执行
    run_on_main_loop: bool = False

    @staticmethod
    @abstractmethod
    def get_tool_definition() -> Tool:
        """获取工具定义"""

    @abstractmethod
    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """执行工具"""

    def to_runnable(self) -> Any:
        """转换为 ToolRunnable。"""
        tool = self.get_tool_definition()
        return tool.to_runnable(self.execute)

    def to_mcp_format(self) -> dict[str, Any]:
        """转换为 MCP 格式"""
        tool = self.get_tool_definition()
        return tool.to_mcp_format()

    def to_llm_format(self) -> dict[str, Any]:
        """转换为 LLM 格式"""
        tool = self.get_tool_definition()
        return tool.to_llm_format()

    def get_schema_enricher(self) -> Callable | None:
        """获取工具的 Schema 动态丰富器。

        子类可重写此方法，返回一个函数：
        (tool: Tool, services: dict) -> Tool

        该函数在 ToolSchemaPlugin 每轮迭代时被调用，
        接收原始 Tool 定义和 services 字典，
        返回丰富后的 Tool 副本（深拷贝）。

        Returns:
            丰富器函数，默认返回 None（无丰富）
        """
        return None


def register_builtin_tool(
    tool_instance: BuiltinTool,
    registry: Any,
) -> str:
    """注册内置工具到注册表"""
    tool = tool_instance.get_tool_definition()
    name = registry.register_with_handler(
        tool=tool,
        handler=tool_instance.execute,
    )
    # 注册 Schema 丰富器（如果有）
    enricher = tool_instance.get_schema_enricher()
    if enricher:
        registry.register_schema_enricher(tool.name, enricher)
    return name
