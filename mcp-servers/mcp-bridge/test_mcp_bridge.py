# @feature: FP-0.2.二 工具插件面 | @ci: python-coverage
# MCP Bridge 网关测试——upstream 会话管理 / 治理层 / HTTP 全链路。
#
# 上游用本目录 stub_upstream.py 替身（真 JSON-RPC stdio server），不依赖
# npx/playwright，测试可在断网环境跑。

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

# policy 裸模块可能被其他车道（security_check 等置前 sys.path 的测试）逐出
# 重解析——双实例防线：PolicyError 等一律在使用点导入（与 check_domain_policy
# 同一导入点，类身份一致），不在模块级缓存类对象。
from server import BridgeApp, make_handler  # noqa: E402
import upstream  # noqa: E402
from upstream import UpstreamError, UpstreamManager, UpstreamSession  # noqa: E402


def _policy_imports():
    from policy import BridgePolicy, PolicyError, is_private_host
    return BridgePolicy, PolicyError, is_private_host

STUB = [sys.executable, str(HERE / "stub_upstream.py")]


def base_config(tmp_path: Path, **over) -> dict:
    cfg = {
        "bind": "127.0.0.1",
        "port": 18765,
        "auth": {"token_env": "MCP_BRIDGE_TEST_TOKEN"},
        "audit_dir": str(tmp_path / "audit"),
        "session_idle_timeout_secs": 300,
        "upstreams": {
            "browser": {
                "command": STUB[0],
                "args": [STUB[1]],
                "tools": ["browser_navigate", "browser_snapshot", "browser_take_screenshot"],
                "domain_policy": {"url_arg_names": ["url"], "allow_private_addresses": False},
                "max_args_bytes": 65536,
            }
        },
    }
    cfg.update(over)
    return cfg


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("MCP_BRIDGE_TEST_TOKEN", "tok-abc")


# ── policy：认证 / 白名单 / 域名 ─────────────────────────────


class TestPolicy:
    def test_auth_missing_token_env_denies_everything(self, monkeypatch):
        monkeypatch.delenv("MCP_BRIDGE_TEST_TOKEN", raising=False)
        BridgePolicy, PolicyError, _ = _policy_imports()
        p = BridgePolicy(token_env="MCP_BRIDGE_TEST_TOKEN", audit_dir="")
        with pytest.raises(PolicyError) as e:
            p.check_auth("Bearer tok-anything")
        assert e.value.http_status == 401

    def test_auth_rejects_wrong_and_missing_header(self, monkeypatch):
        monkeypatch.setenv("MCP_BRIDGE_TEST_TOKEN", "tok-abc")
        BridgePolicy, PolicyError, _ = _policy_imports()
        p = BridgePolicy(token_env="MCP_BRIDGE_TEST_TOKEN", audit_dir="")
        for header in (None, "Bearer wrong", "Basic abc", "tok-abc"):
            with pytest.raises(PolicyError):
                p.check_auth(header)
        p.check_auth("Bearer tok-abc")  # 正确 token 放行

    def test_tool_whitelist_denies_unknown_tool(self):
        BridgePolicy, PolicyError, _ = _policy_imports()
        p = BridgePolicy(
            token_env="X", audit_dir="",
            upstreams={"browser": {"tools": ["browser_navigate"]}},
        )
        with pytest.raises(PolicyError) as e:
            p.check_tool("browser", {"name": "browser_hack", "arguments": {}})
        assert e.value.http_status == 403

    def test_tool_whitelist_allows_listed_tool(self):
        BridgePolicy, _, _ = _policy_imports()
        p = BridgePolicy(
            token_env="X", audit_dir="",
            upstreams={"browser": {"tools": ["browser_navigate"]}},
        )
        p.check_tool("browser", {"name": "browser_navigate", "arguments": {"url": "https://example.com"}})

    def test_unknown_upstream_404(self):
        BridgePolicy, PolicyError, _ = _policy_imports()
        p = BridgePolicy(token_env="X", audit_dir="")
        with pytest.raises(PolicyError) as e:
            p.check_tool("nope", {"name": "x"})
        assert e.value.http_status == 404

    def test_domain_policy_blocks_private_and_metadata(self):
        _, PolicyError, _ = _policy_imports()
        from policy import check_domain_policy
        p = {"url_arg_names": ["url"]}
        for url in ("http://127.0.0.1/x", "http://169.254.169.254/latest", "http://10.0.0.5/a",
                    "http://192.168.1.3/", "http://172.16.2.1/", "http://100.100.1.1/"):
            with pytest.raises(PolicyError):
                check_domain_policy(p, "browser_navigate", {"url": url})

    def test_domain_policy_allows_public(self):
        _, PolicyError, _ = _policy_imports()
        from policy import check_domain_policy
        check_domain_policy({"url_arg_names": ["url"]}, "browser_navigate", {"url": "https://example.com/page"})

    def test_domain_blocked_list(self):
        _, PolicyError, _ = _policy_imports()
        from policy import check_domain_policy
        pol = {"url_arg_names": ["url"], "blocked_domains": ["evil.com"]}
        with pytest.raises(PolicyError):
            check_domain_policy(pol, "browser_navigate", {"url": "https://sub.evil.com/x"})
        check_domain_policy(pol, "browser_navigate", {"url": "https://notevil.com/"})  # 后缀不误伤

    def test_domain_allowed_list(self):
        _, PolicyError, _ = _policy_imports()
        from policy import check_domain_policy
        pol = {"url_arg_names": ["url"], "allowed_domains": ["example.com"]}
        with pytest.raises(PolicyError):
            check_domain_policy(pol, "browser_navigate", {"url": "https://other.com/"})
        check_domain_policy(pol, "browser_navigate", {"url": "https://example.com/"})

    def test_non_http_scheme_rejected(self):
        _, PolicyError, _ = _policy_imports()
        from policy import check_domain_policy
        with pytest.raises(PolicyError):
            check_domain_policy({"url_arg_names": ["url"]}, "browser_navigate", {"url": "file:///etc/passwd"})

    def test_is_private_host_property_table(self):
        _, _, is_private_host = _policy_imports()
        assert is_private_host("localhost")
        assert is_private_host("METADATA.google.internal")
        assert is_private_host("10.255.1.1")
        assert is_private_host("127.0.0.1")
        assert is_private_host("169.254.0.1")
        assert is_private_host("172.31.9.9")
        assert is_private_host("172.15.0.1") is False  # 172.16-31 才是私网
        assert is_private_host("100.64.0.1")
        assert is_private_host("100.34.0.1") is False  # 100.64/10 之外
        assert is_private_host("8.8.8.8") is False
        assert is_private_host("example.com") is False


