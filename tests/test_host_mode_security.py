"""Host 模式统一规则 — security_check 行为单元测试。

验证 Host 模式权限改造后的契约（纯单元测试，不走 engine.run，避免环境初始化拖累）：

1. host 模式不管工作目录边界（删除了 workspace 越界检查）
2. 路径遍历（../）仍拦截（防注入底线）
3. 敏感系统目录黑名单拦截（新增）
4. 危险工具判定双轨：command_in_container OR dangerous_operations
5. 非危险工具直接放行
"""

from __future__ import annotations

from tests._pipeline_plugin_path import add_plugin_dir

add_plugin_dir("input", "security_check")
import plugin as sc_mod  # noqa: E402

import os
from typing import Any
from unittest.mock import MagicMock

import pytest
from pipeline.plugin import PluginContext
from pipeline.types import StateKeys

from tests._security_check_harness import (
    unwire_approval_cap,
    wire_approval_cap,
)

pytestmark = pytest.mark.unit


def _make_plugin(rules: list[dict[str, Any]] | None = None) -> Any:
    """构建 SecurityCheckPlugin 实例。"""
    add_plugin_dir("input", "security_check")
    from plugin import SecurityCheckPlugin
    config: dict[str, Any] = {"enabled": True}
    if rules is not None:
        config["rules"] = rules
    return SecurityCheckPlugin(config=config)


def _make_ctx(
    tool_calls: list[dict[str, Any]],
    *,
    provider: str = "host",
    services: dict[str, Any] | None = None,
) -> PluginContext:
    """构建 tool_execute 的 PluginContext。"""
    execution_contexts = [
        {"tool_name": tc["name"], "provider": provider} for tc in tool_calls
    ]
    state = {
        StateKeys.CORE_TYPE: "tool_execute",
        StateKeys.RAW_TOOL_CALLS: tool_calls,
        "execution_contexts": execution_contexts,
    }
    return PluginContext(state=state, _services=services or {})


class TestHostModeNoWorkspaceBoundary:
    """改动1：host 模式不再做工作目录越界检查。"""

    @pytest.mark.asyncio
    async def test_path_outside_workspace_not_blocked(self, tmp_path) -> None:
        """host 模式下访问 workspace 外的绝对路径不因边界被拦截。

        改动前：会被 _check_workspace_boundary 拦截（soft_block）。
        改动后：workspace 边界检查已删除，直接放行（无 dangerous_operations）。
        """
        plugin = _make_plugin(rules=[])
        outside = str(tmp_path.parent / "other_dir" / "file.txt")
        ctx = _make_ctx([{"name": "file_read", "args": {"path": outside}}])

        result = await plugin.execute(ctx)
        decision = result.state_updates.get("security.decision", {})

        assert decision.get("allowed") is True
        # 不应因工作目录越界被拦截
        assert "越界" not in decision.get("reason", "")


class TestPathTraversalStillBlocked:
    """改动后路径遍历（../）仍拦截——防注入底线保留。"""

    @pytest.mark.asyncio
    async def test_traversal_blocked(self) -> None:
        """含 ../ 的路径仍被拦截。"""
        plugin = _make_plugin(rules=[])
        ctx = _make_ctx([{"name": "file_read", "args": {"path": "../../../etc/passwd"}}])

        result = await plugin.execute(ctx)

        # 路径遍历走 soft_block，allowed=True 但有拒绝反馈
        assert "路径遍历" in result.state_updates.get(StateKeys.RAW_RESULT, "")


class TestSensitivePathBlocked:
    """改动后新增敏感系统目录黑名单拦截。"""

    @pytest.mark.asyncio
    @pytest.mark.skipif(os.name != "nt", reason="Windows 敏感目录仅 Windows 测试")
    async def test_windows_sensitive_blocked(self) -> None:
        """访问 C:/Windows 被拦截。"""
        plugin = _make_plugin(rules=[])
        ctx = _make_ctx([{"name": "file_read", "args": {"path": "C:\\Windows\\System32"}}])

        result = await plugin.execute(ctx)
        assert "敏感系统目录" in result.state_updates.get(StateKeys.RAW_RESULT, "")

    @pytest.mark.asyncio
    @pytest.mark.skipif(os.name == "nt", reason="Linux 敏感目录仅非 Windows 测试")
    async def test_linux_sensitive_blocked(self) -> None:
        """访问 /etc 被拦截。"""
        plugin = _make_plugin(rules=[])
        ctx = _make_ctx([{"name": "file_read", "args": {"path": "/etc/passwd"}}])

        result = await plugin.execute(ctx)
        assert "敏感系统目录" in result.state_updates.get(StateKeys.RAW_RESULT, "")


