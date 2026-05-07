"""
人类交互服务实现（纯内存版）。

使用内存 dict 存储请求和响应，无外部数据库依赖。

暴露接口：
- get_human_interaction_service：获取全局单例
- set_human_interaction_service：设置全局单例
- reset_human_interaction_service：重置全局单例
- HumanInteractionService：人类交互服务类
- InteractionTimeoutError：交互超时异常
- InteractionCancelledError：交互取消异常
- InteractionDeniedError：交互拒绝异常
"""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from human_interaction.interfaces import (
    IHumanInteractionService,
    IInteractionNotifier,
)
from human_interaction.models import (
    InteractionMode,
    InteractionStatus,
    Priority,
    ResponseType,
)

logger = logging.getLogger(__name__)


class InteractionTimeoutError(Exception):
    """交互超时异常。"""

    def __init__(self, request_id: str, timeout: float):
        self.request_id = request_id
        self.timeout = timeout
        super().__init__(f"交互超时: {request_id} (超时时间: {timeout}秒)")


class InteractionCancelledError(Exception):
    """交互取消异常。"""

    def __init__(self, request_id: str, reason: str | None = None):
        self.request_id = request_id
        self.reason = reason
        message = f"交互取消: {request_id}"
        if reason:
            message += f" (原因: {reason})"
        super().__init__(message)


class InteractionDeniedError(Exception):
    """交互拒绝异常。"""

    def __init__(self, request_id: str, reason: str | None = None):
        self.request_id = request_id
        self.reason = reason
        message = f"交互拒绝: {request_id}"
        if reason:
            message += f" (原因: {reason})"
        super().__init__(message)


