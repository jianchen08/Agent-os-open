"""外部系统 Agent 管道执行端点测试。

验证 POST /api/v1/external/chat 端点的完整行为：
1. 正常调用 → 200 + ExternalChatResponse
2. Agent 不存在 → 404 + EXT_CHAT_002
3. AgentRegistry 不可用 → 503 + EXT_CHAT_001
4. PipelineFactory 不可用 → 503 + EXT_CHAT_003
5. 引擎创建失败 → 500 + EXT_CHAT_004
6. 引擎执行失败 → 500 + EXT_CHAT_005
7. 未认证访问 → 401
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from channels.api.deps import APIError, api_error_handler, require_auth
from channels.api.routes_external_chat import router

pytestmark = [pytest.mark.unit, pytest.mark.offline, pytest.mark.timeout(10)]


# ============================================================
# 测试应用工厂
# ============================================================


def _create_test_app() -> FastAPI:
    """创建包含 external_chat 路由和错误处理的测试应用。"""
    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(APIError, api_error_handler)  # type: ignore[arg-type]
    return app


async def _mock_auth() -> dict:
    """Mock 认证依赖，返回固定用户信息。"""
    return {"sub": "user1", "username": "tester"}


def _auth_headers() -> dict[str, str]:
    """返回认证请求头（配合 dependency_overrides 使用时 header 本身内容不重要）。"""
    return {"Authorization": "Bearer test-token"}


def _make_client_with_auth() -> TestClient:
    """创建带有认证 override 的 TestClient。"""
    app = _create_test_app()
    app.dependency_overrides[require_auth] = _mock_auth
    return TestClient(app)


# ============================================================
# 测试类
# ============================================================


class TestExternalChatSuccess:
    """正常调用场景。"""

    @patch("channels.api.routes_external_chat._get_pipeline_factory")
    @patch("channels.api.routes_external_chat._get_agent_registry")
    def test_success_returns_200_with_reply(
        self,
        mock_get_registry: MagicMock,
        mock_get_factory: MagicMock,
    ) -> None:
        """agent_id 存在 + message 有效 → 200 + ExternalChatResponse。"""
        mock_registry = MagicMock()
        mock_registry.get.return_value = {"id": "agent_001", "name": "测试Agent"}
        mock_get_registry.return_value = mock_registry

        mock_engine = AsyncMock()
        mock_engine.run.return_value = {"raw_result": "这是Agent的回复"}
        mock_factory = MagicMock(return_value=mock_engine)
        mock_get_factory.return_value = mock_factory

        client = _make_client_with_auth()
        response = client.post(
            "/api/v1/external/chat",
            json={"agent_id": "agent_001", "message": "你好"},
            headers=_auth_headers(),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["agent_id"] == "agent_001"
        assert data["reply"] == "这是Agent的回复"

        mock_engine.run.assert_awaited_once_with(
            user_input="你好",
            agent_config={"id": "agent_001", "name": "测试Agent"},
            allow_default_fallback=False,
        )

    @patch("channels.api.routes_external_chat._get_pipeline_factory")
    @patch("channels.api.routes_external_chat._get_agent_registry")
    def test_success_with_none_raw_result(
        self,
        mock_get_registry: MagicMock,
        mock_get_factory: MagicMock,
    ) -> None:
        """raw_result 为 None 时 reply 为空字符串。"""
        mock_registry = MagicMock()
        mock_registry.get.return_value = {"id": "agent_002"}
        mock_get_registry.return_value = mock_registry

        mock_engine = AsyncMock()
        mock_engine.run.return_value = {"raw_result": None}
        mock_factory = MagicMock(return_value=mock_engine)
        mock_get_factory.return_value = mock_factory

        client = _make_client_with_auth()
        response = client.post(
            "/api/v1/external/chat",
            json={"agent_id": "agent_002", "message": "测试"},
            headers=_auth_headers(),
        )

        assert response.status_code == 200
        assert response.json()["reply"] == ""

    @patch("channels.api.routes_external_chat._get_pipeline_factory")
    @patch("channels.api.routes_external_chat._get_agent_registry")
    def test_success_without_raw_result_key(
        self,
        mock_get_registry: MagicMock,
        mock_get_factory: MagicMock,
    ) -> None:
        """state 中无 raw_result 键时 reply 为空字符串（取默认值）。"""
        mock_registry = MagicMock()
        mock_registry.get.return_value = {"id": "agent_003"}
        mock_get_registry.return_value = mock_registry

        mock_engine = AsyncMock()
        mock_engine.run.return_value = {}
        mock_factory = MagicMock(return_value=mock_engine)
        mock_get_factory.return_value = mock_factory

        client = _make_client_with_auth()
        response = client.post(
            "/api/v1/external/chat",
            json={"agent_id": "agent_003", "message": "测试"},
            headers=_auth_headers(),
        )

        assert response.status_code == 200
        assert response.json()["reply"] == ""

    @patch("channels.api.routes_external_chat._get_pipeline_factory")
    @patch("channels.api.routes_external_chat._get_agent_registry")
    def test_success_with_non_string_raw_result(
        self,
        mock_get_registry: MagicMock,
        mock_get_factory: MagicMock,
    ) -> None:
        """raw_result 为非字符串时自动转为字符串。"""
        mock_registry = MagicMock()
        mock_registry.get.return_value = {"id": "agent_004"}
        mock_get_registry.return_value = mock_registry

        mock_engine = AsyncMock()
        mock_engine.run.return_value = {"raw_result": 42}
        mock_factory = MagicMock(return_value=mock_engine)
        mock_get_factory.return_value = mock_factory

        client = _make_client_with_auth()
        response = client.post(
            "/api/v1/external/chat",
            json={"agent_id": "agent_004", "message": "计算"},
            headers=_auth_headers(),
        )

        assert response.status_code == 200
        assert response.json()["reply"] == "42"


class TestExternalChatAgentNotFound:
    """Agent 不存在场景。"""

    @patch("channels.api.routes_external_chat._get_agent_registry")
    def test_agent_not_found_returns_404(
        self,
        mock_get_registry: MagicMock,
    ) -> None:
        """agent_id 无效 → 404 + EXT_CHAT_002。"""
        mock_registry = MagicMock()
        mock_registry.get.return_value = None
        mock_get_registry.return_value = mock_registry

        client = _make_client_with_auth()
        response = client.post(
            "/api/v1/external/chat",
            json={"agent_id": "nonexistent_agent", "message": "你好"},
            headers=_auth_headers(),
        )

        assert response.status_code == 404
        data = response.json()
        assert data["error"]["code"] == "EXT_CHAT_002"
        assert "nonexistent_agent" in data["error"]["message"]

    @patch("channels.api.routes_external_chat._get_agent_registry")
    def test_agent_not_found_message_contains_agent_id(
        self,
        mock_get_registry: MagicMock,
    ) -> None:
        """404 错误消息中包含具体的 agent_id。"""
        mock_registry = MagicMock()
        mock_registry.get.return_value = None
        mock_get_registry.return_value = mock_registry

        client = _make_client_with_auth()
        response = client.post(
            "/api/v1/external/chat",
            json={"agent_id": "my_special_agent", "message": "test"},
            headers=_auth_headers(),
        )

        assert response.status_code == 404
        assert "my_special_agent" in response.json()["error"]["message"]


class TestExternalChatRegistryUnavailable:
    """AgentRegistry 不可用场景。"""

    @patch("channels.api.routes_external_chat._get_agent_registry")
    def test_registry_none_returns_503(
        self,
        mock_get_registry: MagicMock,
    ) -> None:
        """registry 为 None → 503 + EXT_CHAT_001。"""
        mock_get_registry.return_value = None

        client = _make_client_with_auth()
        response = client.post(
            "/api/v1/external/chat",
            json={"agent_id": "any_agent", "message": "你好"},
            headers=_auth_headers(),
        )

        assert response.status_code == 503
        data = response.json()
        assert data["error"]["code"] == "EXT_CHAT_001"
        assert "AgentRegistry" in data["error"]["message"]


class TestExternalChatFactoryUnavailable:
    """PipelineFactory 不可用场景。"""

    @patch("channels.api.routes_external_chat._get_pipeline_factory")
    @patch("channels.api.routes_external_chat._get_agent_registry")
    def test_factory_none_returns_503(
        self,
        mock_get_registry: MagicMock,
        mock_get_factory: MagicMock,
    ) -> None:
        """factory 为 None → 503 + EXT_CHAT_003。"""
        mock_registry = MagicMock()
        mock_registry.get.return_value = {"id": "agent_001"}
        mock_get_registry.return_value = mock_registry

        mock_get_factory.return_value = None

        client = _make_client_with_auth()
        response = client.post(
            "/api/v1/external/chat",
            json={"agent_id": "agent_001", "message": "你好"},
            headers=_auth_headers(),
        )

        assert response.status_code == 503
        data = response.json()
        assert data["error"]["code"] == "EXT_CHAT_003"
        assert "Pipeline" in data["error"]["message"]


class TestExternalChatEngineCreateFailure:
    """引擎创建失败场景。"""

    @patch("channels.api.routes_external_chat._get_pipeline_factory")
    @patch("channels.api.routes_external_chat._get_agent_registry")
    def test_factory_raises_returns_500(
        self,
        mock_get_registry: MagicMock,
        mock_get_factory: MagicMock,
    ) -> None:
        """factory() 抛异常 → 500 + EXT_CHAT_004。"""
        mock_registry = MagicMock()
        mock_registry.get.return_value = {"id": "agent_001"}
        mock_get_registry.return_value = mock_registry

        mock_factory = MagicMock(side_effect=RuntimeError("引擎初始化失败"))
        mock_get_factory.return_value = mock_factory

        client = _make_client_with_auth()
        response = client.post(
            "/api/v1/external/chat",
            json={"agent_id": "agent_001", "message": "你好"},
            headers=_auth_headers(),
        )

        assert response.status_code == 500
        data = response.json()
        assert data["error"]["code"] == "EXT_CHAT_004"
        assert "创建失败" in data["error"]["message"]


class TestExternalChatEngineRunFailure:
    """引擎执行失败场景。"""

    @patch("channels.api.routes_external_chat._get_pipeline_factory")
    @patch("channels.api.routes_external_chat._get_agent_registry")
    def test_engine_run_raises_returns_500(
        self,
        mock_get_registry: MagicMock,
        mock_get_factory: MagicMock,
    ) -> None:
        """engine.run() 抛异常 → 500 + EXT_CHAT_005。"""
        mock_registry = MagicMock()
        mock_registry.get.return_value = {"id": "agent_001"}
        mock_get_registry.return_value = mock_registry

        mock_engine = AsyncMock()
        mock_engine.run.side_effect = RuntimeError("管道执行异常")
        mock_factory = MagicMock(return_value=mock_engine)
        mock_get_factory.return_value = mock_factory

        client = _make_client_with_auth()
        response = client.post(
            "/api/v1/external/chat",
            json={"agent_id": "agent_001", "message": "你好"},
            headers=_auth_headers(),
        )

        assert response.status_code == 500
        data = response.json()
        assert data["error"]["code"] == "EXT_CHAT_005"
        assert "执行失败" in data["error"]["message"]


class TestExternalChatUnauthenticated:
    """未认证访问场景。"""

    def test_no_auth_header_returns_401(self) -> None:
        """不带 Authorization header → 401。"""
        app = _create_test_app()
        # 不设置 dependency_overrides，走真实认证链路
        client = TestClient(app)
        response = client.post(
            "/api/v1/external/chat",
            json={"agent_id": "agent_001", "message": "你好"},
        )

        assert response.status_code == 401

    def test_empty_auth_header_returns_401(self) -> None:
        """Authorization header 为空 → 401。"""
        app = _create_test_app()
        client = TestClient(app)
        response = client.post(
            "/api/v1/external/chat",
            json={"agent_id": "agent_001", "message": "你好"},
            headers={"Authorization": ""},
        )

        assert response.status_code == 401

    def test_invalid_token_returns_401(self) -> None:
        """无效 token → 401。"""
        app = _create_test_app()
        client = TestClient(app)
        response = client.post(
            "/api/v1/external/chat",
            json={"agent_id": "agent_001", "message": "你好"},
            headers={"Authorization": "Bearer invalid-token-xxx"},
        )

        assert response.status_code == 401
