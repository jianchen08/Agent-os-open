"""MessageInjectPlugin 单元测试 — 消息注入（Mock MessageQueue）。"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from infrastructure.message_queue import Message, MessageQueue, create_message_id
from pipeline.plugin import PluginContext
from plugins.input.message_inject import MessageInjectPlugin


# ── 辅助 ──────────────────────────────────────────────


def _make_context(
    state: dict[str, Any] | None = None,
    services: dict[str, Any] | None = None,
) -> PluginContext:
    """构造插件上下文。"""
    return PluginContext(
        state=state or {},
        config={},
        _services=services or {},
    )


def _make_message(
    session_id: str = "s1",
    target_id: str = "t1",
    content: str = "注入的消息",
) -> Message:
    """构造测试消息。"""
    return Message(
        id=create_message_id(),
        session_id=session_id,
        target_id=target_id,
        content=content,
    )


# ── 基本属性 ──────────────────────────────────────────


class TestProperties:
    """插件属性测试。"""

    def test_name(self) -> None:
        """插件名称。"""
        plugin = MessageInjectPlugin()
        assert plugin.name == "message_inject"

    def test_default_priority(self) -> None:
        """默认优先级 5。"""
        plugin = MessageInjectPlugin()
        assert plugin.priority == 5

    def test_custom_priority(self) -> None:
        """自定义优先级。"""
        plugin = MessageInjectPlugin(config={"priority": 10})
        assert plugin.priority == 10


# ── 正常注入 ──────────────────────────────────────────


class TestInject:
    """消息注入测试。"""

    @pytest.mark.asyncio
    async def test_inject_single_message(self) -> None:
        """注入单条消息到空 messages。"""
        plugin = MessageInjectPlugin()
        msg = _make_message(content="请处理任务")
        # 使用 spec=MessageQueue 使 isinstance 检查通过
        mock_queue = MagicMock(spec=MessageQueue)
        mock_queue.pop.return_value = msg

        ctx = _make_context(
            state={"session_id": "s1", "messages": []},
            services={"message_queue": mock_queue},
        )

        result = await plugin.execute(ctx)
        assert "messages" in result.state_updates
        messages = result.state_updates["messages"]
        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "请处理任务"

    @pytest.mark.asyncio
    async def test_inject_prepends_to_existing(self) -> None:
        """注入到已有 messages 前部。"""
        plugin = MessageInjectPlugin()
        msg = _make_message(content="新消息")
        mock_queue = MagicMock(spec=MessageQueue)
        mock_queue.pop.return_value = msg

        existing = [{"role": "user", "content": "旧消息"}]
        ctx = _make_context(
            state={"session_id": "s1", "messages": existing},
            services={"message_queue": mock_queue},
        )

        result = await plugin.execute(ctx)
        messages = result.state_updates["messages"]
        assert len(messages) == 2
        assert messages[0]["content"] == "新消息"
        assert messages[1]["content"] == "旧消息"


# ── 队列为空 ──────────────────────────────────────────


class TestEmptyQueue:
    """队列为空时的行为。"""

    @pytest.mark.asyncio
    async def test_empty_queue_no_injection(self) -> None:
        """队列为空时不注入。"""
        plugin = MessageInjectPlugin()
        mock_queue = MagicMock(spec=MessageQueue)
        mock_queue.pop.return_value = None

        ctx = _make_context(
            state={"session_id": "s1", "messages": []},
            services={"message_queue": mock_queue},
        )

        result = await plugin.execute(ctx)
        assert result.state_updates == {}


# ── 服务不可用 ────────────────────────────────────────


class TestServiceUnavailable:
    """消息队列服务不可用时的降级行为。"""

    @pytest.mark.asyncio
    async def test_no_message_queue_service(self) -> None:
        """无 message_queue 服务时跳过。"""
        plugin = MessageInjectPlugin()
        ctx = _make_context(
            state={"session_id": "s1"},
            services={},
        )

        result = await plugin.execute(ctx)
        assert result.state_updates == {}

    @pytest.mark.asyncio
    async def test_wrong_type_message_queue(self) -> None:
        """message_queue 类型不正确时跳过。"""
        plugin = MessageInjectPlugin()
        ctx = _make_context(
            state={"session_id": "s1"},
            services={"message_queue": "not_a_queue"},
        )

        result = await plugin.execute(ctx)
        assert result.state_updates == {}


# ── session_id 缺失 ───────────────────────────────────


class TestNoSessionId:
    """session_id 缺失时的行为。"""

    @pytest.mark.asyncio
    async def test_no_session_id_in_state(self) -> None:
        """state 中无 session_id 时跳过。"""
        plugin = MessageInjectPlugin()
        mock_queue = MagicMock(spec=MessageQueue)

        ctx = _make_context(
            state={"messages": []},
            services={"message_queue": mock_queue},
        )

        result = await plugin.execute(ctx)
        assert result.state_updates == {}
        mock_queue.pop.assert_not_called()


# ── pop 异常 ──────────────────────────────────────────


class TestPopException:
    """pop 操作异常时的降级行为。"""

    @pytest.mark.asyncio
    async def test_pop_raises_exception(self) -> None:
        """pop 抛异常时降级处理。"""
        plugin = MessageInjectPlugin()
        mock_queue = MagicMock(spec=MessageQueue)
        mock_queue.pop.side_effect = RuntimeError("queue error")

        ctx = _make_context(
            state={"session_id": "s1", "messages": []},
            services={"message_queue": mock_queue},
        )

        result = await plugin.execute(ctx)
        assert result.state_updates == {}
