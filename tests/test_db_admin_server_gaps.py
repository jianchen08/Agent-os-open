# @feature: FP-0.2.二 可观测性 | @ci: python-coverage
"""db_admin HTTP 面缺口补测（根级，供插桩基集登记）。

经公开工具入口 http.handle / db_admin.status 行为驱动（不直接断言私有函数）：
- 路由族：list_tables / execute / table 二段 GET·POST / 行级 GET·PATCH·DELETE
- 路由 404：错误前缀、未知子路径、错误方法、空路径
- query 族：limit/offset 转 int 与非法忽略、filter 多值（query_multi 全量）
  优先于单值、filter[] 键名等价、空多值回落单值、sort 透传
- 鉴权透传：Authorization 头大小写不敏感进入 params._authorization，空头不透传
- 路径段 unquote：pk 含百分号编码字符时解码
- body 族：table_insert.row / table_update_row.updates / execute.sql+confirm
- 容错族：body 非法 JSON 400、capability 缺席/异常 502、非 dict 信封 502、
  错误信封 code 字符串化与缺省、2xx 透传 body
- db_admin.status：capability 注入两态
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins" / "shared" / "db_admin"

_MODULE_NAME = "db_admin_server_gaps_under_test"


def _load_server() -> Any:
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, str(_PLUGIN_DIR / "server.py"))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def server() -> Any:
    return _load_server()


class FakeCap:
    """db-admin capability 替身：脚本化响应/异常，记录调用序列。"""

    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, method: str, params: dict[str, Any]) -> Any:
        self.calls.append((method, params))
        if self.error is not None:
            raise self.error
        return self.result


def _inject(monkeypatch: pytest.MonkeyPatch, mod: Any, cap: Any) -> None:
    monkeypatch.setattr(mod.plugin, "get_capability", lambda name: cap)


def _b64(payload: Any) -> str:
    return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")


def _body_json(resp: dict[str, Any]) -> Any:
    """http_json 信封 data.body 为 base64 —— 解码为 JSON 断言。"""
    return json.loads(base64.b64decode(resp["data"]["body"]))


def _ok_envelope(payload: Any = {"ok": True}, status: int = 200) -> dict[str, Any]:
    return {"status": status, "body": payload}


# ─────────────────────────── 路由族 ───────────────────────────


class TestRouting:
    @pytest.mark.parametrize(
        ("path", "method", "expected_cap_method"),
        [
            ("/ext/db_admin/tables", "GET", "list_tables"),
            ("/ext/db_admin/execute", "POST", "execute"),
            ("/ext/db_admin/table/users", "GET", "table_query"),
            ("/ext/db_admin/table/users", "POST", "table_insert"),
            ("/ext/db_admin/table/users/pk1", "GET", "table_get_row"),
            ("/ext/db_admin/table/users/pk1", "PATCH", "table_update_row"),
            ("/ext/db_admin/table/users/pk1", "DELETE", "table_delete_row"),
        ],
    )
    async def test_routed_paths_call_capability(
        self, server: Any, monkeypatch: pytest.MonkeyPatch,
        path: str, method: str, expected_cap_method: str,
    ) -> None:
        cap = FakeCap(result=_ok_envelope({"done": True}))
        _inject(monkeypatch, server, cap)

        resp = await server.http_handle(path=path, method=method)

        assert resp["success"] is True
        # 成功信封：2xx 状态与 body 原样透传
        assert resp["data"]["status"] == 200
        assert _body_json(resp) == {"done": True}
        # 恰好一次 capability 调用，method 与路由表一致
        assert [m for m, _ in cap.calls] == [expected_cap_method]

    @pytest.mark.parametrize(
        ("path", "method"),
        [
            ("/ext/other/tables", "GET"),  # 前缀不属于 db_admin
            ("/db_admin/tables", "GET"),  # 缺 ext 段
            ("/ext/db_admin/tables", "POST"),  # list_tables 仅接受 GET
            ("/ext/db_admin/execute", "GET"),  # execute 仅接受 POST
            ("/ext/db_admin/nope", "GET"),  # 未知子路径
            ("/ext/db_admin/table", "GET"),  # 缺表名段
            ("/ext/db_admin/table/users/pk1/extra", "GET"),  # 路径段过多
            ("/", "GET"),  # 空路径
        ],
    )
    async def test_unrouted_paths_return_404_without_capability_call(
        self, server: Any, monkeypatch: pytest.MonkeyPatch, path: str, method: str,
    ) -> None:
        cap = FakeCap(result=_ok_envelope())
        _inject(monkeypatch, server, cap)

        resp = await server.http_handle(path=path, method=method)

        assert resp["success"] is True  # 协议级错误信封：插件全权控制响应形态
        assert resp["data"]["status"] == 404
        body = _body_json(resp)
        assert body["error"]["code"] == "404"
        assert method in body["error"]["message"] and path in body["error"]["message"]
        # 未路由 → 不产生任何 capability 调用
        assert cap.calls == []


# ─────────────────────────── 路径参数 ───────────────────────────


class TestPathParams:
    async def test_pk_percent_decoded(self, server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """内核 uri.path 不做百分号解码，插件侧补齐——含特殊字符的 pk 原样到达。"""
        cap = FakeCap(result=_ok_envelope())
        _inject(monkeypatch, server, cap)

        await server.http_handle(path="/ext/db_admin/table/users/a%2Fb%20c", method="GET")

        _, params = cap.calls[0]
        assert params["table"] == "users"
        assert params["pk_value"] == "a/b c"


# ─────────────────────────── 鉴权透传 ───────────────────────────


class TestAuthorizationPassthrough:
    @pytest.mark.parametrize(
        "headers",
        [
            {"Authorization": "Bearer tok"},
            {"authorization": "Bearer tok"},
            {"AUTHORIZATION": "Bearer tok"},
        ],
    )
    async def test_header_case_insensitive(
        self, server: Any, monkeypatch: pytest.MonkeyPatch, headers: dict[str, str],
    ) -> None:
        cap = FakeCap(result=_ok_envelope())
        _inject(monkeypatch, server, cap)

        await server.http_handle(path="/ext/db_admin/tables", method="GET", headers=headers)

        assert cap.calls[0][1]["_authorization"] == "Bearer tok"

    async def test_missing_or_empty_header_yields_empty_string(
        self, server: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cap = FakeCap(result=_ok_envelope())
        _inject(monkeypatch, server, cap)

        await server.http_handle(path="/ext/db_admin/tables", method="GET", headers=None)
        await server.http_handle(
            path="/ext/db_admin/tables", method="GET", headers={"authorization": ""},
        )

        assert [p["_authorization"] for _, p in cap.calls] == ["", ""]


# ─────────────────────────── query 组参 ───────────────────────────


class TestQueryParams:
    @pytest.mark.parametrize(
        ("query", "query_multi", "expected_extra"),
        [
            # 空 query → 无分页/过滤参数
            ({}, None, {}),
            # limit/offset 合法转 int
            ({"limit": "5", "offset": "10"}, None, {"limit": 5, "offset": 10}),
            # 非法数值忽略（对齐原行为），不整体失败
            ({"limit": "abc", "offset": "2"}, None, {"offset": 2}),
            # query_multi 多值全量透传（多条件 AND 不丢条件），优先于单值
            ({"filter": "a=1"}, {"filter": ["a=1", "b=2"]}, {"filter": ["a=1", "b=2"]}),
            # filter[] 是 axios 数组默认序列化形态，键名等价
            (None, {"filter[]": ["x=1"]}, {"filter": ["x=1"]}),
            # 单值兜底：组单元素数组保持 filter 恒为数组的契约
            ({"filter": "a=1"}, None, {"filter": ["a=1"]}),
            ({"filter[]": "a=1"}, None, {"filter": ["a=1"]}),
            # 多值键全空字符串 → 回落单值键
            ({"filter": "a=1"}, {"filter": ["", ""]}, {"filter": ["a=1"]}),
            # sort 透传
            ({"sort": "-id"}, None, {"sort": "-id"}),
        ],
    )
    async def test_query_matrix(
        self, server: Any, monkeypatch: pytest.MonkeyPatch,
        query: dict[str, str] | None,
        query_multi: dict[str, list[str]] | None,
        expected_extra: dict[str, Any],
    ) -> None:
        cap = FakeCap(result=_ok_envelope())
        _inject(monkeypatch, server, cap)

        resp = await server.http_handle(
            path="/ext/db_admin/table/users", method="GET",
            query=query, query_multi=query_multi,
        )

        assert resp["success"] is True
        method, params = cap.calls[0]
        # 路径参数/鉴权占位之外的 query 组参逐一相等
        assert method == "table_query"
        assert {k: v for k, v in params.items() if k not in ("table", "_authorization")} == expected_extra


# ─────────────────────────── body 组参 ───────────────────────────


class TestBodyParams:
    async def test_insert_takes_row_from_body(
        self, server: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cap = FakeCap(result=_ok_envelope())
        _inject(monkeypatch, server, cap)

        await server.http_handle(
            path="/ext/db_admin/table/users", method="POST",
            raw_body=_b64({"row": {"id": 1, "name": "a"}}),
        )

        method, params = cap.calls[0]
        assert method == "table_insert"
        assert params["row"] == {"id": 1, "name": "a"}

    async def test_update_row_takes_updates_from_body(
        self, server: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cap = FakeCap(result=_ok_envelope())
        _inject(monkeypatch, server, cap)

        await server.http_handle(
            path="/ext/db_admin/table/users/pk9", method="PATCH",
            raw_body=_b64({"updates": {"name": "b"}}),
        )

        method, params = cap.calls[0]
        assert method == "table_update_row"
        assert params["updates"] == {"name": "b"}
        assert params["pk_value"] == "pk9"

    @pytest.mark.parametrize(
        ("body", "expected_confirm"),
        [
            ({"sql": "delete from t", "confirm": True}, True),
            ({"sql": "select 1"}, False),  # confirm 缺省 False
        ],
    )
    async def test_execute_takes_sql_and_confirm(
        self, server: Any, monkeypatch: pytest.MonkeyPatch,
        body: dict[str, Any], expected_confirm: bool,
    ) -> None:
        cap = FakeCap(result=_ok_envelope())
        _inject(monkeypatch, server, cap)

        await server.http_handle(path="/ext/db_admin/execute", method="POST", raw_body=_b64(body))

        method, params = cap.calls[0]
        assert method == "execute"
        assert params["sql"] == body["sql"]
        assert params["confirm"] is expected_confirm


# ─────────────────────────── 容错族 ───────────────────────────


class TestErrorPaths:
    async def test_invalid_json_body_returns_400(
        self, server: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cap = FakeCap(result=_ok_envelope())
        _inject(monkeypatch, server, cap)

        resp = await server.http_handle(
            path="/ext/db_admin/table/users", method="POST",
            raw_body=base64.b64encode(b"not-json{").decode(),
        )

        assert resp["data"]["status"] == 400
        assert "invalid JSON" in _body_json(resp)["error"]["message"]
        assert cap.calls == []  # body 解析失败不触达 capability

    async def test_capability_missing_returns_502(
        self, server: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _raise(name: str) -> None:
            raise KeyError(name)

        monkeypatch.setattr(server.plugin, "get_capability", _raise)
        resp = await server.http_handle(path="/ext/db_admin/tables", method="GET")

        assert resp["data"]["status"] == 502
        assert "not injected" in _body_json(resp)["error"]["message"]

    async def test_capability_failure_returns_502_with_reason(
        self, server: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _inject(monkeypatch, server, FakeCap(error=RuntimeError("boom")))
        resp = await server.http_handle(path="/ext/db_admin/tables", method="GET")

        assert resp["data"]["status"] == 502
        body = _body_json(resp)
        assert "boom" in body["error"]["message"]
        assert "failed" in body["error"]["message"]

    async def test_non_dict_envelope_returns_502(
        self, server: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _inject(monkeypatch, server, FakeCap(result=["not", "a", "dict"]))
        resp = await server.http_handle(path="/ext/db_admin/tables", method="GET")

        assert resp["data"]["status"] == 502
        assert "non-dict envelope" in _body_json(resp)["error"]["message"]

    async def test_error_envelope_code_stringified(
        self, server: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """错误信封：非 2xx → error 结构，code 字符串化透传给前端。"""
        _inject(
            monkeypatch, server,
            FakeCap(result={"status": 403, "error": {"code": 403, "message": "denied"}}),
        )
        resp = await server.http_handle(path="/ext/db_admin/tables", method="GET")

        assert resp["success"] is True
        assert resp["data"]["status"] == 403
        assert _body_json(resp)["error"] == {"code": "403", "message": "denied"}

    async def test_error_envelope_defaults_when_error_missing(
        self, server: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """信封缺 error 对象 → code 回落状态码字符串、message 回缺省文案。"""
        _inject(monkeypatch, server, FakeCap(result={"status": 500}))
        resp = await server.http_handle(path="/ext/db_admin/tables", method="GET")

        assert resp["data"]["status"] == 500
        assert _body_json(resp)["error"] == {"code": "500", "message": "db-admin error"}

    async def test_non_2xx_success_status_passes_body_through(
        self, server: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """2xx 边界内非 200（如 201）走成功分支，body 原样透传。"""
        _inject(monkeypatch, server, FakeCap(result=_ok_envelope({"id": 7}, status=201)))
        resp = await server.http_handle(path="/ext/db_admin/table/users", method="POST")

        assert resp["data"]["status"] == 201
        assert _body_json(resp) == {"id": 7}


# ─────────────────────────── 状态工具 ───────────────────────────


class TestStatusTool:
    async def test_injected_capability_reported(self, server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        _inject(monkeypatch, server, FakeCap())
        status = await server.db_admin_status()

        assert status == {
            "plugin": "db_admin",
            "capability": "db-admin",
            "capability_injected": True,
        }

    async def test_missing_capability_reported(self, server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(name: str) -> None:
            raise KeyError(name)

        monkeypatch.setattr(server.plugin, "get_capability", _raise)
        status = await server.db_admin_status()

        assert status["capability_injected"] is False
