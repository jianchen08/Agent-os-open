"""审批策略与审批服务获取回归测试。

覆盖修复点：

1. isolation_policy.yaml 成为工具安全分类的单一事实源（任务1）：
   - task_submit / memory / state_update 等 host_direct 工具：HOST 模式免审批
   - bash_execute 等 command_in_container 工具：HOST 降级时需审批
   - IsolationGuard 构建 ApprovalContext 时必须传入 policy（否则第 1 层哑火）
   - SecurityCheckPlugin 以 policy.execution 判定，不再依赖硬编码工具名集合

2. 危险工具审批走全局单例而非 ctx._services（任务2）：
   - human_interaction_service 未注入 ctx._services 时（如 websocket channel），
     HOST 模式危险工具仍能触发审批弹窗，而非直接拒绝
   - 用户拒绝/取消/超时 → 未批准（blocked），而非服务获取失败

涉及模块：src/isolation/approval.py, src/plugins/input/isolation_guard/plugin.py,
         src/plugins/input/security_check/plugin.py
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from isolation.approval import (
    ApprovalContext,
    ApprovalDecisionEngine,
    classify_tool_safety,
)
from isolation.policy import IsolationPolicyLoader, ToolIsolationPolicy
from isolation.types import IsolationLevel
from pipeline.plugin import PluginContext


# ═══════════════════════════════════════════════════════════════
# 辅助：构造带指定 execution 的 policy
# ═══════════════════════════════════════════════════════════════


def _policy(execution: str, isolation: IsolationLevel = IsolationLevel.HOST) -> ToolIsolationPolicy:
    """构造测试用 ToolIsolationPolicy。"""
    return ToolIsolationPolicy(
        isolation=isolation,
        execution=execution,
    )


def _ctx():
    """最小 PluginContext（无 services）。"""
    return PluginContext(state={}, _services={})


# ═══════════════════════════════════════════════════════════════
# P0: ApprovalDecisionEngine — 以 policy.execution 为单一事实源
# ═══════════════════════════════════════════════════════════════


class TestApprovalPolicySource:
    """P0: decide() 必须依据 policy.execution 分类，而非硬编码工具名。"""

    @pytest.mark.asyncio
    async def test_host_direct_tool_auto_approved(self):
        """host_direct 工具（task_submit 等）HOST 模式 → 免审批。"""
        engine = ApprovalDecisionEngine()
        ctx = ApprovalContext(
            tool_name="task_submit",
            isolation_level=IsolationLevel.HOST,
            policy=_policy("host_direct"),
        )

        decision = await engine.decide(ctx)

        assert decision.requires_approval is False
        assert decision.decision_type == "AUTO_APPROVED"
        assert "HOST_DIRECT_TOOL" in decision.risk_factors

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "tool_name",
        [
            "task_submit",
            "state_update",
            "resource_merge",
            "trigger_setup",
            "human_interaction",
            "memory",
            "task_manage",
        ],
    )
    async def test_all_host_internal_tools_auto_approved(self, tool_name):
        """所有 isolation_policy.yaml 中 host_direct 工具均免审批。

        这正是 bug 修复的核心契约：这些工具此前因不在硬编码白名单被误判
        为 unknown 并拒绝。
        """
        engine = ApprovalDecisionEngine()
        ctx = ApprovalContext(
            tool_name=tool_name,
            isolation_level=IsolationLevel.HOST,
            policy=_policy("host_direct"),
        )

        decision = await engine.decide(ctx)

        assert decision.requires_approval is False, (
            f"{tool_name} 是 host_direct 内部工具，应免审批，"
            f"但决策为 {decision.decision_type}"
        )

    @pytest.mark.asyncio
    async def test_command_in_container_tool_needs_approval(self):
        """command_in_container 工具降级到 HOST → 必须审批。"""
        engine = ApprovalDecisionEngine()
        ctx = ApprovalContext(
            tool_name="bash_execute",
            inputs={"command": "ls -la"},
            isolation_level=IsolationLevel.HOST,
            policy=_policy("command_in_container"),
        )

        decision = await engine.decide(ctx)

        assert decision.requires_approval is True
        assert decision.decision_type == "NEEDS_APPROVAL"
        assert decision.risk_score >= 0.9

    @pytest.mark.asyncio
    async def test_policy_approval_true_overrides_execution(self):
        """policy.approval=True → 第 1 层直接拦截，优先级最高。"""
        engine = ApprovalDecisionEngine()
        ctx = ApprovalContext(
            tool_name="task_submit",
            isolation_level=IsolationLevel.HOST,
            policy=ToolIsolationPolicy(
                isolation=IsolationLevel.HOST,
                execution="host_direct",
                approval=True,
            ),
        )

        decision = await engine.decide(ctx)

        assert decision.requires_approval is True
        assert "POLICY_APPROVAL" in decision.risk_factors

    @pytest.mark.asyncio
    async def test_non_host_mode_auto_approved(self):
        """非 HOST 模式（容器内）→ 自动批准，无需审批。"""
        engine = ApprovalDecisionEngine()
        ctx = ApprovalContext(
            tool_name="bash_execute",
            isolation_level=IsolationLevel.CONTAINER,
            policy=_policy("command_in_container", isolation=IsolationLevel.CONTAINER),
        )

        decision = await engine.decide(ctx)

        assert decision.requires_approval is False
        assert decision.decision_type == "AUTO_APPROVED"

    @pytest.mark.asyncio
    async def test_task_submit_no_longer_treated_as_unknown(self):
        """回归契约：task_submit 不再走"未知工具安全优先"路径。

        修复前日志: HOST 未知工具需要审批（安全优先）
        修复后日志: HOST 内部工具免审批
        """
        engine = ApprovalDecisionEngine()
        ctx = ApprovalContext(
            tool_name="task_submit",
            isolation_level=IsolationLevel.HOST,
            policy=_policy("host_direct"),
        )

        decision = await engine.decide(ctx)

        # 关键断言：决策原因不含"未知"，且免审批
        assert "未知" not in decision.reason
        assert "unknown" not in decision.details.get("tool_safety", "")
        assert decision.requires_approval is False


# ═══════════════════════════════════════════════════════════════
# P0: classify_tool_safety — 兜底保留但不再误导
# ═══════════════════════════════════════════════════════════════


class TestClassifyToolSafetyFallback:
    """classify_tool_safety 仅作兜底，decide() 不再依赖它作主路径。"""

    def test_task_submit_classified_unknown_but_still_approved(self):
        """classify_tool_safety 对 task_submit 返回 unknown（兜底语义），
        但这不应影响 decide()——后者以 policy.execution 为准。"""
        assert classify_tool_safety("task_submit") == "unknown"

    def test_known_safe_tool_still_safe(self):
        """白名单工具仍返回 safe（向后兼容）。"""
        assert classify_tool_safety("file_read") == "safe"

    def test_known_dangerous_tool_still_dangerous(self):
        """黑名单工具仍返回 dangerous（向后兼容）。"""
        assert classify_tool_safety("bash_execute") == "dangerous"


# ═══════════════════════════════════════════════════════════════
# P0: IsolationGuard — 端到端：host_direct 工具不被 blocked
# ═══════════════════════════════════════════════════════════════


def _make_guard_with_policy(docker_available=False, force_host=False, tools=None):
    """创建 IsolationGuard，注入可控隔离策略。

    Args:
        tools: 自定义工具策略字典 {tool_name: ToolIsolationPolicy}。
               不传则用真实 isolation_policy.yaml。
    """
    from plugins.input.isolation_guard.plugin import IsolationGuard
    from isolation.decider import IsolationDecider

    guard = IsolationGuard(config={
        "docker_available": docker_available,
        "force_host": force_host,
    })
    if tools:
        # 注入自定义策略（不依赖 yaml 加载）
        loader = IsolationPolicyLoader(config_path="/nonexistent/policy.yaml")
        loader._default = ToolIsolationPolicy(
            isolation=IsolationLevel.CONTAINER,
        )
        loader._tools = dict(tools)
    else:
        loader = IsolationPolicyLoader()
    guard._decider = IsolationDecider(policy_loader=loader)
    return guard


class TestGuardHostDirectNotBlocked:
    """P0: host_direct 工具经完整审批链路后不被 blocked。

    这是用户报告的 bug 的端到端验证：
    task_submit → isolation_guard 审批 → human_interaction 不可用 →
    修复前: 默认拒绝 → blocked=True
    修复后: 免审批 → 不 blocked
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "tool_name",
        ["task_submit", "memory", "state_update", "resource_merge"],
    )
    async def test_host_direct_tool_passes_approval(self, tool_name):
        """host_direct 工具在 security_check 中免审批，直接放行。

        重构后审批由 security_check 负责：host_direct 工具（非危险工具）→ 放行。
        """
        from plugins.input.security_check.plugin import SecurityCheckPlugin

        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": []})
        ctx = PluginContext(
            state={
                "core_type": "tool_execute",
                "raw_tool_calls": [{"name": tool_name, "args": {}}],
                "execution_contexts": [{"provider": "host", "tool_name": tool_name}],
            },
            _services={},
        )

        result = await plugin.execute(ctx)
        decision = result.state_updates.get("security.decision", {})
        assert decision.get("allowed") is True, (
            f"{tool_name} 是 host_direct 内部工具，不应被拦截"
        )

    @pytest.mark.asyncio
    async def test_command_in_container_degraded_to_host_blocked_without_service(self, monkeypatch):
        """command_in_container 工具降级到 host + 无审批服务 → soft_block。

        重构后：审批拒绝/异常时走 _soft_block（反馈给 LLM）。
        这里 mock 全局单例抛异常模拟"服务不可用"。
        """
        # mock 全局单例抛异常 → 走 _soft_block 路径
        mock_svc = MagicMock()
        mock_svc.create_choice_request = AsyncMock(side_effect=RuntimeError("service unavailable"))
        import human_interaction
        monkeypatch.setattr(
            human_interaction, "get_human_interaction_service", lambda: mock_svc,
        )

        from plugins.input.security_check.plugin import SecurityCheckPlugin

        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": []})
        ctx = PluginContext(
            state={
                "core_type": "tool_execute",
                "raw_tool_calls": [{"name": "bash_execute", "args": {"command": "ls"}}],
                "execution_contexts": [{"provider": "host", "tool_name": "bash_execute"}],
            },
            _services={},
        )

        result = await plugin.execute(ctx)

        # 异常被 _await_approval 的 except 捕获 → _soft_block
        tool_results = result.state_updates.get("tool_results", [])
        assert len(tool_results) >= 1
        assert tool_results[0]["success"] is False


