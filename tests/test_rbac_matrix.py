"""RBAC 权限矩阵测试。

覆盖场景：
- 资源枚举完整性
- 资源×操作权限矩阵定义
- 角色继承与权限检查
- RBACManager 资源级权限校验
"""

import pytest

from src.auth.rbac import RBACManager, Role, Permission
from src.auth.permission_matrix import (
    Action,
    RESOURCE_PERMISSION_MATRIX,
    Resource,
    has_resource_action_permission,
)
from src.core.exceptions import PermissionDeniedError


# ============================================================
# 枚举完整性测试
# ============================================================


class TestResourceEnum:
    """测试资源枚举定义。"""

    def test_has_all_expected_resources(self) -> None:
        expected = {
            "threads", "tasks", "agents", "tools", "config",
            "memory", "triggers", "evaluation", "users",
            "workspaces", "plugins", "reviews", "maintenance", "artifacts",
        }
        actual = {r.value for r in Resource}
        assert expected.issubset(actual)

    def test_resource_values_are_lowercase(self) -> None:
        for r in Resource:
            assert r.value == r.value.lower()


class TestActionEnum:
    """测试操作枚举定义。"""

    def test_has_all_expected_actions(self) -> None:
        expected = {"read", "create", "update", "delete", "manage", "execute"}
        actual = {a.value for a in Action}
        assert expected.issubset(actual)


# ============================================================
# 权限矩阵完整性测试
# ============================================================


class TestPermissionMatrix:
    """测试资源×操作权限矩阵。"""

    def test_matrix_covers_all_resources(self) -> None:
        for resource in Resource:
            assert resource in RESOURCE_PERMISSION_MATRIX, f"资源 {resource} 缺少权限矩阵定义"

    def test_guest_can_only_read(self) -> None:
        """访客角色只有 read 权限。"""
        for resource in Resource:
            guest_perms = RESOURCE_PERMISSION_MATRIX[resource].get(Role.GUEST, set())
            for action in guest_perms:
                assert action == Action.READ, f"访客不应拥有 {resource} 的 {action} 权限"

    def test_super_admin_has_all_actions_on_all_resources(self) -> None:
        """超级管理员对所有资源拥有全部操作权限。"""
        all_actions = set(Action)
        for resource in Resource:
            admin_perms = RESOURCE_PERMISSION_MATRIX[resource].get(Role.SUPER_ADMIN, set())
            assert all_actions.issubset(admin_perms), (
                f"超级管理员缺少 {resource} 的某些操作: {all_actions - admin_perms}"
            )

    def test_user_can_read_and_create(self) -> None:
        """普通用户可读可创建。"""
        user_perms = RESOURCE_PERMISSION_MATRIX[Resource.THREADS][Role.USER]
        assert Action.READ in user_perms
        assert Action.CREATE in user_perms

    def test_user_cannot_delete_tasks(self) -> None:
        """普通用户不可删除任务。"""
        user_perms = RESOURCE_PERMISSION_MATRIX[Resource.TASKS].get(Role.USER, set())
        assert Action.DELETE not in user_perms

    def test_admin_can_manage_config(self) -> None:
        """管理员可管理配置。"""
        admin_perms = RESOURCE_PERMISSION_MATRIX[Resource.CONFIG][Role.ADMIN]
        assert Action.MANAGE in admin_perms


# ============================================================
# has_resource_action_permission 函数测试
# ============================================================


