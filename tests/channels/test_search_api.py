"""搜索 API 端点测试（P2 搜索框合并-后端部分）。

覆盖 /api/v1/search 端点：
- type=session: 按标题/意图搜索会话（LIKE 语义，大小写不敏感）
- type=message: 按内容搜索消息（执行记录 content 子串匹配）
- type=all: 同时返回会话与消息
- 参数校验与认证

存储事实：本项目会话存于 MemoryStore（JSON 持久化），消息存于
ExecutionRecordStorage（YAML 分片）。搜索 API 对接真实存储层做子串匹配。
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from channels.api.deps import APIError, api_error_handler, require_auth
from channels.api.routes_search import router


@pytest.fixture
def mock_auth():
    """覆盖认证依赖，模拟已登录用户。"""

    async def _mock_auth():
        return {"sub": "test_user", "username": "tester"}

    return _mock_auth


@pytest.fixture
def client(mock_auth):
    """创建 FastAPI TestClient，覆盖认证与异常处理。"""
    app = FastAPI()
    app.dependency_overrides[require_auth] = mock_auth
    app.add_exception_handler(APIError, api_error_handler)
    app.include_router(router)
    with TestClient(app) as c:
        yield c


def _make_thread(thread_id: str, title: str, user_id: str = "test_user") -> dict:
    return {
        "id": thread_id,
        "user_id": user_id,
        "title": title,
        "intent": title,
        "agent_id": "agentos",
        "metadata": {"session_type": "main_pipeline"},
        "current_state": "active",
        "pipeline_ids": [],
        "active_pipeline_id": "",
        "created_at": "2026-08-01T00:00:00+00:00",
        "updated_at": "2026-08-01T00:00:00+00:00",
    }


class _FakeRecord:
    """模拟 ExecutionRecordData 的最小对象。"""

    def __init__(self, record_id: str, pipeline_run_id: str, content: str, rtype: str = "ai"):
        self.record_id = record_id
        self.pipeline_run_id = pipeline_run_id
        self.content = content
        self.type = rtype
        self.role = "assistant" if rtype == "ai" else "user"
        self.sequence = 1
        self.created_at = "2026-08-01T00:00:00"


class _FakeStorage:
    """模拟 ExecutionRecordStorage，提供 records 遍历与 search_records。"""

    def __init__(self, records: list[_FakeRecord]):
        self._records = {f"{r.record_id}::{r.sequence}": r for r in records}

    def search_records(self, keyword: str, limit: int = 50) -> list[_FakeRecord]:
        needle = keyword.lower()
        hits = [
            r for r in self._records.values() if needle in (r.content or "").lower()
        ]
        return hits[:limit]


# ── type=session ──────────────────────────────────────────────


class TestSearchSessions:
    """会话标题/意图搜索。"""

    def test_matches_title_case_insensitive(self, client):
        with patch("channels.api.routes_search._search_sessions", return_value=[
            {"id": "t1", "title": "项目计划讨论", "updated_at": "", "message_count": 3},
        ]) as mock_fn:
            resp = client.get("/api/v1/search", params={"q": "计划", "type": "session"})
            assert resp.status_code == 200
            data = resp.json()
            assert data["type"] == "session"
            assert len(data["sessions"]) == 1
            assert data["sessions"][0]["id"] == "t1"
            assert data["sessions"][0]["title"] == "项目计划讨论"
            assert data["messages"] == []
            mock_fn.assert_called_once()

    def test_no_match_returns_empty(self, client):
        with patch("channels.api.routes_search._search_sessions", return_value=[]):
            resp = client.get("/api/v1/search", params={"q": "不存在的关键词", "type": "session"})
            assert resp.status_code == 200
            data = resp.json()
            assert data["sessions"] == []


# ── type=message ──────────────────────────────────────────────


class TestSearchMessages:
    """消息内容搜索。"""

    def test_matches_content_case_insensitive(self, client):
        with patch(
            "channels.api.routes_search._search_messages",
            return_value=[
                {
                    "id": "r2",
                    "session_id": "pipe-1",
                    "role": "assistant",
                    "content": "分析结果：存在内存泄漏",
                    "timestamp": "",
                    "sequence": 1,
                }
            ],
        ) as mock_fn:
            resp = client.get("/api/v1/search", params={"q": "内存泄漏", "type": "message"})
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["messages"]) == 1
            assert data["messages"][0]["id"] == "r2"
            assert data["messages"][0]["session_id"] == "pipe-1"
            assert data["sessions"] == []
            mock_fn.assert_called_once()

    def test_no_match_returns_empty(self, client):
        with patch("channels.api.routes_search._search_messages", return_value=[]):
            resp = client.get("/api/v1/search", params={"q": "不存在的内容", "type": "message"})
            assert resp.status_code == 200
            assert resp.json()["messages"] == []


# ── type=all ──────────────────────────────────────────────────


class TestSearchAll:
    """同时搜索会话与消息。"""

    def test_returns_both(self, client):
        with (
            patch(
                "channels.api.routes_search._search_sessions",
                return_value=[{"id": "t1", "title": "测试", "updated_at": "", "message_count": 1}],
            ),
            patch(
                "channels.api.routes_search._search_messages",
                return_value=[{"id": "r1", "session_id": "pipe-1", "role": "assistant", "content": "测试内容", "timestamp": "", "sequence": 1}],
            ),
        ):
            resp = client.get("/api/v1/search", params={"q": "测试", "type": "all"})
            assert resp.status_code == 200
            data = resp.json()
            assert data["type"] == "all"
            assert len(data["sessions"]) == 1
            assert len(data["messages"]) == 1


# ── 参数校验与认证 ─────────────────────────────────────────────


class TestSearchValidation:
    """参数校验与认证。"""

    def test_empty_q_returns_empty(self, client):
        resp = client.get("/api/v1/search", params={"q": "", "type": "session"})
        assert resp.status_code == 200
        assert resp.json()["sessions"] == []
        assert resp.json()["messages"] == []

    def test_invalid_type_rejected(self, client):
        resp = client.get("/api/v1/search", params={"q": "x", "type": "invalid"})
        assert resp.status_code == 422

    def test_unauthorized_returns_401(self):
        app = FastAPI()
        app.add_exception_handler(APIError, api_error_handler)
        app.include_router(router)
        with TestClient(app) as c:
            resp = c.get("/api/v1/search", params={"q": "x"})
            assert resp.status_code == 401


# ── 存储层搜索辅助函数 ─────────────────────────────────────────


class TestSearchHelpers:
    """存储层搜索辅助函数（_search_sessions / _search_messages）。"""

    def test_search_sessions_filters_by_user_and_title(self):
        store = type("FakeStore", (), {"threads": {}})()
        store.threads = {
            "t1": _make_thread("t1", "项目计划讨论", user_id="test_user"),
            "t2": _make_thread("t2", "计划周报", user_id="other_user"),
            "t3": _make_thread("t3", "无关话题", user_id="test_user"),
        }
        with patch("channels.api.memory_store.store", store):
            from channels.api.routes_search import _search_sessions

            hits = _search_sessions("test_user", "计划", limit=10)
            ids = [h["id"] for h in hits]
            assert ids == ["t1"]  # t2 属 other_user，不返回

    def test_search_messages_uses_storage(self):
        storage = _FakeStorage(
            [
                _FakeRecord("r1", "pipe-1", "分析代码性能"),
                _FakeRecord("r2", "pipe-2", "无关内容"),
            ]
        )
        with patch("channels.api.routes_search._get_storage", return_value=storage):
            from channels.api.routes_search import _search_messages

            hits = _search_messages("代码", limit=10)
            assert len(hits) == 1
            assert hits[0]["id"] == "r1"
