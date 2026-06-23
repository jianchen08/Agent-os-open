"""
认证 Token 管理单元测试。

覆盖 AC：
- AC-AUTH-03: Token 验证正确
- AC-AUTH-04: Token 过期返回 TOKEN_EXPIRED
- AC-AUTH-05: Token 撤销后无法访问
- AC-AUTH-07: 全设备登出撤销所有 Token
- AC-AUTH-11: Redis 不可用时降级内存

对应需求：F-AUTH-06~11
"""
import time
from datetime import timedelta

import pytest

from src.auth.token import TokenManager
from src.core.exceptions import (
    TokenExpiredError,
    TokenInvalidError,
    TokenRevokedError,
)


# ============================================================
# 辅助
# ============================================================

def _make_manager() -> TokenManager:
    """创建使用内存降级的 TokenManager（无 Redis）。"""
    return TokenManager(
        secret_key="test-secret-key-for-unit-test",
        algorithm="HS256",
        access_token_expire_minutes=30,
        refresh_token_expire_days=7,
        redis_url="redis://invalid:99999/0",  # 强制 Redis 不可用 → 降级内存
    )


# ============================================================
# AC-AUTH-06: Token 对生成
# ============================================================

class TestCreateTokenPair:
    """Token 对生成测试。"""

    def test_create_token_pair_returns_both_tokens(self) -> None:
        """Token 对包含 access_token 和 refresh_token。"""
        mgr = _make_manager()
        pair = mgr.create_token_pair(user_id="user-1", role="admin")

        assert pair.access_token is not None and len(pair.access_token) > 0
        assert pair.refresh_token is not None and len(pair.refresh_token) > 0
        assert pair.token_type == "bearer"
        assert pair.expires_in == 30 * 60  # 30 分钟 = 1800 秒

    def test_access_and_refresh_are_different(self) -> None:
        """access_token 与 refresh_token 不相同。"""
        mgr = _make_manager()
        pair = mgr.create_token_pair(user_id="user-1", role="user")
        assert pair.access_token != pair.refresh_token


# ============================================================
# AC-AUTH-03: Token 验证正确
# ============================================================

class TestVerifyToken:
    """Token 验证测试。"""

    def test_verify_access_token_success(self) -> None:
        """正确签名的 access_token 验证通过。"""
        mgr = _make_manager()
        token = mgr.create_access_token(user_id="user-1", role="user")

        payload = mgr.verify_token(token, token_type="access")
        assert payload.sub == "user-1"
        assert payload.role == "user"
        assert payload.type == "access"

    def test_verify_refresh_token_success(self) -> None:
        """正确签名的 refresh_token 验证通过。"""
        mgr = _make_manager()
        token = mgr.create_refresh_token(user_id="user-1")

        payload = mgr.verify_token(token, token_type="refresh")
        assert payload.sub == "user-1"
        assert payload.type == "refresh"

    def test_verify_token_wrong_type_raises(self) -> None:
        """用 access_token 当 refresh_token 使用应拒绝。"""
        mgr = _make_manager()
        access = mgr.create_access_token(user_id="user-1", role="user")

        with pytest.raises(TokenInvalidError):
            mgr.verify_token(access, token_type="refresh")

    def test_verify_invalid_token_raises(self) -> None:
        """无效 Token 签名应抛出 TokenInvalidError。"""
        mgr = _make_manager()

        with pytest.raises(TokenInvalidError):
            mgr.verify_token("not.a.valid.jwt", token_type="access")


# ============================================================
# AC-AUTH-04: Token 过期返回 TOKEN_EXPIRED
# ============================================================

class TestTokenExpiry:
    """Token 过期测试。"""

    def test_expired_access_token_raises(self) -> None:
        """过期的 access_token 抛出 TokenExpiredError。"""
        mgr = _make_manager()
        # 创建一个已过期的 token
        token = mgr.create_access_token(
            user_id="user-1",
            role="user",
            expires_delta=timedelta(seconds=-1),
        )

        with pytest.raises(TokenExpiredError):
            mgr.verify_token(token, token_type="access")


# ============================================================
# AC-AUTH-05: Token 撤销后无法访问
# ============================================================

class TestRevokeToken:
    """Token 撤销测试。"""

    def test_revoked_token_raises(self) -> None:
        """撤销后的 token 验证时抛出 TokenRevokedError。"""
        mgr = _make_manager()
        token = mgr.create_access_token(user_id="user-1", role="user")

        # 撤销前可以正常验证
        mgr.verify_token(token, token_type="access")

        # 撤销
        mgr.revoke_token(token)

        # 撤销后应拒绝
        with pytest.raises(TokenRevokedError):
            mgr.verify_token(token, token_type="access")

    def test_revoke_only_affects_target_token(self) -> None:
        """撤销单个 token 不影响其他 token。"""
        mgr = _make_manager()
        token_a = mgr.create_access_token(user_id="user-1", role="user")
        token_b = mgr.create_access_token(user_id="user-1", role="user")

        mgr.revoke_token(token_a)

        # token_b 仍可用
        payload = mgr.verify_token(token_b, token_type="access")
        assert payload.sub == "user-1"


