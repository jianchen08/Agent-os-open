"""消息分页 API 测试。

验证 list_messages 端点的倒序分页功能：
1. 默认返回最后 N 条消息（倒序初始加载）
2. before_sequence 参数实现游标分页（加载更多历史）
3. 响应格式包含 messages、total、has_more
4. limit 参数控制每页数量
5. 无消息时返回空列表
6. 兼容旧版前端（无分页参数时也返回新格式）
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


def _create_thread_with_messages(
    client: TestClient,
    headers: dict[str, str],
    message_count: int,
) -> str:
    """创建线程并添加指定数量的消息。

    通过 MemoryStore 直接添加消息（带 sequence 字段），
    以便测试分页逻辑。

    Args:
        client: TestClient 实例
        headers: 认证请求头
        message_count: 要添加的消息数量

    Returns:
        创建的线程 ID
    """
    from channels.api.memory_store import store

    # 创建线程
    response = client.post(
        "/api/v1/threads",
        json={"title": "分页测试"},
        headers=headers,
    )
    assert response.status_code == 201
    thread_id = response.json()["thread_id"]

    # 直接通过 MemoryStore 添加带 sequence 的消息
    for i in range(message_count):
        store.add_message(
            thread_id=thread_id,
            message_id=f"msg_{i:03d}",
            role="user" if i % 2 == 0 else "assistant",
            content=f"消息 #{i}",
            sequence=i,
        )

    return thread_id


class TestMessagePaginationResponseFormat:
    """验证分页响应格式。"""

    @pytest.fixture(autouse=True)
    def setup(self) -> None:
        """每个测试前创建 TestClient。"""
        app = _create_test_app()
        self.client = TestClient(app)
        self.token = _get_auth_token(self.client)
        self.headers = _auth_headers(self.token)

    def test_response_has_messages_total_has_more(self) -> None:
        """响应包含 messages、total、has_more 字段。"""
        thread_id = _create_thread_with_messages(self.client, self.headers, 5)

        response = self.client.get(
            f"/api/v1/threads/{thread_id}/messages",
            headers=self.headers,
        )
        assert response.status_code == 200
        data = response.json()

        assert "messages" in data
        assert "total" in data
        assert "has_more" in data

    def test_messages_is_list(self) -> None:
        """messages 字段是列表。"""
        thread_id = _create_thread_with_messages(self.client, self.headers, 3)

        response = self.client.get(
            f"/api/v1/threads/{thread_id}/messages",
            headers=self.headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data["messages"], list)

    def test_total_is_int(self) -> None:
        """total 字段是整数。"""
        thread_id = _create_thread_with_messages(self.client, self.headers, 5)

        response = self.client.get(
            f"/api/v1/threads/{thread_id}/messages",
            headers=self.headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data["total"], int)

    def test_has_more_is_bool(self) -> None:
        """has_more 字段是布尔值。"""
        thread_id = _create_thread_with_messages(self.client, self.headers, 5)

        response = self.client.get(
            f"/api/v1/threads/{thread_id}/messages",
            headers=self.headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data["has_more"], bool)


class TestMessagePaginationDefaultBehavior:
    """默认行为：返回最后 N 条消息。"""

    @pytest.fixture(autouse=True)
    def setup(self) -> None:
        """每个测试前创建 TestClient。"""
        app = _create_test_app()
        self.client = TestClient(app)
        self.token = _get_auth_token(self.client)
        self.headers = _auth_headers(self.token)

    def test_default_returns_last_20_messages(self) -> None:
        """不传 limit 时默认返回最后 20 条消息。"""
        thread_id = _create_thread_with_messages(self.client, self.headers, 25)

        response = self.client.get(
            f"/api/v1/threads/{thread_id}/messages",
            headers=self.headers,
        )
        assert response.status_code == 200
        data = response.json()

        assert len(data["messages"]) == 20
        assert data["total"] == 25
        assert data["has_more"] is True

    def test_default_returns_last_n_with_limit(self) -> None:
        """传 limit=5 时返回最后 5 条消息。"""
        thread_id = _create_thread_with_messages(self.client, self.headers, 10)

        response = self.client.get(
            f"/api/v1/threads/{thread_id}/messages",
            params={"limit": 5},
            headers=self.headers,
        )
        assert response.status_code == 200
        data = response.json()

        assert len(data["messages"]) == 5
        assert data["total"] == 10
        assert data["has_more"] is True

    def test_messages_ordered_oldest_first(self) -> None:
        """返回的消息按时间正序排列（最旧在前）。"""
        thread_id = _create_thread_with_messages(self.client, self.headers, 10)

        response = self.client.get(
            f"/api/v1/threads/{thread_id}/messages",
            params={"limit": 5},
            headers=self.headers,
        )
        assert response.status_code == 200
        data = response.json()
        messages = data["messages"]

        # 应该是 msg_005 ~ msg_009，按 sequence 正序
        assert len(messages) == 5
        assert messages[0]["content"] == "消息 #5"
        assert messages[4]["content"] == "消息 #9"

    def test_no_messages_returns_empty(self) -> None:
        """线程无消息时返回空列表。"""
        response = self.client.post(
            "/api/v1/threads",
            json={"title": "空线程"},
            headers=self.headers,
        )
        thread_id = response.json()["thread_id"]

        resp = self.client.get(
            f"/api/v1/threads/{thread_id}/messages",
            headers=self.headers,
        )
        assert resp.status_code == 200
        data = resp.json()

        assert data["messages"] == []
        assert data["total"] == 0
        assert data["has_more"] is False

    def test_fewer_messages_than_limit(self) -> None:
        """消息数量不足 limit 时返回全部，has_more=False。"""
        thread_id = _create_thread_with_messages(self.client, self.headers, 3)

        response = self.client.get(
            f"/api/v1/threads/{thread_id}/messages",
            params={"limit": 20},
            headers=self.headers,
        )
        assert response.status_code == 200
        data = response.json()

        assert len(data["messages"]) == 3
        assert data["total"] == 3
        assert data["has_more"] is False

    def test_exact_limit_messages(self) -> None:
        """消息数量恰好等于 limit，has_more=False。"""
        thread_id = _create_thread_with_messages(self.client, self.headers, 5)

        response = self.client.get(
            f"/api/v1/threads/{thread_id}/messages",
            params={"limit": 5},
            headers=self.headers,
        )
        assert response.status_code == 200
        data = response.json()

        assert len(data["messages"]) == 5
        assert data["total"] == 5
        assert data["has_more"] is False


class TestMessagePaginationBeforeSequence:
    """before_sequence 游标分页。"""

    @pytest.fixture(autouse=True)
    def setup(self) -> None:
        """每个测试前创建 TestClient。"""
        app = _create_test_app()
        self.client = TestClient(app)
        self.token = _get_auth_token(self.client)
        self.headers = _auth_headers(self.token)

    def test_before_sequence_returns_older_messages(self) -> None:
        """before_sequence=5 返回 sequence < 5 的最后 limit 条消息。"""
        thread_id = _create_thread_with_messages(self.client, self.headers, 10)

        response = self.client.get(
            f"/api/v1/threads/{thread_id}/messages",
            params={"limit": 3, "before_sequence": 5},
            headers=self.headers,
        )
        assert response.status_code == 200
        data = response.json()

        # sequence < 5 的消息是 msg_000 ~ msg_004 (5条)
        # 取最后 3 条: msg_002, msg_003, msg_004
        assert len(data["messages"]) == 3
        assert data["messages"][0]["content"] == "消息 #2"
        assert data["messages"][2]["content"] == "消息 #4"
        assert data["total"] == 10
        assert data["has_more"] is True

    def test_before_sequence_all_fetched(self) -> None:
        """before_sequence 指向最早消息时 has_more=False。"""
        thread_id = _create_thread_with_messages(self.client, self.headers, 5)

        response = self.client.get(
            f"/api/v1/threads/{thread_id}/messages",
            params={"limit": 10, "before_sequence": 3},
            headers=self.headers,
        )
        assert response.status_code == 200
        data = response.json()

        # sequence < 3 的消息是 msg_000, msg_001, msg_002 (3条)
        assert len(data["messages"]) == 3
        assert data["has_more"] is False

    def test_before_sequence_first_page(self) -> None:
        """先获取最后 5 条，然后用最旧消息的 sequence 加载更多。"""
        thread_id = _create_thread_with_messages(self.client, self.headers, 15)

        # 第一页: 最后 5 条
        resp1 = self.client.get(
            f"/api/v1/threads/{thread_id}/messages",
            params={"limit": 5},
            headers=self.headers,
        )
        data1 = resp1.json()
        assert len(data1["messages"]) == 5
        # msg_010 ~ msg_014
        oldest_sequence = data1["messages"][0]["sequence"]
        assert oldest_sequence == 10

        # 第二页: sequence < 10 的最后 5 条
        resp2 = self.client.get(
            f"/api/v1/threads/{thread_id}/messages",
            params={"limit": 5, "before_sequence": oldest_sequence},
            headers=self.headers,
        )
        data2 = resp2.json()
        assert len(data2["messages"]) == 5
        # msg_005 ~ msg_009
        assert data2["messages"][0]["content"] == "消息 #5"
        assert data2["messages"][4]["content"] == "消息 #9"
        assert data2["has_more"] is True

        # 第三页: sequence < 5 的最后 5 条
        oldest_seq2 = data2["messages"][0]["sequence"]
        resp3 = self.client.get(
            f"/api/v1/threads/{thread_id}/messages",
            params={"limit": 5, "before_sequence": oldest_seq2},
            headers=self.headers,
        )
        data3 = resp3.json()
        assert len(data3["messages"]) == 5
        # msg_000 ~ msg_004
        assert data3["messages"][0]["content"] == "消息 #0"
        assert data3["messages"][4]["content"] == "消息 #4"
        assert data3["has_more"] is False

    def test_before_sequence_exceeds_total(self) -> None:
        """before_sequence 大于所有消息的 sequence 时返回全部。"""
        thread_id = _create_thread_with_messages(self.client, self.headers, 3)

        response = self.client.get(
            f"/api/v1/threads/{thread_id}/messages",
            params={"limit": 20, "before_sequence": 100},
            headers=self.headers,
        )
        assert response.status_code == 200
        data = response.json()

        # 所有消息的 sequence < 100，返回全部 3 条
        assert len(data["messages"]) == 3
        assert data["has_more"] is False

    def test_before_sequence_zero_returns_empty(self) -> None:
        """before_sequence=0 没有消息满足条件。"""
        thread_id = _create_thread_with_messages(self.client, self.headers, 5)

        response = self.client.get(
            f"/api/v1/threads/{thread_id}/messages",
            params={"limit": 20, "before_sequence": 0},
            headers=self.headers,
        )
        assert response.status_code == 200
        data = response.json()

        # 没有消息的 sequence < 0
        assert data["messages"] == []
        assert data["total"] == 5
        assert data["has_more"] is False


class TestMessagePaginationEdgeCases:
    """边界情况。"""

    @pytest.fixture(autouse=True)
    def setup(self) -> None:
        """每个测试前创建 TestClient。"""
        app = _create_test_app()
        self.client = TestClient(app)
        self.token = _get_auth_token(self.client)
        self.headers = _auth_headers(self.token)

    def test_nonexistent_thread_returns_404(self) -> None:
        """访问不存在的线程返回 404。"""
        response = self.client.get(
            "/api/v1/threads/nonexistent_thread/messages",
            headers=self.headers,
        )
        assert response.status_code == 404

    def test_limit_max_100(self) -> None:
        """limit 最大值为 100。"""
        thread_id = _create_thread_with_messages(self.client, self.headers, 5)

        response = self.client.get(
            f"/api/v1/threads/{thread_id}/messages",
            params={"limit": 101},
            headers=self.headers,
        )
        # FastAPI 应该返回 422 验证错误
        assert response.status_code == 422

    def test_limit_min_1(self) -> None:
        """limit 最小值为 1。"""
        thread_id = _create_thread_with_messages(self.client, self.headers, 5)

        response = self.client.get(
            f"/api/v1/threads/{thread_id}/messages",
            params={"limit": 0},
            headers=self.headers,
        )
        assert response.status_code == 422

    def test_single_message_thread(self) -> None:
        """只有一条消息的线程。"""
        thread_id = _create_thread_with_messages(self.client, self.headers, 1)

        response = self.client.get(
            f"/api/v1/threads/{thread_id}/messages",
            headers=self.headers,
        )
        assert response.status_code == 200
        data = response.json()

        assert len(data["messages"]) == 1
        assert data["total"] == 1
        assert data["has_more"] is False

    def test_pipeline_run_id_param_accepted(self) -> None:
        """pipeline_run_id 参数被接受且不报错。"""
        thread_id = _create_thread_with_messages(self.client, self.headers, 3)

        # 传入 pipeline_run_id 参数，应返回有效响应格式
        response = self.client.get(
            f"/api/v1/threads/{thread_id}/messages",
            params={"pipeline_run_id": "nonexistent_pipeline"},
            headers=self.headers,
        )
        assert response.status_code == 200
        data = response.json()

        # 响应格式正确（无 exec_storage 时回退到 MemoryStore）
        assert "messages" in data
        assert "total" in data
        assert "has_more" in data
        assert isinstance(data["messages"], list)


class TestMessagePaginationMessageFields:
    """验证分页响应中消息字段的完整性。"""

    @pytest.fixture(autouse=True)
    def setup(self) -> None:
        """每个测试前创建 TestClient。"""
        app = _create_test_app()
        self.client = TestClient(app)
        self.token = _get_auth_token(self.client)
        self.headers = _auth_headers(self.token)

    def test_message_has_sequence_field(self) -> None:
        """每条消息都包含 sequence 字段。"""
        thread_id = _create_thread_with_messages(self.client, self.headers, 5)

        response = self.client.get(
            f"/api/v1/threads/{thread_id}/messages",
            params={"limit": 3},
            headers=self.headers,
        )
        assert response.status_code == 200
        data = response.json()

        for msg in data["messages"]:
            assert "sequence" in msg
            assert isinstance(msg["sequence"], int)

    def test_message_has_required_fields(self) -> None:
        """每条消息都包含必要字段。"""
        thread_id = _create_thread_with_messages(self.client, self.headers, 3)

        response = self.client.get(
            f"/api/v1/threads/{thread_id}/messages",
            headers=self.headers,
        )
        assert response.status_code == 200
        data = response.json()

        for msg in data["messages"]:
            assert "id" in msg
            assert "thread_id" in msg
            assert "role" in msg
            assert "content" in msg
            assert "timestamp" in msg
