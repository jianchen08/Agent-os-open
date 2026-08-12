"""Trimmed 0.1 ``core.constants`` shim — exposes only ``ToolLimits``.

The full 0.1 module also defines ``Timeout`` / ``Retry`` / ``CostControl`` /
``Evaluation`` / ``QueryLimits`` / ``TaskPriority`` classes, but the only
class referenced by any of the four MCP sidecar tools is ``ToolLimits`` (used
by ``resource_search`` for ``RESOURCE_SEARCH_DEFAULT``).
"""


class ToolLimits:
    """工具限制常量（0.1 兼容子集）。"""

    MEMORY_SEARCH_DEFAULT = 10
    MEMORY_VIEW_DEFAULT = 20
    TASK_LIST_DEFAULT = 50
    RESOURCE_SEARCH_DEFAULT = 20
    WEB_SEARCH_MULTIPLIER = 2
    MAX_RECENT_TURNS_MULTIPLIER = 2
