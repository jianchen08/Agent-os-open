"""
操作队列（非 ORM 存根）

提供降级接口，不再依赖 SQLAlchemy。
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class OperationQueue:
    """操作队列（降级存根）"""

    def __init__(self):
        self._queue: list[Any] = []

    async def enqueue(self, operation: Any) -> None:
        """入队（记录到内存列表）。"""
        self._queue.append(operation)

    async def dequeue(self) -> Any:
        """出队。"""
        if self._queue:
            return self._queue.pop(0)
        return None

    @property
    def size(self) -> int:
        return len(self._queue)

    def clear(self) -> None:
        self._queue.clear()
