"""Agent 层级权限守卫 Input 插件。

在工具执行前检查当前 Agent 的层级是否有权调用该工具。
权限映射由 Agent 配置的 tool_ids 决定——Agent 只能看到/调用
自己 tool_ids 列表里的工具，低层级 Agent 的 tool_ids 不包含
高危工具，自然无法调用。

本插件从 state 读取 agent_level 和当前工具调用列表，
结合 Agent 自身声明的 tool_ids（SSOT）拦截越权工具调用。
不再维护硬编码白名单——可见性 == 授权，由 tool_ids 构造保证。

State 命名空间：
    - security.level_decision : 本插件写入的层级权限决策结果
    - tool_ids : Agent 配置的可见工具集合（由 tool_schema 写入）
"""

from __future__ import annotations

import logging
from typing import Any

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import ErrorPolicy, StateKeys

logger = logging.getLogger(__name__)

# 只读探查类工具：所有层级 Agent 都可调用的基础设施，不受 tool_ids 白名单限制。
# 这些工具只读取信息（不修改系统状态），是 Agent 探查环境的基础能力，
# 不应因 yaml 未显式声明而被 level_guard 拦截。
READONLY_PROBE_TOOLS: frozenset[str] = frozenset({
    "enhanced_search",
    "file_read",
    "read_file",
    "list_directory",
})


class LevelGuardPlugin(IInputPlugin):
    """Agent 层级权限守卫 Input 插件。

    根据当前 Agent 的层级（agent_level）和 tool_ids（SSOT）
    过滤可执行的工具调用。tool_ids 是唯一事实源——
    LLM 看不到的工具，天然无法被调用。

    优先级：20（最先执行，授权最廉价，最先短路）
    错误策略：ABORT（权限问题必须停止）

    Attributes:
        _config: 插件配置字典
    """

    error_policy = ErrorPolicy.ABORT

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化层级权限守卫插件。

        Args:
            config: 插件配置字典，支持以下键：
                - enabled: 是否启用权限守卫（默认 True）
                - strict: 严格模式——tool_ids 缺失时拦截（默认 True）
        """
        self._config = config or {}
        self._enabled = self._config.get("enabled", True)
        self._strict = self._config.get("strict", True)

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "level_guard"

    @property
    def priority(self) -> int:
        """插件执行优先级。授权最廉价，最先短路。"""
        return self._config.get("priority", 20)

    async def execute(self, ctx: PluginContext) -> PluginResult:
        """执行层级权限检查。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含权限决策状态更新的插件执行结果
        """
        result = await self._do_work(ctx)
        return PluginResult(state_updates=result)

    async def _do_work(self, ctx: PluginContext) -> dict[str, Any]:
        """执行层级权限检查逻辑。

        从 state["tool_ids"] 读取 Agent 授权的工具集合（SSOT），
        校验请求调用的工具是否在授权集合内。
        不再维护硬编码白名单——可见性 == 授权，由 tool_ids 构造保证。

        Args:
            ctx: 插件执行上下文

        Returns:
            权限决策结果字典
        """
        if not self._enabled:
            return {"security.level_decision": {"allowed": True, "reason": "level guard disabled"}}

        core_type = ctx.state.get(StateKeys.CORE_TYPE, "llm_call")

        # 非 tool_execute 不需要权限检查
        if core_type != "tool_execute":
            return {"security.level_decision": {"allowed": True, "reason": "not a tool execution"}}

        # 获取当前工具调用
        tool_calls = ctx.state.get(StateKeys.RAW_TOOL_CALLS, [])
        if not tool_calls:
            return {"security.level_decision": {"allowed": True, "reason": "no tool calls to check"}}

        # 从 state 读取 Agent 的 tool_ids（SSOT，由 tool_schema 插件写入）
        tool_ids = ctx.state.get("tool_ids", None)
        if tool_ids is None:
            # tool_ids 缺失：严格模式拦截，非严格模式放行
            if self._strict:
                agent_level = ctx.state.get(StateKeys.AGENT_LEVEL, "unknown")
                reason = f"tool_ids not found in state, cannot verify permissions for level {agent_level}"
                logger.warning("[%s] %s", self.name, reason)
                return {"security.level_decision": {"allowed": False, "reason": reason}}
            return {"security.level_decision": {"allowed": True, "reason": "tool_ids missing but strict=False"}}

        # tool_ids 是唯一事实源：在列表里的工具 = 已授权
        allowed_tools = set(tool_ids)

        # 逐个检查工具调用
        agent_level = ctx.state.get(StateKeys.AGENT_LEVEL, "unknown")
        blocked_tools: list[str] = []
        for tc in tool_calls:
            tool_name = tc.get("name", "")
            # 只读探查工具（enhanced_search/file_read/list_directory 等）
            # 是所有层级 Agent 的基础能力，豁免 tool_ids 检查。
            if tool_name in READONLY_PROBE_TOOLS:
                continue
            if tool_name not in allowed_tools:
                blocked_tools.append(tool_name)

        if blocked_tools:
            reason = (
                f"Agent level {agent_level} not allowed to call: "
                f"{', '.join(blocked_tools)}"
            )
            logger.warning(
                "[%s] Blocked by level guard | level=%s | tools=%s",
                self.name, agent_level, blocked_tools,
            )
            decision = {
                "allowed": False,
                "reason": reason,
                "blocked_tools": blocked_tools,
                "agent_level": agent_level,
            }
            return {"security.level_decision": decision}

        return {"security.level_decision": {"allowed": True, "reason": "all tools within tool_ids authorization"}}
