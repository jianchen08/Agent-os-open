"""Agent 层级权限守卫 Input 插件。

在工具执行前检查当前 Agent 的层级是否有权调用该工具。
权限映射由 Agent 配置的 tool_ids 决定——Agent 只能看到/调用
自己 tool_ids 列表里的工具，低层级 Agent 的 tool_ids 不包含
高危工具，自然无法调用。

本插件从 state 读取 agent_level 和当前工具调用列表，
结合 level_permissions 配置（定义各层级允许的工具模式/前缀），
拦截越权工具调用。

State 命名空间：
    - security.level_decision : 本插件写入的层级权限决策结果
"""

from __future__ import annotations

import logging
from typing import Any

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import ErrorPolicy, StateKeys

logger = logging.getLogger(__name__)


# 各层级允许的工具名称前缀/模式
# L3 可以调用所有工具，L2 只能调用非高危工具，L1 只能调用基础工具
_DEFAULT_L1_ALLOWED = {
    "task_submit", "task_manage", "task_evaluate",
    "resource_search", "read_file", "list_dir",
}
_DEFAULT_L2_ALLOWED = {
    # L2 包含 L1 的所有工具 + 中等风险工具
    *_DEFAULT_L1_ALLOWED,
    "write_file", "edit_file", "web_search", "bash",
}
# L3 不受限，allowed = None 表示全部允许


class LevelGuardPlugin(IInputPlugin):
    """Agent 层级权限守卫 Input 插件。

    根据当前 Agent 的层级（agent_level）过滤可执行的工具调用。
    低层级 Agent 尝试调用超出权限的工具时，该工具调用被拦截。

    优先级：65（在 security_check 之后，参数注入之后）
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
                - l1_allowed: L1 层级允许的工具名集合
                - l2_allowed: L2 层级允许的工具名集合
                - l3_allowed: L3 层级允许的工具名集合（默认 None=全部允许）
                - strict: 严格模式——未在允许列表中的工具一律拦截（默认 True）
        """
        self._config = config or {}
        self._enabled = self._config.get("enabled", True)
        self._l1_allowed: set[str] | None = set(self._config.get("l1_allowed", list(_DEFAULT_L1_ALLOWED)))
        self._l2_allowed: set[str] | None = set(self._config.get("l2_allowed", list(_DEFAULT_L2_ALLOWED)))
        self._l3_allowed: set[str] | None = self._config.get("l3_allowed", None)
        self._strict = self._config.get("strict", True)

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "level_guard"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return self._config.get("priority", 65)

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

        # 获取当前 Agent 层级
        agent_level = ctx.state.get(StateKeys.AGENT_LEVEL, "l1_main")

        # 获取当前工具调用
        tool_calls = ctx.state.get(StateKeys.RAW_TOOL_CALLS, [])
        if not tool_calls:
            return {"security.level_decision": {"allowed": True, "reason": "no tool calls to check"}}

        # 获取该层级的允许列表
        allowed_tools = self._get_allowed_tools(agent_level)

        # L3 或未配置允许列表 = 全部允许
        if allowed_tools is None:
            return {"security.level_decision": {"allowed": True, "reason": f"level {agent_level} has full access"}}

        # 逐个检查工具调用
        blocked_tools: list[str] = []
        for tc in tool_calls:
            tool_name = tc.get("name", "")
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

        return {"security.level_decision": {"allowed": True, "reason": "all tools within level permission"}}

    def _get_allowed_tools(self, agent_level: str) -> set[str] | None:
        """根据 Agent 层级获取允许的工具集合。

        Args:
            agent_level: Agent 层级字符串（l1_main/l2_subtask/l3_atomic）

        Returns:
            允许的工具名集合，None 表示全部允许
        """
        if "l1" in agent_level or agent_level == "L1":
            return self._l1_allowed
        elif "l2" in agent_level or agent_level == "L2":
            return self._l2_allowed
        elif "l3" in agent_level or agent_level == "L3":
            return self._l3_allowed
        else:
            # 未知层级，严格模式禁止，非严格模式允许
            if self._strict:
                return set()  # 空集合 = 全部禁止
            return None
