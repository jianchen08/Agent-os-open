# @feature: FP-0.2.二 可观测性 | @ci: python-coverage
"""user_admin HTTP 面 wire 形状锁（http_json 收敛替换的契约锚）。

锁公开面 http.handle 的 ApiError 结构化形状（本地 `_error(status, message)`，
对齐内核 ApiError）：``{"success": True, "data": {status, body: {"error":
{"code": "<str status>", "message"}}}}`` —— http_json 收敛后由 protocol_error
承载，code 恒字符串化。端点行为语义（角色白名单/能力透传等）由
test_users_domain_http.py 覆盖，本文件只锁错误响应 wire 形状。
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

_PLUGIN_DIR = Path(__file__).resolve().parents[4] / "plugins" / "shared" / "user_admin"


def _load_server() -> Any:
    """动态加载 user_admin/server.py（每次新建，隔离模块级状态）。"""
    spec = importlib.util.spec_from_file_location(
        "user_admin_http_shape_test_server",
        str(_PLUGIN_DIR / "server.py"),
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["user_admin_http_shape_test_server"] = mod
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


def _call(
    server: Any,
    path: str,
    method: str = "GET",
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    return _run(
        server.http_handle(path=path, method=method, raw_body=raw_body,
                           headers=headers, query=query)
    )


def _decode(result: dict[str, Any]) -> tuple[int, Any]:
    """解包 http.handle 返回 → (status, json_body)，并锁信封形状。"""
    assert result["success"] is True, result  # 插件全权控制形态：success 恒 True
    resp = result["data"]
    assert set(resp.keys()) == {"status", "headers", "body", "body_encoding"}
    assert resp["body_encoding"] == "base64"
    assert resp["headers"] == {"Content-Type": "application/json; charset=utf-8"}
    return resp["status"], json.loads(base64.b64decode(resp["body"]).decode("utf-8"))


def test_error_shape_404_unknown_route(server: Any) -> None:
    status, body = _decode(_call(server, "/ext/user_admin/nope", "GET"))
    assert status == 404
    assert "no route" in body["error"]["message"]
    assert body["error"]["code"] == "404"
    assert isinstance(body["error"]["code"], str)  # code 恒字符串化


def test_error_shape_400_invalid_json_body(server: Any) -> None:
    status, body = _decode(
        _call(server, "/ext/user_admin/users/u1/role", "PUT", raw_body="not-json")
    )
    assert status == 400
    assert body["error"]["code"] == "400"
    assert "invalid JSON body" in body["error"]["message"]


def test_error_shape_400_role_whitelist(server: Any) -> None:
    """合法 JSON 但 role 越界 → 400 白名单话术（语义测试见 users 域主文件）。"""
    raw = base64.b64encode(json.dumps({"role": "superadmin"}).encode()).decode("ascii")
    status, body = _decode(
        _call(server, "/ext/user_admin/users/u1/role", "PUT", raw_body=raw)
    )
    assert status == 400
    assert body["error"]["code"] == "400"
    assert "admin 或 user" in body["error"]["message"]
