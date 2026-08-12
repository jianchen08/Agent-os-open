"""P0-2 channel_api 越权修复测试（TDD）。

回归安全缺口：``http_handle`` 不鉴权，各 ``_handle_*`` 把 ``_user={}``（空 dict）
传给底层，丢失 caller 身份；users 域管理员端点（create_user / delete_user /
update_role / update_active）无法做垂直越权检查——任一普通用户即可创建/删除用户、
改他人角色。

契约：
1. users 域管理员端点要求 ``_user.role == "admin"``，否则 403 Forbidden；
2. ``http_handle`` 从 Authorization 头解析真实 caller（sub/role）并透传给 handler；
3. 非管理员调用 create_user / delete_user / update_role / update_active 被拒。
"""

from __future__ import annotations

import base64
import json
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from tests.channels.conftest import use_channel

use_channel("api")

import server as srv  # noqa: E402


def _decode_http(resp: dict) -> tuple[int, dict]:
    """从 http.handle 的 ToolExecutionResult 外壳解出 (status, body_json)。"""
    data = resp["data"]
    status = data["status"]
    body = json.loads(base64.b64decode(data["body"]).decode("utf-8"))
    return status, body


# ═══════════════════════════════════════════════════════════
# RED：管理员端点未做 role 校验 → 普通用户即可越权
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_create_user_rejected_for_non_admin() -> None:
    """create_user：非 admin caller → 403。

    RED：当前 _handle_users_domain 不校验 role，普通用户 POST 即创建成功（200 stub）。
    GREEN：_require_admin_role 校验 → 403。
    """
    resp = await srv._handle_users_domain(
        "/ext/channel_api/users",
        "POST",
        "",
        {"username": "x", "password": "y", "role": "admin"},
        _user={"sub": "u1", "role": "user"},
    )
    status, body = _decode_http(resp)
    assert status == 403, f"非管理员创建用户应被拒绝，实际 status={status} body={body}"


@pytest.mark.asyncio
async def test_create_user_allowed_for_admin() -> None:
    """create_user：admin caller → 200（合法行为不破坏）。"""
    resp = await srv._handle_users_domain(
        "/ext/channel_api/users",
        "POST",
        "",
        {"username": "x", "password": "y", "role": "user"},
        _user={"sub": "admin1", "role": "admin"},
    )
    status, _ = _decode_http(resp)
    assert status == 200


@pytest.mark.asyncio
async def test_delete_user_rejected_for_non_admin() -> None:
    """delete_user：非 admin → 403。"""
    resp = await srv._handle_users_domain(
        "/ext/channel_api/users/some-id",
        "DELETE",
        "",
        {},
        _user={"sub": "u1", "role": "user"},
    )
    status, _ = _decode_http(resp)
    assert status == 403


@pytest.mark.asyncio
async def test_update_role_rejected_for_non_admin() -> None:
    """update_user_role：非 admin → 403（防普通用户给自己/他人提权）。"""
    resp = await srv._handle_users_domain(
        "/ext/channel_api/users/some-id/role",
        "PUT",
        "{}",
        {},
        _user={"sub": "u1", "role": "user"},
    )
    status, _ = _decode_http(resp)
    assert status == 403


@pytest.mark.asyncio
async def test_update_active_rejected_for_non_admin() -> None:
    """update_user_active：非 admin → 403（防普通用户封禁/启用他人）。"""
    resp = await srv._handle_users_domain(
        "/ext/channel_api/users/some-id/active",
        "PUT",
        "{}",
        {},
        _user={"sub": "u1", "role": "user"},
    )
    status, _ = _decode_http(resp)
    assert status == 403


# ═══════════════════════════════════════════════════════════
# 单元：_require_admin_role 行为契约
# ═══════════════════════════════════════════════════════════


def test_require_admin_role_raises_for_non_admin() -> None:
    """非 admin → HTTPException(403)。"""
    with pytest.raises(HTTPException) as exc_info:
        srv._require_admin_role({"sub": "u1", "role": "user"})
    assert exc_info.value.status_code == 403


def test_require_admin_role_raises_for_missing_user() -> None:
    """空身份（_user={}，未鉴权透传）→ 403（默认拒绝）。"""
    with pytest.raises(HTTPException) as exc_info:
        srv._require_admin_role({})
    assert exc_info.value.status_code == 403


def test_require_admin_role_passes_for_admin() -> None:
    """admin → 不抛异常（None 返回）。"""
    # 不抛即通过
    assert srv._require_admin_role({"sub": "a", "role": "admin"}) is None


# ═══════════════════════════════════════════════════════════
# 集成：http_handle 从 Authorization 头解析 caller 并透传（端到端越权拦截）
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_http_handle_resolves_caller_and_blocks_non_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    """http_handle(users create_user)：Authorization 头解析出 role=user → 403。

    证明 caller 身份从 headers → http_handle → _handle_users_domain 完整透传，
    且管理员端点据此做垂直越权检查。用 monkeypatch 把 verify_token 替换为
    确定性返回（避免依赖 Redis 撤销检查）。
    """
    import auth  # noqa: PLC0415

    monkeypatch.setattr(
        auth,
        "verify_token",
        MagicMock(return_value={
            "sub": "u1",
            "username": "normal",
            "role": "user",
            "type": "access",
        }),
    )

    resp = await srv.http_handle(
        path="/ext/channel_api/users",
        method="POST",
        raw_body="",
        headers={"Authorization": "Bearer faketoken"},
        query={"username": "x", "password": "y", "role": "admin"},
    )
    status, _ = _decode_http(resp)
    assert status == 403


@pytest.mark.asyncio
async def test_http_handle_resolves_caller_and_allows_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    """http_handle(users create_user)：Authorization 头解析出 role=admin → 200。"""
    import auth  # noqa: PLC0415

    monkeypatch.setattr(
        auth,
        "verify_token",
        MagicMock(return_value={
            "sub": "a1",
            "username": "root",
            "role": "admin",
            "type": "access",
        }),
    )

    resp = await srv.http_handle(
        path="/ext/channel_api/users",
        method="POST",
        raw_body="",
        headers={"Authorization": "Bearer faketoken"},
        query={"username": "x", "password": "y", "role": "user"},
    )
    status, _ = _decode_http(resp)
    assert status == 200


def test_resolve_caller_returns_role_from_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """_resolve_caller 从 Authorization 头解出 sub/username/role。"""
    import auth  # noqa: PLC0415

    monkeypatch.setattr(
        auth,
        "verify_token",
        MagicMock(return_value={
            "sub": "z",
            "username": "zeus",
            "role": "admin",
            "type": "access",
        }),
    )
    caller = srv._resolve_caller({"Authorization": "Bearer abc"})
    assert caller == {"sub": "z", "username": "zeus", "role": "admin"}


def test_resolve_caller_returns_empty_when_no_token() -> None:
    """无 Authorization 头 → {}（兼容既有未鉴权透传，不在此 401）。"""
    assert srv._resolve_caller({}) == {}
    assert srv._resolve_caller({"Authorization": ""}) == {}
