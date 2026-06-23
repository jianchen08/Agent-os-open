"""审批交互闭环测试。

验证审批流程完整闭环：管道挂起 → interaction_request → resume_action → 管道恢复/终止。
对应需求：F-UI-09, AC-UI-03

测试覆盖：
- 交互请求创建（create_choice_request / create_conversation_request）
- 请求状态流转（pending → completed）
- 响应提交（approved/denied → 管道恢复/终止）
- 超时处理
- 交互取消
- interaction_request 事件格式
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from human_interaction.models import (
    InteractionMode,
    InteractionStatus,
    Priority,
    ResponseType,
)
from human_interaction.service import (
    HumanInteractionService,
    InteractionCancelledError,
    InteractionDeniedError,
    InteractionTimeoutError,
)


# ---------------------------------------------------------------------------
# 测试 fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def service() -> HumanInteractionService:
    """创建 HumanInteractionService 实例（短超时便于测试）。"""
    return HumanInteractionService(default_timeout=2.0)


# ---------------------------------------------------------------------------
# 交互请求创建
# ---------------------------------------------------------------------------


class TestInteractionRequestCreation:
    """交互请求创建测试。"""

    @pytest.mark.asyncio
    async def test_choice_request_creates_pending_record(
        self, service: HumanInteractionService,
    ) -> None:
        """创建 choice 模式交互请求，状态为 pending。

        验证点：
        - create_choice_request() 返回 request_id
        - 请求记录 status == pending
        - message_data 包含正确的 mode/title/thread_id
        """
        request_id = await service.create_choice_request(
            session_id="session-001",
            thread_id="thread-001",
            tab_id="tab-001",
            title="确认操作",
            description="需要用户确认",
            options=[{"label": "确认", "value": "yes"}, {"label": "取消", "value": "no"}],
        )

        assert request_id, "应返回非空 request_id"

        record = service._requests.get(request_id)
        assert record is not None, "请求记录应存在"
        assert record["status"] == InteractionStatus.PENDING.value
        assert record["type"] == "interaction_request"

        msg_data = record["message_data"]
        assert msg_data["interaction_mode"] == "choice"
        assert msg_data["title"] == "确认操作"
        assert msg_data["thread_id"] == "thread-001"

    @pytest.mark.asyncio
    async def test_conversation_request_creates_pending_record(
        self, service: HumanInteractionService,
    ) -> None:
        """conversation 模式交互请求创建。"""
        request_id = await service.create_conversation_request(
            session_id="session-002",
            thread_id="thread-002",
            tab_id="tab-002",
            title="需要更多信息",
        )
        assert request_id

        record = service._requests.get(request_id)
        assert record["message_data"]["interaction_mode"] == "conversation"

    @pytest.mark.asyncio
    async def test_request_with_high_priority(
        self, service: HumanInteractionService,
    ) -> None:
        """带 HIGH 优先级的交互请求。"""
        request_id = await service.create_choice_request(
            session_id="session-003",
            thread_id="thread-003",
            tab_id="tab-003",
            title="紧急确认",
            priority=Priority.HIGH,
        )

        record = service._requests.get(request_id)
        assert record["message_data"]["priority"] == "high"

    @pytest.mark.asyncio
    async def test_request_with_agent_level_and_file_paths(
        self, service: HumanInteractionService,
    ) -> None:
        """请求携带 agent_level 和 file_paths（extra 数据）。"""
        request_id = await service.create_choice_request(
            session_id="session-004",
            thread_id="thread-004",
            tab_id="tab-004",
            title="确认文件操作",
            file_paths=["/test/file.py"],
            agent_level="L2",
        )

        record = service._requests.get(request_id)
        msg_data = record["message_data"]
        assert msg_data.get("file_paths") == ["/test/file.py"]
        assert msg_data.get("agent_level") == "L2"


# ---------------------------------------------------------------------------
# 响应提交 → 状态流转（审批闭环核心）
# ---------------------------------------------------------------------------


class TestInteractionResponse:
    """交互响应处理测试（审批闭环核心）。"""

    @pytest.mark.asyncio
    async def test_submit_approved_resolves_wait(
        self, service: HumanInteractionService,
    ) -> None:
        """提交 approved 响应 → wait_for_choice 返回 approved，请求状态 completed。

        验证点（AC-UI-03）：
        - submit_response(approved) 返回 True
        - wait_for_choice 被唤醒并返回 approved 结果
        - 请求状态变为 completed
        """
        request_id = await service.create_choice_request(
            session_id="s1", thread_id="t1", tab_id="tab1",
            title="确认", options=[{"label": "OK", "value": "ok"}],
        )

        wait_result, _ = await asyncio.gather(
            service.wait_for_choice(request_id, timeout=3.0),
            _delayed_respond(service, request_id, ResponseType.APPROVED),
        )

        assert wait_result["response_type"] == ResponseType.APPROVED.value
        record = service._requests.get(request_id)
        assert record["status"] == InteractionStatus.COMPLETED.value

    @pytest.mark.asyncio
    async def test_submit_denied_raises_error(
        self, service: HumanInteractionService,
    ) -> None:
        """提交 denied 响应 → wait_for_choice 抛出 InteractionDeniedError。

        验证点（AC-UI-03）：
        - denied 响应导致管道终止
        """
        request_id = await service.create_choice_request(
            session_id="s2", thread_id="t2", tab_id="tab2",
            title="确认", options=[{"label": "OK", "value": "ok"}],
        )

        with pytest.raises(InteractionDeniedError):
            await asyncio.gather(
                service.wait_for_choice(request_id, timeout=3.0),
                _delayed_respond(service, request_id, ResponseType.DENIED),
            )

    @pytest.mark.asyncio
    async def test_submit_cancelled_raises_error(
        self, service: HumanInteractionService,
    ) -> None:
        """提交 cancelled 响应 → wait_for_choice 抛出 InteractionCancelledError。"""
        request_id = await service.create_choice_request(
            session_id="s3", thread_id="t3", tab_id="tab3", title="确认",
        )

        with pytest.raises(InteractionCancelledError):
            await asyncio.gather(
                service.wait_for_choice(request_id, timeout=3.0),
                _delayed_respond(service, request_id, ResponseType.CANCELLED),
            )

    @pytest.mark.asyncio
    async def test_double_response_rejected(
        self, service: HumanInteractionService,
    ) -> None:
        """已完成的请求不接受二次响应。"""
        request_id = await service.create_choice_request(
            session_id="s4", thread_id="t4", tab_id="tab4", title="确认",
        )

        await asyncio.gather(
            service.wait_for_choice(request_id, timeout=3.0),
            _delayed_respond(service, request_id, ResponseType.APPROVED),
        )

        second = await service.submit_response(
            request_id=request_id,
            response_type=ResponseType.APPROVED.value,
        )
        assert second is False, "已完成请求不应接受二次响应"

    @pytest.mark.asyncio
    async def test_respond_to_nonexistent_request(
        self, service: HumanInteractionService,
    ) -> None:
        """对不存在的请求提交响应 → 返回 False。"""
        result = await service.submit_response(
            request_id="nonexistent-id",
            response_type=ResponseType.APPROVED.value,
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_submit_with_selected_option(
        self, service: HumanInteractionService,
    ) -> None:
        """提交带 selected_option 的响应。"""
        request_id = await service.create_choice_request(
            session_id="s5", thread_id="t5", tab_id="tab5",
            title="选择方案",
            options=[{"label": "A", "value": "a"}, {"label": "B", "value": "b"}],
        )

        wait_result, _ = await asyncio.gather(
            service.wait_for_choice(request_id, timeout=3.0),
            _delayed_respond(
                service, request_id, ResponseType.APPROVED,
                selected_option="b",
            ),
        )

        assert wait_result["selected_option"] == "b"


# ---------------------------------------------------------------------------
# 超时处理
# ---------------------------------------------------------------------------


class TestInteractionTimeout:
    """交互超时测试。"""

    @pytest.mark.asyncio
    async def test_request_timeout_raises_error(
        self, service: HumanInteractionService,
    ) -> None:
        """超时后 wait_for_choice 抛出 InteractionTimeoutError。"""
        request_id = await service.create_choice_request(
            session_id="s6", thread_id="t6", tab_id="tab6", title="超时测试",
        )

        with pytest.raises(InteractionTimeoutError):
            await service.wait_for_choice(request_id, timeout=0.5)


# ---------------------------------------------------------------------------
# 交互取消
# ---------------------------------------------------------------------------


class TestInteractionCancel:
    """交互取消测试。"""

    @pytest.mark.asyncio
    async def test_cancel_pending_request(
        self, service: HumanInteractionService,
    ) -> None:
        """取消 pending 请求 → 状态变为 cancelled。"""
        request_id = await service.create_choice_request(
            session_id="s7", thread_id="t7", tab_id="tab7", title="取消测试",
        )

        result = await service.cancel_request(request_id, reason="测试取消")
        assert result is True

        record = service._requests.get(request_id)
        assert record["status"] == InteractionStatus.CANCELLED.value

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_request(
        self, service: HumanInteractionService,
    ) -> None:
        """取消不存在的请求 → 返回 False。"""
        result = await service.cancel_request("nonexistent", reason="test")
        assert result is False


# ---------------------------------------------------------------------------
# interaction_request 事件格式
# ---------------------------------------------------------------------------


class TestInteractionRequestFormat:
    """interaction_request 事件格式验证（AC-UI-03）。"""

    @pytest.mark.asyncio
    async def test_record_has_all_protocol_fields(
        self, service: HumanInteractionService,
    ) -> None:
        """交互请求记录包含协议要求的所有字段。

        验证点（对应需求文档 §2.1）：
        - type == "interaction_request"
        - message_data 包含 interaction_mode, title, thread_id, tab_id
        - status == "pending"
        """
        request_id = await service.create_choice_request(
            session_id="format-session",
            thread_id="format-thread",
            tab_id="format-tab",
            title="审批请求",
            description="需要用户确认是否执行",
        )

        record = service._requests.get(request_id)
        assert record is not None

        assert record["type"] == "interaction_request"
        assert record["status"] == "pending"

        msg_data = record["message_data"]
        required_fields = [
            "interaction_mode", "title", "thread_id",
            "tab_id", "user_id", "agent_id",
        ]
        for field in required_fields:
            assert field in msg_data, f"interaction_request 缺少字段: {field}"

    @pytest.mark.asyncio
    async def test_get_request_returns_record(
        self, service: HumanInteractionService,
    ) -> None:
        """get_request 返回请求记录。"""
        request_id = await service.create_choice_request(
            session_id="s8", thread_id="t8", tab_id="tab8", title="查询测试",
        )

        record = await service.get_request(request_id)
        assert record is not None
        assert record["type"] == "interaction_request"

    @pytest.mark.asyncio
    async def test_get_pending_requests(
        self, service: HumanInteractionService,
    ) -> None:
        """get_pending_requests 返回 pending 状态的请求。"""
        await service.create_choice_request(
            session_id="s9", thread_id="t9", tab_id="tab9", title="待处理1",
        )
        await service.create_choice_request(
            session_id="s10", thread_id="t10", tab_id="tab10", title="待处理2",
        )

        pending = await service.get_pending_requests()
        assert len(pending) >= 2, "应至少返回 2 个 pending 请求"


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


async def _delayed_respond(
    service: HumanInteractionService,
    request_id: str,
    response_type: ResponseType,
    selected_option: str | None = None,
    delay: float = 0.1,
) -> None:
    """延迟后提交响应，让 wait_for_choice 先进入等待。"""
    await asyncio.sleep(delay)
    await service.submit_response(
        request_id=request_id,
        response_type=response_type.value,
        selected_option=selected_option,
    )
