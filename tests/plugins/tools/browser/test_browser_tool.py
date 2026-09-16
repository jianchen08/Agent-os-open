# @feature: FP-0.2.二 工具插件面 | @ci: python-coverage
# browser_tool 插件测试——manifest 一致性 / 参数构造 / 双路径传输 / 结果归一。
#
# Bridge 与上游 @playwright/mcp 用 stub 替身（真 HTTP server，同 mcp-bridge
# 测试的 stub_upstream），不依赖 npx/playwright。

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import importlib.util

import pytest

pytestmark = pytest.mark.integration

HERE = Path(__file__).parent
BROWSER_PLUGIN = HERE.parent.parent.parent.parent / "plugins" / "shared" / "tools" / "browser"
BRIDGE_DIR = HERE.parent.parent.parent.parent / "mcp-servers" / "mcp-bridge"

sys.path.insert(0, str(BRIDGE_DIR))
sys.path.insert(0, str(BROWSER_PLUGIN))

from bridge_client import BridgeClient, BridgeClientError  # noqa: E402
from tool import (  # noqa: E402
    BROWSER_TOOLS,
    BrowserTool,
    _build_arguments,
    _to_tool_result,
)

STUB = [sys.executable, str(BRIDGE_DIR / "stub_upstream.py")]


# ── manifest 一致性 ───────────────────────────────────────────


class TestManifest:
    @pytest.fixture()
    def manifest(self) -> dict:
        with open(BROWSER_PLUGIN / "plugin.json", encoding="utf-8") as f:
            return json.load(f)

    def test_six_tools_declared(self, manifest):
        names = [t["name"] for t in manifest["capabilities"]["tools"]]
        assert names == list(BROWSER_TOOLS)

    def test_every_tool_has_output_schema_and_render(self, manifest):
        for t in manifest["capabilities"]["tools"]:
            assert t.get("output_schema"), f"{t['name']} 缺 output_schema"
            assert t.get("render", {}).get("card"), f"{t['name']} 缺 render"

    def test_render_intents(self, manifest):
        cards = {t["name"]: t["render"]["card"] for t in manifest["capabilities"]["tools"]}
        assert cards["browser_navigate"] == "web"
        assert cards["browser_take_screenshot"] == "image"

    def test_bridge_config_file_referenced(self, manifest):
        paths = [c["path"] for c in manifest.get("config_files", [])]
        # B 类配置归位后落点 config/plugins/<plugin_id>/（manifest 已同步迁移）
        assert "config/plugins/browser/browser.yaml" in paths


# ── server.py 导入与工具面校验 ───────────────────────────────