class TestDangerousToolDualTrack:
    """改动2：危险工具判定双轨——command_in_container OR dangerous_operations。"""

    @pytest.mark.asyncio
    async def test_bash_command_in_container_is_dangerous(self) -> None:
        """bash_execute 是 command_in_container → 危险工具。"""
        plugin = _make_plugin(rules=[])
        ctx = _make_ctx([{"name": "bash_execute", "args": {"command": "ls"}}])
        assert plugin._is_dangerous_tool(ctx, "bash_execute") is True

    @pytest.mark.asyncio
    async def test_read_only_tool_not_dangerous(self) -> None:
        """file_read 非危险工具（host_direct + 无 dangerous_operations）。"""
        plugin = _make_plugin(rules=[])
        ctx = _make_ctx([{"name": "file_read", "args": {}}])
        assert plugin._is_dangerous_tool(ctx, "file_read") is False

    @pytest.mark.asyncio
    async def test_tool_with_dangerous_operations_is_dangerous(self) -> None:
        """声明了 dangerous_operations 的工具 → 危险（通过注入 mock registry）。"""
        plugin = _make_plugin(rules=[])
        # 构造 mock tool_registry，delete_file 声明了 dangerous_operations
        mock_tool = MagicMock()
        mock_tool.dangerous_operations = ["write:/tmp/"]
        mock_registry = MagicMock()
        mock_registry.get = MagicMock(return_value=mock_tool)
        ctx = _make_ctx(
            [{"name": "delete_file", "args": {"path": "/tmp/x"}}],
            services={"tool_registry": mock_registry},
        )
        assert plugin._is_dangerous_tool(ctx, "delete_file", {"path": "/tmp/x"}) is True

    @pytest.mark.asyncio
    async def test_dangerous_tool_no_registry_falls_back_to_policy(self) -> None:
        """registry 不可用时回退到 policy.execution 判定（不崩溃）。"""
        plugin = _make_plugin(rules=[])
        # 无 services，registry 回退全局单例（可能抛异常被兜住）
        ctx = _make_ctx([{"name": "bash_execute", "args": {}}], services={})
        # bash_execute 走 command_in_container 轨道判为危险
        assert plugin._is_dangerous_tool(ctx, "bash_execute") is True

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_empty_dangerous_ops(self) -> None:
        """registry 中不存在的工具，_get_dangerous_operations 返回空列表。"""
        plugin = _make_plugin(rules=[])
        ctx = _make_ctx([{"name": "nonexistent_tool", "args": {}}], services={})
        assert plugin._get_dangerous_operations(ctx, "nonexistent_tool") == []


