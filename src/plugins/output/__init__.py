"""Output 插件集合。

提供管道输出阶段的各类插件实现，
负责在 Core 执行后对结果进行后处理和路由决策。
"""

from plugins.output.approval_view_route.plugin import ApprovalViewRoutePlugin

__all__ = [
    "ApprovalViewRoutePlugin",
]
