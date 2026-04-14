"""
人类交互服务实现

暴露接口：
- get_human_interaction_service() -> HumanInteractionService：get_human_interaction_service功能
- set_human_interaction_service(service: HumanInteractionService) -> None：set_human_interaction_service功能
- reset_human_interaction_service() -> None：reset_human_interaction_service功能
- set_notifier(self, notifier: IInteractionNotifier) -> None：set_notifier功能
- InteractionTimeoutError：InteractionTimeoutError类
- InteractionCancelledError：InteractionCancelledError类
- InteractionDeniedError：InteractionDeniedError类
- HumanInteractionService：HumanInteractionService类
"""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select, update

from src.core.human_interaction.interfaces import (
    IHumanInteractionService,
    IInteractionNotifier,
)
from src.core.human_interaction.models import (
    InteractionMode,
    InteractionStatus,
    Priority,
    ResponseType,
)
from src.db.models import ExecutionRecord
from src.db.session_manager import managed_session

logger = logging.getLogger(__name__)


class InteractionTimeoutError(Exception):
    """交互超时异常"""

    def __init__(self, request_id: str, timeout: float):
        self.request_id = request_id
        self.timeout = timeout
        super().__init__(f"交互超时: {request_id} (超时时间: {timeout}秒)")


class InteractionCancelledError(Exception):
    """交互取消异常"""

    def __init__(self, request_id: str, reason: str | None = None):
        self.request_id = request_id
        self.reason = reason
        message = f"交互取消: {request_id}"
        if reason:
            message += f" (原因: {reason})"
        super().__init__(message)


class InteractionDeniedError(Exception):
    """交互拒绝异常"""

    def __init__(self, request_id: str, reason: str | None = None):
        self.request_id = request_id
        self.reason = reason
        message = f"交互拒绝: {request_id}"
        if reason:
            message += f" (原因: {reason})"
        super().__init__(message)


