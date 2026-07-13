"""场景3：隔离模式端到端测试。

覆盖场景：
- host 模式：权限检查生效
- container 模式：容器隔离行为（Mock DockerProvider）
- 隔离不可用即报错：container 不可用时抛 IsolationError，不降级到 host
- IsolationDecider 决策逻辑验证
- PermissionChecker 权限边界验证
"""
import pytest

from isolation.decider import IsolationDecider, IsolationError
from isolation.permission_checker import PermissionChecker
from isolation.permission_policy import (
    PermissionPolicyManager,
    PermissionPolicyType,
    PermissionScope,
    ReadPermission,
    WritePermission,
    WorkspacePermissionPolicy,
)
from isolation.policy import IsolationPolicyLoader, ToolIsolationPolicy
from isolation.types import IsolationLevel


# ── 1. IsolationDecider 决策逻辑 ───────────────────────────────────

class TestIsolationDecider:
    """IsolationDecider 决策逻辑验证。"""

    @staticmethod
    def _make_decider(default_isolation=IsolationLevel.CONTAINER):
        """创建使用空配置的 decider，可控默认策略。"""
        loader = IsolationPolicyLoader(config_path="/nonexistent/path.yaml")
        loader._default = ToolIsolationPolicy(isolation=default_isolation)
        return IsolationDecider(policy_loader=loader), loader

    @pytest.mark.asyncio
    async def test_default_policy_is_host(self):
        """默认策略为宿主机隔离（isolation_policy.yaml 的 default: host）。"""
        decider = IsolationDecider()
        policy = decider.resolve("unknown_tool")
        assert policy.isolation == IsolationLevel.HOST

    @pytest.mark.asyncio
    async def test_decide_without_availability_check(self):
        """不做可用性检查时直接返回策略。"""
        decider = IsolationDecider()
        policy = await decider.decide("some_tool")
        # 默认无可用性检查，返回默认策略（host）
        assert policy.isolation == IsolationLevel.HOST

    @pytest.mark.asyncio
    async def test_container_unavailable_host_available_raises(self):
        """container 不可用 + host 可用 → 抛 IsolationError（不降级到 host）。"""
        decider, _ = self._make_decider()
        available = {IsolationLevel.HOST: True, IsolationLevel.CONTAINER: False}

        with pytest.raises(IsolationError, match="不可用"):
            await decider.decide(
                tool_name="test_tool",
                available_providers=available,
            )

    @pytest.mark.asyncio
    async def test_all_unavailable_raises(self):
        """所有级别都不可用时抛出 IsolationError。"""
        decider, _ = self._make_decider()
        available = {IsolationLevel.HOST: False, IsolationLevel.CONTAINER: False}

        with pytest.raises(IsolationError):
            await decider.decide(
                tool_name="no_provider_tool",
                available_providers=available,
            )

    @pytest.mark.asyncio
    async def test_container_available_returns_container(self):
        """container 可用时正常返回 container 策略。"""
        decider, _ = self._make_decider()
        available = {IsolationLevel.HOST: True, IsolationLevel.CONTAINER: True}

        policy = await decider.decide(
            tool_name="test_tool",
            available_providers=available,
        )
        assert policy.isolation == IsolationLevel.CONTAINER

    def test_resolve_by_tool_name(self):
        """精确工具名匹配。"""
        loader = IsolationPolicyLoader(config_path="/nonexistent/path.yaml")
        loader._tools["bash_execute"] = ToolIsolationPolicy(
            isolation=IsolationLevel.CONTAINER,
        )
        decider = IsolationDecider(policy_loader=loader)
        policy = decider.resolve("bash_execute")
        assert policy.isolation == IsolationLevel.CONTAINER

    def test_resolve_by_category(self):
        """分类匹配。"""
        loader = IsolationPolicyLoader(config_path="/nonexistent/path.yaml")
        loader._categories["code_execution"] = ToolIsolationPolicy(
            isolation=IsolationLevel.CONTAINER,
        )
        decider = IsolationDecider(policy_loader=loader)
        policy = decider.resolve("unknown_tool", tool_category="code_execution")
        assert policy.isolation == IsolationLevel.CONTAINER


# ── 2. PermissionChecker 权限边界 ──────────────────────────────────

