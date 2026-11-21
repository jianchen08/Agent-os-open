# @feature: FP-0.2.可观测性 dsh_adapter 插件 | @ci: python-coverage
"""dsh_adapter 官方插件管理页接入层（server.py 官方 UI 端点 + web_app 监督器）。

**自研插件管理界面已删除**（用户裁定 2026-11-17：界面归 DSH 官方
``@deepseek-ai/dsh-client-ui-plugin-manager``，不自研第二套）。剩下的唯一入口是
``GET /ext/dsh_adapter/api/dsh/official-ui``：它只回答「官方页在哪 / 为什么不可用」，
不产出任何列表——列表的渲染方是官方 Web 应用本身（同一 profile、同一
``pluginManager``，故两侧一致由构造保证）。

契约要点（防退化）：
- 就绪才给 url；未就绪**必须**给可判定 reason（fail-visible），禁编造链接；
- ``?ensure=1`` 才触发启动（只读查询不产生副作用），启动在后台线程进行、
  立即返回 ``starting``，不把 HTTP 调用挂在冷启动上；
- 启动器页（``/page/manager``）与其路由声明已删除——避免「两套入口」回潮。
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

pytestmark = pytest.mark.unit

_SRV_MODULE = "dsh_adapter_server_official_ui_test"


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
    ct = " ".join(str(v) for v in (resp.get("headers") or {}).values())
    if "json" in ct:
        return int(resp.get("status", 200)), json.loads(raw.decode("utf-8"))
    return int(resp.get("status", 200)), raw.decode("utf-8")


class _FakeSupervisor:
    """监督器替身：只暴露端点消费的 status/start。"""

    def __init__(self, status: Any, start_error: Exception | None = None) -> None:
        self._status = status
        self._start_error = start_error
        self.start_calls = 0

    def status(self) -> Any:
        return self._status

    def start(self) -> Any:
        self.start_calls += 1
        if self._start_error is not None:
            raise self._start_error
        return self._status


def _status(srv_module: Any, **kwargs: Any) -> Any:
    """构造 web_app.WebAppStatus（经模块身份取，避免装配两套 dataclass）。"""
    import web_app

    return web_app.WebAppStatus(**kwargs)


def _call_ui(srv: Any, query: dict[str, str] | None = None) -> tuple[int, Any]:
    return _decode(asyncio.run(srv._official_ui_response(query)))  # noqa: SLF001


class TestOfficialUiStatus:
    def test_available_returns_url(self, srv: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeSupervisor(_status(srv, available=True,
                                       url="http://127.0.0.1:41234/?token=t",
                                       origin="http://127.0.0.1:41234", pid=99))
        monkeypatch.setattr("web_app.get_supervisor", lambda *_a, **_k: fake)

        status, body = _call_ui(srv)
        assert status == 200
        assert body["available"] is True
        assert body["url"] == "http://127.0.0.1:41234/?token=t"
        assert body["origin"] == "http://127.0.0.1:41234"
        assert body["reason"] is None
        # 只读查询不触发启动
        assert fake.start_calls == 0

    def test_unavailable_reports_reason(self, srv: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """未就绪必须给可判定原因，且不得编造 url（fail-visible）。"""
        fake = _FakeSupervisor(_status(srv, available=False,
                                       reason="未找到 Node >= 22（DSH 官方 Web 应用要求）"))
        monkeypatch.setattr("web_app.get_supervisor", lambda *_a, **_k: fake)

        _, body = _call_ui(srv)
        assert body["available"] is False
        assert body["url"] is None
        assert "Node >= 22" in body["reason"]

    def test_starting_is_reported_not_error(self, srv: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """启动中：如实报 starting，且不再重复触发启动（幂等）。"""
        fake = _FakeSupervisor(_status(srv, available=False, starting=True,
                                       reason="官方 DSH Web 应用启动中"))
        monkeypatch.setattr("web_app.get_supervisor", lambda *_a, **_k: fake)

        _, body = _call_ui(srv, {"ensure": "1"})
        assert body["available"] is False
        assert body["starting"] is True
        assert fake.start_calls == 0


class TestEnsureSemantics:
    def test_ensure_triggers_background_start(self, srv: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """``?ensure=1`` 触发启动，但只在后台线程（端点立即返回再取状态）。"""
        started: list[int] = []
        fake = _FakeSupervisor(_status(srv, available=False, reason="未启动"))

        def _fake_start_in_background(supervisor: Any) -> None:
            started.append(id(supervisor))

        monkeypatch.setattr("web_app.get_supervisor", lambda *_a, **_k: fake)
        monkeypatch.setattr(srv, "_start_in_background", _fake_start_in_background)

        status, body = _call_ui(srv, {"ensure": "1"})
        assert status == 200
        assert started == [id(fake)]
        # 后台线程尚未改状态 → 端点如实回报当前状态，不假装已就绪
        assert body["available"] is False

    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes"])
    def test_ensure_truthy_values(self, srv: Any, raw: str) -> None:
        assert srv._wants_ensure({"ensure": raw}) is True  # noqa: SLF001

    @pytest.mark.parametrize("query", [None, {}, {"ensure": "0"}, {"ensure": "false"}, {"ensure": ""}])
    def test_ensure_falsy_values(self, srv: Any, query: dict[str, str] | None) -> None:
        assert srv._wants_ensure(query) is False  # noqa: SLF001


class TestRoutingAndDeclarations:
    """路由分发与声明面一致（防幽灵路由 / 声明漂移）。"""

    def test_official_ui_route_hits(self, srv: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeSupervisor(_status(srv, available=False, reason="未启动"))
        monkeypatch.setattr("web_app.get_supervisor", lambda *_a, **_k: fake)
        resp = asyncio.run(srv._http_handle_style(  # noqa: SLF001
            path=srv._OFFICIAL_UI_PATH, method="GET"))  # noqa: SLF001
        assert resp["status"] == 200

    def test_launcher_page_is_gone(self, srv: Any) -> None:
        """启动器页已退役（官方页由 webview 直接内嵌），路由必须回到 404。"""
        assert not hasattr(srv, "_launcher_page_response")
        assert not hasattr(srv, "_MANAGER_PAGE_HTML")
        resp = asyncio.run(srv._http_handle_style(  # noqa: SLF001
            path="/ext/dsh_adapter/page/manager", method="GET"))
        assert resp["status"] == 404

    def test_selfbuilt_management_endpoints_are_404(self, srv: Any) -> None:
        """自研管理端点已删除——回到 404，不得幽灵存活。"""
        handler = srv._http_handle_style  # noqa: SLF001
        for path, method in (
            ("/ext/dsh_adapter/api/dsh/plugins", "GET"),
            ("/ext/dsh_adapter/api/dsh/plugins/index", "GET"),
            ("/ext/dsh_adapter/api/dsh/plugins/install", "POST"),
            ("/ext/dsh_adapter/api/dsh/plugins/uninstall", "POST"),
            ("/ext/dsh_adapter/api/dsh/plugins/enable", "POST"),
            ("/ext/dsh_adapter/api/dsh/plugins/disable", "POST"),
        ):
            resp = asyncio.run(handler(path=path, method=method))
            assert resp["status"] == 404, f"{path} ({method}) 应已删除（返回 {resp['status']}）"

    def test_endpoints_declared(self) -> None:
        data = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
        routes = {e["route_id"] for e in data["http_endpoints"]}
        assert "dsh_official_ui" in routes
        for gone in ("dsh_manager_page", "dsh_plugins_list", "dsh_plugins_install", "dsh_plugins_index"):
            assert gone not in routes, f"{gone} 应已从声明面删除"

    def test_page_embeds_official_ui(self) -> None:
        """页面声明指向官方 UI 端点做外部内嵌（不再走 htmlPath 自研页）。"""
        data = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
        pages = [p for p in data["contributes"]["pages"] if p["id"] == "dsh_plugin_manager"]
        assert len(pages) == 1
        props = pages[0]["props"]
        assert props["externalFramePath"].startswith("/api/dsh/official-ui")
        assert props["externalFramePath"].endswith("ensure=1")
        assert "htmlPath" not in props