class HumanInteractionService(IHumanInteractionService):
    """
    人类交互服务实现

    统一处理所有需要人类参与的场景：
    - 选择模式：审批确认、澄清问题、方案选择
    - 对话模式：跳转到对话标签页

    使用 ExecutionRecord 进行持久化存储。
    """

    def __init__(
        self,
        notifier: IInteractionNotifier | None = None,
        default_timeout: float = 300.0,
        remind_before_seconds: int = 60,
    ):
        self._notifier = notifier
        self._default_timeout = default_timeout
        self._remind_before_seconds = remind_before_seconds
        self._pending_events: dict[str, asyncio.Event] = {}
        self._timeout_tasks: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

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
        """创建选择模式请求"""
        request_id = str(uuid4())
        timeout = timeout_seconds or int(self._default_timeout)

        async with managed_session() as session:
            record = ExecutionRecord(
                id=request_id,
                session_id=session_id,
                type="interaction_request",
                status=InteractionStatus.PENDING.value,
                message_data={
                    "interaction_mode": InteractionMode.CHOICE.value,
                    "title": title,
                    "description": description,
                    "options": options,
                    "questions": questions,
                    "timeout_seconds": timeout,
                    "priority": priority.value,
                    "thread_id": thread_id,
                    "tab_id": tab_id,
                    "user_id": user_id,
                    "agent_id": agent_id,
                    "timeout_reminded": False,
                    "viewed_at": None,
                    "responded_at": None,
                },
            )
            session.add(record)
            await session.commit()

        async with self._lock:
            self._pending_events[request_id] = asyncio.Event()

        if self._notifier:
            await self._notifier.notify_request(record)

        self._setup_timeout(request_id, timeout, thread_id)

        logger.info(
            f"[HumanInteraction] 创建选择请求 | "
            f"request_id={request_id} | title={title}"
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
    ) -> str:
        """创建对话模式请求，返回 request_id"""
        request_id = str(uuid4())

        async with managed_session() as session:
            record = ExecutionRecord(
                id=request_id,
                session_id=session_id,
                type="interaction_request",
                status=InteractionStatus.PENDING.value,
                message_data={
                    "interaction_mode": InteractionMode.CONVERSATION.value,
                    "title": title,
                    "description": description,
                    "initial_message": initial_message,
                    "suggestions": suggestions,
                    "thread_id": thread_id,
                    "tab_id": tab_id,
                    "user_id": user_id,
                    "agent_id": agent_id,
                    "viewed_at": None,
                },
            )
            session.add(record)
            await session.commit()

        async with self._lock:
            self._pending_events[request_id] = asyncio.Event()

        if self._notifier:
            saved_record = await self._get_request(request_id)
            if saved_record:
                await self._notifier.notify_request(saved_record)
            await self._notifier.notify_conversation_start(
                thread_id=thread_id,
                tab_id=tab_id,
                title=title,
                request_id=request_id,
                initial_message=initial_message,
                suggestions=suggestions,
            )

        logger.info(
            f"[HumanInteraction] 创建对话请求 | "
            f"request_id={request_id} | thread_id={thread_id} | tab_id={tab_id}"
        )

        return request_id

    async def wait_for_conversation_arrival(
        self,
        request_id: str,
        timeout: float = 60.0,
    ) -> dict[str, Any]:
        """等待用户到达对话页面"""
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
        """等待用户选择"""
        event = self._pending_events.get(request_id)
        if not event:
            record = await self._get_request(request_id)
            if not record:
                raise ValueError(f"请求不存在: {request_id}")
            async with self._lock:
                self._pending_events[request_id] = asyncio.Event()
                event = self._pending_events[request_id]

        record = await self._get_request(request_id)
        if not record:
            raise ValueError(f"请求不存在: {request_id}")

        timeout = timeout or record.timeout_seconds or self._default_timeout

        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except TimeoutError:
            await self._handle_timeout(request_id)
            raise InteractionTimeoutError(request_id, timeout) from None

        response = await self._get_response(request_id)
        if not response:
            raise InteractionTimeoutError(request_id, timeout)

        if response.response_type == ResponseType.DENIED.value:
            raise InteractionDeniedError(request_id, response.feedback)

        if response.response_type == ResponseType.CANCELLED.value:
            raise InteractionCancelledError(request_id, response.feedback)

        return {
            "request_id": request_id,
            "response_type": response.response_type,
            "selected_option": response.selected_option,
            "answers": response.answers,
            "feedback": response.feedback,
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
        """提交响应"""
        request_record = await self._get_request(request_id)
        if not request_record:
            logger.warning(f"[HumanInteraction] 请求不存在 | request_id={request_id}")
            return False

        if request_record.status != InteractionStatus.PENDING.value:
            logger.warning(
                f"[HumanInteraction] 请求状态不允许响应 | "
                f"request_id={request_id} | status={request_record.status}"
            )
            return False

        response_id = str(uuid4())
        now = datetime.now(UTC)

        async with managed_session() as session:
            response_record = ExecutionRecord(
                id=response_id,
                session_id=request_record.session_id,
                parent_record_id=request_id,
                type="interaction_response",
                status="completed",
                message_data={
                    "request_id": request_id,
                    "response_type": response_type,
                    "selected_option": selected_option,
                    "answers": answers,
                    "feedback": feedback,
                    "user_id": user_id,
                },
            )
            session.add(response_record)

            await session.execute(
                update(ExecutionRecord)
                .where(ExecutionRecord.id == request_id)
                .values(
                    status=InteractionStatus.COMPLETED.value,
                    message_data=request_record.message_data
                    | {"responded_at": now.isoformat()},
                )
            )
            await session.commit()

        async with self._lock:
            if request_id in self._pending_events:
                self._pending_events[request_id].set()
            if request_id in self._timeout_tasks:
                self._timeout_tasks[request_id].cancel()
                del self._timeout_tasks[request_id]

        logger.info(
            f"[HumanInteraction] 响应已提交 | "
            f"request_id={request_id} | response_type={response_type}"
        )

        return True

    async def mark_as_viewed(self, request_id: str) -> bool:
        """标记请求为已查看，conversation 模式下触发到达通知"""
        async with managed_session() as session:
            result = await session.execute(
                update(ExecutionRecord)
                .where(ExecutionRecord.id == request_id)
                .where(ExecutionRecord.status == InteractionStatus.PENDING.value)
                .values(
                    status=InteractionStatus.VIEWED.value,
                    message_data=ExecutionRecord.message_data
                    | {"viewed_at": datetime.now(UTC).isoformat()},
                )
            )
            await session.commit()
            updated = result.rowcount > 0

        if updated:
            async with self._lock:
                if request_id in self._pending_events:
                    self._pending_events[request_id].set()

        return updated

    async def cancel_request(
        self,
        request_id: str,
        reason: str | None = None,
    ) -> bool:
        """取消请求"""
        request_record = await self._get_request(request_id)
        if not request_record:
            return False

        if request_record.status in (
            InteractionStatus.COMPLETED.value,
            InteractionStatus.TIMEOUT.value,
        ):
            return False

        async with managed_session() as session:
            await session.execute(
                update(ExecutionRecord)
                .where(ExecutionRecord.id == request_id)
                .values(status=InteractionStatus.CANCELLED.value)
            )
            await session.commit()

        async with self._lock:
            if request_id in self._pending_events:
                self._pending_events[request_id].set()
            if request_id in self._timeout_tasks:
                self._timeout_tasks[request_id].cancel()
                del self._timeout_tasks[request_id]

        if self._notifier:
            await self._notifier.notify_cancel(
                request_id, reason,
                thread_id=(request_record.message_data or {}).get("thread_id", "")
            )

        logger.info(
            f"[HumanInteraction] 请求已取消 | "
            f"request_id={request_id} | reason={reason}"
        )

        return True

    async def get_request(self, request_id: str) -> ExecutionRecord | None:
        """获取请求详情"""
        return await self._get_request(request_id)

    async def get_pending_requests(
        self,
        session_id: str | None = None,
        user_id: str | None = None,
        limit: int = 50,
    ) -> list[ExecutionRecord]:
        """获取待处理请求列表"""
        async with managed_session() as session:
            query = (
                select(ExecutionRecord)
                .where(ExecutionRecord.type == "interaction_request")
                .where(ExecutionRecord.status == InteractionStatus.PENDING.value)
            )

            if session_id:
                query = query.where(ExecutionRecord.session_id == session_id)

            query = query.order_by(ExecutionRecord.created_at.desc()).limit(limit)

            result = await session.execute(query)
            return list(result.scalars().all())

    async def get_interaction_history(
        self,
        session_id: str,
        limit: int = 100,
    ) -> list[ExecutionRecord]:
        """获取交互历史"""
        async with managed_session() as session:
            query = (
                select(ExecutionRecord)
                .where(ExecutionRecord.session_id == session_id)
                .where(
                    ExecutionRecord.type.in_(
                        ["interaction_request", "interaction_response"]
                    )
                )
                .order_by(ExecutionRecord.created_at.desc())
                .limit(limit)
            )

            result = await session.execute(query)
            return list(result.scalars().all())

    def set_notifier(self, notifier: IInteractionNotifier) -> None:
        """设置通知器"""
        self._notifier = notifier

    async def _get_request(self, request_id: str) -> ExecutionRecord | None:
        """获取请求记录"""
        async with managed_session() as session:
            result = await session.execute(
                select(ExecutionRecord).where(ExecutionRecord.id == request_id)
            )
            return result.scalar_one_or_none()

    async def _get_response(self, request_id: str) -> ExecutionRecord | None:
        """获取响应记录"""
        async with managed_session() as session:
            result = await session.execute(
                select(ExecutionRecord)
                .where(ExecutionRecord.parent_record_id == request_id)
                .where(ExecutionRecord.type == "interaction_response")
            )
            return result.scalar_one_or_none()

    def _setup_timeout(self, request_id: str, timeout_seconds: int, thread_id: str = ""):
        """设置超时任务"""

        async def timeout_handler():
            try:
                await asyncio.sleep(timeout_seconds - self._remind_before_seconds)

                request = await self._get_request(request_id)
                if request and request.status == InteractionStatus.PENDING.value:
                    if self._notifier:
                        msg_data = request.message_data or {}
                        await self._notifier.notify_timeout_reminder(
                            request_id, self._remind_before_seconds, thread_id,
                            title=msg_data.get("title", ""),
                            mode=msg_data.get("interaction_mode", "choice"),
                            options=msg_data.get("options"),
                            questions=msg_data.get("questions"),
                        )

                    async with managed_session() as session:
                        result = await session.execute(
                            select(ExecutionRecord.message_data).where(
                                ExecutionRecord.id == request_id
                            )
                        )
                        current_data = result.scalar_one_or_none()
                        if current_data is not None:
                            merged_data = {
                                **(current_data if isinstance(current_data, dict) else {}),
                                "timeout_reminded": True,
                            }
                            await session.execute(
                                update(ExecutionRecord)
                                .where(ExecutionRecord.id == request_id)
                                .values(message_data=merged_data)
                            )
                            await session.commit()

                await asyncio.sleep(self._remind_before_seconds)

                request = await self._get_request(request_id)
                if request and request.status == InteractionStatus.PENDING.value:
                    await self._handle_timeout(request_id)

            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"[HumanInteraction] 超时处理失败 | error={e}")

        task = asyncio.create_task(timeout_handler())
        self._timeout_tasks[request_id] = task

    async def _handle_timeout(self, request_id: str):
        """处理超时"""
        request = await self._get_request(request_id)
        thread_id = (request.message_data or {}).get("thread_id", "") if request else ""

        async with managed_session() as session:
            await session.execute(
                update(ExecutionRecord)
                .where(ExecutionRecord.id == request_id)
                .where(ExecutionRecord.status == InteractionStatus.PENDING.value)
                .values(status=InteractionStatus.TIMEOUT.value)
            )
            await session.commit()

        async with self._lock:
            if request_id in self._pending_events:
                self._pending_events[request_id].set()

        if self._notifier:
            await self._notifier.notify_timeout(request_id, thread_id=thread_id)

        logger.info(f"[HumanInteraction] 请求超时 | request_id={request_id}")


_service_instance: HumanInteractionService | None = None


def get_human_interaction_service() -> HumanInteractionService:
    """获取服务实例"""
    global _service_instance
    if _service_instance is None:
        _service_instance = HumanInteractionService()
    return _service_instance


def set_human_interaction_service(service: HumanInteractionService) -> None:
    """设置服务实例"""
    global _service_instance
    _service_instance = service


def reset_human_interaction_service() -> None:
    """重置服务实例（用于测试）"""
    global _service_instance
    _service_instance = None
