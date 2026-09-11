# @feature: FP-0.2.二 可观测性 | @ci: python-coverage
"""db_admin HTTP 面 wire 形状锁（http_json 收敛替换的契约锚）。

锁三类响应经公开面 http.handle 的逐字节形状（body base64 解码断言）：

1. **ApiError 结构化形状**（本地 `_error(status, message)`，对齐内核 ApiError）：
   ``{"success": True, "data": {status, body: {"error": {"code": "<str status>",
   "message"}}}}`` —— http_json 收敛后由 protocol_error 承载，code 恒字符串化；
2. ToolExecutionResult/HttpHandleResponse 信封（四键 data、base64 编码）；
3. 200 成功路径 `_ok(_json_response(payload))` 透传 capability 信封 body。

替换走样（success 翻转 / code 数字化 / envelope 缺键）在此立即爆红。
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

_PLUGIN_DIR = Path(__file__).resolve().parents[4] / "plugins" / "shared" / "db_admin"


def _load_server() -> Any:
    """动态加载 db_admin/server.py（每次新建，隔离模块级状态）。"""
    spec = importlib.util.spec_from_file_location(
        "db_admin_http_shape_test_server",
        str(_PLUGIN_DIR / "server.py"),
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["db_admin_http_shape_test_server"] = mod
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


def _call(server: Any, path: str, method: str = "GET", raw_body: str = "") -> dict[str, Any]:
    return _run(
        server.http_handle(path=path, method=method, raw_body=raw_body)
    )


def _decode(result: dict[str, Any]) -> tuple[int, Any]:
    """解包 http.handle 返回 → (status, json_body)，并锁信封形状。"""
    assert result["success"] is True, result  # 插件全权控制形态：success 恒 True
    resp = result["data"]
    assert set(resp.keys()) == {"status", "headers", "body", "body_encoding"}
    assert resp["body_encoding"] == "base64"
    assert resp["headers"] == {"Content-Type": "application/json; charset=utf-8"}
    return resp["status"], json.loads(base64.b64decode(resp["body"]).decode("utf-8"))


class _FakeCap:
    """capability 替身：原样回放预置 envelope。"""

    def __init__(self, envelope: dict[str, Any]) -> None:
        self._envelope = envelope

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return self._envelope


def test_error_shape_404_unknown_route(server: Any) -> None:
    status, body = _decode(_call(server, "/ext/db_admin/nope", "GET"))
    assert status == 404
    assert body == {
        "error": {"code": "404", "message": "db_admin: no route for GET /ext/db_admin/nope"}
    }
    assert isinstance(body["error"]["code"], str)  # code 恒字符串化


def test_error_shape_400_invalid_body(server: Any) -> None:
    status, body = _decode(
        _call(server, "/ext/db_admin/table/users/p1", "PATCH", raw_body="not-json")
    )
    assert status == 400
    assert body["error"]["code"] == "400"
    assert "invalid JSON body" in body["error"]["message"]


def test_error_shape_502_capability_not_injected(server: Any) -> None:
    status, body = _decode(_call(server, "/ext/db_admin/tables", "GET"))
    assert status == 502
    assert body == {
        "error": {
            "code": "502",
            "message": "db-admin capability not injected (kernel handshake pending)",
        }
    }


def test_success_shape_passthrough_capability_body(server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """2xx 信封：capability body 原样成为响应 body（200 键形状锁定）。"""
    monkeypatch.setattr(
        server.plugin,
        "get_capability",
        lambda name: _FakeCap({"status": 200, "body": {"tables": ["t1"]}}),
    )
    status, body = _decode(_call(server, "/ext/db_admin/tables", "GET"))
    assert status == 200
    assert body == {"tables": ["t1"]}


def test_error_shape_passthrough_capability_403(server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """capability 非 2xx 信封 → 插件组 ApiError 形状透传（403 角色拒绝路径）。"""
    monkeypatch.setattr(
        server.plugin,
        "get_capability",
        lambda name: _FakeCap(
            {"status": 403, "error": {"code": "403", "message": "forbidden"}}
        ),
    )
    status, body = _decode(_call(server, "/ext/db_admin/tables", "GET"))
    assert status == 403
    assert body == {"error": {"code": "403", "message": "forbidden"}}
