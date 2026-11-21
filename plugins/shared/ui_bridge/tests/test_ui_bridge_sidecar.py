# @feature: FP-0.2.侧边栏UI对象化 | @vision: V2 全能闭环 | @ci: python-coverage
"""ui_bridge sidecar 运行面缺口测试（实现方补充件，不改签发断言）。

签发套件（test_ui_bridge_state.py）覆盖 FastAPI 自含闭环与 ui_describe 模块入口；
本文件补齐 sidecar 主循环面的改动行覆盖（plugins-diff-coverage 100% 前提）：

- ``http.handle`` 路由核：POST 合法/非法 JSON/非 object 体、GET 单端/定向/全端、未知路由 404；
- ``_ui_describe_tool`` kwargs 分发汇回模块级入口；
- ``ui_describe`` all=true（有端/无端）与 client_id 未命中拒绝路径；
- ``_validate_report`` 未提供 unread_count 放行（仅存档面宽容，echo 保真）与 bool 拒收。

装载方式与签发套件同款：独名模块 importlib 直载（模块级缓存随装载实例隔离）。
"""
import importlib.util
import itertools
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PKG = Path(__file__).resolve().parents[1]

_LOAD_SEQ = itertools.count()


def _load_server() -> Any:
    mod_name = f"ui_bridge_sidecar_server_under_test_{next(_LOAD_SEQ)}"
    spec = importlib.util.spec_from_file_location(mod_name, _PKG / "server.py")
    assert spec is not None, "cannot load server.py"
    assert spec.loader is not None, "cannot load server.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def ui_bridge() -> Any:
    return _load_server()


def _valid_body(client_id: str = "fe-1", **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "client_id": client_id,
        "region": "sidebar",
        "ts": 1_700_000_000_000,
        "state": {
            "collapsed": False,
            "width_ratio": 0.3,
            "active_view": "sessions",
            "unread_count": 2,
        },
        "items": [],
        "sessions": [],
        "actions": ["navigate", "set_collapsed", "open_page", "highlight"],
    }
    body.update(overrides)
    return body


def _body_of(envelope: dict[str, Any]) -> Any:
    """解 HttpHandleResponse 信封（body base64 → JSON 值）。"""
    import base64

    return json.loads(base64.b64decode(envelope["data"]["body"]).decode("utf-8"))


def _status_of(envelope: dict[str, Any]) -> int:
    return int(envelope["data"]["status"])


# ── http.handle 路由核 ─────────────────────────────────────────────────


class TestHttpHandle:
    async def test_post_valid_stores_and_get_reads_back(self, ui_bridge: Any) -> None:
        body = _valid_body()
        stored = await ui_bridge.http_handle(
            path="/ext/ui_bridge/state", method="POST", raw_body=json.dumps(body)
        )
        assert stored["success"] is True
        assert _body_of(stored) == {"ok": True}

        read = await ui_bridge.http_handle(
            path="/ext/ui_bridge/state", method="GET", query={"region": "sidebar"}
        )
        payload = _body_of(read)
        assert payload["client_id"] == "fe-1"
        assert payload["fresh"] is True
        assert payload["snapshot"]["state"] == body["state"]

    async def test_post_invalid_json_returns_400(self, ui_bridge: Any) -> None:
        resp = await ui_bridge.http_handle(
            path="/ext/ui_bridge/state", method="POST", raw_body="not-json{{{"
        )
        assert _status_of(resp) == 400

    async def test_post_non_object_body_returns_400(self, ui_bridge: Any) -> None:
        resp = await ui_bridge.http_handle(
            path="/ext/ui_bridge/state", method="POST", raw_body=json.dumps([1, 2])
        )
        assert _status_of(resp) == 400

    async def test_get_client_id_and_all(self, ui_bridge: Any) -> None:
        await ui_bridge.http_handle(
            path="/ext/ui_bridge/state",
            method="POST",
            raw_body=json.dumps(_valid_body(client_id="fe-a")),
        )
        picked = _body_of(
            await ui_bridge.http_handle(
                path="/ext/ui_bridge/state",
                method="GET",
                query={"region": "sidebar", "client_id": "fe-a"},
            )
        )
        assert picked["client_id"] == "fe-a"

        listing = _body_of(
            await ui_bridge.http_handle(
                path="/ext/ui_bridge/state",
                method="GET",
                query={"region": "sidebar", "all": "1"},
            )
        )
        assert [c["client_id"] for c in listing["clients"]] == ["fe-a"]

    async def test_unknown_route_404(self, ui_bridge: Any) -> None:
        resp = await ui_bridge.http_handle(path="/ext/ui_bridge/other", method="GET")
        assert _status_of(resp) == 404


# ── _ui_describe_tool 分发面 + ui_describe 拒绝/全端路径 ──────────────


