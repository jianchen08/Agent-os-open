"""LLM 领域异常定义与边界分类。

将 LLM 调用链路中可预期的资源耗尽类错误从裸异常区分出来，让调用方能按
异常类型精确处理（而非字符串嗅探），符合
backend_rules §3.1「捕获具体异常而非 Exception」。

边界分类（``classified_provider_raise``）承接两份契约：
- ADR 2026-10-16 错误分类透传：LLMProviderError 稳定中文前缀经 MCP 错误链
  透传，内核折叠文案（280 字截断保头部）能保留分类摘要；
- ADR 2026-11-05 L5 超时后处置归类律：超时到点必须产生归类后处置，禁止裸
  异常穿透调用边界——分类表自持（wire_client / httpx 自有词汇），无第三方
  LLM 依赖。
"""

from __future__ import annotations

from typing import NoReturn

import httpx
import wire_client


class LLMProviderError(Exception):
    """LLM 供应商调用失败（已分类）。

    str() 以稳定中文前缀开头，异常文本经 MCP 错误链跨进程透传时各层只加
    传输前缀不改写尾部，内核折叠文案（280 字截断）因此能保留分类摘要，
    用户看到的是「认证失败/限流」而非原始异常堆栈。

    Attributes:
        category: 分类摘要（含处置指引，如「认证失败（API Key 无效…）」）。
        model: 本次调用的模型标识（llm.complete_stream 的 model 形参）。
        cause: 原始异常的「类型名: 消息」文本（根因尾部，不截断）。
    """

    def __init__(self, category: str, model: str, cause: str) -> None:
        self.category = category
        self.model = model
        self.cause = cause
        super().__init__(f"LLM 供应商调用失败[{category}]（model={model}）: {cause}")


# HTTP 失败契约 code → 分类摘要（wire_client._STATUS_KIND 产物的边界翻译；
# 未登记 code 落 service_down 兜底类目）。
_WIRE_HTTP_CODE_CATEGORIES: dict[str, str] = {
    "auth": "认证失败（API Key 无效或未授权，请检查模型配置的 api_key）",
    "rate_limit": "供应商限流或额度耗尽（请稍后重试或检查 Key 配额）",
    "quota": "供应商额度耗尽（请检查 Key 配额或更换凭据）",
    "bad_request": "请求被供应商拒绝（请检查采样参数与上下文内容合法性）",
    "service_down": "供应商服务暂不可用（请稍后重试）",
}

# 超时/网络类目摘要（WireTimeoutError 与 httpx 传输异常共用，单源不另立文案）。
_TIMEOUT_CATEGORY = "LLM 调用超时（上游长时间无响应，已尝试重试与兜底；请检查网络与供应商可用性）"
_NETWORK_CATEGORY = "网络连接失败（请检查网络与 api_base 端点）"

# 自有异常类型 → 分类摘要。顺序敏感：TimeoutException 是 TransportError 子类，
# 必须前置（表顺序即 isinstance 匹配顺序）。全表为静态自有类型——无运行时
# 缺席面，不需要按第三方暴露面动态构建。
_PROVIDER_ERROR_RULES: tuple[tuple[type[BaseException], str], ...] = (
    (wire_client.WireTimeoutError, _TIMEOUT_CATEGORY),
    (httpx.TimeoutException, _TIMEOUT_CATEGORY),
    (httpx.TransportError, _NETWORK_CATEGORY),
)


def classified_provider_raise(exc: BaseException, model: str) -> NoReturn:
    """边界分类后抛 ``LLMProviderError``（保留 ``__cause__``；L5 超时零裸穿）。

    wire 层 HTTP 失败按结构化失败码直译；自有超时异常与 httpx 传输异常按
    类型表分类。本项目领域异常（LLMKeyUnresolvedError/KeyPoolExhaustedError
    等，str 已是人类可读摘要）与未命中分类表的异常原样上抛，不做二次包装
    （ADR 2026-10-16：禁止未分类也硬归因）。

    Args:
        exc: ``llm.complete_stream`` 零累积路径捕获到的异常。
        model: 本次调用的模型标识（进错误文本供定位）。

    Raises:
        LLMProviderError: ``exc`` 命中分类表（HTTP 码 / 超时 / 网络传输）时。
        BaseException: 未命中时原对象原样上抛（``raise exc`` 保持类型与 traceback）。
    """
    if isinstance(exc, wire_client.WireHTTPError):
        category = _WIRE_HTTP_CODE_CATEGORIES.get(exc.code) or _WIRE_HTTP_CODE_CATEGORIES["service_down"]
        raise LLMProviderError(category, model, f"{type(exc).__name__}: {exc}") from exc
    for exc_type, category in _PROVIDER_ERROR_RULES:
        if isinstance(exc, exc_type):
            raise LLMProviderError(category, model, f"{type(exc).__name__}: {exc}") from exc
    raise exc


class KeyPoolExhaustedError(Exception):
    """所有 key 不可用且等待超时。

    携带诊断信息（不可用 key 列表），让调用方和日志能定位是哪些 key
    在冷却/耗尽，而非只看到「所有 key 不可用」这种无法排查的信息。
    """

    def __init__(self, pool_id: str, timeout: float, unavailable: list[str]) -> None:
        self.pool_id = pool_id
        self.timeout = timeout
        self.unavailable = unavailable
        super().__init__(
            f"KeyPool '{pool_id}' 所有 key 不可用，等待 {timeout:.0f}s 后超时；不可用 key 诊断: {unavailable}"
        )


class LLMKeyUnresolvedError(Exception):
    """模型调用的 api_key 是未解析的 ``${VAR}`` 占位符。

    占位符（进程环境与生效 .env（用户空间优先）双源均无值）发往上游只会得到
    以字面量 ``${VAR}`` 为 key 的鉴权 401，无法排查——发起 HTTP 前抛本错误
    （fail-closed），携带 model/provider/占位符供定位与配置补全。
    """

    def __init__(self, model: str, provider: str, placeholder: str) -> None:
        self.model = model
        self.provider = provider
        self.placeholder = placeholder
        super().__init__(
            f"模型 {model}（provider={provider or '未知'}）的 API key 未配置："
            f"占位符 {placeholder} 在进程环境与 .env（用户空间优先）中均未解析。"
            f"请配置该变量后重试（.env 变更由 sidecar 指纹检测自动重载生效）。"
        )