# ── 审计 ─────────────────────────────────────────────────────


class TestAudit:
    def test_audit_jsonl_line_written(self, tmp_path):
        BridgePolicy, _, _ = _policy_imports()
        p = BridgePolicy(token_env="X", audit_dir=str(tmp_path / "audit"))
        p.audit("browser", "sandbox", "sess-1", "tools/call", "browser_navigate", "allow", "", 12.5)
        day = time.strftime("%Y%m%d", time.gmtime())
        f = tmp_path / "audit" / f"audit-{day}.jsonl"
        assert f.is_file()
        rec = json.loads(f.read_text(encoding="utf-8").strip())
        assert rec["upstream"] == "browser"
        assert rec["caller"] == "sandbox"
        assert rec["decision"] == "allow"
        assert rec["elapsed_ms"] == 12.5

    def test_audit_disabled_by_empty_dir(self, tmp_path):
        BridgePolicy, _, _ = _policy_imports()
        p = BridgePolicy(token_env="X", audit_dir="")
        p.audit("browser", "host", "s", "tools/call", "t", "allow")  # 不应抛异常


# ── upstream：stdio 会话 ─────────────────────────────────────


class TestUpstream:
    def test_request_roundtrip(self):
        s = UpstreamSession("u", "s1", STUB)
        try:
            result = s.request("tools/list")
            assert "tools" in result
        finally:
            s.kill()

    def test_tools_call_echoes_arguments(self):
        s = UpstreamSession("u", "s1", STUB)
        try:
            result = s.request("tools/call", {"name": "stub", "arguments": {"url": "https://example.com"}})
            assert result["content"][0]["text"].startswith("https://example.com")
        finally:
            s.kill()

    def test_upstream_error_raises(self):
        s = UpstreamSession("u", "s1", STUB)
        try:
            with pytest.raises(UpstreamError):
                s.request("tools/call", {"name": "boom", "arguments": {}})
        finally:
            s.kill()

    def test_spawn_failure_is_upstream_error(self):
        s = UpstreamSession("u", "s1", [sys.executable, str(HERE / "definitely_missing_stub.py")])
        with pytest.raises(UpstreamError):
            s.request("tools/list")

    def test_manager_get_or_create_same_key_reuses(self):
        m = UpstreamManager(idle_timeout_secs=1)
        a = m.get_or_create("u", "s1", STUB)
        b = m.get_or_create("u", "s1", STUB)
        assert a is b
        m.shutdown_all()

    def test_manager_discard_kills(self):
        m = UpstreamManager(idle_timeout_secs=60)
        s = m.get_or_create("u", "s1", STUB)
        m.discard("u", "s1")
        assert not s.is_alive()
        assert m.session_count() == 0

    def test_sweep_idle_reclaims(self):
        m = UpstreamManager(idle_timeout_secs=0.05)
        m.get_or_create("u", "s1", STUB)
        m.get_or_create("u", "s2", STUB)
        time.sleep(0.15)
        reclaimed = m.sweep_idle()
        assert ("u", "s1") in reclaimed and ("u", "s2") in reclaimed
        assert m.session_count() == 0


# ── HTTP 全链路（真 server + urllib）──────────────────────────


