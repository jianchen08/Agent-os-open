"""差异化限流策略测试。

覆盖场景：
- 请求分类（GET/POST/DELETE/AUTH/UPLOAD）
- 各类别独立限流
- 超限拦截
- 自定义策略
"""

import time

import pytest

from channels.api.rate_limiter import (
    DEFAULT_POLICIES,
    RateLimitCategory,
    RateLimitPolicy,
    TieredRateLimiter,
    classify_request,
)


# ============================================================
# classify_request 单元测试
# ============================================================


class TestClassifyRequest:
    """测试请求分类逻辑。"""

    def test_get_request_classified_as_read(self) -> None:
        assert classify_request("GET", "/api/v1/threads") == RateLimitCategory.READ

    def test_post_request_classified_as_write(self) -> None:
        assert classify_request("POST", "/api/v1/threads") == RateLimitCategory.WRITE

    def test_put_request_classified_as_write(self) -> None:
        assert classify_request("PUT", "/api/v1/config/system") == RateLimitCategory.WRITE

    def test_patch_request_classified_as_write(self) -> None:
        assert classify_request("PATCH", "/api/v1/tasks/abc") == RateLimitCategory.WRITE

    def test_delete_request_classified_as_delete(self) -> None:
        assert classify_request("DELETE", "/api/v1/threads/123") == RateLimitCategory.DELETE

    def test_auth_login_path_classified_as_auth(self) -> None:
        assert classify_request("POST", "/api/auth/login") == RateLimitCategory.AUTH

    def test_auth_v1_login_path_classified_as_auth(self) -> None:
        assert classify_request("POST", "/api/v1/auth/login") == RateLimitCategory.AUTH

    def test_auth_register_classified_as_auth(self) -> None:
        assert classify_request("POST", "/api/auth/register") == RateLimitCategory.AUTH

    def test_upload_artifact_classified_as_upload(self) -> None:
        assert classify_request("POST", "/api/v1/artifacts/upload") == RateLimitCategory.UPLOAD

    def test_upload_import_classified_as_upload(self) -> None:
        assert classify_request("POST", "/api/v1/memory/import") == RateLimitCategory.UPLOAD

    def test_auth_path_priority_over_method(self) -> None:
        """认证路径优先于方法分类——POST /api/auth/login 应为 AUTH 而非 WRITE。"""
        assert classify_request("POST", "/api/auth/login") == RateLimitCategory.AUTH

    def test_upload_priority_over_write(self) -> None:
        """上传路径优先于写分类——POST /api/v1/artifacts/upload 应为 UPLOAD 而非 WRITE。"""
        assert classify_request("POST", "/api/v1/artifacts/upload") == RateLimitCategory.UPLOAD

    def test_unknown_method_falls_to_default(self) -> None:
        assert classify_request("HEAD", "/api/v1/threads") == RateLimitCategory.DEFAULT


# ============================================================
# TieredRateLimiter 单元测试
# ============================================================


