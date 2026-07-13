"""差异化限流中间件。"""

from __future__ import annotations

import os as _os
import time
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "RateLimitCategory",
    "RateLimitPolicy",
    "TieredRateLimiter",
    "DEFAULT_POLICIES",
    "classify_request",
]


class RateLimitCategory(str, Enum):
    """限流类别枚举——不同类别使用不同的限流参数。"""

    READ = "read"
    WRITE = "write"
    DELETE = "delete"
    AUTH = "auth"
    UPLOAD = "upload"
    DEFAULT = "default"


@dataclass(frozen=True)
class RateLimitPolicy:
    """限流策略：窗口期内允许的最大请求数。"""

    max_requests: int
    window_seconds: int


# ── 默认限流策略 ──────────────────────────────────────────
# 设计原则：
#   AUTH   最严格 — 防暴力破解（10 次/分钟）
#   UPLOAD 较严格 — 防大流量滥用（20 次/分钟）
#   DELETE 较严格 — 不可逆操作需保护（30 次/分钟）
#   WRITE  适中   — 写操作消耗资源较多（120 次/分钟）
#   READ   最宽松 — 读操作可高并发（300 次/分钟）
#   DEFAULT 兜底  — 未分类请求（200 次/分钟）
#
# 环境变量覆盖：测试/CI 环境可通过 RATE_LIMIT_AUTH_MAX 等环境变量放宽限流，
# 避免 E2E 测试并发登录触发 429。生产环境不设这些变量，保持默认严格策略。


def _policy_with_env_override(category: str, default_max: int, window: int = 60) -> RateLimitPolicy:
    """支持环境变量覆盖限流阈值（RATE_LIMIT_<CATEGORY>_MAX）。"""
    env_key = f"RATE_LIMIT_{category}_MAX"
    max_requests = int(_os.environ.get(env_key, default_max))
    return RateLimitPolicy(max_requests=max_requests, window_seconds=window)


DEFAULT_POLICIES: dict[RateLimitCategory, RateLimitPolicy] = {
    RateLimitCategory.AUTH: _policy_with_env_override("AUTH", 10),
    RateLimitCategory.UPLOAD: _policy_with_env_override("UPLOAD", 20),
    RateLimitCategory.DELETE: _policy_with_env_override("DELETE", 30),
    RateLimitCategory.WRITE: _policy_with_env_override("WRITE", 120),
    RateLimitCategory.READ: _policy_with_env_override("READ", 300),
    RateLimitCategory.DEFAULT: _policy_with_env_override("DEFAULT", 200),
}


# ── 请求分类 ──────────────────────────────────────────────

# 认证路径前缀（优先级最高，防止暴力登录/注册）
_AUTH_PATH_PREFIXES = ("/api/auth/", "/api/v1/auth/")

# 上传路径关键词（优先级次高，防止大流量文件滥用）
_UPLOAD_PATH_KEYWORDS = ("/upload", "/import")


def classify_request(method: str, path: str) -> RateLimitCategory:
    """将 HTTP 请求分类到对应的限流类别。"""
    path_lower = path.lower()

    # 1. 认证路径优先
    if any(path_lower.startswith(prefix) for prefix in _AUTH_PATH_PREFIXES):
        return RateLimitCategory.AUTH

    # 2. 上传路径次之
    if any(keyword in path_lower for keyword in _UPLOAD_PATH_KEYWORDS):
        return RateLimitCategory.UPLOAD

    # 3. 按 HTTP 方法分类
    method_upper = method.upper()
    if method_upper == "GET":
        return RateLimitCategory.READ
    if method_upper in ("POST", "PUT", "PATCH"):
        return RateLimitCategory.WRITE
    if method_upper == "DELETE":
        return RateLimitCategory.DELETE

    return RateLimitCategory.DEFAULT


# ── 差异化限流器 ──────────────────────────────────────────


class TieredRateLimiter:
    """差异化限流器。"""

    def __init__(
        self,
        policies: dict[RateLimitCategory, RateLimitPolicy] | None = None,
    ) -> None:
        """初始化限流器。"""
        if policies is None:
            self.policies = dict(DEFAULT_POLICIES)
        else:
            self.policies = dict(policies)
            # 确保 DEFAULT 策略始终存在
            self.policies.setdefault(RateLimitCategory.DEFAULT, DEFAULT_POLICIES[RateLimitCategory.DEFAULT])
        self._hits: dict[tuple[str, RateLimitCategory], list[float]] = {}

    def is_allowed(self, key: str, category: RateLimitCategory) -> bool:
        """检查指定 key 在指定类别下是否被允许。"""
        policy = self.policies.get(category, self.policies[RateLimitCategory.DEFAULT])
        now = time.time()
        cutoff = now - policy.window_seconds

        hit_key = (key, category)
        hits = self._hits.get(hit_key, [])
        hits = [t for t in hits if t > cutoff]

        if len(hits) >= policy.max_requests:
            self._hits[hit_key] = hits
            return False

        hits.append(now)
        self._hits[hit_key] = hits
        return True

    def is_request_allowed(self, key: str, method: str, path: str) -> bool:
        """便捷方法：自动分类并检查限流。"""
        category = classify_request(method, path)
        return self.is_allowed(key, category)


def load_policies_from_yaml(config_path: str) -> dict[RateLimitCategory, RateLimitPolicy]:
    """从 YAML 配置文件加载限流策略。"""
    import yaml  # noqa: PLC0415

    with open(config_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    policies: dict[RateLimitCategory, RateLimitPolicy] = {}
    for cat_name, params in raw.items():
        category = RateLimitCategory(cat_name)
        policies[category] = RateLimitPolicy(
            max_requests=params["max_requests"],
            window_seconds=params["window_seconds"],
        )
    return policies


# 全局差异化限流器实例（使用默认策略）
tiered_rate_limiter = TieredRateLimiter()
