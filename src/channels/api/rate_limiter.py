"""差异化限流中间件。

按 API 类别（读/写/删除/认证/文件上传）设置不同的限流参数，
替代原来全端点统一的限流策略。

分类规则（优先级从高到低）::

    1. 路径匹配 AUTH 模式   → AUTH（最严格）
    2. 路径匹配 UPLOAD 模式 → UPLOAD
    3. HTTP 方法 GET        → READ（最宽松）
    4. HTTP 方法 POST/PUT/PATCH → WRITE
    5. HTTP 方法 DELETE     → DELETE
    6. 其他                  → DEFAULT

使用方式::

    from channels.api.rate_limiter import TieredRateLimiter, classify_request

    limiter = TieredRateLimiter()
    if not limiter.is_request_allowed(client_ip, method, path):
        return JSONResponse(status_code=429, content={"error": "请求过于频繁"})
"""

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
    """限流策略：窗口期内允许的最大请求数。

    Attributes:
        max_requests: 窗口期内允许的最大请求数
        window_seconds: 窗口时间（秒）
    """

    max_requests: int
    window_seconds: int


# ── 默认限流策略 ──────────────────────────────────────────
#
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
    """将 HTTP 请求分类到对应的限流类别。

    分类优先级：路径匹配 > HTTP 方法。

    Args:
        method: HTTP 方法（GET/POST/PUT/PATCH/DELETE 等）
        path: 请求路径

    Returns:
        对应的限流类别
    """
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
    """差异化限流器。

    每个 (key, category) 组合维护独立的滑动窗口计数器，
    不同类别使用不同的限流策略。

    Attributes:
        policies: 类别→策略映射
        _hits: (key, category) → 请求时间戳列表
    """

    def __init__(
        self,
        policies: dict[RateLimitCategory, RateLimitPolicy] | None = None,
    ) -> None:
        """初始化限流器。

        Args:
            policies: 自定义策略；为 None 时使用 DEFAULT_POLICIES。
                      无论传入什么，都会自动补全 DEFAULT 策略。
        """
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
        """检查指定 key 在指定类别下是否被允许。

        Args:
            key: 限流标识（通常是客户端 IP）
            category: 限流类别

        Returns:
            True 表示放行，False 表示超限
        """
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
        """便捷方法：自动分类并检查限流。

        Args:
            key: 限流标识（通常是客户端 IP）
            method: HTTP 方法
            path: 请求路径

        Returns:
            True 表示放行，False 表示超限
        """
        category = classify_request(method, path)
        return self.is_allowed(key, category)


def load_policies_from_yaml(config_path: str) -> dict[RateLimitCategory, RateLimitPolicy]:
    """从 YAML 配置文件加载限流策略。

    配置文件格式见 config/system/rate_limit_config.yaml。

    Args:
        config_path: YAML 配置文件路径

    Returns:
        类别→策略映射
    """
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
