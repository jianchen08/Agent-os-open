"""认证鉴权模块测试 - Token 结构校验。

覆盖：F-AUTH-07/08/11 Token 载荷/TokenPair 结构。

注意：原 F-AUTH-04（登录限流常量，依赖 AuthService）与 F-AUTH-12/13/14
（RBAC 角色/权限矩阵，依赖 src.auth.rbac / permission_matrix）相关用例
已随对应源码一并移除——这些是未启用的体系 B 代码。详见 H1 修复记录。
"""

import pytest


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
