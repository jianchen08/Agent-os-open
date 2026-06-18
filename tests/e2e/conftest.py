"""E2E 测试专用 fixture。

提供 FastAPI app 实例、TestClient、认证辅助等 fixture，
供 5 个核心 E2E 测试文件复用。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# 常量 — 测试用凭证（避免散点硬编码）
# ---------------------------------------------------------------------------

DEMO_CREDENTIALS = {"username": "demo", "password": "demo12345"}


# ---------------------------------------------------------------------------
# test_app — 注入 FastAPI app 实例
# ---------------------------------------------------------------------------

@pytest.fixture
def test_app() -> Any:
    """创建配置好的 FastAPI app 实例（仅 REST API，不含 WebSocket）。

    用于纯 HTTP API 级别的 E2E 测试（认证、配置、任务 CRUD）。

    Returns:
        FastAPI 应用实例
    """
    from channels.api.app import create_app

    return create_app()


@pytest.fixture
def test_app_with_ws() -> Any:
    """创建合并了 WebSocket 功能的 FastAPI app 实例。

    用于需要 WebSocket 端点的 E2E 测试（对话流程等）。

    Returns:
        FastAPI 应用实例（含 /ws 和 /ws/chat 路由）
    """
    from channels.websocket.app_factory import create_combined_app

    return create_combined_app()


# ---------------------------------------------------------------------------
# test_client — FastAPI TestClient
# ---------------------------------------------------------------------------

@pytest.fixture
def test_client(test_app: Any) -> TestClient:
    """提供 FastAPI TestClient，用于 REST API 请求。

    Args:
        test_app: FastAPI 应用实例

    Returns:
        TestClient 实例
    """
    return TestClient(test_app)


@pytest.fixture
def ws_test_client(test_app_with_ws: Any) -> TestClient:
    """提供包含 WebSocket 路由的 FastAPI TestClient。

    基于 create_combined_app() 创建，包含 /ws 和 /ws/chat 路由。
    用于 WebSocket 级别的 E2E 测试（对话流程等）。

    Args:
        test_app_with_ws: 含 WebSocket 路由的 FastAPI 应用实例

    Returns:
        TestClient 实例
    """
    return TestClient(test_app_with_ws)


# ---------------------------------------------------------------------------
# auth_token — 获取认证 token
# ---------------------------------------------------------------------------

@pytest.fixture
def auth_token(test_client: TestClient) -> str:
    """登录 demo 用户并返回 access_token。

    通过 /api/v1/auth/login 登录内置 demo 用户。

    Returns:
        access_token 字符串
    """
    resp = test_client.post("/api/v1/auth/login", json=DEMO_CREDENTIALS)
    assert resp.status_code == 200, f"登录失败: {resp.text}"
    return resp.json()["access_token"]


@pytest.fixture
def auth_headers(auth_token: str) -> dict[str, str]:
    """提供 Authorization 请求头。

    Returns:
        包含 Bearer token 的请求头字典
    """
    return {"Authorization": f"Bearer {auth_token}"}


# ---------------------------------------------------------------------------
# available_agent_id — 获取可用 Agent ID（跨测试文件共享）
# ---------------------------------------------------------------------------

@pytest.fixture
def available_agent_id(
    test_client: TestClient,
    auth_headers: dict[str, str],
) -> str:
    """获取系统中可用的 Agent ID。

    通过 /api/v1/agents/ 查询列表，取第一个 Agent 的 ID。
    如果查询失败或无可用 Agent，直接 pytest.fail 而非静默回退。

    Returns:
        Agent ID 字符串
    """
    resp = test_client.get("/api/v1/agents/", headers=auth_headers)
    assert resp.status_code == 200, (
        f"查询 Agent 列表失败: {resp.status_code} {resp.text}"
    )

    data = resp.json()
    items = data.get("items", [])
    if not items:
        pytest.fail("系统中无可用 Agent，无法进行任务创建测试")

    first_item = items[0]
    agent_id = first_item.get("config_id")
    if not agent_id:
        agent_id = first_item.get("id")
    if not agent_id:
        pytest.fail(f"Agent 列表项缺少 config_id 和 id 字段: {first_item}")

    return agent_id
