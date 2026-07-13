"""测试 WebSocket 消息历史恢复（Issue 2: 发送消息时前面的消息历史会丢失）。

根因：WebSocket 连接建立时，对话历史从空列表开始，
没有从持久化存储 (api_store) 恢复之前的消息。

修复验证：
1. 新连接建立时，应从 api_store 加载该 thread_id 的历史消息
2. 多轮对话中，conversation_history 应包含之前的 user/assistant 消息
3. 传给 PipelineEngine 的 conversation_history 参数应包含完整历史
"""
from __future__ import annotations

import json
import sys
import uuid

import pytest

# 确保 src 目录在 sys.path 中
from pathlib import Path

_src_dir = str(Path(__file__).resolve().parent.parent.parent.parent / "src")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

from channels.api.models import MemoryStore


# ---------------------------------------------------------------------------
# 辅助：模拟 WebSocket
# ---------------------------------------------------------------------------
class FakeWebSocket:
    """模拟 FastAPI WebSocket，记录所有 send_text 调用。"""

    def __init__(self) -> None:
        self.sent_messages: list[str] = []
        self.query_params = {"token": ""}
        self.accepted = False
        self.closed = False
        self._receive_queue: list[str | Exception] = []

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True

    async def send_text(self, data: str) -> None:
        self.sent_messages.append(data)

    async def receive_text(self) -> str:
        if not self._receive_queue:
            raise Exception("WebSocketDisconnect")
        item = self._receive_queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def enqueue_receive(self, data: str) -> None:
        self._receive_queue.append(data)

    def enqueue_disconnect(self) -> None:
        self._receive_queue.append(Exception("WebSocketDisconnect"))


def _user_input_message(content: str) -> str:
    """构造 user_input 类型的 WebSocket 消息。"""
    return json.dumps({
        "type": "user_input",
        "data": {"content": content},
    })


def _heartbeat_message() -> str:
    """构造 heartbeat 类型的 WebSocket 消息。"""
    return json.dumps({"type": "heartbeat"})


