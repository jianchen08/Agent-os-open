# @feature: FP-0.2.二 | @vision: V2 安全
"""隔离判断单元测试 — security_check 的 _is_isolated 按 task_isolated 判定。

验证隔离判断契约（纯单元测试）：

1. 隔离任务（task_isolated=True）→ 已隔离（放行，所有工具不审批）
2. 非隔离任务（task_isolated=False）→ 未隔离（危险工具需审批）

isolation_level 是隔离的唯一真相源，由 isolation_guard 归一化后注入
每个 execution_context 的 task_isolated 字段。
"""

from __future__ import annotations

import pytest

from tests._pipeline_plugin_path import add_plugin_dir
from tests._security_check_harness import (
    make_tool_ctx,
    unwire_approval_cap,
    wire_approval_cap,
)

add_plugin_dir("input", "security_check")
import plugin as sc_mod  # noqa: E402
from plugin import SecurityCheckPlugin  # noqa: E402


class TestIsIsolated:
    """_is_isolated 按 task_isolated 判定，不看 provider。"""

    def test_isolated_task_passes_all_tools(self) -> None:
        """隔离任务（task_isolated=True）放行所有工具，无论 docker/host。"""
        plugin = SecurityCheckPlugin()
        ctxs = [
            {"tool_name": "bash_execute", "provider": "docker", "task_isolated": True},
            {"tool_name": "delete_file", "provider": "host", "task_isolated": True},
        ]
        assert plugin._is_isolated(ctxs) is True

    def test_non_isolated_task_needs_approval(self) -> None:
        """非隔离任务（task_isolated=False）→ 未隔离，危险工具需审批。"""
        plugin = SecurityCheckPlugin()
        ctxs = [{"tool_name": "bash_execute", "provider": "host", "task_isolated": False}]
        assert plugin._is_isolated(ctxs) is False

    def test_missing_task_isolated_treated_as_not_isolated(self) -> None:
        """context 无 task_isolated 字段 → 保守判为未隔离。"""
        plugin = SecurityCheckPlugin()
        ctxs = [{"tool_name": "bash_execute", "provider": "docker"}]
        assert plugin._is_isolated(ctxs) is False

    def test_empty_execution_contexts_not_isolated(self) -> None:
        """空 execution_contexts → 未隔离（保守）。"""
        plugin = SecurityCheckPlugin()
        assert plugin._is_isolated([]) is False

    def test_mixed_isolation_flags_not_isolated(self) -> None:
        """混合 task_isolated（部分 True 部分 False）→ 未隔离（保守）。"""
        plugin = SecurityCheckPlugin()
        ctxs = [
            {"tool_name": "bash_execute", "provider": "docker", "task_isolated": True},
            {"tool_name": "delete_file", "provider": "host", "task_isolated": False},
        ]
        assert plugin._is_isolated(ctxs) is False


class TestIsolatedSkipsApprovalEndToEnd:
    """行为级：task_isolated 经公开入口 execute 放行，审批通道零发起。

    白盒 TestIsIsolated 钉判定函数；这里钉用户可观察行为——隔离任务的
    危险命令不弹审批直接放行，同命令非隔离/缺字段则必须弹审批。
    """

    @pytest.fixture(autouse=True)
    def _restore_cap(self):
        yield
        unwire_approval_cap(sc_mod)

    @pytest.mark.asyncio
    async def test_isolated_dangerous_command_allowed_without_approval(self) -> None:
        """隔离任务的危险命令 → 放行且审批通道零发起。"""
        _cap, create = wire_approval_cap(sc_mod, [])  # 空 sequence：任何审批尝试即失败

        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": []})
        ctx = make_tool_ctx("bash_execute", {"command": "rm -rf /tmp/a"}, task_isolated=True)

        result = await plugin.execute(ctx)
        decision = result.state_updates.get("security.decision", {})
        assert decision.get("allowed") is True
        assert "isolated" in decision.get("reason", "")
        assert create.calls == 0, "隔离任务不得发起审批"

    @pytest.mark.asyncio
    async def test_non_isolated_same_command_requires_approval(self) -> None:
        """对照：同一命令 task_isolated=False → 必须发起审批。"""
        _cap, create = wire_approval_cap(sc_mod, [{"selected_option": "approved_once"}])

        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": []})
        ctx = make_tool_ctx("bash_execute", {"command": "rm -rf /tmp/a"}, task_isolated=False)

        result = await plugin.execute(ctx)
        assert result.state_updates.get("security.decision", {}).get("allowed") is True
        assert create.calls == 1, "非隔离任务的危险命令必须审批"

    @pytest.mark.asyncio
    async def test_missing_task_isolated_flag_is_conservative(self) -> None:
        """对照：context 缺 task_isolated 字段 → 按未隔离处理（审批兜底）。"""
        _cap, create = wire_approval_cap(sc_mod, [{"selected_option": "approved_once"}])

        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": []})
        ctx = make_tool_ctx("bash_execute", {"command": "rm -rf /tmp/a"})

        result = await plugin.execute(ctx)
        assert result.state_updates.get("security.decision", {}).get("allowed") is True
        assert create.calls == 1, "缺隔离标记必须保守走审批"
