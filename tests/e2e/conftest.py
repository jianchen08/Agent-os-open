"""E2E 测试专用 fixture。

提供 FastAPI app 实例、TestClient、认证辅助等 fixture，
供 5 个核心 E2E 测试文件复用。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 环境变量 — 必须在任何项目 import 之前设置，避免 litellm 初始化时网络调用
# ---------------------------------------------------------------------------

import os

# 禁止 litellm 从 GitHub 拉取远程 model cost map（网络超时约 10s）
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

from typing import Any

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# 常量 — 测试用凭证（避免散点硬编码）
# ---------------------------------------------------------------------------

DEMO_CREDENTIALS = {"username": "demo", "password": "demo12345"}


# ---------------------------------------------------------------------------
# _reset_rate_limiter — 每个测试前重置限流器（autouse）
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> Any:
    """每个测试前清空限流器状态，防止测试间相互干扰。

    tiered_rate_limiter 是模块级单例，AUTH 类别限制 10 次/60 秒。
    87 个 E2E 测试大量调用登录接口，不重置会触发 429。
    """
    from channels.api.rate_limiter import tiered_rate_limiter

    tiered_rate_limiter._hits.clear()
    yield
    tiered_rate_limiter._hits.clear()


# ---------------------------------------------------------------------------
# test_app — 注入 FastAPI app 实例
# ---------------------------------------------------------------------------

def _ensure_demo_user() -> None:
    """确保 store 中存在 demo 用户（测试种子数据）。

    如果 demo 用户不存在则创建，已存在则跳过。
    """
    from channels.api.memory_store import store

    if "demo" not in store.users:
        store.create_user(
            username="demo",
            password=DEMO_CREDENTIALS["password"],
            email="demo@example.com",
        )


@pytest.fixture(scope="session")
def test_app() -> Any:
    """创建配置好的 FastAPI app 实例（仅 REST API，不含 WebSocket）。

    session 级别复用：FastAPI app 初始化加载 litellm/router/tokenizers/插件链等
    重资源，function 级别重建会导致内存累积和 OOM 段错误（容器仅 1.3GB RAM）。
    app 本身无状态，TestClient 保持 function 级别即可实现测试隔离。

    Returns:
        FastAPI 应用实例
    """
    from channels.api.app import create_app

    app = create_app()
    _ensure_demo_user()
    return app


@pytest.fixture(scope="session")
def test_app_with_ws() -> Any:
    """创建合并了 WebSocket 功能的 FastAPI app 实例。

    session 级别复用（同 test_app 理由）。
    跳过管道引擎初始化（litellm/tokenizers 加载约 7s），
    WS 测试只需连接/心跳协议层，不需要真实 AI 管道。

    Returns:
        FastAPI 应用实例（含 /ws 和 /ws/chat 路由）
    """
    import channels.websocket.app_factory as af

    # 用轻量 Mock 替换 _init_pipeline_context，跳过 litellm/router 初始化
    class _MockCtx:
        available = False
        pipeline_config = None
        plugin_registry = None
        services = None
        _engines = {}

        def get_or_create_engine(self, _pipeline_id: str) -> None:
            return None

    _original_init = getattr(af, "_init_pipeline_context", None)
    af._init_pipeline_context = lambda: _MockCtx()  # type: ignore[assignment]

    from channels.websocket.app_factory import create_combined_app

    try:
        app = create_combined_app()
    finally:
        if _original_init is not None:
            af._init_pipeline_context = _original_init  # type: ignore[assignment]

    _ensure_demo_user()
    return app


# ---------------------------------------------------------------------------
# test_client — FastAPI TestClient
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def test_client(test_app: Any) -> TestClient:
    """提供 FastAPI TestClient，用于 REST API 请求（session 级别复用）。

    session 级别复用 TestClient 避免 auth_token 重复 bcrypt 登录（约180ms/次）。
    测试间状态隔离通过 UUID 唯一数据和 _reset_rate_limiter autouse fixture 保证。

    Args:
        test_app: FastAPI 应用实例

    Returns:
        TestClient 实例
    """
    return TestClient(test_app)


@pytest.fixture(scope="session")
def ws_test_client(test_app_with_ws: Any) -> TestClient:
    """提供包含 WebSocket 路由的 FastAPI TestClient（session 级别复用）。

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

@pytest.fixture(scope="session")
def auth_token(test_client: TestClient) -> str:
    """登录 demo 用户并返回 access_token（session 级别复用）。

    通过 /api/v1/auth/login 登录内置 demo 用户。
    session 级别复用避免每次测试重复 bcrypt 哈希（约180ms/次）。

    Returns:
        access_token 字符串
    """
    resp = test_client.post("/api/v1/auth/login", json=DEMO_CREDENTIALS)
    assert resp.status_code == 200, f"登录失败: {resp.text}"
    return resp.json()["access_token"]


@pytest.fixture(scope="session")
def auth_headers(auth_token: str) -> dict[str, str]:
    """提供 Authorization 请求头（session 级别复用）。

    Returns:
        包含 Bearer token 的请求头字典
    """
    return {"Authorization": f"Bearer {auth_token}"}


# ---------------------------------------------------------------------------
# available_agent_id — 获取可用 Agent ID（跨测试文件共享）
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
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