class TestDangerousDecisionEndToEnd:
    """行为级：危险判定的公开入口可观察结果——审批发起次数与放行决策。

    TestDangerousToolDualTrack 在 _is_dangerous_tool 级钉判定；这里钉用户
    可观察行为：危险工具弹审批、只读/未知工具零审批直接放行。

    注意：插件实例统一取自模块级 sc_mod（_make_plugin 内 add_plugin_dir 会
    逐出并重导裸名 plugin，产生第二个模块实例，审批通道全局量会分叉）。
    """

    @pytest.fixture(autouse=True)
    def _restore_cap(self):
        yield
        unwire_approval_cap(sc_mod)

    def _plugin(self, rules: list[dict[str, Any]] | None = None) -> Any:
        config: dict[str, Any] = {"enabled": True}
        if rules is not None:
            config["rules"] = rules
        return sc_mod.SecurityCheckPlugin(config=config)

    @pytest.mark.asyncio
    async def test_read_only_tool_passes_without_approval(self) -> None:
        """file_read（只读白名单）→ 放行且审批通道零发起。"""
        _cap, create = wire_approval_cap(sc_mod, [])  # 空 sequence：任何审批尝试即失败

        plugin = self._plugin(rules=[])
        ctx = _make_ctx([{"name": "file_read", "args": {"path": "docs/a.md"}}])

        result = await plugin.execute(ctx)
        assert result.state_updates.get("security.decision", {}).get("allowed") is True
        assert create.calls == 0, "只读工具不得发起审批"

    @pytest.mark.asyncio
    async def test_unknown_tool_passes_without_approval(self) -> None:
        """registry 中不存在的工具 → 无危险操作声明 → 放行零审批。"""
        _cap, create = wire_approval_cap(sc_mod, [])

        plugin = self._plugin(rules=[])
        ctx = _make_ctx([{"name": "nonexistent_tool", "args": {}}])

        result = await plugin.execute(ctx)
        assert result.state_updates.get("security.decision", {}).get("allowed") is True
        assert create.calls == 0, "未声明危险操作的工具不得发起审批"

    @pytest.mark.asyncio
    async def test_dangerous_operations_tool_triggers_approval(self) -> None:
        """声明 dangerous_operations 的工具 + needs_approval 规则 → 弹审批后按裁定放行。

        危险判定（registry 轨）是规则处置的前置闸门：工具未判危险时直接跳过
        规则处置（不弹审批）——故本测试同时钉住两段接线的协作。
        """
        _cap, create = wire_approval_cap(sc_mod, [{"selected_option": "approved_once"}])

        rules = [{
            "name": "delete_protect",
            "tools": ["delete_file"],
            "params": ["path"],
            "action": "needs_approval",
            "patterns": [{"type": "keyword", "value": "/tmp/"}],
        }]
        plugin = self._plugin(rules=rules)
        mock_tool = MagicMock()
        mock_tool.dangerous_operations = ["write:/tmp/"]
        mock_registry = MagicMock()
        mock_registry.get = MagicMock(return_value=mock_tool)
        ctx = _make_ctx(
            [{"name": "delete_file", "args": {"path": "/tmp/x"}}],
            services={"tool_registry": mock_registry},
        )

        result = await plugin.execute(ctx)
        assert create.calls == 1, "危险工具（registry 轨）命中 needs_approval 规则必须弹审批"
        assert result.state_updates.get("security.decision", {}).get("allowed") is True

    @pytest.mark.asyncio
    async def test_non_dangerous_tool_skips_rule_dispatch(self) -> None:
        """对照：未判危险的工具即使命中规则也不弹审批（危险判定是前置闸门）。

        工具选 report_export：不在只读白名单（file_read 会被白名单先放行，
        钉不到闸门）、无 dangerous_operations 声明、非 command_in_container
        ——危险闸门是它免于规则处置的唯一屏障。
        """
        _cap, create = wire_approval_cap(sc_mod, [])

        rules = [{
            "name": "export_guard",
            "tools": ["report_export"],
            "params": ["path"],
            "action": "needs_approval",
            "patterns": [{"type": "keyword", "value": "/tmp/"}],
        }]
        plugin = self._plugin(rules=rules)
        ctx = _make_ctx([{"name": "report_export", "args": {"path": "/tmp/a.csv"}}])

        result = await plugin.execute(ctx)
        assert create.calls == 0, "非危险工具不得进规则处置/审批"
        assert result.state_updates.get("security.decision", {}).get("allowed") is True

    @pytest.mark.asyncio
    async def test_command_in_container_tool_triggers_approval(self) -> None:
        """bash_execute（command_in_container 轨）命中危险命令规则 → 弹审批。"""
        _cap, create = wire_approval_cap(sc_mod, [{"selected_option": "approved_once"}])

        plugin = self._plugin(rules=[])
        ctx = _make_ctx(
            [{"name": "bash_execute", "args": {"command": "rm -rf /tmp/x"}}], services={}
        )

        result = await plugin.execute(ctx)
        assert create.calls == 1, "命令执行类工具命中危险命令规则必须弹审批"
        assert result.state_updates.get("security.decision", {}).get("allowed") is True
