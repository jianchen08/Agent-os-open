# @feature: FP-0.2.PACK 装后冒烟会话清理契约（用户裁定 2026-11-12） | @ci: python-test
"""post_install_smoke.py 清理契约单测（不连真内核，HTTP 层整体打桩）。

行为契约：
- 冒烟会话识别：title/intent/metadata 任一命中 post-install-smoke 即命中，
  普通会话不碰（历史残留按同口径清扫）；
- 历史清扫：列表 → 逐个 DELETE，删除数与失败详情如实返回；
- 本轮清理：聊天成功 / 聊天失败 / ws-ticket 失败三条路径都必须 DELETE 本轮
  会话；聊天失败时原异常优先返回（清理失败不得掩盖聊天失败），聊天成功而
  清理失败判红（会话列表被污染即冒烟未收口）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
pytestmark = pytest.mark.unit


def _load():
    spec = importlib.util.spec_from_file_location(
        "post_install_smoke", SCRIPTS / "post_install_smoke.py"
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load()


class FakeKernel:
    """最小 HTTP 面：按 (method, path) 应答，记录调用与删除顺序。"""

    def __init__(self, sessions: list[dict] | None = None, delete_status: int = 200, ticket_status: int = 200):
        self.sessions = sessions or []
        self.delete_status = delete_status
        self.ticket_status = ticket_status
        self.created_payloads: list[dict] = []
        self.deleted: list[str] = []

    def __call__(self, base, path, payload=None, token=None, method=None, timeout=15):
        effective = method or ("POST" if payload else "GET")
        if effective == "GET" and path == "/api/v1/sessions":
            return 200, {"threads": self.sessions, "total": len(self.sessions)}
        if effective == "POST" and path == "/api/v1/sessions":
            self.created_payloads.append(payload or {})
            return 200, {"thread_id": "t-new"}
        if effective == "POST" and path == "/api/v1/ws-ticket":
            if self.ticket_status != 200:
                return self.ticket_status, {"error": "ticket unavailable"}
            return 200, {"ticket": "ticket-1"}
        if effective == "DELETE" and path.startswith("/api/v1/sessions/"):
            thread_id = path.rsplit("/", 1)[-1]
            self.deleted.append(thread_id)
            return self.delete_status, {"deleted": self.delete_status == 200}
        raise AssertionError(f"未预期的请求：{effective} {path}")


def _install_fake(monkeypatch: pytest.MonkeyPatch, fake: FakeKernel) -> None:
    monkeypatch.setattr(mod, "_request", fake)


class TestSessionMarker:
    def test_title_intent_metadata_all_match(self):
        assert mod._session_is_smoke({"title": mod.SMOKE_TAG})
        assert mod._session_is_smoke({"intent": mod.SMOKE_TAG})
        assert mod._session_is_smoke({"metadata": {"smoke": mod.SMOKE_TAG}})

    def test_ordinary_session_not_matched(self):
        assert not mod._session_is_smoke({"title": "普通会话", "intent": "chat"})
        assert not mod._session_is_smoke({"metadata": {"smoke": "other"}})
        assert not mod._session_is_smoke({"metadata": "not-a-dict"})


class TestPurgeStale:
    def test_deletes_only_smoke_sessions(self, monkeypatch: pytest.MonkeyPatch):
        fake = FakeKernel(
            sessions=[
                {"thread_id": "a", "title": mod.SMOKE_TAG},
                {"thread_id": "b", "title": "用户会话"},
                {"thread_id": "c", "intent": mod.SMOKE_TAG},
                {"thread_id": "d", "metadata": {"smoke": mod.SMOKE_TAG}},
            ]
        )
        _install_fake(monkeypatch, fake)

        removed, failures = mod.purge_stale_smoke_sessions("http://127.0.0.1:9101", "tok")

        assert (removed, failures) == (3, [])
        assert fake.deleted == ["a", "c", "d"]

    def test_delete_failure_reported_not_raised(self, monkeypatch: pytest.MonkeyPatch):
        fake = FakeKernel(sessions=[{"thread_id": "a", "title": mod.SMOKE_TAG}], delete_status=503)
        _install_fake(monkeypatch, fake)

        removed, failures = mod.purge_stale_smoke_sessions("http://127.0.0.1:9101", "tok")

        assert removed == 0
        assert len(failures) == 1
        assert "a" in failures[0]
        assert "503" in failures[0]

    def test_list_failure_is_red(self, monkeypatch: pytest.MonkeyPatch):
        def fake_request(base, path, payload=None, token=None, method=None, timeout=15):
            return 503, {"error": "db down"}

        monkeypatch.setattr(mod, "_request", fake_request)
        with pytest.raises(AssertionError, match="会话列表查询失败"):
            mod.purge_stale_smoke_sessions("http://127.0.0.1:9101", "tok")


def _chat_noop(*_args, **_kwargs):
    return None


def _chat_raises(*_args, **_kwargs):
    raise AssertionError("聊天流报错")


class TestSmokeChatCleanup:
    def test_success_marks_and_deletes_session(self, monkeypatch: pytest.MonkeyPatch):
        fake = FakeKernel()
        _install_fake(monkeypatch, fake)
        monkeypatch.setattr(mod, "_chat_round", _chat_noop)

        mod.smoke_chat("http://127.0.0.1:9101", "tok", 5)

        assert fake.created_payloads == [
            {"title": mod.SMOKE_TAG, "intent": mod.SMOKE_TAG, "metadata": {"smoke": mod.SMOKE_TAG}}
        ]
        assert fake.deleted == ["t-new"]

    def test_chat_failure_still_deletes_and_keeps_original_error(self, monkeypatch: pytest.MonkeyPatch):
        fake = FakeKernel()
        _install_fake(monkeypatch, fake)

        def chat_timeout(*args, **kwargs):
            raise AssertionError("聊天超时 300s 未等到 stream_end")

        monkeypatch.setattr(mod, "_chat_round", chat_timeout)

        with pytest.raises(AssertionError, match="聊天超时"):
            mod.smoke_chat("http://127.0.0.1:9101", "tok", 5)
        assert fake.deleted == ["t-new"]

    def test_ticket_failure_still_deletes_session(self, monkeypatch: pytest.MonkeyPatch):
        fake = FakeKernel(ticket_status=500)
        _install_fake(monkeypatch, fake)
        monkeypatch.setattr(mod, "_chat_round", _chat_noop)

        with pytest.raises(AssertionError, match="ws-ticket"):
            mod.smoke_chat("http://127.0.0.1:9101", "tok", 5)
        assert fake.deleted == ["t-new"]

    def test_cleanup_failure_after_success_is_red(self, monkeypatch: pytest.MonkeyPatch):
        fake = FakeKernel(delete_status=503)
        _install_fake(monkeypatch, fake)
        monkeypatch.setattr(mod, "_chat_round", _chat_noop)

        with pytest.raises(AssertionError, match="清理失败"):
            mod.smoke_chat("http://127.0.0.1:9101", "tok", 5)
        assert fake.deleted == ["t-new"]

    def test_cleanup_failure_does_not_mask_chat_failure(self, monkeypatch: pytest.MonkeyPatch):
        fake = FakeKernel(delete_status=503)
        _install_fake(monkeypatch, fake)
        monkeypatch.setattr(mod, "_chat_round", _chat_raises)

        with pytest.raises(AssertionError, match="聊天流报错"):
            mod.smoke_chat("http://127.0.0.1:9101", "tok", 5)
        assert fake.deleted == ["t-new"]
