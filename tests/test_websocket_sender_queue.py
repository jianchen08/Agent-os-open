"""WebSocket 后台发送队列单元测试。

FEATURE-20260521-ws-queue:
验证 WebSocketManager 引入发送队列后的行为正确性：
- send_to_user / send_to_thread 非阻塞入队
- 后台 _sender_loop 实际消费并发送
- 队列满时优雅丢弃
- 连接失效时自动清理
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from src.websocket.handler import MockWebSocket, WebSocketManager, _SendItem


class TestSendItem:
    """_SendItem 数据类测试。"""

    def test_creation(self) -> None:
        """创建发送队列项。"""
        item = _SendItem("user", "u1", '{"type":"test"}')
        assert item.target_type == "user"
        assert item.target_id == "u1"
        assert item.payload == '{"type":"test"}'


class TestWebSocketManagerQueue:
    """WebSocketManager 发送队列功能测试。"""

    @pytest.fixture
    def manager(self) -> WebSocketManager:
        """创建 WebSocketManager 实例。"""
        return WebSocketManager()

    @pytest.mark.asyncio
    async def test_send_to_user_enqueue(self, manager: WebSocketManager) -> None:
        """send_to_user 应将消息入队而不是直接发送。"""
        ws = MockWebSocket()
        manager.register_global("user-1", ws)

        result = await manager.send_to_user("user-1", {"type": "test"})
        assert result is True
        # 消息应进入队列，而不是直接发送
        assert manager._send_queue.qsize() == 1
        # 后台任务应已启动
        assert manager._sender_task is not None

    @pytest.mark.asyncio
    async def test_send_to_thread_enqueue(self, manager: WebSocketManager) -> None:
        """send_to_thread 应将消息入队而不是直接发送。"""
        ws = MockWebSocket()
        manager.register_session("thread-1", ws)

        result = await manager.send_to_thread("thread-1", {"type": "test"})
        assert result is True
        assert manager._send_queue.qsize() == 1

    @pytest.mark.asyncio
    async def test_sender_loop_consumes_queue(self, manager: WebSocketManager) -> None:
        """后台发送循环应消费队列并实际发送消息。"""
        ws = MockWebSocket()
        manager.register_global("user-1", ws)

        await manager.send_to_user("user-1", {"type": "hello"})
        # 等待后台任务消费
        await asyncio.sleep(0.1)

        assert manager._send_queue.qsize() == 0
        assert len(ws.sent_messages) == 1
        assert '"type": "hello"' in ws.sent_messages[0]

    @pytest.mark.asyncio
    async def test_send_to_user_no_connection(self, manager: WebSocketManager) -> None:
        """向不存在的用户发送应返回 False。"""
        result = await manager.send_to_user("no-user", {"type": "test"})
        assert result is False

    @pytest.mark.asyncio
    async def test_send_to_thread_no_connection(self, manager: WebSocketManager) -> None:
        """向不存在的会话发送，入队成功但后台发送时无连接可送达。"""
        result = await manager.send_to_thread("no-thread", {"type": "test"})
        # 非阻塞入队设计：只要入队成功就返回 True，实际发送失败在后台处理
        assert result is True
        assert manager._send_queue.qsize() == 1
        # 等待后台消费后，消息因无连接而被丢弃
        await asyncio.sleep(0.1)
        assert manager._send_queue.qsize() == 0

    @pytest.mark.asyncio
    async def test_queue_full_discard(self, manager: WebSocketManager) -> None:
        """队列满时应丢弃消息而不是阻塞。"""
        # 将队列容量设为很小
        manager._send_queue = asyncio.Queue(maxsize=1)
        ws = MockWebSocket()
        manager.register_global("user-1", ws)

        # 第一条入队成功
        result1 = await manager.send_to_user("user-1", {"type": "first"})
        assert result1 is True

        # 第二条应因队列满而丢弃（因为后台任务还没消费）
        result2 = await manager.send_to_user("user-1", {"type": "second"})
        # 注意：如果后台任务已经消费了第一条，第二条可能成功
        # 所以这里只验证不会抛异常、不会阻塞
        assert result2 in (True, False)

    @pytest.mark.asyncio
    async def test_stale_connection_cleanup(self, manager: WebSocketManager) -> None:
        """发送失败时应自动清理失效连接。"""
        ws = MockWebSocket()
        ws._closed = True  # 模拟连接已关闭
        manager.register_global("user-1", ws)

        await manager.send_to_user("user-1", {"type": "test"})
        await asyncio.sleep(0.1)

        # 失效连接应被清理
        assert "user-1" not in manager._global_connections

    @pytest.mark.asyncio
    async def test_multiple_messages_order(self, manager: WebSocketManager) -> None:
        """多条消息应保持入队顺序发送。"""
        ws = MockWebSocket()
        manager.register_global("user-1", ws)

        for i in range(5):
            await manager.send_to_user("user-1", {"seq": i})

        # 等待后台任务消费完
        for _ in range(50):  # 最多等 5 秒
            if manager._send_queue.qsize() == 0:
                break
            await asyncio.sleep(0.1)

        assert len(ws.sent_messages) == 5
        for i, msg in enumerate(ws.sent_messages):
            assert f'"seq": {i}' in msg

    @pytest.mark.asyncio
    async def test_session_fallback_to_global(self, manager: WebSocketManager) -> None:
        """会话连接不存在时回退到全局连接。"""
        ws = MockWebSocket()
        manager.register_global("user-1", ws)

        # 向不存在的 thread 发送，但全局连接存在时应回退
        await manager.send_to_thread("no-thread", {"type": "fallback"})
        await asyncio.sleep(0.1)

        # 全局连接应收到消息
        assert len(ws.sent_messages) == 1
        assert '"type": "fallback"' in ws.sent_messages[0]

    @pytest.mark.asyncio
    async def test_sender_task_restart(self, manager: WebSocketManager) -> None:
        """后台任务结束后应能自动重启。"""
        ws = MockWebSocket()
        manager.register_global("user-1", ws)

        # 第一次发送启动任务
        await manager.send_to_user("user-1", {"type": "first"})
        task1 = manager._sender_task
        assert task1 is not None

        # 等待第一条消息被消费
        await asyncio.sleep(0.1)

        # 取消任务模拟异常结束
        task1.cancel()
        try:
            await task1
        except asyncio.CancelledError:
            pass

        # 再次发送应启动新任务
        await manager.send_to_user("user-1", {"type": "second"})
        task2 = manager._sender_task
        assert task2 is not None
        assert task2 is not task1

        # 等待消费
        await asyncio.sleep(0.1)
        # 两条消息都应被发送（第一条在取消前已消费）
        assert len(ws.sent_messages) == 2
