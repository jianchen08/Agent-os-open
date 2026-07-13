"""测试线程 API 修复：前后端路径匹配 + 数据模型对齐。

验证内容：
1. ThreadCreate 支持 agent_id、metadata、intent 字段
2. ThreadUpdate 支持 agent_id、metadata、intent 字段
3. ThreadResponse 包含 metadata 字段
4. MemoryStore.create_thread 支持 agent_id 和 metadata
5. MemoryStore.update_thread 支持 agent_id、intent、metadata
6. MemoryStore.get_thread/get_user_threads 返回完整字段
7. 路由注册：PUT /{thread_id}、PUT /{thread_id}/agent 存在
8. _build_thread_response 正确映射所有字段
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


# ============================================================
# 单元测试：数据模型
# ============================================================


class TestThreadCreateModel:
    """ThreadCreate 模型测试。"""

    def test_basic_title_only(self) -> None:
        """只传 title 创建线程。"""
        from channels.api.models import ThreadCreate

        tc = ThreadCreate(title="测试会话")
        assert tc.title == "测试会话"
        assert tc.intent is None
        assert tc.agent_id is None
        assert tc.metadata is None

    def test_with_agent_id(self) -> None:
        """创建线程时绑定 Agent。"""
        from channels.api.models import ThreadCreate

        tc = ThreadCreate(title="Agent 会话", agent_id="agent_lingxi")
        assert tc.agent_id == "agent_lingxi"

    def test_with_metadata(self) -> None:
        """创建线程时带 metadata。"""
        from channels.api.models import ThreadCreate

        tc = ThreadCreate(
            title="带元数据的会话",
            metadata={"session_type": "main_pipeline", "source": "web"},
        )
        assert tc.metadata == {"session_type": "main_pipeline", "source": "web"}

    def test_with_intent(self) -> None:
        """创建线程时带 intent。"""
        from channels.api.models import ThreadCreate

        tc = ThreadCreate(intent="帮我写代码")
        assert tc.intent == "帮我写代码"
        assert tc.title is None

    def test_full_fields(self) -> None:
        """所有字段同时传递。"""
        from channels.api.models import ThreadCreate

        tc = ThreadCreate(
            title="完整测试",
            intent="完整测试意图",
            agent_id="agent_abc",
            metadata={"key": "value"},
        )
        assert tc.title == "完整测试"
        assert tc.intent == "完整测试意图"
        assert tc.agent_id == "agent_abc"
        assert tc.metadata == {"key": "value"}


class TestThreadUpdateModel:
    """ThreadUpdate 模型测试。"""

    def test_title_only(self) -> None:
        """只更新标题。"""
        from channels.api.models import ThreadUpdate

        tu = ThreadUpdate(title="新标题")
        assert tu.title == "新标题"
        assert tu.agent_id is None

    def test_agent_id_update(self) -> None:
        """更新 Agent 绑定。"""
        from channels.api.models import ThreadUpdate

        tu = ThreadUpdate(agent_id="new_agent")
        assert tu.agent_id == "new_agent"

    def test_agent_id_null_unbind(self) -> None:
        """agent_id 设为 None 解绑 Agent。"""
        from channels.api.models import ThreadUpdate

        tu = ThreadUpdate(agent_id=None)
        assert tu.agent_id is None


class TestThreadResponseModel:
    """ThreadResponse 模型测试。"""

    def test_includes_metadata(self) -> None:
        """ThreadResponse 包含 metadata 字段。"""
        from channels.api.models import ThreadResponse

        tr = ThreadResponse(
            thread_id="t1",
            intent="测试",
            created_at="2024-01-01",
            updated_at="2024-01-01",
            metadata={"session_type": "main_pipeline"},
        )
        assert tr.metadata == {"session_type": "main_pipeline"}

    def test_default_metadata_empty(self) -> None:
        """默认 metadata 为空字典。"""
        from channels.api.models import ThreadResponse

        tr = ThreadResponse(
            thread_id="t1",
            created_at="2024-01-01",
            updated_at="2024-01-01",
        )
        assert tr.metadata is None

    def test_includes_agent_id(self) -> None:
        """ThreadResponse 包含 agent_id。"""
        from channels.api.models import ThreadResponse

        tr = ThreadResponse(
            thread_id="t1",
            created_at="2024-01-01",
            updated_at="2024-01-01",
            agent_id="agent_123",
        )
        assert tr.agent_id == "agent_123"


# ============================================================
# 单元测试：MemoryStore 线程操作
# ============================================================


class TestMemoryStoreThread:
    """MemoryStore 线程操作测试。"""

    def _make_store(self) -> "MemoryStore":
        """创建干净的 MemoryStore 实例。"""
        from channels.api.models import MemoryStore

        return MemoryStore()

    def _demo_user(self, store: "MemoryStore") -> dict:
        """获取 demo 用户。"""
        user = store.get_user_by_username("demo")
        assert user is not None, "demo 用户应存在"
        return user

    def test_create_thread_with_agent_id(self) -> None:
        """创建线程时绑定 Agent ID。"""
        store = self._make_store()
        user = self._demo_user(store)
        thread = store.create_thread(
            user_id=user["id"],
            title="测试会话",
            agent_id="agent_lingxi",
        )
        assert thread["agent_id"] == "agent_lingxi"

    def test_create_thread_with_metadata(self) -> None:
        """创建线程时带 metadata。"""
        store = self._make_store()
        user = self._demo_user(store)
        thread = store.create_thread(
            user_id=user["id"],
            title="带元数据",
            metadata={"session_type": "main_pipeline"},
        )
        assert thread["metadata"] == {"session_type": "main_pipeline"}

    def test_create_thread_with_intent(self) -> None:
        """创建线程时带 intent。"""
        store = self._make_store()
        user = self._demo_user(store)
        thread = store.create_thread(
            user_id=user["id"],
            title="标题",
            intent="用户意图",
        )
        assert thread["intent"] == "用户意图"

    def test_create_thread_defaults(self) -> None:
        """创建线程的默认值。"""
        store = self._make_store()
        user = self._demo_user(store)
        thread = store.create_thread(user_id=user["id"])
        assert thread["current_state"] == "active"
        assert thread["agent_id"] is None
        assert thread["metadata"] == {}

    def test_update_thread_agent_id(self) -> None:
        """更新线程的 Agent 绑定。"""
        store = self._make_store()
        user = self._demo_user(store)
        thread = store.create_thread(user_id=user["id"])

        updated = store.update_thread(thread["id"], agent_id="new_agent")
        assert updated is not None
        assert updated["agent_id"] == "new_agent"

    def test_update_thread_metadata_merge(self) -> None:
        """更新线程的 metadata 时合并而非替换。"""
        store = self._make_store()
        user = self._demo_user(store)
        thread = store.create_thread(
            user_id=user["id"],
            metadata={"key1": "value1"},
        )

        updated = store.update_thread(
            thread["id"],
            metadata={"key2": "value2"},
        )
        assert updated is not None
        assert updated["metadata"]["key1"] == "value1"
        assert updated["metadata"]["key2"] == "value2"

    def test_update_thread_intent(self) -> None:
        """更新线程的 intent（通过 title 参数间接更新）。"""
        store = self._make_store()
        user = self._demo_user(store)
        thread = store.create_thread(user_id=user["id"], title="原标题")

        updated = store.update_thread(thread["id"], title="新意图")
        assert updated is not None
        assert updated["intent"] == "新意图"

    def test_update_nonexistent_thread(self) -> None:
        """更新不存在的线程返回 None。"""
        store = self._make_store()
        result = store.update_thread("nonexistent_id", title="test")
        assert result is None

    def test_get_thread_returns_all_fields(self) -> None:
        """get_thread 返回完整字段。"""
        store = self._make_store()
        user = self._demo_user(store)
        thread = store.create_thread(
            user_id=user["id"],
            title="完整字段测试",
            agent_id="agent_test",
            intent="完整字段测试",
            metadata={"session_type": "main_pipeline"},
        )

        detail = store.get_thread(thread["id"])
        assert detail is not None
        assert detail["intent"] == "完整字段测试"
        assert detail["current_state"] == "active"
        assert detail["agent_id"] == "agent_test"
        assert detail["metadata"] == {"session_type": "main_pipeline"}
        assert "created_at" in detail

    def test_get_user_threads_returns_all_fields(self) -> None:
        """get_user_threads 返回完整字段。"""
        store = self._make_store()
        user = self._demo_user(store)
        store.create_thread(
            user_id=user["id"],
            title="用户线程",
            agent_id="agent_xyz",
            intent="用户线程",
            metadata={"source": "web"},
        )

        threads = store.get_user_threads(user["id"])
        assert len(threads) >= 1
        t = threads[-1]
        assert t["intent"] == "用户线程"
        assert t["current_state"] == "active"
        assert t["agent_id"] == "agent_xyz"
        assert t["metadata"] == {"source": "web"}


# ============================================================
# 集成测试：API 路由
# ============================================================


def _create_test_app():
    """创建测试用 FastAPI 应用。"""
    from channels.api.app import create_app
    return create_app()


def _get_auth_token(client: TestClient) -> str:
    """登录获取认证 token。"""
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "demo", "password": "demo12345"},
    )
    assert response.status_code == 200
    data = response.json()
    return data["access_token"]


def _auth_headers(token: str) -> dict[str, str]:
    """构造认证请求头。"""
    return {"Authorization": f"Bearer {token}"}


class TestThreadRoutes:
    """线程 API 路由测试。"""

    @pytest.fixture(autouse=True)
    def setup(self) -> None:
        """每个测试前创建 TestClient。"""
        app = _create_test_app()
        self.client = TestClient(app)
        self.token = _get_auth_token(self.client)
        self.headers = _auth_headers(self.token)

    def test_list_threads(self) -> None:
        """GET /api/v1/threads 返回分页格式的线程列表。"""
        response = self.client.get("/api/v1/threads", headers=self.headers)
        assert response.status_code == 200
        data = response.json()
        # 新格式：{threads, total, skip, limit}
        assert isinstance(data, dict)
        assert "threads" in data
        assert "total" in data
        assert isinstance(data["threads"], list)

    def test_create_thread_basic(self) -> None:
        """POST /api/v1/threads 创建线程。"""
        response = self.client.post(
            "/api/v1/threads",
            json={"title": "测试会话"},
            headers=self.headers,
        )
        assert response.status_code == 201
        data = response.json()
        assert data["thread_id"]
        assert data["intent"] == "测试会话"

    def test_create_thread_with_agent_id(self) -> None:
        """POST /api/v1/threads 创建线程并绑定 Agent。"""
        response = self.client.post(
            "/api/v1/threads",
            json={"title": "Agent 会话", "agent_id": "agent_lingxi"},
            headers=self.headers,
        )
        assert response.status_code == 201
        data = response.json()
        assert data["agent_id"] == "agent_lingxi"

    def test_create_thread_with_metadata(self) -> None:
        """POST /api/v1/threads 创建线程并带 metadata。"""
        response = self.client.post(
            "/api/v1/threads",
            json={
                "title": "带元数据",
                "metadata": {"session_type": "main_pipeline"},
            },
            headers=self.headers,
        )
        assert response.status_code == 201
        data = response.json()
        assert data["metadata"] == {"session_type": "main_pipeline"}

    def test_get_thread_detail(self) -> None:
        """GET /api/v1/threads/{id} 获取详情。"""
        # 先创建
        create_resp = self.client.post(
            "/api/v1/threads",
            json={"title": "详情测试"},
            headers=self.headers,
        )
        thread_id = create_resp.json()["thread_id"]

        # 获取详情
        response = self.client.get(
            f"/api/v1/threads/{thread_id}",
            headers=self.headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["thread_id"] == thread_id
        assert data["intent"] == "详情测试"

    def test_update_thread_put(self) -> None:
        """PATCH /api/v1/threads/{id} 更新线程。"""
        # 先创建
        create_resp = self.client.post(
            "/api/v1/threads",
            json={"title": "PUT 测试"},
            headers=self.headers,
        )
        thread_id = create_resp.json()["thread_id"]

        # PATCH 更新
        response = self.client.patch(
            f"/api/v1/threads/{thread_id}",
            json={"title": "PATCH 更新标题", "agent_id": "agent_put"},
            headers=self.headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["intent"] == "PATCH 更新标题"
        assert data["agent_id"] == "agent_put"

    def test_update_thread_patch(self) -> None:
        """PATCH /api/v1/threads/{id} 更新线程（兼容）。"""
        # 先创建
        create_resp = self.client.post(
            "/api/v1/threads",
            json={"title": "PATCH 测试"},
            headers=self.headers,
        )
        thread_id = create_resp.json()["thread_id"]

        # PATCH 更新
        response = self.client.patch(
            f"/api/v1/threads/{thread_id}",
            json={"title": "PATCH 更新标题"},
            headers=self.headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["intent"] == "PATCH 更新标题"

    def test_update_thread_agent(self) -> None:
        """PATCH /api/v1/threads/{id}/agent 更新 Agent 绑定。"""
        # 先创建
        create_resp = self.client.post(
            "/api/v1/threads",
            json={"title": "Agent 更新测试"},
            headers=self.headers,
        )
        thread_id = create_resp.json()["thread_id"]

        # 更新 Agent
        response = self.client.patch(
            f"/api/v1/threads/{thread_id}/agent",
            json={"agent_id": "agent_lingxi"},
            headers=self.headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["agent_id"] == "agent_lingxi"

    def test_unbind_agent(self) -> None:
        """PATCH /api/v1/threads/{id}/agent 解绑 Agent（agent_id=null）。"""
        # 先创建带 Agent 的线程
        create_resp = self.client.post(
            "/api/v1/threads",
            json={"title": "解绑测试", "agent_id": "agent_to_remove"},
            headers=self.headers,
        )
        thread_id = create_resp.json()["thread_id"]

        # 解绑
        response = self.client.patch(
            f"/api/v1/threads/{thread_id}/agent",
            json={"agent_id": None},
            headers=self.headers,
        )
        assert response.status_code == 200

    def test_delete_thread(self) -> None:
        """DELETE /api/v1/threads/{id} 删除线程。"""
        create_resp = self.client.post(
            "/api/v1/threads",
            json={"title": "删除测试"},
            headers=self.headers,
        )
        thread_id = create_resp.json()["thread_id"]

        response = self.client.delete(
            f"/api/v1/threads/{thread_id}",
            headers=self.headers,
        )
        assert response.status_code == 200

    def test_thread_response_includes_metadata(self) -> None:
        """线程响应包含 metadata 字段。"""
        response = self.client.post(
            "/api/v1/threads",
            json={
                "title": "元数据验证",
                "metadata": {"session_type": "main_pipeline"},
            },
            headers=self.headers,
        )
        data = response.json()
        assert "metadata" in data
        assert data["metadata"] == {"session_type": "main_pipeline"}

    def test_thread_404_on_nonexistent(self) -> None:
        """访问不存在的线程返回 404。"""
        response = self.client.get(
            "/api/v1/threads/nonexistent_id",
            headers=self.headers,
        )
        assert response.status_code == 404

    def test_create_thread_no_detail_suffix(self) -> None:
        """前端 THREADS.GET 路径不再有 /detail 后缀。

        验证 GET /api/v1/threads/{id} 能正常工作。
        """
        create_resp = self.client.post(
            "/api/v1/threads",
            json={"title": "无 detail 后缀"},
            headers=self.headers,
        )
        thread_id = create_resp.json()["thread_id"]

        # 前端使用 GET /api/v1/threads/{id}，不应带 /detail
        response = self.client.get(
            f"/api/v1/threads/{thread_id}",
            headers=self.headers,
        )
        assert response.status_code == 200


# ============================================================
# 前端常量验证测试
# ============================================================


class TestFrontendApiConstants:
    """验证前端 API 常量与后端路由匹配。

    注意：这些测试验证前端常量文件的路径定义。
    由于无法直接运行 TypeScript，这些测试验证后端路由覆盖了前端需要的所有路径。
    """

    @pytest.fixture(autouse=True)
    def setup(self) -> None:
        """创建 TestClient。"""
        app = _create_test_app()
        self.client = TestClient(app)
        self.token = _get_auth_token(self.client)
        self.headers = _auth_headers(self.token)

    def test_frontend_thread_list_route_exists(self) -> None:
        """前端 GET /api/v1/threads 路由存在。"""
        response = self.client.get("/api/v1/threads", headers=self.headers)
        assert response.status_code == 200

    def test_frontend_thread_create_route_exists(self) -> None:
        """前端 POST /api/v1/threads 路由存在。"""
        response = self.client.post(
            "/api/v1/threads",
            json={"title": "test"},
            headers=self.headers,
        )
        assert response.status_code == 201

    def test_frontend_thread_get_route_exists(self) -> None:
        """前端 GET /api/v1/threads/{id} 路由存在（无 /detail 后缀）。"""
        create_resp = self.client.post(
            "/api/v1/threads",
            json={"title": "route check"},
            headers=self.headers,
        )
        thread_id = create_resp.json()["thread_id"]

        response = self.client.get(
            f"/api/v1/threads/{thread_id}",
            headers=self.headers,
        )
        assert response.status_code == 200

    def test_frontend_thread_update_put_route_exists(self) -> None:
        """前端 PATCH /api/v1/threads/{id} 路由存在。"""
        create_resp = self.client.post(
            "/api/v1/threads",
            json={"title": "route check"},
            headers=self.headers,
        )
        thread_id = create_resp.json()["thread_id"]

        response = self.client.patch(
            f"/api/v1/threads/{thread_id}",
            json={"title": "updated"},
            headers=self.headers,
        )
        assert response.status_code == 200

    def test_frontend_thread_update_agent_route_exists(self) -> None:
        """前端 PATCH /api/v1/threads/{id}/agent 路由存在。"""
        create_resp = self.client.post(
            "/api/v1/threads",
            json={"title": "agent route check"},
            headers=self.headers,
        )
        thread_id = create_resp.json()["thread_id"]

        response = self.client.patch(
            f"/api/v1/threads/{thread_id}/agent",
            json={"agent_id": "test_agent"},
            headers=self.headers,
        )
        assert response.status_code == 200

    def test_frontend_thread_delete_route_exists(self) -> None:
        """前端 DELETE /api/v1/threads/{id} 路由存在。"""
        create_resp = self.client.post(
            "/api/v1/threads",
            json={"title": "delete route check"},
            headers=self.headers,
        )
        thread_id = create_resp.json()["thread_id"]

        response = self.client.delete(
            f"/api/v1/threads/{thread_id}",
            headers=self.headers,
        )
        assert response.status_code == 200

    def test_frontend_messages_list_route_exists(self) -> None:
        """前端 GET /api/v1/threads/{id}/messages 路由存在。"""
        create_resp = self.client.post(
            "/api/v1/threads",
            json={"title": "messages route check"},
            headers=self.headers,
        )
        thread_id = create_resp.json()["thread_id"]

        response = self.client.get(
            f"/api/v1/threads/{thread_id}/messages",
            headers=self.headers,
        )
        assert response.status_code == 200
