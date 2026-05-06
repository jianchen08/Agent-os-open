"""Bash 安全修复测试 — fix_20260506_bash_security。

测试覆盖：
- Bug 1: IsolationGuard fallback:deny 真正阻止执行
- Bug 2: working_dir 参数加入路径边界检查
- Bug 3: allowed_base_paths 多基路径支持（Skill 脚本）
- 额外: startswith 边界 bug 修复、Windows 大小写修复
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from pipeline.plugin import PluginContext
from pipeline.types import StateKeys

from tests.suites.plugins.conftest import load_module_from_file

_SRC_DIR = os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "src"
))


def _load(module_name, rel_path):
    """加载指定模块。"""
    return load_module_from_file(
        module_name,
        os.path.join(_SRC_DIR, *rel_path),
    )


def _make_ctx(state=None, services=None):
    """创建 Mock PluginContext。"""
    return PluginContext(
        state=state or {},
        config={},
        _services=services or {},
    )


# ============================================================================
# Bug 1: IsolationGuard fallback:deny 阻止执行
# ============================================================================


class TestFallbackDenyBlocked:
    """Bug 1: fallback:deny 时应真正阻止执行，而非静默降级到 host。"""

    def _make_plugin(self, config=None):
        """创建 IsolationGuard 实例（Docker 不可用）。"""
        mod = _load("isolation_guard", ["plugins", "input", "isolation_guard.py"])
        with patch.object(mod, "IsolationDecider"):
            return mod.IsolationGuard(config=config)

    def _mock_deny_policy(self, plugin):
        """模拟 fallback:deny 的策略。"""
        mock_policy = MagicMock()
        mock_policy.isolation = MagicMock()
        mock_policy.isolation.value = "container"
        mock_policy.fallback = "deny"
        plugin._decider.resolve = MagicMock(return_value=mock_policy)
        return plugin

    @pytest.mark.asyncio
    async def test_deny_sets_blocked_context(self):
        """fallback:deny + Docker 不可用 → 返回 blocked=True 的上下文。"""
        plugin = self._make_plugin(config={"docker_available": False})
        self._mock_deny_policy(plugin)

        ctx = _make_ctx({
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.RAW_TOOL_CALLS: [{"name": "bash_execute", "args": {}}],
        })

        result = await plugin.execute(ctx)

        contexts = result.state_updates.get("execution_contexts", [])
        assert len(contexts) == 1
        assert contexts[0].get("blocked") is True
        assert contexts[0].get("provider") == "denied"
        assert contexts[0].get("level") == "denied"
        assert contexts[0].get("reason") == "policy_fallback_denied"

    @pytest.mark.asyncio
    async def test_deny_sets_security_decision_blocked(self):
        """fallback:deny 应设置 security.decision = {allowed: False}。"""
        plugin = self._make_plugin(config={"docker_available": False})
        self._mock_deny_policy(plugin)

        ctx = _make_ctx({
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.RAW_TOOL_CALLS: [{"name": "bash_execute", "args": {}}],
        })

        result = await plugin.execute(ctx)

        decision = result.state_updates.get("security.decision")
        assert decision is not None
        assert decision.get("allowed") is False
        assert "fallback" in decision.get("reason", "").lower() or "deny" in decision.get("reason", "").lower()

    @pytest.mark.asyncio
    async def test_allow_fallback_still_works(self):
        """fallback:allow 仍应正常降级到 host。"""
        plugin = self._make_plugin(config={"docker_available": False})

        mock_policy = MagicMock()
        mock_policy.isolation = MagicMock()
        mock_policy.isolation.value = "container"
        mock_policy.fallback = "allow"
        plugin._decider.resolve = MagicMock(return_value=mock_policy)

        ctx = _make_ctx({
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.RAW_TOOL_CALLS: [{"name": "some_tool", "args": {}}],
        })

        result = await plugin.execute(ctx)

        contexts = result.state_updates.get("execution_contexts", [])
        assert len(contexts) == 1
        assert contexts[0].get("blocked") is not True
        assert contexts[0].get("provider") == "host"
        assert result.state_updates.get("security.decision") is None

    @pytest.mark.asyncio
    async def test_docker_available_no_block(self):
        """Docker 可用时 fallback:deny 不应触发阻止。"""
        plugin = self._make_plugin(config={"docker_available": True})

        mock_policy = MagicMock()
        mock_policy.isolation = MagicMock()
        mock_policy.isolation.value = "container"
        mock_policy.fallback = "deny"
        plugin._decider.resolve = MagicMock(return_value=mock_policy)

        ctx = _make_ctx({
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.RAW_TOOL_CALLS: [{"name": "bash_execute", "args": {}}],
        })

        result = await plugin.execute(ctx)

        contexts = result.state_updates.get("execution_contexts", [])
        assert len(contexts) == 1
        assert contexts[0].get("blocked") is not True
        assert contexts[0].get("provider") == "docker"
        assert result.state_updates.get("security.decision") is None


# ============================================================================
# Bug 2: working_dir 参数加入路径边界检查
# ============================================================================


class TestWorkingDirBoundaryCheck:
    """Bug 2: working_dir 参数应被路径边界检查覆盖。"""

    def _make_plugin(self, config=None):
        """创建 SecurityCheckPlugin 实例。"""
        mod = _load("security_check", ["plugins", "input", "security_check.py"])
        return mod.SecurityCheckPlugin(config=config)

    def test_working_dir_outside_workspace_blocked(self):
        """working_dir 指向 workspace 外应被拦截。"""
        plugin = self._make_plugin(config={
            "workspace": "D:\\safe\\workspace",
            "allowed_base_paths": [],
        })
        reason = plugin._check_workspace_boundary(
            {"working_dir": "D:\\other\\workspace"},
            workspace="D:\\safe\\workspace",
        )
        assert reason != ""
        assert "boundary" in reason.lower() or "outside" in reason.lower()

    def test_working_dir_inside_workspace_passes(self):
        """working_dir 在 workspace 内应通过。"""
        plugin = self._make_plugin(config={
            "workspace": "D:\\safe\\workspace",
        })
        reason = plugin._check_workspace_boundary(
            {"working_dir": "D:\\safe\\workspace\\subdir"},
            workspace="D:\\safe\\workspace",
        )
        assert reason == ""

    def test_working_dir_relative_skipped(self):
        """相对路径的 working_dir 应跳过边界检查。"""
        plugin = self._make_plugin(config={
            "workspace": "D:\\safe\\workspace",
        })
        reason = plugin._check_workspace_boundary(
            {"working_dir": "relative/path"},
            workspace="D:\\safe\\workspace",
        )
        assert reason == ""


# ============================================================================
# Bug 3: allowed_base_paths 多基路径支持（Skill 脚本）
# ============================================================================


class TestAllowedBasePaths:
    """Bug 3: allowed_base_paths 允许访问 workspace 外的项目目录。"""

    def _make_plugin(self, config=None):
        """创建 SecurityCheckPlugin 实例。"""
        mod = _load("security_check", ["plugins", "input", "security_check.py"])
        return mod.SecurityCheckPlugin(config=config)

    def test_default_allowed_base_paths_includes_skills(self):
        """默认 allowed_base_paths 应包含 skills 目录。"""
        plugin = self._make_plugin()
        assert "skills" in plugin._allowed_base_paths

    def test_skills_path_allowed_even_outside_workspace(self):
        """skills 目录路径即使在 workspace 外也应被允许。"""
        plugin = self._make_plugin(config={
            "workspace": "D:\\workspaces\\task_123",
            "allowed_base_paths": ["skills"],
        })
        # skills 路径基于项目根目录，通常不在 task workspace 内
        # 模拟一个在项目根目录下的 skills 路径
        project_root = os.path.normpath(os.path.join(
            os.path.dirname(__file__), "..", "..", ".."
        ))
        skills_path = os.path.join(project_root, "skills", "skill-code-impl", "scripts")

        reason = plugin._check_workspace_boundary(
            {"path": skills_path},
            workspace="D:\\workspaces\\task_123",
        )
        assert reason == ""

    def test_custom_allowed_base_paths(self):
        """自定义 allowed_base_paths 应生效。"""
        plugin = self._make_plugin(config={
            "workspace": "D:\\workspaces\\task_123",
            "allowed_base_paths": ["scripts", "templates"],
        })
        # scripts 在 allowed_base_paths 中
        project_root = os.path.normpath(os.path.join(
            os.path.dirname(__file__), "..", "..", ".."
        ))
        scripts_path = os.path.join(project_root, "scripts", "build.bat")

        reason = plugin._check_workspace_boundary(
            {"path": scripts_path},
            workspace="D:\\workspaces\\task_123",
        )
        assert reason == ""

    def test_arbitrary_path_still_blocked(self):
        """不在任何 allowed 路径内的路径仍应被拦截。"""
        plugin = self._make_plugin(config={
            "workspace": "D:\\workspaces\\task_123",
            "allowed_base_paths": ["skills"],
        })
        reason = plugin._check_workspace_boundary(
            {"path": "D:\\Users\\hacker\\malware"},
            workspace="D:\\workspaces\\task_123",
        )
        assert reason != ""

    def test_empty_allowed_base_paths_only_workspace(self):
        """allowed_base_paths 为空时只允许 workspace。"""
        plugin = self._make_plugin(config={
            "workspace": "D:\\safe\\workspace",
            "allowed_base_paths": [],
        })
        # workspace 内 → 通过
        reason = plugin._check_workspace_boundary(
            {"path": "D:\\safe\\workspace\\file.txt"},
            workspace="D:\\safe\\workspace",
        )
        assert reason == ""

        # workspace 外 → 拦截
        reason = plugin._check_workspace_boundary(
            {"path": "D:\\other\\dir\\file.txt"},
            workspace="D:\\safe\\workspace",
        )
        assert reason != ""


# ============================================================================
# 额外: startswith 边界 bug 修复
# ============================================================================


class TestStartswithBoundaryFix:
    """startswith(base + os.sep) 防止前缀误匹配。"""

    def _make_plugin(self, config=None):
        """创建 SecurityCheckPlugin 实例。"""
        mod = _load("security_check", ["plugins", "input", "security_check.py"])
        return mod.SecurityCheckPlugin(config=config)

    def test_similar_prefix_not_matched(self):
        """路径前缀相似但不同目录不应被误判为在 workspace 内。"""
        plugin = self._make_plugin(config={
            "workspace": "D:\\workspace",
            "allowed_base_paths": [],
        })
        # D:\workspace-backup 不应匹配 D:\workspace 前缀
        reason = plugin._check_workspace_boundary(
            {"path": "D:\\workspace-backup\\secrets"},
            workspace="D:\\workspace",
        )
        assert reason != ""

    def test_exact_subdir_matched(self):
        """workspace 的子目录应正确匹配。"""
        plugin = self._make_plugin(config={
            "workspace": "D:\\workspace",
            "allowed_base_paths": [],
        })
        reason = plugin._check_workspace_boundary(
            {"path": "D:\\workspace\\subdir\\file.txt"},
            workspace="D:\\workspace",
        )
        assert reason == ""


# ============================================================================
# 集成: IsolationGuard + SecurityCheck 联动
# ============================================================================


class TestIsolationGuardSecurityCheckIntegration:
    """IsolationGuard 阻止的请求与 security_check 幂等机制的联动。"""

    def _make_isolation_guard(self, config=None):
        """创建 IsolationGuard 实例。"""
        mod = _load("isolation_guard", ["plugins", "input", "isolation_guard.py"])
        with patch.object(mod, "IsolationDecider"):
            return mod.IsolationGuard(config=config)

    def _make_security_check(self, config=None):
        """创建 SecurityCheckPlugin 实例。"""
        mod = _load("security_check", ["plugins", "input", "security_check.py"])
        return mod.SecurityCheckPlugin(config=config)

    @pytest.mark.asyncio
    async def test_isolation_guard_blocked_skips_security_check(self):
        """IsolationGuard 已阻止时，SecurityCheck 幂等检查应跳过。"""
        guard = self._make_isolation_guard(config={"docker_available": False})

        mock_policy = MagicMock()
        mock_policy.isolation = MagicMock()
        mock_policy.isolation.value = "container"
        mock_policy.fallback = "deny"
        guard._decider.resolve = MagicMock(return_value=mock_policy)

        ctx = _make_ctx({
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.RAW_TOOL_CALLS: [{"name": "bash_execute", "args": {}}],
        })

        guard_result = await guard.execute(ctx)
        decision = guard_result.state_updates.get("security.decision")
        assert decision is not None
        assert decision.get("allowed") is False

        # SecurityCheck 看到已有 security.decision，应跳过
        security = self._make_security_check()
        ctx_after = _make_ctx({
            "security.decision": decision,
        })
        sec_result = await security.execute(ctx_after)
        assert sec_result.state_updates == {}
