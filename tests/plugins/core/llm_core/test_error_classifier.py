"""llm_core error_classifier 测试——异常 → ErrorKind 统一翻译。

覆盖 docstring 声明的全部翻译路径：

- 异常类名映射（litellm/openai 标准类型层次）：AuthenticationError /
  RateLimitError / BudgetExceededError / Timeout / APIConnectionError /
  ServiceUnavailableError / InternalServerError / BadRequestError /
  BadGateway / GatewayTimeout；
- 配额判定三通道：message 关键词嗅探（中英文）、response body JSON
  递归搜索、response body 文本关键词；429 伪装配额（智谱"每周/每月
  使用上限"）必须识别为 QUOTA_EXHAUSTED 并冷却 3600s；
- 503 伪装限流（"group requests-per-minute limit"）→ RATE_LIMIT；
- BadRequestError 配额 → QUOTA、纯参数错 → BAD_REQUEST（retry_after
  强制 None）；
- retry_after 三来源：异常属性 / response headers / response body
  JSON 字段；非法值忽略；
- 兜底消息嗅探（中转站自定义错误）与 UNKNOWN 分类。

不依赖 litellm：用动态类型名构造异常 + 注入 fake response 属性。
"""

from __future__ import annotations

from typing import Any

import pytest

from agentos_plugin_sdk.error_classifier import ErrorInfo, ErrorKind, classify_error

pytestmark = pytest.mark.unit


def _exc(type_name: str, message: str = "", **attrs: Any) -> Exception:
    """构造指定类型名（litellm 异常层次按名字匹配）+ 任意属性的异常。"""
    cls = type(type_name, (Exception,), {"__module__": "error_classifier_test"})
    exc = cls(message)
    for key, value in attrs.items():
        setattr(exc, key, value)
    return exc


class _FakeResponse:
    """httpx.Response 替身：headers / json() / text 按需注入。"""

    def __init__(
        self,
        headers: dict[str, str] | None = None,
        json_data: Any = None,
        text: str | None = None,
        json_raises: bool = False,
    ) -> None:
        self.headers = headers or {}
        self._json_data = json_data
        self.text = text if text is not None else ""
        self._json_raises = json_raises

    def json(self) -> Any:
        if self._json_raises:
            raise ValueError("no json body")
        return self._json_data


class TestExceptionTypeMapping:
    """异常类名 → ErrorKind 直接映射。"""

    def test_authentication_error(self) -> None:
        info = classify_error(_exc("AuthenticationError", "401 invalid key"))
        assert info.kind is ErrorKind.AUTH_FAILED
        assert info.original is not None

    def test_rate_limit_error_plain(self) -> None:
        info = classify_error(_exc("RateLimitError", "rate limit exceeded"))
        assert info.kind is ErrorKind.RATE_LIMIT
        assert info.retry_after is None

    def test_budget_exceeded_error(self) -> None:
        info = classify_error(_exc("BudgetExceededError", "budget"))
        assert info.kind is ErrorKind.QUOTA_EXHAUSTED
        assert info.retry_after == 3600.0

    def test_timeout_variants_map_to_network(self) -> None:
        for type_name in ("APITimeoutError", "ReadTimeout"):
            info = classify_error(_exc(type_name, "timed out"))
            assert info.kind is ErrorKind.NETWORK, type_name

    def test_api_connection_error(self) -> None:
        info = classify_error(_exc("APIConnectionError", "conn refused"))
        assert info.kind is ErrorKind.NETWORK

    def test_service_unavailable_error(self) -> None:
        info = classify_error(_exc("ServiceUnavailableError", "503 upstream"))
        assert info.kind is ErrorKind.SERVICE_DOWN

    def test_internal_server_error_prefers_service_down(self) -> None:
        info = classify_error(_exc("InternalServerError", "500 oops"))
        assert info.kind is ErrorKind.SERVICE_DOWN

    def test_bad_gateway_maps_to_service_down(self) -> None:
        info = classify_error(_exc("BadGatewayError", "502 from cloudflare"))
        assert info.kind is ErrorKind.SERVICE_DOWN

    def test_gateway_timeout_shadowed_by_timeout_branch(self) -> None:
        # 现实行为：类名含 "Timeout" 的判定先于 BadGateway/GatewayTimeout 分支，
        # GatewayTimeoutError 实际落到 NETWORK（504 网关超时按网络错处理可重试）
        info = classify_error(_exc("GatewayTimeoutError", "504 gateway timeout"))
        assert info.kind is ErrorKind.NETWORK


