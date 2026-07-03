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

    RATE_LIMIT = "rate_limit"  # 限流（429，含 group limit / upstream rate）
    QUOTA_EXHAUSTED = "quota_exhausted"  # 配额耗尽（余额不足、月度上限）
    AUTH_FAILED = "auth_failed"  # 认证失败（401）
    SERVICE_DOWN = "service_down"  # 服务不可用（503，上游临时挂，值得重试）
    SERVER_ERROR = "server_error"  # 服务器内部错误（500）
    NETWORK = "network"  # 网络错误（超时、连接失败）
    BAD_REQUEST = "bad_request"  # 请求参数错误（400，不可重试）
    UNKNOWN = "unknown"  # 未分类


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


# 配额耗尽的关键词（400 BadRequest 但语义上是配额问题，以及 429 但实为配额上限）
# 注意：智谱的"每周/每月使用上限"会被 litellm 包装成 RateLimitError(429)，
# 必须在这里识别出来，否则只冷却 5s 后又选回同一个 key，永远轮不到备用 key。
# 收紧：不收录 "limit" 这种宽泛词（group requests-per-minute limit 也含 limit，
# 但那是 RPM 限流不是配额）。只收明确表示"耗尽/上限/余额"的词。
_QUOTA_KEYWORDS = (
    "insufficient",
    "balance",
    "quota",
    "额度",
    "上限",
    "用完",
    "余额",
    "不足",
    "使用上限",
    "每周",
    "每月",
    "monthly",
    "weekly",
    "exhausted",
    "depleted",
)

# 限流类错误消息的关键词（区分于配额）
_RATE_LIMIT_KEYWORDS = (
    "rate limit",
    "rate_limit",
    "requests-per-minute",
    "too many requests",
    "请求过快",
    "频率",
)


def _extract_retry_after(exc: BaseException) -> float | None:
    """从异常里提取 Retry-After 建议秒数。

    三个来源，按可靠性排序：
    1. 异常对象的 retry_after 属性（litellm 部分异常直接带）
    2. response headers 的 Retry-After 头（标准 HTTP）
    3. response body 里的 retry_after 字段（Cloudflare/网关层常放这里）
    """
    # 1. 异常属性
    for attr in ("retry_after",):
        val = getattr(exc, attr, None)
        if isinstance(val, (int, float)) and val > 0:
            return float(val)

    resp = getattr(exc, "response", None)
    if resp is None:
        return None

    # 2. response headers
    headers = getattr(resp, "headers", None)
    if headers:
        ra = headers.get("retry-after") or headers.get("Retry-After")
        if ra:
            try:
                return float(ra)
            except (TypeError, ValueError):
                pass

    # 3. response body（Cloudflare/网关层把 retry_after 放 body 里）
    try:
        body = None
        if hasattr(resp, "json"):
            try:
                body = resp.json()
            except Exception:
                body = None
        if isinstance(body, dict):
            ra = body.get("retry_after") or body.get("retry-after")
            if isinstance(ra, (int, float)) and ra > 0:
                return float(ra)
    except Exception:
        pass

    return None


def _is_quota_from_body(exc: BaseException) -> bool:
    """从异常携带的 HTTP response body 精确判定是否配额耗尽。

    litellm 的 APIStatusError（RateLimitError/BadRequestError 等）带 response 属性，
    是原始 httpx.Response。从 body 里提取结构化错误信息，比嗅探 message 可靠：
    - 配额耗尽的响应体常含 quota/balance/depleted 等字段
    - RPM 限流的响应体是 requests-per-minute / too many requests

    无法提取 body 时返回 False，回退到关键词嗅探。
    """
    resp = getattr(exc, "response", None)
    if resp is None:
        return False
    try:
        body = None
        # httpx.Response.text / .json() / ._content
        if hasattr(resp, "json"):
            try:
                body = resp.json()
            except Exception:
                body = None
        if body is None:
            text = getattr(resp, "text", "") or ""
            if not text:
                content = getattr(resp, "_content", b"")
                if isinstance(content, bytes):
                    text = content.decode("utf-8", errors="ignore")
                else:
                    text = str(content)
            # 文本也按关键词判
            return any(kw in text.lower() for kw in _QUOTA_KEYWORDS)
        # 递归搜索 JSON 里的字符串值
        return _search_json_for_keywords(body, _QUOTA_KEYWORDS)
    except Exception:
        return False


