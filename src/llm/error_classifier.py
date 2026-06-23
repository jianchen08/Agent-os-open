"""LLM 错误分类器 — 把 litellm / 中转站的各种异常翻译成统一类型。

这是错误处理的唯一入口：不同 provider（官方 deepseek/minimax、openai 兼容中转站
apigo/yichengc）报错格式千差万别，本模块负责把它们统一翻译成 ErrorKind，
让上层（KeyPoolAdapter / KeySlot）按统一策略处理，不再为每种异常单独写 except 分支。

设计原则：
- 这里是唯一嗅探异常字符串的地方。新增中转站/错误消息只改这里。
- 上层只看 ErrorKind，不依赖 litellm 异常类型。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class ErrorKind(Enum):
    """LLM 调用错误的统一分类。

    分类依据是「该如何处理」，而不是「原始异常类型」。
    """

    RATE_LIMIT = "rate_limit"          # 限流（429，含 group limit / upstream rate）
    QUOTA_EXHAUSTED = "quota_exhausted"  # 配额耗尽（余额不足、月度上限）
    AUTH_FAILED = "auth_failed"        # 认证失败（401）
    SERVICE_DOWN = "service_down"      # 服务不可用（503，上游临时挂，值得重试）
    SERVER_ERROR = "server_error"      # 服务器内部错误（500）
    NETWORK = "network"                # 网络错误（超时、连接失败）
    BAD_REQUEST = "bad_request"        # 请求参数错误（400，不可重试）
    UNKNOWN = "unknown"                # 未分类


@dataclass(frozen=True)
class ErrorInfo:
    """翻译后的错误信息。

    Attributes:
        kind: 统一错误类型
        retry_after: 服务端建议的等待秒数（Retry-After 头），无则 None
        original: 原始异常，用于上层 re-raise 时保留异常链
    """

    kind: ErrorKind
    retry_after: float | None = None
    original: BaseException | None = None


# 配额耗尽的关键词（400 BadRequest 但语义上是配额问题）
_QUOTA_KEYWORDS = (
    "insufficient", "balance", "quota", "exceeded",
    "limit", "额度", "上限", "用完", "余额", "不足",
)

# 限流类错误消息的关键词（区分于配额）
_RATE_LIMIT_KEYWORDS = (
    "rate limit", "rate_limit", "requests-per-minute",
    "too many requests", "请求过快", "频率",
)


def _extract_retry_after(exc: BaseException) -> float | None:
    """从异常里提取 Retry-After 建议秒数（litellm 部分异常会带）。"""
    for attr in ("retry_after", "response_headers"):
        val = getattr(exc, attr, None)
        if isinstance(val, (int, float)) and val > 0:
            return float(val)
    return None


def classify_error(exc: BaseException) -> ErrorInfo:
    """把任意异常翻译成 ErrorInfo。

    优先用异常类名（litellm/openai 的类型层次），其次嗅探消息字符串
    （中转站的自定义错误消息）。

    Args:
        exc: litellm.acompletion 抛出的异常

    Returns:
        包含 ErrorKind 和可选 retry_after 的 ErrorInfo
    """
    retry_after = _extract_retry_after(exc)
    type_name = type(exc).__name__
    msg = str(exc).lower()

    # 1. 按异常类型名直接映射（litellm/openai 标准类型）
    if "AuthenticationError" in type_name:
        return ErrorInfo(ErrorKind.AUTH_FAILED, retry_after, exc)

    if "RateLimitError" in type_name:
        # 429 家族：可能是 RPM 限流，也可能是 group/upstream 限流
        return ErrorInfo(ErrorKind.RATE_LIMIT, retry_after, exc)

    if "BudgetExceededError" in type_name:
        return ErrorInfo(ErrorKind.QUOTA_EXHAUSTED, 3600.0, exc)

    if "Timeout" in type_name or "APITimeoutError" in type_name:
        return ErrorInfo(ErrorKind.NETWORK, retry_after, exc)

    if "APIConnectionError" in type_name:
        return ErrorInfo(ErrorKind.NETWORK, retry_after, exc)

    if "ServiceUnavailableError" in type_name:
        return ErrorInfo(ErrorKind.SERVICE_DOWN, retry_after, exc)

    if "InternalServerError" in type_name:
        # 500 家族：多数上游抖动，按 SERVICE_DOWN 重试更合理
        return ErrorInfo(ErrorKind.SERVICE_DOWN, retry_after, exc)

    # 2. BadRequestError（400）需进一步判定：配额类消息 → QUOTA，否则真参数错
    if "BadRequestError" in type_name:
        if any(kw.lower() in msg for kw in _QUOTA_KEYWORDS):
            return ErrorInfo(ErrorKind.QUOTA_EXHAUSTED, 3600.0, exc)
        return ErrorInfo(ErrorKind.BAD_REQUEST, None, exc)

    # 3. 兜底：嗅探消息字符串（中转站自定义错误，可能没套在标准异常类里）
    if any(kw in msg for kw in _QUOTA_KEYWORDS):
        return ErrorInfo(ErrorKind.QUOTA_EXHAUSTED, 3600.0, exc)

    if any(kw in msg for kw in _RATE_LIMIT_KEYWORDS):
        return ErrorInfo(ErrorKind.RATE_LIMIT, retry_after, exc)

    if "service temporarily unavailable" in msg or "503" in msg:
        return ErrorInfo(ErrorKind.SERVICE_DOWN, retry_after, exc)

    if "timeout" in msg or "timed out" in msg:
        return ErrorInfo(ErrorKind.NETWORK, retry_after, exc)

    logger.debug("[error_classifier] 未分类异常 type=%s msg=%s", type_name, msg[:120])
    return ErrorInfo(ErrorKind.UNKNOWN, retry_after, exc)
