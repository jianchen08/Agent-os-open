"""Agent 层级权限守卫 Input 插件。

只对「任务类工具」做硬限制——这类工具直接改变任务系统的状态
（提交/管理/评估子任务），层级越权会破坏 L1→L2→L3 的委托链，
必须按 Agent 自身声明的 tool_ids 严格拦截。

其它工具（file_write / bash_execute / enhanced_search / memory /
human_interaction 等通用执行工具）一律软放行：本插件不拦截、不告警、
不写日志。它们的「软限制」由两层兜住：
1. 可见性过滤：tool_schema 插件只把 tool_ids 内的工具注入到
   state["tool_schemas"]，LLM 看不到未授权工具，自然不会调用；
2. 提示词约束：Agent yaml 的 system_prompt / hard_constraints
   说明该 Agent 只应使用哪些工具。

这样既不破坏「编排者必须自己产出报告」等法定产出职责
（L2 需要写文件时不会被误拦），又能精确守住任务委托边界。

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

# 任务类工具：直接操作任务系统的工具，越权会破坏 L1→L2→L3 委托链。
# 只有这类工具受 tool_ids 硬限制——不在 Agent 授权集合内就拦截。
# task_manage / task_submit / task_evaluate 内部还会按 parent_agent_level
# 做二次校验，本插件负责第一道 tool_ids 过滤。
TASK_CONTROL_TOOLS: frozenset[str] = frozenset(
    {
        "task_submit",
        "task_manage",
        "task_evaluate",
    }
)


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

    async def _do_work(self, ctx: PluginContext) -> dict[str, Any]:  # noqa: PLR0911
        """执行层级权限检查逻辑。

        只对任务类工具（TASK_CONTROL_TOOLS）做硬限制：检查它是否在
        Agent 的 tool_ids 授权集合内。其余工具一律软放行，由 tool_schema
        的可见性过滤和提示词约束兜底（软限制）。

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

        agent_level = ctx.state.get(StateKeys.AGENT_LEVEL, "unknown")

        # 只检查任务类工具的授权。其它工具软放行——
        # 可见性由 tool_schema 控制（LLM 看不到未授权工具），
        # 职责由 yaml 提示词约束，无需本插件硬拦。
        task_tool_calls = [tc for tc in tool_calls if tc.get("name", "") in TASK_CONTROL_TOOLS]
        if not task_tool_calls:
            return {
                "security.level_decision": {
                    "allowed": True,
                    "reason": "no task-control tools to check (others are soft-gated by tool_schema visibility)",
                },
            }

        # 从 state 读取 Agent 的 tool_ids（SSOT，由 tool_schema 插件写入）
        tool_ids = ctx.state.get("tool_ids", None)
        if tool_ids is None:
            # tool_ids 缺失：严格模式拦截，非严格模式放行
            if self._strict:
                reason = f"tool_ids not found in state, cannot verify task-control permissions for level {agent_level}"
                logger.warning("[%s] %s", self.name, reason)
                return {"security.level_decision": {"allowed": False, "reason": reason}}
            return {"security.level_decision": {"allowed": True, "reason": "tool_ids missing but strict=False"}}

        # tool_ids 是任务类工具授权的唯一事实源
        allowed_tools = set(tool_ids)

        # 逐个检查任务类工具调用
        blocked_tools: list[str] = []
        for tc in task_tool_calls:
            tool_name = tc.get("name", "")
            if tool_name not in allowed_tools:
                blocked_tools.append(tool_name)

        if blocked_tools:
            reason = f"Agent level {agent_level} not allowed to call task tools: {', '.join(blocked_tools)}"
            logger.warning(
                "[%s] Blocked by level guard | level=%s | tools=%s",
                self.name,
                agent_level,
                blocked_tools,
            )
            decision = {
                "allowed": False,
                "reason": reason,
                "blocked_tools": blocked_tools,
                "agent_level": agent_level,
            }
            return {"security.level_decision": decision}

        return {
            "security.level_decision": {"allowed": True, "reason": "task-control tools within tool_ids authorization"}
        }