class TestQuotaDetection:
    """配额耗尽识别：429/400 伪装配额必须冷却 3600s（防 5s 冷却死循环）。"""

    def test_rate_limit_with_chinese_quota_message(self) -> None:
        # 智谱"每周使用上限"被 litellm 包成 429 → 必须 QUOTA_EXHAUSTED
        info = classify_error(_exc("RateLimitError", "已达到每周使用上限"))
        assert info.kind is ErrorKind.QUOTA_EXHAUSTED
        assert info.retry_after == 3600.0

    def test_rate_limit_quota_forces_3600_over_attr(self) -> None:
        # 即使异常自带 retry_after=5，配额判定优先冷却 3600s
        exc = _exc("RateLimitError", "insufficient balance", retry_after=5)
        info = classify_error(exc)
        assert info.kind is ErrorKind.QUOTA_EXHAUSTED
        assert info.retry_after == 3600.0

    def test_rate_limit_quota_from_response_json_body(self) -> None:
        resp = _FakeResponse(json_data={"error": {"code": "insufficient_quota"}})
        info = classify_error(_exc("RateLimitError", "429", response=resp))
        assert info.kind is ErrorKind.QUOTA_EXHAUSTED

    def test_quota_from_response_text_body(self) -> None:
        # json() 抛异常 → 回退 text 关键词判定
        resp = _FakeResponse(text="account balance depleted", json_raises=True)
        info = classify_error(_exc("RateLimitError", "429", response=resp))
        assert info.kind is ErrorKind.QUOTA_EXHAUSTED

    def test_quota_json_nested_list_search(self) -> None:
        # JSON 递归搜索：关键词藏在嵌套 list 里
        resp = _FakeResponse(json_data={"a": {"b": ["credits exhausted"]}})
        info = classify_error(_exc("RateLimitError", "429", response=resp))
        assert info.kind is ErrorKind.QUOTA_EXHAUSTED

    def test_quota_json_depth_limit_stops_search(self) -> None:
        # 深度 >6 不再搜索 → 非配额 → RATE_LIMIT
        deep: Any = "monthly quota exceeded"
        for _ in range(8):
            deep = {"nested": deep}
        resp = _FakeResponse(json_data=deep)
        info = classify_error(_exc("RateLimitError", "429", response=resp))
        assert info.kind is ErrorKind.RATE_LIMIT

    def test_bad_request_with_quota_keyword_is_quota(self) -> None:
        info = classify_error(_exc("BadRequestError", "Insufficient Balance"))
        assert info.kind is ErrorKind.QUOTA_EXHAUSTED
        assert info.retry_after == 3600.0

    def test_bad_request_plain_is_bad_request_with_no_retry(self) -> None:
        # DeepSeek "insufficient tool messages following tool_calls" 是参数错，
        # 不是配额（裸词 insufficient 不收录）→ BAD_REQUEST 且 retry_after 强制 None
        exc = _exc("BadRequestError", "insufficient tool messages", retry_after=10)
        info = classify_error(exc)
        assert info.kind is ErrorKind.BAD_REQUEST
        assert info.retry_after is None


class TestRateLimitDisguise:
    """限流伪装识别：503 包 RPM 限流必须按 RATE_LIMIT 处理。"""

    def test_service_unavailable_with_rpm_limit_message(self) -> None:
        # yichengc 把 RPM 限流包成 503；误判 SERVICE_DOWN 会无限选回同一 key
        info = classify_error(
            _exc("ServiceUnavailableError", "group requests-per-minute limit exceeded")
        )
        assert info.kind is ErrorKind.RATE_LIMIT

    def test_service_unavailable_with_chinese_frequency_message(self) -> None:
        info = classify_error(_exc("ServiceUnavailableError", "请求过快，频率超限"))
        assert info.kind is ErrorKind.RATE_LIMIT


class TestRetryAfterExtraction:
    """Retry-After 三来源提取 + 非法值忽略。"""

    def test_from_exception_attribute(self) -> None:
        exc = _exc("RateLimitError", "rate limit", retry_after=30)
        info = classify_error(exc)
        assert info.kind is ErrorKind.RATE_LIMIT
        assert info.retry_after == 30.0

    def test_zero_or_negative_attr_ignored(self) -> None:
        exc = _exc("RateLimitError", "rate limit", retry_after=0)
        assert classify_error(exc).retry_after is None

    def test_from_response_headers(self) -> None:
        resp = _FakeResponse(headers={"Retry-After": "7"})
        exc = _exc("RateLimitError", "rate limit", response=resp)
        assert classify_error(exc).retry_after == 7.0

    def test_invalid_header_value_falls_through(self) -> None:
        resp = _FakeResponse(headers={"Retry-After": "not-a-number"})
        exc = _exc("RateLimitError", "rate limit", response=resp)
        assert classify_error(exc).retry_after is None

    def test_from_response_body_json(self) -> None:
        # Cloudflare/网关层把 retry_after 放 body 里
        resp = _FakeResponse(json_data={"retry_after": 9})
        exc = _exc("RateLimitError", "rate limit", response=resp)
        assert classify_error(exc).retry_after == 9.0

    def test_response_without_headers_or_body(self) -> None:
        resp = _FakeResponse(json_data=None, json_raises=True, text="")
        exc = _exc("RateLimitError", "rate limit", response=resp)
        assert classify_error(exc).retry_after is None


class TestMessageFallbackAndUnknown:
    """兜底消息嗅探（中转站自定义错误）与 UNKNOWN。"""

    def test_generic_quota_message_sniffed(self) -> None:
        info = classify_error(_exc("SomeProxyError", "账户余额不足"))
        assert info.kind is ErrorKind.QUOTA_EXHAUSTED

    def test_generic_rate_limit_message_sniffed(self) -> None:
        info = classify_error(_exc("SomeProxyError", "Too many requests, slow down"))
        assert info.kind is ErrorKind.RATE_LIMIT

    def test_generic_service_down_message_sniffed(self) -> None:
        info = classify_error(_exc("SomeProxyError", "upstream 503 service temporarily unavailable"))
        assert info.kind is ErrorKind.SERVICE_DOWN

    def test_generic_timeout_message_sniffed(self) -> None:
        info = classify_error(_exc("SomeProxyError", "request timed out"))
        assert info.kind is ErrorKind.NETWORK

    def test_unclassifiable_returns_unknown(self) -> None:
        info = classify_error(_exc("WeirdError", "something strange happened"))
        assert info.kind is ErrorKind.UNKNOWN
        assert isinstance(info, ErrorInfo)
        assert info.original is not None
