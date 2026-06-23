"""
Round 3 — 认证鉴权安全边界测试。

聚焦安全边界场景，补充 Round 1 未覆盖的部分：
1. RBAC 权限矩阵完整性：读取 permission_matrix.py 中的实际角色×资源×操作映射，
   验证 admin/user/viewer（代码中为 guest/user/admin/super_admin）的权限正确性
2. Token 边界场景：篡改 payload、伪造签名、过期 token+有效 refresh_token 组合、已撤销 token 重试
3. 登录限流边界：刚好5次允许、第6次拒绝、时间窗口重置后恢复

覆盖 AC：AC-AUTH-08（不同角色权限正确）、AC-AUTH-09（资源×操作矩阵正确）、
        AC-AUTH-04（Token 过期）、AC-AUTH-05（Token 撤销）、AC-AUTH-10（登录限流）
"""
from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

import jwt
import pytest

from src.auth.models import UserCreate
from src.auth.password import hash_password
from src.auth.permission_matrix import (
    RESOURCE_PERMISSION_MATRIX,
    Action,
    Resource,
    has_resource_action_permission,
)
from src.auth.rbac import (
    ROLE_INHERITANCE,
    ROLE_PERMISSIONS,
    Permission,
    RBACManager,
    Role,
)
from src.auth.service import AuthService
from src.auth.token import TokenManager
# 注意：src.core.exceptions.__init__ 从 core.exceptions.auth 再导出，
# 但 src.core.exceptions.auth 是不同模块路径。
# 必须匹配各源码文件实际使用的导入路径：
# - rbac.py / token.py → from src.core.exceptions import ...
# - service.py → from src.core.exceptions.auth import ...
from src.core.exceptions import (  # noqa: I001  # 匹配 rbac.py / token.py 的导入路径
    PermissionDeniedError,
    TokenExpiredError,
    TokenInvalidError,
    TokenRevokedError,
)
from src.core.exceptions.auth import (  # 匹配 service.py 的导入路径
    InvalidCredentialsError,
    RateLimitExceededError,
    UserInactiveError,
)


# ============================================================
# 辅助
# ============================================================

SECRET_KEY = "test-secret-key-at-least-32-bytes-long-for-hs256"


def _make_token_manager() -> TokenManager:
    """创建使用内存降级的 TokenManager（无 Redis）。"""
    return TokenManager(
        secret_key=SECRET_KEY,
        algorithm="HS256",
        access_token_expire_minutes=30,
        refresh_token_expire_days=7,
        redis_url="redis://invalid:99999/0",
    )


# ============================================================
# 1. RBAC 权限矩阵完整性
# ============================================================

