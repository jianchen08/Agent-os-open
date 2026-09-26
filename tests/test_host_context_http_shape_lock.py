# @feature: FP-0.2.三 宿主接入 | @ci: python-coverage
"""host_context HTTP 面 wire 形状锁（http_json 收敛替换的契约锚）。

锁公开面 http.handle 的响应形状：本插件 `_json_response` 直接返回
``{"success": True, "data": {status, headers, body(base64), body_encoding}}``
信封（与 http_json 的 ok(json_response(...)) 组合等价），错误是内嵌 body 的
``{"error": <str>}`` + 对应 HTTP 状态。业务语义由 test_host_context_plugin.py
覆盖，本文件只锁 wire 形状（400 非法 body / 403 共享密钥 / 404 未知路由）。

加载经 importlib 唯名模块（不占裸名 ``server`` 缓存，防与 security_check 等
tests/ 根裸名导入的兄弟测试串扰；sys.path 注入复用 _pipeline_plugin_path）。
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from tests._pipeline_plugin_path import add_plugin_dir

add_plugin_dir("input", "host_context")

_PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugins" / "shared" / "pipeline" / "input" / "host_context"


@pytest.fixture
def server() -> Any:
    spec = importlib.util.spec_from_file_location(
        "host_context_http_shape_test_server",
        str(_PLUGIN_DIR / "server.py"),
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["host_context_http_shape_test_server"] = mod
    spec.loader.exec_module(mod)
    return mod


def _call(
    server_mod: Any,
    path: str,
    method: str = "GET",
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    import asyncio

    return asyncio.run(
        server_mod.http_handle(path=path, method=method, raw_body=raw_body,
                               headers=headers, query=query)
    )


def _decode(result: dict[str, Any]) -> tuple[int, Any]:
    """锁信封四键 + base64 解码 body。"""
    assert result["success"] is True, result
    data = result["data"]
    assert set(data.keys()) == {"status", "headers", "body", "body_encoding"}
    assert data["body_encoding"] == "base64"
    assert data["headers"] == {"Content-Type": "application/json; charset=utf-8"}
    return data["status"], json.loads(base64.b64decode(data["body"]).decode("utf-8"))


def test_error_shape_400_invalid_json_body(server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "get_instance", lambda: object.__new__(server.HostContextPlugin))
    status, body = _decode(
        _call(server, "/ext/pipeline_host_context/selection", "POST", raw_body="not-json")
    )
    assert status == 400
    assert body == {"error": "invalid json"}


def test_error_shape_403_wrong_shared_secret(server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """设置共享密钥后，错误 X-Host-Secret → 403 {"error": "forbidden"}。"""
    monkeypatch.setattr(server, "get_instance", lambda: object.__new__(server.HostContextPlugin))
    monkeypatch.setenv("HOST_CONTEXT_SHARED_SECRET", "s3cret")
    status, body = _decode(
        _call(server, "/ext/pipeline_host_context/selection", "POST", raw_body="{}",
              headers={"X-Host-Secret": "wrong"})
    )
    assert status == 403
    assert body == {"error": "forbidden"}


def test_error_shape_404_unknown_route(server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "get_instance", lambda: object.__new__(server.HostContextPlugin))
    status, body = _decode(_call(server, "/ext/pipeline_host_context/nope", "GET"))
    assert status == 404
    assert body == {"error": "unknown route GET /ext/pipeline_host_context/nope"}


def test_success_shape_subscribe(server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """200 成功路径：业务 dict 原样入 body（锁定 json 序列化形状）。"""
    monkeypatch.setattr(server, "get_instance", lambda: server.HostContextPlugin())
    status, body = _decode(
        _call(server, "/ext/pipeline_host_context/subscribe", "POST", raw_body='{"thread_id": "t1"}')
    )
    assert status == 200
    assert body == {"status": "ok", "threads": 1}
