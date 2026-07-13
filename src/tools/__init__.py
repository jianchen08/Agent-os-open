"""工具注册表模块。

提供简化版工具注册与查询能力，供 ToolCore 和 LLM 工具调用使用。
使用懒导入避免循环依赖。
"""


def __getattr__(name: str):
    if name == "ToolRegistry":
        from tools.registry import ToolRegistry  # noqa: PLC0415

        return ToolRegistry
    if name == "ToolDefinition":
        from tools.types import ToolDefinition  # noqa: PLC0415

        return ToolDefinition
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["ToolRegistry", "ToolDefinition"]
