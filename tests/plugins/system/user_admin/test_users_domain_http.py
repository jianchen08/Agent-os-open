# @feature: FP-0.2.二 可观测性 | @vision: V3 可嵌入 | @ci: python-coverage
"""user_admin 插件 users 域（channel_api 退役批次 2 自持承接）HTTP 测试。

覆盖 /ext/user_admin/users* 六组端点（源 routes_missing.py users_router）：
1. GET /users —— db-admin.table_query 凭证透传查 users 表（脱敏/映射/id/is_active），
   能力不可用降级 200 空列表，内核非 2xx 信封透传（403 等）
2. GET /users/stats —— 聚合统计（total/active/admin）
3. PUT /users/{id}/role —— db-admin.table_update_row 真实写（role 白名单校验）
4. DELETE /users/{id} —— db-admin.table_delete_row 真实删
5. PUT/PATCH /users/{id}/active + GET/PUT /users/settings —— 存根语义
6. PATCH /users/{id}/role + tenant —— user-admin capability 保留面（原 4 端点回归）
7. _authorization 透传：Authorization 头原样进 capability params（内核真鉴权）
8. POST /users 已删除（create_user 处置）→ 404
9. 404 未知路由 / 非法 body / 非法 role 边界
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

_PLUGIN_DIR = Path(__file__).resolve().parents[4] / "plugins" / "shared" / "user_admin"


def _load_server() -> Any:
    """动态加载 user_admin/server.py（每次新建，隔离模块级状态）。"""
    spec = importlib.util.spec_from_file_location(
        "user_admin_http_test_server",
        str(_PLUGIN_DIR / "server.py"),
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["user_admin_http_test_server"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def server() -> Any:
    return _load_server()


def _run(coro: Any) -> Any:
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _call(
    server: Any,
    path: str,
    method: str = "GET",
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    return _run(server.http_handle(path=path, method=method, raw_body=raw_body,
                                   headers=headers, query=query))


def _decode(result: dict[str, Any]) -> tuple[int, Any]:
    """解包 http.handle 返回 → (status, json_body)。"""
    assert result["success"], result
    resp = result["data"]
    body = base64.b64decode(resp["body"]).decode("utf-8")
    return resp["status"], json.loads(body)


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


class FakeCapability:
    """fake capability 句柄：记录调用、按路由表返回信封。"""

    def __init__(self, responses: dict[str, Any], calls: list[tuple[str, dict[str, Any]]]) -> None:
        self._responses = responses
        self._calls = calls

    async def call(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        self._calls.append((method, params))
        return self._responses.get(method, {"status": 200, "body": {}})


def _inject(server: Any, name: str, responses: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """把 fake capability 塞进 plugin._capabilities；返回调用记录列表。"""
    calls: list[tuple[str, dict[str, Any]]] = []
    server.plugin._capabilities[name] = FakeCapability(responses, calls)
    return calls


_ROWS = [
    {
        "user_id": "u1", "username": "alice", "password": "sekrit", "email": "a@x.io",
        "role": "admin", "tenant_id": "u1", "created_at": "2026-01-01T00:00:00Z",
        "last_login_at": "2026-08-01T00:00:00Z",
    },
    {
        "user_id": "u2", "username": "bob", "password": "hunter2", "email": "b@x.io",
        "role": "user", "tenant_id": "u2", "created_at": "2026-02-01T00:00:00Z",
    },
]


def _db_envelope(body: Any, status: int = 200) -> dict[str, Any]:
    if 200 <= status < 300:
        return {"status": status, "body": body}
    return {"status": status, "error": {"code": str(status), "message": "forbidden" if status == 403 else "error"}}


# ── GET /users 列表（db-admin 凭证透传）───────────────────────────────────


def test_users_list_maps_rows(server: Any) -> None:
    resp = {"table": "users", "total": 2, "limit": 100, "offset": 0, "rows": _ROWS}
    calls = _inject(server, "db-admin", {"table_query": _db_envelope(resp)})

    status, body = _decode(_call(server, "/ext/user_admin/users"))

    assert status == 200
    assert [u["id"] for u in body] == ["u1", "u2"]
    assert body[0]["username"] == "alice"
    assert body[0]["role"] == "admin"
    assert "password" not in body[0] and "password_hash" not in body[0]
    assert body[0]["is_active"] is True  # 缺列补齐
    assert calls[0][0] == "table_query"
    assert calls[0][1]["table"] == "users"
    assert calls[0][1]["limit"] == 100
    assert calls[0][1]["offset"] == 0


def test_users_list_query_params_and_auth_passthrough(server: Any) -> None:
    resp = {"table": "users", "total": 0, "limit": 20, "offset": 5, "rows": []}
    calls = _inject(server, "db-admin", {"table_query": _db_envelope(resp)})

    _decode(_call(
        server, "/ext/user_admin/users", query={"skip": "5", "limit": "20"},
        headers={"authorization": "Bearer eyJ0eXBlIjowfQ=="},
    ))

    params = calls[0][1]
    assert params["limit"] == 20
    assert params["offset"] == 5
    assert params["_authorization"] == "Bearer eyJ0eXBlIjowfQ=="  # 原样透传（内核真鉴权）


def test_users_list_degrades_empty_when_capability_missing(server: Any) -> None:
    """db-admin 未注入（内核握手未完成）→ HTTP 200 空列表（前端契约不破坏）。"""
    status, body = _decode(_call(server, "/ext/user_admin/users"))

    assert status == 200
    assert body == []


def test_users_list_passthrough_kernel_403(server: Any) -> None:
    """内核真鉴权 403（非 admin/viewer）不透传业务数据，原样暴露。"""
    calls = _inject(server, "db-admin", {"table_query": _db_envelope({}, status=403)})

    status, body = _decode(_call(server, "/ext/user_admin/users"))

    assert calls[0][1]["_authorization"] == ""  # 无头 → 空凭证 → 内核 401/403
    assert status == 403
    assert body["error"]["code"] == "403"


# ── GET /users/stats ──────────────────────────────────────────────────────


def test_users_stats_counts(server: Any) -> None:
    resp = {"table": "users", "total": 2, "limit": 500, "offset": 0, "rows": _ROWS}
    calls = _inject(server, "db-admin", {"table_query": _db_envelope(resp)})

    status, body = _decode(_call(server, "/ext/user_admin/users/stats"))

    assert status == 200
    assert body == {"total_users": 2, "active_users": 2, "admin_count": 1}
    assert calls[0][1]["limit"] == 500


def test_users_stats_degrades_empty(server: Any) -> None:
    status, body = _decode(_call(server, "/ext/user_admin/users/stats"))

    assert status == 200
    assert body == {"total_users": 0, "active_users": 0, "admin_count": 0}


# ── PUT /users/{id}/role（db-admin 真实写）────────────────────────────────


def test_users_role_update_writes(server: Any) -> None:
    calls = _inject(server, "db-admin", {"table_update_row": _db_envelope({"updated": 1})})

    status, body = _decode(_call(
        server, "/ext/user_admin/users/u2/role", "PUT",
        raw_body=_b64(json.dumps({"role": "admin"})),
        headers={"authorization": "Bearer tok"},
    ))

    assert status == 200
    assert body == {"id": "u2", "role": "admin"}
    params = calls[0][1]
    assert params["table"] == "users"
    assert params["pk_value"] == "u2"
    assert params["updates"] == {"role": "admin"}
    assert params["_authorization"] == "Bearer tok"


def test_users_role_update_invalid_role_400(server: Any) -> None:
    status, body = _decode(_call(
        server, "/ext/user_admin/users/u2/role", "PUT",
        raw_body=_b64(json.dumps({"role": "superadmin"})),
    ))

    assert status == 400
    assert "admin 或 user" in body["error"]["message"]


def test_users_role_update_empty_body_400(server: Any) -> None:
    status, _ = _decode(_call(server, "/ext/user_admin/users/u2/role", "PUT"))

    assert status == 400


def test_users_role_update_kernel_403_passthrough(server: Any) -> None:
    _inject(server, "db-admin", {"table_update_row": _db_envelope({}, status=403)})

    status, body = _decode(_call(
        server, "/ext/user_admin/users/u2/role", "PUT", raw_body=_b64(json.dumps({"role": "user"})),
    ))

    assert status == 403


# ── DELETE /users/{id}（db-admin 真实删）──────────────────────────────────


def test_users_delete_real(server: Any) -> None:
    calls = _inject(server, "db-admin", {"table_delete_row": _db_envelope({"deleted": 1})})

    status, body = _decode(_call(
        server, "/ext/user_admin/users/u1", "DELETE", headers={"authorization": "Bearer tok"},
    ))

    assert status == 200
    assert body == {"message": "用户已删除", "id": "u1"}
    assert calls[0][1] == {"table": "users", "pk_value": "u1", "_authorization": "Bearer tok"}


def test_users_delete_kernel_403_passthrough(server: Any) -> None:
    _inject(server, "db-admin", {"table_delete_row": _db_envelope({}, status=403)})

    status, body = _decode(_call(server, "/ext/user_admin/users/u1", "DELETE"))

    assert status == 403


# ── active / settings 存根 ────────────────────────────────────────────────


def test_users_active_stub_put_and_patch(server: Any) -> None:
    for method in ("PUT", "PATCH"):
        status, body = _decode(_call(
            server, "/ext/user_admin/users/u2/active", method, raw_body=_b64(json.dumps({"is_active": False})),
        ))
        assert status == 200
        assert body == {"id": "u2", "is_active": True}  # 表无 is_active 列，保持存根


def test_users_settings_stub(server: Any) -> None:
    status, body = _decode(_call(server, "/ext/user_admin/users/settings"))
    assert status == 200
    assert body == {"settings": {}}

    status, body = _decode(_call(
        server, "/ext/user_admin/users/settings", "PUT", raw_body=_b64(json.dumps({"theme": "dark"})),
    ))
    assert status == 200
    assert body == {"settings": {}, "message": "设置已更新"}


# ── user-admin capability 保留面（PATCH role / tenant 回归）───────────────


def test_patch_role_keeps_user_admin_capability(server: Any) -> None:
    env = {"status": 200, "body": {"user": {"id": "u2", "username": "bob", "role": "admin"}}}
    calls = _inject(server, "user-admin", {"update_role": env})

    status, body = _decode(_call(
        server, "/ext/user_admin/users/u2/role", "PATCH",
        raw_body=_b64(json.dumps({"role": "admin"})), headers={"authorization": "Bearer tok"},
    ))

    assert status == 200
    assert body["user"]["role"] == "admin"
    assert calls[0][0] == "update_role"
    assert calls[0][1]["_authorization"] == "Bearer tok"
    assert calls[0][1]["user_id"] == "u2"


def test_patch_tenant_keeps_user_admin_capability(server: Any) -> None:
    env = {"status": 200, "body": {"user": {"id": "u2", "tenant_id": "t9"}}}
    calls = _inject(server, "user-admin", {"update_tenant": env})

    status, body = _decode(_call(
        server, "/ext/user_admin/users/u2/tenant", "PATCH",
        raw_body=_b64(json.dumps({"tenant_id": "t9"})), headers={"authorization": "Bearer tok"},
    ))

    assert status == 200
    assert calls[0][1] == {"user_id": "u2", "_authorization": "Bearer tok", "tenant_id": "t9"}


def test_user_admin_capability_error_envelope(server: Any) -> None:
    env = {"status": 403, "error": {"code": "403", "message": "不能修改自己的角色"}}
    _inject(server, "user-admin", {"update_role": env})

    status, body = _decode(_call(
        server, "/ext/user_admin/users/u2/role", "PATCH", raw_body=_b64(json.dumps({"role": "user"})),
    ))

    assert status == 403
    assert body["error"]["message"] == "不能修改自己的角色"


# ── 边界：create_user 已删除 / 未知路由 / body 非法 ──────────────────────


def test_users_create_removed_404(server: Any) -> None:
    """create_user 存根按方案删除（无消费方；用户创建归内核 register/user-admin 面）。"""
    status, body = _decode(_call(server, "/ext/user_admin/users", "POST"))

    assert status == 404


def test_unknown_route_404(server: Any) -> None:
    status, body = _decode(_call(server, "/ext/user_admin/whatever"))

    assert status == 404
    assert "no route" in body["error"]["message"]


def test_role_update_invalid_json_400(server: Any) -> None:
    status, _ = _decode(_call(server, "/ext/user_admin/users/u2/role", "PUT", raw_body=_b64("{bad")))

    assert status == 400