"""Host 模式统一规则 — security_check 行为单元测试。

验证 Host 模式权限改造后的契约（纯单元测试，不走 engine.run，避免环境初始化拖累）：

1. host 模式不管工作目录边界（删除了 workspace 越界检查）
2. 路径遍历（../）仍拦截（防注入底线）
3. 敏感系统目录黑名单拦截（新增）
4. 危险工具判定双轨：command_in_container OR dangerous_operations
5. 非危险工具直接放行
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock

import pytest

from pipeline.plugin import PluginContext
from pipeline.types import StateKeys


def _make_plugin(rules: list[dict[str, Any]] | None = None) -> Any:
    """构建 SecurityCheckPlugin 实例。"""
    from plugins.input.security_check.plugin import SecurityCheckPlugin
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
        decision = result.state_updates.get("security.decision", {})

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
        mock_tool.dangerous_operations = ["delete:recursive"]
        mock_registry = MagicMock()
        mock_registry.get = MagicMock(return_value=mock_tool)
        ctx = _make_ctx(
            [{"name": "delete_file", "args": {"path": "/tmp/x"}}],
            services={"tool_registry": mock_registry},
        )
        assert plugin._is_dangerous_tool(ctx, "delete_file") is True

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
