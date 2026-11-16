# @feature: FP-0.2.二 内部模块统一 manifest 化 | @vision: V3 可嵌入 | @ci: python-coverage
"""边界分类契约（exceptions.classified_provider_raise）：自有词汇全表 + 原样上抛保底。

分类表自持（wire_client / httpx 自有异常类型），无第三方 LLM 依赖——
全部类型静态在位，无运行时缺席面。本文件锁定：

- WireHTTPError 按 HTTP 失败契约 code 直译（auth/rate_limit/quota/
  bad_request/service_down，未登记 code 落 service_down）
- WireTimeoutError → 超时类（L5：超时零裸穿——RuntimeError 形态不再穿透
  MCP 调用边界，装机 2026-11-05 十九次裸穿的回归面）
- httpx 传输异常按类型分类：TimeoutException 前置于 TransportError
  （子类前置顺序性质断言）
- 领域异常与未命中异常原样上抛（ADR 2026-10-16：禁止未分类也硬归因）

测试断行为不断实现：经 classified_provider_raise 的输入→输出断言为主。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = str(Path(__file__).resolve().parent)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

for _m in ("exceptions",):
    sys.modules.pop(_m, None)

import exceptions  # noqa: E402
import wire_client  # noqa: E402


def _cause_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def test_wire_timeout_classified_not_bare() -> None:
    """L5 主契约：WireTimeoutError → LLMProviderError 超时类（RuntimeError 不裸穿）。

    装机 2026-11-05 反面：19 次首块超时以 RuntimeError 形态炸穿 MCP
    tools/call——本断言即该形态的回归面。
    """
    cause = wire_client.WireTimeoutError(
        "Stream first chunk timeout (incl. connect): no response for 180s"
    )
    with pytest.raises(exceptions.LLMProviderError) as exc_info:
        exceptions.classified_provider_raise(cause, "glm-5.3-flash")
    err = exc_info.value
    assert err.category.startswith("LLM 调用超时"), err.category
    assert err.model == "glm-5.3-flash"
    assert "WireTimeoutError" in err.cause
    assert "180s" in err.cause
    assert err.__cause__ is cause
    # 稳定中文前缀（ADR 2026-10-16 契约：MCP 折叠截断保头部）
    assert str(err).startswith("LLM 供应商调用失败[")


@pytest.mark.parametrize(
    ("status_code", "code", "category_prefix"),
    [
        (401, None, "认证失败"),
        (429, None, "供应商限流或额度耗尽"),
        (400, None, "请求被供应商拒绝"),
        (403, None, "认证失败"),
        (503, None, "供应商服务暂不可用"),
        (529, None, "供应商服务暂不可用"),  # 未登记码落 service_down
        (500, "quota", "供应商额度耗尽"),  # 显式 kind 覆盖（provider 插件契约码）
    ],
)
def test_wire_http_error_code_map(status_code: int, code: str | None, category_prefix: str) -> None:
    """HTTP 失败契约码直译：状态码映射 / 显式 kind 覆盖 / 未登记码兜底。"""
    kwargs: dict[str, Any] = {}
    if code is not None:
        kwargs["kind"] = code
    cause = wire_client.WireHTTPError(status_code, "upstream said no", **kwargs)
    with pytest.raises(exceptions.LLMProviderError) as exc_info:
        exceptions.classified_provider_raise(cause, "m")
    err = exc_info.value
    assert err.category.startswith(category_prefix), err.category
    assert err.__cause__ is cause
    assert str(status_code) in err.cause or "upstream said no" in err.cause


def test_httpx_connect_error_network_category() -> None:
    """建连被拒（不可重试终态）→ 网络类结构化失败带归因（不再 UNKNOWN 裸抛）。"""
    cause = httpx.ConnectError("All connection attempts failed")
    with pytest.raises(exceptions.LLMProviderError) as exc_info:
        exceptions.classified_provider_raise(cause, "m")
    assert exc_info.value.category.startswith("网络连接失败"), exc_info.value.category
    assert "ConnectError" in exc_info.value.cause


def test_httpx_read_timeout_timeout_category() -> None:
    """httpx 层读超时 → 超时类（与 WireTimeoutError 同类目单源）。"""
    cause = httpx.ReadTimeout("timed out")
    with pytest.raises(exceptions.LLMProviderError) as exc_info:
        exceptions.classified_provider_raise(cause, "m")
    assert exc_info.value.category.startswith("LLM 调用超时"), exc_info.value.category


def test_subclass_ordering_timeout_wins_over_transport() -> None:
    """性质断言：TimeoutException（TransportError 子类）必须前置命中超时类目。

    表顺序即 isinstance 匹配顺序——若 TransportError 前置，读超时会被误归
    网络类目（分类塌缩）。
    """
    names = [t.__name__ for t, _ in exceptions._PROVIDER_ERROR_RULES]
    assert names.index("TimeoutException") < names.index("TransportError")
    # 全表在位（防实现塌缩成空表——空表 = 全部原样上抛 = 分类面消失）
    assert set(names) == {"WireTimeoutError", "TimeoutException", "TransportError"}
    # 行为对照：子类异常实际命中超时类目
    cause = httpx.ReadTimeout("timed out")
    with pytest.raises(exceptions.LLMProviderError) as exc_info:
        exceptions.classified_provider_raise(cause, "m")
    assert exc_info.value.category.startswith("LLM 调用超时")


@pytest.mark.parametrize(
    "raw",
    [
        RuntimeError("boom"),
        ValueError("bad"),
        RuntimeError("model sample 不存在"),
    ],
)
def test_unmatched_exception_passthrough(raw: Exception) -> None:
    """未命中分类表的异常原样上抛（类型不换、同一对象、不误归因）。"""
    with pytest.raises(type(raw)) as exc_info:
        exceptions.classified_provider_raise(raw, "m")
    assert exc_info.value is raw


def test_domain_exceptions_passthrough() -> None:
    """领域异常（人类可读 str 已自带归因）原样上抛，不做二次包装。"""
    unresolved = exceptions.LLMKeyUnresolvedError("m", "p", "${VAR}")
    with pytest.raises(exceptions.LLMKeyUnresolvedError) as e1:
        exceptions.classified_provider_raise(unresolved, "m")
    assert e1.value is unresolved

    exhausted = exceptions.KeyPoolExhaustedError("pool", 60.0, ["sk-1..."])
    with pytest.raises(exceptions.KeyPoolExhaustedError) as e2:
        exceptions.classified_provider_raise(exhausted, "m")
    assert e2.value is exhausted


def test_provider_error_payload_contract() -> None:
    """LLMProviderError 三元组载荷契约：category/model/cause 各就各位。"""
    err = exceptions.LLMProviderError("认证失败", "glm-5.2", "WireHTTPError: 401")
    assert err.category == "认证失败"
    assert err.model == "glm-5.2"
    assert err.cause == "WireHTTPError: 401"
    assert str(err) == "LLM 供应商调用失败[认证失败]（model=glm-5.2）: WireHTTPError: 401"
    # SimpleNamespace 对照仅证明无耦合：载荷不依赖异常对象结构
    assert not isinstance(err, SimpleNamespace)
