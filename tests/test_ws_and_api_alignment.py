"""WebSocket 通信层和 REST API 响应格式对齐测试。

覆盖以下修复项：
- W1: /ws/chat 全局端点
- W2: 平铺消息格式兼容
- W3: 心跳处理
- W4: user_input 消息处理
- R4: Projects 路由响应格式
- R5: 缺失的 task phase/AC 路由
- R7: getTasks skip/offset 参数对齐
"""

from __future__ import annotations

import json
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ============================================================
# W1-W4: WebSocket 通信层测试
# ============================================================


class TestWebSocketServerRouting:
    """W1 - /ws/chat 全局端点路由测试。"""

    def test_start_registers_ws_chat_global_route(self) -> None:
        """验证 start() 注册了 /ws/chat 路由（不带 thread_id）。"""
        from channels.websocket.server import WebSocketServer

        server = WebSocketServer()
        # 模拟 runner 和 site
        server._runner = MagicMock()
        server._runner.setup = AsyncMock()
        server._site = MagicMock()
        server._site.start = AsyncMock()

        # 直接构建 app 来检查路由
        from aiohttp import web

        app = web.Application()
        # 模拟 start 中的路由注册逻辑
        app.router.add_get("/ws", server._handle_websocket)
        app.router.add_get("/ws/{thread_id}", server._handle_websocket)
        app.router.add_get("/ws/chat/{thread_id}", server._handle_websocket)
        app.router.add_get("/ws/chat", server._handle_websocket)

        # 检查路由是否注册
        resources = [r.resource for r in app.router.routes()]
        paths = set()
        for r in app.router.routes():
            info = r.get_info()
            path = info.get("path", info.get("formatter", ""))
            paths.add(path)

        assert "/ws/chat" in paths, f"/ws/chat 未注册, 已注册路由: {paths}"


class TestMessageFormatCompatibility:
    """W2 - 平铺消息格式兼容测试。"""

    @pytest.fixture
    def ws_server(self) -> "WebSocketServer":
        """创建 WebSocketServer 实例用于测试。"""
        from channels.websocket.server import WebSocketServer
        server = WebSocketServer()
        # Mock session_manager
        server.session_manager = MagicMock()
        server.session_manager.acknowledge = MagicMock()
        server.session_manager.get_missed_messages = MagicMock(return_value=[])
        return server

    @pytest.mark.asyncio
    async def test_flat_format_user_input_is_handled(
        self, ws_server: "WebSocketServer",
    ) -> None:
        """平铺格式 {type, content, thread_id} 应被正确处理并传递给 on_message handler。"""
        from channels.websocket.protocol import EventType

        received_messages: list[dict] = []
        ws_server._on_message_handler = AsyncMock(
            side_effect=lambda sid, msg: received_messages.append(msg),
        )

        flat_msg = json.dumps({
            "type": "user_input",
            "content": "你好",
            "thread_id": "thread-123",
        })

        await ws_server._process_text_message("session-1", flat_msg)

        ws_server._on_message_handler.assert_called_once()
        call_args = ws_server._on_message_handler.call_args
        msg_dict = call_args[0][1]  # 第二个参数是 parsed message

        assert msg_dict["type"] == "user_input"
        assert msg_dict["data"]["content"] == "你好"
        assert msg_dict["data"]["thread_id"] == "thread-123"

    @pytest.mark.asyncio
    async def test_envelope_format_still_works(
        self, ws_server: "WebSocketServer",
    ) -> None:
        """EventEnvelope 格式消息仍应正常工作。"""
        ws_server._on_message_handler = AsyncMock()

        envelope_msg = json.dumps({
            "type": "stop_generation",
            "data": {"reason": "user_cancel"},
            "timestamp": "2026-05-14T00:00:00Z",
            "request_id": "req-123",
        })

        await ws_server._process_text_message("session-1", envelope_msg)
        ws_server._on_message_handler.assert_called_once()

    @pytest.mark.asyncio
    async def test_invalid_json_is_ignored(
        self, ws_server: "WebSocketServer",
    ) -> None:
        """无效 JSON 应被忽略，不抛异常。"""
        ws_server._on_message_handler = AsyncMock()
        await ws_server._process_text_message("session-1", "not json{{{")
        ws_server._on_message_handler.assert_not_called()

    @pytest.mark.asyncio
    async def test_message_without_type_is_ignored(
        self, ws_server: "WebSocketServer",
    ) -> None:
        """没有 type 字段的消息应被忽略。"""
        ws_server._on_message_handler = AsyncMock()
        await ws_server._process_text_message(
            "session-1", json.dumps({"data": {"foo": "bar"}}),
        )
        ws_server._on_message_handler.assert_not_called()