def _post(url: str, payload: dict, token: str | None = "tok-abc", session: str | None = None, caller: str | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if session:
        req.add_header("X-Bridge-Session", session)
    if caller:
        req.add_header("X-Bridge-Caller", caller)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


class TestHTTP:
    @pytest.fixture()
    def server(self, tmp_path):
        import threading
        from http.server import ThreadingHTTPServer

        app = BridgeApp(base_config(tmp_path))
        srv = ThreadingHTTPServer(("127.0.0.1", 18765), make_handler(app))
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        yield f"http://127.0.0.1:18765", app
        srv.shutdown()
        app.manager.shutdown_all()

    def test_health_no_auth(self, server):
        url, app = server
        with urllib.request.urlopen(f"{url}/health", timeout=5) as resp:
            body = json.loads(resp.read())
        assert resp.status == 200
        assert body["status"] == "ok"

    def test_missing_token_401(self, server):
        url, _ = server
        code, body = _post(f"{url}/mcp/browser", {"method": "tools/list"}, token=None)
        assert code == 401
        assert "error" in body

    def test_wrong_token_401(self, server):
        url, _ = server
        code, _ = _post(f"{url}/mcp/browser", {"method": "tools/list"}, token="bad")
        assert code == 401

    def test_unknown_upstream_404(self, server):
        url, _ = server
        code, body = _post(f"{url}/mcp/nope", {"method": "tools/list"})
        assert code == 404

    def test_initialize_gateway_self_answer(self, server):
        url, _ = server
        code, body = _post(
            f"{url}/mcp/browser",
            {"method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}}},
        )
        assert code == 200
        assert body["result"]["serverInfo"]["name"] == "mcp-bridge:browser"

    def test_tools_list_via_upstream(self, server):
        url, _ = server
        code, body = _post(f"{url}/mcp/browser", {"method": "tools/list"})
        assert code == 200
        names = [t["name"] for t in body["result"]["tools"]]
        assert "browser_navigate" in names

    def test_tools_call_roundtrip(self, server):
        url, _ = server
        code, body = _post(
            f"{url}/mcp/browser",
            {"method": "tools/call", "params": {"name": "browser_navigate", "arguments": {"url": "https://example.com"}}},
            session="sess-A",
        )
        assert code == 200
        assert "https://example.com" in body["result"]["content"][0]["text"]

    def test_whitelisted_tool_denied_403(self, server):
        url, _ = server
        code, body = _post(f"{url}/mcp/browser", {"method": "tools/call", "params": {"name": "browser_evil"}})
        assert code == 403
        assert "白名单" in body["error"]

    def test_private_url_denied_403(self, server):
        url, _ = server
        code, body = _post(
            f"{url}/mcp/browser",
            {"method": "tools/call", "params": {"name": "browser_navigate", "arguments": {"url": "http://127.0.0.1:9100"}}},
        )
        assert code == 403
        assert "内网" in body["error"]

    def test_audit_records_host_and_sandbox(self, server, tmp_path):
        url, _ = server
        _post(f"{url}/mcp/browser", {"method": "tools/list"}, session="sX", caller="sandbox")
        _post(f"{url}/mcp/browser", {"method": "tools/list"}, session="sY", caller="host")
        day = time.strftime("%Y%m%d", time.gmtime())
        audit_file = tmp_path / "audit" / f"audit-{day}.jsonl"
        lines = [json.loads(x) for x in audit_file.read_text(encoding="utf-8").strip().splitlines()]
        callers = {r["caller"] for r in lines}
        assert "sandbox" in callers and "host" in callers

    def test_session_key_isolates_upstream_sessions(self, server):
        url, app = server
        _post(f"{url}/mcp/browser", {"method": "tools/list"}, session="s1")
        _post(f"{url}/mcp/browser", {"method": "tools/list"}, session="s2")
        assert app.manager.session_count() == 2

    def test_bad_json_400(self, server):
        url, _ = server
        req = urllib.request.Request(f"{url}/mcp/browser", data=b"not-json", method="POST")
        req.add_header("Authorization", "Bearer tok-abc")
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("should fail")
        except urllib.error.HTTPError as e:
            assert e.code == 400

class TestWindowsCommandResolution:
    def test_windows_resolves_npx_cmd(self, monkeypatch):
        monkeypatch.setattr(upstream.os, "name", "nt")
        monkeypatch.setenv("PATH", r"C:\Program Files\nodejs")
        monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
        r = upstream.resolve_windows_command("npx")
        assert r.lower().endswith(".cmd") or r.lower().endswith(".exe")

    def test_non_windows_passthrough(self, monkeypatch):
        monkeypatch.setattr(upstream.os, "name", "linux")
        assert upstream.resolve_windows_command("npx") == "npx"

    def test_extension_command_passthrough(self, monkeypatch):
        monkeypatch.setattr(upstream.os, "name", "nt")
        assert upstream.resolve_windows_command("python3.11") == "python3.11"
