# @feature: FP-0.2.二 可观测性 | @vision: V3 可嵌入 | @ci: python-coverage
"""user_admin server.py 缺口补测（_route 非前缀早退 + capability 调用异常 502）。

覆盖：
1. `_route` 返回 None 的路径判定（第 112 行）：非 /ext/user_admin/... 前缀
   → http_handle 404，且不触达 capability（零副作用）；
2. capability 调用抛异常（368-370 行）：统一 502 + logger.warning 一条，
   PATCH role / PATCH tenant 两种入参同源（方法名进日志与信封）。

capability 为内核注入的外部面（跨进程反向调用通道），用替身注入；
路径与信封归一走真实实现。
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import logging
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parents[4] / "plugins" / "shared" / "user_admin"


def _load_server() -> Any:
    spec = importlib.util.spec_from_file_location(
        "user_admin_server_gaps_under_test",
        str(_PLUGIN_DIR / "server.py"),
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["user_admin_server_gaps_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def server() -> Any:
    return _load_server()


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _status_and_message(result: dict[str, Any]) -> tuple[int, str]:
    """解包 http.handle 的 protocol_error 形态 → (status, message)。"""
    assert result["success"] is True, result
    resp = result["data"]
    body = json.loads(base64.b64decode(resp["body"]).decode("utf-8"))
    return resp["status"], str(body["error"]["message"])


class _FailingCapability:
    """capability 替身：任何调用都抛连接异常（内核通道断）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        self.calls.append((method, params))
        raise ConnectionError("kernel channel down")


class TestRouteNonPrefixReturnsNone:
    """第 112 行：路径不是 /ext/user_admin 前缀 → 无路由 → 404。"""

    @pytest.mark.parametrize(
        ("path", "method"),
        [
            ("/other/x/y", "PATCH"),
            ("/ext/other/x/y", "PATCH"),
            ("/", "PATCH"),
            ("/ext/user_admin", "PATCH"),  # 仅前缀、无 users/{id}/{field} 段
        ],
    )
    def test_unknown_path_maps_to_404_without_capability(
        self, server: Any, path: str, method: str
    ) -> None:
        cap = _FailingCapability()
        server.plugin._capabilities["user-admin"] = cap

        result = _run(server.http_handle(path=path, method=method))

        status, message = _status_and_message(result)
        assert status == 404
        assert "no route" in message
        assert cap.calls == []  # 路由未命中，绝不触达 capability

    @pytest.mark.parametrize(
        "path",
        ["/ext/user_admin/users/u1/role", "/ext/user_admin/users/u1/tenant"],
    )
    def test_registered_paths_not_treated_as_unrouted(self, server: Any, path: str) -> None:
        """对照组：已支持路径不落 404（走 capability 面，方法名正确）。"""

        class _OkCapability:
            async def call(self, method: str, params: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
                return {"status": 200, "body": {"ok": True}}

        server.plugin._capabilities["user-admin"] = _OkCapability()

        result = _run(server.http_handle(path=path, method="PATCH", raw_body="e30="))

        assert result["success"] is True
        assert result["data"]["status"] == 200


class TestCapabilityCallExceptionMaps502:
    """368-370 行：capability 抛异常 → 502 + 一条 warning。"""

    @pytest.mark.parametrize(
        ("path", "body", "expected_method"),
        [
            ("/ext/user_admin/users/u1/role", {"role": "admin"}, "update_role"),
            ("/ext/user_admin/users/u1/tenant", {"tenant_id": "t9"}, "update_tenant"),
        ],
    )
    def test_failing_capability_yields_502_and_warns(
        self, server: Any, caplog: pytest.LogCaptureFixture, path: str, body: dict, expected_method: str
    ) -> None:
        cap = _FailingCapability()
        server.plugin._capabilities["user-admin"] = cap
        raw_body = base64.b64encode(json.dumps(body).encode("utf-8")).decode("ascii")

        with caplog.at_level(logging.WARNING, logger="user_admin"):
            result = _run(server.http_handle(path=path, method="PATCH", raw_body=raw_body))

        status, message = _status_and_message(result)
        assert status == 502
        assert "capability call failed" in message
        assert "kernel channel down" in message  # 底层原因随信封外显
        assert cap.calls[0][0] == expected_method  # 路由参数真实入参
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert expected_method in warnings[0].getMessage()
        assert "capability" in warnings[0].getMessage()
