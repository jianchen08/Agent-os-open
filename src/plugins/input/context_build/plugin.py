"""上下文构建 Input 插件 — 从旧代码 agents/context.py 迁移。

负责在管道循环的输入阶段构建上下文信息，
将 agent 配置、层级信息、会话元数据等写入 state，
供后续插件（prompt_build、knowledge_inject 等）和 Core 读取。

M6a 阶段：从 AgentContext 的依赖注入模式迁移为插件模式，
核心逻辑是组装管道执行所需的上下文字段到 state 中。

State 命名空间：
    - context.* : 本插件写入的上下文字段
"""

from __future__ import annotations

import logging
from typing import Any

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import ErrorPolicy, StateKeys

logger = logging.getLogger(__name__)


class ContextBuildPlugin(IInputPlugin):
    """上下文构建 Input 插件。

    从旧代码 AgentContext 迁移而来。将 agent 配置、层级信息、
    会话元数据等组装为管道执行所需的上下文字段，写入 state。

    旧代码中 AgentContext 是一个大的依赖注入容器，包含协调器、
    服务、配置等。迁移后，本插件只负责构建"上下文数据"本身，
    不负责管理服务实例（服务通过 PluginContext.get_service 获取）。

    优先级：10（准备级，先于其他 Input 插件执行）
    错误策略：FALLBACK（最小上下文也能跑）

    Attributes:
        _config: 插件配置字典
        _system_prompt: 系统 prompt 模板
        _agent_name: Agent 名称
        _agent_level: Agent 层级
    """

    error_policy = ErrorPolicy.ABORT

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化上下文构建插件。

        Args:
            config: 插件配置字典，支持以下键：
                - system_prompt: 系统 prompt 模板
                - agent_name: Agent 名称
                - agent_level: Agent 层级 (l1_main/l2_subtask/l3_atomic)
                - extra_context: 额外上下文字典
        """
        self._config = config or {}
        self._system_prompt = self._config.get("system_prompt", "")
        self._agent_name = self._config.get("agent_name", "")
        self._agent_level = self._config.get("agent_level", "L1")
        self._extra_context = self._config.get("extra_context", {})

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "context_build"

    @property
    def priority(self) -> int:
        """插件执行优先级，数值越小越先执行。"""
        return self._config.get("priority", 10)

    async def execute(self, ctx: PluginContext) -> PluginResult:
        """构建上下文信息并写入 state。

        从 state 读取已有的 session_id、task_id 等字段，
        结合插件配置中的 system_prompt、agent_name 等，
        组装为完整的上下文字段写入 state。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含上下文状态更新的插件执行结果
        """
        result = await self._do_work(ctx)
        return PluginResult(state_updates=result)

    async def _do_work(self, ctx: PluginContext) -> dict[str, Any]:
        """执行上下文构建逻辑。

        Args:
            ctx: 插件执行上下文

        Returns:
            要写入 state 的上下文字段字典
        """
        updates: dict[str, Any] = {}

        # 1. 系统提示词（优先 state 注入，回退到插件配置）
        updates["context.system_prompt"] = ctx.state.get("system_prompt", "") or self._system_prompt

        # 2. Agent 身份信息
        updates["context.agent_name"] = self._agent_name
        updates["context.agent_level"] = self._agent_level

        # 始终用实际 Agent 层级覆盖 state 中的 AGENT_LEVEL，
        # 防止子管道继承父管道的层级（如 L2 agent 错误继承 L1）。
        updates[StateKeys.AGENT_LEVEL] = self._agent_level

        # 3. 会话元数据
        session_id = ctx.state.get(StateKeys.SESSION_ID, "")
        task_id = ctx.state.get(StateKeys.TASK_ID, "")
        updates["context.session_id"] = session_id
        updates["context.task_id"] = task_id

        # 4. 迭代信息
        iteration = ctx.state.get(StateKeys.ITERATION, 0)
        updates["context.iteration"] = iteration

        # 5. 额外上下文
        if self._extra_context:
            for key, value in self._extra_context.items():
                updates[f"context.{key}"] = value

        # 6. 工具执行标记（从 core_type 推断）
        core_type = ctx.state.get(StateKeys.CORE_TYPE, "llm_call")
        updates["context.is_tool_execution"] = core_type == "tool_execute"

        # 7. 项目级标记
        updates["context.is_project"] = self._agent_level == "L1"

        logger.debug(
            "[%s] Context built | agent=%s | level=%s | iteration=%d",
            self.name,
            self._agent_name,
            self._agent_level,
            iteration,
        )

        return updates
