# @feature: FP-0.2.二 可观测性 | @ci: python-coverage
"""metrics_admin HTTP 面缺口补测（根级，供插桩基集登记）。

经公开工具入口 http.handle 行为驱动（不直接断言私有函数）：
- 路由族：query/series/prometheus 三分支；前缀不符/段数不足/未知子路径 → 404
- query 过滤参数：_QUERY_KEYS 白名单内非空值透传、空值键丢弃、白名单外键丢弃
- 鉴权透传：Authorization 头大小写不敏感进 params._authorization，空头/无头为空串
- 容错族：capability 调用异常 502（原因带出）、非 dict 信封 502
- 非 2xx 信封：error 对象缺省时 code 回落状态码字符串、message 回落缺省文案
- status 工具：capability 注入两态

插件位于非基集目录（plugins/shared/metrics_admin/），本文件自带路径自举。
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

_PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugins" / "shared" / "metrics_admin"
_MODULE_NAME = "metrics_admin_gaps_under_test"


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
    """metrics-admin capability 替身：脚本化响应/异常，记录调用序列。"""

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


def _body_json(resp: dict[str, Any]) -> Any:
    return json.loads(base64.b64decode(resp["data"]["body"]))


# ─────────────────────────── 路由族 ───────────────────────────


class TestRouting:
    @pytest.mark.parametrize(
        ("path", "expected_cap_method"),
        [
            ("/ext/metrics_admin/query", "query"),
            ("/ext/metrics_admin/series", "list"),
            ("/ext/metrics_admin/prometheus", "prometheus"),
            # 百分号编码的段先 unquote 再匹配（前端/代理编码形态）
            ("/ext/metrics_admin/quer%79", "query"),
        ],
    )
    async def test_routed_paths_call_expected_method(
        self, server: Any, monkeypatch: pytest.MonkeyPatch,
        path: str, expected_cap_method: str,
    ) -> None:
        cap = FakeCap(result={"status": 200, "body": {}})
        _inject(monkeypatch, server, cap)

        resp = await server.http_handle(path=path, method="GET")

        assert resp["success"] is True
        assert [m for m, _ in cap.calls] == [expected_cap_method]

    @pytest.mark.parametrize(
        ("path", "method"),
        [
            ("/ext/other/query", "GET"),        # 前缀不属于 metrics_admin
            ("/metrics_admin/query", "GET"),    # 缺 ext 段
            ("/ext", "GET"),                    # 段数不足
            ("/", "GET"),                       # 空路径
            ("/ext/metrics_admin", "GET"),      # 无子路径
            ("/ext/metrics_admin/query/extra", "GET"),  # 子路径过多
            ("/ext/metrics_admin/nope", "GET"),  # 未知子路径
        ],
    )
    async def test_unrouted_paths_404_without_capability_call(
        self, server: Any, monkeypatch: pytest.MonkeyPatch, path: str, method: str,
    ) -> None:
        cap = FakeCap(result={"status": 200, "body": {}})
        _inject(monkeypatch, server, cap)

        resp = await server.http_handle(path=path, method=method)

        assert resp["success"] is True
        assert resp["data"]["status"] == 404
        body = _body_json(resp)
        assert body["error"]["code"] == "404"
        assert path in body["error"]["message"]
        assert cap.calls == []


# ─────────────────────────── query 过滤参数 ───────────────────────────


class TestQueryParams:
    @pytest.mark.parametrize(
        ("query", "expected_extra"),
        [
            (None, {}),
            ({}, {}),
            # 白名单四键非空值全量透传
            (
                {"plugin": "memory", "metric": "calls", "window": "1h", "labels": "a=1"},
                {"plugin": "memory", "metric": "calls", "window": "1h", "labels": "a=1"},
            ),
            # 空值/缺失键丢弃（不产生空过滤条件）
            ({"plugin": "", "metric": "calls"}, {"metric": "calls"}),
            # 白名单外键丢弃（不透传任意 query 到内核）
            ({"unexpected": "x", "metric": "calls"}, {"metric": "calls"}),
        ],
    )
    async def test_query_whitelist_filtering(
        self, server: Any, monkeypatch: pytest.MonkeyPatch,
        query: dict[str, str] | None, expected_extra: dict[str, str],
    ) -> None:
        cap = FakeCap(result={"status": 200, "body": {}})
        _inject(monkeypatch, server, cap)

        resp = await server.http_handle(path="/ext/metrics_admin/query", method="GET", query=query)

        assert resp["success"] is True
        method, params = cap.calls[0]
        assert method == "query"
        assert {k: v for k, v in params.items() if k != "_authorization"} == expected_extra

    async def test_non_query_routes_send_no_filter_params(
        self, server: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """series/prometheus 面无过滤参数（query 只属于 query 方法）。"""
        cap = FakeCap(result={"status": 200, "body": {}})
        _inject(monkeypatch, server, cap)

        await server.http_handle(
            path="/ext/metrics_admin/series", method="GET",
            query={"plugin": "memory", "metric": "calls"},
        )
        await server.http_handle(
            path="/ext/metrics_admin/prometheus", method="GET", query={"plugin": "memory"},
        )

        assert [m for m, _ in cap.calls] == ["list", "prometheus"]
        for _method, params in cap.calls:
            assert set(params) == {"_authorization"}


# ─────────────────────────── 鉴权透传 ───────────────────────────


class TestAuthorizationPassthrough:
    @pytest.mark.parametrize(
        "headers",
        [
            {"Authorization": "Bearer tok"},
            {"authorization": "Bearer tok"},
            {"AUTHORIZATION": "Bearer tok"},
            {"X-Other": "v", "authorization": "Bearer tok"},
        ],
    )
    async def test_header_case_insensitive(
        self, server: Any, monkeypatch: pytest.MonkeyPatch, headers: dict[str, str],
    ) -> None:
        cap = FakeCap(result={"status": 200, "body": {}})
        _inject(monkeypatch, server, cap)

        await server.http_handle(path="/ext/metrics_admin/query", method="GET", headers=headers)

        assert cap.calls[0][1]["_authorization"] == "Bearer tok"

    @pytest.mark.parametrize("headers", [None, {}, {"authorization": ""}, {"x-other": "v"}])
    async def test_missing_or_empty_header_yields_empty_string(
        self, server: Any, monkeypatch: pytest.MonkeyPatch, headers: dict[str, str] | None,
    ) -> None:
        cap = FakeCap(result={"status": 200, "body": {}})
        _inject(monkeypatch, server, cap)

        await server.http_handle(path="/ext/metrics_admin/query", method="GET", headers=headers)

        assert cap.calls[0][1]["_authorization"] == ""


# ─────────────────────────── 容错族 ───────────────────────────


class TestErrorPaths:
    async def test_capability_missing_returns_502(
        self, server: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _raise(name: str) -> None:
            raise KeyError(name)

        monkeypatch.setattr(server.plugin, "get_capability", _raise)
        resp = await server.http_handle(path="/ext/metrics_admin/query", method="GET")

        assert resp["data"]["status"] == 502
        assert "not injected" in _body_json(resp)["error"]["message"]

    async def test_capability_failure_returns_502_with_reason(
        self, server: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _inject(monkeypatch, server, FakeCap(error=RuntimeError("router down")))
        resp = await server.http_handle(path="/ext/metrics_admin/series", method="GET")

        body = _body_json(resp)
        assert resp["data"]["status"] == 502
        assert "router down" in body["error"]["message"]
        assert "failed" in body["error"]["message"]

    async def test_non_dict_envelope_returns_502(
        self, server: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _inject(monkeypatch, server, FakeCap(result=["not", "a", "dict"]))
        resp = await server.http_handle(path="/ext/metrics_admin/query", method="GET")

        assert resp["data"]["status"] == 502
        assert "non-dict envelope" in _body_json(resp)["error"]["message"]

    async def test_error_envelope_code_stringified(
        self, server: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """非 2xx 信封：error 结构透传，code 字符串化（前端契约）。"""
        _inject(
            monkeypatch, server,
            FakeCap(result={"status": 401, "error": {"code": 401, "message": "unauthorized"}}),
        )
        resp = await server.http_handle(path="/ext/metrics_admin/query", method="GET")

        assert resp["data"]["status"] == 401
        assert _body_json(resp)["error"] == {"code": "401", "message": "unauthorized"}

    async def test_error_envelope_defaults_when_error_missing(
        self, server: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """信封缺 error 对象 → code 回落状态码字符串、message 回落缺省文案。"""
        _inject(monkeypatch, server, FakeCap(result={"status": 500}))
        resp = await server.http_handle(path="/ext/metrics_admin/query", method="GET")

        assert resp["data"]["status"] == 500
        assert _body_json(resp)["error"] == {"code": "500", "message": "metrics-admin error"}

    async def test_envelope_status_defaults_to_200_and_passes_body(
        self, server: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """信封缺 status → 按 200 成功处理，body 原样透传。"""
        _inject(monkeypatch, server, FakeCap(result={"body": {"total": 3}}))
        resp = await server.http_handle(path="/ext/metrics_admin/query", method="GET")

        assert resp["data"]["status"] == 200
        assert _body_json(resp) == {"total": 3}

    async def test_2xx_non_200_passes_body_through(
        self, server: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """2xx 边界内非 200（如 202）走成功分支。"""
        _inject(monkeypatch, server, FakeCap(result={"status": 202, "body": {"queued": True}}))
        resp = await server.http_handle(path="/ext/metrics_admin/series", method="GET")

        assert resp["data"]["status"] == 202
        assert _body_json(resp) == {"queued": True}

    async def test_prometheus_missing_body_becomes_empty_text(
        self, server: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """prometheus 信封缺 body → 空 exposition 文本（非 JSON 空对象）。"""
        _inject(monkeypatch, server, FakeCap(result={"status": 200}))
        resp = await server.http_handle(path="/ext/metrics_admin/prometheus", method="GET")

        assert resp["data"]["status"] == 200
        assert base64.b64decode(resp["data"]["body"]).decode("utf-8") == ""


# ─────────────────────────── 状态工具 ───────────────────────────


class TestStatusTool:
    async def test_injected_capability_reported(
        self, server: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _inject(monkeypatch, server, FakeCap())
        status = await server.metrics_admin_status()

        assert status == {
            "plugin": "metrics_admin",
            "capability": "metrics-admin",
            "capability_injected": True,
        }

    async def test_missing_capability_reported(
        self, server: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _raise(name: str) -> None:
            raise KeyError(name)

        monkeypatch.setattr(server.plugin, "get_capability", _raise)
        status = await server.metrics_admin_status()

        assert status["capability_injected"] is False
        assert status["plugin"] == "metrics_admin"