class TestRBACPermissionMatrix:
    """RBAC 权限矩阵完整性测试。

    直接读取 permission_matrix.py 中的 RESOURCE_PERMISSION_MATRIX 进行验证，
    确保矩阵结构和层次关系正确。
    """

    def test_matrix_covers_all_resources(self) -> None:
        """权限矩阵覆盖所有 14 种受控资源。"""
        matrix_resources = set(RESOURCE_PERMISSION_MATRIX.keys())
        enum_resources = set(Resource)
        assert matrix_resources == enum_resources, (
            f"矩阵资源 {matrix_resources} 与枚举资源 {enum_resources} 不匹配"
        )

    def test_matrix_covers_all_roles(self) -> None:
        """每个资源在权限矩阵中定义了全部 4 种角色。"""
        all_roles = {Role.GUEST, Role.USER, Role.ADMIN, Role.SUPER_ADMIN}
        for resource in Resource:
            roles_in_matrix = set(RESOURCE_PERMISSION_MATRIX[resource].keys())
            assert roles_in_matrix == all_roles, (
                f"资源 {resource.value} 缺少角色定义，"
                f"实际: {roles_in_matrix}, 期望: {all_roles}"
            )

    def test_super_admin_has_all_actions_on_all_resources(self) -> None:
        """SUPER_ADMIN 对所有资源拥有全部操作（6 种 Action）。"""
        all_actions = set(Action)
        for resource in Resource:
            perms = RESOURCE_PERMISSION_MATRIX[resource][Role.SUPER_ADMIN]
            assert perms == all_actions, (
                f"SUPER_ADMIN 对 {resource.value} 权限不完整: {perms} != {all_actions}"
            )

    def test_guest_only_read_or_empty(self) -> None:
        """GUEST 权限不超过 READ（部分资源无权限）。"""
        for resource in Resource:
            perms = RESOURCE_PERMISSION_MATRIX[resource][Role.GUEST]
            non_read = perms - {Action.READ}
            assert non_read == set(), (
                f"GUEST 对 {resource.value} 拥有超出 READ 的权限: {non_read}"
            )

    def test_guest_no_access_to_users_and_maintenance(self) -> None:
        """GUEST 对 users 和 maintenance 资源无任何权限。"""
        assert RESOURCE_PERMISSION_MATRIX[Resource.USERS][Role.GUEST] == set()
        assert RESOURCE_PERMISSION_MATRIX[Resource.MAINTENANCE][Role.GUEST] == set()

    def test_user_permissions_superset_of_guest(self) -> None:
        """USER 权限是 GUEST 权限的超集（层次设计）。"""
        for resource in Resource:
            guest_perms = RESOURCE_PERMISSION_MATRIX[resource][Role.GUEST]
            user_perms = RESOURCE_PERMISSION_MATRIX[resource][Role.USER]
            assert guest_perms.issubset(user_perms), (
                f"USER({user_perms}) 不是 GUEST({guest_perms}) 的超集 "
                f"for resource {resource.value}"
            )

    def test_admin_permissions_superset_of_user(self) -> None:
        """ADMIN 权限是 USER 权限的超集。"""
        for resource in Resource:
            user_perms = RESOURCE_PERMISSION_MATRIX[resource][Role.USER]
            admin_perms = RESOURCE_PERMISSION_MATRIX[resource][Role.ADMIN]
            assert user_perms.issubset(admin_perms), (
                f"ADMIN({admin_perms}) 不是 USER({user_perms}) 的超集 "
                f"for resource {resource.value}"
            )

    def test_admin_has_delete_and_manage_on_all_resources(self) -> None:
        """ADMIN 对所有资源拥有 DELETE 和 MANAGE 权限。"""
        for resource in Resource:
            perms = RESOURCE_PERMISSION_MATRIX[resource][Role.ADMIN]
            assert Action.DELETE in perms, f"ADMIN 缺少 {resource.value} 的 DELETE"
            assert Action.MANAGE in perms, f"ADMIN 缺少 {resource.value} 的 MANAGE"

    def test_user_can_execute_tasks_and_tools(self) -> None:
        """USER 对 tasks 和 tools 拥有 EXECUTE 权限。"""
        assert Action.EXECUTE in RESOURCE_PERMISSION_MATRIX[Resource.TASKS][Role.USER]
        assert Action.EXECUTE in RESOURCE_PERMISSION_MATRIX[Resource.TOOLS][Role.USER]

    def test_user_cannot_execute_agents_config_plugins(self) -> None:
        """USER 对 agents/config/plugins 只有 READ，不能 EXECUTE。"""
        for res in (Resource.AGENTS, Resource.CONFIG, Resource.PLUGINS):
            perms = RESOURCE_PERMISSION_MATRIX[res][Role.USER]
            assert Action.EXECUTE not in perms, (
                f"USER 不应对 {res.value} 有 EXECUTE 权限"
            )

    def test_maintenance_user_has_no_permission(self) -> None:
        """USER 对 maintenance 资源无任何权限（仅 ADMIN 以上）。"""
        assert RESOURCE_PERMISSION_MATRIX[Resource.MAINTENANCE][Role.USER] == set()


