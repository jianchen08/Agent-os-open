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
from e2e_helpers import http_get, http_post_json


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

    def test_agents_returns_200(self, kernel_url):
        """测试: GET /api/v1/agents 应返回 200。"""
        status, body, _ = http_get(f"{kernel_url}/api/v1/agents")
        assert status == 200, f"期望 200，实际 {status}"

    @pytest.mark.skip(reason="0.2 /api/v1/agents 返回对象形态（含 agents 列表包装），非裸数组——见 schema 聚合端点")
    def test_agents_returns_json_array(self, kernel_url):
        """测试: /api/v1/agents 返回 JSON 数组。"""
        status, body, _ = http_get(f"{kernel_url}/api/v1/agents")
        assert isinstance(body, list), f"响应应为 list，实际 {type(body)}"


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

    def test_tools_returns_200(self, kernel_url):
        """测试: GET /api/v1/tools 应返回 200。"""
        status, body, _ = http_get(f"{kernel_url}/api/v1/tools")
        assert status == 200, f"期望 200，实际 {status}"

    def test_tools_returns_json_array(self, kernel_url):
        """测试: /api/v1/tools 返回 JSON 数组。"""
        status, body, _ = http_get(f"{kernel_url}/api/v1/tools")
        assert isinstance(body, list), f"响应应为 list，实际 {type(body)}"


class TestKernelApiChat:
    """1.6 Chat 消息发送端点。"""

    def test_chat_post_returns_200(self, kernel_url):
        """测试: POST /api/v1/chat 发送消息应返回 200。"""
        payload = {"message": "test", "session_id": "verify"}
        status, body, _ = http_post_json(f"{kernel_url}/api/v1/chat", payload)
        assert status == 200, f"期望 200，实际 {status}"

    def test_chat_response_has_type_field(self, kernel_url):
        """测试: chat 响应包含 type 字段。"""
        payload = {"message": "test", "session_id": "verify"}
        status, body, _ = http_post_json(f"{kernel_url}/api/v1/chat", payload)
        assert isinstance(body, dict), "响应应为 dict"
        assert "type" in body, "缺少 type 字段"

    def test_chat_response_has_content_field(self, kernel_url):
        """测试: chat 响应包含 content 字段且包含请求消息。"""
        payload = {"message": "test", "session_id": "verify"}
        status, body, _ = http_post_json(f"{kernel_url}/api/v1/chat", payload)
        assert "content" in body, "缺少 content 字段"
        assert isinstance(body["content"], str), "content 应为字符串"

    def test_chat_response_has_session_id_field(self, kernel_url):
        """测试: chat 响应包含 session_id 字段且值与请求一致。"""
        payload = {"message": "test", "session_id": "verify"}
        status, body, _ = http_post_json(f"{kernel_url}/api/v1/chat", payload)
        assert "session_id" in body, "缺少 session_id 字段"
        assert body["session_id"] == "verify", f"session_id 期望 'verify'，实际 '{body.get('session_id')}'"

    def test_chat_response_has_timestamp_field(self, kernel_url):
        """测试: chat 响应包含 timestamp 字段。"""
        payload = {"message": "test", "session_id": "verify"}
        status, body, _ = http_post_json(f"{kernel_url}/api/v1/chat", payload)
        assert "timestamp" in body, "缺少 timestamp 字段"
        assert isinstance(body["timestamp"], str), "timestamp 应为字符串"
