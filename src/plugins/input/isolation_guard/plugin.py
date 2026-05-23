"""隔离环境守卫 Input 插件。

在工具执行前根据安全策略决定是否在容器内执行。
优先使用 IsolationDecider 从 isolation_policy.yaml 决策隔离级别，
task metadata 可覆盖决策结果。

HOST 模式安全审批：
- 决策为 host 执行后，调用 ApprovalDecisionEngine 判断是否需要审批
- 危险工具（file_write、bash_execute 等）弹出 human_interaction 让用户确认
- 只读工具（file_read、搜索等）免审批白名单
- 用户拒绝则标记 blocked=True，阻断执行

State 命名空间：
    - execution_contexts : 各工具调用的执行上下文列表
    - security.decision  : 安全决策（被阻止时设置）
"""

from __future__ import annotations

import logging
from typing import Any

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import ErrorPolicy, StateKeys
from isolation.decider import IsolationDecider
from isolation.approval import ApprovalDecisionEngine, ApprovalContext
from isolation.types import IsolationLevel

logger = logging.getLogger(__name__)


class IsolationGuard(IInputPlugin):
    """隔离环境守卫 Input 插件。

    根据工具类型和配置的安全策略，决定每个工具调用
    应在何种隔离级别下执行（docker 或 host）。

    决策优先级：
    1. task metadata 中的 isolation_level 覆盖
    2. IsolationDecider 基于 isolation_policy.yaml 策略决策
    3. Docker 不可用时根据策略的 fallback 字段降级

    HOST 模式审批：
    4. 决策为 host 后，ApprovalDecisionEngine 判断是否需要用户审批
    5. 危险工具 → human_interaction 请求用户确认
    6. 用户拒绝 → blocked=True

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
        self._approval_engine = ApprovalDecisionEngine()
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
        为每个工具调用决定隔离级别和执行上下文，
        并在 HOST 模式下进行安全审批检查。

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
            tool_args = tc.get("args", tc.get("arguments", {}))
            context = await self._decide_isolation_with_approval(
                tool_name, tool_args, ctx
            )
            execution_contexts.append(context)

        state_updates: dict[str, Any] = {
            "execution_contexts": execution_contexts,
        }

        # BUG-FIX-fix_20260506_bash_security: 检测被策略阻止的工具调用
        # 问题根因: fallback:deny 时仍降级到 host 执行，仅记录 warning 日志
        # 修复方案: 被阻止时设置 security.decision 为 blocked，复用现有安全机制阻断执行
        # 影响范围: bash_execute 等配置了 fallback:deny 的工具
        blocked_tools = [c for c in execution_contexts if c.get("blocked")]
        if blocked_tools:
            tool_names = ", ".join(c["tool_name"] for c in blocked_tools)
            state_updates["security.decision"] = {
                "allowed": False,
                "reason": f"隔离策略阻止或审批被拒绝: {tool_names}",
                "tool": blocked_tools[0]["tool_name"],
            }
            logger.warning(
                "[IsolationGuard] 阻止工具执行 | tools=%s",
                tool_names,
            )

        return PluginResult(state_updates=state_updates)

    async def _decide_isolation_with_approval(
        self,
        tool_name: str,
        tool_args: dict[str, Any] | str,
        ctx: PluginContext,
    ) -> dict[str, Any]:
        """决定工具的隔离级别，并在 HOST 模式下进行安全审批。

        流程：
        1. 调用 _decide_isolation 决定隔离级别
        2. 如果 provider=host，调用 ApprovalDecisionEngine 检查是否需要审批
        3. 如果需要审批，通过 human_interaction_service 请求用户确认
        4. 用户拒绝或审批服务不可用时，按降级策略处理

        Args:
            tool_name: 工具名称
            tool_args: 工具调用参数
            ctx: 插件执行上下文

        Returns:
            执行上下文字典，可能包含 blocked=True
        """
        # 第一步：基础隔离决策
        exec_context = self._decide_isolation(tool_name, ctx)

        # 非 host 模式或已阻止的，直接返回
        if exec_context.get("provider") != "host" or exec_context.get("blocked"):
            return exec_context

        # 第二步：HOST 模式安全审批
        # 构建 tool_args 的标准化形式（可能是 str 或 dict）
        if isinstance(tool_args, str):
            import json
            try:
                tool_args = json.loads(tool_args)
            except (json.JSONDecodeError, TypeError):
                tool_args = {}

        approval_ctx = ApprovalContext(
            tool_name=tool_name,
            inputs=tool_args if isinstance(tool_args, dict) else {},
            isolation_level=IsolationLevel.HOST,
            task_id=ctx.state.get(StateKeys.TASK_ID),
        )

        decision = await self._approval_engine.decide(approval_ctx)

        # 更新上下文中的审批信息
        exec_context["approval_decision"] = decision.decision_type
        exec_context["approval_reason"] = decision.reason
        exec_context["risk_score"] = decision.risk_score

        if not decision.requires_approval:
            logger.debug(
                "[IsolationGuard] HOST 模式工具免审批 | tool=%s | reason=%s",
                tool_name,
                decision.reason,
            )
            return exec_context

        # 第三步：需要审批 → 请求用户确认
        approved = await self._request_user_approval(
            tool_name, tool_args, decision, ctx
        )

        if approved:
            exec_context["approval_status"] = "approved"
            logger.info(
                "[IsolationGuard] 用户已批准 HOST 模式危险工具 | tool=%s",
                tool_name,
            )
            return exec_context
        else:
            exec_context["blocked"] = True
            exec_context["block_reason"] = "user_denied"
            exec_context["approval_status"] = "denied"
            logger.warning(
                "[IsolationGuard] 用户拒绝 HOST 模式危险工具 | tool=%s",
                tool_name,
            )
            return exec_context

    async def _request_user_approval(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        decision: Any,
        ctx: PluginContext,
    ) -> bool:
        """通过 human_interaction_service 请求用户审批。

        兼容 human_interaction_service 不可用的场景：
        - 服务不可用时，按降级策略处理（危险工具默认拒绝）

        Args:
            tool_name: 工具名称
            tool_args: 工具调用参数
            decision: 审批决策
            ctx: 插件执行上下文

        Returns:
            用户是否批准
        """
        try:
            human_svc = ctx.get_service("human_interaction_service")
        except (KeyError, AttributeError):
            human_svc = None

        if human_svc is None:
            logger.warning(
                "[IsolationGuard] human_interaction_service 不可用，"
                "降级策略：危险工具默认拒绝 | tool=%s",
                tool_name,
            )
            return False

        try:
            from human_interaction.models import Priority

            # 构建审批描述
            args_preview = ""
            if isinstance(tool_args, dict):
                cmd = tool_args.get("command") or tool_args.get("content") or tool_args.get("code")
                if cmd:
                    args_preview = str(cmd)[:200]

            description = (
                f"HOST 模式安全审批\n\n"
                f"工具: {tool_name}\n"
                f"风险等级: {decision.risk_score:.1f}\n"
                f"风险因素: {', '.join(decision.risk_factors)}\n"
                f"原因: {decision.reason}"
            )
            if args_preview:
                description += f"\n\n操作内容预览:\n{args_preview}"

            # 确定线程 ID
            task_id = ctx.state.get(StateKeys.TASK_ID) or ""
            session_id = ctx.state.get("session_id")

            request_id = await human_svc.create_choice_request(
                session_id=session_id or task_id,
                thread_id=task_id,
                tab_id="security_approval",
                title=f"安全审批: {tool_name}",
                description=description,
                options=[
                    {"id": "approve", "label": "批准执行", "description": "允许在 HOST 模式下执行此工具", "is_default": True},
                    {"id": "deny", "label": "拒绝执行", "description": "阻止此工具执行", "is_destructive": True},
                ],
                timeout_seconds=120,
                priority=Priority.HIGH if decision.risk_score >= 0.8 else Priority.NORMAL,
                agent_id="isolation_guard",
            )

            response = await human_svc.wait_for_choice(request_id, timeout=120.0)

            return response.get("response_type") == "approved"

        except Exception as e:
            logger.error(
                "[IsolationGuard] 审批请求异常，降级为拒绝 | tool=%s | error=%s",
                tool_name,
                e,
            )
            return False

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
