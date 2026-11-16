# @feature: FP-0.2.二 None | @ci: python-coverage
# @feature: browser-live-workspace-tab | @ci: python-coverage
# browser_tool 直播面（webview 页 + 换票路由）——Bridge 契约映射与等待态语义。

from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

HERE = Path(__file__).parent
BROWSER_PLUGIN = HERE.parent.parent.parent.parent / "plugins" / "shared" / "browser"

sys.path.insert(0, str(BROWSER_PLUGIN))


def _load_server():
    mod_name = "browser_plugin_server_live_under_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, BROWSER_PLUGIN / "server.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _decode_envelope(result: dict) -> tuple[int, dict]:
    """{success, data: HttpHandleResponse} → (status, json payload)。"""
    assert result["success"] is True
    data = result["data"]
    assert data["body_encoding"] == "base64"
    return data["status"], json.loads(base64.b64decode(data["body"]).decode("utf-8"))


class TestLivePage:
    @pytest.mark.asyncio
    async def test_live_page_serves_html(self):
        """直播页路由：200 text/html，内含换票中继与 img 直连要素。"""
        server = _load_server()
        result = await server.http_handle(path="/ext/browser_tool/page/browser-live", method="GET")
        assert result["success"] is True
        data = result["data"]
        assert data["status"] == 200
        assert "text/html" in data["headers"]["Content-Type"]
        assert data["body_encoding"] == "base64"
        html = base64.b64decode(data["body"]).decode("utf-8")
        assert "/ext/browser_tool/live" in html  # 换票中继调用
        assert "mjpeg_url" in html  # img src 取自换票回执
        assert "agentos.postMessage" in html  # 走宿主中继（connect-src 'none' 沙箱）

    @pytest.mark.asyncio
    async def test_live_page_missing_file_maps_500(self, monkeypatch):
        server = _load_server()
        monkeypatch.setattr(server, "_LIVE_PAGE_HTML_PATH", "Z:/no/such/browser_live.html")
        result = await server.http_handle(path="/ext/browser_tool/page/browser-live", method="GET")
        assert result["success"] is False
        assert "unavailable" in result["error"]


class TestLiveRoute:
    @pytest.mark.asyncio
    async def test_live_route_maps_bridge_ticket(self, monkeypatch):
        server = _load_server()
        # 构造与换票都打桩：token 解析（环境差异）与 Bridge 往返（外部依赖）。
        monkeypatch.setattr(server.BridgeClient, "_resolve_token", staticmethod(lambda _cfg: "tok-abc"))
        monkeypatch.setattr(
            server.BridgeClient,
            "live_ticket",
            lambda _self: {
                "ok": True,
                "status": 200,
                "body": {"ticket": "t-1", "mjpeg_url": "http://127.0.0.1:8765/browser/live.mjpeg?ticket=t-1", "page_url": "https://x", "session": "s"},
            },
        )
        result = await server.http_handle(path="/ext/browser_tool/live", method="GET")
        status, payload = _decode_envelope(result)
        assert status == 200
        assert payload["ticket"] == "t-1"
        assert "/browser/live.mjpeg" in payload["mjpeg_url"]

    @pytest.mark.asyncio
    async def test_live_route_bridge_down_maps_waiting(self, monkeypatch):
        """Bridge 不可达 → 200 + available=false（等待态语义，页面轮询重试）。"""
        server = _load_server()
        monkeypatch.setattr(server.BridgeClient, "_resolve_token", staticmethod(lambda _cfg: "tok-abc"))
        monkeypatch.setattr(
            server.BridgeClient,
            "live_ticket",
            lambda _self: {"ok": False, "status": 0, "body": {"error": "Bridge 不可达"}},
        )
        result = await server.http_handle(path="/ext/browser_tool/live", method="GET")
        status, payload = _decode_envelope(result)
        assert status == 200
        assert payload["available"] is False
        assert payload["reason"] == "runtime_down"

    @pytest.mark.asyncio
    async def test_live_route_runtime_disabled_maps_disabled(self, monkeypatch):
        """运行时未启用（Bridge 404）→ reason=disabled（页面 15s 慢轮询）。"""
        server = _load_server()
        monkeypatch.setattr(server.BridgeClient, "_resolve_token", staticmethod(lambda _cfg: "tok-abc"))
        monkeypatch.setattr(
            server.BridgeClient,
            "live_ticket",
            lambda _self: {"ok": False, "status": 404, "body": {"error": "浏览器运行时未启用（配置 browser 段）"}},
        )
        result = await server.http_handle(path="/ext/browser_tool/live", method="GET")
        status, payload = _decode_envelope(result)
        assert status == 200
        assert payload["available"] is False
        assert payload["reason"] == "disabled"

    @pytest.mark.asyncio
    async def test_live_route_bridge_auth_error_passthrough(self, monkeypatch):
        """Bridge 治理面拒绝（401/403）属配置错误：非 200 原样透传。"""
        server = _load_server()
        monkeypatch.setattr(server.BridgeClient, "_resolve_token", staticmethod(lambda _cfg: "tok-abc"))
        monkeypatch.setattr(
            server.BridgeClient,
            "live_ticket",
            lambda _self: {"ok": False, "status": 401, "body": {"error": "认证失败"}},
        )
        result = await server.http_handle(path="/ext/browser_tool/live", method="GET")
        status, _ = _decode_envelope(result)
        assert status == 401

    @pytest.mark.asyncio
    async def test_live_route_client_init_failure_maps_waiting(self, monkeypatch):
        """BridgeClient 构造失败（token 缺失等 BridgeClientError）→ 等待态语义。"""
        server = _load_server()
        monkeypatch.setattr(server, "_bridge_config", lambda: {})
        monkeypatch.setattr(
            server.BridgeClient,
            "_resolve_token",
            staticmethod(lambda _cfg: (_ for _ in ()).throw(server.BridgeClientError("AGENTOS_BRIDGE_TOKEN 未配置"))),
        )
        result = await server.http_handle(path="/ext/browser_tool/live", method="GET")
        status, payload = _decode_envelope(result)
        assert status == 200
        assert payload["available"] is False
        assert "TOKEN" in payload["error"]

    @pytest.mark.asyncio
    async def test_unknown_route_404(self):
        server = _load_server()
        result = await server.http_handle(path="/ext/browser_tool/other", method="GET")
        assert result["success"] is False
        assert "未知端点" in result["error"]


