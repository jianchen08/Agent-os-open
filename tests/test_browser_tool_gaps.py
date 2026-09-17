# @feature: FP-0.2.二 工具插件面 | @ci: python-coverage
"""browser 工具缺口补测（2026-09-13 官方车道 tool.py miss=41 / bridge_client.py miss=31）。

覆盖缺口：
- tool.py：BrowserTool 构造、execute 全分发链（未知工具名 / token 缺失 /
  Bridge 不可达 / 成功路径含 sandbox caller 与容器 env 注入）、
  _build_arguments 透传过滤与截图默认值、_to_tool_result 的 isError 分支、
  截图超限拒收、截图落盘（mime→扩展名）、_content_text 提取；
- bridge_client.py：token 从项目 .env 解析（含引号剥离）、_rpc 状态码错误
  映射（401/403/502/其余）、_send_direct 成功路径、_send_via_container
  （成功 / docker exec 失败 / 无输出 / 输出不可解析）、容器地址候选
  （env 注入优先 / 网关 IP 追加）。

打桩边界：Bridge HTTP 面与 docker exec 子进程为外部依赖，按既有惯例打桩；
无 Bridge 真实不可达用例（127.0.0.1:19998 真实连接拒绝）。
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.request
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_BROWSER_DIR = _REPO_ROOT / "plugins" / "shared" / "tools" / "browser"


def _evict_foreign_bare_modules() -> None:
    """逐出指向 browser 目录之外的裸名缓存（tool 为多插件同名裸模块）。"""
    for name in ("tool", "bridge_client"):
        mod = sys.modules.get(name)
        if mod is None:
            continue
        mod_file = getattr(mod, "__file__", None)
        if mod_file and Path(mod_file).resolve() == (_BROWSER_DIR / f"{name}.py").resolve():
            continue
        sys.modules.pop(name, None)


_evict_foreign_bare_modules()
if str(_BROWSER_DIR) not in sys.path or sys.path[0] != str(_BROWSER_DIR):
    sys.path.insert(0, str(_BROWSER_DIR))

import tool as browser_tool  # noqa: E402
from tool import (  # noqa: E402
    BrowserTool,
    _build_arguments,
)

from agentos_plugin_sdk import bridge_client as bc  # noqa: E402
from agentos_plugin_sdk.bridge_client import BridgeClient, BridgeClientError  # noqa: E402

pytestmark = pytest.mark.unit


class _StubBridgeClient:
    """Bridge 客户端桩：记录构造/调用参数，返回固定 MCP result。"""

    last_init: tuple = ()
    last_call: tuple = ()
    response: dict = {}

    def __init__(self, config, session_id="", caller="host"):
        type(self).last_init = (config, session_id, caller)

    def call(self, tool_name, arguments):
        type(self).last_call = (tool_name, arguments)
        return type(self).response


# ───────────────────────────── tool.py: 构造与分发 ─────────────────────────────


class TestBrowserToolConstructor:
    def test_default_config_uses_browser_upstream(self):
        t = BrowserTool()
        assert t._bridge_config == {}
        assert t._upstream == "browser"

    def test_custom_config_kept(self):
        t = BrowserTool({"upstream": "search", "bridge_base": "http://127.0.0.1:1"})
        assert t._upstream == "search"
        assert t._bridge_config["bridge_base"] == "http://127.0.0.1:1"


class TestExecuteDispatch:
    async def test_unknown_tool_name_rejected(self):
        r = await BrowserTool({}).execute({"_tool_name": "browser_hover"})
        assert r.success is False
        assert r.error_code == "UNKNOWN_BROWSER_TOOL"

    async def test_missing_token_fails_cleanly(self, monkeypatch, tmp_path):
        monkeypatch.delenv("AGENTOS_BRIDGE_TOKEN", raising=False)
        monkeypatch.delenv("AGENTOS_PROJECT_ROOT", raising=False)
        monkeypatch.setattr(bc, "_find_project_root", lambda: str(tmp_path))  # 空目录无 .env

        r = await BrowserTool({}).execute({"_tool_name": "browser_snapshot"})

        assert r.success is False
        assert r.error_code == "BRIDGE_TOKEN_MISSING"

    async def test_unreachable_bridge_maps_to_call_failed(self, monkeypatch):
        monkeypatch.setattr(BridgeClient, "_resolve_token", staticmethod(lambda cfg: "tok"))
        config = {"bridge_base": "http://127.0.0.1:19998", "upstream": "browser", "timeout_secs": 2}

        r = await BrowserTool(config).execute({"_tool_name": "browser_snapshot", "session_id": "sess-1"})

        assert r.success is False
        assert r.error_code == "BRIDGE_CALL_FAILED"

    async def test_sandbox_dispatch_sets_container_env_and_caller(self, monkeypatch, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.setenv("_CONTAINER_ID", "placeholder")
        _StubBridgeClient.response = {"content": [{"type": "text", "text": "snapshot body"}]}
        monkeypatch.setattr(browser_tool, "BridgeClient", _StubBridgeClient)

        tool = BrowserTool({"upstream": "browser"})
        r = await tool.execute(
            {
                "_tool_name": "browser_snapshot",
                "session_id": "sess-9",
                "_container_id": "cid-7",
                "workspace": str(ws),
            }
        )

        assert r.success is True
        assert r.output["snapshot_text"] == "snapshot body"
        # 容器上下文经环境变量传给 bridge_client，不进 MCP 参数
        assert os.environ["_CONTAINER_ID"] == "cid-7"
        config, session, caller = _StubBridgeClient.last_init
        assert session == "sess-9"
        assert caller == "sandbox"
        assert _StubBridgeClient.last_call == ("browser_snapshot", {})

    async def test_host_dispatch_navigates_with_url_echo(self, monkeypatch):
        _StubBridgeClient.response = {"content": [{"type": "text", "text": "done"}]}
        monkeypatch.setattr(browser_tool, "BridgeClient", _StubBridgeClient)

        r = await BrowserTool({}).execute({"_tool_name": "browser_navigate", "url": "https://example.com/"})

        assert r.success is True
        assert _StubBridgeClient.last_init[2] == "host"
        assert _StubBridgeClient.last_call == ("browser_navigate", {"url": "https://example.com/"})
        assert r.output["url"] == "https://example.com/"


# ───────────────────────────── tool.py: 参数构造 ─────────────────────────────


class TestBuildArguments:
    @pytest.mark.parametrize(
        ("tool_name", "inputs", "expected"),
        [
            ("browser_navigate", {"url": "https://a/", "bogus": 1}, {"url": "https://a/"}),
            ("browser_snapshot", {"ignored": "x"}, {}),
            (
                "browser_click",
                {"target": "t1", "element": "Submit", "doubleClick": True, "internal": "y"},
                {"target": "t1", "element": "Submit", "doubleClick": True},
            ),
            (
                "browser_type",
                {"target": "t2", "element": None, "text": "hello", "submit": True},
                {"target": "t2", "text": "hello", "submit": True},
            ),
            ("browser_console_messages", {"level": "error", "extra": 1}, {"level": "error"}),
        ],
    )
    def test_passthrough_filters_declared_fields_only(self, tool_name, inputs, expected):
        assert _build_arguments(tool_name, inputs) == expected

    def test_screenshot_gets_png_css_defaults(self):
        args = _build_arguments("browser_take_screenshot", {"filename": "shot"})
        assert args == {"filename": "shot", "scale": "css", "type": "png"}

    def test_screenshot_empty_inputs_still_get_defaults(self):
        assert _build_arguments("browser_take_screenshot", {}) == {"scale": "css", "type": "png"}

    def test_unknown_tool_yields_empty_arguments(self):
        assert _build_arguments("no_such_tool", {"url": "u"}) == {}


# ───────────────────────────── tool.py: 结果归一 ─────────────────────────────


# ───────────────────────────── bridge_client.py ─────────────────────────────


class TestTokenFromDotEnv:
    @pytest.mark.parametrize(
        ("raw_line", "expected"),
        [
            ("AGENTOS_BRIDGE_TOKEN=plain-tok", "plain-tok"),
            ('AGENTOS_BRIDGE_TOKEN="dq-tok"', "dq-tok"),
            ("AGENTOS_BRIDGE_TOKEN='sq-tok'", "sq-tok"),
        ],
    )
    def test_token_parsed_from_project_env(self, tmp_path, monkeypatch, raw_line, expected):
        (tmp_path / ".env").write_text(f"OTHER=1\n{raw_line}\n# tail comment\n", encoding="utf-8")
        monkeypatch.delenv("AGENTOS_BRIDGE_TOKEN", raising=False)
        monkeypatch.setenv("AGENTOS_PROJECT_ROOT", str(tmp_path))

        c = BridgeClient({"bridge_base": "http://127.0.0.1:8765"})

        assert c._token == expected

    def test_env_var_wins_without_dot_env_read(self, tmp_path, monkeypatch):
        (tmp_path / ".env").write_text("AGENTOS_BRIDGE_TOKEN=from-file\n", encoding="utf-8")
        monkeypatch.setenv("AGENTOS_BRIDGE_TOKEN", "from-env")
        monkeypatch.setenv("AGENTOS_PROJECT_ROOT", str(tmp_path))

        c = BridgeClient({"bridge_base": "http://127.0.0.1:8765"})

        assert c._token == "from-env"


class TestRpcStatusMapping:
    def _client(self, monkeypatch, resp: dict) -> BridgeClient:
        monkeypatch.setattr(BridgeClient, "_resolve_token", staticmethod(lambda cfg: "tok"))
        c = BridgeClient({"bridge_base": "http://127.0.0.1:8765", "upstream": "browser"})
        monkeypatch.setattr(c, "_send", lambda payload: resp)
        return c

    @pytest.mark.parametrize(
        ("status", "expected_fragment"),
        [
            (401, "认证失败"),
            (403, "治理拒绝"),
            (502, "上游不可用"),
            (500, "Bridge 调用失败(status=500)"),
        ],
    )
    def test_error_status_maps_to_distinct_message(self, monkeypatch, status, expected_fragment):
        c = self._client(monkeypatch, {"ok": False, "status": status, "body": {"error": "denied"}})

        with pytest.raises(BridgeClientError) as exc:
            c._rpc("tools/list")

        assert expected_fragment in str(exc.value)
        assert "denied" in str(exc.value)

    def test_error_body_without_error_key_still_raises(self, monkeypatch):
        c = self._client(monkeypatch, {"ok": False, "status": 503, "body": {}})

        with pytest.raises(BridgeClientError) as exc:
            c._rpc("tools/call")

        assert "status=503" in str(exc.value)


class TestSendDirectSuccess:
    def test_success_body_parsed_and_headers_attached(self, monkeypatch):
        monkeypatch.setattr(BridgeClient, "_resolve_token", staticmethod(lambda cfg: "tok"))
        c = BridgeClient({"bridge_base": "http://127.0.0.1:8765", "upstream": "browser"})
        captured: dict = {}

        class FakeResp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"browser_navigate"}]}}'

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["auth"] = req.get_header("Authorization")
            captured["session"] = req.get_header("X-bridge-session")
            return FakeResp()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        r = c._send({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

        assert r == {
            "ok": True,
            "status": 200,
            "body": {"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "browser_navigate"}]}},
        }
        assert captured["url"].endswith("/mcp/browser")
        assert captured["auth"] == "Bearer tok"
        assert captured["session"] == "default"  # 未注入 session_id 时的缺省会话


class TestContainerTransport:
    def _client(self, monkeypatch) -> BridgeClient:
        monkeypatch.setattr(BridgeClient, "_resolve_token", staticmethod(lambda cfg: "tok"))
        monkeypatch.setenv("_CONTAINER_ID", "cid-9")
        monkeypatch.setattr(bc, "_docker_gateway_ip", lambda: "")  # 网关探测是外部面
        return BridgeClient(
            {"bridge_base": "http://127.0.0.1:8765", "upstream": "browser", "timeout_secs": 5},
            session_id="sess-2",
            caller="sandbox",
        )

    @staticmethod
    def _patch_run(monkeypatch, returncode: int, stdout: bytes, stderr: bytes = b"") -> list:
        captured: list = []

        class Proc:
            pass

        proc = Proc()
        proc.returncode = returncode
        proc.stdout = stdout
        proc.stderr = stderr

        def fake_run(args, input=None, capture_output=None, timeout=None):
            captured.append({"args": args, "input": input})
            return proc

        monkeypatch.setattr(bc.subprocess, "run", fake_run)
        return captured

    def test_success_parses_inner_client_json_line(self, monkeypatch):
        c = self._client(monkeypatch)
        line = json.dumps({"ok": True, "status": 200, "body": {"result": {"content": []}}})
        captured = self._patch_run(monkeypatch, 0, (line + "\n").encode("utf-8"))

        r = c._send({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

        assert r["ok"] is True
        assert r["body"]["result"]["content"] == []
        args = captured[0]["args"]
        assert args[:4] == ["docker", "exec", "-i", "-e"]
        assert "cid-9" in args
        assert "PYTHONIOENCODING=utf-8" in args
        assert captured[0]["input"] == bc._INNER_SCRIPT.encode("utf-8")

    def test_docker_exec_failure_reports_stderr(self, monkeypatch):
        c = self._client(monkeypatch)
        self._patch_run(monkeypatch, 1, b"", b"docker: no such container\n")

        r = c._send({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

        assert r["ok"] is False
        assert "容器内 MCP Client 执行失败" in r["body"]["error"]
        assert "no such container" in r["body"]["error"]

    def test_empty_output_reports_no_output(self, monkeypatch):
        c = self._client(monkeypatch)
        self._patch_run(monkeypatch, 0, b"  \n")

        r = c._send({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

        assert r == {"ok": False, "status": 0, "body": {"error": "容器内客户端无输出"}}

    def test_unparsable_output_reports_snippet(self, monkeypatch):
        c = self._client(monkeypatch)
        self._patch_run(monkeypatch, 0, b"Traceback garbage, not json\n")

        r = c._send({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

        assert r["ok"] is False
        assert "容器内客户端输出不可解析" in r["body"]["error"]
        assert "Traceback garbage" in r["body"]["error"]


class TestContainerCandidates:
    @staticmethod
    def _client() -> BridgeClient:
        c = BridgeClient.__new__(BridgeClient)
        c._base = "http://127.0.0.1:8765"
        c._upstream = "browser"
        return c

    def test_env_url_takes_first_slot(self, monkeypatch):
        monkeypatch.setenv("AGENTOS_BRIDGE_URL", "http://env-bridge:9999")
        monkeypatch.setattr(bc, "_docker_gateway_ip", lambda: "")

        cands = self._client()._container_base_candidates()

        assert cands[0] == "http://env-bridge:9999"
        assert "http://host.docker.internal:8765" in cands

    def test_gateway_ip_appended_when_probed(self, monkeypatch):
        monkeypatch.delenv("AGENTOS_BRIDGE_URL", raising=False)
        monkeypatch.setattr(bc, "_docker_gateway_ip", lambda: "172.20.0.1")

        cands = self._client()._container_base_candidates()

        assert cands == ["http://host.docker.internal:8765", "http://172.20.0.1:8765"]
