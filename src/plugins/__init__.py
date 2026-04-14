"""Agent OS 插件集合。

提供 Core / Input / Output 插件的具体实现。
"""

from plugins.input import (
    ContextBuildPlugin,
    KnowledgeInjectPlugin,
    ParamInjectPlugin,
    PromptBuildPlugin,
    ReasoningCheckPlugin,
    SecurityCheckPlugin,
    ToolSchemaPlugin,
)
from plugins.output import (
    DuplicateCheckPlugin,
    ErrorCheckPlugin,
    MemoryWritePlugin,
    PendingToolsOutput,
    ResultFormatPlugin,
    StopCheckPlugin,
    TaskEvaluationPlugin,
    TrackPlugin,
)

__all__ = [
    # Input plugins
    "ContextBuildPlugin",
    "KnowledgeInjectPlugin",
    "ParamInjectPlugin",
    "PromptBuildPlugin",
    "ReasoningCheckPlugin",
    "SecurityCheckPlugin",
    "ToolSchemaPlugin",
    # Output plugins
    "DuplicateCheckPlugin",
    "ErrorCheckPlugin",
    "MemoryWritePlugin",
    "PendingToolsOutput",
    "ResultFormatPlugin",
    "StopCheckPlugin",
    "TaskEvaluationPlugin",
    "TrackPlugin",
]
