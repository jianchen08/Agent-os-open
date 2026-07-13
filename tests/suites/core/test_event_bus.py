"""EventBus 测试。

覆盖 emit / subscribe / unsubscribe / has_subscribers。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from pipeline.event_bus import EventBus


class TestEventBus:
    """EventBus 基础功能测试。"""

    @pytest.mark.asyncio
    async def test_emit_with_subscriber(self) -> None:
        """emit 通知订阅者。"""
        bus = EventBus()
        callback = AsyncMock()
        bus.subscribe("test_event", callback)

        await bus.emit("test_event", {"key": "value"})
        callback.assert_called_once_with({"key": "value"})

    @pytest.mark.asyncio
    async def test_emit_no_subscribers(self) -> None:
        """无订阅者时 emit 不报错。"""
        bus = EventBus()
        await bus.emit("nonexistent", {"key": "value"})

    @pytest.mark.asyncio
    async def test_multiple_subscribers(self) -> None:
        """多个订阅者按顺序收到事件。"""
        bus = EventBus()
        cb1 = AsyncMock()
        cb2 = AsyncMock()
        bus.subscribe("test_event", cb1)
        bus.subscribe("test_event", cb2)

        await bus.emit("test_event", {"data": 42})
        cb1.assert_called_once_with({"data": 42})
        cb2.assert_called_once_with({"data": 42})

    @pytest.mark.asyncio
    async def test_unsubscribe(self) -> None:
        """取消订阅后不再收到事件。"""
        bus = EventBus()
        callback = AsyncMock()
        bus.subscribe("test_event", callback)
        bus.unsubscribe("test_event", callback)

        await bus.emit("test_event", {"key": "value"})
        callback.assert_not_called()

    @pytest.mark.asyncio
    async def test_unsubscribe_nonexistent_event(self) -> None:
        """取消不存在的事件订阅不报错。"""
        bus = EventBus()
        callback = AsyncMock()
        bus.unsubscribe("nonexistent", callback)  # 不应抛异常

    @pytest.mark.asyncio
    async def test_unsubscribe_wrong_callback(self) -> None:
        """取消不匹配的回调不影响其他订阅者。"""
        bus = EventBus()
        cb1 = AsyncMock()
        cb2 = AsyncMock()
        bus.subscribe("test_event", cb1)
        bus.subscribe("test_event", cb2)

        other_cb = AsyncMock()
        bus.unsubscribe("test_event", other_cb)  # other_cb 不在列表中

        await bus.emit("test_event", {"data": 1})
        cb1.assert_called_once()
        cb2.assert_called_once()

    def test_has_subscribers_true(self) -> None:
        """有订阅者时返回 True。"""
        bus = EventBus()
        callback = AsyncMock()
        bus.subscribe("test_event", callback)
        assert bus.has_subscribers("test_event") is True

    def test_has_subscribers_false(self) -> None:
        """无订阅者时返回 False。"""
        bus = EventBus()
        assert bus.has_subscribers("nonexistent") is False

    def test_has_subscribers_after_unsubscribe(self) -> None:
        """取消所有订阅后返回 False。"""
        bus = EventBus()
        callback = AsyncMock()
        bus.subscribe("test_event", callback)
        bus.unsubscribe("test_event", callback)
        assert bus.has_subscribers("test_event") is False

    @pytest.mark.asyncio
    async def test_callback_exception_does_not_block_others(self) -> None:
        """某个回调异常不影响后续回调执行。"""
        bus = EventBus()
        failing_cb = AsyncMock(side_effect=RuntimeError("cb error"))
        ok_cb = AsyncMock()
        bus.subscribe("test_event", failing_cb)
        bus.subscribe("test_event", ok_cb)

        await bus.emit("test_event", {"data": 1})
        failing_cb.assert_called_once()
        ok_cb.assert_called_once()

    @pytest.mark.asyncio
    async def test_different_events_isolated(self) -> None:
        """不同事件的订阅互不影响。"""
        bus = EventBus()
        cb_a = AsyncMock()
        cb_b = AsyncMock()
        bus.subscribe("event_a", cb_a)
        bus.subscribe("event_b", cb_b)

        await bus.emit("event_a", {"a": 1})
        cb_a.assert_called_once()
        cb_b.assert_not_called()

    @pytest.mark.asyncio
    async def test_pipeline_completed_event(self) -> None:
        """pipeline_completed 事件典型用法。"""
        bus = EventBus()
        callback = AsyncMock()
        bus.subscribe("pipeline_completed", callback)

        await bus.emit("pipeline_completed", {
            "pipeline_id": "pipeline-1",
            "status": "completed",
            "result": {"output": "done"},
        })
        callback.assert_called_once()
        data = callback.call_args[0][0]
        assert data["pipeline_id"] == "pipeline-1"
        assert data["status"] == "completed"
