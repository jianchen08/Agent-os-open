"""隔离判断单元测试 — security_check 的 _is_isolated 只认 docker provider。

验证隔离判断契约（纯单元测试）：

1. docker 容器（所有工具 provider 均为 docker）→ 已隔离（放行）
2. 非 docker（含 host、混合、空）→ 未隔离（危险工具需审批）

isolation_level 只决定是否用 docker 隔离，工作空间副本（worktree/shared 等）
不参与隔离审批判定。
"""

from __future__ import annotations

from typing import Any

from plugins.input.security_check.plugin import SecurityCheckPlugin


class TestIsIsolated:
    """_is_isolated 只认 docker provider，不读工作空间副本。"""

    def test_all_docker_is_isolated(self) -> None:
        """所有工具 provider 均为 docker → 已隔离（放行）。"""
        plugin = SecurityCheckPlugin()
        assert plugin._is_isolated([{"provider": "docker"}]) is True

    def test_multiple_all_docker_is_isolated(self) -> None:
        """多个工具全部 docker → 已隔离。"""
        plugin = SecurityCheckPlugin()
        assert plugin._is_isolated([{"provider": "docker"}, {"provider": "docker"}]) is True

    def test_host_not_isolated(self) -> None:
        """provider 为 host → 未隔离（危险工具需审批）。"""
        plugin = SecurityCheckPlugin()
        assert plugin._is_isolated([{"provider": "host"}]) is False

    def test_mixed_docker_host_not_isolated(self) -> None:
        """混合 provider（含 host）→ 未隔离。"""
        plugin = SecurityCheckPlugin()
        assert plugin._is_isolated([{"provider": "docker"}, {"provider": "host"}]) is False

    def test_empty_execution_contexts_not_isolated(self) -> None:
        """空 execution_contexts → 未隔离（保守）。"""
        plugin = SecurityCheckPlugin()
        assert plugin._is_isolated([]) is False

    def test_denied_provider_not_isolated(self) -> None:
        """provider 为 denied → 未隔离。"""
        plugin = SecurityCheckPlugin()
        assert plugin._is_isolated([{"provider": "denied"}]) is False
