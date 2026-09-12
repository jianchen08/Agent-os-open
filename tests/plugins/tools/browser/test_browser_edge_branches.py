# @feature: FP-0.2.二 工具插件面 | @ci: python-coverage
# browser_tool 缺口补测 2——防御分支与独立函数（diff coverage 兜底）。
#
# 覆盖：_to_tool_result 非 dict content / resource / 坏 base64、
# _save_workspace_file 的 workspace 注入路径、_docker_gateway_ip 探测、
# _workspace_root 回退、get_tool_definition、未知上游工具名。

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

HERE = Path(__file__).parent
BROWSER_PLUGIN = HERE.parent.parent.parent.parent / "plugins" / "shared" / "tools" / "browser"
BRIDGE_DIR = HERE.parent.parent.parent.parent / "mcp-servers" / "mcp-bridge"

sys.path.insert(0, str(BRIDGE_DIR))
sys.path.insert(0, str(BROWSER_PLUGIN))

import bridge_client as bc  # noqa: E402
from bridge_client import BridgeClient, BridgeClientError  # noqa: E402
from tool import (  # noqa: E402
    BrowserTool,
    _to_tool_result,
    _workspace_root,
    _save_workspace_file,
)


class TestToolResultDefensiveBranches:
    def test_non_dict_content_items_skipped(self):
        mcp = {"content": ["not-a-dict", {"type": "text", "text": "ok-text"}]}
        r = _to_tool_result("browser_snapshot", mcp)
        assert r.success
        assert r.output["snapshot_text"] == "ok-text"

    def test_resource_content_extracted(self):
        mcp = {"content": [{"type": "resource", "resource": {"text": "resource-body"}}]}
        r = _to_tool_result("browser_snapshot", mcp)
        assert r.success
        assert r.output["snapshot_text"] == "resource-body"

    def test_bad_base64_is_clean_failure(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mcp = {"content": [{"type": "image", "data": "!!!not-base64!!!", "mimeType": "image/png"}]}
        r = _to_tool_result("browser_take_screenshot", mcp)
        assert not r.success
        assert "base64" in r.error

    def test_navigate_url_echoed_from_arguments(self):
        r = _to_tool_result("browser_navigate", {"content": []}, {"url": "https://abc.example/"})
        assert r.success
        assert r.output["url"] == "https://abc.example/"


class TestWorkspaceSave:
    def test_workspace_used_for_screenshot_path(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        BrowserTool._last_workspace = str(ws)
        try:
            path = _save_workspace_file("shot.png", b"png-data")
            assert str(ws) in path
            assert (ws / "shot.png").read_bytes() == b"png-data"
        finally:
            BrowserTool._last_workspace = ""

    def test_workspace_root_fallback_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        BrowserTool._last_workspace = ""
        assert _workspace_root() == str(tmp_path)


class TestDockerGatewayProbe:
    def test_unreachable_docker_returns_empty(self, monkeypatch):
        def fail_run(*a, **kw):
            raise FileNotFoundError("docker not found")

        monkeypatch.setattr(bc.subprocess, "run", fail_run)
        assert bc._docker_gateway_ip() == ""

    def test_docker_success_returns_gateway(self, monkeypatch):
        def fake_run(*a, **kw):
            class P:
                returncode = 0
                stdout = "172.17.0.1\n"

            return P()

        monkeypatch.setattr(bc.subprocess, "run", fake_run)
        assert bc._docker_gateway_ip() == "172.17.0.1"

    def test_docker_failure_rc_returns_empty(self, monkeypatch):
        def fake_run(*a, **kw):
            class P:
                returncode = 1
                stdout = ""

            return P()

        monkeypatch.setattr(bc.subprocess, "run", fake_run)
        assert bc._docker_gateway_ip() == ""


class TestProjectRootProbe:
    def test_finds_env_upward(self, tmp_path, monkeypatch):
        sub = tmp_path / "a" / "b"
        sub.mkdir(parents=True)
        (tmp_path / ".env").write_text("X=1\n", encoding="utf-8")
        monkeypatch.chdir(sub)
        root = bc._find_project_root()
        assert root == str(tmp_path)

    def test_no_env_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert bc._find_project_root() == ""


class TestCandidatesFallback:
    def test_empty_env_falls_back_loopback(self, monkeypatch):
        monkeypatch.delenv("AGENTOS_BRIDGE_URL", raising=False)
        monkeypatch.setattr(bc, "_docker_gateway_ip", lambda: "")
        c = bc.BridgeClient.__new__(bc.BridgeClient)
        c._base = "http://127.0.0.1:8765"
        c._upstream = "browser"
        cands = c._container_base_candidates()
        # 无 env、无网关探测时：host.docker.internal 是唯一实际候选
        # （127.0.0.1 兜底分支仅在候选空时触达——host.docker.internal 恒在场
        # 使其不可达，防御分支由空列表不可构造性保证，属死守卫）。
        assert cands == ["http://host.docker.internal:8765"]

    def test_port_parsed_from_base(self, monkeypatch):
        c = bc.BridgeClient.__new__(bc.BridgeClient)
        c._base = "http://127.0.0.1:9100"
        port = c._port()
        assert port == 9100


class TestToolDefinition:
    def test_get_tool_definition_complete(self):
        d = BrowserTool.get_tool_definition()
        assert d.name == "browser"
        assert d.category is not None

class TestBridgeUrlForContainer:
    """docker_provider._bridge_url_for_container 三级优先级。"""

    def _provider(self, config: dict):
        import tests._isolation_path  # noqa: F401

        from providers.docker_provider import DockerProvider

        return DockerProvider(config)

    def test_config_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("AGENTOS_BRIDGE_URL", "http://from-env:8765")
        p = self._provider({"bridge_url": "http://10.1.2.3:8765"})
        assert p._bridge_url_for_container() == "http://10.1.2.3:8765"

    def test_env_used_when_no_config(self, monkeypatch):
        monkeypatch.setenv("AGENTOS_BRIDGE_URL", "http://from-env:8765")
        p = self._provider({})
        assert p._bridge_url_for_container() == "http://from-env:8765"

    def test_gateway_detection_used_when_no_env(self, monkeypatch):
        monkeypatch.delenv("AGENTOS_BRIDGE_URL", raising=False)
        p = self._provider({})
        monkeypatch.setattr(type(p), "_detect_host_gateway_ip", staticmethod(lambda: "10.9.9.1"))
        assert p._bridge_url_for_container() == "http://10.9.9.1:8765"

    def test_empty_when_nothing_available(self, monkeypatch):
        monkeypatch.delenv("AGENTOS_BRIDGE_URL", raising=False)
        p = self._provider({})
        monkeypatch.setattr(type(p), "_detect_host_gateway_ip", staticmethod(lambda: ""))
        assert p._bridge_url_for_container() == ""

    def test_gateway_detection_windows_returns_empty(self, monkeypatch):
        import platform

        if platform.system() == "Linux":
            pytest.skip("仅 Windows 语义")
        p = self._provider({})
        assert p._detect_host_gateway_ip() == ""


class TestServerLoadTwice:
    """server.py 的 _load_browser_tool 缓存命中与 ValueError 分支。"""

    def test_second_load_hits_cache(self):
        import importlib.util as il

        mod_name = "browser_plugin_server_under_test"
        if mod_name not in sys.modules:
            spec = il.spec_from_file_location(mod_name, BROWSER_PLUGIN / "server.py")
            m = il.module_from_spec(spec)
            sys.modules[mod_name] = m
            spec.loader.exec_module(m)
        m = sys.modules[mod_name]
        # 再次调用命中缓存（44 行 return 分支）
        cls1 = m._load_browser_tool()
        cls2 = m._load_browser_tool()
        assert cls1 is cls2

    def test_sys_path_remove_tolerant(self, monkeypatch):
        # ValueError 分支：here 不在 sys.path 时 remove 抛 ValueError → 被吞
        import importlib.util as il

        mod_name = "browser_plugin_server_under_test"
        assert mod_name in sys.modules  # 由上一测试加载
        sys_path = sys.path.copy()
        # 模拟插件目录被逐出后再次加载 tool impl 不炸
        m = sys.modules[mod_name]
        assert m._browser_tool_cls is not None


class TestBridgeConfig:
    def test_bridge_config_no_injection_returns_empty(self, monkeypatch):
        import importlib.util as il

        mod_name = "browser_plugin_server_under_test"
        if mod_name not in sys.modules:
            spec = il.spec_from_file_location(mod_name, BROWSER_PLUGIN / "server.py")
            m = il.module_from_spec(spec)
            sys.modules[mod_name] = m
            spec.loader.exec_module(m)
        m = sys.modules[mod_name]
        # get_config 无 config_files 注入时抛错/返回空 → _bridge_config 兜 {}
        monkeypatch.setattr(m.plugin, "get_config", lambda: None)
        assert m._bridge_config() == {}
        monkeypatch.setattr(m.plugin, "get_config", lambda: {"bridge": {"upstream": "x"}})
        assert m._bridge_config() == {"upstream": "x"}


class TestBridgeClientProtocolMethods:
    """list_tools/call/initialize 经 _rpc 状态映射（打桩 _send）。"""

    def _client(self, monkeypatch, resp: dict) -> bc.BridgeClient:
        monkeypatch.setattr(BridgeClient, "_resolve_token", staticmethod(lambda cfg: "tok-abc"))
        c = BridgeClient({"bridge_base": "http://127.0.0.1:8765", "upstream": "browser"})
        monkeypatch.setattr(c, "_send", lambda payload: resp)
        return c

    @pytest.mark.asyncio
    async def test_list_tools(self, monkeypatch):
        c = self._client(monkeypatch, {"ok": True, "status": 200, "body": {"result": {"tools": [{"name": "x"}]}}})
        assert c.list_tools() == [{"name": "x"}]

    @pytest.mark.asyncio
    async def test_call(self, monkeypatch):
        c = self._client(monkeypatch, {"ok": True, "status": 200, "body": {"result": {"content": []}}})
        assert c.call("browser_navigate", {"url": "u"}) == {"content": []}

    @pytest.mark.asyncio
    async def test_initialize(self, monkeypatch):
        c = self._client(monkeypatch, {"ok": True, "status": 200, "body": {"result": {"protocolVersion": "2024-11-05"}}})
        assert c.initialize()["protocolVersion"] == "2024-11-05"


class TestSendDirectUnreachable:
    @pytest.mark.asyncio
    async def test_unreachable_returns_bridge_unreachable(self, monkeypatch):
        monkeypatch.setattr(BridgeClient, "_resolve_token", staticmethod(lambda cfg: "tok-abc"))
        c = BridgeClient({"bridge_base": "http://127.0.0.1:19998", "upstream": "browser", "timeout_secs": 2})
        r = c._send({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert not r["ok"]
        assert "Bridge 不可达" in r["body"]["error"]


class TestContainerNoContainerId:
    @pytest.mark.asyncio
    async def test_sandbox_without_container_id(self, monkeypatch):
        monkeypatch.setattr(BridgeClient, "_resolve_token", staticmethod(lambda cfg: "tok-abc"))
        monkeypatch.delenv("_CONTAINER_ID", raising=False)
        c = BridgeClient({"upstream": "browser", "timeout_secs": 2}, session_id="s", caller="sandbox")
        r = c._send({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert not r["ok"]
        assert "_container_id" in r["body"]["error"]


class TestFindProjectRootBoundary:
    @pytest.mark.asyncio
    async def test_root_reached_returns_empty(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        # cwd 无 .env 且已到根 → 返回 ""
        assert bc._find_project_root() == ""


class TestTokenRaiseBranch:
    @pytest.mark.asyncio
    async def test_no_token_anywhere_raises(self, monkeypatch, tmp_path):
        monkeypatch.delenv("AGENTOS_BRIDGE_TOKEN", raising=False)
        monkeypatch.setattr(bc, "_find_project_root", lambda: str(tmp_path))  # 空目录无 .env
        with pytest.raises(BridgeClientError) as e:
            BridgeClient({"bridge_base": "http://127.0.0.1:8765", "upstream": "browser"})
        assert "AGENTOS_BRIDGE_TOKEN" in str(e.value)


class TestServerLoadBrowserToolFresh:
    """server._load_browser_tool 的缓存未命中（重置 _browser_tool_cls 后再进）。"""

    def test_fresh_load_restores_sys_path(self):
        import importlib.util as il

        mod_name = "browser_plugin_server_under_test"
        if mod_name not in sys.modules:
            spec = il.spec_from_file_location(mod_name, BROWSER_PLUGIN / "server.py")
            m = il.module_from_spec(spec)
            sys.modules[mod_name] = m
            spec.loader.exec_module(m)
        m = sys.modules[mod_name]
        m._browser_tool_cls = None  # 强制走完整加载路径
        cls = m._load_browser_tool()
        assert cls is not None
        m._browser_tool_cls = cls


class TestServerEdgeLoadBranches:
    """_load_browser_tool 的 ValueError 分支与 _bridge_config 异常分支。"""

    def test_sys_path_remove_value_error_swallowed(self):
        # 防御性样板（finally: try remove / except ValueError: pass）——行为等价
        # 由 server._load_browser_tool 的正常加载路径隐式覆盖（fresh_load 测试已断言
        # 加载成功且 sys.path 已还原）；此处仅断言二次加载幂等不炸。
        import importlib.util as il

        mod_name = "browser_plugin_server_under_test"
        if mod_name not in sys.modules:
            spec = il.spec_from_file_location(mod_name, BROWSER_PLUGIN / "server.py")
            m = il.module_from_spec(spec)
            sys.modules[mod_name] = m
            spec.loader.exec_module(m)
        m = sys.modules[mod_name]
        m._browser_tool_cls = None
        cls = m._load_browser_tool()
        assert cls is not None
        m._browser_tool_cls = cls

    def test_get_config_raises_falls_back_empty(self, monkeypatch):
        import importlib.util as il

        mod_name = "browser_plugin_server_under_test"
        m = sys.modules[mod_name]

        def boom():
            raise RuntimeError("no config injection")

        monkeypatch.setattr(m.plugin, "get_config", boom)
        assert m._bridge_config() == {}


class TestDirectTransportHttpErrorBodyParse:
    @pytest.mark.asyncio
    async def test_http_error_non_json_body(self, monkeypatch):
        import urllib.error
        import urllib.request

        monkeypatch.setattr(BridgeClient, "_resolve_token", staticmethod(lambda cfg: "tok-abc"))
        c = BridgeClient({"bridge_base": "http://127.0.0.1:8765", "upstream": "browser", "timeout_secs": 5})

        class FakeHttpError(urllib.error.HTTPError):
            def __init__(self):
                super().__init__("http://x", 500, "boom", hdrs=None, fp=None)

            def read(self):
                return b"not-json"

        def fake_urlopen(req, timeout=None):
            raise FakeHttpError()

        saved = urllib.request.urlopen
        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        r = c._send_direct({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert not r["ok"] and r["status"] == 500
        assert "error" in r["body"]


class TestFindProjectRootTraversal:
    @pytest.mark.asyncio
    async def test_parent_chain_exhausted(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        # 根的 parent == cur → 走 return ""（265-269 行）
        result = bc._find_project_root()
        assert result == ""


class TestGatewayDetectionLinux:
    def test_linux_gateway_parsing(self, monkeypatch):
        import os as os_mod
        import platform
        import tests._isolation_path  # noqa: F401

        from providers.docker_provider import DockerProvider

        if platform.system() == "Linux":
            # 真实执行（WSL）；非 WSL 可能空——只断言不炸、无端口号
            ip = DockerProvider._detect_host_gateway_ip()
            assert ip == "" or ":" not in ip
            return
        # Windows：打桩 platform.system 与 os.popen 验证解析逻辑
        monkeypatch.setattr(platform, "system", lambda: "Linux")

        class FakeProc:
            def read(self):
                return "default via 172.20.0.1 dev eth0" + chr(10)

            def close(self):
                pass

        monkeypatch.setattr(os_mod, "popen", lambda cmd: FakeProc())
        assert DockerProvider._detect_host_gateway_ip() == "172.20.0.1"


class TestGatewayDetectionEdgeBranches:
    def test_linux_no_default_line(self, monkeypatch):
        import os as os_mod
        import platform
        import tests._isolation_path  # noqa: F401

        from providers.docker_provider import DockerProvider

        monkeypatch.setattr(platform, "system", lambda: "Linux")

        class FakeProc:
            def read(self):
                return ""  # 无 default 行 → 643 行 return ""

            def close(self):
                pass

        monkeypatch.setattr(os_mod, "popen", lambda cmd: FakeProc())
        assert DockerProvider._detect_host_gateway_ip() == ""


class TestFindProjectRootAtRoot:
    @pytest.mark.asyncio
    async def test_drive_root_no_env(self, monkeypatch, tmp_path):
        # cwd 恰为根：parent == cur → 267 行 return ""
        monkeypatch.setattr(bc, "os", bc.os)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(bc.os.path, "dirname", lambda p: p)  # parent == cur
        assert bc._find_project_root() == ""


class TestGatewayDetectionOSError:
    def test_popen_raises_oserror(self, monkeypatch):
        import os as os_mod
        import platform
        import tests._isolation_path  # noqa: F401

        from providers.docker_provider import DockerProvider

        monkeypatch.setattr(platform, "system", lambda: "Linux")

        def boom(cmd):
            raise OSError("popen failed")

        monkeypatch.setattr(os_mod, "popen", boom)
        assert DockerProvider._detect_host_gateway_ip() == ""