class TestPermissionChecker:
    """PermissionChecker 权限检查验证。"""

    @pytest.fixture
    def checker(self, tmp_path):
        return PermissionChecker(project_root=str(tmp_path))

    @pytest.fixture
    def default_policy(self):
        return PermissionPolicyManager().get_default_policy()

    @pytest.fixture
    def readonly_policy(self):
        return PermissionPolicyManager().get_readonly_policy()

    # ── 读取权限 ──

    def test_read_project_scope_allows_any_path(self, checker, default_policy):
        """PROJECT scope 允许读取任意路径。"""
        ok, msg = checker.check_read_permission(
            "/any/path/file.txt", workspace=None, policy=default_policy,
        )
        assert ok

    def test_read_none_scope_denies_all(self, checker):
        """NONE scope 禁止所有读取。"""
        policy = WorkspacePermissionPolicy(
            name="no_read",
            policy_type=PermissionPolicyType.DEFAULT,
            read=ReadPermission(scope=PermissionScope.NONE),
            write=WritePermission(scope=PermissionScope.NONE),
        )
        ok, msg = checker.check_read_permission(
            "some_file.txt", workspace=None, policy=policy,
        )
        assert not ok
        assert "禁止" in msg

    def test_read_workspace_scope_without_workspace_denies(self, checker):
        """WORKSPACE scope 未指定 workspace 时拒绝。"""
        policy = WorkspacePermissionPolicy(
            name="ws_read",
            policy_type=PermissionPolicyType.DEFAULT,
            read=ReadPermission(scope=PermissionScope.WORKSPACE),
            write=WritePermission(scope=PermissionScope.NONE),
        )
        ok, msg = checker.check_read_permission(
            "file.txt", workspace=None, policy=policy,
        )
        assert not ok

    # ── 写入权限 ──

    def test_write_workspace_scope_inside_workspace(self, checker, tmp_path):
        """WORKSPACE scope 在 workspace 内允许写入。"""
        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir()
        test_file = workspace_dir / "output.txt"

        policy = WorkspacePermissionPolicy(
            name="ws_write",
            policy_type=PermissionPolicyType.DEFAULT,
            read=ReadPermission(scope=PermissionScope.PROJECT),
            write=WritePermission(scope=PermissionScope.WORKSPACE),
        )
        ok, msg = checker.check_write_permission(
            str(test_file), workspace=str(workspace_dir), policy=policy,
        )
        assert ok

    def test_write_workspace_scope_outside_denies(self, checker, tmp_path):
        """WORKSPACE scope 在 workspace 外拒绝写入。"""
        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir()
        outside_file = tmp_path / "outside" / "hack.txt"

        policy = WorkspacePermissionPolicy(
            name="ws_write_strict",
            policy_type=PermissionPolicyType.DEFAULT,
            read=ReadPermission(scope=PermissionScope.PROJECT),
            write=WritePermission(scope=PermissionScope.WORKSPACE),
        )
        ok, msg = checker.check_write_permission(
            str(outside_file), workspace=str(workspace_dir), policy=policy,
        )
        assert not ok
        assert "权限拒绝" in msg or "之外" in msg

    def test_write_none_scope_denies_all(self, checker):
        """NONE scope 禁止所有写入（只读策略）。"""
        policy = WorkspacePermissionPolicy(
            name="readonly",
            policy_type=PermissionPolicyType.READONLY,
            read=ReadPermission(scope=PermissionScope.PROJECT),
            write=WritePermission(scope=PermissionScope.NONE),
        )
        ok, msg = checker.check_write_permission(
            "any_file.txt", workspace=None, policy=policy,
        )
        assert not ok

    def test_write_project_scope_allows_all(self, checker):
        """PROJECT scope 允许写入整个项目。"""
        policy = WorkspacePermissionPolicy(
            name="full_write",
            policy_type=PermissionPolicyType.DEFAULT,
            read=ReadPermission(scope=PermissionScope.PROJECT),
            write=WritePermission(scope=PermissionScope.PROJECT),
        )
        ok, msg = checker.check_write_permission(
            "any_file.txt", workspace=None, policy=policy,
        )
        assert ok

    def test_is_path_in_workspace_true(self, checker, tmp_path):
        """路径在 workspace 内。"""
        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir()
        test_file = workspace_dir / "sub" / "file.txt"

        ok, error = checker.is_path_in_workspace(str(test_file), str(workspace_dir))
        assert ok

    def test_is_path_in_workspace_false(self, checker, tmp_path):
        """路径不在 workspace 内。"""
        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir()
        other_dir = tmp_path / "other"
        other_dir.mkdir()
        test_file = other_dir / "file.txt"

        ok, error = checker.is_path_in_workspace(str(test_file), str(workspace_dir))
        assert not ok

    def test_check_write_permission_dict_policy(self, tmp_path):
        """便捷函数 check_write_permission 接受字典策略。"""
        from isolation.permission_checker import check_write_permission

        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir()
        test_file = workspace_dir / "output.txt"

        policy_dict = {
            "name": "test",
            "policy_type": "default",
            "read": {"scope": "project", "allow_all": True},
            "write": {"scope": "workspace", "allow_outside": False},
        }
        ok, msg = check_write_permission(
            path=str(test_file),
            workspace=str(workspace_dir),
            policy=policy_dict,
            project_root=str(tmp_path),
        )
        assert ok