class HumanInteractionService(IHumanInteractionService):
    """
    人类交互服务（纯内存版）。

    使用内存 dict 存储 InteractionRecord，通过 asyncio.Event
    实现请求-响应的异步等待。

    支持：
    - 选择模式：审批确认、澄清问题、方案选择
    - 对话模式：跳转到对话标签页
    """

    def __init__(
        self,
        notifier: IInteractionNotifier | None = None,
        default_timeout: float = 86400.0,
        remind_before_seconds: int = 300,
    ):
        self._notifier = notifier
        self._default_timeout = default_timeout
        self._remind_before_seconds = remind_before_seconds
        self._pending_events: dict[str, asyncio.Event] = {}
        self._timeout_tasks: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self._requests: dict[str, dict[str, Any]] = {}
        self._responses: dict[str, dict[str, Any]] = {}

    async def send_notification(
        self,
        session_id: str,
        thread_id: str,
        title: str,
        message: str = "",
        priority: Priority = Priority.NORMAL,
        progress: float | None = None,
        agent_id: str | None = None,
    ) -> str:
        """发送非阻塞通知，不等待用户响应，立即返回 request_id。"""
        request_id = str(uuid4())
        record = self._make_request_record(
            request_id=request_id,
            session_id=session_id,
            mode=InteractionMode.NOTIFICATION,
            title=title,
            description=message,
            thread_id=thread_id,
            tab_id="",
            user_id=None,
            agent_id=agent_id,
            extra={
                "progress": progress,
                "priority": priority.value,
            },
        )
        self._requests[request_id] = record
        # 不创建 asyncio.Event，不等待 —— 非阻塞核心逻辑
        if self._notifier:
            await self._notifier.notify_request(record)

        logger.info(
            "[HumanInteraction] 发送通知 | request_id=%s | title=%s",
            request_id, title,
        )
        return request_id

    async def create_choice_request(
        self,
        session_id: str,
        thread_id: str,
        tab_id: str,
        title: str,
        description: str = "",
        options: list[dict[str, Any]] | None = None,
        questions: list[str] | None = None,
        timeout_seconds: int | None = None,
        priority: Priority = Priority.NORMAL,
        user_id: str | None = None,
        agent_id: str | None = None,
    ) -> str:
        """创建选择模式请求，返回 request_id。"""
        request_id = str(uuid4())
        timeout = timeout_seconds or int(self._default_timeout)

        record = self._make_request_record(
            request_id=request_id,
            session_id=session_id,
            mode=InteractionMode.CHOICE,
            title=title,
            description=description,
            thread_id=thread_id,
            tab_id=tab_id,
            user_id=user_id,
            agent_id=agent_id,
            extra={
                "options": options,
                "questions": questions,
                "timeout_seconds": timeout,
                "priority": priority.value,
                "timeout_reminded": False,
            },
        )
        self._requests[request_id] = record

        async with self._lock:
            self._pending_events[request_id] = asyncio.Event()

        if self._notifier:
            await self._notifier.notify_request(record)

        self._setup_timeout(request_id, timeout, thread_id)

        logger.info(
            "[HumanInteraction] 创建选择请求 | request_id=%s | title=%s",
            request_id, title,
        )
        return request_id

    async def create_conversation_request(
        self,
        session_id: str,
        thread_id: str,
        tab_id: str,
        title: str,
        description: str = "",
        initial_message: str | None = None,
        suggestions: list[str] | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
        artifacts: list[dict[str, Any]] | None = None,
        workspace_tab_id: str | None = None,
    ) -> str:
        """创建对话模式请求，返回 request_id。

        扩展支持 artifacts（制品列表）和 workspace_tab_id（关联工作区 Tab）。
        artifacts 用于在 Chat 卡片中展示 AI 产出物预览。
        workspace_tab_id 用于跳转到工作区进行深度审阅。
        """
        request_id = str(uuid4())

        record = self._make_request_record(
            request_id=request_id,
            session_id=session_id,
            mode=InteractionMode.CONVERSATION,
            title=title,
            description=description,
            thread_id=thread_id,
            tab_id=tab_id,
            user_id=user_id,
            agent_id=agent_id,
            extra={
                "initial_message": initial_message,
                "suggestions": suggestions,
                "artifacts": artifacts or [],
                "workspace_tab_id": workspace_tab_id,
            },
        )
        self._requests[request_id] = record

        async with self._lock:
            self._pending_events[request_id] = asyncio.Event()

        if self._notifier:
            await self._notifier.notify_request(record)
            await self._notifier.notify_conversation_start(
                thread_id=thread_id,
                tab_id=tab_id,
                title=title,
                request_id=request_id,
                initial_message=initial_message,
                suggestions=suggestions,
            )

        logger.info(
            "[HumanInteraction] 创建对话请求 | request_id=%s | thread_id=%s",
            request_id, thread_id,
        )
        return request_id

    async def wait_for_conversation_arrival(
        self,
        request_id: str,
        timeout: float = 86400.0,
    ) -> dict[str, Any]:
        """等待用户到达对话页面。"""
        event = self._pending_events.get(request_id)
        if not event:
            return {"status": "timeout", "message": "用户未到达对话页面"}

        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except TimeoutError:
            return {"status": "timeout", "message": f"用户在 {timeout} 秒内未到达对话页面"}

        return {"status": "arrived", "message": "用户已到达对话页面"}

    async def wait_for_choice(
        self,
        request_id: str,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """等待用户选择。"""
        event = self._pending_events.get(request_id)
        if not event:
            record = self._requests.get(request_id)
            if not record:
                raise ValueError(f"请求不存在: {request_id}")
            async with self._lock:
                self._pending_events[request_id] = asyncio.Event()
                event = self._pending_events[request_id]

        record = self._requests.get(request_id)
        if not record:
            raise ValueError(f"请求不存在: {request_id}")

        msg_data = record.get("message_data", {})
        timeout = timeout or msg_data.get("timeout_seconds") or self._default_timeout

        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except TimeoutError:
            await self._handle_timeout(request_id)
            raise InteractionTimeoutError(request_id, timeout) from None

        response = self._responses.get(request_id)
        if not response:
            raise InteractionTimeoutError(request_id, timeout)

        resp_data = response.get("message_data", {})
        resp_type = resp_data.get("response_type", "")

        if resp_type == ResponseType.DENIED.value:
            raise InteractionDeniedError(request_id, resp_data.get("feedback"))

        if resp_type == ResponseType.CANCELLED.value:
            raise InteractionCancelledError(request_id, resp_data.get("feedback"))

        return {
            "request_id": request_id,
            "response_type": resp_type,
            "selected_option": resp_data.get("selected_option"),
            "answers": resp_data.get("answers"),
            "feedback": resp_data.get("feedback"),
        }

    async def submit_response(
        self,
        request_id: str,
        response_type: str,
        selected_option: str | None = None,
        answers: list[str] | None = None,
        feedback: str | None = None,
        user_id: str | None = None,
    ) -> bool:
        """提交响应。"""
        request_record = self._requests.get(request_id)
        if not request_record:
            logger.warning("[HumanInteraction] 请求不存在 | request_id=%s", request_id)
            return False

        if request_record.get("status") != InteractionStatus.PENDING.value:
            logger.warning(
                "[HumanInteraction] 请求状态不允许响应 | request_id=%s | status=%s",
                request_id, request_record.get("status"),
            )
            return False

        response_id = str(uuid4())
        now = datetime.now(UTC).isoformat()

        self._responses[request_id] = {
            "id": response_id,
            "session_id": request_record.get("session_id"),
            "parent_record_id": request_id,
            "type": "interaction_response",
            "status": "completed",
            "message_data": {
                "request_id": request_id,
                "response_type": response_type,
                "selected_option": selected_option,
                "answers": answers,
                "feedback": feedback,
                "user_id": user_id,
            },
        }

        request_record["status"] = InteractionStatus.COMPLETED.value
        msg_data = request_record.setdefault("message_data", {})
        msg_data["responded_at"] = now

        async with self._lock:
            if request_id in self._pending_events:
                self._pending_events[request_id].set()
            if request_id in self._timeout_tasks:
                self._timeout_tasks[request_id].cancel()
                del self._timeout_tasks[request_id]

        logger.info(
            "[HumanInteraction] 响应已提交 | request_id=%s | response_type=%s",
            request_id, response_type,
        )
        return True

    async def submit_review_feedback(
        self,
        request_id: str,
        action: str,
        annotations: list[dict[str, Any]] | None = None,
        feedback_text: str | None = None,
        user_id: str | None = None,
    ) -> bool:
        """提交审批反馈（包含批注）。

        扩展的响应提交方法，支持结构化的批注数据。
        批注类型包括：text_selection、image_area、video_timestamp、screenshot_area。
        """
        request_record = self._requests.get(request_id)
        if not request_record:
            logger.warning("[HumanInteraction] 审批请求不存在 | request_id=%s", request_id)
            return False

        if request_record.get("status") not in (
            InteractionStatus.PENDING.value,
            InteractionStatus.VIEWED.value,
        ):
            logger.warning(
                "[HumanInteraction] 请求状态不允许审批 | request_id=%s | status=%s",
                request_id, request_record.get("status"),
            )
            return False

        response_id = str(uuid4())
        now = datetime.now(UTC).isoformat()

        # 映射 action 到 response_type
        response_type_map = {
            "approve": ResponseType.APPROVED.value,
            "reject": ResponseType.DENIED.value,
            "annotate": ResponseType.ANSWERED.value,
        }
        response_type = response_type_map.get(action, ResponseType.ANSWERED.value)

        self._responses[request_id] = {
            "id": response_id,
            "session_id": request_record.get("session_id"),
            "parent_record_id": request_id,
            "type": "review_feedback",
            "status": "completed",
            "message_data": {
                "request_id": request_id,
                "response_type": response_type,
                "action": action,
                "annotations": annotations or [],
                "feedback_text": feedback_text,
                "user_id": user_id,
            },
        }

        request_record["status"] = InteractionStatus.COMPLETED.value
        msg_data = request_record.setdefault("message_data", {})
        msg_data["responded_at"] = now

        async with self._lock:
            if request_id in self._pending_events:
                self._pending_events[request_id].set()
            if request_id in self._timeout_tasks:
                self._timeout_tasks[request_id].cancel()
                del self._timeout_tasks[request_id]

        annotation_count = len(annotations or [])
        logger.info(
            "[HumanInteraction] 审批反馈已提交 | request_id=%s | action=%s | annotations=%d",
            request_id, action, annotation_count,
        )
        return True

    async def mark_as_viewed(self, request_id: str) -> bool:
        """标记请求为已查看，conversation 模式下触发到达通知。"""
        record = self._requests.get(request_id)
        if not record or record.get("status") != InteractionStatus.PENDING.value:
            return False

        record["status"] = InteractionStatus.VIEWED.value
        record.setdefault("message_data", {})["viewed_at"] = datetime.now(UTC).isoformat()

        async with self._lock:
            if request_id in self._pending_events:
                self._pending_events[request_id].set()

        return True

    async def cancel_request(
        self,
        request_id: str,
        reason: str | None = None,
    ) -> bool:
        """取消请求。"""
        record = self._requests.get(request_id)
        if not record:
            return False

        status = record.get("status")
        if status in (InteractionStatus.COMPLETED.value, InteractionStatus.TIMEOUT.value):
            return False

        record["status"] = InteractionStatus.CANCELLED.value

        async with self._lock:
            if request_id in self._pending_events:
                self._pending_events[request_id].set()
            if request_id in self._timeout_tasks:
                self._timeout_tasks[request_id].cancel()
                del self._timeout_tasks[request_id]

        if self._notifier:
            msg_data = record.get("message_data") or {}
            await self._notifier.notify_cancel(
                request_id, reason,
                thread_id=msg_data.get("thread_id", ""),
            )

        logger.info(
            "[HumanInteraction] 请求已取消 | request_id=%s | reason=%s",
            request_id, reason,
        )
        return True

    async def get_request(self, request_id: str) -> dict[str, Any] | None:
        """获取请求详情。"""
        return self._requests.get(request_id)

    async def get_pending_requests(
        self,
        session_id: str | None = None,
        user_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """获取待处理请求列表。"""
        results: list[dict[str, Any]] = []
        for record in self._requests.values():
            if record.get("status") != InteractionStatus.PENDING.value:
                continue
            msg_data = record.get("message_data") or {}
            if session_id and record.get("session_id") != session_id:
                continue
            if user_id and msg_data.get("user_id") != user_id:
                continue
            results.append(record)
            if len(results) >= limit:
                break
        return results

    async def get_interaction_history(
        self,
        session_id: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """获取交互历史。"""
        results: list[dict[str, Any]] = []
        for record in self._requests.values():
            if record.get("session_id") == session_id:
                results.append(record)
        for resp in self._responses.values():
            if resp.get("session_id") == session_id:
                results.append(resp)
        return results[:limit]

    def set_notifier(self, notifier: IInteractionNotifier) -> None:
        """设置通知器。"""
        self._notifier = notifier

    def _make_request_record(
        self,
        request_id: str,
        session_id: str,
        mode: InteractionMode,
        title: str,
        description: str,
        thread_id: str,
        tab_id: str,
        user_id: str | None,
        agent_id: str | None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """构建请求记录字典。"""
        message_data: dict[str, Any] = {
            "interaction_mode": mode.value,
            "title": title,
            "description": description,
            "thread_id": thread_id,
            "tab_id": tab_id,
            "user_id": user_id,
            "agent_id": agent_id,
            "viewed_at": None,
        }
        if extra:
            message_data.update(extra)

        return {
            "id": request_id,
            "session_id": session_id,
            "type": "interaction_request",
            "status": InteractionStatus.PENDING.value,
            "message_data": message_data,
        }

    def _setup_timeout(self, request_id: str, timeout_seconds: int, thread_id: str = ""):
        """设置超时任务。"""

        async def timeout_handler():
            try:
                await asyncio.sleep(timeout_seconds - self._remind_before_seconds)

                record = self._requests.get(request_id)
                if record and record.get("status") == InteractionStatus.PENDING.value:
                    if self._notifier:
                        msg_data = record.get("message_data") or {}
                        await self._notifier.notify_timeout_reminder(
                            request_id, self._remind_before_seconds, thread_id,
                            title=msg_data.get("title", ""),
                            mode=msg_data.get("interaction_mode", "choice"),
                            options=msg_data.get("options"),
                            questions=msg_data.get("questions"),
                        )

                    record.setdefault("message_data", {})["timeout_reminded"] = True

                await asyncio.sleep(self._remind_before_seconds)

                record = self._requests.get(request_id)
                if record and record.get("status") == InteractionStatus.PENDING.value:
                    await self._handle_timeout(request_id)

            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error("[HumanInteraction] 超时处理失败 | error=%s", e)

        task = asyncio.create_task(timeout_handler())
        self._timeout_tasks[request_id] = task

    async def _handle_timeout(self, request_id: str):
        """处理超时。"""
        record = self._requests.get(request_id)
        thread_id = ""
        if record:
            msg_data = record.get("message_data") or {}
            thread_id = msg_data.get("thread_id", "")
            record["status"] = InteractionStatus.TIMEOUT.value

        async with self._lock:
            if request_id in self._pending_events:
                self._pending_events[request_id].set()

        if self._notifier:
            await self._notifier.notify_timeout(request_id, thread_id=thread_id)

        logger.info("[HumanInteraction] 请求超时 | request_id=%s", request_id)


_service_instance: HumanInteractionService | None = None


def get_human_interaction_service() -> HumanInteractionService:
    """获取服务单例。"""
    global _service_instance
    if _service_instance is None:
        _service_instance = HumanInteractionService()
    return _service_instance


def set_human_interaction_service(service: HumanInteractionService) -> None:
    """设置服务单例。"""
    global _service_instance
    _service_instance = service


def reset_human_interaction_service() -> None:
    """重置服务单例（用于测试）。"""
    global _service_instance
    _service_instance = None
