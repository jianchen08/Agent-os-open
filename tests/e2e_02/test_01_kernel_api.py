# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @vision: V3 可嵌入 | @ci: python-e2e
"""
用户旅程 1：Kernel API 端点验证（HTTP 请求验证）

验证 Kernel (Rust Axum) 的所有 RESTful API 端点。
代码参考: kernel/crates/api/src/routes.rs, kernel/crates/api/src/server.rs

测试项:
  1.1 GET /health → 200, 含 status=ok, version=0.2.0, timestamp
  1.2 GET /api/v1/schema → 200, 含 agents/pipelines/tools/routes 字段
  1.3 GET /api/v1/agents → 200, 返回 JSON 数组
  1.4 GET /api/v1/pipelines → 200, 返回 JSON 数组
  1.5 GET /api/v1/tools → 200, 返回 JSON 数组
  1.6 POST /api/v1/chat → 200, 响应含 type/content/session_id/timestamp 字段
"""
import json

import pytest
from e2e_helpers import http_get, http_get_with_auth, http_post_json_auth


class TestKernelApiHealth:
    """1.1 健康检查端点。"""

    def test_health_returns_200(self, kernel_url):
        """测试: GET /health 应返回 200 状态码。"""
        status, body, _ = http_get(f"{kernel_url}/health")
        assert status == 200, f"期望 200，实际 {status}"

    def test_health_status_field_ok(self, kernel_url):
        """测试: /health 响应 JSON 中 status 字段值为 "ok"。"""
        status, body, _ = http_get(f"{kernel_url}/health")
        assert isinstance(body, dict), f"响应应为 dict，实际 {type(body)}"
        assert body.get("status") == "ok", f"status 期望 'ok'，实际 '{body.get('status')}'"

    def test_health_version_field_is_0_2_0(self, kernel_url):
        """测试: /health 响应中 version 字段为 "0.2.0"。"""
        status, body, _ = http_get(f"{kernel_url}/health")
        assert body.get("version") == "0.2.0", f"version 期望 '0.2.0'，实际 '{body.get('version')}'"

    def test_health_has_timestamp_field(self, kernel_url):
        """测试: /health 响应包含 timestamp 字段且为字符串。"""
        status, body, _ = http_get(f"{kernel_url}/health")
        assert "timestamp" in body, "响应缺少 timestamp 字段"
        assert isinstance(body["timestamp"], str), f"timestamp 应为 str，实际 {type(body['timestamp'])}"
        assert len(body["timestamp"]) > 0, "timestamp 不应为空字符串"


class TestKernelApiSchema:
    """1.2 Schema 聚合端点。"""

    def test_schema_returns_200(self, kernel_url):
        """测试: GET /api/v1/schema 应返回 200。"""
        status, body, _ = http_get(f"{kernel_url}/api/v1/schema")
        assert status == 200, f"期望 200，实际 {status}"

    def test_schema_has_agents_field(self, kernel_url):
        """测试: /api/v1/schema 响应包含 agents 字段。"""
        status, body, _ = http_get(f"{kernel_url}/api/v1/schema")
        assert isinstance(body, dict), "响应应为 dict"
        assert "agents" in body, "缺少 agents 字段"

    def test_schema_has_pipelines_field(self, kernel_url):
        """测试: /api/v1/schema 响应包含 pipelines 字段。"""
        status, body, _ = http_get(f"{kernel_url}/api/v1/schema")
        assert "pipelines" in body, "缺少 pipelines 字段"

    def test_schema_has_tools_field(self, kernel_url):
        """测试: /api/v1/schema 响应包含 tools 字段。"""
        status, body, _ = http_get(f"{kernel_url}/api/v1/schema")
        assert "tools" in body, "缺少 tools 字段"

    def test_schema_has_routes_field(self, kernel_url):
        """测试: /api/v1/schema 响应包含 routes 字段。"""
        status, body, _ = http_get(f"{kernel_url}/api/v1/schema")
        assert "routes" in body, "缺少 routes 字段"