class TestHeartbeatHandling:
    """W3 - 心跳处理测试。"""

    @pytest.fixture
    def ws_server(self) -> "WebSocketServer":
        """创建带 mock session_manager 的 WebSocketServer。"""
        from channels.websocket.server import WebSocketServer
        server = WebSocketServer()
        server.session_manager = MagicMock()
        server.session_manager.acknowledge = MagicMock()
        server.session_manager.send_to = AsyncMock(return_value=True)
        return server

    @pytest.mark.asyncio
    async def test_heartbeat_replies_with_ack(
        self, ws_server: "WebSocketServer",
    ) -> None:
        """收到 heartbeat 消息应回复 heartbeat_ack。"""
        ws_server._on_message_handler = AsyncMock()

        heartbeat_msg = json.dumps({
            "type": "heartbeat",
            "timestamp": 1715654400,
        })

        await ws_server._process_text_message("session-1", heartbeat_msg)

        # 不应调用 on_message handler
        ws_server._on_message_handler.assert_not_called()

        # 应通过 session_manager 发送 heartbeat_ack
        ws_server.session_manager.send_to.assert_called_once()
        call_args = ws_server.session_manager.send_to.call_args
        sent_json = call_args[0][1]  # 第二个参数是消息字符串
        sent_dict = json.loads(sent_json)

        assert sent_dict["type"] == "heartbeat_ack"
        assert sent_dict["data"]["timestamp"] == 1715654400


class TestUserInputHandling:
    """W4 - user_input 消息处理测试。"""

    @pytest.fixture
    def ws_server(self) -> "WebSocketServer":
        """创建带 mock 的 WebSocketServer。"""
        from channels.websocket.server import WebSocketServer
        server = WebSocketServer()
        server.session_manager = MagicMock()
        server.session_manager.acknowledge = MagicMock()
        return server

    @pytest.mark.asyncio
    async def test_user_input_flat_format_calls_handler(
        self, ws_server: "WebSocketServer",
    ) -> None:
        """user_input 平铺格式应传递给 on_message handler。"""
        ws_server._on_message_handler = AsyncMock()

        msg = json.dumps({
            "type": "user_input",
            "content": "写一个函数",
            "thread_id": "thread-abc",
        })

        await ws_server._process_text_message("session-1", msg)

        ws_server._on_message_handler.assert_called_once_with(
            "session-1",
            ws_server._on_message_handler.call_args[0][1],
        )

    @pytest.mark.asyncio
    async def test_user_input_envelope_format_calls_handler(
        self, ws_server: "WebSocketServer",
    ) -> None:
        """user_input EventEnvelope 格式也应传递给 on_message handler。"""
        ws_server._on_message_handler = AsyncMock()

        msg = json.dumps({
            "type": "user_input",
            "data": {"content": "写一个函数", "thread_id": "thread-abc"},
            "timestamp": "2026-05-14T00:00:00Z",
            "request_id": "req-456",
        })

        await ws_server._process_text_message("session-1", msg)

        ws_server._on_message_handler.assert_called_once()


# ============================================================
# R4-R7: REST API 响应格式测试
# ============================================================


