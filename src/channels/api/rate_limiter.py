"""差异化限流中间件。"""

from __future__ import annotations

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

DEFAULT_POLICIES: dict[RateLimitCategory, RateLimitPolicy] = {
    RateLimitCategory.AUTH: RateLimitPolicy(max_requests=10, window_seconds=60),
    RateLimitCategory.UPLOAD: RateLimitPolicy(max_requests=20, window_seconds=60),
    RateLimitCategory.DELETE: RateLimitPolicy(max_requests=30, window_seconds=60),
    RateLimitCategory.WRITE: RateLimitPolicy(max_requests=120, window_seconds=60),
    RateLimitCategory.READ: RateLimitPolicy(max_requests=300, window_seconds=60),
    RateLimitCategory.DEFAULT: RateLimitPolicy(max_requests=200, window_seconds=60),
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
            self.policies.setdefault(
                RateLimitCategory.DEFAULT, DEFAULT_POLICIES[RateLimitCategory.DEFAULT]
            )
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