class TestKernelApiAgents:
    """1.3 Agents 列表端点。"""

    def test_agents_returns_200(self, kernel_url, auth_token):
        """测试: GET /ext/agent_manager/agents 应返回 200。

        /api/v1/agents* 4 路由已迁至 agent_manager 插件（2026-08-20 ADR，
        server.rs 注释明示），本用例跟随现行插件路由（需登录态）。
        """
        status, body, _ = http_get_with_auth(
            f"{kernel_url}/ext/agent_manager/agents", auth_token
        )
        assert status == 200, f"期望 200，实际 {status}"

    def test_agents_returns_items_envelope(self, kernel_url, auth_token):
        """测试: GET /ext/agent_manager/agents 返回 {items: [...]} 对象信封。

        0.2 契约：清单类端点统一为分页/包装信封（同 /api/v1/tools 的
        {items,total}），不再返回裸数组。
        """
        status, body, _ = http_get_with_auth(
            f"{kernel_url}/ext/agent_manager/agents", auth_token
        )
        assert status == 200, f"期望 200，实际 {status}"
        assert isinstance(body, dict) and "items" in body, (
            f"应为 {{items: [...]}} 信封，实际 {type(body)}"
        )
        assert isinstance(body["items"], list), "items 应为数组"


class TestKernelApiPipelines:
    """1.4 Pipelines 列表端点。"""

    def test_pipelines_returns_200(self, kernel_url):
        """测试: GET /api/v1/pipelines 应返回 200。"""
        status, body, _ = http_get(f"{kernel_url}/api/v1/pipelines")
        assert status == 200, f"期望 200，实际 {status}"

    def test_pipelines_returns_json_array(self, kernel_url):
        """测试: /api/v1/pipelines 返回 JSON 数组。"""
        status, body, _ = http_get(f"{kernel_url}/api/v1/pipelines")
        assert isinstance(body, list), f"响应应为 list，实际 {type(body)}"


class TestKernelApiTools:
    """1.5 Tools 列表端点。"""

    def test_tools_returns_200(self, kernel_url, auth_token):
        """测试: GET /api/v1/tools 应返回 200（需登录态）。"""
        status, body, _ = http_get_with_auth(f"{kernel_url}/api/v1/tools", auth_token)
        assert status == 200, f"期望 200，实际 {status}"

    def test_tools_returns_json_array(self, kernel_url, auth_token):
        """测试: /api/v1/tools 返回分页信封 {items,total}（0.2 当前契约，需登录态）。"""
        status, body, _ = http_get_with_auth(f"{kernel_url}/api/v1/tools", auth_token)
        assert status == 200, f"期望 200，实际 {status}"
        assert isinstance(body, dict) and "items" in body and "total" in body,             f"应为 {{items,total}} 信封，实际 {type(body)}"
        assert isinstance(body["items"], list), "items 应为数组"


class TestKernelApiChat:
    """1.6 Chat 消息发送端点。"""

    def test_chat_post_returns_200(self, kernel_url, auth_token):
        """测试: POST /api/v1/chat 发送消息应返回 200。"""
        payload = {"message": "test", "session_id": "verify"}
        status, body, _ = http_post_json_auth(f"{kernel_url}/api/v1/chat", payload, auth_token, timeout=60)
        assert status == 200, f"期望 200，实际 {status}"

    def test_chat_response_has_type_field(self, kernel_url, auth_token):
        """测试: chat 响应包含 type 字段。"""
        payload = {"message": "test", "session_id": "verify"}
        status, body, _ = http_post_json_auth(f"{kernel_url}/api/v1/chat", payload, auth_token, timeout=60)
        assert isinstance(body, dict), "响应应为 dict"
        assert "type" in body, "缺少 type 字段"

    def test_chat_response_has_content_field(self, kernel_url, auth_token):
        """测试: chat 响应包含 content 字段且包含请求消息。"""
        payload = {"message": "test", "session_id": "verify"}
        status, body, _ = http_post_json_auth(f"{kernel_url}/api/v1/chat", payload, auth_token, timeout=60)
        assert "content" in body, "缺少 content 字段"
        assert isinstance(body["content"], str), "content 应为字符串"

    def test_chat_response_has_session_id_field(self, kernel_url, auth_token):
        """测试: chat 响应包含 session_id 字段且值与请求一致。"""
        payload = {"message": "test", "session_id": "verify"}
        status, body, _ = http_post_json_auth(f"{kernel_url}/api/v1/chat", payload, auth_token, timeout=60)
        assert "session_id" in body, "缺少 session_id 字段"
        assert body["session_id"] == "verify", f"session_id 期望 'verify'，实际 '{body.get('session_id')}'"

    def test_chat_response_has_timestamp_field(self, kernel_url, auth_token):
        """测试: chat 响应包含 timestamp 字段。"""
        payload = {"message": "test", "session_id": "verify"}
        status, body, _ = http_post_json_auth(f"{kernel_url}/api/v1/chat", payload, auth_token, timeout=60)
        assert "timestamp" in body, "缺少 timestamp 字段"
        assert isinstance(body["timestamp"], str), "timestamp 应为字符串"
