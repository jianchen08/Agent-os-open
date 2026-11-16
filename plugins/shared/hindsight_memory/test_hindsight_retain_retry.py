# @feature: FP-0.2.六 记忆检索 | @vision: V1 可进化 | @ci: python-coverage
"""hindsight.retain 有界重试（装机 2026-11-05 §B3）。

现象：hindsight-api 对 retain 做事实抽取要内嵌调一次 LLM（同网关上游），
上游 502 窗口把抽取失败包装成 HTTP 500——抽取调用单发无重试使多 agent 并发
高峰窗口记忆写入 100% 全灭（装机 PG 断流 5 天实证，日志 ``error=(500)`` ×26）。

契约：
1. 5xx（ServiceException）可重试：指数退避 1s/2s，第 3 次成功即存入成功，
   共 3 次尝试；三次全败则原样上抛（retain 返回 stored=False + error）；
2. 4xx（BadRequestException/NotFound 等）不重试：首次失败即败，仅 1 次尝试；
3. 成功即停：首次成功只调 1 次且零 sleep（无重试税）；
4. 网络类（ApiException status=0 包装 / TimeoutError）与 5xx 同待遇可重试。

异常用真实 hindsight_client_api.exceptions 类（生产客户端同源，防拟合）；
时序用 asyncio.sleep 打桩记录退避序列，不真等。
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

try:
    # 真实客户端异常类优先（生产同源；插件 aux venv 场景在位）
    from hindsight_client_api.exceptions import (  # type: ignore[attr-defined]
        ApiException,
        BadRequestException,
        NotFoundException,
        ServiceException,
    )
except ImportError:
    # 仓库 uv venv 未装 hindsight 客户端（其依赖只在插件 aux venv）——按生成
    # 客户端契约构造同形替身（status 属性 + "(status)\nReason:" str 形态）。
    # 被测行为只消费 .status，两臂语义等价。
    class ApiException(Exception):  # type: ignore[no-redef]  # 契约替身双臂（见 except 分支注释）
        def __init__(self, status=None, reason=None, **_kw: Any) -> None:
            super().__init__(f"({status})\nReason: {reason}\n")
            self.status = status
            self.reason = reason

    class ServiceException(ApiException):  # type: ignore[no-redef]
        pass

    class BadRequestException(ApiException):  # type: ignore[no-redef]
        pass

    class NotFoundException(ApiException):  # type: ignore[no-redef]
        pass

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_module() -> Any:
    """动态加载 server.py（每次新建，避免模块级状态跨测试污染）。"""
    mod_name = "hindsight_retain_retry_test"
    path = _PLUGIN_DIR / "server.py"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def srv() -> Any:
    module = _load_module()
    module._client = None
    module._init_inflight = None
    return module


async def _call_retain(module: Any, **kwargs: Any) -> dict[str, Any]:
    td = module.plugin._tools["hindsight.retain"]
    return await td.handler(content=kwargs.pop("content", "hello"), **kwargs)


def _stub_sleep(module: Any, monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """打桩 asyncio.sleep 记录退避序列（不真等；模块经全局 asyncio 取 sleep）。"""
    delays: list[float] = []

    async def fake_sleep(t: float) -> None:
        delays.append(t)

    monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)
    return delays


class TestRetainRetry:
    async def test_5xx_retries_then_succeeds(self, srv: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """5xx 前两次失败第 3 次成功：退避 1s/2s，stored=True，共 3 次尝试。"""
        client = MagicMock()
        client.aretain = AsyncMock(
            side_effect=[
                ServiceException(status=500, reason="Internal Server Error"),
                ServiceException(status=502, reason="Bad Gateway"),
                MagicMock(id="mem_ok", operation_id="op1"),
            ]
        )
        srv._client = client
        delays = _stub_sleep(srv, monkeypatch)

        result = await _call_retain(srv, bank_id="b1")

        assert result["stored"] is True
        assert result["id"] == "mem_ok"
        assert client.aretain.await_count == 3
        assert delays == [1.0, 2.0], "指数退避序列必须是 1s/2s"

    async def test_5xx_exhausted_raises_last_error(
        self, srv: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """三次全 5xx：末次原样上抛，retain 返回 stored=False + error（共 3 次尝试）。"""
        boom = ServiceException(status=500, reason="Internal Server Error")
        client = MagicMock()
        client.aretain = AsyncMock(side_effect=boom)
        srv._client = client
        delays = _stub_sleep(srv, monkeypatch)

        result = await _call_retain(srv, bank_id="b1")

        assert result["stored"] is False
        assert "(500)" in result["error"], "错误面须保留 5xx 形态（生产日志实测 error=(500)）"
        assert client.aretain.await_count == 3
        assert delays == [1.0, 2.0]

    async def test_4xx_no_retry(self, srv: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """4xx 客户端错误不重试：400/404 各自首败即败，仅 1 次尝试零 sleep。"""
        delays = _stub_sleep(srv, monkeypatch)
        for exc in (
            BadRequestException(status=400, reason="Bad Request"),
            NotFoundException(status=404, reason="Not Found"),
        ):
            client = MagicMock()
            client.aretain = AsyncMock(side_effect=exc)
            srv._client = client

            result = await _call_retain(srv, bank_id="b1")

            assert result["stored"] is False
            assert client.aretain.await_count == 1, f"{type(exc).__name__} 不得重试"
        assert delays == []

    async def test_success_first_try_no_retry_tax(
        self, srv: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """首次成功即停：仅 1 次尝试，零 sleep（无重试税）。"""
        client = MagicMock()
        client.aretain = AsyncMock(return_value=MagicMock(id="mem_first", operation_id="op0"))
        srv._client = client
        delays = _stub_sleep(srv, monkeypatch)

        result = await _call_retain(srv, bank_id="b1")

        assert result["stored"] is True and result["id"] == "mem_first"
        assert client.aretain.await_count == 1
        assert delays == []

    async def test_network_wrapped_status_zero_is_retryable(
        self, srv: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """连接层失败（生成客户端 rest.py 包装 status=0）与 5xx 同待遇可重试。"""
        client = MagicMock()
        client.aretain = AsyncMock(
            side_effect=[
                ApiException(status=0, reason="connection refused"),
                MagicMock(id="mem_net", operation_id="op2"),
            ]
        )
        srv._client = client
        delays = _stub_sleep(srv, monkeypatch)

        result = await _call_retain(srv, bank_id="b1")

        assert result["stored"] is True
        assert client.aretain.await_count == 2
        assert delays == [1.0]

    async def test_non_retryable_exception_shapes_fail_fast(
        self, srv: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """判不出的异常形态（ValueError 等）不重试：白名单制不放大参数错误。"""
        client = MagicMock()
        client.aretain = AsyncMock(side_effect=ValueError("bad metadata shape"))
        srv._client = client
        delays = _stub_sleep(srv, monkeypatch)

        result = await _call_retain(srv, bank_id="b1")

        assert result["stored"] is False
        assert client.aretain.await_count == 1
        assert delays == []


class TestRetryableClassifier:
    """_is_retryable_error 分类面：status 属性形态 + 基类形态。"""

    def test_service_exception_5xx_true(self, srv: Any) -> None:
        assert srv._is_retryable_error(ServiceException(status=500, reason="x")) is True
        assert srv._is_retryable_error(ServiceException(status=599, reason="x")) is True

    def test_api_exception_4xx_false(self, srv: Any) -> None:
        assert srv._is_retryable_error(ApiException(status=409, reason="x")) is False
        assert srv._is_retryable_error(BadRequestException(status=400, reason="x")) is False

    def test_status_zero_and_transport_bases_true(self, srv: Any) -> None:
        assert srv._is_retryable_error(ApiException(status=0, reason="conn")) is True
        assert srv._is_retryable_error(TimeoutError("t")) is True
        assert srv._is_retryable_error(ConnectionError("c")) is True
        assert srv._is_retryable_error(OSError("o")) is True

    def test_unknown_shapes_false(self, srv: Any) -> None:
        assert srv._is_retryable_error(ValueError("v")) is False
        assert srv._is_retryable_error(RuntimeError("r")) is False
        assert srv._is_retryable_error(Exception("e")) is False
