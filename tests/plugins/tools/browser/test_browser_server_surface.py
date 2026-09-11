# browser_tool 缺口补测——bridge_client 分支 / server handler 全链。
#
# 覆盖：_rpc 状态映射（401/403/502/其他）、直发 HTTPError body 解析、
# 容器路径失败分支（非零退出/无输出/坏 JSON）、token .env 兜底、
# server.py handler 真调用（BridgeApp + stub 上游全链）。

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

HERE = Path(__file__).parent
BROWSER_PLUGIN = HERE.parent.parent.parent.parent / "plugins" / "shared" / "tools" / "browser"
BRIDGE_DIR = HERE.parent.parent.parent.parent / "mcp-servers" / "mcp-bridge"

sys.path.insert(0, str(BRIDGE_DIR))
sys.path.insert(0, str(BROWSER_PLUGIN))

import importlib.util  # noqa: E402

from bridge_client import BridgeClient, BridgeClientError  # noqa: E402
from tool import BROWSER_TOOLS  # noqa: E402

STUB = [sys.executable, str(BRIDGE_DIR / "stub_upstream.py")]


def _load_bridge_server():
    """按显式路径加载 mcp-bridge 的 server.py（裸名 server 被插件 server 劫持）。"""
    if "mcp_bridge_server_under_test" in sys.modules:
        return sys.modules["mcp_bridge_server_under_test"]
    spec = importlib.util.spec_from_file_location("mcp_bridge_server_under_test", BRIDGE_DIR / "server.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["mcp_bridge_server_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


class TestRpcStatusMapping:
    def _client(self, monkeypatch, status: int, body: dict) -> BridgeClient:
        monkeypatch.setattr(BridgeClient, "_resolve_token", staticmethod(lambda cfg: "tok-abc"))
        c = BridgeClient({"bridge_base": "http://127.0.0.1:8765", "upstream": "browser"})
        monkeypatch.setattr(c, "_send_direct", lambda payload: {"ok": False, "status": status, "body": body})
        return c

    @pytest.mark.asyncio
    async def test_401_maps_to_auth_error(self, monkeypatch):
        c = self._client(monkeypatch, 401, {"error": "认证失败"})
        with pytest.raises(BridgeClientError) as e:
            c.list_tools()
        assert "认证失败" in str(e.value)

    @pytest.mark.asyncio
    async def test_403_maps_to_policy_error(self, monkeypatch):
        c = self._client(monkeypatch, 403, {"error": "白名单"})
        with pytest.raises(BridgeClientError) as e:
            c.list_tools()
        assert "治理拒绝" in str(e.value)

    @pytest.mark.asyncio
    async def test_502_maps_to_upstream_error(self, monkeypatch):
        c = self._client(monkeypatch, 502, {"error": "上游死"})
        with pytest.raises(BridgeClientError) as e:
            c.list_tools()
        assert "上游不可用" in str(e.value)

    @pytest.mark.asyncio
    async def test_generic_status_passthrough(self, monkeypatch):
        c = self._client(monkeypatch, 400, {"error": "坏请求"})
        with pytest.raises(BridgeClientError) as e:
            c.list_tools()
        assert "status=400" in str(e.value)


class TestDirectTransportErrorBody:
    @pytest.mark.asyncio
    async def test_http_error_body_parsed(self, monkeypatch):
        import threading
        from http.server import ThreadingHTTPServer

        monkeypatch.setattr(BridgeClient, "_resolve_token", staticmethod(lambda cfg: "tok-abc"))
        bridge_server_mod = _load_bridge_server()
        BridgeApp, make_handler = bridge_server_mod.BridgeApp, bridge_server_mod.make_handler

        cfg = {
            "bind": "127.0.0.1",
            "port": 18767,
            "auth": {"token_env": "MCP_BRIDGE_TEST_TOKEN"},
            "audit_dir": "",
            "upstreams": {"browser": {"command": STUB[0], "args": [STUB[1]], "tools": ["browser_navigate"]}},
        }
        monkeypatch.setenv("MCP_BRIDGE_TEST_TOKEN", "real-tok")
        app = BridgeApp(cfg)
        srv = ThreadingHTTPServer(("127.0.0.1", 18767), make_handler(app))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            c = BridgeClient({"bridge_base": "http://127.0.0.1:18767", "upstream": "browser"})
            with pytest.raises(BridgeClientError) as e:
                c._token = "wrong"
                c.list_tools()
            assert "认证失败" in str(e.value)
        finally:
            srv.shutdown()
            app.manager.shutdown_all()


class TestContainerFailureBranches:
    @pytest.mark.asyncio
    async def test_exec_nonzero_exit_is_clean_error(self, monkeypatch):
        monkeypatch.setattr(BridgeClient, "_resolve_token", staticmethod(lambda cfg: "tok-abc"))
        import bridge_client as bc

        def fake_exec(cmd, **kwargs):
            class P:
                returncode = 1
                stderr = b"exec failed in container"
                stdout = b""

            return P()

        monkeypatch.setattr(bc.subprocess, "run", fake_exec)
        monkeypatch.setenv("_CONTAINER_ID", "c-test-1")
        c = BridgeClient({"upstream": "browser", "timeout_secs": 5}, session_id="s", caller="sandbox")
        r = c._send({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert not r["ok"]
        assert "容器内 MCP Client 执行失败" in r["body"]["error"]

    @pytest.mark.asyncio
    async def test_exec_no_output_is_clean_error(self, monkeypatch):
        monkeypatch.setattr(BridgeClient, "_resolve_token", staticmethod(lambda cfg: "tok-abc"))
        import bridge_client as bc

        def fake_exec(cmd, **kwargs):
            class P:
                returncode = 0
                stderr = b""
                stdout = b"   \n  "

            return P()

        monkeypatch.setattr(bc.subprocess, "run", fake_exec)
        monkeypatch.setenv("_CONTAINER_ID", "c-test-1")
        c = BridgeClient({"upstream": "browser", "timeout_secs": 5}, session_id="s", caller="sandbox")
        r = c._send({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert not r["ok"]
        assert "无输出" in r["body"]["error"]

    @pytest.mark.asyncio
    async def test_exec_bad_json_output_is_clean_error(self, monkeypatch):
        monkeypatch.setattr(BridgeClient, "_resolve_token", staticmethod(lambda cfg: "tok-abc"))
        import bridge_client as bc

        def fake_exec(cmd, **kwargs):
            class P:
                returncode = 0
                stderr = b""
                stdout = b"not-json-line"

            return P()

        monkeypatch.setattr(bc.subprocess, "run", fake_exec)
        monkeypatch.setenv("_CONTAINER_ID", "c-test-1")
        c = BridgeClient({"upstream": "browser", "timeout_secs": 5}, session_id="s", caller="sandbox")
        r = c._send({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert not r["ok"]
        assert "不可解析" in r["body"]["error"]


class TestTokenFromEnvFile:
    @pytest.mark.asyncio
    async def test_env_file_fallback(self, monkeypatch, tmp_path):
        monkeypatch.delenv("AGENTOS_BRIDGE_TOKEN", raising=False)
        (tmp_path / ".env").write_text("AGENTOS_BRIDGE_TOKEN=from-dotenv\n", encoding="utf-8")
        monkeypatch.setattr("bridge_client._find_project_root", lambda: str(tmp_path))
        import bridge_client as bc

        c = bc.BridgeClient({"bridge_base": "http://127.0.0.1:8765", "upstream": "browser"})
        assert c._token == "from-dotenv"

    @pytest.mark.asyncio
    async def test_env_file_tolerates_quotes(self, monkeypatch, tmp_path):
        monkeypatch.delenv("AGENTOS_BRIDGE_TOKEN", raising=False)
        (tmp_path / ".env").write_text('AGENTOS_BRIDGE_TOKEN="quoted-token"\n', encoding="utf-8")
        monkeypatch.setattr("bridge_client._find_project_root", lambda: str(tmp_path))
        import bridge_client as bc

        c = bc.BridgeClient({"bridge_base": "http://127.0.0.1:8765", "upstream": "browser"})
        assert c._token == "quoted-token"


class TestServerHandlerRoundtrip:
    """server.py handler 真调用（BridgeApp + stub 上游全链）。"""

    @pytest.fixture()
    def bridge_server(self, monkeypatch):
        import threading
        from http.server import ThreadingHTTPServer

        monkeypatch.setenv("MCP_BRIDGE_TEST_TOKEN", "tok-abc")
        monkeypatch.setattr(BridgeClient, "_resolve_token", staticmethod(lambda cfg: "tok-abc"))
        bridge_server_mod = _load_bridge_server()
        BridgeApp, make_handler = bridge_server_mod.BridgeApp, bridge_server_mod.make_handler

        cfg = {
            "bind": "127.0.0.1",
            "port": 18768,
            "auth": {"token_env": "MCP_BRIDGE_TEST_TOKEN"},
            "audit_dir": "",
            "upstreams": {
                "browser": {
                    "command": STUB[0],
                    "args": [STUB[1]],
                    "tools": list(BROWSER_TOOLS) + ["stub"],
                    "domain_policy": {"url_arg_names": ["url"], "allow_private_addresses": False},
                }
            },
        }
        app = BridgeApp(cfg)
        srv = ThreadingHTTPServer(("127.0.0.1", 18768), make_handler(app))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        yield app
        srv.shutdown()
        app.manager.shutdown_all()

    @pytest.mark.asyncio
    async def test_handler_end_to_end(self, bridge_server, monkeypatch):
        monkeypatch.setattr(BridgeClient, "_resolve_token", staticmethod(lambda cfg: "tok-abc"))
        import importlib.util as _ilu
        mod_name = "browser_plugin_server_under_test"
        if mod_name not in sys.modules:
            spec2 = _ilu.spec_from_file_location(mod_name, BROWSER_PLUGIN / "server.py")
            m2 = _ilu.module_from_spec(spec2)
            sys.modules[mod_name] = m2
            spec2.loader.exec_module(m2)
        server_mod = sys.modules[mod_name]
        monkeypatch.setattr(
            server_mod,
            "_bridge_config",
            lambda: {"bridge_base": "http://127.0.0.1:18768", "upstream": "browser", "timeout_secs": 30},
        )
        handler = server_mod.plugin._tools["browser_navigate"].handler
        r = await handler(url="https://example.com", session_id="sess-77")
        assert "https://example.com" in r.get("snapshot_text", "")

    @pytest.mark.asyncio
    async def test_handler_failure_envelope(self, bridge_server, monkeypatch):
        monkeypatch.setattr(BridgeClient, "_resolve_token", staticmethod(lambda cfg: "tok-abc"))
        import bridge_client as bc

        def fake_exec(cmd, **kwargs):
            class P:
                returncode = 1
                stderr = b"boom"
                stdout = b""

            return P()

        monkeypatch.setattr(bc.subprocess, "run", fake_exec)
        import importlib.util as _ilu2
        mod_name = "browser_plugin_server_under_test"
        if mod_name not in sys.modules:
            spec2 = _ilu2.spec_from_file_location(mod_name, BROWSER_PLUGIN / "server.py")
            m2 = _ilu2.module_from_spec(spec2)
            sys.modules[mod_name] = m2
            spec2.loader.exec_module(m2)
        server_mod = sys.modules[mod_name]
        handler = server_mod.plugin._tools["browser_navigate"].handler
        r = await handler(url="https://example.com", _container_id="c1")
        assert "error" in r and r.get("status") == 500