"""隔离环境守卫 Input 插件。

在工具执行前根据安全策略决定是否在容器内执行。
优先使用 IsolationDecider 从 isolation_policy.yaml 决策隔离级别，
task metadata 可覆盖决策结果。

State 命名空间：
    - execution_contexts : 各工具调用的执行上下文列表
"""

from __future__ import annotations

import logging
from typing import Any

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import ErrorPolicy, StateKeys
from src.isolation.decider import IsolationDecider
from src.isolation.policy import IsolationPolicyLoader

logger = logging.getLogger(__name__)


class IsolationGuard(IInputPlugin):
    """隔离环境守卫 Input 插件。

    根据工具类型和配置的安全策略，决定每个工具调用
    应在何种隔离级别下执行（docker 或 host）。

    决策优先级：
    1. task metadata 中的 isolation_level 覆盖
    2. IsolationDecider 基于 isolation_policy.yaml 策略决策
    3. Docker 不可用时根据策略的 fallback 字段降级

    优先级：25（在参数注入之前，尽早决定执行环境）
    错误策略：SKIP（隔离决策失败不应阻断管道）
    """

    error_policy = ErrorPolicy.SKIP

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化隔离环境守卫插件。

        Args:
            config: 插件配置字典，支持以下键：
                - enabled: 是否启用隔离守卫（默认 True）
                - docker_available: Docker 是否可用（默认 False）
                - force_host: 强制所有工具在 host 执行（默认 False）
        """
        self._config = config or {}
        self._enabled = self._config.get("enabled", True)
        self._docker_available = self._config.get("docker_available", False)
        if not self._docker_available:
            logger.warning(
                "[%s] docker_available=False, tool isolation will be degraded to host execution",
                self.name,
            )
        self._force_host = self._config.get("force_host", False)
        self._decider = IsolationDecider()
        self._enabled_by_agent: bool = True

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "isolation_guard"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return self._config.get("priority", 25)

    async def execute(self, ctx: PluginContext) -> PluginResult:
        """执行隔离环境决策。

        遍历当前管道状态中的工具调用列表，
        为每个工具调用决定隔离级别和执行上下文。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含 execution_contexts 状态更新的插件执行结果
        """
        self._apply_runtime_config(ctx)

        if not self._enabled or not self._enabled_by_agent:
            return PluginResult()

        state = ctx.state
        core_type = state.get(StateKeys.CORE_TYPE, "llm_call")

        if core_type != "tool_execute":
            return PluginResult()

        tool_calls = state.get(StateKeys.RAW_TOOL_CALLS, [])
        if not tool_calls:
            return PluginResult()

        execution_contexts = []
        for tc in tool_calls:
            tool_name = tc.get("name", "")
            context = self._decide_isolation(tool_name, ctx)
            execution_contexts.append(context)

        return PluginResult(state_updates={
            "execution_contexts": execution_contexts,
        })

    def _decide_isolation(self, tool_name: str, ctx: PluginContext) -> dict[str, Any]:
        """决定工具的隔离级别。

        规则（按优先级）：
        1. 从 task metadata 读取 isolation_level 覆盖决策
        2. 从 task metadata 读取 workspace 传入上下文
        3. 使用 IsolationDecider 基于 isolation_policy.yaml 决策
        4. Docker 不可用时根据策略 fallback 降级

        Args:
            tool_name: 工具名称
            ctx: 插件执行上下文

        Returns:
            执行上下文字典，包含 provider、level、tool_name、workspace 等信息
        """
        task_metadata = self._get_task_metadata(ctx)
        metadata_isolation = task_metadata.get("isolation_level")
        metadata_workspace = task_metadata.get("workspace")

        if self._force_host:
            return self._build_context(
                tool_name, "host", "force_host",
                workspace=metadata_workspace,
            )

        if metadata_isolation:
            if metadata_isolation == "container" and self._docker_available:
                return self._build_context(
                    tool_name, "docker", "task_metadata",
                    workspace=metadata_workspace,
                )
            if metadata_isolation == "container" and not self._docker_available:
                logger.info(
                    "[IsolationGuard] metadata 要求容器但 Docker 不可用，回退 host | tool=%s",
                    tool_name,
                )
                return self._build_context(
                    tool_name, "host", "task_metadata_fallback",
                    workspace=metadata_workspace,
                )
            if metadata_isolation == "host":
                return self._build_context(
                    tool_name, "host", "task_metadata",
                    workspace=metadata_workspace,
                )

        policy = self._decider.resolve(tool_name)
        isolation = policy.isolation

        if isolation.value == "container" and self._docker_available:
            return self._build_context(
                tool_name, "docker", "policy",
                workspace=metadata_workspace,
            )

        if isolation.value == "container" and not self._docker_available:
            if policy.fallback == "allow":
                logger.info(
                    "[IsolationGuard] Docker 不可用，策略允许降级 | tool=%s",
                    tool_name,
                )
                return self._build_context(
                    tool_name, "host", "policy_fallback",
                    workspace=metadata_workspace,
                )
            logger.warning(
                "[IsolationGuard] Docker 不可用且策略禁止降级 | tool=%s",
                tool_name,
            )
            return self._build_context(
                tool_name, "host", "policy_fallback_forced",
                workspace=metadata_workspace,
            )

        return self._build_context(
            tool_name, "host", "policy",
            workspace=metadata_workspace,
        )

    def _apply_runtime_config(self, ctx: PluginContext) -> None:
        """从 ctx.state 读取 Agent 覆盖的运行时配置。

        Args:
            ctx: 插件执行上下文
        """
        from pipeline.plugin import find_plugin_config

        plugin_configs = ctx.state.get("plugin_configs", {})
        config = find_plugin_config("isolation_guard", plugin_configs)

        if not config.get("enabled", True):
            self._enabled_by_agent = False
            return

        self._enabled_by_agent = True
        if "docker_available" in config:
            self._docker_available = config["docker_available"]
        if "force_host" in config:
            self._force_host = config["force_host"]

    def _build_context(
        self, tool_name: str, provider: str, reason: str,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """构建执行上下文字典。

        Args:
            tool_name: 工具名称
            provider: 执行提供者（docker / host）
            reason: 决策原因
            workspace: 工作目录路径

        Returns:
            执行上下文字典
        """
        context = {
            "tool_name": tool_name,
            "provider": provider,
            "level": "container" if provider == "docker" else "host",
            "reason": reason,
        }
        if workspace:
            context["workspace"] = workspace
        return context

    def _get_task_metadata(self, ctx: PluginContext) -> dict[str, Any]:
        """从 ctx.state 中获取当前 task 的 metadata。

        Args:
            ctx: 插件执行上下文

        Returns:
            task 的 metadata 字典，未找到时返回空字典
        """
        task_id = ctx.state.get(StateKeys.TASK_ID)
        if not task_id:
            return {}

        try:
            task_service = ctx.get_service("task_service")
        except KeyError:
            return {}

        try:
            task = task_service.get_task(task_id)
            if task and task.metadata:
                return task.metadata
        except Exception as e:
            logger.debug(
                "[IsolationGuard] 读取 task metadata 失败 | task_id=%s | error=%s",
                task_id, e,
            )
        return {}
