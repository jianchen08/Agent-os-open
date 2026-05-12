"""
限流机制模块

实现滑动窗口和令牌桶限流算法，支持分布式场景。
"""

import time
from enum import Enum

from pydantic import BaseModel, Field


class RateLimitStrategy(str, Enum):
    """限流策略类型"""

    FIXED_WINDOW = "fixed_window"  # 固定窗口
    SLIDING_WINDOW = "sliding_window"  # 滑动窗口
    TOKEN_BUCKET = "token_bucket"  # 令牌桶
    LEAKY_BUCKET = "leaky_bucket"  # 漏桶


class RateLimitConfig(BaseModel):
    """限流配置"""

    requests_per_second: int | None = Field(None, description="每秒请求数限制")
    requests_per_minute: int = Field(default=60, description="每分钟请求数限制")
    requests_per_hour: int = Field(default=1000, description="每小时请求数限制")
    requests_per_day: int | None = Field(None, description="每天请求数限制")
    burst_size: int = Field(default=10, description="突发请求容量")
    strategy: RateLimitStrategy = Field(
        default=RateLimitStrategy.SLIDING_WINDOW, description="限流策略"
    )


class RateLimitHeaders(BaseModel):
    """限流响应头"""

    x_ratelimit_limit: int = Field(..., description="当前时间窗口的请求限制数")
    x_ratelimit_remaining: int = Field(..., description="当前时间窗口剩余请求数")
    x_ratelimit_reset: int = Field(..., description="限制重置的Unix时间戳")
    x_ratelimit_window: int = Field(..., description="时间窗口大小（秒）")
    retry_after: int | None = Field(None, description="建议重试等待时间（秒）")


class RateLimitKeyStrategy(str, Enum):
    """限流键策略"""

    USER = "user"  # 按用户ID
    IP = "ip"  # 按IP地址
    ENDPOINT = "endpoint"  # 按端点
    USER_ENDPOINT = "user_endpoint"  # 用户+端点组合
    IP_ENDPOINT = "ip_endpoint"  # IP+端点组合


class SlidingWindowRateLimiter:
    """滑动窗口限流器"""

    def __init__(self, config: RateLimitConfig):
        self.config = config
        self.window_size = 60  # 秒
        self.requests: dict[str, list[float]] = {}  # key -> [timestamps]

    def is_allowed(self, key: str) -> tuple[bool, RateLimitHeaders]:
        """
        检查请求是否被允许

        Args:
            key: 限流键（如用户ID、IP地址）

        Returns:
            (是否允许, 限流响应头)
        """
        now = time.time()
        window_start = now - self.window_size

        # 清理过期请求
        if key in self.requests:
            self.requests[key] = [ts for ts in self.requests[key] if ts > window_start]
        else:
            self.requests[key] = []

        current_count = len(self.requests[key])
        limit = self.config.requests_per_minute
        remaining = max(0, limit - current_count)
        reset_time = int(now + self.window_size)

        headers = RateLimitHeaders(
            x_ratelimit_limit=limit,
            x_ratelimit_remaining=remaining,
            x_ratelimit_reset=reset_time,
            x_ratelimit_window=self.window_size,
        )

        if current_count >= limit:
            # 计算需要等待的时间
            if self.requests[key]:
                oldest_request = min(self.requests[key])
                wait_time = int(oldest_request - window_start) + 1
                headers.retry_after = max(1, wait_time)
            else:
                headers.retry_after = 1
            return False, headers

        self.requests[key].append(now)
        headers.x_ratelimit_remaining = remaining - 1
        return True, headers

    def get_current_count(self, key: str) -> int:
        """获取当前窗口内的请求数"""
        now = time.time()
        window_start = now - self.window_size

        if key not in self.requests:
            return 0

        return len([ts for ts in self.requests[key] if ts > window_start])

    def reset(self, key: str) -> None:
        """重置指定键的限流计数"""
        if key in self.requests:
            del self.requests[key]


class TokenBucketRateLimiter:
    """令牌桶限流器"""

    def __init__(self, config: RateLimitConfig):
        self.config = config
        self.buckets: dict[str, dict] = {}

    def is_allowed(self, key: str) -> tuple[bool, RateLimitHeaders]:
        """检查请求是否被允许"""
        now = time.time()

        if key not in self.buckets:
            self.buckets[key] = {
                "tokens": float(self.config.burst_size),
                "last_update": now,
            }

        bucket = self.buckets[key]

        # 补充令牌
        elapsed = now - bucket["last_update"]
        refill_rate = self.config.requests_per_minute / 60.0
        new_tokens = elapsed * refill_rate
        bucket["tokens"] = min(
            float(self.config.burst_size), bucket["tokens"] + new_tokens
        )
        bucket["last_update"] = now

        # 检查是否有可用令牌
        if bucket["tokens"] >= 1:
            bucket["tokens"] -= 1
            remaining = int(bucket["tokens"])
            return True, RateLimitHeaders(
                x_ratelimit_limit=self.config.requests_per_minute,
                x_ratelimit_remaining=remaining,
                x_ratelimit_reset=int(now + 60),
                x_ratelimit_window=60,
            )

        # 计算需要等待的时间
        wait_time = (1 - bucket["tokens"]) / refill_rate
        return False, RateLimitHeaders(
            x_ratelimit_limit=self.config.requests_per_minute,
            x_ratelimit_remaining=0,
            x_ratelimit_reset=int(now + wait_time),
            x_ratelimit_window=60,
            retry_after=int(wait_time) + 1,
        )

    def get_available_tokens(self, key: str) -> float:
        """获取当前可用令牌数"""
        if key not in self.buckets:
            return float(self.config.burst_size)

        now = time.time()
        bucket = self.buckets[key]
        elapsed = now - bucket["last_update"]
        refill_rate = self.config.requests_per_minute / 60.0
        new_tokens = elapsed * refill_rate

        return min(float(self.config.burst_size), bucket["tokens"] + new_tokens)


def get_rate_limit_key(
    strategy: RateLimitKeyStrategy,
    user_id: str | None,
    client_ip: str,
    endpoint: str,
) -> str:
    """生成限流键"""
    if strategy == RateLimitKeyStrategy.USER and user_id:
        return f"user:{user_id}"
    elif strategy == RateLimitKeyStrategy.IP:
        return f"ip:{client_ip}"
    elif strategy == RateLimitKeyStrategy.ENDPOINT:
        return f"endpoint:{endpoint}"
    elif strategy == RateLimitKeyStrategy.USER_ENDPOINT and user_id:
        return f"user:{user_id}:endpoint:{endpoint}"
    elif strategy == RateLimitKeyStrategy.IP_ENDPOINT:
        return f"ip:{client_ip}:endpoint:{endpoint}"
    else:
        return f"ip:{client_ip}"


class RateLimitWhitelist(BaseModel):
    """限流白名单配置"""

    user_ids: list[str] = Field(default_factory=list, description="白名单用户ID")
    ip_addresses: list[str] = Field(default_factory=list, description="白名单IP地址")
    api_keys: list[str] = Field(default_factory=list, description="白名单API密钥")


def is_whitelisted(
    whitelist: RateLimitWhitelist,
    user_id: str | None,
    client_ip: str,
    api_key: str | None,
) -> bool:
    """检查是否在白名单中"""
    if user_id and user_id in whitelist.user_ids:
        return True
    if client_ip in whitelist.ip_addresses:
        return True
    if api_key and api_key in whitelist.api_keys:
        return True
    return False
