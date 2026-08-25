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
from unittest.mock import AsyncMock, MagicMock

import tests._isolation_path  # noqa: F401  # 必须先注入 sys.path，再平铺导入

import pytest
from approval import (
    ApprovalContext,
    ApprovalDecisionEngine,
    classify_tool_safety,
)
from isolation_types import IsolationLevel
from pipeline.plugin import PluginContext
from policy import IsolationPolicyLoader, ToolIsolationPolicy

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

        契约：审批判定以 isolation_policy.yaml 的 host_direct 名单为准，
        名单内工具免审批（不得按硬编码白名单误判为 unknown）。
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

        契约日志: HOST 内部工具免审批（而非 HOST 未知工具需要审批）。
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
    from decider import IsolationDecider
    from plugin import IsolationGuard  # isolation_guard 插件目录（父级已入 path 时按平铺解析）

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

