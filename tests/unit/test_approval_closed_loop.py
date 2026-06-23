"""审批交互闭环测试。

验证管道挂起→推送 interaction_request→用户 resume_action(approved:true/false)→管道恢复/终止的完整流程。
对应需求：F-UI-09, AC-UI-03

覆盖内容：
1. HumanInteractionService 创建请求 → 等待 → 提交响应的异步闭环
2. WebSocketInteractionNotifier 推送 interaction_request 消息格式
3. interaction_cancelled 消息格式
4. 审批通过/拒绝的完整流程
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from human_interaction.service import (
    HumanInteractionService,
    InteractionCancelledError,
    InteractionDeniedError,
)


# ---------------------------------------------------------------------------
# Mock Notifier — 捕获推送的消息
# ---------------------------------------------------------------------------


class MockInteractionNotifier:
    """模拟交互通知器，捕获所有推送的 interaction_request/cancelled 消息。"""

    def __init__(self) -> None:
        self.sent_requests: list[dict[str, Any]] = []
        self.sent_cancels: list[tuple[str, str | None]] = []
        self.sent_timeouts: list[str] = []
        self._fallback_tasks: set = set()
        self._fallback_request_map: dict[str, Any] = {}

    async def notify_request(self, request: dict | Any) -> bool:
        record = request if isinstance(request, dict) else {}
        self.sent_requests.append(record)
        return True

    async def notify_cancel(
        self, request_id: str, reason: str | None = None, thread_id: str = ""
    ) -> bool:
        self.sent_cancels.append((request_id, reason))
        return True

    async def notify_timeout(self, request_id: str, thread_id: str = "") -> bool:
        self.sent_timeouts.append(request_id)
        return True

    async def notify_timeout_reminder(
        self, request_id: str, remaining_seconds: int, thread_id: str = "", **kw
    ) -> bool:
        return True

    async def notify_conversation_start(
        self, thread_id: str, tab_id: str, title: str, **kw
    ) -> bool:
        return True

    def cancel_fallback(self, request_id: str) -> None:
        task = self._fallback_request_map.pop(request_id, None)
        if task and not task.done():
            task.cancel()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def notifier() -> MockInteractionNotifier:
    return MockInteractionNotifier()


@pytest.fixture
def service(notifier: MockInteractionNotifier) -> HumanInteractionService:
    return HumanInteractionService(
        notifier=notifier,
        default_timeout=30.0,
    )


# ===========================================================================
# 一、审批请求创建与通知
# ===========================================================================


class TestApprovalRequestCreation:
    """审批请求创建 → 推送 interaction_request → 前端接收。

    验证点（F-UI-09, AC-UI-03）：工具调用需要用户确认时弹出审批框。
    """

    @pytest.mark.asyncio
    async def test_create_choice_request_notifies_frontend(
        self,
        service: HumanInteractionService,
        notifier: MockInteractionNotifier,
    ) -> None:
        """创建审批请求 → notifier 收到 interaction_request。"""
        request_id = await service.create_choice_request(
            session_id="session-001",
            thread_id="thread-001",
            tab_id="tab-001",
            title="确认执行 file_write？",
            description="将在 workspace 写入文件",
            options=[
                {"id": "approve", "label": "批准"},
                {"id": "reject", "label": "拒绝"},
            ],
        )

        assert request_id, "应返回非空 request_id"
        assert len(notifier.sent_requests) == 1, "应推送 1 个 interaction_request"
        record = notifier.sent_requests[0]
        assert record["type"] == "interaction_request"
        assert record["id"] == request_id

    @pytest.mark.asyncio
    async def test_request_stored_in_pending(
        self, service: HumanInteractionService
    ) -> None:
        """创建的请求存储在 _requests 中且状态为 pending。"""
        request_id = await service.create_choice_request(
            session_id="session-001",
            thread_id="thread-001",
            tab_id="",
            title="审批",
        )

        record = service._requests.get(request_id)
        assert record is not None
        assert record["status"] == "pending"


# ===========================================================================
# 二、审批通过闭环（approved: true）
# ===========================================================================


class TestApprovalApproved:
    """审批通过闭环：创建→等待→submit_response(approved)→wait_for_choice 返回。"""

    @pytest.mark.asyncio
    async def test_approve_resumes_waiting(
        self, service: HumanInteractionService
    ) -> None:
        """用户批准 → wait_for_choice 返回 selected_option=approve。

        验证点（AC-UI-03）：approved:true → 管道恢复。
        """
        request_id = await service.create_choice_request(
            session_id="session-001",
            thread_id="thread-001",
            tab_id="",
            title="确认执行",
            options=[
                {"id": "approve", "label": "批准"},
                {"id": "reject", "label": "拒绝"},
            ],
            timeout_seconds=5,
        )

        async def delayed_approve():
            await asyncio.sleep(0.1)
            await service.submit_response(
                request_id=request_id,
                response_type="approved",
                selected_option="approve",
            )

        approve_task = asyncio.create_task(delayed_approve())
        result = await service.wait_for_choice(request_id)
        await approve_task

        assert result["selected_option"] == "approve"
        assert result["response_type"] == "approved"

    @pytest.mark.asyncio
    async def test_approve_updates_request_status(
        self, service: HumanInteractionService
    ) -> None:
        """批准后请求状态变为 completed。"""
        request_id = await service.create_choice_request(
            session_id="session-001",
            thread_id="thread-001",
            tab_id="",
            title="审批",
            timeout_seconds=5,
        )

        await service.submit_response(
            request_id=request_id,
            response_type="approved",
            selected_option="approve",
        )

        record = service._requests[request_id]
        assert record["status"] == "completed"


# ===========================================================================
# 三、审批拒绝闭环（approved: false）
# ===========================================================================


class TestApprovalRejected:
    """审批拒绝闭环：创建→等待→submit_response(denied)→wait_for_choice 抛异常。"""

    @pytest.mark.asyncio
    async def test_reject_raises_denied_error(
        self, service: HumanInteractionService
    ) -> None:
        """用户拒绝 → wait_for_choice 抛出 InteractionDeniedError。

        验证点（AC-UI-03）：approved:false → 管道终止。
        """
        request_id = await service.create_choice_request(
            session_id="session-001",
            thread_id="thread-001",
            tab_id="",
            title="确认执行",
            timeout_seconds=5,
        )

        async def delayed_reject():
            await asyncio.sleep(0.1)
            await service.submit_response(
                request_id=request_id,
                response_type="denied",
                selected_option="reject",
                feedback="风险太高",
            )

        reject_task = asyncio.create_task(delayed_reject())

        with pytest.raises(InteractionDeniedError) as exc_info:
            await service.wait_for_choice(request_id)

        await reject_task
        assert "拒绝" in str(exc_info.value) or "denied" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_cancel_raises_cancelled_error(
        self, service: HumanInteractionService
    ) -> None:
        """用户取消 → wait_for_choice 抛出 InteractionCancelledError。"""
        request_id = await service.create_choice_request(
            session_id="session-001",
            thread_id="thread-001",
            tab_id="",
            title="确认执行",
            timeout_seconds=5,
        )

        async def delayed_cancel():
            await asyncio.sleep(0.1)
            await service.submit_response(
                request_id=request_id,
                response_type="cancelled",
            )

        cancel_task = asyncio.create_task(delayed_cancel())

        with pytest.raises(InteractionCancelledError):
            await service.wait_for_choice(request_id)

        await cancel_task


# ===========================================================================
# 四、交互取消通知
# ===========================================================================


class TestInteractionCancel:
    """interaction_cancelled 消息推送。"""

    @pytest.mark.asyncio
    async def test_cancel_request_notifies_frontend(
        self,
        service: HumanInteractionService,
        notifier: MockInteractionNotifier,
    ) -> None:
        """cancel_request → 推送 interaction_cancelled 消息。

        验证点（需求 §2.1）：interaction_cancelled 事件格式正确。
        """
        request_id = await service.create_choice_request(
            session_id="session-001",
            thread_id="thread-001",
            tab_id="",
            title="审批",
            timeout_seconds=30,
        )

        await service.cancel_request(request_id, reason="用户关闭页面")

        assert len(notifier.sent_cancels) == 1
        cancelled_id, reason = notifier.sent_cancels[0]
        assert cancelled_id == request_id
        assert reason == "用户关闭页面"

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_request_no_crash(
        self, service: HumanInteractionService
    ) -> None:
        """取消不存在的请求不崩溃。"""
        result = await service.cancel_request("nonexistent-id")
        assert result is False or result is None


# ===========================================================================
# 五、重复响应防护
# ===========================================================================


class TestDuplicateResponseGuard:
    """重复响应防护。"""

    @pytest.mark.asyncio
    async def test_double_submit_returns_false(
        self, service: HumanInteractionService
    ) -> None:
        """已完成的请求再次提交响应返回 False。"""
        request_id = await service.create_choice_request(
            session_id="session-001",
            thread_id="thread-001",
            tab_id="",
            title="审批",
            timeout_seconds=5,
        )

        first = await service.submit_response(
            request_id=request_id,
            response_type="approved",
            selected_option="approve",
        )
        second = await service.submit_response(
            request_id=request_id,
            response_type="approved",
            selected_option="approve",
        )

        assert first is True, "第一次提交应成功"
        assert second is False, "第二次提交应失败（状态已变更）"

    @pytest.mark.asyncio
    async def test_submit_to_nonexistent_returns_false(
        self, service: HumanInteractionService
    ) -> None:
        """向不存在的请求提交响应返回 False。"""
        result = await service.submit_response(
            request_id="nonexistent",
            response_type="approved",
        )
        assert result is False


# ===========================================================================
# 六、respond 方法（前端路由入口）
# ===========================================================================


class TestRespondMethod:
    """respond 方法是前端 WebSocket 消息的解析入口。"""

    @pytest.mark.asyncio
    async def test_respond_with_nested_data(
        self, service: HumanInteractionService
    ) -> None:
        """前端发送嵌套结构的响应数据 → 正确解析。"""
        request_id = await service.create_choice_request(
            session_id="session-001",
            thread_id="thread-001",
            tab_id="",
            title="审批",
            timeout_seconds=5,
        )

        result = await service.respond(
            request_id,
            {
                "response": {
                    "response_type": "approved",
                    "selected_option": "approve",
                    "feedback": "同意执行",
                }
            },
        )

        assert result is True
        record = service._requests[request_id]
        assert record["status"] == "completed"
