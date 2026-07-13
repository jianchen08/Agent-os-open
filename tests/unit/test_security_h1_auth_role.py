"""H1 安全回归：认证 role 链路 + 注册开关。

漏洞：
1. create_user 创建的用户记录不含 role 字段。
2. login/register/refresh 签发的 token 不带 role。
3. get_current_user 不返回 role。
4. /register 默认开放，任何人可注册。

修复：
1. create_user 默认 "role": "user"。
2. 三处 token_data 带 role。
3. get_current_user 返回 role（兼容旧 token 回退 user）。
4. allow_public_register 默认 False，/register 返回 403。

本测试守护修复：若有人移除 role 字段或放开注册，测试变红。
"""
from __future__ import annotations

import os


class TestCreateUserRoleField:
    """H1: memory_store.create_user 默认写入 role=user。"""

    def test_new_user_gets_user_role(self) -> None:
        from channels.api.memory_store import MemoryStore

        store = MemoryStore()
        # 不传 role 参数，应默认 user
        user = store.create_user(username="test_h1_user", password="whatever123")
        assert user.get("role") == "user", f"create_user 未写 role: {user}"


class TestTokenCarriesRole:
    """H1: token payload 带 role，且 get_current_user 能读回。"""

    def test_token_encodes_role(self) -> None:
        from channels.api.auth import create_access_token

        token = create_access_token({"sub": "u1", "username": "alice", "role": "admin"})
        # decode 验证（不依赖 get_current_user，直接验 payload）
        import jwt  # noqa: PLC0415

        # 用与 auth.py 相同的 secret 解码
        from src.config.settings import get_settings

        payload = jwt.decode(
            token,
            get_settings().jwt_secret_key,
            algorithms=["HS256"],
        )
        assert payload.get("role") == "admin", f"token 未带 role: {payload}"

    def test_get_current_user_returns_role(self) -> None:
        from channels.api.auth import create_access_token, get_current_user

        token = create_access_token({"sub": "u1", "username": "bob", "role": "user"})
        info = get_current_user(token)
        assert info is not None
        assert info.get("role") == "user", f"get_current_user 未返回 role: {info}"

    def test_old_token_without_role_falls_back_to_user(self) -> None:
        """旧 token（修复前签发，无 role）兼容回退为 user，不报错。"""
        from channels.api.auth import create_access_token, get_current_user

        token = create_access_token({"sub": "u1", "username": "legacy"})
        info = get_current_user(token)
        assert info is not None
        assert info.get("role") == "user"


class TestRegisterSwitch:
    """H1/M2: /register 默认关闭开放注册。"""

    def test_register_blocked_by_default(self) -> None:
        """未设 APP_ALLOW_PUBLIC_REGISTER 时，/register 返回 403。"""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from src.config import settings as settings_module

        os.environ.pop("APP_ALLOW_PUBLIC_REGISTER", None)
        settings_module.settings = settings_module.Settings()

        from channels.api.routes_auth import router

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        resp = client.post(
            "/api/v1/auth/register",
            json={"username": "blocked_should_403", "password": "12345678"},
        )
        assert resp.status_code == 403, f"默认应拒绝注册，实际 {resp.status_code}: {resp.text}"
        assert "未开启公开注册" in resp.json().get("detail", "")

    def test_register_allowed_when_explicitly_enabled(self) -> None:
        """显式设 APP_ALLOW_PUBLIC_REGISTER=true 后，/register 放行。"""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from src.config import settings as settings_module

        os.environ["APP_ALLOW_PUBLIC_REGISTER"] = "true"
        settings_module.settings = settings_module.Settings()

        from channels.api.routes_auth import router

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        resp = client.post(
            "/api/v1/auth/register",
            json={"username": "open_should_200", "password": "12345678"},
        )
        # 清理环境变量（即便 assert 失败也要清理）
        os.environ.pop("APP_ALLOW_PUBLIC_REGISTER", None)
        settings_module.settings = settings_module.Settings()

        assert resp.status_code == 200, f"开启后应放行，实际 {resp.status_code}: {resp.text}"
