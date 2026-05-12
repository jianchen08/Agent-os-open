"""
API 模块

提供 RESTful API 和 WebSocket 接口
"""

from src.api.dependencies import (
    CommonQueryParams,
    FilterParams,
    PaginationParams,
    SortingParams,
    generate_trace_id,
)
from src.api.errors import (
    ErrorCode,
    ErrorResponse,
    create_error_response,
    get_error_message,
    is_valid_error_code,
)
from src.api.main import app, create_app
from src.api.rate_limit import (
    RateLimitConfig,
    RateLimitHeaders,
    RateLimitKeyStrategy,
    RateLimitStrategy,
    RateLimitWhitelist,
    SlidingWindowRateLimiter,
    TokenBucketRateLimiter,
    get_rate_limit_key,
    is_whitelisted,
)
from src.core.errors import get_http_status
from src.core.exceptions import (
    BaseAppException as BaseError,
)
from src.core.exceptions import (
    CacheException as CacheConnectionError,
)
from src.core.exceptions import (
    ConfigurationException as ConfigurationError,
)
from src.core.exceptions import (
    DatabaseException as DatabaseConnectionError,
)
from src.core.exceptions import (
    ValidationException as ValidationError,
)

__all__ = [
    # 应用
    "create_app",
    "app",
    # 依赖
    "generate_trace_id",
    "PaginationParams",
    "SortingParams",
    "FilterParams",
    "CommonQueryParams",
    # 错误
    "ErrorResponse",
    "ErrorCode",
    "get_http_status",
    "get_error_message",
    "create_error_response",
    "is_valid_error_code",
    "BaseError",
    "DatabaseConnectionError",
    "CacheConnectionError",
    "ConfigurationError",
    "ValidationError",
    # 限流
    "RateLimitConfig",
    "RateLimitStrategy",
    "RateLimitHeaders",
    "RateLimitKeyStrategy",
    "SlidingWindowRateLimiter",
    "TokenBucketRateLimiter",
    "get_rate_limit_key",
    "RateLimitWhitelist",
    "is_whitelisted",
]
