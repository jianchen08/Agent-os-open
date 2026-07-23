"""
E2E 测试：Auth 登录全链路 + Chat 管道 + 插件工具加载（HTTP API 验证）

验证 0.2 内核的 Auth 系统、插件系统和 Chat 管道引擎。
代码参考: kernel/crates/api/src/auth.rs, kernel/crates/api/src/server.rs,
          kernel/crates/api/src/routes.rs

测试项:
  用户旅程1 — Auth 登录全链路验证
    1.1 POST /api/v1/auth/login 正确凭证 → 200, 返回 access_token/refresh_token/token_type/expires_in
    1.2 GET  /api/v1/auth/me 带 Bearer token → 200, username=admin/role=admin/is_active=true/email
    1.3 POST /api/v1/auth/refresh 用 refresh_token → 200, 返回新 access_token
    1.4 POST /api/v1/auth/login 错误密码 → 400
    1.5 GET  /api/v1/auth/me 无 token → 401

  用户旅程2 — 插件系统工具加载验证
    2.1 GET /api/v1/tools → 200, 非空数组（期望 44 个工具）
    2.2 每个工具含 name/description/plugin_id/category/source 字段
    2.3 GET /api/v1/schema → 含 pipelines 和 tools 字段

  用户旅程3 — Chat 管道引擎验证（非 echo 模式）
    3.1 POST /api/v1/chat → content 以 '[pipeline:' 开头（证明使用管道引擎）
    3.2 content 不以 'Response to:' 开头（echo 模式特征）
    3.3 响应含 type='message', session_id, timestamp 字段
"""
import json
import urllib.request
import urllib.error

import pytest

from e2e_helpers import KERNEL_URL, http_get, http_post_json


