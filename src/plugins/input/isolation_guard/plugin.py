"""隔离环境守卫 Input 插件。

在工具执行前根据安全策略决定是否在容器内执行。
优先使用 IsolationDecider 从 isolation_policy.yaml 决策隔离级别，
task metadata 可覆盖决策结果。

职责边界（SRP）：
- 只管"执行环境"决策（container / host / denied）
- 不做审批（审批归 security_check 插件）
- 不写 security.decision（仅 security_check 写）
- blocked 信号通过 isolation.blocked 表达

State 命名空间：
    - execution_contexts : 各工具调用的执行上下文列表
    - isolation.blocked   : 被策略阻止时设置（供路由拦截）
"""

from __future__ import annotations

import logging
from typing import Any

from isolation.decider import IsolationDecider
from isolation.types import IsolationLevel
from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import ErrorPolicy, StateKeys

logger = logging.getLogger(__name__)


class IsolationGuard(IInputPlugin):
    """隔离环境守卫 Input 插件。

    根据工具类型和配置的安全策略，决定每个工具调用
    应在何种隔离级别下执行（docker 或 host）。

    决策优先级：
    1. task metadata 中的 isolation_level 覆盖
    2. IsolationDecider 基于 isolation_policy.yaml 策略决策
    3. Docker 不可用时根据策略的 fallback 字段降级

    优先级：40（在 level_guard 之后，security_check 之前）
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
        # Docker 可用性：优先用配置，未配置则同步检测（避免永远默认 False）
        if "docker_available" in self._config:
            self._docker_available = self._config["docker_available"]
        else:
            # 启动时真正检测 Docker，不依赖外部注入
            self._docker_available = self._detect_docker()
        if not self._docker_available:
            logger.warning(
                "[%s] docker_available=False, tool isolation will be degraded to host execution",
                self.name,
            )
        self._force_host = self._config.get("force_host", False)
        self._decider = IsolationDecider()
        self._enabled_by_agent: bool = True

    @staticmethod
    def _detect_docker() -> bool:
        """同步检测 Docker 是否可用（CLI 存在 + daemon 运行）。

        用 subprocess.run 替代 asyncio subprocess，避免 Windows 静默失败。
        """
        import shutil
        import subprocess
        if not shutil.which("docker"):
            return False
        try:
            # 用 docker version 替代 docker info（info 在某些 Docker Desktop 配置下会卡 stdin）
            result = subprocess.run(
                ["docker", "version", "--format", "{{.Server.Version}}"],
                capture_output=True,
                timeout=15,
            )
            return result.returncode == 0
        except Exception:
            return False

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "isolation_guard"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return self._config.get("priority", 40)

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

        state_updates: dict[str, Any] = {
            "execution_contexts": execution_contexts,
        }

        # 被策略阻止的工具写入 isolation.blocked，供路由拦截
        blocked_tools = [c for c in execution_contexts if c.get("blocked")]
        if blocked_tools:
            tool_names = ", ".join(c["tool_name"] for c in blocked_tools)
            state_updates["isolation.blocked"] = True
            state_updates["isolation.block_reason"] = f"隔离策略阻止: {tool_names}"
            logger.warning(
                "[IsolationGuard] 阻止工具执行 | tools=%s",
                tool_names,
            )

        return PluginResult(state_updates=state_updates)

    def _decide_isolation(self, tool_name: str, ctx: PluginContext) -> dict[str, Any]:
        """决定工具的隔离级别。

        规则（按优先级）：
        1. 先查工具级 policy（isolation_policy.yaml）确定工具的隔离能力
        2. task metadata 的 isolation_level 只允许降级（container→host），
           不允许提升（host→container），避免把不支持容器的工具塞进容器
        3. Docker 不可用时根据策略 fallback 降级

        Args:
            tool_name: 工具名称
            ctx: 插件执行上下文

        Returns:
            执行上下文字典，包含 provider、level、tool_name、workspace 等信息
        """
        task_metadata = self._get_task_metadata(ctx)
        metadata_isolation = task_metadata.get("isolation_level")
        metadata_workspace = task_metadata.get("workspace")

        # 先解析工具级 policy，作为决策基础
        policy = self._decider.resolve(tool_name)
        policy_isolation = policy.isolation

        if self._force_host:
            # P0-安全: force_host 不能绕过 fallback:deny 策略
            if policy.fallback == "deny" and policy_isolation == IsolationLevel.CONTAINER:
                logger.warning(
                    "[IsolationGuard] force_host 被策略阻止: "
                    "工具 %s 要求容器隔离且禁止降级 | tool=%s",
                    tool_name, tool_name,
                )
                return self._build_context(
                    tool_name, "denied", "force_host_denied_by_policy",
                    workspace=metadata_workspace,
                    blocked=True,
                )
            return self._build_context(
                tool_name, "host", "force_host",
                workspace=metadata_workspace,
            )

        # ── metadata 覆盖：只允许降级，不允许提升 ──
        # policy 是 host 的工具（如 file_write/task_submit），即使 metadata
        # 要求 container 也不路由到 docker（容器内没有工具代码，会报
        # "[isolated] tool=xxx not supported in container"）。
        if metadata_isolation and policy_isolation == IsolationLevel.CONTAINER:
            # policy 允许容器的工具（如 bash_execute），metadata 可控制实际级别
            if metadata_isolation == "container":
                if self._docker_available:
                    return self._build_context(
                        tool_name, "docker", "task_metadata",
                        workspace=metadata_workspace,
                    )
                # Docker 不可用时按 fallback 决策
                if policy.fallback == "deny":
                    logger.warning(
                        "[IsolationGuard] metadata 要求容器但 Docker 不可用，"
                        "策略禁止降级 | tool=%s",
                        tool_name,
                    )
                    return self._build_context(
                        tool_name, "denied", "task_metadata_fallback_denied",
                        workspace=metadata_workspace,
                        blocked=True,
                    )
                logger.info(
                    "[IsolationGuard] metadata 要求容器但 Docker 不可用，"
                    "策略允许降级 | tool=%s",
                    tool_name,
                )
                return self._build_context(
                    tool_name, "host", "task_metadata_fallback",
                    workspace=metadata_workspace,
                )
            # metadata 强制 host → 降级
            return self._build_context(
                tool_name, "host", "task_metadata_downgrade",
                workspace=metadata_workspace,
            )

        # ── 工具级 policy 决策（metadata 不适用或 policy 为 host）──
        if policy_isolation == IsolationLevel.CONTAINER and self._docker_available:
            return self._build_context(
                tool_name, "docker", "policy",
                workspace=metadata_workspace,
            )

        if policy_isolation == IsolationLevel.CONTAINER and not self._docker_available:
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
                "[IsolationGuard] Docker 不可用且策略禁止降级，阻止执行 | tool=%s",
                tool_name,
            )
            return self._build_context(
                tool_name, "denied", "policy_fallback_denied",
                workspace=metadata_workspace,
                blocked=True,
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
        blocked: bool = False,
    ) -> dict[str, Any]:
        """构建执行上下文字典。

        Args:
            tool_name: 工具名称
            provider: 执行提供者（docker / host / denied）
            reason: 决策原因
            workspace: 工作目录路径
            blocked: 是否被策略阻止执行

        Returns:
            执行上下文字典
        """
        context: dict[str, Any] = {
            "tool_name": tool_name,
            "provider": provider,
            "level": "denied" if blocked else ("container" if provider == "docker" else "host"),
            "reason": reason,
        }
        if blocked:
            context["blocked"] = True
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