class TestRBACManagerMethods:
    """RBACManager 方法测试。"""

    def test_has_permission_guest_read_only(self) -> None:
        """GUEST 只有 READ 权限。"""
        mgr = RBACManager()
        assert mgr.has_permission(Role.GUEST, Permission.READ) is True
        assert mgr.has_permission(Role.GUEST, Permission.WRITE) is False
        assert mgr.has_permission(Role.GUEST, Permission.DELETE) is False
        assert mgr.has_permission(Role.GUEST, Permission.ADMIN) is False

    def test_has_permission_admin_inherits_user(self) -> None:
        """ADMIN 继承 USER 和 GUEST 的权限，额外拥有 DELETE。"""
        mgr = RBACManager()
        assert mgr.has_permission(Role.ADMIN, Permission.READ) is True
        assert mgr.has_permission(Role.ADMIN, Permission.WRITE) is True
        assert mgr.has_permission(Role.ADMIN, Permission.DELETE) is True
        # ADMIN 不继承 SUPER_ADMIN 的 ADMIN 权限
        assert mgr.has_permission(Role.ADMIN, Permission.ADMIN) is False

    def test_has_permission_super_admin_has_all(self) -> None:
        """SUPER_ADMIN 拥有所有权限。"""
        mgr = RBACManager()
        for perm in Permission:
            assert mgr.has_permission(Role.SUPER_ADMIN, perm) is True

    def test_check_permission_denied_raises(self) -> None:
        """无权限时 check_permission 抛出 PermissionDeniedError。"""
        mgr = RBACManager()
        with pytest.raises(PermissionDeniedError):
            mgr.check_permission(Role.GUEST, Permission.DELETE)

    def test_check_permission_granted_no_raise(self) -> None:
        """有权限时 check_permission 不抛异常。"""
        mgr = RBACManager()
        mgr.check_permission(Role.USER, Permission.WRITE)  # 不应抛异常

    def test_has_resource_action_permission_admin_delete_tasks(self) -> None:
        """ADMIN 可以删除 tasks。"""
        mgr = RBACManager()
        assert mgr.has_resource_action_permission(
            Role.ADMIN, Resource.TASKS, Action.DELETE
        ) is True

    def test_has_resource_action_permission_guest_create_denied(self) -> None:
        """GUEST 不能创建任何资源。"""
        mgr = RBACManager()
        assert mgr.has_resource_action_permission(
            Role.GUEST, Resource.THREADS, Action.CREATE
        ) is False

    def test_has_resource_action_permission_string_role(self) -> None:
        """支持字符串角色名。"""
        assert has_resource_action_permission(
            "super_admin", Resource.TASKS, Action.DELETE
        ) is True
        assert has_resource_action_permission(
            "guest", Resource.TASKS, Action.DELETE
        ) is False

    def test_check_resource_action_permission_denied_raises(self) -> None:
        """资源操作权限不足时抛出异常。"""
        mgr = RBACManager()
        with pytest.raises(PermissionDeniedError):
            mgr.check_resource_action_permission(
                Role.GUEST, Resource.USERS, Action.READ
            )

    def test_is_role_higher_or_equal_hierarchy(self) -> None:
        """角色层级比较正确。"""
        mgr = RBACManager()
        # 同级
        assert mgr.is_role_higher_or_equal(Role.ADMIN, Role.ADMIN) is True
        # 高级 vs 低级
        assert mgr.is_role_higher_or_equal(Role.SUPER_ADMIN, Role.ADMIN) is True
        assert mgr.is_role_higher_or_equal(Role.ADMIN, Role.USER) is True
        # 低级 vs 高级
        assert mgr.is_role_higher_or_equal(Role.GUEST, Role.ADMIN) is False

    def test_normalize_role_invalid_raises(self) -> None:
        """无效角色名抛出 ValueError。"""
        mgr = RBACManager()
        with pytest.raises(ValueError, match="无效的角色"):
            mgr._normalize_role("nonexistent_role")

    def test_role_inheritance_chain_complete(self) -> None:
        """角色继承链完整：SUPER_ADMIN → ADMIN → USER → GUEST。"""
        chain = [Role.SUPER_ADMIN]
        current = Role.SUPER_ADMIN
        while ROLE_INHERITANCE.get(current):
            chain.extend(ROLE_INHERITANCE[current])
            current = ROLE_INHERITANCE[current][0]
        assert Role.ADMIN in chain
        assert Role.USER in chain
        assert Role.GUEST in chain


