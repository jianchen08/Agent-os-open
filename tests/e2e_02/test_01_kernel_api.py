# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @vision: V3 可嵌入 | @ci: python-e2e
"""
用户旅程 1：Kernel API 端点验证（HTTP 请求验证）

验证 Kernel (Rust Axum) 的基础 RESTful API 端点。
代码参考: kernel/crates/api/src/routes.rs, kernel/crates/api/src/server.rs

测试项:
  1.1 GET /health → 200, 含 status=ok, version=0.2.0, timestamp
  1.2 GET /api/v1/schema → 200, 含 agents/pipelines/tools/routes 字段
  1.3 GET /api/v1/agents → 200, 返回 JSON 数组
  1.4 GET /api/v1/pipelines → 200, 返回 JSON 数组

tools 明细（非空/total 自洽/逐工具字段）见 test_02 TestPluginToolLoading；
chat 信封见 test_02 TestChatPipelineEngine 与 test_05。
"""
from e2e_helpers import http_get, http_get_with_auth


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

    def test_schema_returns_200(self, kernel_url, auth_token):
        """测试: GET /api/v1/schema（已认证）应返回 200。

        30d1b0959 匿名读面收口后 schema 需登录态，用例跟随现行契约。"""
        status, body, _ = http_get_with_auth(f"{kernel_url}/api/v1/schema", auth_token)
        assert status == 200, f"期望 200，实际 {status}"

    def test_schema_has_agents_field(self, kernel_url, auth_token):
        """测试: /api/v1/schema 响应包含 agents 字段。"""
        status, body, _ = http_get_with_auth(f"{kernel_url}/api/v1/schema", auth_token)
        assert isinstance(body, dict), "响应应为 dict"
        assert "agents" in body, "缺少 agents 字段"

    def test_schema_has_pipelines_field(self, kernel_url, auth_token):
        """测试: /api/v1/schema 响应包含 pipelines 字段。"""
        status, body, _ = http_get_with_auth(f"{kernel_url}/api/v1/schema", auth_token)
        assert "pipelines" in body, "缺少 pipelines 字段"

    def test_schema_has_tools_field(self, kernel_url, auth_token):
        """测试: /api/v1/schema 响应包含 tools 字段。"""
        status, body, _ = http_get_with_auth(f"{kernel_url}/api/v1/schema", auth_token)
        assert "tools" in body, "缺少 tools 字段"

    def test_schema_has_routes_field(self, kernel_url, auth_token):
        """测试: /api/v1/schema 响应包含 routes 字段。"""
        status, body, _ = http_get_with_auth(f"{kernel_url}/api/v1/schema", auth_token)
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
        assert isinstance(body, dict), f"应为 dict 信封，实际 {type(body)}"
        assert "items" in body, "信封应含 items 键"
        assert isinstance(body["items"], list), "items 应为数组"


class TestKernelApiPipelines:
    """1.4 Pipelines 列表端点。"""

    def test_pipelines_returns_200(self, kernel_url, auth_token):
        """测试: GET /api/v1/pipelines（已认证）应返回 200。"""
        status, body, _ = http_get_with_auth(f"{kernel_url}/api/v1/pipelines", auth_token)
        assert status == 200, f"期望 200，实际 {status}"

    def test_pipelines_returns_json_array(self, kernel_url, auth_token):
        """测试: /api/v1/pipelines 返回 JSON 数组。"""
        status, body, _ = http_get_with_auth(f"{kernel_url}/api/v1/pipelines", auth_token)
        assert isinstance(body, list), f"响应应为 list，实际 {type(body)}"
