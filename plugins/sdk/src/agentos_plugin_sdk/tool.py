"""@tool 装饰器——快捷注册工具的模块级接口。

允许在不创建 AgentOSPlugin 实例的情况下声明工具，
随后由 AgentOSPlugin 自动收集。

[来源: docs/tasks/task_08_python_sdk.md AC-07-1/AC-07-4]
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agentos_plugin_sdk.types import ToolDef

# 属性名常量——用于在函数对象上标记 ToolDef
_AGENTOS_TOOL_ATTR = "_agentos_tool"


def tool(
    name: str,
    schema: dict[str, Any],
    description: str = "",
    output_schema: dict[str, Any] | None = None,
    render: dict[str, Any] | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """装饰器——声明一个 MCP 工具。

    可在模块级别使用，也可作为 AgentOSPlugin 实例方法使用。

    Usage (模块级):
        @tool(name="search", schema={"type": "object", ...})
        async def search(query: str) -> dict:
            return {"results": [...]}

    Usage (实例级，通过 AgentOSPlugin.tool):
        plugin = AgentOSPlugin("my_plugin")

        @plugin.tool(name="search", schema={...})
        async def search(query: str) -> dict:
            ...

    Args:
        name: 工具名称。
        schema: JSON Schema 描述输入参数。
        description: 工具描述。
        output_schema: 输出 JSON Schema（可选）。
        render: 渲染意图声明（可选，对齐 DSH ToolResultView 词汇表），如
            ``{"card": "terminal"}``。见 ToolDef.render。

    Returns:
        装饰器函数。
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        # 将 ToolDef 存储在函数对象上，供 AgentOSPlugin 自动收集
        # 使用 setattr 避免 mypy strict 的 attr-defined 错误
        setattr(
            func,
            _AGENTOS_TOOL_ATTR,
            ToolDef(
                name=name,
                schema=schema,
                handler=func,
                description=description,
                output_schema=output_schema,
                render=render,
            ),
        )
        return func

    return decorator


def collect_tools(obj: Any) -> dict[str, ToolDef]:
    """从对象中收集所有标记了 @tool 装饰器的方法。

    扫描对象的所有属性，找出带有 _agentos_tool 标记的函数。

    Args:
        obj: 要扫描的对象（模块、类实例等）。

    Returns:
        工具名称到 ToolDef 的映射。
    """
    tools: dict[str, ToolDef] = {}
    for attr_name in dir(obj):
        attr = getattr(obj, attr_name, None)
        if attr is None:
            continue
        td = getattr(attr, _AGENTOS_TOOL_ATTR, None)
        if isinstance(td, ToolDef):
            tools[td.name] = td
    return tools