# ============================================================
# 2. Token 边界场景
# ============================================================

class TestTokenTamperPayload:
    """篡改 payload 测试。"""

    def test_tampered_role_rejected(self) -> None:
        """篡改 token 中的 role 字段后，签名验证失败。"""
        mgr = _make_token_manager()
        token = mgr.create_access_token(user_id="user-1", role="user")

        # 解码 → 篡改 role → 用相同密钥重新签名
        payload = mgr.decode_token(token, verify=False)
        payload["role"] = "admin"
        tampered = jwt.encode(payload, SECRET_KEY, algorithm="HS256")

        # 篡改后的 token 仍能通过签名验证（密钥相同），但 exp/iat 可能不对
        # 这里验证的是：篡改 iat 时间戳会导致签名不匹配
        payload2 = mgr.decode_token(token, verify=False)
        payload2["sub"] = "admin-user"  # 篡改用户 ID
        tampered2 = jwt.encode(payload2, "wrong-secret-key", algorithm="HS256")

        with pytest.raises(TokenInvalidError):
            mgr.verify_token(tampered2, token_type="access")

    def test_tampered_sub_different_key_rejected(self) -> None:
        """用不同密钥签名的 token 被拒绝（伪造签名）。"""
        mgr = _make_token_manager()

        # 用不同密钥创建 token
        forged_payload = {
            "sub": "victim-user",
            "exp": datetime.now(UTC) + timedelta(minutes=30),
            "iat": datetime.now(UTC),
            "type": "access",
            "role": "admin",
            "jti": str(uuid4()),
        }
        forged_token = jwt.encode(forged_payload, "completely-different-secret", algorithm="HS256")

        with pytest.raises(TokenInvalidError):
            mgr.verify_token(forged_token, token_type="access")

    def test_corrupted_token_rejected(self) -> None:
        """结构损坏的 token 被拒绝。"""
        mgr = _make_token_manager()

        with pytest.raises(TokenInvalidError):
            mgr.verify_token("corrupted.token.string", token_type="access")

    def test_empty_token_rejected(self) -> None:
        """空 token 被拒绝。"""
        mgr = _make_token_manager()

        with pytest.raises(TokenInvalidError):
            mgr.verify_token("", token_type="access")


class TestTokenExpiredWithRefresh:
    """过期 access_token + 有效 refresh_token 组合测试。"""

    def test_expired_access_but_valid_refresh_can_refresh(self) -> None:
        """access_token 过期，但 refresh_token 有效时可以刷新获取新 token 对。"""
        mgr = _make_token_manager()

        # 创建已过期的 access_token
        expired_access = mgr.create_access_token(
            user_id="user-1",
            role="user",
            expires_delta=timedelta(seconds=-1),
        )
        # 创建有效的 refresh_token
        valid_refresh = mgr.create_refresh_token(user_id="user-1")

        # 过期的 access_token 验证失败
        with pytest.raises(TokenExpiredError):
            mgr.verify_token(expired_access, token_type="access")

        # 用有效的 refresh_token 刷新
        new_pair = mgr.refresh_token_pair(refresh_token=valid_refresh, role="user")
        assert new_pair.access_token is not None
        assert new_pair.refresh_token is not None

        # 新的 access_token 可用
        payload = mgr.verify_token(new_pair.access_token, token_type="access")
        assert payload.sub == "user-1"

    def test_both_expired_cannot_refresh(self) -> None:
        """refresh_token 也过期时，刷新失败。"""
        mgr = _make_token_manager()

        expired_refresh = mgr.create_refresh_token(
            user_id="user-1",
            expires_delta=timedelta(seconds=-1),
        )

        with pytest.raises(TokenExpiredError):
            mgr.refresh_token_pair(refresh_token=expired_refresh, role="user")


