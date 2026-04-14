"""事件总线接口定义。"""

from abc import ABC, abstractmethod
from typing import Any, Callable, Coroutine


class IEventBus(ABC):
    """事件总线抽象接口。

    用于进程内异步通信，支持发布-订阅模式。
    """

    @abstractmethod
    async def emit(self, event_type: str, data: dict[str, Any]) -> None:
        """发射事件，通知所有订阅者。

        Args:
            event_type: 事件类型标识
            data: 事件数据字典
        """
        ...

    @abstractmethod
    def subscribe(
        self, event_type: str, callback: Callable[..., Coroutine[Any, Any, None]]
    ) -> None:
        """订阅事件。

        Args:
            event_type: 事件类型标识
            callback: 异步回调函数，接收 dict 参数
        """
        ...

    @abstractmethod
    def unsubscribe(
        self, event_type: str, callback: Callable[..., Coroutine[Any, Any, None]]
    ) -> None:
        """取消订阅。

        Args:
            event_type: 事件类型标识
            callback: 要移除的回调函数
        """
        ...

    @abstractmethod
    def has_subscribers(self, event_type: str) -> bool:
        """检查事件是否有订阅者。

        Args:
            event_type: 事件类型标识

        Returns:
            是否存在订阅者
        """
        ...
