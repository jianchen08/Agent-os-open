"""委派深度守卫 Output 插件（已退役为纯透传）。

delegate 路由信号已从引擎协议移除（Rust `RouteType` 仅 next_llm/next_tool/
end/wait 四种；跨管道路由统一经任务系统/复盘系统等专门服务的工具调用显式
发起），深度检查随之失效。本插件仅保留深度字段的首次初始化，不再声明
路由信号（`route_signals` 返回空）。

State 命名空间：
    - delegate_depth : 当前委派深度（仅初始化，无消费方）
    - max_delegate_depth : 最大允许深度（仅初始化，无消费方）
"""

from __future__ import annotations

from typing import Any

from pipeline.plugin import IOutputPlugin, OutputResult, PluginContext


class DelegateDepthGuardPlugin(IOutputPlugin):
    """委派深度守卫 Output 插件。

    delegate 路由信号移除后仅保留深度字段的首次初始化（默认 0 / 默认上限），
    不再做深度检查。

    优先级：3

    Attributes:
        _config: 插件配置字典
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化委派深度守卫插件。

        Args:
            config: 插件配置字典，支持以下键：
                - enabled: 是否启用深度守卫（默认 True）
                - max_depth: 最大允许委派深度（默认 3）
                - depth_key: state 中深度字段的键名（默认 "delegate_depth"）
                - max_depth_key: state 中最大深度字段的键名（默认 "max_delegate_depth"）
        """
        self._config = config or {}
        self._enabled = self._config.get("enabled", True)
        self._max_depth = self._config.get("max_depth", 3)
        self._depth_key = self._config.get("depth_key", "delegate_depth")
        self._max_depth_key = self._config.get("max_depth_key", "max_delegate_depth")

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "delegate_depth_guard"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return self._config.get("priority", 3)

    @property
    def route_signals(self) -> list[str]:
        """本插件关注的路由信号类型列表。

        0.2 起返回空：delegate 信号已从引擎协议移除（见模块 docstring），
        插件不再声明/拦截任何路由信号，执行时纯透传。
        """
        return []

    async def execute(self, ctx: PluginContext) -> OutputResult:
        """初始化深度字段（仅首次运行写入）。"""
        if not self._enabled:
            return OutputResult()

        state_updates: dict[str, Any] = {}
        if self._depth_key not in ctx.state:
            state_updates[self._depth_key] = 0
        if self._max_depth_key not in ctx.state:
            state_updates[self._max_depth_key] = self._max_depth
        return OutputResult(state_updates=state_updates)