class TestManifestLiveSurface:
    def test_manifest_declares_live_endpoints_and_webview_page(self):
        with open(BROWSER_PLUGIN / "plugin.json", encoding="utf-8") as f:
            manifest = json.load(f)
        routes = {r["route_id"]: r for r in manifest.get("http_endpoints", [])}
        assert routes["browser_live_ticket"]["path"] == "/ext/browser_tool/live"
        assert routes["browser_live_ticket"]["auth"] == "user"
        assert routes["browser_live_page"]["path"] == "/ext/browser_tool/page/browser-live"
        pages = {p["id"]: p for p in manifest.get("contributes", {}).get("pages", [])}
        page = pages["browser_live"]
        assert page["widget"] == "webview"
        assert page["space"] == "workspace"
        assert page["props"]["pluginId"] == "browser_tool"
        assert page["props"]["htmlPath"] == "/page/browser-live"
        # CSP 白名单声明（宿主注入 CSP 的 img-src 扩展，fail-closed 归一）
        assert page["props"]["csp"]["imgSrc"] == ["http://127.0.0.1:*", "http://localhost:*"]


class TestBridgeConfigNesting:
    """_bridge_config 嵌套读：内核注入按 config_files[].id 命名空间嵌套
    （AGENTS.md 口径）；平铺兜底。dev 长期未炸纯因 BridgeClient 默认值恰等
    于配置值——装机侧 token 供给靠本读法。"""

    def test_nested_by_config_files_id(self, monkeypatch):
        server = _load_server()
        monkeypatch.setattr(
            server.plugin, "_injected_config",
            {"browser_tool": {"bridge": {"bridge_base": "http://127.0.0.1:9999", "token": "tk"}}},
            raising=False,
        )
        cfg = server._bridge_config()
        assert cfg["token"] == "tk"
        assert cfg["bridge_base"] == "http://127.0.0.1:9999"

    def test_flat_fallback(self, monkeypatch):
        server = _load_server()
        monkeypatch.setattr(
            server.plugin, "_injected_config",
            {"bridge": {"token": "flat-tk"}},
            raising=False,
        )
        assert server._bridge_config()["token"] == "flat-tk"

    def test_missing_config_returns_empty(self, monkeypatch):
        server = _load_server()
        monkeypatch.setattr(server.plugin, "_injected_config", {}, raising=False)
        assert server._bridge_config() == {}