# ============================================================
# 辅助函数：带 Bearer Token 的 GET 请求
# ============================================================
def http_get_with_auth(url, token=None, timeout=5):
    """发起带可选 Bearer Token 的 GET 请求，返回 (status_code, body_dict_or_str)。

    使用 urllib 标准库，无第三方依赖。
    """
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            status = resp.status
            try:
                body_json = json.loads(body)
            except (json.JSONDecodeError, ValueError):
                body_json = body
            return status, body_json, {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8") if e.fp else ""
        try:
            body_json = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            body_json = body
        return e.code, body_json, {}


# ============================================================
# 用户旅程1：Auth 登录全链路验证
# 代码参考: kernel/crates/api/src/auth.rs, kernel/crates/api/src/server.rs
# ============================================================
class TestAuthLoginFullChain:
    """Auth 登录全链路验证：登录 → 获取用户信息 → 刷新令牌 → 错误处理。"""

    def test_login_success_returns_access_token(self):
        """1.1a 正确凭证登录应返回 access_token 字符串。"""
        payload = {"username": "admin", "password": "admin12345"}
        status, body, _ = http_post_json(f"{KERNEL_URL}/api/v1/auth/login", payload)
        assert status == 200, f"期望 200，实际 {status}"
        assert isinstance(body, dict), f"响应应为 dict，实际 {type(body)}"
        assert "access_token" in body, "响应缺少 access_token 字段"
        assert isinstance(body["access_token"], str), "access_token 应为字符串"
        assert len(body["access_token"]) > 0, "access_token 不应为空字符串"

    def test_login_success_returns_refresh_token(self):
        """1.1b 正确凭证登录应返回 refresh_token 字符串。"""
        payload = {"username": "admin", "password": "admin12345"}
        status, body, _ = http_post_json(f"{KERNEL_URL}/api/v1/auth/login", payload)
        assert status == 200, f"期望 200，实际 {status}"
        assert "refresh_token" in body, "响应缺少 refresh_token 字段"
        assert isinstance(body["refresh_token"], str), "refresh_token 应为字符串"
        assert len(body["refresh_token"]) > 0, "refresh_token 不应为空字符串"

    def test_login_success_token_type_bearer(self):
        """1.1c 正确凭证登录应返回 token_type='bearer'。"""
        payload = {"username": "admin", "password": "admin12345"}
        status, body, _ = http_post_json(f"{KERNEL_URL}/api/v1/auth/login", payload)
        assert status == 200
        assert body.get("token_type") == "bearer", \
            f"token_type 期望 'bearer'，实际 '{body.get('token_type')}'"

    def test_login_success_expires_in_1800(self):
        """1.1d 正确凭证登录应返回 expires_in=1800。"""
        payload = {"username": "admin", "password": "admin12345"}
        status, body, _ = http_post_json(f"{KERNEL_URL}/api/v1/auth/login", payload)
        assert status == 200
        assert body.get("expires_in") == 1800, \
            f"expires_in 期望 1800，实际 {body.get('expires_in')}"

    def test_me_with_token_returns_admin_user(self):
        """1.2a 用 Bearer token 访问 /auth/me 应返回 username='admin'。"""
        # 先登录获取 token
        login_payload = {"username": "admin", "password": "admin12345"}
        _, login_body, _ = http_post_json(f"{KERNEL_URL}/api/v1/auth/login", login_payload)
        token = login_body["access_token"]
        # 用 token 访问 /auth/me
        status, body, _ = http_get_with_auth(f"{KERNEL_URL}/api/v1/auth/me", token=token)
        assert status == 200, f"期望 200，实际 {status}"
        assert isinstance(body, dict), f"响应应为 dict，实际 {type(body)}"
        assert body.get("username") == "admin", \
            f"username 期望 'admin'，实际 '{body.get('username')}'"

    def test_me_with_token_returns_admin_role(self):
        """1.2b 用 Bearer token 访问 /auth/me 应返回 role='admin'。"""
        login_payload = {"username": "admin", "password": "admin12345"}
        _, login_body, _ = http_post_json(f"{KERNEL_URL}/api/v1/auth/login", login_payload)
        token = login_body["access_token"]
        status, body, _ = http_get_with_auth(f"{KERNEL_URL}/api/v1/auth/me", token=token)
        assert status == 200
        assert body.get("role") == "admin", \
            f"role 期望 'admin'，实际 '{body.get('role')}'"

    def test_me_with_token_returns_is_active_true(self):
        """1.2c 用 Bearer token 访问 /auth/me 应返回 is_active=true。"""
        login_payload = {"username": "admin", "password": "admin12345"}
        _, login_body, _ = http_post_json(f"{KERNEL_URL}/api/v1/auth/login", login_payload)
        token = login_body["access_token"]
        status, body, _ = http_get_with_auth(f"{KERNEL_URL}/api/v1/auth/me", token=token)
        assert status == 200
        assert body.get("is_active") is True, \
            f"is_active 期望 True，实际 {body.get('is_active')}"

    def test_me_with_token_returns_email(self):
        """1.2d 用 Bearer token 访问 /auth/me 应返回 email='admin@agentos.dev'。"""
        login_payload = {"username": "admin", "password": "admin12345"}
        _, login_body, _ = http_post_json(f"{KERNEL_URL}/api/v1/auth/login", login_payload)
        token = login_body["access_token"]
        status, body, _ = http_get_with_auth(f"{KERNEL_URL}/api/v1/auth/me", token=token)
        assert status == 200
        assert body.get("email") == "admin@agentos.dev", \
            f"email 期望 'admin@agentos.dev'，实际 '{body.get('email')}'"

    def test_refresh_returns_new_access_token(self):
        """1.3 用 refresh_token 刷新应返回新的 access_token。"""
        # 先登录获取 refresh_token
        login_payload = {"username": "admin", "password": "admin12345"}
        _, login_body, _ = http_post_json(f"{KERNEL_URL}/api/v1/auth/login", login_payload)
        refresh_token = login_body["refresh_token"]
        # 刷新令牌
        refresh_payload = {"refresh_token": refresh_token}
        status, body, _ = http_post_json(
            f"{KERNEL_URL}/api/v1/auth/refresh", refresh_payload
        )
        assert status == 200, f"期望 200，实际 {status}"
        assert "access_token" in body, "刷新响应缺少 access_token 字段"
        assert isinstance(body["access_token"], str), "新 access_token 应为字符串"
        assert len(body["access_token"]) > 0, "新 access_token 不应为空"

    def test_refresh_new_token_differs_from_old(self):
        """1.3b 刷新后的新 access_token 应与旧的不同。

        注意：token 过期时间以秒为粒度（exp = now + TTL），
        同一秒内 login 和 refresh 会生成相同 token。
        因此等待 1.5 秒后再 refresh，确保时间戳不同。
        """
        import time
        login_payload = {"username": "admin", "password": "admin12345"}
        _, login_body, _ = http_post_json(f"{KERNEL_URL}/api/v1/auth/login", login_payload)
        old_access = login_body["access_token"]
        refresh_token = login_body["refresh_token"]
        # 等待超过 1 秒确保 token 过期时间戳不同
        time.sleep(1.5)
        refresh_payload = {"refresh_token": refresh_token}
        _, refresh_body, _ = http_post_json(
            f"{KERNEL_URL}/api/v1/auth/refresh", refresh_payload
        )
        assert refresh_body["access_token"] != old_access, \
            "刷新后的 access_token 应与旧令牌不同"

    def test_login_wrong_password_returns_400(self):
        """1.4 错误密码登录应返回 400。"""
        payload = {"username": "admin", "password": "wrongpassword"}
        status, body, _ = http_post_json(f"{KERNEL_URL}/api/v1/auth/login", payload)
        assert status == 400, f"期望 400，实际 {status}"

    def test_me_without_token_returns_401(self):
        """1.5 无 token 访问 /auth/me 应返回 401。"""
        status, body, _ = http_get_with_auth(f"{KERNEL_URL}/api/v1/auth/me", token=None)
        assert status == 401, f"期望 401，实际 {status}"


# ============================================================
# 用户旅程2：插件系统工具加载验证
# 代码参考: kernel/crates/api/src/routes.rs
# ============================================================
class TestPluginToolLoading:
    """插件系统工具加载验证：工具列表非空、字段完整、Schema 聚合。"""

    def test_tools_returns_non_empty_array(self):
        """2.1a GET /api/v1/tools 应返回非空数组。"""
        status, body, _ = http_get(f"{KERNEL_URL}/api/v1/tools")
        assert status == 200, f"期望 200，实际 {status}"
        assert isinstance(body, list), f"响应应为 list，实际 {type(body)}"
        assert len(body) > 0, "工具列表不应为空"

    def test_tools_count_expected_44(self):
        """2.1b GET /api/v1/tools 应返回 44 个工具。"""
        status, body, _ = http_get(f"{KERNEL_URL}/api/v1/tools")
        assert status == 200
        assert len(body) == 44, f"期望 44 个工具，实际 {len(body)} 个"

    def test_each_tool_has_name_field(self):
        """2.2a 每个工具应包含 name 字段。"""
        status, body, _ = http_get(f"{KERNEL_URL}/api/v1/tools")
        assert status == 200
        for i, tool in enumerate(body):
            assert "name" in tool, f"第 {i} 个工具缺少 name 字段"
            assert isinstance(tool["name"], str), f"第 {i} 个工具 name 应为字符串"

    def test_each_tool_has_description_field(self):
        """2.2b 每个工具应包含 description 字段。"""
        status, body, _ = http_get(f"{KERNEL_URL}/api/v1/tools")
        assert status == 200
        for i, tool in enumerate(body):
            assert "description" in tool, f"第 {i} 个工具缺少 description 字段"

    def test_each_tool_has_plugin_id_field(self):
        """2.2c 每个工具应包含 plugin_id 字段。"""
        status, body, _ = http_get(f"{KERNEL_URL}/api/v1/tools")
        assert status == 200
        for i, tool in enumerate(body):
            assert "plugin_id" in tool, f"第 {i} 个工具缺少 plugin_id 字段"

    def test_each_tool_has_category_field(self):
        """2.2d 每个工具应包含 category 字段。"""
        status, body, _ = http_get(f"{KERNEL_URL}/api/v1/tools")
        assert status == 200
        for i, tool in enumerate(body):
            assert "category" in tool, f"第 {i} 个工具缺少 category 字段"

    def test_each_tool_has_source_field(self):
        """2.2e 每个工具应包含 source 字段。"""
        status, body, _ = http_get(f"{KERNEL_URL}/api/v1/tools")
        assert status == 200
        for i, tool in enumerate(body):
            assert "source" in tool, f"第 {i} 个工具缺少 source 字段"

    def test_schema_has_pipelines_field(self):
        """2.3a GET /api/v1/schema 应包含 pipelines 字段。"""
        status, body, _ = http_get(f"{KERNEL_URL}/api/v1/schema")
        assert status == 200
        assert isinstance(body, dict), "schema 响应应为 dict"
        assert "pipelines" in body, "schema 响应缺少 pipelines 字段"

    def test_schema_has_tools_field(self):
        """2.3b GET /api/v1/schema 应包含 tools 字段。"""
        status, body, _ = http_get(f"{KERNEL_URL}/api/v1/schema")
        assert status == 200
        assert "tools" in body, "schema 响应缺少 tools 字段"


# ============================================================
# 用户旅程3：Chat 管道引擎验证（非 echo 模式）
# 代码参考: kernel/crates/api/src/server.rs
# ============================================================
class TestChatPipelineEngine:
    """Chat 管道引擎验证：证明响应来自管道引擎而非 echo 模式。"""

    def test_chat_content_starts_with_pipeline_prefix(self):
        """3.1 chat 响应 content 应以 '[pipeline:' 开头（证明使用管道引擎）。"""
        payload = {"message": "hello", "session_id": "test"}
        status, body, _ = http_post_json(f"{KERNEL_URL}/api/v1/chat", payload, timeout=30)
        assert status == 200, f"期望 200，实际 {status}"
        assert isinstance(body, dict), "响应应为 dict"
        assert "content" in body, "响应缺少 content 字段"
        content = body["content"]
        assert content.startswith("[pipeline:"), \
            f"content 应以 '[pipeline:' 开头（证明使用管道引擎），实际: '{content[:80]}'"

    def test_chat_content_not_echo_mode(self):
        """3.2 chat 响应 content 不应以 'Response to:' 开头（echo 模式特征）。"""
        payload = {"message": "hello", "session_id": "test"}
        status, body, _ = http_post_json(f"{KERNEL_URL}/api/v1/chat", payload, timeout=30)
        assert status == 200
        content = body["content"]
        assert not content.startswith("Response to:"), \
            f"content 不应以 'Response to:' 开头（echo 模式特征），实际: '{content[:80]}'"

    def test_chat_response_type_is_message(self):
        """3.3a chat 响应应包含 type='message'。"""
        payload = {"message": "hello", "session_id": "test"}
        status, body, _ = http_post_json(f"{KERNEL_URL}/api/v1/chat", payload, timeout=30)
        assert status == 200
        assert body.get("type") == "message", \
            f"type 期望 'message'，实际 '{body.get('type')}'"

    def test_chat_response_has_session_id(self):
        """3.3b chat 响应应包含 session_id='test'。"""
        payload = {"message": "hello", "session_id": "test"}
        status, body, _ = http_post_json(f"{KERNEL_URL}/api/v1/chat", payload, timeout=30)
        assert status == 200
        assert body.get("session_id") == "test", \
            f"session_id 期望 'test'，实际 '{body.get('session_id')}'"

    def test_chat_response_has_timestamp(self):
        """3.3c chat 响应应包含 timestamp 字段。"""
        payload = {"message": "hello", "session_id": "test"}
        status, body, _ = http_post_json(f"{KERNEL_URL}/api/v1/chat", payload, timeout=30)
        assert status == 200
        assert "timestamp" in body, "响应缺少 timestamp 字段"
        assert isinstance(body["timestamp"], str), "timestamp 应为字符串"