class TestTieredRateLimiter:
    """测试差异化限流器。"""

    def test_allows_within_limit(self) -> None:
        """窗口内未超限应放行。"""
        limiter = TieredRateLimiter()
        assert limiter.is_allowed("192.168.1.1", RateLimitCategory.READ) is True

    def test_blocks_over_read_limit(self) -> None:
        """读操作超过限制应拦截。"""
        policy = {RateLimitCategory.READ: RateLimitPolicy(max_requests=3, window_seconds=60)}
        limiter = TieredRateLimiter(policy)
        for _ in range(3):
            assert limiter.is_allowed("10.0.0.1", RateLimitCategory.READ) is True
        assert limiter.is_allowed("10.0.0.1", RateLimitCategory.READ) is False

    def test_different_categories_have_independent_limits(self) -> None:
        """不同类别使用独立计数器——READ 超限不影响 WRITE。"""
        policies = {
            RateLimitCategory.READ: RateLimitPolicy(max_requests=2, window_seconds=60),
            RateLimitCategory.WRITE: RateLimitPolicy(max_requests=5, window_seconds=60),
        }
        limiter = TieredRateLimiter(policies)

        # READ 打满
        assert limiter.is_allowed("1.1.1.1", RateLimitCategory.READ) is True
        assert limiter.is_allowed("1.1.1.1", RateLimitCategory.READ) is True
        assert limiter.is_allowed("1.1.1.1", RateLimitCategory.READ) is False

        # WRITE 仍然可用
        assert limiter.is_allowed("1.1.1.1", RateLimitCategory.WRITE) is True

    def test_different_keys_have_independent_limits(self) -> None:
        """不同 IP 使用独立计数器。"""
        policy = {RateLimitCategory.READ: RateLimitPolicy(max_requests=1, window_seconds=60)}
        limiter = TieredRateLimiter(policy)
        assert limiter.is_allowed("1.1.1.1", RateLimitCategory.READ) is True
        assert limiter.is_allowed("1.1.1.1", RateLimitCategory.READ) is False
        assert limiter.is_allowed("2.2.2.2", RateLimitCategory.READ) is True

    def test_auth_category_more_restrictive_than_read(self) -> None:
        """认证类别限流更严格——使用默认策略验证。"""
        limiter = TieredRateLimiter()
        auth_policy = DEFAULT_POLICIES[RateLimitCategory.AUTH]
        read_policy = DEFAULT_POLICIES[RateLimitCategory.READ]
        assert auth_policy.max_requests < read_policy.max_requests

    def test_is_request_allowed_convenience_method(self) -> None:
        """便捷方法应自动分类并检查。"""
        policy = {RateLimitCategory.AUTH: RateLimitPolicy(max_requests=1, window_seconds=60)}
        limiter = TieredRateLimiter(policy)
        assert limiter.is_request_allowed("3.3.3.3", "POST", "/api/auth/login") is True
        assert limiter.is_request_allowed("3.3.3.3", "POST", "/api/auth/login") is False

    def test_falls_back_to_default_for_unknown_category(self) -> None:
        """未配置的类别回退到 DEFAULT。"""
        policy = {
            RateLimitCategory.DEFAULT: RateLimitPolicy(max_requests=1, window_seconds=60),
        }
        limiter = TieredRateLimiter(policy)
        # READ 未配置 → 回退到 DEFAULT
        assert limiter.is_allowed("4.4.4.4", RateLimitCategory.READ) is True
        assert limiter.is_allowed("4.4.4.4", RateLimitCategory.READ) is False

    def test_window_expiry_allows_again(self) -> None:
        """窗口过期后应重新放行。"""
        policy = {RateLimitCategory.READ: RateLimitPolicy(max_requests=1, window_seconds=1)}
        limiter = TieredRateLimiter(policy)
        assert limiter.is_allowed("5.5.5.5", RateLimitCategory.READ) is True
        assert limiter.is_allowed("5.5.5.5", RateLimitCategory.READ) is False
        time.sleep(1.1)
        assert limiter.is_allowed("5.5.5.5", RateLimitCategory.READ) is True

    def test_default_always_present(self) -> None:
        """即使自定义策略不含 DEFAULT，也应自动补全。"""
        policy = {RateLimitCategory.READ: RateLimitPolicy(max_requests=10, window_seconds=60)}
        limiter = TieredRateLimiter(policy)
        # DEFAULT 应可用
        assert limiter.is_allowed("6.6.6.6", RateLimitCategory.DEFAULT) is True


# ============================================================
# 边界场景补充
# ============================================================

