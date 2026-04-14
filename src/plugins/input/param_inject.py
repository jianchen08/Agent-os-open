"""参数注入 Input 插件。

负责在管道循环的输入阶段为工具调用注入运行时参数，
包括会话 ID、用户信息、时间戳等上下文参数，
以及工具特定的默认参数填充。

M6b 阶段：从旧代码 isolation/tools.py 的参数预处理逻辑迁移。

State 命名空间：
    - tool.params_injected : 本插件标记参数已注入
"""

from __future__ import annotations

import logging
from typing import Any

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import ErrorPolicy, StateKeys

logger = logging.getLogger(__name__)


class ParamInjectPlugin(IInputPlugin):
    """参数注入 Input 插件。

    在工具执行前为工具调用参数注入运行时上下文信息，
    例如会话 ID、用户 ID、时间戳等。同时支持为特定工具
    填充默认参数值。

    优先级：20（准备级，在安全检查之前完成参数注入）
    错误策略：ABORT（参数注入失败工具无法执行）

    Attributes:
        _config: 插件配置字典
    """

    error_policy = ErrorPolicy.ABORT

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化参数注入插件。

        Args:
            config: 插件配置字典，支持以下键：
                - inject_session_id: 是否注入会话 ID（默认 True）
                - inject_user_id: 是否注入用户 ID（默认 True）
                - inject_timestamp: 是否注入时间戳（默认 True）
                - default_params: 工具默认参数映射 {tool_name: {param: value}}
        """
        self._config = config or {}
        self._inject_session_id = self._config.get("inject_session_id", True)
        self._inject_user_id = self._config.get("inject_user_id", True)
        self._inject_timestamp = self._config.get("inject_timestamp", True)
        self._default_params = self._config.get("default_params", {})

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "param_inject"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return self._config.get("priority", 20)

    async def execute(self, ctx: PluginContext) -> PluginResult:
        """执行参数注入。

        为 state 中的工具调用参数注入运行时上下文信息。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含注入参数状态更新的插件执行结果
        """
        result = await self._do_work(ctx)
        return PluginResult(state_updates=result)

    async def _do_work(self, ctx: PluginContext) -> dict[str, Any]:
        """执行参数注入逻辑。

        Args:
            ctx: 插件执行上下文

        Returns:
            更新后的工具调用参数字典
        """
        updates: dict[str, Any] = {}

        core_type = ctx.state.get(StateKeys.CORE_TYPE, "llm_call")
        if core_type != "tool_execute":
            return {"tool.params_injected": False}

        tool_calls = ctx.state.get(StateKeys.RAW_TOOL_CALLS, [])
        if not tool_calls:
            return {"tool.params_injected": False}

        # 注入上下文参数到每个工具调用
        injected_calls = []
        for tc in tool_calls:
            injected_tc = dict(tc)
            args = dict(injected_tc.get("args", {}))

            # 注入运行时参数（仅当参数不存在时才注入）
            if self._inject_session_id and "session_id" not in args:
                session_id = ctx.state.get(StateKeys.SESSION_ID, "")
                if session_id:
                    args["session_id"] = session_id

            if self._inject_user_id and "user_id" not in args:
                user_id = ctx.state.get("user_id", "")
                if user_id:
                    args["user_id"] = user_id

            if self._inject_timestamp and "timestamp" not in args:
                from datetime import UTC, datetime
                args["timestamp"] = datetime.now(UTC).isoformat()

            # 注入工具默认参数
            tool_name = injected_tc.get("name", "")
            if tool_name in self._default_params:
                for param, value in self._default_params[tool_name].items():
                    if param not in args:
                        args[param] = value

            injected_tc["args"] = args
            injected_calls.append(injected_tc)

        updates[StateKeys.RAW_TOOL_CALLS] = injected_calls
        updates["tool.params_injected"] = True

        logger.debug(
            "[%s] Parameters injected | count=%d",
            self.name, len(injected_calls),
        )

        return updates
