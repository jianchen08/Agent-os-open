# @feature: FP-0.2.二 可观测性 | @ci: python-coverage
"""db_admin HTTP 面缺口补测（与 shape lock 契约测试互补）：

- _route：错误前缀 404、execute POST、table GET/POST 二段路由
- _query_params：limit/offset 合法转 int 与非法忽略、filter 多值（query_multi）
  优先于单值、filter[] 键名等价、sort 透传、空 query 空参数
- _authorization：大小写不敏感取头、缺头空串
- db_admin.status：capability 已注入/未注入两态
- http.handle 容错族：body 非法 JSON 400、capability 缺席 502、capability
  异常 502、非 dict 信封 502、错误信封 code 字符串化透传
"""

from __future__ import annotations

import base64
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parents[4] / "plugins" / "shared" / "db_admin"


def _load_server() -> Any:
    spec = importlib.util.spec_from_file_location(
        "db_admin_gaps_test_server",
        str(_PLUGIN_DIR / "server.py"),
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["db_admin_gaps_test_server"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def server() -> Any:
    return _load_server()


class FakeCap:
    """db-admin capability 替身：脚本化响应/异常。"""

    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, method: str, params: dict[str, Any]) -> Any:
        self.calls.append((method, params))
        if self.error is not None:
            raise self.error
        return self.result


def _ok_envelope(payload: Any = {"ok": True}, status: int = 200) -> dict[str, Any]:
    return {"status": status, "body": payload}


def _body_json(resp: dict[str, Any]) -> Any:
    """http_json 信封 data.body 为 base64 —— 解码为 JSON 断言。"""
    import json

    return json.loads(base64.b64decode(resp["data"]["body"]))


def _inject(monkeypatch: pytest.MonkeyPatch, mod: Any, cap: Any) -> None:
    monkeypatch.setattr(mod.plugin, "get_capability", lambda name: cap)


async def test_route_rejects_wrong_prefix_and_non_routes(server: Any) -> None:
    assert server._route("/ext/other/tables", "GET") is None
    assert server._route("/ext/db_admin/nope", "GET") is None
    assert server._route("/ext/db_admin/tables", "DELETE") is None
    # execute POST 路由
    assert server._route("/ext/db_admin/execute", "POST") == ("execute", {})
    assert server._route("/ext/db_admin/execute", "GET") is None
    # table 二段 GET/POST
    assert server._route("/ext/db_admin/table/users", "GET") == ("table_query", {"table": "users"})
    assert server._route("/ext/db_admin/table/users", "POST") == (
        "table_insert",
        {"table": "users"},
    )
    assert server._route("/ext/db_admin/table/users", "PATCH") is None


async def test_authorization_header_lookup(server: Any) -> None:
    assert server._authorization({"authorization": "Bearer t", "x": "1"}) == "Bearer t"
    assert server._authorization({"AUTHORIZATION": "Bearer u"}) == "Bearer u"
    assert server._authorization({"authorization": ""}) == ""
    assert server._authorization(None) == ""
    assert server._authorization({}) == ""


async def test_query_params_full_matrix(server: Any) -> None:
    # 空 query → 空参数
    assert server._query_params(None, None) == {}
    # limit/offset 合法转 int；非法忽略
    assert server._query_params({"limit": "5", "offset": "abc"}, None) == {"limit": 5}
    # 多值 filter 优先（query_multi 全量 AND），filter[] 键名等价
    assert server._query_params(
        {"filter": "a=1"},
        {"filter": ["a=1", "b=2"]},
    ) == {"filter": ["a=1", "b=2"]}
    assert server._query_params(None, {"filter[]": ["x=1"]}) == {"filter": ["x=1"]}
    # 单值兜底组单元素数组
    assert server._query_params({"filter": "a=1"}, None) == {"filter": ["a=1"]}
    assert server._query_params({"filter[]": "a=1"}, None) == {"filter": ["a=1"]}
    # 多值键全空 → 回落单值键
    assert server._query_params({"filter": "a=1"}, {"filter": ["", ""]}) == {"filter": ["a=1"]}
    # sort 透传
    assert server._query_params({"sort": "id"}, None) == {"sort": "id"}


async def test_status_tool_reflects_capability_injection(server: Any, monkeypatch: Any) -> None:
    _inject(monkeypatch, server, FakeCap())
    status = await server.db_admin_status()
    assert status["capability_injected"] is True

    def _raise(name: str) -> None:
        raise KeyError(name)

    monkeypatch.setattr(server.plugin, "get_capability", _raise)
    status2 = await server.db_admin_status()
    assert status2["capability_injected"] is False


async def test_http_handle_query_and_body_paths(server: Any, monkeypatch: Any) -> None:
    cap = FakeCap(result=_ok_envelope({"rows": []}))
    _inject(monkeypatch, server, cap)

    # table_query：query 参数进 params，Authorization 头透传
    resp = await server.http_handle(
        path="/ext/db_admin/table/users",
        method="GET",
        headers={"Authorization": "Bearer tok"},
        query={"limit": "2", "filter": "a=1"},
        query_multi=None,
    )
    assert cap.calls[0][0] == "table_query"
    assert cap.calls[0][1] == {
        "table": "users",
        "_authorization": "Bearer tok",
        "limit": 2,
        "filter": ["a=1"],
    }
    assert resp["success"] is True

    # table_insert：row 取自 body
    body = base64.b64encode('{"row": {"id": 1}}'.encode()).decode()
    resp2 = await server.http_handle(
        path="/ext/db_admin/table/users",
        method="POST",
        raw_body=body,
    )
    assert cap.calls[1][0] == "table_insert"
    assert cap.calls[1][1]["row"] == {"id": 1}
    assert resp2["success"] is True

    # execute：sql + confirm
    body_sql = base64.b64encode(b'{"sql": "delete from t", "confirm": true}').decode()
    await server.http_handle(path="/ext/db_admin/execute", method="POST", raw_body=body_sql)
    method3, params3 = cap.calls[2]
    assert (method3, params3["sql"], params3["confirm"]) == ("execute", "delete from t", True)

    # table_update_row：updates 取自 body
    body_up = base64.b64encode(b'{"updates": {"n": "x"}}').decode()
    await server.http_handle(
        path="/ext/db_admin/table/users/pk1",
        method="PATCH",
        raw_body=body_up,
    )
    method4, params4 = cap.calls[3]
    assert method4 == "table_update_row"
    assert params4["updates"] == {"n": "x"}
    assert params4["pk_value"] == "pk1"


async def test_http_handle_error_paths(server: Any, monkeypatch: Any) -> None:
    _inject(monkeypatch, server, FakeCap())

    # 非法 body → 400
    bad = await server.http_handle(
        path="/ext/db_admin/table/users",
        method="POST",
        raw_body=base64.b64encode(b"not-json").decode(),
    )
    assert bad["data"]["status"] == 400
    assert "invalid JSON" in _body_json(bad)["error"]["message"]

    # capability 缺席 → 502
    def _raise(name: str) -> None:
        raise KeyError(name)

    monkeypatch.setattr(server.plugin, "get_capability", _raise)
    missing = await server.http_handle(path="/ext/db_admin/tables", method="GET")
    assert missing["data"]["status"] == 502
    assert "not injected" in _body_json(missing)["error"]["message"]

    # capability 调用异常 → 502（message 带原因）
    _inject(monkeypatch, server, FakeCap(error=RuntimeError("boom")))
    failed = await server.http_handle(path="/ext/db_admin/tables", method="GET")
    assert failed["data"]["status"] == 502
    assert "boom" in _body_json(failed)["error"]["message"]

    # 非 dict 信封 → 502
    _inject(monkeypatch, server, FakeCap(result=[1, 2]))
    non_dict = await server.http_handle(path="/ext/db_admin/tables", method="GET")
    assert non_dict["data"]["status"] == 502

    # 错误信封：非 2xx → error 结构，code 字符串化
    _inject(
        monkeypatch,
        server,
        FakeCap(result={"status": 403, "error": {"code": 403, "message": "denied"}}),
    )
    denied = await server.http_handle(path="/ext/db_admin/tables", method="GET")
    assert _body_json(denied)["error"] == {"code": "403", "message": "denied"}
