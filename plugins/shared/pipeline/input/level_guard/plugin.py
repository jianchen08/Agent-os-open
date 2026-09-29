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
    - pre_decided_results : 拦截方直出的预定结果（ADR 2026-09-28 结果预填：
      本插件对越权任务类工具调用直写拒绝结果，tool_core 命中即跳过执行；
      多 guard 经 SDK merge_pre_decided 按 call_id 合并，无 id 按工具名兜底）
    - tool_ids : Agent 配置的可见工具集合（由 tool_schema 写入）
"""

from __future__ import annotations

import logging
from typing import Any

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import StateKeys

from agentos_plugin_sdk.tool_result_protocol import (
    merge_pre_decided,
    tool_result_entry,
)

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
    权限问题必须停止。

    Attributes:
        _config: 插件配置字典
    """

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

        只对任务类工具（TASK_CONTROL_TOOLS）做硬限制：检查它是否在
        Agent 的 tool_ids 授权集合内。其余工具一律软放行，由 tool_schema
        的可见性过滤和提示词约束兜底（软限制）。越权调用经结果预填直出
        拒绝结果（ADR 2026-09-28），tool_core 幂等跳过执行。

        Args:
            ctx: 插件执行上下文

        Returns:
            状态更新（拦截时含 pre_decided_results；无拦截零产出）
        """
        if not self._enabled:
            return {}

        core_type = ctx.state.get(StateKeys.CORE_TYPE, "llm_call")

        # 非 tool_execute 不需要权限检查
        if core_type != "tool_execute":
            return {}

        tool_calls = ctx.state.get(StateKeys.RAW_TOOL_CALLS, [])
        if not tool_calls:
            return {}

        agent_level = ctx.state.get(StateKeys.AGENT_LEVEL, "unknown")

        # 只检查任务类工具的授权。其它工具软放行——
        # 可见性由 tool_schema 控制（LLM 看不到未授权工具），
        # 职责由 yaml 提示词约束，无需本插件硬拦。
        task_tool_calls = [tc for tc in tool_calls if tc.get("name", "") in TASK_CONTROL_TOOLS]
        if not task_tool_calls:
            return {}

        # 从 state 读取 Agent 的 tool_ids（SSOT，由 tool_schema 插件写入）
        tool_ids = ctx.state.get("tool_ids", None)
        if tool_ids is None:
            # tool_ids 缺失：严格模式全量预填拒绝（fail-closed，对齐旧
            # 决策键缺 blocked_tools = 全拦语义），非严格模式放行
            if self._strict:
                reason = (
                    f"tool_ids not found in state, cannot verify task-control "
                    f"permissions for level {agent_level}"
                )
                logger.warning("[%s] %s", self.name, reason)
                return self._prefill_rejections(ctx, tool_calls, reason, agent_level)
            return {}

        # tool_ids 是任务类工具授权的唯一事实源
        allowed_tools = set(tool_ids)
        blocked = [tc for tc in task_tool_calls if tc.get("name", "") not in allowed_tools]
        if not blocked:
            return {}

        names = ", ".join(tc.get("name", "") for tc in blocked)
        reason = f"Agent level {agent_level} not allowed to call task tools: {names}"
        logger.warning(
            "[%s] Blocked by level guard | level=%s | tools=%s",
            self.name,
            agent_level,
            names,
        )
        return self._prefill_rejections(ctx, blocked, reason, agent_level)

    def _prefill_rejections(
        self,
        ctx: PluginContext,
        calls: list[dict[str, Any]],
        reason: str,
        agent_level: str,
    ) -> dict[str, Any]:
        """对被拦调用直出预定拒绝结果（有 id 按 call_id，无 id 名字兜底）。"""
        entries = [
            tool_result_entry(
                tc.get("name", ""),
                call_id=tc.get("id"),
                success=False,
                error=f"工具被权限策略拦截: {reason}",
                metadata={"decided_by": "level_guard", "agent_level": agent_level},
            )
            for tc in calls
        ]
        return {
            "pre_decided_results": merge_pre_decided(
                ctx.state.get("pre_decided_results"), entries
            )
        }
