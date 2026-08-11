"""Core 插件集合。

提供核心执行能力插件：LLMCore（大模型调用）和 ToolCore（工具执行骨架）。
使用懒导入避免循环依赖。
"""


def __getattr__(name: str):
    if name == "LLMCore":
        from plugins.core.llm_core.plugin import LLMCore  # noqa: PLC0415

        return LLMCore
    if name == "ToolCore":
        from plugins.core.tool_core.plugin import ToolCore  # noqa: PLC0415

        return ToolCore
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["LLMCore", "ToolCore"]
