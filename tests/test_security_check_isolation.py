# @feature: FP-0.2.二 | @vision: V2 安全
"""隔离与审批正交性测试 — security_check 的隔离任务审批契约。

验证契约（隔离 ≠ 免审批）：

1. 隔离任务命中参数级黑名单（needs_approval 规则）→ 照常发起审批，
   审批层在隔离任务不缺位（隔离豁免的是环境类检查，不是参数级危险判定）
2. 隔离任务未命中任何规则 → 放行且审批零发起（环境类豁免：隔离边界
   承担执行环境风险，容器内常规操作不弹审批）
3. 非隔离任务 / 缺隔离标记 → 危险命令必须审批（保守对照）

isolation_level 是隔离的唯一真相源，由 isolation_guard 归一化后注入
每个 execution_context 的 task_isolated 字段。
"""

from __future__ import annotations

from typing import Any

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

# 显式守门规则（不触发降级默认规则，判定来源可归因）：
# 两条命令类 needs_approval（区分输入）+ 一条非命令工具的 needs_approval。
_GUARD_RULES: list[dict[str, Any]] = [
    {
        "name": "guard_rm",
        "tools": ["*"],
        "params": ["command", "cmd"],
        "action": "needs_approval",
        "patterns": [{"type": "keyword", "value": "rm -rf"}],
    },
    {
        "name": "guard_sudo",
        "tools": ["*"],
        "params": ["command", "cmd"],
        "action": "needs_approval",
        "patterns": [{"type": "keyword", "value": "sudo "}],
    },
    {
        "name": "guard_publish",
        "tools": ["github_ops"],
        "params": ["action"],
        "action": "needs_approval",
        "patterns": [{"type": "keyword", "value": "comment"}],
    },
]


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


class TestIsolatedBlacklistApproval:
    """行为级：隔离任务命中黑名单照常审批，未命中照常放行（审批零发起）。

    经公开入口 execute 观察用户可感知行为：审批是否发起（create_choice
    次数）、决策是否放行、拒绝是否转为 tool_result 反馈——不断插件内部
    分支细节。
    """

    @pytest.fixture(autouse=True)
    def _restore_cap(self):
        yield
        unwire_approval_cap(sc_mod)

    @pytest.mark.parametrize(
        ("keyword", "command"),
        [
            ("rm -rf", "rm -rf .packtest_probe_dir"),
            ("sudo ", "sudo apt-get install -y curl"),
        ],
        ids=["dangerous_rm", "high_risk_sudo"],
    )
    @pytest.mark.asyncio
    async def test_isolated_blacklist_hit_goes_through_approval(
        self, keyword: str, command: str
    ) -> None:
        """隔离任务 + 黑名单命中 → 必须发起审批；批准后放行。"""
        _cap, create = wire_approval_cap(sc_mod, [{"selected_option": "approved_once"}])

        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": _GUARD_RULES})
        ctx = make_tool_ctx("bash_execute", {"command": command}, task_isolated=True)

        result = await plugin.execute(ctx)
        decision = result.state_updates.get("security.decision", {})
        assert create.calls == 1, f"隔离任务命中黑名单（{keyword!r}）必须发起审批"
        assert decision.get("allowed") is True
        assert decision.get("reason") == "approved", "审批通过后才允许执行"

    @pytest.mark.asyncio
    async def test_isolated_blacklist_denied_soft_blocks(self) -> None:
        """隔离任务 + 黑名单命中 + 用户拒绝 → 软拦截反馈 LLM，命令不执行。"""
        _cap, create = wire_approval_cap(sc_mod, [{"selected_option": "denied"}])

        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": _GUARD_RULES})
        ctx = make_tool_ctx(
            "bash_execute", {"command": "rm -rf .packtest_probe_dir"}, task_isolated=True
        )

        result = await plugin.execute(ctx)
        updates = result.state_updates
        decision = updates.get("security.decision", {})
        assert create.calls == 1, "拒绝路径同样必须先发起审批"
        assert "soft_block" in decision.get("reason", ""), f"拒绝必须软拦截: {decision!r}"
        assert updates.get("raw_tool_calls") == [], "被拒命令不得进入执行"

    @pytest.mark.asyncio
    async def test_isolated_rule_miss_passes_without_approval(self) -> None:
        """隔离任务 + 未命中规则 → 放行且审批零发起（环境类检查豁免）。"""
        _cap, create = wire_approval_cap(sc_mod, [])  # 空 sequence：任何审批尝试即失败

        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": _GUARD_RULES})
        ctx = make_tool_ctx(
            "bash_execute", {"command": "ls -la .packtest_probe_dir"}, task_isolated=True
        )

        result = await plugin.execute(ctx)
        decision = result.state_updates.get("security.decision", {})
        assert decision.get("allowed") is True
        assert create.calls == 0, "隔离任务未命中规则不得弹审批（隔离承担环境风险）"

    @pytest.mark.asyncio
    async def test_isolated_non_command_tool_blacklist_hit_approves(self) -> None:
        """隔离任务 + 非命令类工具命中黑名单（对外发布）→ 仍发起审批。

        黑名单是参数级判定，不受危险工具分类门槛限制：github_ops 非
        command_in_container 也未声明 dangerous_operations，规则命中照样审批。
        """
        _cap, create = wire_approval_cap(sc_mod, [{"selected_option": "approved_once"}])

        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": _GUARD_RULES})
        ctx = make_tool_ctx(
            "github_ops", {"action": "create_comment", "body": "release note"}, task_isolated=True
        )

        result = await plugin.execute(ctx)
        decision = result.state_updates.get("security.decision", {})
        assert create.calls == 1, "隔离任务的对外发布黑名单不得被危险分类门槛吞掉"
        assert decision.get("allowed") is True


class TestConservativeControls:
    """对照：非隔离 / 缺隔离标记时危险命令必须审批（保守侧不变）。"""

    @pytest.fixture(autouse=True)
    def _restore_cap(self):
        yield
        unwire_approval_cap(sc_mod)

    @pytest.mark.asyncio
    async def test_non_isolated_same_command_requires_approval(self) -> None:
        """同一危险命令 task_isolated=False → 必须发起审批。"""
        _cap, create = wire_approval_cap(sc_mod, [{"selected_option": "approved_once"}])

        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": _GUARD_RULES})
        ctx = make_tool_ctx("bash_execute", {"command": "rm -rf /tmp/a"}, task_isolated=False)

        result = await plugin.execute(ctx)
        assert result.state_updates.get("security.decision", {}).get("allowed") is True
        assert create.calls == 1, "非隔离任务的危险命令必须审批"

    @pytest.mark.asyncio
    async def test_missing_task_isolated_flag_is_conservative(self) -> None:
        """context 缺 task_isolated 字段 → 按未隔离处理（审批兜底）。"""
        _cap, create = wire_approval_cap(sc_mod, [{"selected_option": "approved_once"}])

        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": _GUARD_RULES})
        ctx = make_tool_ctx("bash_execute", {"command": "rm -rf /tmp/a"})

        result = await plugin.execute(ctx)
        assert result.state_updates.get("security.decision", {}).get("allowed") is True
        assert create.calls == 1, "缺隔离标记必须保守走审批"
