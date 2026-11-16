# @feature: FP-0.2.可观测性 dsh_adapter 插件 | @ci: python-coverage
"""dsh_adapter 管理端点层（server.py /ext/dsh_adapter/api/dsh/plugins* 分发）。

端点层职责：路由 → body 解析（base64/裸 JSON 双形态）→ manager 业务调用 →
ManagerError 错误码到 HTTP 状态映射。manager 业务面（审批/engines/依赖保护）
在 test_manager.py 已覆盖——本文件用真实 DshPackageManager（npm/索引/审批全
注入替身）打端点，验路由与状态映射契约：

- GET  ``…/plugins``            → 200 清单（实扫）；
- GET  ``…/plugins/index``      → 200 发现源（stale 标记透传）；
- POST ``…/plugins/install``    → 200 pending_approval（审批卡已建）；
  engines 不兼容 → 422 结构化错误体（AC-M2 可判定失败原因）；
  缺 pkg → 400；
- POST ``…/plugins/uninstall``  → 依赖者 409 / 成功 200；
- POST ``…/plugins/enable|disable`` → 200 / 未安装 404；
- GET  ``/ext/dsh_adapter/page/manager`` → 200 text/html（webview 自伺服页）。
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

import runtime_workspace as rw  # noqa: E402

_SRV_MODULE = "dsh_adapter_server_mgmt_test"

# 裸名逐出约定（tests/conftest.py）：manager/server 在收集后统一逐出 sys.modules，
# server.py 装载会以 'manager' 名重执行——manager 类一律取 server 模块所见的
# 同一模块对象（sys.modules['manager']），禁在模块顶层 import manager 后跨用。


def _load_server() -> Any:
    spec = importlib.util.spec_from_file_location(_SRV_MODULE, PLUGIN_DIR / "server.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_SRV_MODULE] = module
    spec.loader.exec_module(module)
    return module


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


class _StubApproval:
    def __init__(self) -> None:
        self.cards: list[dict[str, Any]] = []

    async def call(self, method: str, params: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        if method == "create_choice":
            self.cards.append(params)
            return {"request_id": "req-ep-1", "ok": True}
        if method == "wait_for_choice":
            return {"ok": True, "status": "answered",
                    "response": {"response_type": "answered", "selected_option": "approve"}}
        return {"ok": True}


class _StubNpm:
    """npm 替身：view 返回 engines 兼容元数据；install/uninstall/pack 走真实
    文件系统效果的最小模拟（node_modules 拷入/移除）。"""

    def __init__(self, src_dir: Path, engines: dict[str, str] | None = None) -> None:
        self.src_dir = src_dir
        self.engines = engines or {}

    def __call__(self, cmd: list[str], timeout: float) -> tuple[int, str, str]:
        import shutil as _shutil
        import tarfile as _tarfile

        if "view" in cmd:
            meta = {"engines": self.engines} if self.engines else {}
            return (0, json.dumps(meta), "")
        if "pack" in cmd:
            dest = Path(cmd[cmd.index("--pack-destination") + 1]) / "pkg.tgz"
            with _tarfile.open(dest, mode="w:gz") as tf:
                for f in sorted(self.src_dir.rglob("*")):
                    if f.is_file():
                        data = f.read_bytes()
                        info = _tarfile.TarInfo("package/" + f.relative_to(self.src_dir).as_posix())
                        info.size = len(data)
                        tf.addfile(info, __import__("io").BytesIO(data))
            return (0, f"{dest}\n", "")
        prefix = Path(cmd[cmd.index("--prefix") + 1])
        if "install" in cmd:
            spec = cmd[-1]
            if spec.startswith("/") or spec[1:2] == ":":
                name = json.loads((Path(spec) / "package.json").read_text(encoding="utf-8"))["name"]
                dest = prefix / "node_modules" / name
                if dest.exists():
                    _shutil.rmtree(dest)
                _shutil.copytree(Path(spec), dest)
                return (0, "added", "")
            return (1, "", "npm error no network in endpoint tests")
        if "uninstall" in cmd:
            name = cmd[-1]
            target = prefix / "node_modules" / name
            if target.exists():
                _shutil.rmtree(target)
                return (0, "removed", "")
            return (1, "", "npm error not installed")
        return (0, "", "")


@pytest.fixture
def srv(tmp_path: Path) -> Any:
    module = _load_server()
    module.reset_manager()
    yield module
    module.reset_manager()


@pytest.fixture
def manager_env(srv: Any, tmp_path: Path) -> dict[str, Any]:
    """真实 manager（全替身注入；取 server 模块所见的 manager 对象——裸名逐出
    约定下与 srv 的 except ManagerError 同类）。"""
    import sys as _sys

    mgr = _sys.modules["manager"]
    ws = tmp_path / "ws"
    ws.mkdir()
    rw.ensure_workspace(ws)
    skins_dir = tmp_path / "skins-out"
    src = tmp_path / "pkg-src"
    src.mkdir()
    (src / "package.json").write_text(
        json.dumps({"name": "ep-tool", "version": "1.0.0"}), encoding="utf-8")
    (src / "lib").mkdir()
    (src / "lib" / "index.js").write_text("export const apply = () => {}\n", encoding="utf-8")
    approval = _StubApproval()
    m = mgr.DshPackageManager(
        runtime_root=str(ws), skins_dir=str(skins_dir),
        npm_runner=_StubNpm(src, engines={"dsh": ">=0.2.1-rc.2"}),
        capability_factory=lambda: approval,
        fetcher=None, config={},
    )
    return {"manager": m, "ws": ws, "approval": approval, "skins_dir": skins_dir, "src": src}


def _bind(srv: Any, m: Any) -> None:
    srv._MANAGER = m  # noqa: SLF001 — 端点层测试注入面


def _req(srv: Any, path: str, method: str = "GET", raw_body: str = "",
         query: dict[str, str] | None = None) -> dict[str, Any] | None:
    return asyncio.run(srv._api_plugins_dispatch(path, method, raw_body, query))  # noqa: SLF001


class TestListAndIndexRoutes:
    def test_list_empty_workspace(self, srv: Any, manager_env: dict[str, Any]) -> None:
        _bind(srv, manager_env["manager"])
        resp = _req(srv, "/ext/dsh_adapter/api/dsh/plugins")
        assert resp is not None
        status, body = _decode(resp)
        assert status == 200
        assert body["initialized"] is False

    def test_list_route_requires_get(self, srv: Any, manager_env: dict[str, Any]) -> None:
        _bind(srv, manager_env["manager"])
        assert _req(srv, "/ext/dsh_adapter/api/dsh/plugins", method="POST") is None

    def test_index_route_passes_through_stale(self, srv: Any, manager_env: dict[str, Any],
                                              monkeypatch: pytest.MonkeyPatch) -> None:
        _bind(srv, manager_env["manager"])

        async def fake_refresh() -> dict[str, Any]:
            return {"sources": [{"name": "x", "ok": False, "stale": True, "error": "down",
                                 "fetched_at": 1.0}],
                    "plugins": [{"name": "idx-pkg", "versions": [
                        {"version": "1.0.0", "source": {"type": "npm"}, "engines": {}}]}],
                    "cached": True, "stale": True}

        monkeypatch.setattr(manager_env["manager"], "refresh_index", fake_refresh)
        status, body = _decode(_req(srv, "/ext/dsh_adapter/api/dsh/plugins/index"))
        assert status == 200
        assert body["stale"] is True
        assert body["cached"] is True
        assert body["plugins"][0]["name"] == "idx-pkg"


class TestInstallRoute:
    def test_install_missing_pkg_400(self, srv: Any, manager_env: dict[str, Any]) -> None:
        _bind(srv, manager_env["manager"])
        status, body = _decode(_req(srv, "/ext/dsh_adapter/api/dsh/plugins/install",
                                    method="POST", raw_body=_b64({})))
        assert status == 400
        assert body["error"]["code"] == "invalid_request"

    def test_install_engines_incompatible_422_structured(
        self, srv: Any, manager_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        m = manager_env["manager"]
        _bind(srv, m)

        def compat_false(_pj: Any, _host: str) -> bool | None:
            return False

        monkeypatch.setattr(rw, "engine_compatible", compat_false)
        status, body = _decode(_req(
            srv, "/ext/dsh_adapter/api/dsh/plugins/install", method="POST",
            raw_body=_b64({"pkg": "ep-tool", "version": "1.0.0"})))
        assert status == 422
        assert body["error"]["code"] == "engines_incompatible"
        assert manager_env["approval"].cards == []  # 审批之前拦截

    def test_install_happy_path_returns_pending_with_request(
        self, srv: Any, manager_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        m = manager_env["manager"]
        _bind(srv, m)
        monkeypatch.setattr(m, "_npm_runner_blocking", lambda _cmd: (
            0, json.dumps({"engines": {"dsh": ">=0.2.1-rc.2"}}), ""))
        status, body = _decode(_req(
            srv, "/ext/dsh_adapter/api/dsh/plugins/install", method="POST",
            raw_body=_b64({"pkg": "ep-tool", "version": "1.0.0"})))
        assert status == 200
        assert body["status"] == "pending_approval"
        assert body["request_id"] == "req-ep-1"
        assert "ep-tool" in (manager_env["approval"].cards[0].get("description") or "")  # 卡=翻译预览

    def test_body_accepts_plain_json_and_base64(self, srv: Any, manager_env: dict[str, Any],
                                                monkeypatch: pytest.MonkeyPatch) -> None:
        m = manager_env["manager"]
        _bind(srv, m)
        monkeypatch.setattr(m, "_npm_runner_blocking", lambda _cmd: (
            0, json.dumps({"engines": {}}), ""))
        status, body = _decode(_req(
            srv, "/ext/dsh_adapter/api/dsh/plugins/install", method="POST",
            raw_body=json.dumps({"pkg": "ep-tool", "version": "1.0.0"})))
        assert status == 200
        assert body["status"] == "pending_approval"


class TestLifecycleRoutes:
    def _installed(self, manager_env: dict[str, Any]) -> Path:
        ws = Path(manager_env["ws"])
        pkg = ws / "node_modules" / "ep-tool"
        pkg.mkdir(parents=True)
        (pkg / "package.json").write_text(json.dumps({"name": "ep-tool", "version": "1.0.0"}),
                                          encoding="utf-8")
        return ws

    def test_enable_disable_ok(self, srv: Any, manager_env: dict[str, Any]) -> None:
        ws = self._installed(manager_env)
        _bind(srv, manager_env["manager"])
        status, body = _decode(_req(srv, "/ext/dsh_adapter/api/dsh/plugins/disable",
                                    method="POST", raw_body=_b64({"pkg": "ep-tool"})))
        assert status == 200
        assert body["ok"] is True
        assert rw.package_state(ws, "ep-tool")["enabled"] is False
        status, body = _decode(_req(srv, "/ext/dsh_adapter/api/dsh/plugins/enable",
                                    method="POST", raw_body=_b64({"pkg": "ep-tool"})))
        assert status == 200
        assert body["enabled"] is True

    def test_uninstall_unknown_404(self, srv: Any, manager_env: dict[str, Any]) -> None:
        _bind(srv, manager_env["manager"])
        status, body = _decode(_req(srv, "/ext/dsh_adapter/api/dsh/plugins/uninstall",
                                    method="POST", raw_body=_b64({"pkg": "ghost"})))
        assert status == 404
        assert body["error"]["code"] == "package_not_installed"

    def test_uninstall_ok(self, srv: Any, manager_env: dict[str, Any]) -> None:
        ws = self._installed(manager_env)
        _bind(srv, manager_env["manager"])
        status, body = _decode(_req(srv, "/ext/dsh_adapter/api/dsh/plugins/uninstall",
                                    method="POST", raw_body=_b64({"pkg": "ep-tool"})))
        assert status == 200
        assert body["ok"] is True
        assert not (ws / "node_modules" / "ep-tool").exists()


class TestManagerPage:
    def test_manager_page_html_served(self, srv: Any) -> None:
        resp = srv._manager_page_response()  # noqa: SLF001 — 端点直测
        status, text = _decode(resp)
        assert status == 200
        assert "text/html" in resp["headers"]["Content-Type"]
        assert "api/dsh/plugins" in text

    def test_http_handle_dispatches_page_and_api(self, srv: Any, manager_env: dict[str, Any]) -> None:
        _bind(srv, manager_env["manager"])
        page = asyncio.run(srv._http_handle_style(path="/ext/dsh_adapter/page/manager"))
        assert page["status"] == 200
        listing = asyncio.run(srv._http_handle_style(path="/ext/dsh_adapter/api/dsh/plugins"))
        assert listing["status"] == 200
        status, body = _decode(listing)
        assert body["initialized"] is False
        unknown = asyncio.run(srv._http_handle_style(path="/ext/dsh_adapter/api/dsh/other"))
        assert unknown["status"] == 404


# ── X 批端点/服务面：钩子方言翻译、webServer 反代、E1 桥服务方法 ─────────


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
    monkeypatch.setattr(bridge_mod, "get_bridge", lambda *a, **k: stub)


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