class TestRateLimiterEdgeCases:
    """差异化限流边界场景。"""

    def test_zero_max_requests_blocks_immediately(self) -> None:
        """max_requests=0 时任何请求都被拦截。"""
        policy = {RateLimitCategory.READ: RateLimitPolicy(max_requests=0, window_seconds=60)}
        limiter = TieredRateLimiter(policy)
        assert limiter.is_allowed("1.1.1.1", RateLimitCategory.READ) is False

    def test_large_window_allows_many(self) -> None:
        """大窗口内允许大量请求。"""
        policy = {RateLimitCategory.READ: RateLimitPolicy(max_requests=1000, window_seconds=3600)}
        limiter = TieredRateLimiter(policy)
        for _ in range(1000):
            assert limiter.is_allowed("2.2.2.2", RateLimitCategory.READ) is True
        assert limiter.is_allowed("2.2.2.2", RateLimitCategory.READ) is False

    def test_short_window_expires_quickly(self) -> None:
        """短窗口快速过期。"""
        import time as _time
        policy = {RateLimitCategory.READ: RateLimitPolicy(max_requests=2, window_seconds=1)}
        limiter = TieredRateLimiter(policy)
        assert limiter.is_allowed("3.3.3.3", RateLimitCategory.READ) is True
        assert limiter.is_allowed("3.3.3.3", RateLimitCategory.READ) is True
        assert limiter.is_allowed("3.3.3.3", RateLimitCategory.READ) is False
        _time.sleep(1.1)
        assert limiter.is_allowed("3.3.3.3", RateLimitCategory.READ) is True

    def test_all_categories_have_policies(self) -> None:
        """所有默认策略类别都有定义。"""
        expected_categories = {
            RateLimitCategory.READ,
            RateLimitCategory.WRITE,
            RateLimitCategory.DELETE,
            RateLimitCategory.AUTH,
            RateLimitCategory.UPLOAD,
            RateLimitCategory.DEFAULT,
        }
        assert set(DEFAULT_POLICIES.keys()) == expected_categories

    def test_auth_most_restrictive(self) -> None:
        """AUTH 类别是最严格的。"""
        auth = DEFAULT_POLICIES[RateLimitCategory.AUTH]
        upload = DEFAULT_POLICIES[RateLimitCategory.UPLOAD]
        delete = DEFAULT_POLICIES[RateLimitCategory.DELETE]
        write = DEFAULT_POLICIES[RateLimitCategory.WRITE]
        read = DEFAULT_POLICIES[RateLimitCategory.READ]
        assert auth.max_requests < upload.max_requests
        assert auth.max_requests < delete.max_requests
        assert auth.max_requests < write.max_requests
        assert auth.max_requests < read.max_requests

    def test_read_most_permissive(self) -> None:
        """READ 类别是最宽松的。"""
        read = DEFAULT_POLICIES[RateLimitCategory.READ]
        for cat in (RateLimitCategory.WRITE, RateLimitCategory.DELETE,
                     RateLimitCategory.AUTH, RateLimitCategory.UPLOAD):
            assert read.max_requests > DEFAULT_POLICIES[cat].max_requests

    def test_is_request_allowed_uses_correct_category(self) -> None:
        """is_request_allowed 正确分类并限流。"""
        policy = {RateLimitCategory.AUTH: RateLimitPolicy(max_requests=1, window_seconds=60)}
        limiter = TieredRateLimiter(policy)
        # POST /api/auth/login → AUTH，第一次放行，第二次拦截
        assert limiter.is_request_allowed("a.a.a.a", "POST", "/api/auth/login") is True
        assert limiter.is_request_allowed("a.a.a.a", "POST", "/api/auth/login") is False
        # GET /api/config → READ，不应受 AUTH 限流影响（使用默认策略放行）
        k = "a.a.a.a"
        # 注意：READ 使用默认策略（200/min），可放行
        assert limiter.is_request_allowed(k, "GET", "/api/config") is True

    def test_custom_policy_override(self) -> None:
        """自定义策略覆盖默认。"""
        policy = {RateLimitCategory.READ: RateLimitPolicy(max_requests=5, window_seconds=60)}
        limiter = TieredRateLimiter(policy)
        for _ in range(5):
            assert limiter.is_allowed("b.b.b.b", RateLimitCategory.READ) is True
        assert limiter.is_allowed("b.b.b.b", RateLimitCategory.READ) is False

    def test_classify_unknown_method(self) -> None:
        """未知 HTTP 方法分类到 DEFAULT。"""
        assert classify_request("OPTIONS", "/api/v1/threads") == RateLimitCategory.DEFAULT
        assert classify_request("TRACE", "/api/v1/threads") == RateLimitCategory.DEFAULT
