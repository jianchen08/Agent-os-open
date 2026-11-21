# @feature: FP-0.2.可观测性 dsh_adapter 插件 | @ci: python-coverage
"""dsh_adapter 存活处理器直测（自研管理端点删除后的承接面）。

自研插件管理端点已删除（用户裁定 2026-11-17），test_dsh_manager_endpoints.py
随之退役；但其尾部三组**仍存活行为**的用例在此承接（断言语义不变）：

- TestHooksTranslateEndpoint → server.py ``_hooks_translate_response``；
- TestWebProxyEndpoint        → ``_web_proxy_dispatch``；
- TestBridgeServiceHandlers   → ``dsh_service_call`` / ``dsh_hook_emit`` /
  ``dsh_web_request``。

已删端点（list/index/install/uninstall/enable/disable 页面）的用例不迁——
那是能力退役的正确删除（404 防幽灵路由在 test_dsh_official_ui.py 覆盖）。
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

_SRV_MODULE = "dsh_adapter_server_live_test"

# 裸名逐出约定（tests/conftest.py）：本文件只在用例内取 server 模块与
# sys.modules["bridge"]（conftest 用例期装回本插件身份）。


def _load_server() -> Any:
    spec = importlib.util.spec_from_file_location(_SRV_MODULE, PLUGIN_DIR / "server.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_SRV_MODULE] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def srv() -> Any:
    return _load_server()


def _decode(resp: dict[str, Any]) -> tuple[int, Any]:
    body = resp.get("body", "")
    if resp.get("body_encoding") == "base64":
        raw = base64.b64decode(body)
    else:
        raw = body.encode("utf-8")
    ct = (resp.get("headers") or {}).get("Content-Type", "")
    if "json" in ct:
        return int(resp.get("status", 200)), json.loads(raw.decode("utf-8"))
    return int(resp.get("status", 200)), raw.decode("utf-8")


def _b64(payload: dict[str, Any] | None) -> str:
    if payload is None:
        return ""
    return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")


class _StubBridge:
    """共享桥替身：只暴露端点层消费的方法，记录调用参数。"""

    def __init__(self, result: dict[str, Any] | None = None) -> None:
        self.result = result or {"ok": True, "status": 200, "headers": {},
                                 "body_base64": "aGk=", "error": None, "duration_ms": 1}
        self.calls: list[tuple[Any, ...]] = []

    async def http_request(self, method: str, path: str,
                           headers: dict[str, str] | None = None,
                           body: bytes | None = None) -> dict[str, Any]:
        self.calls.append(("http", method, path, headers, body))
        return self.result

    async def call_service(self, name: str, method: str,
                           args: list[Any] | None = None) -> dict[str, Any]:
        self.calls.append(("service", name, method, args))
        return {"success": True, "data": {"echo": [name, method]}, "error": None,
                "duration_ms": 1}

    async def emit_hook(self, event: str, payload: Any = None) -> dict[str, Any]:
        self.calls.append(("emit", event, payload))
        return {"ok": True, "event": event, "decision": None, "error": None}


def _patch_bridge(srv: Any, monkeypatch: Any, stub: _StubBridge) -> None:
    """端点层从裸名 `bridge` 模块取 get_bridge（函数内 import）——替换该符号。"""
    bridge_mod = sys.modules["bridge"]
    monkeypatch.setattr(bridge_mod, "get_bridge", lambda *_a, **_k: stub)


def _call_style(srv: Any, path: str, method: str = "GET", raw_body: str = "",
                headers: dict[str, str] | None = None) -> dict[str, Any]:
    return asyncio.run(srv._http_handle_style(  # noqa: SLF001
        path=path, method=method, plugin_id="dsh_adapter", raw_body=raw_body,
        headers=headers or {}, query={},
    ))


class TestHooksTranslateEndpoint:
    def test_cc_dialect_translated(self, srv: Any) -> None:
        body = _b64({"dialect": "claude-code",
                     "config": {"SessionStart": [{"hooks": [
                         {"type": "command", "command": "node init.mjs", "timeout": 5}]}]}})
        resp = _call_style(srv, "/ext/dsh_adapter/api/dsh/hooks/translate",
                           method="POST", raw_body=body)
        status, out = _decode(resp)
        assert status == 200
        assert out["mapped"] == 1
        assert out["checks"][0]["at"] == "pre:<step>"

    def test_event_outside_dialect_vocabulary_fail_visible(self, srv: Any) -> None:
        body = json.dumps({"config": {"UnknownEvent": [{"hooks": [{"command": "x"}]}]}})
        resp = _call_style(srv, "/ext/dsh_adapter/api/dsh/hooks/translate",
                           method="POST", raw_body=body)
        status, out = _decode(resp)
        assert status == 200
        assert out["mapped"] == 0
        assert "词表外" in out["unmapped"][0]["reason"]

    def test_requires_post(self, srv: Any) -> None:
        resp = _call_style(srv, "/ext/dsh_adapter/api/dsh/hooks/translate", method="GET")
        assert resp["status"] == 404


class TestWebProxyEndpoint:
    def test_passthrough_status_headers_body(self, srv: Any, monkeypatch: Any) -> None:
        stub = _StubBridge({"ok": True, "status": 201,
                            "headers": {"content-type": "application/json",
                                        "content-encoding": "gzip", "content-length": "3"},
                            "body_base64": "aGk=", "error": None, "duration_ms": 2})
        _patch_bridge(srv, monkeypatch, stub)
        resp = _call_style(srv, "/ext/dsh_adapter/web/api/data", method="POST",
                           raw_body="aGk=", headers={"accept": "*/*", "authorization": "Bearer x"})
        assert resp["status"] == 201
        assert resp["body"] == "aGk="
        assert resp["body_encoding"] == "base64"
        assert "content-encoding" not in resp["headers"]
        assert "content-length" not in resp["headers"]
        assert stub.calls == [("http", "POST", "/api/data", {"accept": "*/*"}, b"hi")]

    def test_unavailable_is_502_with_reason(self, srv: Any, monkeypatch: Any) -> None:
        stub = _StubBridge({"ok": False, "status": 0, "headers": {}, "body_base64": "",
                            "error": "webServer 轴需要 @deepseek-ai/dsh-host-webserver",
                            "duration_ms": 0})
        _patch_bridge(srv, monkeypatch, stub)
        resp = _call_style(srv, "/ext/dsh_adapter/web/x")
        status, out = _decode(resp)
        assert status == 502
        assert out["error"]["code"] == "web_proxy_unavailable"
        assert "dsh-host-webserver" in out["error"]["message"]

    def test_raw_body_bytes_base64_and_plain(self, srv: Any) -> None:
        assert srv._raw_body_bytes("aGk=") == b"hi"  # noqa: SLF001
        assert srv._raw_body_bytes("裸文本") == "裸文本".encode()  # noqa: SLF001


class TestBridgeServiceHandlers:
    def test_service_call_requires_name_and_method(self, srv: Any) -> None:
        out = asyncio.run(srv.dsh_service_call("", "m"))
        assert out["success"] is False
        assert "缺 name/method" in out["error"]
        out2 = asyncio.run(srv.dsh_service_call("svc", ""))
        assert out2["success"] is False

    def test_service_call_delegates_to_bridge(self, srv: Any, monkeypatch: Any) -> None:
        stub = _StubBridge()
        _patch_bridge(srv, monkeypatch, stub)
        out = asyncio.run(srv.dsh_service_call("webServer", "getOrigin", []))
        assert out["success"] is True
        assert stub.calls == [("service", "webServer", "getOrigin", [])]

    def test_hook_emit_requires_event(self, srv: Any) -> None:
        out = asyncio.run(srv.dsh_hook_emit(""))
        assert out["ok"] is False
        assert "缺 event" in out["error"]

    def test_hook_emit_delegates(self, srv: Any, monkeypatch: Any) -> None:
        stub = _StubBridge()
        _patch_bridge(srv, monkeypatch, stub)
        out = asyncio.run(srv.dsh_hook_emit("turn/end", {"reason": "x"}))
        assert out["ok"] is True
        assert stub.calls == [("emit", "turn/end", {"reason": "x"})]

    def test_web_request_invalid_base64(self, srv: Any) -> None:
        out = asyncio.run(srv.dsh_web_request("POST", "/x", body_base64="!!!not-base64!!!"))
        assert out["ok"] is False
        assert "body_base64" in out["error"]

    def test_web_request_delegates(self, srv: Any, monkeypatch: Any) -> None:
        stub = _StubBridge()
        _patch_bridge(srv, monkeypatch, stub)
        out = asyncio.run(srv.dsh_web_request("POST", "/x", {"a": "b"}, "aGk="))
        assert out["ok"] is True
        assert stub.calls == [("http", "POST", "/x", {"a": "b"}, b"hi")]