# ── 3. IsolationPolicyLoader ──────────────────────────────────────

class TestIsolationPolicyLoader:
    """策略加载器测试。"""

    def test_missing_config_uses_defaults(self):
        """配置文件不存在时使用默认策略。"""
        loader = IsolationPolicyLoader(config_path="/nonexistent/policy.yaml")
        policy = loader.resolve("any_tool")
        assert policy.isolation == IsolationLevel.CONTAINER

    def test_tool_name_priority_over_category(self):
        """工具名匹配优先于分类匹配。"""
        loader = IsolationPolicyLoader(config_path="/nonexistent/policy.yaml")
        loader._tools["special_tool"] = ToolIsolationPolicy(
            isolation=IsolationLevel.HOST,
        )
        loader._categories["code_execution"] = ToolIsolationPolicy(
            isolation=IsolationLevel.CONTAINER,
        )
        # 工具名匹配
        policy = loader.resolve("special_tool", category="code_execution")
        assert policy.isolation == IsolationLevel.HOST
        # 只分类匹配
        policy = loader.resolve("other_tool", category="code_execution")
        assert policy.isolation == IsolationLevel.CONTAINER

    def test_get_tool_names(self):
        loader = IsolationPolicyLoader(config_path="/nonexistent/policy.yaml")
        loader._tools = {
            "tool_a": ToolIsolationPolicy(),
            "tool_b": ToolIsolationPolicy(),
        }
        assert set(loader.get_tool_names()) == {"tool_a", "tool_b"}

    def test_get_category_names(self):
        loader = IsolationPolicyLoader(config_path="/nonexistent/policy.yaml")
        loader._categories = {
            "cat_a": ToolIsolationPolicy(),
        }
        assert loader.get_category_names() == ["cat_a"]


# ── 4. PermissionPolicyManager 策略管理 ────────────────────────────

class TestPermissionPolicyManager:
    """权限策略管理器测试。"""

    def test_default_policies_loaded(self):
        manager = PermissionPolicyManager()
        assert "default" in manager.list_policies()
        assert "readonly" in manager.list_policies()

    def test_get_default_policy(self):
        manager = PermissionPolicyManager()
        policy = manager.get_default_policy()
        assert policy.write.scope == PermissionScope.WORKSPACE

    def test_get_readonly_policy(self):
        manager = PermissionPolicyManager()
        policy = manager.get_readonly_policy()
        assert policy.write.scope == PermissionScope.NONE

    def test_has_policy(self):
        manager = PermissionPolicyManager()
        assert manager.has_policy("default")
        assert manager.has_policy("readonly")
        assert not manager.has_policy("nonexistent")

    def test_custom_policy_loading(self):
        custom = {
            "custom_policy": {
                "read": {"scope": "workspace"},
                "write": {"scope": "workspace", "allow_outside": True},
            }
        }
        manager = PermissionPolicyManager(custom_policies=custom)
        assert manager.has_policy("custom_policy")
        policy = manager.get_policy("custom_policy")
        assert policy.write.allow_outside is True


# ── 5. 隔离类型定义 ─────────────────────────────────────────────────

class TestIsolationTypes:
    """隔离系统类型定义验证。"""

    def test_isolation_level_values(self):
        assert IsolationLevel.CONTAINER == "isolated"
        assert IsolationLevel.HOST == "non_isolated"

    def test_isolation_environment_defaults(self):
        from isolation.types import IsolationContext, IsolationEnvironment, TaskType

        ctx = IsolationContext(task_id="test", task_type=TaskType.ATOMIC)
        env = IsolationEnvironment(
            env_id="env_1",
            level=IsolationLevel.HOST,
            provider_type="host",
            status="ready",
            context=ctx,
        )
        assert env.level == IsolationLevel.HOST
        assert env.status == "ready"

    def test_execution_result(self):
        from isolation.types import ExecutionResult

        result = ExecutionResult(success=True, output="done")
        d = result.to_dict()
        assert d["success"] is True
        assert d["output"] == "done"
        assert d["error"] is None