class TestUiDescribeEntry:
    async def test_tool_entry_passes_kwargs_to_module_entry(self, ui_bridge: Any) -> None:
        out = await ui_bridge._ui_describe_tool(region="sidebar")
        assert out["status"] == "no_snapshot"
        assert out["region"] == "sidebar"

    async def test_client_id_not_found_reports_no_snapshot(self, ui_bridge: Any) -> None:
        await ui_bridge.http_handle(
            path="/ext/ui_bridge/state",
            method="POST",
            raw_body=json.dumps(_valid_body(client_id="fe-a")),
        )
        out = ui_bridge.ui_describe({"region": "sidebar", "client_id": "fe-missing"})
        assert out["status"] == "no_snapshot"
        assert "fe-missing" in out["error"]

    async def test_all_true_lists_clients_and_reflects_latest_freshness(
        self, ui_bridge: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _FakeClock:
            current = 100_000.0

            def monotonic(self) -> float:
                return self.current

        fake = _FakeClock()
        monkeypatch.setattr(ui_bridge, "_time", fake)
        await ui_bridge.http_handle(
            path="/ext/ui_bridge/state",
            method="POST",
            raw_body=json.dumps(_valid_body(client_id="fe-a")),
        )
        fake.current += 31
        await ui_bridge.http_handle(
            path="/ext/ui_bridge/state",
            method="POST",
            raw_body=json.dumps(_valid_body(client_id="fe-b")),
        )

        out = ui_bridge.ui_describe({"all": True})
        assert out["status"] == "ok", "最近端 fresh → ok"
        assert [c["client_id"] for c in out["clients"]] == ["fe-b", "fe-a"]
        assert out["clients"][1]["fresh"] is False

    async def test_all_true_without_clients_reports_no_snapshot(self, ui_bridge: Any) -> None:
        out = ui_bridge.ui_describe({"all": True})
        assert out["status"] == "no_snapshot"


# ── _validate_report 宽容/严拒边界（签发枚举之外的两侧）───────────────


class TestValidateReportEdges:
    async def test_missing_unread_count_passes_and_echoes(self, ui_bridge: Any) -> None:
        body = _valid_body()
        del body["state"]["unread_count"]
        stored = await ui_bridge.http_handle(
            path="/ext/ui_bridge/state", method="POST", raw_body=json.dumps(body)
        )
        assert _body_of(stored) == {"ok": True}, "未提供 unread_count 放行（§2.1 枚举只拒 <0）"

    async def test_bool_unread_count_rejected(self, ui_bridge: Any) -> None:
        body = _valid_body(
            state={"collapsed": False, "width_ratio": 0.3, "active_view": "sessions", "unread_count": True}
        )
        resp = await ui_bridge.http_handle(
            path="/ext/ui_bridge/state", method="POST", raw_body=json.dumps(body)
        )
        assert _status_of(resp) == 400, "bool 冒充 int 拒收"

    @pytest.mark.parametrize(
        ("mutate", "fragment"),
        [
            (lambda b: {**b, "state": "flat"}, "state must be an object"),
            (lambda b: {**b, "items": {}}, "items must be a list"),
            (lambda b: {**b, "sessions": "x"}, "sessions must be a list"),
            (lambda b: {**b, "actions": None}, "actions must be a list"),
        ],
        ids=["state-non-object", "items-non-list", "sessions-non-list", "actions-non-list"],
    )
    async def test_structural_type_violations_rejected(
        self, ui_bridge: Any, mutate: Any, fragment: str
    ) -> None:
        resp = await ui_bridge.http_handle(
            path="/ext/ui_bridge/state",
            method="POST",
            raw_body=json.dumps(mutate(_valid_body())),
        )
        assert _status_of(resp) == 400, "结构非法 fail-closed"
        assert fragment in json.dumps(_body_of(resp), ensure_ascii=False)


class TestFastApiMalformedBody:
    """create_app 的 FastAPI 通道：非 JSON 体归一为校验失败（400，零残留）。"""

    def _app_and_client(self, ui_bridge: Any) -> Any:
        from fastapi.testclient import TestClient

        return TestClient(ui_bridge.create_app())

    def test_non_json_body_rejected_400_without_cache_write(self, ui_bridge: Any) -> None:
        client = self._app_and_client(ui_bridge)
        resp = client.post(
            "/ext/ui_bridge/state",
            content=b"not-json{{{",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400, "非 JSON 体 fail-closed 400"

        data = client.get("/ext/ui_bridge/state", params={"region": "sidebar"}).json()
        assert data["snapshot"] is None, "违规上报不得落缓存"

    def test_json_scalar_body_rejected_400(self, ui_bridge: Any) -> None:
        client = self._app_and_client(ui_bridge)
        resp = client.post("/ext/ui_bridge/state", json=[1, 2, 3])
        assert resp.status_code == 400, "非 object 顶层 JSON fail-closed 400"
