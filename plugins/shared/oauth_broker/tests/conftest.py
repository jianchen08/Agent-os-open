"""oauth_broker 测试 fixtures：假授权服务器 + 临时用户根 + 回调入口 + 模块装载。

夹具一律真依赖：真 HTTP（``FakeAuthServer`` 127.0.0.1 随机端口）、真文件系统
（``tmp_path`` 用户根）、真 PKCE。mock 只出现在浏览器打开面（``open_browser``
替身——无显示环境无法真开浏览器）与外部观测（日志捕获）。

装载器与源目录晋升在 ``oauth_fixtures``（独名模块，跨插件 conftest 同名不互扰）。
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from typing import Any

import pytest
from fake_auth_server import FakeAuthServer
from oauth_fixtures import load_server_module  # noqa: F401 —— 测试经 ``oauth_fixtures`` 直接使用


@pytest.fixture
def fake_as() -> Any:
    """真实 HTTP 假授权服务器（每用例独立实例与端口）。"""
    server = FakeAuthServer().start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def auth_server_factory() -> Any:
    """带行为开关的假授权服务器工厂（同用例可起多台，退出时统一停）。"""
    servers: list[FakeAuthServer] = []

    def make(**flags: Any) -> FakeAuthServer:
        server = FakeAuthServer()
        for name, value in flags.items():
            setattr(server, name, value)
        server.start()
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.stop()


@pytest.fixture
def user_root_env(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """用户根摘出到临时目录（插件侧经 ``diagnostics_settings.user_root()`` 解析）。"""
    root = tmp_path / "user-root"
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(root))
    return root


@pytest.fixture
def env_path(user_root_env) -> Any:
    return user_root_env / ".env"


@pytest.fixture
def callback_endpoint() -> Callable[..., Any]:
    """插件回调入口：走 ``server.py`` 注册的真实 ``http.handle``（async）。

    返回 ``async (query) -> (status, body_text, envelope)``——envelope 为
    handler 原始返回（成功信封 + HttpHandleResponse），供断言 body 编码契约。
    """
    server_module = load_server_module()

    async def handle(query: dict[str, str]) -> tuple[int, str, dict[str, Any]]:
        envelope = await server_module.http_handle(
            path="/ext/oauth_broker/callback",
            method="GET",
            plugin_id="oauth_broker",
            raw_body="",
            headers={},
            query=query,
        )
        data = envelope["data"]
        body = base64.b64decode(data["body"]).decode("utf-8")
        return int(data["status"]), body, envelope

    return handle

@pytest.fixture(autouse=True)
def _isolate_user_root_everywhere(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """全目录自动隔离用户根：oauth 测试绝不读写真实用户空间 .env。

    2026-11-04 实证：未声明 ``user_root_env`` 的用例经 SDK 缺省根落到装机
    ``%APPDATA%\agentos``，跨轮累积 347 条 ``MCP_OAUTH_127_0_0_1_<port>_*``
    假令牌；新用例的假服务器万一复用同一端口，``authorize`` 命中残留条目走
    ``existing`` 短路 → 两条用例 ``DID NOT RAISE``（单跑不复现、全量必现）。
    自动隔离后读写成对落 tmp，真实用户资产零触碰。
    """
    root = tmp_path / "auto-user-root"
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(root))
    return root
