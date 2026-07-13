"""工具上下文感知 Input 插件。

在管道输入阶段收集工具在线状态和 Electron 窗口信息，
构建 tool_context 字典注入到管道 state 中，供后续插件和 LLM 使用。

感知层级：
- 轻量感知：从 ToolRegistry 读取所有工具在线状态（has_handler 判断）
- 中量感知：从 ctx.state 读取 Electron 附带的窗口信息（可能为空）
- 适配器感知：从 capability_adapters.yaml 读取适配器配置状态

降级策略：ToolRegistry 不可用时跳过，记录日志但不中断管道。

State 命名空间：
    - tool_context : 本插件写入的工具上下文字典

tool_context 数据结构（满足审批视图路由输入要求）：
    {
        "online_tools": list[str],           # 在线工具名列表
        "active_window": dict | None,        # 规范化后的窗口信息
        "adapter_status": dict,              # 适配器配置状态摘要
        "timestamp": float,                  # 采集时间戳
    }
"""

from __future__ import annotations

import logging
import time
from typing import Any

from bridge.window_info import normalize_window_info
from connectors.adapter_config import get_adapter_status_summary
from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import ErrorPolicy

logger = logging.getLogger(__name__)


class ToolContextPlugin(IInputPlugin):
    """工具上下文感知 Input 插件。

    从 ToolRegistry 收集在线工具列表，从 ctx.state 读取 Electron 窗口信息，
    从 capability_adapters.yaml 读取适配器状态，构建 tool_context 注入管道 state。

    优先级：40（在 ToolSchemaPlugin(50) 之前执行）
    错误策略：FALLBACK（上下文缺失不影响管道运行）

    Attributes:
        _config: 插件配置字典
        _enabled: 是否启用
    """

    error_policy = ErrorPolicy.ABORT

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化工具上下文感知插件。

        Args:
            config: 插件配置字典，支持以下键：
                - enabled: 是否启用（默认 True）
                - priority: 执行优先级（默认 40）
        """
        self._config = config or {}
        self._enabled = self._config.get("enabled", True)

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "tool_context"

    @property
    def priority(self) -> int:
        """插件执行优先级，数值越小越先执行。"""
        return self._config.get("priority", 40)

    async def execute(self, ctx: PluginContext) -> PluginResult:
        """收集工具上下文并写入 state。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含 tool_context 状态更新的插件执行结果
        """
        tool_context = await self._build_tool_context(ctx)
        return PluginResult(state_updates={"tool_context": tool_context})

    async def _build_tool_context(self, ctx: PluginContext) -> dict[str, Any]:
        """构建工具上下文字典。

        分别收集轻量感知（工具在线状态）、中量感知（窗口信息）
        和适配器感知（配置状态），合并为完整的 tool_context。

        Args:
            ctx: 插件执行上下文

        Returns:
            工具上下文字典，包含 online_tools、active_window、
            adapter_status、timestamp
        """
        if not self._enabled:
            logger.debug("[%s] Plugin disabled, returning empty context", self.name)
            return {
                "online_tools": [],
                "active_window": None,
                "adapter_status": {},
                "timestamp": time.time(),
            }

        online_tools = self._collect_online_tools(ctx)
        active_window = self._collect_active_window(ctx)
        adapter_status = self._collect_adapter_status()

        tool_context: dict[str, Any] = {
            "online_tools": online_tools,
            "active_window": active_window,
            "adapter_status": adapter_status,
            "timestamp": time.time(),
        }

        logger.debug(
            "[%s] Tool context built | online_tools=%d | has_window=%s | adapters=%d",
            self.name,
            len(online_tools),
            active_window is not None,
            len(adapter_status),
        )

        return tool_context

    def _collect_online_tools(self, ctx: PluginContext) -> list[str]:
        """从 ToolRegistry 收集在线工具名称列表。

        轻量感知：遍历 registry 中所有工具，筛选出有 handler 的（即在线可用的）。

        降级策略：ToolRegistry 不可用或调用异常时，返回空列表并记录日志。

        Args:
            ctx: 插件执行上下文

        Returns:
            在线工具名称列表
        """
        try:
            tool_registry = ctx.get_service("tool_registry")
        except KeyError:
            logger.debug(
                "[%s] No tool_registry service available, skipping online tools",
                self.name,
            )
            return []

        try:
            all_tools = tool_registry.list_all()
            online_tools: list[str] = []
            for tool in all_tools:
                tool_name = getattr(tool, "name", None)
                if tool_name is None:
                    continue
                if tool_registry.has_handler(tool_name):
                    online_tools.append(tool_name)
            return online_tools
        except Exception as exc:
            logger.warning(
                "[%s] Failed to collect online tools from registry: %s",
                self.name,
                exc,
            )
            return []

    def _collect_active_window(self, ctx: PluginContext) -> dict[str, Any] | None:
        """从 ctx.state 读取 Electron 附带的窗口信息并规范化。

        中量感知：Electron 客户端运行时会附带当前窗口信息，
        未运行时该字段不存在。

        规范化处理：
        1. 支持 Electron 标准格式（processName 字段）
        2. 兼容旧格式（app/bounds 字段）
        3. 无效输入返回 None

        Args:
            ctx: 插件执行上下文

        Returns:
            规范化后的窗口信息字典，不存在时返回 None
        """
        raw_window_info = ctx.state.get("electron_window")
        if raw_window_info is None:
            return None

        # 使用桥接层规范化
        window_data = normalize_window_info(raw_window_info)
        if window_data is None:
            return None

        return window_data.to_dict()

    def _collect_adapter_status(self) -> dict[str, dict[str, Any]]:
        """从 capability_adapters.yaml 收集适配器配置状态。

        适配器感知：读取所有适配器的配置信息，包括类型、启用状态、能力列表。

        降级策略：配置文件不可用时返回空字典。

        Returns:
            适配器名称到状态摘要的映射
        """
        try:
            return get_adapter_status_summary()
        except Exception as exc:
            logger.warning(
                "[%s] Failed to load adapter configs: %s",
                self.name,
                exc,
            )
            return {}