class TestHasResourceActionPermission:
    """测试资源级权限检查函数。"""

    def test_guest_can_read_threads(self) -> None:
        assert has_resource_action_permission(Role.GUEST, Resource.THREADS, Action.READ) is True

    def test_guest_cannot_create_threads(self) -> None:
        assert has_resource_action_permission(Role.GUEST, Resource.THREADS, Action.CREATE) is False

    def test_user_can_create_tasks(self) -> None:
        assert has_resource_action_permission(Role.USER, Resource.TASKS, Action.CREATE) is True

    def test_user_cannot_delete_tasks(self) -> None:
        assert has_resource_action_permission(Role.USER, Resource.TASKS, Action.DELETE) is False

    def test_admin_can_delete_tasks(self) -> None:
        assert has_resource_action_permission(Role.ADMIN, Resource.TASKS, Action.DELETE) is True

    def test_super_admin_can_execute_tools(self) -> None:
        assert (
            has_resource_action_permission(Role.SUPER_ADMIN, Resource.TOOLS, Action.EXECUTE)
            is True
        )

    def test_string_role_accepted(self) -> None:
        """字符串角色也可使用。"""
        assert (
            has_resource_action_permission("user", Resource.THREADS, Action.READ) is True
        )

    def test_user_can_update_threads(self) -> None:
        """普通用户可更新自己的会话线程。"""
        assert (
            has_resource_action_permission(Role.USER, Resource.THREADS, Action.UPDATE) is True
        )

    def test_user_cannot_manage_users(self) -> None:
        """普通用户不能管理用户。"""
        assert (
            has_resource_action_permission(Role.USER, Resource.USERS, Action.MANAGE) is False
        )


# ============================================================
# RBACManager 资源级权限校验测试
# ============================================================


class TestRBACManagerResourceAction:
    """测试 RBACManager 的资源级权限方法。"""

    def setup_method(self) -> None:
        self.manager = RBACManager()

    def test_has_resource_action_returns_true(self) -> None:
        assert self.manager.has_resource_action_permission(
            Role.USER, Resource.THREADS, Action.READ
        ) is True

    def test_has_resource_action_returns_false(self) -> None:
        assert self.manager.has_resource_action_permission(
            Role.GUEST, Resource.THREADS, Action.CREATE
        ) is False

    def test_check_resource_action_passes(self) -> None:
        """有权限时不抛异常。"""
        self.manager.check_resource_action_permission(
            Role.ADMIN, Resource.CONFIG, Action.UPDATE
        )

    def test_check_resource_action_raises_on_denied(self) -> None:
        """无权限时抛 PermissionDeniedError。"""
        with pytest.raises(PermissionDeniedError):
            self.manager.check_resource_action_permission(
                Role.GUEST, Resource.TASKS, Action.DELETE
            )

    def test_check_resource_action_error_message_contains_details(self) -> None:
        """错误消息包含角色、资源、操作信息。"""
        with pytest.raises(PermissionDeniedError, match="GUEST.*tasks.*delete"):
            self.manager.check_resource_action_permission(
                Role.GUEST, Resource.TASKS, Action.DELETE
            )

    def test_string_arguments_accepted(self) -> None:
        """字符串参数也应正常工作。"""
        assert self.manager.has_resource_action_permission(
            "admin", "tasks", "delete"
        ) is True

    def test_role_inheritance_applies_to_resources(self) -> None:
        """角色继承同样适用于资源级权限——admin 继承 user 的 read 权限。"""
        assert self.manager.has_resource_action_permission(
            Role.ADMIN, Resource.THREADS, Action.READ
        ) is True


# ============================================================
# 细化测试：RBACManager 完整方法覆盖
# ============================================================

