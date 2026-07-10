"""隔离判断单元测试 — security_check 的 _is_isolated 按 task_isolated 判定。

验证隔离判断契约（纯单元测试）：

1. 隔离任务（task_isolated=True）→ 已隔离（放行，所有工具不审批）
2. 非隔离任务（task_isolated=False）→ 未隔离（危险工具需审批）

isolation_level 是隔离的唯一真相源，由 isolation_guard 归一化后注入
每个 execution_context 的 task_isolated 字段。
"""

from __future__ import annotations

from plugins.input.security_check.plugin import SecurityCheckPlugin


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
