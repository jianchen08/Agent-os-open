"""工具进度回调 Output 插件。

负责在管道循环的输出阶段构建工具执行进度信息，
通过 PluginContext 发布进度事件（如果 event_bus 服务可用），
并记录进度日志。

State 命名空间：
    - tool_progress : 工具执行进度信息列表
"""

from __future__ import annotations

import logging
from typing import Any

from pipeline.plugin import IOutputPlugin, OutputResult, PluginContext
from pipeline.types import ErrorPolicy, StateKeys

logger = logging.getLogger(__name__)


class ToolProgressReporter(IOutputPlugin):
    """工具进度回调 Output 插件。

    读取工具执行结果和原始调用信息，构建 ToolProgress 数据，
    通过 event_bus 服务发布进度事件，并记录日志。

    进度数据结构：
    {
        "tool_name": str,
        "status": str,       # success / failed / pending
        "result_summary": str  # 结果摘要（截断到 200 字符）
    }

    优先级：30（处理级）
    错误策略：SKIP（进度报告失败不阻塞管道）

    Attributes:
        _config: 插件配置字典
    """

    error_policy = ErrorPolicy.SKIP

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化工具进度报告插件。

        Args:
            config: 插件配置字典，支持以下键：
                - enabled: 是否启用进度报告（默认 True）
                - summary_max_length: 结果摘要最大长度（默认 200）
        """
        self._config = config or {}
        self._enabled = self._config.get("enabled", True)
        self._summary_max_length = self._config.get("summary_max_length", 200)

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "tool_progress_reporter"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return self._config.get("priority", 30)

    async def execute(self, ctx: PluginContext) -> OutputResult:
        """执行进度报告。

        读取 tool_results 和 raw_tool_calls，构建进度数据列表，
        尝试通过 event_bus 发布进度事件，并记录日志。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含进度信息状态更新的输出结果
        """
        if not self._enabled:
            return OutputResult()

        tool_results = ctx.state.get(StateKeys.TOOL_RESULTS, [])
        raw_tool_calls = ctx.state.get(StateKeys.RAW_TOOL_CALLS, [])

        progress_list = self._build_progress(raw_tool_calls, tool_results)

        if progress_list:
            self._publish_progress(ctx, progress_list)

            for p in progress_list:
                logger.info(
                    "[%s] Tool progress | name=%s | status=%s | summary=%.80s",
                    self.name,
                    p["tool_name"],
                    p["status"],
                    p["result_summary"],
                )

        return OutputResult(state_updates={"tool_progress": progress_list})

    def _build_progress(
        self,
        raw_tool_calls: list[dict[str, Any]],
        tool_results: list[Any],
    ) -> list[dict[str, Any]]:
        """构建工具执行进度数据列表。

        将 raw_tool_calls 和 tool_results 对应组合，
        生成每个工具的进度信息。

        Args:
            raw_tool_calls: 原始工具调用列表
            tool_results: 工具执行结果列表

        Returns:
            进度数据列表
        """
        progress_list: list[dict[str, Any]] = []
        call_count = len(raw_tool_calls)
        result_count = len(tool_results)

        for i in range(max(call_count, result_count)):
            tool_name = ""
            status = "pending"
            result_summary = ""

            if i < call_count:
                tool_name = raw_tool_calls[i].get("name", "unknown")

            if i < result_count:
                result = tool_results[i]
                if isinstance(result, dict) and "error" in result:
                    status = "failed"
                    result_summary = str(result["error"])[: self._summary_max_length]
                elif result is not None:
                    status = "success"
                    result_summary = str(result)[: self._summary_max_length]
                else:
                    status = "pending"

            progress_list.append(
                {
                    "tool_name": tool_name,
                    "status": status,
                    "result_summary": result_summary,
                }
            )

        return progress_list

    def _publish_progress(
        self,
        ctx: PluginContext,
        progress_list: list[dict[str, Any]],
    ) -> None:
        """通过 event_bus 发布进度事件。

        尝试获取 event_bus 服务，如果可用则发布 tool_progress 事件。
        服务不可用时静默跳过，不影响管道执行。

        Args:
            ctx: 插件执行上下文
            progress_list: 进度数据列表
        """
        try:
            event_bus = ctx.get_service("event_bus")
            if hasattr(event_bus, "emit"):
                event_bus.emit(
                    "tool_progress",
                    {
                        "progress": progress_list,
                        "session_id": ctx.state.get(StateKeys.SESSION_ID, ""),
                        "task_id": ctx.state.get(StateKeys.TASK_ID, ""),
                    },
                )
        except KeyError:
            logger.debug(
                "[%s] event_bus service not available, skipping event publish",
                self.name,
            )
