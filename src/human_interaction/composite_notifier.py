"""
组合通知器 — 将多个 IInteractionNotifier 组合为一个。

所有方法委托到每个子通知器，任一失败不影响其他。
任一返回 True 则整体返回 True。
"""

import logging
from typing import Any

from human_interaction.interfaces import IInteractionNotifier

logger = logging.getLogger(__name__)


class CompositeNotifier(IInteractionNotifier):
    """组合多个交互通知器，逐一委托调用。"""

    def __init__(self, *notifiers: IInteractionNotifier):
        self._notifiers = list(notifiers)

    async def notify_request(self, request: Any) -> bool:
        return await self._delegate("notify_request", request)

    async def notify_cancel(self, request_id: str, reason: str | None = None, thread_id: str = "") -> bool:
        return await self._delegate("notify_cancel", request_id, reason, thread_id)

    async def notify_timeout(self, request_id: str, thread_id: str = "") -> bool:
        return await self._delegate("notify_timeout", request_id, thread_id)

    async def notify_timeout_reminder(
        self,
        request_id: str,
        remaining_seconds: int,
        thread_id: str = "",
        *,
        title: str = "",
        mode: str = "",
        options: list[dict] | None = None,
        questions: list[str] | None = None,
    ) -> bool:
        return await self._delegate(
            "notify_timeout_reminder",
            request_id,
            remaining_seconds,
            thread_id,
            title=title,
            mode=mode,
            options=options,
            questions=questions,
        )

    async def notify_conversation_start(
        self,
        thread_id: str,
        tab_id: str,
        title: str,
        request_id: str = "",
        initial_message: str | None = None,
        suggestions: list[str] | None = None,
    ) -> bool:
        return await self._delegate(
            "notify_conversation_start",
            thread_id,
            tab_id,
            title,
            request_id=request_id,
            initial_message=initial_message,
            suggestions=suggestions,
        )

    async def _delegate(self, method_name: str, *args: Any, **kwargs: Any) -> bool:
        """逐一调用子通知器的同名方法，任一成功即返回 True。"""
        any_ok = False
        for notifier in self._notifiers:
            try:
                fn = getattr(notifier, method_name)
                result = await fn(*args, **kwargs)
                if result:
                    any_ok = True
            except Exception:
                logger.warning(
                    "Notifier %s.%s failed",
                    type(notifier).__name__,
                    method_name,
                    exc_info=True,
                )
        return any_ok