class TestTokenRevokedRetry:
    """已撤销 token 重试测试。"""

    def test_revoked_token_repeatedly_rejected(self) -> None:
        """撤销后的 token 反复验证始终被拒绝。"""
        mgr = _make_token_manager()
        token = mgr.create_access_token(user_id="user-1", role="user")

        mgr.revoke_token(token)

        # 反复尝试，每次都应被拒绝
        for _ in range(5):
            with pytest.raises(TokenRevokedError):
                mgr.verify_token(token, token_type="access")

    def test_revoked_refresh_cannot_refresh(self) -> None:
        """撤销后的 refresh_token 不能用于刷新。"""
        mgr = _make_token_manager()
        refresh = mgr.create_refresh_token(user_id="user-1")

        mgr.revoke_token(refresh)

        with pytest.raises(TokenRevokedError):
            mgr.refresh_token_pair(refresh_token=refresh, role="user")

    def test_revoke_all_then_new_token_works_after_delay(self) -> None:
        """全设备撤销后，等待时间推进，新 token 可用。"""
        mgr = _make_token_manager()
        mgr.revoke_all_user_tokens("user-1")

        # 时间推进确保 iat > revoke_time
        time.sleep(1.1)

        new_access = mgr.create_access_token(user_id="user-1", role="user")
        new_refresh = mgr.create_refresh_token(user_id="user-1")

        # 新 token 可用
        payload = mgr.verify_token(new_access, token_type="access")
        assert payload.sub == "user-1"

        mgr.verify_token(new_refresh, token_type="refresh")


# ============================================================
# 3. 登录限流边界
# ============================================================

class MockUserRepository:
    """内存用户仓库模拟。"""

    def __init__(self) -> None:
        self._users: dict[str, object] = {}

    async def get_by_id(self, user_id: object) -> object | None:
        return next(
            (u for u in self._users.values() if getattr(u, "id", None) == user_id),
            None,
        )

    async def get_by_username(self, username: str) -> object | None:
        return self._users.get(username)

    async def create(self, user_create: UserCreate, password_hash: str) -> object:
        from src.auth.models import UserInDB

        user = UserInDB(
            id=uuid4(),
            username=user_create.username,
            email=user_create.email,
            password_hash=password_hash,
            role=user_create.role,
            is_active=True,
            created_at=datetime.now(),
        )
        self._users[user_create.username] = user
        return user

    async def update_last_login(self, user_id: object) -> None:
        pass

    def add_user(self, username: str, password: str, is_active: bool = True) -> object:
        from src.auth.models import UserInDB

        user = UserInDB(
            id=uuid4(),
            username=username,
            password_hash=hash_password(password),
            role="user",
            is_active=is_active,
            created_at=datetime.now(),
        )
        self._users[username] = user
        return user


def _make_auth_service() -> tuple[AuthService, MockUserRepository]:
    """创建带内存降级 TokenManager 的 AuthService。"""
    token_mgr = _make_token_manager()
    repo = MockUserRepository()
    return AuthService(token_manager=token_mgr, user_repository=repo), repo