def _search_json_for_keywords(obj: object, keywords: tuple, depth: int = 0) -> bool:
    """递归搜索 JSON 结构里是否有任意字符串值命中关键词。"""
    if depth > 6:
        return False
    if isinstance(obj, str):
        low = obj.lower()
        return any(kw in low for kw in keywords)
    if isinstance(obj, dict):
        return any(_search_json_for_keywords(v, keywords, depth + 1) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return any(_search_json_for_keywords(v, keywords, depth + 1) for v in obj)
    return False


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

    # 判定是否配额耗尽：优先用 response body 精确判定，其次嗅探 message。
    # 这样能兼容各种 provider（智谱/DeepSeek/中转站）的不同错误格式，
    # 而不用为每个 provider 单独写规则。
    is_quota = _is_quota_from_body(exc) or any(kw.lower() in msg for kw in _QUOTA_KEYWORDS)

    # 1. 按异常类型名直接映射（litellm/openai 标准类型）
    if "AuthenticationError" in type_name:
        return ErrorInfo(ErrorKind.AUTH_FAILED, retry_after, exc)

    if "RateLimitError" in type_name:
        # 429 家族：可能是 RPM 限流，也可能是 group/upstream 限流，
        # 也可能是配额耗尽被包装成 429（如智谱的"每周/每月使用上限"）。
        # 配额类必须识别出来冷却 3600s，否则只冷却 5s 会反复选回同一 key。
        if is_quota:
            return ErrorInfo(ErrorKind.QUOTA_EXHAUSTED, 3600.0, exc)
        return ErrorInfo(ErrorKind.RATE_LIMIT, retry_after, exc)

    if "BudgetExceededError" in type_name:
        return ErrorInfo(ErrorKind.QUOTA_EXHAUSTED, 3600.0, exc)

    if "Timeout" in type_name or "APITimeoutError" in type_name:
        return ErrorInfo(ErrorKind.NETWORK, retry_after, exc)

    if "APIConnectionError" in type_name:
        return ErrorInfo(ErrorKind.NETWORK, retry_after, exc)

    if "ServiceUnavailableError" in type_name:
        # 503 家族：多数上游临时抖动 → SERVICE_DOWN；但中转站（如 yichengc）
        # 会把 RPM 限流也包成 503（消息含 "group requests-per-minute limit
        # exceeded"）。限流必须按 RATE_LIMIT 处理（冷却 + 并发降级），否则
        # SERVICE_DOWN 不冷却 key，会无限选回同一个被限流的 key 死循环。
        # 先嗅探消息体，命中限流特征则归 RATE_LIMIT。
        if any(kw in msg for kw in _RATE_LIMIT_KEYWORDS):
            return ErrorInfo(ErrorKind.RATE_LIMIT, retry_after, exc)
        return ErrorInfo(ErrorKind.SERVICE_DOWN, retry_after, exc)

    if "InternalServerError" in type_name:
        # 500 家族：多数上游抖动，按 SERVICE_DOWN 重试更合理
        return ErrorInfo(ErrorKind.SERVICE_DOWN, retry_after, exc)

    # Cloudflare/网关层错误：502 BadGateway、504 GatewayTimeout
    # 这些异常类型不在 litellm 标准层次里，但中转站常返回（Cloudflare 前置时）。
    # retry_after 通常在响应 body 里（Cloudflare 会给 retryable + retry_after）。
    if "BadGateway" in type_name or "GatewayTimeout" in type_name:
        return ErrorInfo(ErrorKind.SERVICE_DOWN, retry_after, exc)

    # 2. BadRequestError（400）需进一步判定：配额类 → QUOTA，否则真参数错
    if "BadRequestError" in type_name:
        if is_quota:
            return ErrorInfo(ErrorKind.QUOTA_EXHAUSTED, 3600.0, exc)
        return ErrorInfo(ErrorKind.BAD_REQUEST, None, exc)

    # 3. 兜底：嗅探消息字符串（中转站自定义错误，可能没套在标准异常类里）
    if any(kw in msg for kw in _QUOTA_KEYWORDS):
        return ErrorInfo(ErrorKind.QUOTA_EXHAUSTED, 3600.0, exc)

    if any(kw in msg for kw in _RATE_LIMIT_KEYWORDS):
        return ErrorInfo(ErrorKind.RATE_LIMIT, retry_after, exc)

    if (
        "service temporarily unavailable" in msg
        or "503" in msg
        or "502" in msg
        or "bad gateway" in msg
        or "badgateway" in msg
    ):
        return ErrorInfo(ErrorKind.SERVICE_DOWN, retry_after, exc)

    if "timeout" in msg or "timed out" in msg:
        return ErrorInfo(ErrorKind.NETWORK, retry_after, exc)

    logger.debug("[error_classifier] 未分类异常 type=%s msg=%s", type_name, msg[:120])
    return ErrorInfo(ErrorKind.UNKNOWN, retry_after, exc)
