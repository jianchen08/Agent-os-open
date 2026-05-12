"""
人类交互服务实现

提供统一的人类交互服务实现
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.core.human_interaction.interfaces import (
    IHumanInteractionService,
    IInteractionNotifier,
)
from src.core.human_interaction.models import (
    InteractionRequest,
    InteractionResponse,
    InteractionStatus,
    InteractionType,
    Priority,
    ResponseType,
    TimeoutAction,
)

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


@dataclass
class AutoApprovalConfig:
    """自动审批配置"""

    whitelist: list[str] = field(
        default_factory=lambda: [
            "read_file",
            "list_directory",
            "search_content",
            "run_tests",
            "get_file_info",
            "search_code",
        ]
    )
    blacklist: list[str] = field(
        default_factory=lambda: [
            "delete_file",
            "execute_shell_dangerous",
            "modify_system_config",
            "write_file",
        ]
    )
    auto_approve_threshold: int = 3
    default_timeout: float = 300.0


class HumanInteractionService(IHumanInteractionService):
    """
    人类交互服务实现

    统一处理所有需要人类参与的场景：
    - 审批模式：工具调用确认、任务审批、高风险操作
    - 对话模式：需求澄清、复杂问题讨论、人机协作
    """

    def __init__(
        self,
        config: AutoApprovalConfig | None = None,
        notifier: IInteractionNotifier | None = None,
    ):
        self._config = config or AutoApprovalConfig()
        self._notifier = notifier

        self._requests: dict[str, InteractionRequest] = {}
        self._responses: dict[str, InteractionResponse] = {}
        self._pending_events: dict[str, asyncio.Event] = {}
        self._history: list[InteractionResponse] = []

        self._lock = asyncio.Lock()

    async def request_interaction(
        self,
        request: InteractionRequest,
    ) -> str:
        request_id = request.request_id
        request.created_at = datetime.now()
        request.updated_at = datetime.now()

        if request.context and await self.should_auto_approve(
            request.context.operation, request.context.risk_level
        ):
            request.status = InteractionStatus.AUTO_APPROVED
            logger.info(
                f"[HumanInteraction] 请求自动批准 | "
                f"request_id={request_id} | "
                f"operation={request.context.operation}"
            )

            auto_response = InteractionResponse(
                request_id=request_id,
                response_type=ResponseType.APPROVED,
                reason="自动审批",
                user_id="system",
            )
            self._responses[request_id] = auto_response
            self._history.append(auto_response)
        else:
            request.status = InteractionStatus.PENDING
            if self._notifier:
                try:
                    await self._notifier.notify_request(request)
                    logger.info(
                        f"[HumanInteraction] 请求已通知 | "
                        f"request_id={request_id} | "
                        f"type={request.interaction_type.value}"
                    )
                except Exception as e:
                    logger.error(
                        f"[HumanInteraction] 通知失败 | "
                        f"request_id={request_id} | error={e}"
                    )

        async with self._lock:
            self._requests[request_id] = request
            self._pending_events[request_id] = asyncio.Event()

        return request_id

    async def wait_for_response(
        self,
        request_id: str,
        timeout: float | None = None,
    ) -> InteractionResponse:
        request = self._requests.get(request_id)
        if not request:
            raise ValueError(f"请求不存在: {request_id}")

        if request.status == InteractionStatus.AUTO_APPROVED:
            return self._responses[request_id]

        if request.status == InteractionStatus.CANCELLED:
            raise InteractionCancelledError(request_id, "请求已被取消")

        if request.status == InteractionStatus.COMPLETED:
            response = self._responses.get(request_id)
            if response:
                return response

        timeout = timeout or request.timeout
        event = self._pending_events.get(request_id)

        if event:
            try:
                await asyncio.wait_for(event.wait(), timeout=timeout)
            except TimeoutError:
                request.status = InteractionStatus.TIMEOUT
                request.updated_at = datetime.now()

                if self._notifier:
                    await self._notifier.notify_timeout(request_id)

                if request.timeout_action == TimeoutAction.AUTO_APPROVE:
                    return InteractionResponse(
                        request_id=request_id,
                        response_type=ResponseType.APPROVED,
                        reason="超时自动批准",
                        user_id="system",
                    )
                elif request.timeout_action == TimeoutAction.IGNORE:
                    return InteractionResponse(
                        request_id=request_id,
                        response_type=ResponseType.TIMEOUT,
                        reason="超时忽略",
                        user_id="system",
                    )
                else:
                    raise InteractionTimeoutError(request_id, timeout)

        response = self._responses.get(request_id)
        if not response:
            raise InteractionTimeoutError(request_id, timeout)

        if response.response_type == ResponseType.DENIED:
            raise InteractionDeniedError(request_id, response.reason)

        return response

    async def submit_response(
        self,
        request_id: str,
        response: InteractionResponse,
    ) -> bool:
        request = self._requests.get(request_id)
        if not request:
            logger.warning(f"[HumanInteraction] 提交响应失败，请求不存在 | request_id={request_id}")
            return False

        if request.status not in (InteractionStatus.PENDING, InteractionStatus.PROCESSING):
            logger.warning(
                f"[HumanInteraction] 提交响应失败，请求状态不允许 | "
                f"request_id={request_id} | status={request.status.value}"
            )
            return False

        request.status = InteractionStatus.COMPLETED
        request.updated_at = datetime.now()

        response.responded_at = datetime.now()
        self._responses[request_id] = response
        self._history.append(response)

        if request_id in self._pending_events:
            self._pending_events[request_id].set()

        logger.info(
            f"[HumanInteraction] 响应已提交 | "
            f"request_id={request_id} | "
            f"response_type={response.response_type.value}"
        )

        return True

    async def cancel_request(
        self,
        request_id: str,
        reason: str | None = None,
    ) -> bool:
        request = self._requests.get(request_id)
        if not request:
            return False

        if request.status in (InteractionStatus.COMPLETED, InteractionStatus.TIMEOUT):
            return False

        request.status = InteractionStatus.CANCELLED
        request.updated_at = datetime.now()

        cancel_response = InteractionResponse(
            request_id=request_id,
            response_type=ResponseType.CANCELLED,
            reason=reason,
            user_id="system",
        )
        self._responses[request_id] = cancel_response

        if self._notifier:
            await self._notifier.notify_cancel(request_id, reason)

        if request_id in self._pending_events:
            self._pending_events[request_id].set()

        logger.info(
            f"[HumanInteraction] 请求已取消 | "
            f"request_id={request_id} | reason={reason}"
        )

        return True

    async def get_request(self, request_id: str) -> InteractionRequest | None:
        return self._requests.get(request_id)

    async def get_request_status(self, request_id: str) -> InteractionStatus | None:
        request = self._requests.get(request_id)
        return request.status if request else None

    async def get_pending_requests(
        self,
        thread_id: str | None = None,
        agent_id: str | None = None,
        priority: Priority | None = None,
        interaction_type: InteractionType | None = None,
        limit: int = 50,
    ) -> list[InteractionRequest]:
        requests = [
            req
            for req in self._requests.values()
            if req.status == InteractionStatus.PENDING
        ]

        if thread_id:
            requests = [r for r in requests if r.thread_id == thread_id]
        if agent_id:
            requests = [r for r in requests if r.agent_id == agent_id]
        if priority:
            requests = [r for r in requests if r.priority == priority]
        if interaction_type:
            requests = [r for r in requests if r.interaction_type == interaction_type]

        priority_order = {
            Priority.CRITICAL: 0,
            Priority.HIGH: 1,
            Priority.NORMAL: 2,
            Priority.LOW: 3,
        }
        requests.sort(key=lambda r: (priority_order.get(r.priority, 2), r.created_at))

        return requests[:limit]

    async def approve(
        self,
        request_id: str,
        option_id: str | None = None,
        reason: str | None = None,
        user_id: str | None = None,
    ) -> InteractionResponse:
        response = InteractionResponse.create_approval_response(
            request_id=request_id,
            approved=True,
            option_id=option_id,
            reason=reason,
            user_id=user_id,
        )
        await self.submit_response(request_id, response)
        return response

    async def deny(
        self,
        request_id: str,
        reason: str | None = None,
        user_id: str | None = None,
    ) -> InteractionResponse:
        response = InteractionResponse.create_approval_response(
            request_id=request_id,
            approved=False,
            reason=reason,
            user_id=user_id,
        )
        await self.submit_response(request_id, response)
        return response

    async def modify_and_approve(
        self,
        request_id: str,
        modified_data: dict[str, Any],
        reason: str | None = None,
        user_id: str | None = None,
    ) -> InteractionResponse:
        response = InteractionResponse.create_approval_response(
            request_id=request_id,
            approved=True,
            modified_data=modified_data,
            reason=reason,
            user_id=user_id,
        )
        await self.submit_response(request_id, response)
        return response

    async def end_conversation(
        self,
        request_id: str,
        result: str,
        user_id: str | None = None,
    ) -> InteractionResponse:
        request = self._requests.get(request_id)
        messages = []
        if request and request.conversation_context:
            messages = request.conversation_context.history

        response = InteractionResponse.create_conversation_end_response(
            request_id=request_id,
            result=result,
            messages=messages,
            user_id=user_id,
        )
        await self.submit_response(request_id, response)
        return response

    def set_notifier(self, notifier: IInteractionNotifier) -> None:
        self._notifier = notifier

    async def should_auto_approve(
        self,
        operation: str,
        risk_level: int,
    ) -> bool:
        if operation in self._config.blacklist:
            return False

        if operation in self._config.whitelist:
            return True

        if risk_level <= self._config.auto_approve_threshold:
            return True

        return False

    def configure_auto_approval(
        self,
        whitelist: list[str] | None = None,
        blacklist: list[str] | None = None,
        threshold: int | None = None,
    ) -> None:
        if whitelist is not None:
            self._config.whitelist = whitelist
        if blacklist is not None:
            self._config.blacklist = blacklist
        if threshold is not None:
            self._config.auto_approve_threshold = threshold

    async def get_history(
        self,
        limit: int = 100,
    ) -> list[InteractionResponse]:
        return self._history[-limit:]

    async def cleanup_expired(self) -> int:
        now = datetime.now()
        expired_count = 0

        async with self._lock:
            expired_ids = [
                req_id
                for req_id, req in self._requests.items()
                if req.expires_at and req.expires_at < now
                and req.status == InteractionStatus.PENDING
            ]

            for req_id in expired_ids:
                await self.cancel_request(req_id, "请求已过期")
                expired_count += 1

        if expired_count > 0:
            logger.info(f"[HumanInteraction] 清理过期请求 | count={expired_count}")

        return expired_count


_service_instance: HumanInteractionService | None = None


def get_human_interaction_service() -> HumanInteractionService:
    global _service_instance
    if _service_instance is None:
        _service_instance = HumanInteractionService()
    return _service_instance


def set_human_interaction_service(service: HumanInteractionService) -> None:
    global _service_instance
    _service_instance = service


def reset_human_interaction_service() -> None:
    """重置服务实例（用于测试）"""
    global _service_instance
    _service_instance = None
