# @feature: FP-0.2.二 | @vision: V2 安全
"""隔离会话审批语义测试 — security_check 的免审批默认与显式覆盖契约。

用户裁定（2026-09-15，推翻并取代 c11025c04 的「隔离≠免审批」方向）：

1. 隔离/worktree 会话 + 用户未显式选择权限档 → 基础检查通过即整体放行
   （隔离容器即安全边界，黑名单命中也不弹审批，审批零发起）
2. 显式覆盖：用户在前端权限选择器显式选择任一档（含 default）→ 以所选档
   为准，黑名单 block/needs_approval 照常按档处置，隔离不再豁免
3. 非隔离任务 / 缺隔离标记 → 危险命令必须审批（保守对照，无免审批默认）

「显式选择」判定 = 权限模式表（_PERMISSION_MODES，键 session_id——会话稳定
键，BUG-15 裁定）存在该会话条目——表只由前端切换端点写入，缺省态不写表，
故在场即显式。执行上下文的 pipeline_id 同会话内随任务/子代理轮换，不作键。
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
        """隔离任务（task_isolated=True）判为隔离，无论 docker/host。"""
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


class TestExplicitModeResolution:
    """显式选择判定：权限模式表在场即显式，缺省（未写表）= 未显式选择。

    键位 = session_id（会话稳定键，BUG-15）：执行 ctx 的 pipeline_id 同会话内
    漂移（主/子管道、任务轮换），不得影响显式档命中。
    """

    def test_session_entry_is_explicit_despite_different_pipeline_id(self) -> None:
        """同会话、pipeline_id 不同（主/子管道轮换）→ 会话条目必须命中。"""
        plugin = SecurityCheckPlugin()
        ctx = make_tool_ctx(
            "bash_execute", {"command": "ls"}, task_isolated=True,
            pipeline_id="p-x", session_id="sess-x",
        )
        sc_mod._PERMISSION_MODES["sess-x"] = "bypass"
        try:
            assert plugin._explicit_permission_mode(ctx) == "bypass"
        finally:
            sc_mod._PERMISSION_MODES.pop("sess-x", None)

    def test_absent_key_is_not_explicit(self) -> None:
        plugin = SecurityCheckPlugin()
        ctx = make_tool_ctx(
            "bash_execute", {"command": "ls"}, task_isolated=True,
            pipeline_id="p-y", session_id="sess-y",
        )
        assert plugin._explicit_permission_mode(ctx) is None

    def test_missing_pipeline_id_still_resolves_by_session_id(self) -> None:
        """state 无 pipeline_id（部分内部链路）→ 按 session_id 命中。"""
        plugin = SecurityCheckPlugin()
        ctx = make_tool_ctx("bash_execute", {"command": "ls"}, session_id="sess-1")
        sc_mod._PERMISSION_MODES["sess-1"] = "auto"
        try:
            assert plugin._explicit_permission_mode(ctx) == "auto"
        finally:
            sc_mod._PERMISSION_MODES.pop("sess-1", None)


class TestIsolatedDefaultFreePass:
    """行为级：隔离任务未显式选择权限档 → 免审批默认（黑名单命中也放行）。

    经公开入口 execute 观察用户可感知行为：审批是否发起（create_choice
    次数）、决策是否放行——不断插件内部分支细节。
    """

    @pytest.fixture(autouse=True)
    def _clean_modes(self):
        sc_mod._PERMISSION_MODES.clear()
        yield
        sc_mod._PERMISSION_MODES.clear()

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
    async def test_isolated_blacklist_hit_passes_without_explicit_mode(
        self, keyword: str, command: str
    ) -> None:
        """隔离任务 + 黑名单命中 + 未显式选择 → 直接放行，审批零发起。"""
        _cap, create = wire_approval_cap(sc_mod, [])  # 空 sequence：任何审批尝试即失败

        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": _GUARD_RULES})
        ctx = make_tool_ctx(
            "bash_execute", {"command": command}, task_isolated=True, pipeline_id="p-iso"
        )

        result = await plugin.execute(ctx)
        decision = result.state_updates.get("security.decision", {})
        assert decision.get("allowed") is True, f"隔离免审批默认必须放行（{keyword!r}）"
        assert create.calls == 0, "隔离任务未显式选择权限档不得弹审批"
        assert decision.get("reason") == "isolated task, base checks passed"

    @pytest.mark.asyncio
    async def test_isolated_non_command_tool_blacklist_hit_passes(self) -> None:
        """隔离免审批默认覆盖全部工具：非命令类（对外发布）命中规则同样放行。"""
        _cap, create = wire_approval_cap(sc_mod, [])

        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": _GUARD_RULES})
        ctx = make_tool_ctx(
            "github_ops",
            {"action": "create_comment", "body": "release note"},
            task_isolated=True,
            pipeline_id="p-iso",
        )

        result = await plugin.execute(ctx)
        decision = result.state_updates.get("security.decision", {})
        assert decision.get("allowed") is True
        assert create.calls == 0, "免审批默认不区分工具类型"

    @pytest.mark.asyncio
    async def test_isolated_rule_miss_passes_without_approval(self) -> None:
        """隔离任务 + 未命中规则 → 放行且审批零发起（与命中行为一致）。"""
        _cap, create = wire_approval_cap(sc_mod, [])

        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": _GUARD_RULES})
        ctx = make_tool_ctx(
            "bash_execute",
            {"command": "ls -la .packtest_probe_dir"},
            task_isolated=True,
            pipeline_id="p-iso",
        )

        result = await plugin.execute(ctx)
        decision = result.state_updates.get("security.decision", {})
        assert decision.get("allowed") is True
        assert create.calls == 0


class TestIsolatedExplicitOverride:
    """行为级：显式选择权限档后，隔离任务按所选档处置（隔离不再豁免黑名单）。

    键位契约（BUG-15）：表键 = session_id（会话稳定键）；执行 ctx 携带同会话
    的 pipeline_id（"p-iso"）刻意与表键不同——主/子管道、任务轮换不得丢显式档。
    """

    # 会话键与执行 pipeline_id 分离：复现生产日志「写入 93ed78d66817、
    # 执行 35a2a5e52923/16350484cc98」的同会话键位漂移。
    _SESSION_ID = "sess-iso"
    _PIPELINE_ID = "p-iso"

    @pytest.fixture(autouse=True)
    def _clean_modes(self):
        sc_mod._PERMISSION_MODES.clear()
        yield
        sc_mod._PERMISSION_MODES.clear()

    @pytest.fixture(autouse=True)
    def _restore_cap(self):
        yield
        unwire_approval_cap(sc_mod)

    def _isolated_ctx(self, tool_name: str, args: dict[str, Any]) -> Any:
        return make_tool_ctx(
            tool_name,
            args,
            task_isolated=True,
            pipeline_id=self._PIPELINE_ID,
            session_id=self._SESSION_ID,
        )

    @pytest.mark.parametrize(
        ("tool_name", "args", "hit"),
        [
            ("bash_execute", {"command": "rm -rf .packtest_probe_dir"}, "rm -rf"),
            ("github_ops", {"action": "create_comment", "body": "note"}, "comment"),
        ],
        ids=["command_tool", "publish_tool"],
    )
    @pytest.mark.asyncio
    async def test_explicit_default_blacklist_hit_approves(
        self, tool_name: str, args: dict[str, Any], hit: str
    ) -> None:
        """隔离任务 + 显式选 default + 黑名单命中 → 必须发起审批；批准后放行。"""
        sc_mod._PERMISSION_MODES[self._SESSION_ID] = "default"
        _cap, create = wire_approval_cap(sc_mod, [{"selected_option": "approved_once"}])

        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": _GUARD_RULES})
        ctx = self._isolated_ctx(tool_name, args)

        result = await plugin.execute(ctx)
        decision = result.state_updates.get("security.decision", {})
        assert create.calls == 1, f"显式选 default 后隔离任务命中黑名单（{hit!r}）必须弹审批"
        assert decision.get("allowed") is True
        assert decision.get("reason") == "approved", "审批通过后才允许执行"

    @pytest.mark.asyncio
    async def test_explicit_default_denied_soft_blocks(self) -> None:
        """隔离任务 + 显式选 default + 用户拒绝 → 软拦截反馈 LLM，命令不执行。"""
        sc_mod._PERMISSION_MODES[self._SESSION_ID] = "default"
        _cap, create = wire_approval_cap(sc_mod, [{"selected_option": "denied"}])

        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": _GUARD_RULES})
        ctx = self._isolated_ctx("bash_execute", {"command": "rm -rf .packtest_probe_dir"})

        result = await plugin.execute(ctx)
        updates = result.state_updates
        decision = updates.get("security.decision", {})
        assert create.calls == 1, "拒绝路径同样必须先发起审批"
        assert "soft_block" in decision.get("reason", ""), f"拒绝必须软拦截: {decision!r}"
        assert updates.get("raw_tool_calls") == [], "被拒命令不得进入执行"

    @pytest.mark.asyncio
    async def test_explicit_bypass_passes_without_approval(self) -> None:
        """隔离任务 + 显式选 bypass → 命中规则也直接放行（按所选档处置）。"""
        sc_mod._PERMISSION_MODES[self._SESSION_ID] = "bypass"
        _cap, create = wire_approval_cap(sc_mod, [])

        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": _GUARD_RULES})
        ctx = self._isolated_ctx("bash_execute", {"command": "rm -rf .packtest_probe_dir"})

        result = await plugin.execute(ctx)
        decision = result.state_updates.get("security.decision", {})
        assert decision.get("allowed") is True
        assert create.calls == 0, "显式 bypass 按档放行，不弹审批"


class TestConservativeControls:
    """对照：非隔离 / 缺隔离标记时危险命令必须审批（保守侧不变）。"""

    @pytest.fixture(autouse=True)
    def _clean_modes(self):
        sc_mod._PERMISSION_MODES.clear()
        yield
        sc_mod._PERMISSION_MODES.clear()

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