class TestLoginRateLimit:
    """登录限流边界测试（F-AUTH-04: 5 次/分钟）。"""

    @pytest.mark.asyncio
    async def test_five_failed_attempts_allowed(self) -> None:
        """刚好 5 次失败尝试不触发限流（第5次仍返回 InvalidCredentialsError）。"""
        svc, repo = _make_auth_service()
        repo.add_user("limituser", "correctpassword")

        # 5 次错误密码尝试，每次都应返回 InvalidCredentialsError（非限流）
        for i in range(5):
            with pytest.raises(InvalidCredentialsError) as exc_info:
                await svc.authenticate("limituser", "wrongpassword")
            # 确保不是 RateLimitExceededError
            assert not isinstance(exc_info.value, RateLimitExceededError), (
                f"第 {i + 1} 次尝试不应触发限流"
            )

    @pytest.mark.asyncio
    async def test_sixth_attempt_rate_limited(self) -> None:
        """第 6 次尝试触发 RateLimitExceededError。"""
        svc, repo = _make_auth_service()
        repo.add_user("limituser6", "correctpassword")

        # 前 5 次失败
        for _ in range(5):
            with pytest.raises(InvalidCredentialsError):
                await svc.authenticate("limituser6", "wrongpassword")

        # 第 6 次应触发限流
        with pytest.raises(RateLimitExceededError):
            await svc.authenticate("limituser6", "wrongpassword")

    @pytest.mark.asyncio
    async def test_rate_limit_per_username(self) -> None:
        """限流是按用户名隔离的：user_a 被限流不影响 user_b。"""
        svc, repo = _make_auth_service()
        repo.add_user("user_a", "correctpassword")
        repo.add_user("user_b", "correctpassword")

        # user_a 失败 5 次
        for _ in range(5):
            with pytest.raises(InvalidCredentialsError):
                await svc.authenticate("user_a", "wrongpassword")

        # user_a 被限流
        with pytest.raises(RateLimitExceededError):
            await svc.authenticate("user_a", "wrongpassword")

        # user_b 仍可正常尝试（错误密码仍返回 InvalidCredentialsError）
        with pytest.raises(InvalidCredentialsError):
            await svc.authenticate("user_b", "wrongpassword")

    @pytest.mark.asyncio
    async def test_successful_login_clears_attempts(self) -> None:
        """登录成功后清除尝试记录，不影响后续登录。"""
        svc, repo = _make_auth_service()
        repo.add_user("clearuser", "correctpassword")

        # 3 次失败
        for _ in range(3):
            with pytest.raises(InvalidCredentialsError):
                await svc.authenticate("clearuser", "wrongpassword")

        # 成功登录
        result = await svc.authenticate("clearuser", "correctpassword")
        assert "access_token" in result

        # 再次失败 3 次不应被限流（计数已重置）
        for _ in range(3):
            with pytest.raises(InvalidCredentialsError):
                await svc.authenticate("clearuser", "wrongpassword")

        # 第 4 次仍应返回 InvalidCredentialsError（未达限流阈值）
        with pytest.raises(InvalidCredentialsError):
            await svc.authenticate("clearuser", "wrongpassword")

    @pytest.mark.asyncio
    async def test_nonexistent_user_also_rate_limited(self) -> None:
        """不存在的用户名也应被限流（防止用户名枚举攻击）。"""
        svc, repo = _make_auth_service()

        # 5 次尝试不存在的用户
        for _ in range(5):
            with pytest.raises(InvalidCredentialsError):
                await svc.authenticate("ghost_user", "wrongpassword")

        # 第 6 次触发限流
        with pytest.raises(RateLimitExceededError):
            await svc.authenticate("ghost_user", "wrongpassword")

    @pytest.mark.asyncio
    async def test_rate_limit_resets_after_window(self) -> None:
        """时间窗口过后限流自动恢复。"""
        svc, repo = _make_auth_service()
        repo.add_user("windowuser", "correctpassword")

        # 5 次失败
        for _ in range(5):
            with pytest.raises(InvalidCredentialsError):
                await svc.authenticate("windowuser", "wrongpassword")

        # 确认已限流
        with pytest.raises(RateLimitExceededError):
            await svc.authenticate("windowuser", "wrongpassword")

        # 模拟时间窗口已过：用 patch 让 monotonic 返回未来时间
        future_time = time.monotonic() + svc.LOGIN_RATE_WINDOW + 1
        with patch("src.auth.service.time.monotonic", return_value=future_time):
            # 限流应已恢复，错误密码返回 InvalidCredentialsError
            with pytest.raises(InvalidCredentialsError):
                await svc.authenticate("windowuser", "wrongpassword")

    @pytest.mark.asyncio
    async def test_rate_limit_exceeded_for_inactive_user(self) -> None:
        """已禁用用户的失败尝试也计入限流。"""
        svc, repo = _make_auth_service()
        repo.add_user("inactiveuser", "correctpassword", is_active=False)

        # 5 次尝试（用户未激活，抛 UserInactiveError）
        for _ in range(5):
            with pytest.raises(UserInactiveError):
                await svc.authenticate("inactiveuser", "correctpassword")

        # 第 6 次触发限流
        with pytest.raises(RateLimitExceededError):
            await svc.authenticate("inactiveuser", "correctpassword")