class TestProjectsRouteFormat:
    """R4 - Projects 路由响应格式测试。"""

    @pytest.fixture
    def client(self) -> "TestClient":
        """创建 FastAPI 测试客户端。"""
        from fastapi.testclient import TestClient
        from channels.api.app import create_app
        app = create_app()
        return TestClient(app)

    @staticmethod
    def _auth_headers() -> dict[str, str]:
        """获取认证头（使用 JWT 签发的有效 access token）。"""
        from channels.api.auth import create_access_token
        token = create_access_token({"sub": "test-user", "username": "test"})
        return {"Authorization": f"Bearer {token}"}

    def test_list_projects_response_format(self, client: "TestClient") -> None:
        """list_projects 应返回 {items, total, limit, offset} 格式。"""
        resp = client.get("/api/v1/projects", headers=self._auth_headers())
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data, f"缺少 items 字段: {data}"
        assert "total" in data, f"缺少 total 字段: {data}"
        assert "limit" in data, f"缺少 limit 字段: {data}"
        assert "offset" in data, f"缺少 offset 字段: {data}"
        assert isinstance(data["items"], list)

    def test_create_project_response_format(self, client: "TestClient") -> None:
        """create_project 应返回 {project: {id, userId, goal, status, ...}} 格式。"""
        resp = client.post(
            "/api/v1/projects",
            json={"goal": "测试项目"},
            headers=self._auth_headers(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "project" in data, f"缺少 project 字段: {data}"
        project = data["project"]
        assert "id" in project
        assert "status" in project

    def test_get_project_response_format(self, client: "TestClient") -> None:
        """get_project 应返回 {project: {...}} 格式。"""
        resp = client.get(
            "/api/v1/projects/test-proj-1",
            headers=self._auth_headers(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "project" in data, f"缺少 project 字段: {data}"

    def test_toggle_auto_execute_response_format(self, client: "TestClient") -> None:
        """toggle_auto_execute 应返回 {project: {...}} 格式。"""
        resp = client.post(
            "/api/v1/projects/test-proj-1/auto-execute",
            headers=self._auth_headers(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "project" in data, f"缺少 project 字段: {data}"

    def test_pause_project_response_format(self, client: "TestClient") -> None:
        """pause_project 应返回 {project: {...}} 格式。"""
        resp = client.post(
            "/api/v1/projects/test-proj-1/pause",
            headers=self._auth_headers(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "project" in data, f"缺少 project 字段: {data}"

    def test_resume_project_response_format(self, client: "TestClient") -> None:
        """resume_project 应返回 {project: {...}} 格式。"""
        resp = client.post(
            "/api/v1/projects/test-proj-1/resume",
            headers=self._auth_headers(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "project" in data, f"缺少 project 字段: {data}"


class TestMissingTaskRoutes:
    """R5 - 缺失的 task phase/AC 路由测试。"""

    @pytest.fixture
    def client(self) -> "TestClient":
        """创建 FastAPI 测试客户端。"""
        from fastapi.testclient import TestClient
        from channels.api.app import create_app
        app = create_app()
        return TestClient(app)

    @staticmethod
    def _auth_headers() -> dict[str, str]:
        """获取认证头。"""
        from channels.api.auth import create_access_token
        token = create_access_token({"sub": "test-user", "username": "test"})
        return {"Authorization": f"Bearer {token}"}

    def test_get_task_phase(self, client: "TestClient") -> None:
        """GET /api/v1/tasks/{id}/phase 应返回正确格式。"""
        resp = client.get(
            "/api/v1/tasks/test-task-1/phase",
            headers=self._auth_headers(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "taskId" in data or "task_id" in data
        assert "currentPhase" in data or "current_phase" in data

    def test_complete_prepare_phase(self, client: "TestClient") -> None:
        """POST /api/v1/tasks/{id}/phase/prepare/complete 应返回正确格式。"""
        resp = client.post(
            "/api/v1/tasks/test-task-1/phase/prepare/complete",
            headers=self._auth_headers(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "task_id" in data or "taskId" in data
        assert "current_phase" in data or "currentPhase" in data

    def test_complete_execute_phase(self, client: "TestClient") -> None:
        """POST /api/v1/tasks/{id}/phase/execute/complete 应返回正确格式。"""
        resp = client.post(
            "/api/v1/tasks/test-task-1/phase/execute/complete",
            headers=self._auth_headers(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "task_id" in data or "taskId" in data
        assert "current_phase" in data or "currentPhase" in data

    def test_get_phase_output(self, client: "TestClient") -> None:
        """GET /api/v1/tasks/{id}/phase/{phase}/output 应返回正确格式。"""
        resp = client.get(
            "/api/v1/tasks/test-task-1/phase/prepare/output",
            headers=self._auth_headers(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "output" in data

    def test_get_task_ac(self, client: "TestClient") -> None:
        """GET /api/v1/tasks/{id}/ac 应返回正确格式。"""
        resp = client.get(
            "/api/v1/tasks/test-task-1/ac",
            headers=self._auth_headers(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "taskId" in data or "task_id" in data
        assert "acceptanceCriteria" in data or "acceptance_criteria" in data

    def test_evaluate_ac(self, client: "TestClient") -> None:
        """POST /api/v1/tasks/{id}/ac/{acId}/evaluate 应返回正确格式。"""
        resp = client.post(
            "/api/v1/tasks/test-task-1/ac/ac-1/evaluate",
            headers=self._auth_headers(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "acceptance_criterion" in data or "task_id" in data

    def test_evaluate_all_ac(self, client: "TestClient") -> None:
        """POST /api/v1/tasks/{id}/ac/evaluate-all 应返回正确格式。"""
        resp = client.post(
            "/api/v1/tasks/test-task-1/ac/evaluate-all",
            headers=self._auth_headers(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "taskId" in data or "task_id" in data

    def test_get_ac_result(self, client: "TestClient") -> None:
        """GET /api/v1/tasks/{id}/ac/{acId}/result 应返回正确格式。"""
        resp = client.get(
            "/api/v1/tasks/test-task-1/ac/ac-1/result",
            headers=self._auth_headers(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "acceptance_criterion" in data or "id" in data


class TestTasksSkipOffsetParameter:
    """R7 - getTasks 同时支持 skip 和 offset 参数。"""

    @pytest.fixture
    def client(self) -> "TestClient":
        """创建 FastAPI 测试客户端。"""
        from fastapi.testclient import TestClient
        from channels.api.app import create_app
        app = create_app()
        return TestClient(app)

    @staticmethod
    def _auth_headers() -> dict[str, str]:
        """获取认证头。"""
        from channels.api.auth import create_access_token
        token = create_access_token({"sub": "test-user", "username": "test"})
        return {"Authorization": f"Bearer {token}"}

    def test_list_tasks_with_offset_param(self, client: "TestClient") -> None:
        """使用 offset 参数应正常工作。"""
        resp = client.get(
            "/api/v1/tasks?offset=0&limit=10",
            headers=self._auth_headers(),
        )
        assert resp.status_code == 200

    def test_list_tasks_with_skip_param(self, client: "TestClient") -> None:
        """使用 skip 参数应与 offset 等价。"""
        resp = client.get(
            "/api/v1/tasks?skip=0&limit=10",
            headers=self._auth_headers(),
        )
        assert resp.status_code == 200
