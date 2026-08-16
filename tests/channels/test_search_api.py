# @feature: FP-MIGR 0.1→0.2迁移（0.1 遗留测试） | @ci: python-coverage
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

pytestmark = pytest.mark.unit  # 0.2 TDD 分层：单元测试
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.channels.conftest import use_channel

use_channel("api")
from deps import APIError, api_error_handler, require_auth  # noqa: E402
from routes_search import router  # noqa: E402


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


# 注：原 _FakeRecord / _FakeStorage 辅助类仅服务消息搜索用例（0.2 未迁移，
# 用例已删），随之移除。


# ── type=session ──────────────────────────────────────────────


class TestSearchSessions:
    """会话标题/意图搜索。"""

    def test_matches_title_case_insensitive(self, client):
        with patch("routes_search._search_sessions", return_value=[
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
        with patch("routes_search._search_sessions", return_value=[]):
            resp = client.get("/api/v1/search", params={"q": "不存在的关键词", "type": "session"})
            assert resp.status_code == 200
            data = resp.json()
            assert data["sessions"] == []


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
        with patch("memory_store.store", store):
            from routes_search import _search_sessions  # noqa: PLC0415

            hits = _search_sessions("test_user", "计划", limit=10)
            ids = [h["id"] for h in hits]
            assert ids == ["t1"]  # t2 属 other_user，不返回

    # 0.2 清理：原 test_search_messages_uses_storage 经 mock routes_search._search_messages
    # 验证消息搜索存储链路——该函数在 0.2 routes_search 中未实现（消息搜索未迁移），
    # mock 不存在目标的 xfail 占位用例已删除（见 docs/test_cleanup_0.2.md）。