# ═══════════════════════════════════════════════════════════════
# P0: 危险工具审批走全局单例，不依赖 ctx._services 注入（任务2）
# ═══════════════════════════════════════════════════════════════


def _mock_human_svc(response_type: str = "approved"):
    """构造 mock 的 HumanInteractionService。

    Args:
        response_type: wait_for_choice 返回的 response_type
                       ("approved" / "denied" / 抛 InteractionDeniedError 等)
    """
    from human_interaction.service import InteractionDeniedError

    svc = MagicMock()
    svc.create_choice_request = AsyncMock(return_value="req-123")

    if response_type == "denied_exception":
        # 模拟用户拒绝：wait_for_choice 抛 InteractionDeniedError
        svc.wait_for_choice = AsyncMock(
            side_effect=InteractionDeniedError("req-123", "user said no")
        )
    else:
        svc.wait_for_choice = AsyncMock(
            return_value={"response_type": response_type, "request_id": "req-123"}
        )
    return svc


class TestApprovalUsesGlobalService:
    """P0: 危险工具审批必须走全局单例，即使 ctx._services 未注入服务。

    修复前: isolation_guard/security_check 用 ctx.get_service 取服务，
            websocket 等环境未注入 → 取不到 → 直接拒绝（不弹审批）。
    修复后: 用 get_human_interaction_service() 全局单例，
            无论 ctx._services 是否注入都能正常弹审批。
    """

    @pytest.mark.asyncio
    async def test_isolation_guard_approval_even_without_service_injected(
        self, monkeypatch,
    ):
        """ctx._services 为空（websocket 场景）+ 危险工具 → 仍弹审批。

        重构后审批由 security_check 负责。关键断言：
        create_choice_request 被调用（审批弹窗已发起）。
        """
        mock_svc = _mock_human_svc("approved")
        import human_interaction
        monkeypatch.setattr(
            human_interaction, "get_human_interaction_service", lambda: mock_svc,
        )

        from plugins.input.security_check.plugin import SecurityCheckPlugin

        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": []})
        ctx = PluginContext(
            state={
                "core_type": "tool_execute",
                "raw_tool_calls": [{"name": "bash_execute", "args": {"command": "ls"}}],
                "execution_contexts": [{"provider": "host", "tool_name": "bash_execute"}],
            },
            _services={},
        )

        result = await plugin.execute(ctx)

        # 核心契约：审批被发起（而非直接拒绝）
        mock_svc.create_choice_request.assert_awaited_once()
        # 用户批准 → allowed=True
        decision = result.state_updates.get("security.decision", {})
        assert decision.get("allowed") is True

    @pytest.mark.asyncio
    async def test_isolation_guard_user_denial_blocks_via_global_service(
        self, monkeypatch,
    ):
        """用户拒绝审批 → soft_block（反馈给 LLM），且审批确实被发起。"""
        mock_svc = _mock_human_svc("denied_exception")
        import human_interaction
        monkeypatch.setattr(
            human_interaction, "get_human_interaction_service", lambda: mock_svc,
        )

        from plugins.input.security_check.plugin import SecurityCheckPlugin

        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": []})
        ctx = PluginContext(
            state={
                "core_type": "tool_execute",
                "raw_tool_calls": [{"name": "bash_execute", "args": {"command": "ls"}}],
                "execution_contexts": [{"provider": "host", "tool_name": "bash_execute"}],
            },
            _services={},
        )

        result = await plugin.execute(ctx)

        # 审批被发起，用户拒绝 → soft_block
        mock_svc.create_choice_request.assert_awaited_once()
        tool_results = result.state_updates.get("tool_results", [])
        assert len(tool_results) >= 1
        assert tool_results[0]["success"] is False

    @pytest.mark.asyncio
    async def test_security_check_uses_global_service(self, monkeypatch):
        """security_check 走全局单例，不依赖 ctx.get_service。"""
        mock_svc = _mock_human_svc("approved")
        import human_interaction
        monkeypatch.setattr(
            human_interaction, "get_human_interaction_service", lambda: mock_svc,
        )

        from plugins.input.security_check.plugin import SecurityCheckPlugin

        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": []})
        ctx = PluginContext(
            state={
                "core_type": "tool_execute",
                "raw_tool_calls": [{"name": "bash_execute", "args": {"command": "ls"}}],
                "execution_contexts": [{"provider": "host", "tool_name": "bash_execute"}],
            },
            _services={},
        )

        result = await plugin.execute(ctx)

        # 审批被发起，用户批准 → allowed
        mock_svc.create_choice_request.assert_awaited_once()
        decision = result.state_updates.get("security.decision", {})
        assert decision.get("allowed") is True

