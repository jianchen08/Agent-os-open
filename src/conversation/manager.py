"""会话管理器。

管理会话（thread）的创建、列表查询（分页）、消息历史加载（分页）
和消息发送/接收（含流式输出）。
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from src.schemas.message import (
    MessageType,
    UnifiedMessage,
    create_message,
)


class Message:
    """消息实体。"""

    def __init__(
        self,
        msg_id: str = "",
        thread_id: str = "",
        role: str = "user",
        content: str = "",
        msg_type: MessageType | None = None,
        metadata: dict[str, Any] | None = None,
        created_at: float = 0.0,
    ) -> None:
        self.msg_id = msg_id or str(uuid.uuid4())
        self.thread_id = thread_id
        self.role = role
        self.content = content
        self.msg_type = msg_type or MessageType.COMPLETED
        self.metadata = metadata or {}
        self.created_at = created_at or time.time()

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "msg_id": self.msg_id,
            "thread_id": self.thread_id,
            "role": self.role,
            "content": self.content,
            "type": self.msg_type.value,
            "metadata": self.metadata,
            "created_at": self.created_at,
        }


class Thread:
    """会话实体。"""

    def __init__(
        self,
        thread_id: str = "",
        title: str = "",
        agent_id: str = "",
        created_at: float = 0.0,
        updated_at: float = 0.0,
    ) -> None:
        self.thread_id = thread_id or str(uuid.uuid4())
        self.title = title
        self.agent_id = agent_id
        self.created_at = created_at or time.time()
        self.updated_at = updated_at or self.created_at
        self._messages: list[Message] = []

    def add_message(self, message: Message) -> None:
        """添加消息到会话。"""
        message.thread_id = self.thread_id
        self._messages.append(message)
        self.updated_at = time.time()

    @property
    def message_count(self) -> int:
        return len(self._messages)

    @property
    def messages(self) -> list[Message]:
        return list(self._messages)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "thread_id": self.thread_id,
            "title": self.title,
            "agent_id": self.agent_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "message_count": self.message_count,
        }


class ConversationManager:
    """会话管理器。

    管理会话的创建、列表查询（分页）和消息历史加载（分页）。
    """

    DEFAULT_PAGE_SIZE = 20
    MAX_PAGE_SIZE = 100

    def __init__(self) -> None:
        self._threads: dict[str, Thread] = {}
        self._thread_order: list[str] = []  # 按时间排序的 thread_id

    def create_thread(self, title: str = "", agent_id: str = "") -> Thread:
        """创建新会话。"""
        thread = Thread(title=title, agent_id=agent_id)
        self._threads[thread.thread_id] = thread
        self._thread_order.append(thread.thread_id)
        return thread

    def get_thread(self, thread_id: str) -> Thread | None:
        """获取指定会话。"""
        return self._threads.get(thread_id)

    def delete_thread(self, thread_id: str) -> bool:
        """删除指定会话。"""
        if thread_id in self._threads:
            del self._threads[thread_id]
            if thread_id in self._thread_order:
                self._thread_order.remove(thread_id)
            return True
        return False

    def list_threads(
        self,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """分页获取会话列表。

        Args:
            page: 页码（从 1 开始）。
            page_size: 每页大小。

        Returns:
            包含 items, total, page, page_size, has_next 的分页结果。
        """
        page_size = min(page_size, self.MAX_PAGE_SIZE)
        page = max(page, 1)

        # 按更新时间倒序排列
        sorted_ids = list(reversed(self._thread_order))
        total = len(sorted_ids)
        start = (page - 1) * page_size
        end = start + page_size
        page_ids = sorted_ids[start:end]

        items = [self._threads[tid].to_dict() for tid in page_ids if tid in self._threads]

        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "has_next": end < total,
        }

    def get_messages(
        self,
        thread_id: str,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any] | None:
        """分页获取会话消息历史。

        Args:
            thread_id: 会话 ID。
            page: 页码（从 1 开始）。
            page_size: 每页大小。

        Returns:
            分页结果字典，会话不存在时返回 None。
        """
        thread = self._threads.get(thread_id)
        if thread is None:
            return None

        page_size = min(page_size, self.MAX_PAGE_SIZE)
        page = max(page, 1)

        messages = list(reversed(thread.messages))  # 最新的在前
        total = len(messages)
        start = (page - 1) * page_size
        end = start + page_size
        page_messages = messages[start:end]

        return {
            "items": [m.to_dict() for m in page_messages],
            "total": total,
            "page": page,
            "page_size": page_size,
            "has_next": end < total,
        }

    def add_message(
        self,
        thread_id: str,
        role: str,
        content: str,
        msg_type: MessageType | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Message | None:
        """向会话添加消息。

        Args:
            thread_id: 会话 ID。
            role: 消息角色（user/assistant/system）。
            content: 消息内容。
            msg_type: 消息类型。
            metadata: 元数据。

        Returns:
            创建的消息实体，会话不存在时返回 None。
        """
        thread = self._threads.get(thread_id)
        if thread is None:
            return None

        message = Message(
            thread_id=thread_id,
            role=role,
            content=content,
            msg_type=msg_type,
            metadata=metadata,
        )
        thread.add_message(message)
        return message

    def send_streaming_chunk(
        self,
        thread_id: str,
        chunk: str,
        msg_id: str = "",
    ) -> Message | None:
        """发送流式消息块。

        在实际系统中，这会通过 WebSocket 推送到前端。
        这里仅记录到会话消息历史。

        Args:
            thread_id: 会话 ID。
            chunk: 流式内容块。
            msg_id: 消息 ID（同一流式消息的所有块共享 ID）。

        Returns:
            创建或追加的消息实体。
        """
        thread = self._threads.get(thread_id)
        if thread is None:
            return None

        # 查找已有同 ID 消息并追加，或创建新消息
        if msg_id:
            for msg in thread.messages:
                if msg.msg_id == msg_id:
                    msg.content += chunk
                    return msg

        message = Message(
            msg_id=msg_id,
            thread_id=thread_id,
            role="assistant",
            content=chunk,
            msg_type=MessageType.EXECUTING,
        )
        thread.add_message(message)
        return message

    @property
    def thread_count(self) -> int:
        """当前会话总数。"""
        return len(self._threads)

    def bulk_create_threads(
        self, count: int, prefix: str = "Thread"
    ) -> list[Thread]:
        """批量创建会话（用于测试）。"""
        threads = []
        for i in range(count):
            thread = self.create_thread(title=f"{prefix} {i + 1}")
            threads.append(thread)
        return threads

    def add_messages_to_thread(
        self,
        thread_id: str,
        count: int,
        role: str = "user",
        prefix: str = "Message",
    ) -> list[Message]:
        """向指定会话批量添加消息（用于测试）。"""
        messages = []
        for i in range(count):
            msg = self.add_message(
                thread_id=thread_id,
                role=role,
                content=f"{prefix} {i + 1}",
            )
            if msg:
                messages.append(msg)
        return messages