# ============================================================
# AC-AUTH-07: 全设备登出撤销所有 Token
# ============================================================

class TestRevokeAllUserTokens:
    """全设备登出测试。"""

    def test_revoke_all_invalidates_all_user_tokens(self) -> None:
        """撤销用户所有 token 后，之前签发的 token 不可用。"""
        mgr = _make_manager()

        # 签发多个 token
        access = mgr.create_access_token(user_id="user-1", role="user")
        refresh = mgr.create_refresh_token(user_id="user-1")

        # 全设备撤销
        mgr.revoke_all_user_tokens("user-1")

        # access_token 被拒绝
        with pytest.raises(TokenRevokedError):
            mgr.verify_token(access, token_type="access")

        # refresh_token 也被拒绝
        with pytest.raises(TokenRevokedError):
            mgr.verify_token(refresh, token_type="refresh")

    def test_revoke_all_does_not_affect_other_users(self) -> None:
        """撤销用户 A 的 token 不影响用户 B。"""
        mgr = _make_manager()
        access_b = mgr.create_access_token(user_id="user-B", role="user")

        mgr.revoke_all_user_tokens("user-A")

        # 用户 B 的 token 仍可用
        payload = mgr.verify_token(access_b, token_type="access")
        assert payload.sub == "user-B"

    def test_new_token_after_revoke_all_works(self) -> None:
        """全设备撤销后，新签发的 token 可以正常使用（需时间推进）。"""
        mgr = _make_manager()
        mgr.revoke_all_user_tokens("user-1")

        # JWT iat 存储为整数秒，同一秒内签发的新 token 仍会被旧 revoke 拦截。
        # 模拟时间推进 1 秒后签发新 token，确保不被误拒。
        import time as _time
        _time.sleep(1.1)
        new_token = mgr.create_access_token(user_id="user-1", role="user")
        payload = mgr.verify_token(new_token, token_type="access")
        assert payload.sub == "user-1"


# ============================================================
# AC-AUTH-08: Token 刷新
# ============================================================

class TestRefreshTokenPair:
    """Token 刷新测试。"""

    def test_refresh_returns_new_pair(self) -> None:
        """refresh_token_pair 返回新的 Token 对。"""
        mgr = _make_manager()
        original = mgr.create_token_pair(user_id="user-1", role="user")

        new_pair = mgr.refresh_token_pair(
            refresh_token=original.refresh_token, role="user"
        )

        assert new_pair.access_token != original.access_token
        assert new_pair.refresh_token != original.refresh_token
        assert new_pair.token_type == "bearer"

        # 新 token 可用
        payload = mgr.verify_token(new_pair.access_token, token_type="access")
        assert payload.sub == "user-1"

    def test_old_refresh_revoked_after_refresh(self) -> None:
        """刷新后旧 refresh_token 被撤销。"""
        mgr = _make_manager()
        original = mgr.create_token_pair(user_id="user-1", role="user")

        mgr.refresh_token_pair(refresh_token=original.refresh_token, role="user")

        # 旧 refresh_token 不可用
        with pytest.raises(TokenRevokedError):
            mgr.verify_token(original.refresh_token, token_type="refresh")


# ============================================================
# AC-AUTH-11: Redis 不可用时降级内存
# ============================================================

class TestRedisFallback:
    """Redis 不可用降级测试。"""

    def test_redis_unavailable_falls_back_to_memory(self) -> None:
        """Redis 不可用时，撤销机制降级到内存存储。"""
        mgr = _make_manager()
        assert mgr._redis_available is False
        assert mgr._redis is None

    def test_memory_fallback_revoke_works(self) -> None:
        """内存模式下撤销仍然生效。"""
        mgr = _make_manager()
        token = mgr.create_access_token(user_id="user-1", role="user")

        mgr.revoke_token(token)

        with pytest.raises(TokenRevokedError):
            mgr.verify_token(token, token_type="access")

    def test_memory_fallback_revoke_all_works(self) -> None:
        """内存模式下全设备撤销仍然生效。"""
        mgr = _make_manager()
        token = mgr.create_access_token(user_id="user-1", role="user")

        mgr.revoke_all_user_tokens("user-1")

        with pytest.raises(TokenRevokedError):
            mgr.verify_token(token, token_type="access")