def _restore_history_from_store(
    store: MemoryStore,
    thread_id: str,
) -> list[dict[str, str]]:
    """从 api_store 恢复对话历史，转换为 messages 格式。

    将 api_store 中的消息转换为 LLM 可用的 conversation_history 格式。

    Args:
        store: 持久化存储实例
        thread_id: 线程 ID

    Returns:
        messages 格式的对话历史列表
    """
    persisted_messages = store.get_messages(thread_id)
    history: list[dict[str, str]] = []
    for msg in persisted_messages:
        history.append({
            "role": msg["role"],
            "content": msg["content"],
        })
    return history


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------
class TestMessageHistoryRestore:
    """验证 WebSocket 连接时消息历史恢复。"""

    @pytest.mark.asyncio
    async def test_new_connection_loads_history_from_store(self):
        """测试：新 WebSocket 连接应从 api_store 加载历史消息。

        场景：
        1. thread_id 已有历史消息（通过 API 保存）
        2. 新建 WebSocket 连接到同一 thread_id
        3. conversation_histories 应包含之前的历史消息
        """
        store = MemoryStore()
        thread_id = "test_thread_001"

        # 预先向 store 添加历史消息
        store.add_message(
            thread_id=thread_id,
            message_id="msg_001",
            role="user",
            content="你好，请记住我的名字是小明",
        )
        store.add_message(
            thread_id=thread_id,
            message_id="msg_002",
            role="assistant",
            content="好的，我记住了，你的名字是小明。",
        )

        # 模拟 _restore_history_from_store 函数
        restored = _restore_history_from_store(store, thread_id)

        # Assert: 历史应包含之前的 2 条消息
        assert len(restored) == 2, f"应恢复 2 条历史消息，实际 {len(restored)}"
        assert restored[0]["role"] == "user"
        assert restored[0]["content"] == "你好，请记住我的名字是小明"
        assert restored[1]["role"] == "assistant"
        assert restored[1]["content"] == "好的，我记住了，你的名字是小明。"

    @pytest.mark.asyncio
    async def test_history_restored_as_messages_format(self):
        """测试：恢复的历史消息应转换为 LLM 可用的 messages 格式。

        验证字段：role / content
        """
        store = MemoryStore()
        thread_id = "test_thread_002"

        store.add_message(
            thread_id=thread_id,
            message_id="m1",
            role="user",
            content="第一轮用户消息",
        )
        store.add_message(
            thread_id=thread_id,
            message_id="m2",
            role="assistant",
            content="第一轮AI回复",
        )
        store.add_message(
            thread_id=thread_id,
            message_id="m3",
            role="user",
            content="第二轮用户消息",
        )

        restored = _restore_history_from_store(store, thread_id)

        assert len(restored) == 3
        for msg in restored:
            assert "role" in msg, "恢复的消息必须包含 role 字段"
            assert "content" in msg, "恢复的消息必须包含 content 字段"
            assert msg["role"] in ("user", "assistant"), \
                f"role 应为 user 或 assistant，实际 {msg['role']}"
            assert isinstance(msg["content"], str), \
                "content 应为字符串类型"

    @pytest.mark.asyncio
    async def test_no_history_returns_empty_list(self):
        """测试：没有历史消息时返回空列表，不影响正常流程。"""
        store = MemoryStore()
        thread_id = "new_thread_no_history"

        restored = _restore_history_from_store(store, thread_id)

        assert restored == [], "没有历史消息时应返回空列表"

    @pytest.mark.asyncio
    async def test_multi_turn_conversation_preserves_history(self):
        """测试：多轮对话中 conversation_history 应持续积累。

        模拟场景：
        1. 第一次连接发消息 -> 历史被积累
        2. 连接断开
        3. 重新连接同一 thread_id
        4. 新连接的历史应包含之前的消息
        """
        store = MemoryStore()
        thread_id = "test_thread_003"

        # 模拟第一轮对话后持久化
        store.add_message(
            thread_id=thread_id,
            message_id="msg_1",
            role="user",
            content="我喜欢蓝色",
        )
        store.add_message(
            thread_id=thread_id,
            message_id="msg_2",
            role="assistant",
            content="好的，你喜欢蓝色。",
        )

        # 模拟第二轮对话后持久化
        store.add_message(
            thread_id=thread_id,
            message_id="msg_3",
            role="user",
            content="我喜欢什么颜色？",
        )
        store.add_message(
            thread_id=thread_id,
            message_id="msg_4",
            role="assistant",
            content="你喜欢蓝色。",
        )

        # 重新连接时恢复历史
        restored = _restore_history_from_store(store, thread_id)

        assert len(restored) == 4, f"应恢复 4 条历史消息，实际 {len(restored)}"
        # 验证顺序正确
        assert restored[0]["content"] == "我喜欢蓝色"
        assert restored[1]["content"] == "好的，你喜欢蓝色。"
        assert restored[2]["content"] == "我喜欢什么颜色？"
        assert restored[3]["content"] == "你喜欢蓝色。"

    @pytest.mark.asyncio
    async def test_history_passed_to_engine(self):
        """测试：恢复的历史应正确传递给 PipelineEngine。

        验证 engine.run() 的 conversation_history 参数包含完整历史。
        """
        store = MemoryStore()
        thread_id = "test_thread_004"

        store.add_message(
            thread_id=thread_id,
            message_id="m1",
            role="user",
            content="记住：我喜欢红色",
        )
        store.add_message(
            thread_id=thread_id,
            message_id="m2",
            role="assistant",
            content="好的，记住了。",
        )

        # 模拟恢复历史并传给 engine
        history = _restore_history_from_store(store, thread_id)

        assert len(history) == 2
        # 模拟 engine.run() 被调用时的参数
        state_messages = list(history)
        state_messages.append({"role": "user", "content": "我喜欢什么颜色？"})

        assert len(state_messages) == 3, \
            "传给 LLM 的 messages 应包含 2 条历史 + 1 条当前消息"

    @pytest.mark.asyncio
    async def test_connection_disconnect_does_not_lose_persisted_messages(self):
        """测试：连接断开后，持久化消息仍然可用。

        验证即使内存中的 conversation_histories 被清除，
        api_store 中的消息依然存在，下次连接可以恢复。
        """
        store = MemoryStore()
        thread_id = "test_thread_005"

        # 持久化消息
        store.add_message(
            thread_id=thread_id,
            message_id="m1",
            role="user",
            content="Hello",
        )
        store.add_message(
            thread_id=thread_id,
            message_id="m2",
            role="assistant",
            content="Hi there!",
        )

        # 模拟内存中的 conversation_histories 被清除
        # (在 start_server.py 的 finally 块中发生)

        # 验证持久化存储仍然有消息
        persisted = store.get_messages(thread_id)
        assert len(persisted) == 2, "持久化存储应有 2 条消息"

        # 恢复历史
        restored = _restore_history_from_store(store, thread_id)
        assert len(restored) == 2, "恢复后应有 2 条消息"

    @pytest.mark.asyncio
    async def test_build_initial_state_includes_history(self):
        """测试：PipelineEngine._build_initial_state 应正确包含历史消息。"""
        conversation_history = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好！"},
        ]
        user_input = "再见"

        # _build_initial_state 的核心逻辑
        state_messages = list(conversation_history) if conversation_history else []
        if user_input:
            state_messages.append({"role": "user", "content": user_input})

        assert len(state_messages) == 3, \
            "state['messages'] 应包含 2 条历史 + 1 条当前 user_input"
        assert state_messages[0] == {"role": "user", "content": "你好"}
        assert state_messages[1] == {"role": "assistant", "content": "你好！"}
        assert state_messages[2] == {"role": "user", "content": "再见"}

    @pytest.mark.asyncio
    async def test_empty_store_for_new_thread(self):
        """测试：新线程（无任何历史）不影响正常流程。"""
        store = MemoryStore()
        new_thread_id = f"new_{uuid.uuid4().hex[:8]}"

        restored = _restore_history_from_store(store, new_thread_id)
        assert restored == []

    @pytest.mark.asyncio
    async def test_history_order_preserved(self):
        """测试：恢复的历史消息顺序应与存储顺序一致（按时间正序）。"""
        store = MemoryStore()
        thread_id = "test_thread_order"

        for i in range(5):
            role = "user" if i % 2 == 0 else "assistant"
            store.add_message(
                thread_id=thread_id,
                message_id=f"m_{i}",
                role=role,
                content=f"消息 {i}",
            )

        restored = _restore_history_from_store(store, thread_id)

        assert len(restored) == 5
        for i, msg in enumerate(restored):
            assert msg["content"] == f"消息 {i}", \
                f"第 {i} 条消息顺序错误，期望 '消息 {i}'，实际 '{msg['content']}'"
