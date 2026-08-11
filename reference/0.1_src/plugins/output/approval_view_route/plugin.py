"""审批视图路由 Output 插件。

根据 tool_context 中的内容类型，将审批请求路由到不同的渲染模式。
支持多种内容类型的自动识别和路由决策。

路由规则：
    - content_type 为空或 "text"    → 渲染为纯文本审批视图
    - content_type 为 "code_diff"   → 渲染为代码差异审批视图
    - content_type 为 "file_change" → 渲染为文件变更审批视图
    - content_type 为 "command"     → 渲染为命令确认审批视图
    - content_type 未知             → 渲染为默认文本审批视图 + 警告日志

State 读取：
    - tool_context : 由 ToolContextPlugin 写入的工具上下文
    - approval_required : 管道状态中的审批标记
    - raw_result : 核心执行结果（含 content_type 信息）

State 写入：
    - approval_render_mode : 路由决策结果（渲染模式字符串）
    - routed_to : 路由目标标记
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any

from pipeline.plugin import IOutputPlugin, OutputResult, PluginContext
from pipeline.types import ErrorPolicy, StateKeys

logger = logging.getLogger(__name__)


class RenderMode(str, Enum):
    """审批视图渲染模式。"""

    TEXT = "text"
    CODE_DIFF = "code_diff"
    FILE_CHANGE = "file_change"
    COMMAND = "command"
    UNKNOWN = "unknown"


# 已知内容类型到渲染模式的映射
_CONTENT_TYPE_MAP: dict[str, RenderMode] = {
    "text": RenderMode.TEXT,
    "code_diff": RenderMode.CODE_DIFF,
    "file_change": RenderMode.FILE_CHANGE,
    "command": RenderMode.COMMAND,
    "diff": RenderMode.CODE_DIFF,
    "patch": RenderMode.CODE_DIFF,
    "file": RenderMode.FILE_CHANGE,
    "shell": RenderMode.COMMAND,
    "bash": RenderMode.COMMAND,
}


class ApprovalViewRoutePlugin(IOutputPlugin):
    """审批视图路由 Output 插件。

    读取 tool_context 和 raw_result 中的信息，判断内容类型，
    将审批请求路由到对应的渲染模式。

    路由输入依赖：
        - tool_context（由 ToolContextPlugin 产出）
        - approval_required（管道状态标记）
        - raw_result（核心执行结果）

    优先级：35（在 result_format(20) 之后，stop_check(10) 之前）
    错误策略：SKIP（路由失败不阻塞管道）

    Attributes:
        _config: 插件配置字典
    """

    error_policy = ErrorPolicy.SKIP

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化审批视图路由插件。

        Args:
            config: 插件配置字典，支持以下键：
                - priority: 执行优先级（默认 35）
                - custom_type_map: 自定义内容类型映射
        """
        self._config = config or {}
        self._type_map = dict(_CONTENT_TYPE_MAP)
        # 合并自定义类型映射
        custom_map = self._config.get("custom_type_map", {})
        for k, v in custom_map.items():
            try:
                self._type_map[k] = RenderMode(v)
            except ValueError:
                logger.warning(
                    "[%s] 忽略无效的自定义渲染模式: %s=%s",
                    self.name,
                    k,
                    v,
                )

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "approval_view_route"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return self._config.get("priority", 35)

    @property
    def route_signals(self) -> list[str]:
        """本插件可能产出的路由信号。"""
        return ["approval_required"]

    async def execute(self, ctx: PluginContext) -> OutputResult:
        """执行审批视图路由决策。

        仅当 approval_required=True 时执行路由逻辑，
        否则跳过（返回空结果）。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含路由决策状态更新的输出结果
        """
        approval_required = ctx.state.get(StateKeys.APPROVAL_REQUIRED, False)
        if not approval_required:
            return OutputResult()

        # 从 raw_result 提取 content_type
        content_type = self._extract_content_type(ctx)

        # 路由到渲染模式
        render_mode = self._route(content_type)

        # 补充 tool_context 中的适配器信息（如有）
        tool_context = ctx.state.get("tool_context", {})
        adapter_hint = self._get_adapter_hint(tool_context, content_type)

        logger.info(
            "[%s] 审批路由决策 | content_type=%s | render_mode=%s | adapter=%s",
            self.name,
            content_type,
            render_mode.value,
            adapter_hint,
        )

        return OutputResult(
            state_updates={
                "approval_render_mode": render_mode.value,
                StateKeys.ROUTED_TO: f"approval:{render_mode.value}",
            }
        )

    def _extract_content_type(self, ctx: PluginContext) -> str:
        """从管道状态中提取内容类型。

        优先级：
        1. state["content_type"]（显式指定）
        2. raw_result 中的 content_type 字段
        3. 默认 "text"

        Args:
            ctx: 插件执行上下文

        Returns:
            内容类型字符串
        """
        # 1. 显式指定
        explicit = ctx.state.get("content_type")
        if explicit and isinstance(explicit, str):
            return explicit

        # 2. 从 raw_result 推断
        raw_result = ctx.state.get(StateKeys.RAW_RESULT)
        if isinstance(raw_result, dict):
            ct = raw_result.get("content_type")
            if isinstance(ct, str) and ct:
                return ct

        # 3. 从 tool_results 推断
        tool_results = ctx.state.get(StateKeys.TOOL_RESULTS, [])
        if isinstance(tool_results, list) and tool_results:
            first_result = tool_results[0]
            if isinstance(first_result, dict):
                name = first_result.get("name", "")
                # 根据工具名推断内容类型
                if "diff" in name or "patch" in name:
                    return "code_diff"
                if "file" in name:
                    return "file_change"
                if "bash" in name or "shell" in name or "exec" in name:
                    return "command"

        # 4. 默认
        return "text"

    def _route(self, content_type: str) -> RenderMode:
        """根据内容类型路由到渲染模式。

        Args:
            content_type: 内容类型字符串

        Returns:
            对应的渲染模式枚举值
        """
        content_type_lower = content_type.lower().strip()
        mode = self._type_map.get(content_type_lower)

        if mode is not None:
            return mode

        # 未知内容类型
        logger.warning(
            "[%s] 未知内容类型 '%s'，降级为默认文本渲染",
            self.name,
            content_type,
        )
        return RenderMode.UNKNOWN

    def _get_adapter_hint(
        self,
        tool_context: dict[str, Any],
        content_type: str,
    ) -> str | None:
        """从 tool_context 的 adapter_status 中找到匹配的适配器。

        Args:
            tool_context: 工具上下文字典
            content_type: 内容类型

        Returns:
            匹配的适配器名称，无匹配时返回 None
        """
        adapter_status = tool_context.get("adapter_status", {})
        if not isinstance(adapter_status, dict):
            return None

        # 根据内容类型和适配器能力进行匹配
        type_capability_map: dict[str, set[str]] = {
            "code_diff": {"show_diff", "open_file"},
            "file_change": {"open_file", "open_folder"},
            "command": {"keyboard_input"},
            "screenshot": {"screenshot", "screen_capture"},
        }

        required_caps = type_capability_map.get(content_type, set())
        if not required_caps:
            return None

        for adapter_name, status in adapter_status.items():
            if not isinstance(status, dict):
                continue
            adapter_caps = set(status.get("capabilities", []))
            if required_caps & adapter_caps:
                return adapter_name

        return None
