# @feature: FP-0.2.二 可观测性 | @ci: python-coverage
"""metrics_admin HTTP 面 wire 形状锁（http_json 收敛替换的契约锚）。

锁三类响应经公开面 http.handle 的逐字节形状（body base64 解码断言）：

1. **ApiError 结构化形状**（本地 `_error(status, message)`，对齐内核 ApiError）：
   ``{"success": True, "data": {status, body: {"error": {"code": "<str status>",
   "message"}}}}`` —— http_json 收敛后由 protocol_error 承载，code 恒字符串化；
2. 200 JSON 读面（query/list 走 _json_response）与 prometheus 文本面
   （text/plain; charset=utf-8; version=0.0.4，exposition 原文）；
3. capability 非 2xx 信封 → 插件组 ApiError 形状透传。

替换走样（success 翻转 / code 数字化 / prometheus content-type 漂移）在此爆红。
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parents[4] / "plugins" / "shared" / "metrics_admin"


def _load_server() -> Any:
    """动态加载 metrics_admin/server.py（每次新建，隔离模块级状态）。"""
    spec = importlib.util.spec_from_file_location(
        "metrics_admin_http_shape_test_server",
        str(_PLUGIN_DIR / "server.py"),
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["metrics_admin_http_shape_test_server"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def server() -> Any:
    return _load_server()


def _run(coro: Any) -> Any:
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _call(server: Any, path: str, method: str = "GET") -> dict[str, Any]:
    return _run(server.http_handle(path=path, method=method))


def _decode(result: dict[str, Any]) -> tuple[int, dict[str, str], Any]:
    """解包 http.handle 返回 → (status, headers, json_body)，并锁信封形状。"""
    assert result["success"] is True, result  # 插件全权控制形态：success 恒 True
    resp = result["data"]
    assert set(resp.keys()) == {"status", "headers", "body", "body_encoding"}
    assert resp["body_encoding"] == "base64"
    return resp["status"], resp["headers"], json.loads(base64.b64decode(resp["body"]).decode("utf-8"))


class _FakeCap:
    """capability 替身：原样回放预置 envelope。"""

    def __init__(self, envelope: dict[str, Any]) -> None:
        self._envelope = envelope

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return self._envelope


def test_error_shape_404_unknown_route(server: Any) -> None:
    status, _headers, body = _decode(_call(server, "/ext/metrics_admin/nope", "GET"))
    assert status == 404
    assert body == {
        "error": {
            "code": "404",
            "message": "metrics_admin: no route for GET /ext/metrics_admin/nope",
        }
    }
    assert isinstance(body["error"]["code"], str)  # code 恒字符串化


def test_error_shape_502_capability_not_injected(server: Any) -> None:
    status, _headers, body = _decode(_call(server, "/ext/metrics_admin/query", "GET"))
    assert status == 502
    assert body == {
        "error": {
            "code": "502",
            "message": "metrics-admin capability not injected (kernel handshake pending)",
        }
    }


def test_success_shape_json_body(server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        server.plugin,
        "get_capability",
        lambda name: _FakeCap({"status": 200, "body": {"points": [], "total": 0}}),
    )
    status, headers, body = _decode(_call(server, "/ext/metrics_admin/query", "GET"))
    assert status == 200
    assert headers == {"Content-Type": "application/json; charset=utf-8"}
    assert body == {"points": [], "total": 0}


def test_error_shape_passthrough_capability_403(server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """capability 非 2xx 信封 → 插件组 ApiError 形状透传（403 角色拒绝路径）。"""
    monkeypatch.setattr(
        server.plugin,
        "get_capability",
        lambda name: _FakeCap(
            {"status": 403, "error": {"code": "403", "message": "forbidden"}}
        ),
    )
    status, _headers, body = _decode(_call(server, "/ext/metrics_admin/series", "GET"))
    assert status == 403
    assert body == {"error": {"code": "403", "message": "forbidden"}}


def test_prometheus_text_content_type_and_body(server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """prometheus 面是 exposition 文本：text/plain 头 + body 原文（非 JSON）。"""
    monkeypatch.setattr(
        server.plugin,
        "get_capability",
        lambda name: _FakeCap({"status": 200, "body": "# HELP m\nm 1\n"}),
    )
    result = _call(server, "/ext/metrics_admin/prometheus", "GET")
    assert result["success"] is True
    resp = result["data"]
    assert resp["status"] == 200
    assert resp["headers"] == {
        "Content-Type": "text/plain; charset=utf-8; version=0.0.4"
    }
    assert base64.b64decode(resp["body"]).decode("utf-8") == "# HELP m\nm 1\n"