class TestRBACManagerComprehensive:
    """RBACManager 全面测试——基础权限、资源权限、角色判断。"""

    def setup_method(self) -> None:
        self.manager = RBACManager()

    # ── get_role_permissions ──────────────────────────

    def test_guest_permissions(self) -> None:
        """GUEST 只有 READ。"""
        perms = self.manager.get_role_permissions(Role.GUEST)
        assert Permission.READ in perms
        assert Permission.WRITE not in perms
        assert Permission.DELETE not in perms
        assert Permission.ADMIN not in perms

    def test_user_permissions(self) -> None:
        """USER 有 READ + WRITE（继承 GUEST 的 READ）。"""
        perms = self.manager.get_role_permissions(Role.USER)
        assert Permission.READ in perms
        assert Permission.WRITE in perms
        assert Permission.DELETE not in perms
        assert Permission.ADMIN not in perms

    def test_admin_permissions(self) -> None:
        """ADMIN 有 READ + WRITE + DELETE。"""
        perms = self.manager.get_role_permissions(Role.ADMIN)
        assert Permission.READ in perms
        assert Permission.WRITE in perms
        assert Permission.DELETE in perms
        assert Permission.ADMIN not in perms

    def test_super_admin_permissions(self) -> None:
        """SUPER_ADMIN 拥有全部权限。"""
        perms = self.manager.get_role_permissions(Role.SUPER_ADMIN)
        assert Permission.READ in perms
        assert Permission.WRITE in perms
        assert Permission.DELETE in perms
        assert Permission.ADMIN in perms

    def test_string_role_get_permissions(self) -> None:
        """字符串角色也可获取权限。"""
        perms = self.manager.get_role_permissions("admin")
        assert Permission.DELETE in perms

    # ── has_permission / check_permission ──────────────

    def test_has_permission_true(self) -> None:
        assert self.manager.has_permission(Role.ADMIN, Permission.DELETE) is True

    def test_has_permission_false(self) -> None:
        assert self.manager.has_permission(Role.GUEST, Permission.DELETE) is False

    def test_check_permission_passes(self) -> None:
        """有权限时不抛异常。"""
        self.manager.check_permission(Role.USER, Permission.READ)

    def test_check_permission_raises(self) -> None:
        """无权限时抛 PermissionDeniedError。"""
        with pytest.raises(PermissionDeniedError):
            self.manager.check_permission(Role.GUEST, Permission.DELETE)

    # ── 资源级权限 ────────────────────────────────────

    def test_add_and_check_resource_permission(self) -> None:
        """添加自定义资源权限后检查通过。"""
        self.manager.add_resource_permission(
            "custom_resource", Role.USER, [Permission.READ, Permission.WRITE]
        )
        assert self.manager.has_resource_permission(Role.USER, "custom_resource", Permission.READ)
        assert self.manager.has_resource_permission(Role.USER, "custom_resource", Permission.WRITE)
        assert not self.manager.has_resource_permission(Role.USER, "custom_resource", Permission.DELETE)

    def test_resource_inheritance(self) -> None:
        """admin 继承 user 的资源权限。"""
        self.manager.add_resource_permission(
            "res_x", Role.USER, [Permission.READ]
        )
        assert self.manager.has_resource_permission(Role.ADMIN, "res_x", Permission.READ)

    def test_resource_no_permission_for_unconfigured(self) -> None:
        """未配置的资源返回 False。"""
        assert not self.manager.has_resource_permission(Role.USER, "nonexistent", Permission.READ)

    def test_check_resource_permission_raises(self) -> None:
        """无资源权限时抛异常（含角色、资源、操作信息）。"""
        self.manager.add_resource_permission("res_y", Role.USER, [Permission.READ])
        with pytest.raises(PermissionDeniedError, match="res_y"):
            self.manager.check_resource_permission(Role.USER, "res_y", Permission.DELETE)

    # ── is_role_higher_or_equal ────────────────────────

    def test_admin_higher_than_user(self) -> None:
        assert self.manager.is_role_higher_or_equal(Role.ADMIN, Role.USER) is True

    def test_admin_higher_than_guest(self) -> None:
        assert self.manager.is_role_higher_or_equal(Role.ADMIN, Role.GUEST) is True

    def test_user_not_higher_than_admin(self) -> None:
        assert self.manager.is_role_higher_or_equal(Role.USER, Role.ADMIN) is False

    def test_same_role_equal(self) -> None:
        assert self.manager.is_role_higher_or_equal(Role.USER, Role.USER) is True

    def test_guest_not_higher_than_any(self) -> None:
        assert self.manager.is_role_higher_or_equal(Role.GUEST, Role.USER) is False

    # ── _normalize_role ────────────────────────────────

    def test_normalize_enum_passthrough(self) -> None:
        assert self.manager._normalize_role(Role.ADMIN) == Role.ADMIN

    def test_normalize_string_to_enum(self) -> None:
        assert self.manager._normalize_role("user") == Role.USER

    def test_normalize_invalid_role_raises(self) -> None:
        with pytest.raises(ValueError, match="无效的角色"):
            self.manager._normalize_role("god")


