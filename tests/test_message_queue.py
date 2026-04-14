"""MessageQueue 单元测试 — push/pop/peek/get_all/clear/size/过期清理/统计。"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from infrastructure.message_queue import (
    Message,
    MessageQueue,
    create_message_id,
)


# ── 辅助 ──────────────────────────────────────────────


def _make_message(
    session_id: str = "s1",
    target_id: str = "t1",
    content: str = "hello",
    priority: int = 0,
    expires_at: datetime | None = None,
) -> Message:
    """构造测试消息。"""
    return Message(
        id=create_message_id(),
        session_id=session_id,
        target_id=target_id,
        content=content,
        priority=priority,
        expires_at=expires_at,
    )


# ── Message ────────────────────────────────────────────


class TestMessage:
    """Message 数据类测试。"""

    def test_is_expired_no_expiry(self) -> None:
        """无过期时间 → 永不过期。"""
        msg = _make_message()
        assert msg.is_expired() is False

    def test_is_expired_future(self) -> None:
        """过期时间在未来 → 未过期。"""
        msg = _make_message(expires_at=datetime.utcnow() + timedelta(hours=1))
        assert msg.is_expired() is False

    def test_is_expired_past(self) -> None:
        """过期时间在过去 → 已过期。"""
        msg = _make_message(expires_at=datetime.utcnow() - timedelta(seconds=1))
        assert msg.is_expired() is True


# ── create_message_id ──────────────────────────────────


class TestCreateMessageId:
    """消息 ID 工厂测试。"""

    def test_format(self) -> None:
        """ID 格式为 msg_<hex12>。"""
        mid = create_message_id()
        assert mid.startswith("msg_")
        assert len(mid) == 16  # msg_ + 12 hex

    def test_unique(self) -> None:
        """两次调用生成不同 ID。"""
        assert create_message_id() != create_message_id()


# ── MessageQueue.push ──────────────────────────────────


class TestPush:
    """push 操作测试。"""

    def test_push_basic(self) -> None:
        """基本 push 操作。"""
        q = MessageQueue()
        msg = _make_message()
        assert q.push(msg) is True
        assert q.size("s1") == 1

    def test_push_priority_sort(self) -> None:
        """高优先级消息排在队首。"""
        q = MessageQueue()
        q.push(_make_message(content="low", priority=1))
        q.push(_make_message(content="high", priority=10))
        q.push(_make_message(content="mid", priority=5))

        msgs = q.get_all("s1")
        assert msgs[0].content == "high"
        assert msgs[1].content == "mid"
        assert msgs[2].content == "low"

    def test_push_default_ttl(self) -> None:
        """无 expires_at 时自动设置默认过期。"""
        q = MessageQueue(default_ttl=600)
        msg = _make_message()
        q.push(msg)
        assert msg.expires_at is not None

    def test_push_queue_full_evicts_oldest(self) -> None:
        """队列满时移除最早消息。"""
        q = MessageQueue(max_queue_size=2)
        q.push(_make_message(content="first"))
        q.push(_make_message(content="second"))
        q.push(_make_message(content="third"))

        # 队列大小仍为 2
        assert q.size("s1") == 2
        msgs = q.get_all("s1")
        # first 被移除（它优先级 0 且最早）
        contents = [m.content for m in msgs]
        assert "first" not in contents


# ── MessageQueue.pop ───────────────────────────────────


class TestPop:
    """pop 操作测试。"""

    def test_pop_empty(self) -> None:
        """空队列 pop 返回 None。"""
        q = MessageQueue()
        assert q.pop("s1") is None

    def test_pop_highest_priority(self) -> None:
        """pop 返回最高优先级消息。"""
        q = MessageQueue()
        q.push(_make_message(content="low", priority=1))
        q.push(_make_message(content="high", priority=10))

        msg = q.pop("s1")
        assert msg is not None
        assert msg.content == "high"

    def test_pop_by_target_id(self) -> None:
        """按 target_id 精确 pop。"""
        q = MessageQueue()
        q.push(_make_message(target_id="t1", content="msg1"))
        q.push(_make_message(target_id="t2", content="msg2"))

        msg = q.pop("s1", target_id="t2")
        assert msg is not None
        assert msg.content == "msg2"
        assert q.size("s1") == 1

    def test_pop_removes_message(self) -> None:
        """pop 后消息从队列移除。"""
        q = MessageQueue()
        q.push(_make_message())
        q.pop("s1")
        assert q.size("s1") == 0

    def test_pop_expired_skipped(self) -> None:
        """过期消息被自动清理，pop 返回下一条。"""
        q = MessageQueue()
        q.push(_make_message(content="expired", expires_at=datetime.utcnow() - timedelta(seconds=1)))
        q.push(_make_message(content="valid"))

        msg = q.pop("s1")
        assert msg is not None
        assert msg.content == "valid"


# ── MessageQueue.peek ──────────────────────────────────


class TestPeek:
    """peek 操作测试。"""

    def test_peek_empty(self) -> None:
        """空队列 peek 返回 None。"""
        q = MessageQueue()
        assert q.peek("s1") is None

    def test_peek_does_not_remove(self) -> None:
        """peek 不移除消息。"""
        q = MessageQueue()
        q.push(_make_message())
        q.peek("s1")
        assert q.size("s1") == 1

    def test_peek_by_target_id(self) -> None:
        """按 target_id 精确 peek。"""
        q = MessageQueue()
        q.push(_make_message(target_id="t1", content="msg1"))
        q.push(_make_message(target_id="t2", content="msg2"))

        msg = q.peek("s1", target_id="t2")
        assert msg is not None
        assert msg.content == "msg2"
        # 未移除
        assert q.size("s1") == 2


# ── MessageQueue.get_all ───────────────────────────────


class TestGetAll:
    """get_all 操作测试。"""

    def test_get_all_empty(self) -> None:
        """空队列返回空列表。"""
        q = MessageQueue()
        assert q.get_all("s1") == []

    def test_get_all_returns_copy(self) -> None:
        """get_all 返回副本，不影响原队列。"""
        q = MessageQueue()
        q.push(_make_message())
        msgs = q.get_all("s1")
        msgs.clear()
        assert q.size("s1") == 1

    def test_get_all_filter_by_target_id(self) -> None:
        """按 target_id 过滤。"""
        q = MessageQueue()
        q.push(_make_message(target_id="t1", content="m1"))
        q.push(_make_message(target_id="t2", content="m2"))
        q.push(_make_message(target_id="t1", content="m3"))

        msgs = q.get_all("s1", target_id="t1")
        assert len(msgs) == 2
        assert all(m.target_id == "t1" for m in msgs)


# ── MessageQueue.clear ────────────────────────────────


class TestClear:
    """clear 操作测试。"""

    def test_clear_empty(self) -> None:
        """空队列 clear 返回 0。"""
        q = MessageQueue()
        assert q.clear("s1") == 0

    def test_clear_returns_count(self) -> None:
        """clear 返回清除数量。"""
        q = MessageQueue()
        q.push(_make_message())
        q.push(_make_message())
        assert q.clear("s1") == 2
        assert q.size("s1") == 0

    def test_clear_does_not_affect_other_sessions(self) -> None:
        """clear 不影响其他 session。"""
        q = MessageQueue()
        q.push(_make_message(session_id="s1"))
        q.push(_make_message(session_id="s2"))
        q.clear("s1")
        assert q.size("s2") == 1


# ── MessageQueue.size ──────────────────────────────────


class TestSize:
    """size 操作测试。"""

    def test_size_empty(self) -> None:
        """空队列 size 返回 0。"""
        q = MessageQueue()
        assert q.size("s1") == 0

    def test_size_after_push_pop(self) -> None:
        """push/pop 后 size 正确。"""
        q = MessageQueue()
        q.push(_make_message())
        q.push(_make_message())
        assert q.size("s1") == 2
        q.pop("s1")
        assert q.size("s1") == 1


# ── 过期清理 ──────────────────────────────────────────


class TestExpiration:
    """过期清理测试。"""

    def test_cleanup_on_size(self) -> None:
        """size 自动清理过期消息。"""
        q = MessageQueue()
        q.push(_make_message(expires_at=datetime.utcnow() - timedelta(seconds=1)))
        assert q.size("s1") == 0

    def test_cleanup_on_get_all(self) -> None:
        """get_all 自动清理过期消息。"""
        q = MessageQueue()
        q.push(_make_message(expires_at=datetime.utcnow() - timedelta(seconds=1)))
        q.push(_make_message())
        msgs = q.get_all("s1")
        assert len(msgs) == 1


# ── get_statistics ─────────────────────────────────────


class TestGetStatistics:
    """统计信息测试。"""

    def test_statistics_empty(self) -> None:
        """空队列统计。"""
        q = MessageQueue()
        stats = q.get_statistics()
        assert stats["total_sessions"] == 0
        assert stats["total_messages"] == 0

    def test_statistics_with_messages(self) -> None:
        """有消息时的统计。"""
        q = MessageQueue(max_queue_size=50, default_ttl=600)
        q.push(_make_message(session_id="s1"))
        q.push(_make_message(session_id="s1"))
        q.push(_make_message(session_id="s2"))

        stats = q.get_statistics()
        assert stats["total_sessions"] == 2
        assert stats["total_messages"] == 3
        assert stats["sessions"]["s1"] == 2
        assert stats["sessions"]["s2"] == 1
        assert stats["max_queue_size"] == 50
        assert stats["default_ttl"] == 600


# ── 线程安全 ──────────────────────────────────────────


class TestThreadSafety:
    """线程安全测试。"""

    def test_concurrent_push(self) -> None:
        """多线程并发 push 不丢消息。"""
        import threading

        q = MessageQueue(max_queue_size=500)
        errors: list[Exception] = []

        def push_many() -> None:
            try:
                for i in range(50):
                    q.push(_make_message(content=f"msg_{i}"))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=push_many) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert q.size("s1") == 200
