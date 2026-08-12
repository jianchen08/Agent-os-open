"""资源搜索工具的限制常量。

来源：原 0.1 ``core/constants.py`` 中的 ``ToolLimits``。该常量类在 0.2 仅被
``resource_search`` 工具引用（``RESOURCE_SEARCH_DEFAULT``），属单插件消费，
按「插件自有的放插件目录」原则就近放在本工具目录，不再依赖 0.1 兼容 shim。

其余 0.1 常量（Timeout/Retry/CostControl/Evaluation/QueryLimits/TaskPriority）
未被本工具使用，未一并搬迁。
"""


class ToolLimits:
    """工具限制常量（resource_search 使用子集）。"""

    MEMORY_SEARCH_DEFAULT = 10
    MEMORY_VIEW_DEFAULT = 20
    TASK_LIST_DEFAULT = 50
    RESOURCE_SEARCH_DEFAULT = 20
    WEB_SEARCH_MULTIPLIER = 2
    MAX_RECENT_TURNS_MULTIPLIER = 2
