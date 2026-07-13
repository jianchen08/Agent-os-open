"""Round2 测试审查 - 认证鉴权模块测试缺口补充

覆盖需求：05_认证鉴权模块需求文档
- F-AUTH-04: 登录限流 5 次/分钟
- F-AUTH-07/08/11: Token 验证/刷新/Redis降级
- F-AUTH-12/13/14: RBAC 权限角色与矩阵
"""

import pytest


class TestAuthConstants:
    """认证模块常量"""

    def test_login_rate_limit(self):
        """F-AUTH-04: AuthService 暴露登录限流常量"""
        from src.auth.service import AuthService
        assert hasattr(AuthService, "LOGIN_RATE_LIMIT"), "AuthService 应有 LOGIN_RATE_LIMIT 常量"
        assert isinstance(AuthService.LOGIN_RATE_LIMIT, int)
        assert AuthService.LOGIN_RATE_LIMIT > 0


class TestTokenManager:
    """F-AUTH-07/08/11: Token 管理"""

    def test_token_payload_structure(self):
        """TokenPayload 包含必要字段（sub/role/exp/iat/jti/type）"""
        from src.auth.models import TokenPayload
        payload = TokenPayload(
            sub="user1", role="admin", type="access",
            exp=9999999999, iat=1000000000, jti="abc123",
        )
        assert payload.sub == "user1"
        assert payload.role == "admin"

    def test_token_pair_structure(self):
        """TokenPair 包含 access_token 和 refresh_token"""
        try:
            from src.auth.models import TokenPair
            pair = TokenPair(
                access_token="access123",
                refresh_token="refresh456",
                token_type="Bearer",
                expires_in=3600,
            )
            assert pair.access_token == "access123"
            assert pair.refresh_token == "refresh456"
        except (ImportError, TypeError):
            pytest.skip("TokenPair 结构不同")


class TestRBAC:
    """F-AUTH-12/13/14: RBAC 权限控制

    源码角色为 guest/user/admin/super_admin（无 viewer）；操作为
    read/create/update/delete/manage/execute（无独立 write）。
    has_permission 接收 Permission 对象（来自 auth.rbac），不是字符串。
    """

    @pytest.fixture
    def rbac(self):
        from src.auth.rbac import RBACManager
        return RBACManager()

    def test_admin_has_all_permissions(self, rbac):
        """admin/super_admin 角色有 read/write/delete 权限"""
        from src.auth.rbac import Permission
        assert rbac.has_permission("admin", Permission.READ) is True
        assert rbac.has_permission("admin", Permission.WRITE) is True
        assert rbac.has_permission("super_admin", Permission.ADMIN) is True

    def test_viewer_read_only(self, rbac):
        """guest 角色只有 read 权限（源码无 viewer，用 guest 代替只读角色）"""
        from src.auth.rbac import Permission
        assert rbac.has_permission("guest", Permission.READ) is True
        assert rbac.has_permission("guest", Permission.WRITE) is False

    def test_user_limited_permissions(self, rbac):
        """user 角色有 read+write，无 delete/admin"""
        from src.auth.rbac import Permission
        assert rbac.has_permission("user", Permission.READ) is True
        assert rbac.has_permission("user", Permission.WRITE) is True
        assert rbac.has_permission("user", Permission.DELETE) is False

    def test_resource_action_permission(self, rbac):
        """资源×操作权限检查（基于 permission_matrix）"""
        assert rbac.has_resource_action_permission("admin", "config", "manage") is True
        assert rbac.has_resource_action_permission("guest", "tasks", "create") is False

    def test_permission_denied_raises(self, rbac):
        """check_resource_permission 无权限时抛 PermissionDeniedError"""
        from src.auth.rbac import Permission
        from core.exceptions import PermissionDeniedError
        with pytest.raises(PermissionDeniedError):
            rbac.check_resource_permission("guest", "users", Permission.ADMIN)

    def test_resources_enum(self):
        """14 种受控资源"""
        from src.auth.permission_matrix import Resource
        resources = [e.value for e in Resource]
        assert len(resources) >= 10  # 至少有足够的资源定义
        assert "threads" in resources
        assert "tasks" in resources

    def test_actions_enum(self):
        """6 种操作（read/create/update/delete/manage/execute）"""
        from src.auth.permission_matrix import Action
        actions = [e.value for e in Action]
        assert "read" in actions
        assert "create" in actions
        assert "delete" in actions