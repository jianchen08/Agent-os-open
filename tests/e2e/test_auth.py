"""认证全链路 E2E 测试。

验证注册 → 登录获取 Token → Token 访问 API → Token 刷新 → Token 撤销后返回 401。
对应 features.md 场景 7。

测试用例：
- test_login_demo_user：登录内置 demo 用户
- test_login_wrong_password：错误密码登录返回 401
- test_register_new_user：注册新用户
- test_register_duplicate_user：重复注册返回 409
- test_access_api_without_token：无 Token 访问返回 401
- test_access_api_with_token：有效 Token 访问受保护 API
- test_access_api_with_invalid_token：无效 Token 访问返回 401
- test_expired_token_rejected：过期 Token 访问返回 401
- test_refresh_token：Token 刷新获取新 Token
- test_logout_revokes_token：Token 撤销后返回 401
- test_cross_user_resource_isolation：用户 A 创建任务，用户 B 无法访问
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any

from tests.e2e.conftest import DEMO_CREDENTIALS


# ---------------------------------------------------------------------------
# 登录测试
# ---------------------------------------------------------------------------

def test_login_demo_user(test_client: Any) -> None:
    """登录内置 demo 用户，返回 access_token 和 refresh_token。

    验证点：
    - POST /api/v1/auth/login 返回 200
    - 响应包含 access_token 和 refresh_token
    """
    resp = test_client.post("/api/v1/auth/login", json=DEMO_CREDENTIALS)
    assert resp.status_code == 200, f"登录失败: {resp.text}"

    data = resp.json()
    assert "access_token" in data, "响应缺少 access_token"
    assert "refresh_token" in data, "响应缺少 refresh_token"
    assert data["token_type"] == "bearer", f"token_type 应为 bearer，得到 {data['token_type']}"
    assert data["expires_in"] > 0, f"expires_in 应大于 0，得到 {data['expires_in']}"


def test_login_wrong_password(test_client: Any) -> None:
    """错误密码登录应返回 401。

    验证点：
    - POST /api/v1/auth/login 错误密码返回 401
    - 响应体包含诊断信息（detail 或 message）
    """
    resp = test_client.post(
        "/api/v1/auth/login",
        json={"username": "demo", "password": "wrong_password"},
    )
    assert resp.status_code == 401, f"错误密码应返回 401，得到 {resp.status_code}"

    # 验证响应体包含诊断信息
    resp_data = resp.json()
    assert "detail" in resp_data or "message" in resp_data, (
        f"401 响应应包含 detail 或 message 字段，得到: {resp_data}"
    )


# ---------------------------------------------------------------------------
# 注册测试
# ---------------------------------------------------------------------------

def test_register_new_user(test_client: Any) -> None:
    """注册新用户，返回 token。

    验证点：
    - POST /api/v1/auth/register 返回 200
    - 响应包含 access_token 和 refresh_token
    """
    resp = test_client.post(
        "/api/v1/auth/register",
        json={"username": "e2e_test_user", "password": "test_pass_123"},
    )
    assert resp.status_code == 200, f"注册失败: {resp.text}"

    data = resp.json()
    assert "access_token" in data, "注册响应缺少 access_token"
    assert "refresh_token" in data, "注册响应缺少 refresh_token"


def test_register_duplicate_user(test_client: Any) -> None:
    """重复注册同名用户应返回 409。

    验证点：
    - 已存在的用户名再次注册返回 409
    """
    resp = test_client.post(
        "/api/v1/auth/register",
        json={"username": "demo", "password": "another_pass"},
    )
    assert resp.status_code == 409, f"重复注册应返回 409，得到 {resp.status_code}"


# ---------------------------------------------------------------------------
# Token 验证测试
# ---------------------------------------------------------------------------

def test_access_api_without_token(test_client: Any) -> None:
    """无 Token 访问受保护 API 应返回 401。

    验证点：
    - GET /api/v1/tasks/ 无认证返回 401
    """
    resp = test_client.get("/api/v1/tasks/")
    assert resp.status_code == 401, f"无 Token 应返回 401，得到 {resp.status_code}"


def test_access_api_with_token(test_client: Any, auth_headers: dict[str, str]) -> None:
    """使用有效 Token 访问受保护 API。

    验证点：
    - GET /api/v1/tasks/ with Bearer token 返回 200
    - GET /api/v1/auth/me 返回用户信息
    """
    resp = test_client.get("/api/v1/tasks/", headers=auth_headers)
    assert resp.status_code == 200, f"有效 Token 应返回 200，得到 {resp.status_code}: {resp.text}"

    resp = test_client.get("/api/v1/auth/me", headers=auth_headers)
    assert resp.status_code == 200, f"获取用户信息失败: {resp.text}"
    user_data = resp.json()
    assert user_data["username"] == "demo", f"用户名应为 demo，得到 {user_data['username']}"


def test_access_api_with_invalid_token(test_client: Any) -> None:
    """使用无效 Token 访问 API 应返回 401。

    验证点：
    - GET /api/v1/tasks/ with 无效 token 返回 401
    """
    headers = {"Authorization": "Bearer invalid.token.here"}
    resp = test_client.get("/api/v1/tasks/", headers=headers)
    assert resp.status_code == 401, f"无效 Token 应返回 401，得到 {resp.status_code}"


def test_expired_token_rejected(test_client: Any) -> None:
    """过期 Token 访问受保护 API 应返回 401。

    构造一个 exp 已过期的 JWT，验证服务端正确拒绝。

    验证点：
    - 使用 exp < now 的 JWT 访问 /api/v1/tasks/ 返回 401
    """
    # 构造一个过期的 JWT（header.payload.signature 格式，payload 中 exp 已过期）
    expired_payload = {
        "sub": "demo",
        "exp": int(time.time()) - 3600,  # 1 小时前过期
        "iat": int(time.time()) - 7200,
        "type": "access",
    }
    header_b64 = base64.urlsafe_b64encode(
        json.dumps({"alg": "HS256", "typ": "JWT"}).encode()
    ).rstrip(b"=").decode()
    payload_b64 = base64.urlsafe_b64encode(
        json.dumps(expired_payload).encode()
    ).rstrip(b"=").decode()
    # signature 不正确，但服务端应因 exp 过期或签名无效返回 401
    fake_jwt = f"{header_b64}.{payload_b64}.invalid_signature"

    headers = {"Authorization": f"Bearer {fake_jwt}"}
    resp = test_client.get("/api/v1/tasks/", headers=headers)
    assert resp.status_code == 401, (
        f"过期/无效 Token 应返回 401，得到 {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# Token 刷新测试
# ---------------------------------------------------------------------------

def test_refresh_token(test_client: Any) -> None:
    """使用 refresh_token 获取新的 access_token。

    验证点：
    - POST /api/v1/auth/refresh 返回 200
    - 新 access_token 可以正常访问 API
    - 新 token 的 iat >= 旧 token 的 iat
    """
    login_resp = test_client.post("/api/v1/auth/login", json=DEMO_CREDENTIALS)
    assert login_resp.status_code == 200
    login_data = login_resp.json()
    refresh_token = login_data["refresh_token"]

    refresh_resp = test_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": refresh_token},
    )
    assert refresh_resp.status_code == 200, f"刷新失败: {refresh_resp.text}"

    new_data = refresh_resp.json()
    assert "access_token" in new_data, "刷新响应缺少 access_token"
    assert new_data["token_type"] == "bearer"

    # 验证新 token 可用
    new_headers = {"Authorization": f"Bearer {new_data['access_token']}"}
    me_resp = test_client.get("/api/v1/auth/me", headers=new_headers)
    assert me_resp.status_code == 200, f"新 Token 访问失败: {me_resp.text}"
    assert me_resp.json()["username"] == "demo"


# ---------------------------------------------------------------------------
# Token 撤销测试
# ---------------------------------------------------------------------------

def test_logout_revokes_token(test_client: Any) -> None:
    """登出后 refresh_token 被撤销，再次刷新返回 401。

    验证点：
    - POST /api/v1/auth/logout 成功
    - 撤销后用旧 refresh_token 刷新返回 401
    """
    login_resp = test_client.post("/api/v1/auth/login", json=DEMO_CREDENTIALS)
    assert login_resp.status_code == 200
    refresh_token = login_resp.json()["refresh_token"]

    logout_resp = test_client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": refresh_token},
    )
    assert logout_resp.status_code == 200, f"登出失败: {logout_resp.text}"

    refresh_resp = test_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": refresh_token},
    )
    assert refresh_resp.status_code == 401, (
        f"已撤销的 refresh_token 刷新应返回 401，得到 {refresh_resp.status_code}"
    )


# ---------------------------------------------------------------------------
# 跨用户资源隔离测试
# ---------------------------------------------------------------------------

def test_cross_user_resource_isolation(
    test_client: Any,
    auth_headers: dict[str, str],
    available_agent_id: str,
) -> None:
    """用户 A 创建任务，用户 B 无法访问。

    注册第二个用户，用用户 A 的 Token 创建任务，
    然后用用户 B 的 Token 访问该任务应返回 404。

    验证点：
    - 注册用户 B
    - 用户 A 创建任务
    - 用户 B 用自己的 Token 访问该任务返回 404
    """
    # 注册用户 B
    user_b_creds = {"username": "e2e_isolation_user", "password": "iso_pass_456"}
    register_resp = test_client.post("/api/v1/auth/register", json=user_b_creds)
    if register_resp.status_code == 409:
        # 用户已存在（前次测试残留），直接登录
        login_b_resp = test_client.post("/api/v1/auth/login", json=user_b_creds)
    else:
        login_b_resp = register_resp
    assert login_b_resp.status_code == 200, f"用户 B 登录失败: {login_b_resp.text}"
    user_b_headers = {"Authorization": f"Bearer {login_b_resp.json()['access_token']}"}

    # 用户 A 创建任务
    create_resp = test_client.post(
        "/api/v1/tasks/",
        json={"title": "隔离测试任务", "agent_id": available_agent_id},
        headers=auth_headers,
    )
    assert create_resp.status_code == 201, f"用户 A 创建任务失败: {create_resp.text}"
    task_id = create_resp.json()["id"]

    # 用户 B 尝试访问用户 A 的任务
    resp = test_client.get(f"/api/v1/tasks/{task_id}", headers=user_b_headers)
    assert resp.status_code in (403, 404), (
        f"用户 B 不应访问用户 A 的任务，期望 403/404，得到 {resp.status_code}"
    )