# ============================================================
# 细化测试：permission_matrix 全覆盖
# ============================================================

class TestPermissionMatrixComprehensive:
    """permission_matrix 的完整性和正确性验证。"""

    def test_all_actions_enum(self) -> None:
        """Action 枚举完整性。"""
        values = {a.value for a in Action}
        assert values == {"read", "create", "update", "delete", "manage", "execute"}

    def test_artifacts_guest_read(self) -> None:
        """ARTIFACTS: GUEST 可读。"""
        assert Action.READ in RESOURCE_PERMISSION_MATRIX[Resource.ARTIFACTS][Role.GUEST]

    def test_artifacts_user_create(self) -> None:
        """ARTIFACTS: USER 可创建。"""
        assert Action.CREATE in RESOURCE_PERMISSION_MATRIX[Resource.ARTIFACTS][Role.USER]

    def test_artifacts_user_cannot_delete(self) -> None:
        """ARTIFACTS: USER 不可删除。"""
        assert Action.DELETE not in RESOURCE_PERMISSION_MATRIX[Resource.ARTIFACTS][Role.USER]

    def test_artifacts_admin_can_manage(self) -> None:
        """ARTIFACTS: ADMIN 可管理。"""
        assert Action.MANAGE in RESOURCE_PERMISSION_MATRIX[Resource.ARTIFACTS][Role.ADMIN]

    def test_maintenance_guest_no_access(self) -> None:
        """MAINTENANCE: GUEST 无权限。"""
        assert RESOURCE_PERMISSION_MATRIX[Resource.MAINTENANCE][Role.GUEST] == set()

    def test_maintenance_user_no_access(self) -> None:
        """MAINTENANCE: USER 无权限。"""
        assert RESOURCE_PERMISSION_MATRIX[Resource.MAINTENANCE][Role.USER] == set()

    def test_maintenance_admin_manager(self) -> None:
        """MAINTENANCE: ADMIN 可管理。"""
        assert Action.MANAGE in RESOURCE_PERMISSION_MATRIX[Resource.MAINTENANCE][Role.ADMIN]

    def test_users_guest_no_access(self) -> None:
        """USERS: GUEST 无权查看用户列表。"""
        assert RESOURCE_PERMISSION_MATRIX[Resource.USERS][Role.GUEST] == set()

    def test_users_user_read_only(self) -> None:
        """USERS: USER 只读。"""
        perms = RESOURCE_PERMISSION_MATRIX[Resource.USERS][Role.USER]
        assert Action.READ in perms
        assert Action.CREATE not in perms

    def test_tasks_user_can_execute(self) -> None:
        """TASKS: USER 可执行。"""
        assert Action.EXECUTE in RESOURCE_PERMISSION_MATRIX[Resource.TASKS][Role.USER]

    def test_matrix_no_unknown_roles(self) -> None:
        """权限矩阵不含未定义角色。"""
        valid_roles = set(Role)
        for _resource, role_perms in RESOURCE_PERMISSION_MATRIX.items():
            for role in role_perms:
                assert role in valid_roles, f"未知角色 {role} in {_resource}"

    def test_has_resource_action_permission_invalid_action(self) -> None:
        """无效 action 字符串抛出 ValueError。"""
        with pytest.raises(ValueError, match="not a valid Action"):
            has_resource_action_permission(Role.ADMIN, Resource.TASKS, "fly")

    def test_has_resource_action_permission_invalid_resource(self) -> None:
        """无效 resource 返回 False。"""
        # 使用一个不在枚举中的字符串
        class FakeRes:
            value = "fake_resource"
        assert has_resource_action_permission(Role.ADMIN, FakeRes(), Action.READ) is False
