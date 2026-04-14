"""Input 插件集合。

提供管道输入阶段的各类插件实现，
负责在 Core 执行前对状态进行预处理。
"""

from plugins.input.context_build import ContextBuildPlugin
from plugins.input.knowledge_inject import KnowledgeInjectPlugin
from plugins.input.memory_read import MemoryReadPlugin
from plugins.input.param_inject import ParamInjectPlugin
from plugins.input.prompt_build import PromptBuildPlugin
from plugins.input.reasoning_check import ReasoningCheckPlugin
from plugins.input.security_check import SecurityCheckPlugin
from plugins.input.tool_schema import ToolSchemaPlugin

__all__ = [
    "ContextBuildPlugin",
    "KnowledgeInjectPlugin",
    "MemoryReadPlugin",
    "ParamInjectPlugin",
    "PromptBuildPlugin",
    "ReasoningCheckPlugin",
    "SecurityCheckPlugin",
    "ToolSchemaPlugin",
]
