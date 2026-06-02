"""线程列表分页和线程历史分页测试。

覆盖：
1. list_threads 端点的 skip/limit 分页
2. 响应格式从 list[ThreadResponse] 变更为 {threads, total, skip, limit}
3. get_thread_history 端点的 limit/before_sequence 游标分页
4. 前端向后兼容性验证
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


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


def _create_threads(client: TestClient, headers: dict, count: int) -> list[str]:
    """批量创建线程，返回 thread_id 列表。"""
    thread_ids: list[str] = []
    for i in range(count):
        resp = client.post(
            "/api/v1/threads",
            json={"title": f"线程_{i:03d}", "metadata": {"session_type": "main_pipeline"}},
            headers=headers,
        )
        assert resp.status_code == 201
        thread_ids.append(resp.json()["thread_id"])
    return thread_ids


# ============================================================
# list_threads 分页测试
# ============================================================


class TestListThreadsPagination:
    """list_threads 端点分页功能测试。"""

    @pytest.fixture(autouse=True)
    def setup(self) -> None:
        """每个测试前创建 TestClient。"""
        app = _create_test_app()
        self.client = TestClient(app)
        self.token = _get_auth_token(self.client)
        self.headers = _auth_headers(self.token)

    def test_list_threads_returns_paginated_format(self) -> None:
        """list_threads 返回 {threads, total, skip, limit} 格式而非裸列表。"""
        _create_threads(self.client, self.headers, 3)

        response = self.client.get("/api/v1/threads", headers=self.headers)
        assert response.status_code == 200
        data = response.json()

        # 新格式必须是 dict，不是 list
        assert isinstance(data, dict)
        assert "threads" in data
        assert "total" in data
        assert "skip" in data
        assert "limit" in data
        assert isinstance(data["threads"], list)
        assert isinstance(data["total"], int)

    def test_list_threads_total_reflects_all_threads(self) -> None:
        """total 字段反映用户所有线程总数，不受分页影响。"""
        _create_threads(self.client, self.headers, 5)

        response = self.client.get(
            "/api/v1/threads",
            params={"limit": 2},
            headers=self.headers,
        )
        assert response.status_code == 200
        data = response.json()

        # total 应该是全部数量（>=5，因为 demo 用户可能有其他线程）
        assert data["total"] >= 5
        # 但返回的 threads 数量受 limit 限制
        assert len(data["threads"]) <= 2

    def test_list_threads_skip_offset(self) -> None:
        """skip 参数控制偏移量。"""
        thread_ids = _create_threads(self.client, self.headers, 5)

        # 请求第一页
        page1 = self.client.get(
            "/api/v1/threads",
            params={"skip": 0, "limit": 3},
            headers=self.headers,
        )
        assert page1.status_code == 200
        page1_data = page1.json()

        # 请求第二页
        page2 = self.client.get(
            "/api/v1/threads",
            params={"skip": 3, "limit": 3},
            headers=self.headers,
        )
        assert page2.status_code == 200
        page2_data = page2.json()

        # 两页的 thread_id 不应完全重叠
        page1_ids = {t["thread_id"] for t in page1_data["threads"]}
        page2_ids = {t["thread_id"] for t in page2_data["threads"]}
        # 第二页的 ID 不在第一页中（可能有部分重叠在边界，但不应全部相同）
        # 当线程总数 > limit 时，两页必须有差异
        if page1_data["total"] > 3:
            assert not page2_ids.issubset(page1_ids), "第二页应包含第一页之外的线程"

    def test_list_threads_default_pagination(self) -> None:
        """不传分页参数时使用默认值。"""
        _create_threads(self.client, self.headers, 2)

        response = self.client.get("/api/v1/threads", headers=self.headers)
        assert response.status_code == 200
        data = response.json()

        assert data["limit"] == 100

    def test_list_threads_invalid_limit(self) -> None:
        """limit 超出范围时返回错误。"""
        # limit=0 应该被拒绝
        # 默认 skip=0, limit=100
        assert data["skip"] == 0
        assert data["limit"] == 100

    def test_list_threads_invalid_limit(self) -> None:
        """limit 超出范围时返回错误。"""
        # limit=0 应该被拒绝 (ge=1 约束)
        response = self.client.get(
            "/api/v1/threads",
            params={"limit": 0},
            headers=self.headers,
        )
        assert response.status_code == 400
        assert response.status_code in (400, 422)

        # limit=10000 应该被拒绝 (le=9999 约束)
        response = self.client.get(
            "/api/v1/threads",
            params={"limit": 10000},
            headers=self.headers,
        )
        assert response.status_code in (400, 422)

    def test_list_threads_negative_skip(self) -> None:
        """skip 为负数时返回错误。"""
        response = self.client.get(
            "/api/v1/threads",
            params={"skip": -1},
            headers=self.headers,
        )
        assert response.status_code == 422

    def test_list_threads_session_type_filter_preserved(self) -> None:
        """分页时 session_type 过滤仍然生效。"""
        _create_threads(self.client, self.headers, 3)

        response = self.client.get(
            "/api/v1/threads",
            params={"session_type": "main_pipeline", "limit": 2},
            headers=self.headers,
        )
        assert response.status_code == 200
        data = response.json()

        for thread in data["threads"]:
            meta = thread.get("metadata") or {}
            assert meta.get("session_type") == "main_pipeline"

    def test_list_threads_thread_response_fields(self) -> None:
        """分页响应中每个 thread 对象字段完整。"""
        _create_threads(self.client, self.headers, 1)

        response = self.client.get("/api/v1/threads", headers=self.headers)
        assert response.status_code == 200
        data = response.json()

        assert len(data["threads"]) >= 1
        thread = data["threads"][0]
        assert "thread_id" in thread
        assert "title" in thread
        assert "intent" in thread
        assert "created_at" in thread
        assert "updated_at" in thread

    def test_list_threads_skip_beyond_total(self) -> None:
        """skip 超过总数时返回空列表。"""
        _create_threads(self.client, self.headers, 2)

        response = self.client.get(
            "/api/v1/threads",
            params={"skip": 1000, "limit": 10},
            headers=self.headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["threads"] == []
        assert data["total"] >= 2  # total 仍然反映实际总数


# ============================================================
# get_thread_history 分页测试
# ============================================================


class TestThreadHistoryPagination:
    """get_thread_history 端点分页功能测试。"""

    @pytest.fixture(autouse=True)
    def setup(self) -> None:
        """每个测试前创建 TestClient。"""
        app = _create_test_app()
        self.client = TestClient(app)
        self.token = _get_auth_token(self.client)
        self.headers = _auth_headers(self.token)

    def _create_thread_with_messages(
        self, count: int
    ) -> str:
        """创建线程并添加 MemoryStore 消息，返回 thread_id。"""
        from channels.api.memory_store import store

        user = store.get_user_by_username("demo")
        assert user is not None
        thread = store.create_thread(
            user_id=user["id"],
            title="历史分页测试",
        )
        thread_id = thread["id"]

        for i in range(count):
            store.add_message(
                thread_id=thread_id,
                message_id=f"msg_{thread_id}_{i}",
                role="user" if i % 2 == 0 else "assistant",
                content=f"消息_{i:03d}",
                sequence=i,
            )
        return thread_id

    def test_history_returns_paginated_format(self) -> None:
        """history 端点返回带分页元数据的格式。"""
        thread_id = self._create_thread_with_messages(5)

        response = self.client.get(
            f"/api/v1/threads/{thread_id}/history",
            headers=self.headers,
        )
        assert response.status_code == 200
        data = response.json()

        # 必须包含分页字段
        assert "messages" in data
        assert "total" in data
        assert "has_more" in data
        assert isinstance(data["messages"], list)

    def test_history_limit_param(self) -> None:
        """limit 参数限制返回的消息数量。"""
        thread_id = self._create_thread_with_messages(10)

        response = self.client.get(
            f"/api/v1/threads/{thread_id}/history",
            params={"limit": 3},
            headers=self.headers,
        )
        assert response.status_code == 200
        data = response.json()

        assert len(data["messages"]) <= 3
        assert data["total"] == 10
        assert data["has_more"] is True

    def test_history_before_sequence_cursor(self) -> None:
        """before_sequence 游标分页：返回 sequence < before_sequence 的最新消息。"""
        thread_id = self._create_thread_with_messages(10)

        # 请求 sequence < 7 的消息，最多 3 条
        response = self.client.get(
            f"/api/v1/threads/{thread_id}/history",
            params={"before_sequence": 7, "limit": 3},
            headers=self.headers,
        )
        assert response.status_code == 200
        data = response.json()

        # 所有消息的 sequence < 7
        for msg in data["messages"]:
            assert msg["sequence"] < 7

    def test_history_default_no_pagination(self) -> None:
        """不传分页参数时返回全部消息（向后兼容）。"""
        thread_id = self._create_thread_with_messages(5)

        response = self.client.get(
            f"/api/v1/threads/{thread_id}/history",
            headers=self.headers,
        )
        assert response.status_code == 200
        data = response.json()

        # 无分页时返回全部
        assert len(data["messages"]) == 5
        assert data["total"] == 5
        assert data["has_more"] is False

    def test_history_nonexistent_thread_404(self) -> None:
        """访问不存在的线程返回 404。"""
        response = self.client.get(
            "/api/v1/threads/nonexistent_thread/history",
            headers=self.headers,
        )
        assert response.status_code == 404


# ============================================================
# 前端向后兼容性测试
# ============================================================


class TestFrontendBackwardCompatibility:
    """验证前端已有代码能与新的分页格式对接。"""

    @pytest.fixture(autouse=True)
    def setup(self) -> None:
        """每个测试前创建 TestClient。"""
        app = _create_test_app()
        self.client = TestClient(app)
        self.token = _get_auth_token(self.client)
        self.headers = _auth_headers(self.token)

    def test_frontend_skip_limit_params_accepted(self) -> None:
        """前端发送的 skip/limit 参数被后端接受。"""
        response = self.client.get(
            "/api/v1/threads",
            params={"skip": 0, "limit": 20, "session_type": "main_pipeline"},
            headers=self.headers,
        )
        assert response.status_code == 200

    def test_frontend_pagination_response_structure(self) -> None:
        """响应格式包含前端 getSessions 期望的 threads 和 total 字段。"""
        _create_threads(self.client, self.headers, 2)

        response = self.client.get(
            "/api/v1/threads",
            params={"skip": 0, "limit": 20},
            headers=self.headers,
        )
        data = response.json()

        # 前端 getSessions 通过 'threads' in response.data 判断新格式
        assert "threads" in data
        # 前端通过 total ?? threads.length 获取总数
        assert "total" in data
        assert data["total"] >= len(data["threads"])

    def test_thread_objects_have_required_fields(self) -> None:
        """分页返回的 thread 对象包含前端 mapThreadToSession 需要的字段。"""
        _create_threads(self.client, self.headers, 1)

        response = self.client.get("/api/v1/threads", headers=self.headers)
        data = response.json()

        assert len(data["threads"]) >= 1
        thread = data["threads"][0]
        # 前端 mapThreadToSession 依赖的字段
        required_fields = ["thread_id", "created_at", "updated_at"]
        for field in required_fields:
            assert field in thread, f"缺少前端必需字段: {field}"
