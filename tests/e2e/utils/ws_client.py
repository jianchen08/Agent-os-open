"""WebSocket 测试客户端封装。

封装 FastAPI TestClient.websocket_connect，提供事件订阅、超时控制、
wait_for_event_type() 等便捷 API，消除后续 E2E 用例中的重复 WS 操作代码。

暴露接口：
- WSTestClient：WebSocket 测试客户端类
"""

from __future__ import annotations

import logging
import signal
import time
from typing import Any, Self

__all__ = ["WSTestClient"]

logger = logging.getLogger(__name__)


class WSTestClient:
    """WebSocket 测试客户端，封装 TestClient 的 WS 操作。

    提供事件收集、按类型等待（含 wall-clock 超时）、事件序列断言等便捷功能。
    所有接收到的事件自动存入内部事件列表，供后续断言使用。

    Usage::

        with WSTestClient(client, "/ws/chat?token=xxx") as ws:
            ws.send_json({"type": "heartbeat"})
            event = ws.wait_for_event_type("heartbeat_ack", timeout_seconds=5)
            assert event["type"] == "heartbeat_ack"
    """

    def __init__(self, client: Any, path: str) -> None:
        """初始化 WS 测试客户端。

        Args:
            client: FastAPI TestClient 实例
            path: WebSocket 连接路径（含 query 参数）
        """
        self._client = client
        self._path = path
        self._ws: Any = None
        self._events: list[dict[str, Any]] = []

    def __enter__(self) -> Self:
        """建立 WebSocket 连接。"""
        self._ws = self._client.websocket_connect(self._path)
        self._ws.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        """关闭 WebSocket 连接。"""
        if self._ws is not None:
            self._ws.__exit__(*exc)
            self._ws = None

    def send_json(self, data: dict[str, Any]) -> None:
        """发送 JSON 格式的 WebSocket 消息。

        Args:
            data: 要发送的字典数据
        """
        self._ws.send_json(data)

    def receive_json(self, timeout_seconds: float = 10.0) -> dict[str, Any]:
        """接收并解析 JSON 格式的 WebSocket 消息（含 wall-clock 超时保护）。

        使用 SIGALRM 实现 wall-clock 超时，防止服务端不发送事件时测试永久挂起。
        仅支持 Unix 平台（Linux/macOS）。

        Args:
            timeout_seconds: 单次接收的最大等待秒数

        Returns:
            解析后的事件字典

        Raises:
            TimeoutError: 超过 timeout_seconds 未收到消息
            WebSocketDisconnect: 连接已断开
        """
        if not hasattr(signal, "SIGALRM"):
            # Windows fallback：直接阻塞接收（测试环境通常为 Linux）
            event = self._ws.receive_json()
            self._events.append(event)
            return event

        def _alarm_handler(signum: int, frame: Any) -> None:
            raise TimeoutError(f"接收消息超时（{timeout_seconds}s）")

        old_handler = signal.signal(signal.SIGALRM, _alarm_handler)
        signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
        try:
            event = self._ws.receive_json()
            self._events.append(event)
            return event
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old_handler)

    def wait_for_event_type(
        self,
        event_type: str,
        max_events: int = 50,
        timeout_seconds: float = 10.0,
    ) -> dict[str, Any]:
        """持续接收事件，直到匹配指定类型或超时。

        同时使用事件计数器（max_events）和 wall-clock 超时（timeout_seconds）
        双重保护，确保测试不会因服务端异常而永久挂起。

        Args:
            event_type: 要等待的事件类型字符串
            max_events: 最大接收事件数，防止无限循环
            timeout_seconds: wall-clock 总超时秒数

        Returns:
            匹配的事件字典

        Raises:
            TimeoutError: 在 max_events 范围内未收到目标事件，或超过 timeout_seconds
        """
        deadline = time.monotonic() + timeout_seconds

        for i in range(max_events):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                received_types = [e.get("type") for e in self._events]
                raise TimeoutError(
                    f"等待事件 '{event_type}' 总超时（{timeout_seconds}s）。"
                    f"已收到的事件类型: {received_types}"
                )

            event = self.receive_json(timeout_seconds=remaining)
            logger.debug("wait_for_event_type: 收到事件 %s", event.get("type"))

            if event.get("type") == event_type:
                logger.debug("wait_for_event_type: 匹配目标事件 %s", event_type)
                return event

        received_types = [e.get("type") for e in self._events]
        raise TimeoutError(
            f"在 {max_events} 个事件内未收到类型 '{event_type}'。"
            f"已收到的事件类型: {received_types}"
        )

    def collect_events_until(
        self,
        terminal_types: set[str],
        max_events: int = 100,
        timeout_seconds: float = 10.0,
    ) -> list[dict[str, Any]]:
        """持续接收事件，直到遇到终止类型或达到上限。

        Args:
            terminal_types: 终止事件类型集合（如 {"execution_done", "pipeline_end"}）
            max_events: 最大接收事件数
            timeout_seconds: wall-clock 总超时秒数

        Returns:
            收集到的全部事件列表（含终止事件）

        Raises:
            TimeoutError: 超过 timeout_seconds 或 max_events
        """
        deadline = time.monotonic() + timeout_seconds

        for _ in range(max_events):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            event = self.receive_json(timeout_seconds=remaining)
            if event.get("type") in terminal_types:
                logger.debug(
                    "collect_events_until: 遇到终止事件 %s，共收集 %d 个事件",
                    event.get("type"),
                    len(self._events),
                )
                break

        return list(self._events)

    @property
    def events(self) -> list[dict[str, Any]]:
        """返回已接收的全部事件（只读副本）。"""
        return list(self._events)

    def get_events_by_type(self, event_type: str) -> list[dict[str, Any]]:
        """筛选已接收事件中匹配指定类型的子集。

        Args:
            event_type: 事件类型字符串

        Returns:
            匹配的事件列表
        """
        return [e for e in self._events if e.get("type") == event_type]

    def clear_events(self) -> None:
        """清空已收集的事件列表。"""
        self._events.clear()
