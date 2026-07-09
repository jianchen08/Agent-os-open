"""Agent OS 插件集合。

提供 Core / Input / Output 插件的具体实现，以及插件热重载管理。
"""

from plugins.input import (
    CircuitBreaker,
    ContextBuildPlugin,
    CostControlPlugin,
    InjectedParamValidator,
    IsolationGuard,
    KnowledgeInjectPlugin,
    LevelGuardPlugin,
    MemoryReadPlugin,
    MultimodalPreprocessor,
    ParamInjectPlugin,
    PauseGuardPlugin,
    PromptBuildPlugin,
    ReasoningCheckPlugin,
    SecurityCheckPlugin,
    ToolCache,
    ToolCallGuard,
    ToolContextPlugin,
    ToolSchemaPlugin,
    ToolSchemaValidator,
)

# Lazy import for hot-reload to avoid circular imports at module level
# Use: from plugins.hot_reload import PluginHotReloader


def get_hot_reloader():
    """Get the PluginHotReloader class (lazy import).

    Returns:
        PluginHotReloader class.
    """
    from plugins.hot_reload import PluginHotReloader  # noqa: PLC0415

    return PluginHotReloader


__all__ = [
    # Input plugins
    "CircuitBreaker",
    "ContextBuildPlugin",
    "CostControlPlugin",
    "InjectedParamValidator",
    "IsolationGuard",
    "KnowledgeInjectPlugin",
    "LevelGuardPlugin",
    "MemoryReadPlugin",
    "MultimodalPreprocessor",
    "ParamInjectPlugin",
    "PauseGuardPlugin",
    "PromptBuildPlugin",
    "ReasoningCheckPlugin",
    "SecurityCheckPlugin",
    "ToolCallGuard",
    "ToolCache",
    "ToolContextPlugin",
    "ToolSchemaPlugin",
    "ToolSchemaValidator",
    # Hot-reload
    "get_hot_reloader",
]