def _load_browser_server() -> object:
    """按显式路径加载 browser 插件 server.py（裸名 server 会被其他插件劫持）。"""
    mod_name = "browser_plugin_server_under_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    import importlib

    spec = importlib.util.spec_from_file_location(mod_name, BROWSER_PLUGIN / "server.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


class TestServerSurface:
    def test_six_tools_registered(self):
        plugin = _load_browser_server().plugin
        registered = set(plugin._tools.keys())
        assert registered == set(BROWSER_TOOLS)

    def test_registered_schemas_match_manifest(self):
        plugin = _load_browser_server().plugin
        with open(BROWSER_PLUGIN / "plugin.json", encoding="utf-8") as f:
            manifest = json.load(f)
        declared = {t["name"]: t for t in manifest["capabilities"]["tools"]}
        for name, tooldef in plugin._tools.items():
            assert tooldef.schema == declared[name]["input_schema"], name
            assert tooldef.render == declared[name]["render"], name


# ── 参数构造（LLM 入参 → 上游契约）───────────────────────────


class TestBuildArguments:
    def test_navigate_passes_url_only(self):
        args = _build_arguments("browser_navigate", {"url": "https://example.com", "session_id": "s1"})
        assert args == {"url": "https://example.com"}

    def test_click_passthrough_fields(self):
        args = _build_arguments(
            "browser_click",
            {"target": "e5", "element": "登录按钮", "button": "left", "session_id": "s1", "_container_id": "c1"},
        )
        assert args == {"target": "e5", "element": "登录按钮", "button": "left"}

    def test_screenshot_defaults(self):
        args = _build_arguments("browser_take_screenshot", {"scale": None})
        assert args["scale"] == "css"
        assert args["type"] == "png"

    def test_screenshot_keeps_explicit(self):
        args = _build_arguments("browser_take_screenshot", {"scale": "device", "fullPage": True})
        assert args == {"scale": "device", "fullPage": True, "type": "png"}


# ── 结果归一 ─────────────────────────────────────────────────


class TestToToolResult:
    def test_text_content_to_snapshot(self):
        mcp = {"content": [{"type": "text", "text": "- page snapshot"}]}
        r = _to_tool_result("browser_snapshot", mcp)
        assert r.success
        assert r.output["snapshot_text"] == "- page snapshot"

    def test_image_content_saved_to_workspace(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        import base64

        png = b"\x89PNG\r\n\x1a\nfake"
        mcp = {"content": [{"type": "image", "data": base64.b64encode(png).decode(), "mimeType": "image/png"}]}
        r = _to_tool_result("browser_take_screenshot", mcp)
        assert r.success
        assert r.output["file_path"].endswith(".png")
        assert (tmp_path / os.path.basename(r.output["file_path"])).read_bytes() == png

    def test_is_error_maps_to_failure(self):
        mcp = {"isError": True, "content": [{"type": "text", "text": "page not found"}]}
        r = _to_tool_result("browser_navigate", mcp)
        assert not r.success
        assert "page not found" in r.error

    def test_oversized_image_rejected(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        import base64

        big = b"x" * (8 * 1024 * 1024 + 1)
        mcp = {"content": [{"type": "image", "data": base64.b64encode(big).decode(), "mimeType": "image/png"}]}
        r = _to_tool_result("browser_take_screenshot", mcp)
        assert not r.success
        assert "超限" in r.error


# ── tool.execute 双路径（stub Bridge + stub 上游）────────────


def _stub_bridge_config(tmp_path: Path, port: int) -> dict:
    return {"bridge_base": f"http://127.0.0.1:{port}", "upstream": "browser", "timeout_secs": 30}


class TestExecuteHostPath:
    """宿主直发路径（非隔离会话）。"""

    @pytest.fixture()
    def bridge_server(self, tmp_path, monkeypatch):
        import threading
        from http.server import ThreadingHTTPServer

        monkeypatch.setenv("MCP_BRIDGE_TEST_TOKEN", "tok-abc")
        monkeypatch.setattr(BridgeClient, "_resolve_token", staticmethod(lambda cfg: "tok-abc"))
        sys.path.insert(0, str(BRIDGE_DIR))
        from server import BridgeApp, make_handler

        cfg = {
            "bind": "127.0.0.1",
            "port": 18766,
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
        srv = ThreadingHTTPServer(("127.0.0.1", 18766), make_handler(app))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        yield app
        srv.shutdown()
        app.manager.shutdown_all()
        sys.path.remove(str(BRIDGE_DIR))

    @pytest.mark.asyncio
    async def test_navigate_roundtrip(self, bridge_server, monkeypatch):
        monkeypatch.setattr(BridgeClient, "_resolve_token", staticmethod(lambda cfg: "tok-abc"))
        t = BrowserTool({"bridge_base": "http://127.0.0.1:18766", "upstream": "browser", "timeout_secs": 30})
        r = await t.execute({"_tool_name": "browser_navigate", "url": "https://example.com", "session_id": "sess-9"})
        assert r.success, r.error
        # stub 回显 url
        assert "https://example.com" in r.output["snapshot_text"]

    @pytest.mark.asyncio
    async def test_bridge_down_is_clean_retryable_error(self, monkeypatch):
        monkeypatch.setattr(BridgeClient, "_resolve_token", staticmethod(lambda cfg: "tok-abc"))
        t = BrowserTool({"bridge_base": "http://127.0.0.1:19999", "upstream": "browser", "timeout_secs": 3})
        r = await t.execute({"_tool_name": "browser_navigate", "url": "https://example.com"})
        assert not r.success
        assert "start_bridge" in r.error

    @pytest.mark.asyncio
    async def test_whitelist_denied_is_clean_error(self, bridge_server, monkeypatch):
        monkeypatch.setattr(BridgeClient, "_resolve_token", staticmethod(lambda cfg: "tok-abc"))
        t = BrowserTool({"bridge_base": "http://127.0.0.1:18766", "upstream": "browser", "timeout_secs": 30})
        r = await t.execute({"_tool_name": "browser_hack", "x": 1})
        assert not r.success
        assert "未知的浏览器工具" in r.error


class TestExecuteContainerPath:
    """容器路径：docker exec 替身（不真跑 docker）。"""

    @pytest.mark.asyncio
    async def test_container_path_invokes_docker_exec(self, tmp_path, monkeypatch):
        monkeypatch.setattr(BridgeClient, "_resolve_token", staticmethod(lambda cfg: "tok-abc"))
        # 替身 docker exec：消费 stdin（内嵌脚本），模拟容器内客户端输出。
        def fake_exec(cmd, **kwargs):
            if cmd[:2] == ["docker", "network"]:
                # _docker_gateway_ip 的网关探测调用——返回空（无网关候选）。
                class Net:
                    returncode = 1
                    stdout = b""
                return Net()

            assert cmd[:3] == ["docker", "exec", "-i"]
            assert "python" in cmd
            class P:
                returncode = 0
                stderr = b""
                stdout = json.dumps({
                    "ok": True,
                    "status": 200,
                    "body": {"result": {"content": [{"type": "text", "text": "sandbox-ok"}]}},
                }).encode()
            return P()

        import bridge_client as bc
        monkeypatch.setattr(bc.subprocess, "run", fake_exec)
        monkeypatch.setenv("AGENTOS_BRIDGE_URL", "http://10.9.8.7:8765")
        t = BrowserTool({"upstream": "browser", "timeout_secs": 10})
        # _container_id 由 isolation_guard 注入 args（tool.py 据此判 sandbox 并
        # 转环境变量给容器内客户端）。
        r = await t.execute({"_tool_name": "browser_navigate", "url": "https://example.com", "_container_id": "c-test-1"})
        assert r.success
        assert "sandbox-ok" in r.output["snapshot_text"]

    @pytest.mark.asyncio
    async def test_container_path_without_container_id_fails_clean(self, monkeypatch):
        monkeypatch.setattr(BridgeClient, "_resolve_token", staticmethod(lambda cfg: "tok-abc"))
        monkeypatch.delenv("_CONTAINER_ID", raising=False)
        # 无 _container_id → caller 判为 host → 走直发路径；断言不误入容器路径。
        import bridge_client as bc
        called = []

        def fail_exec(*a, **kw):
            called.append(1)
            raise AssertionError("不应触发 docker exec")

        monkeypatch.setattr(bc.subprocess, "run", fail_exec)
        t = BrowserTool({"bridge_base": "http://127.0.0.1:19999", "upstream": "browser", "timeout_secs": 2})
        r = await t.execute({"_tool_name": "browser_navigate", "url": "https://example.com"})
        assert not r.success
        assert not called

    @pytest.mark.asyncio
    async def test_container_candidates_include_env_and_hostname(self, monkeypatch):
        monkeypatch.setattr(BridgeClient, "_resolve_token", staticmethod(lambda cfg: "tok-abc"))
        monkeypatch.setenv("AGENTOS_BRIDGE_URL", "http://10.9.8.7:8765")
        monkeypatch.setattr("bridge_client._docker_gateway_ip", lambda: "172.17.0.1")
        client = BridgeClient({"bridge_base": "http://127.0.0.1:8765", "upstream": "browser"})
        cands = client._container_base_candidates()
        assert cands[0] == "http://10.9.8.7:8765"
        assert "http://host.docker.internal:8765" in cands
        assert "http://172.17.0.1:8765" in cands


# ── token 解析 ───────────────────────────────────────────────


class TestToken:
    @pytest.mark.asyncio
    async def test_missing_token_is_clean_error(self, monkeypatch, tmp_path):
        monkeypatch.delenv("AGENTOS_BRIDGE_TOKEN", raising=False)
        monkeypatch.setattr("bridge_client._find_project_root", lambda: "")
        t = BrowserTool({"bridge_base": "http://127.0.0.1:8765", "upstream": "browser"})
        r = await t.execute({"_tool_name": "browser_navigate", "url": "https://example.com"})
        assert not r.success
        assert "AGENTOS_BRIDGE_TOKEN" in r.error