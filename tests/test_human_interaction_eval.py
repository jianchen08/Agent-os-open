"""
Human Interaction 评估逻辑测试

验证 Bug 2 (Choice 模式拒绝应判定为不通过) 和
Bug 3 (Conversation 模式不应自动通过) 的修复。
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest


# ── helpers ──────────────────────────────────────────────────────

def _make_tool() -> "HumanInteractionTool":
    from tools.builtin.human_interaction.tool import HumanInteractionTool
    return HumanInteractionTool(pipeline_id="test_pipeline")


class _FakeService:
    """模拟 HumanInteractionService，可控制 wait_for_choice 的返回值。"""

    def __init__(self, *, response: dict[str, Any] | None = None,
                 deny: bool = False, cancel: bool = False):
        self._response = response
        self._deny = deny
        self._cancel = cancel

    async def create_choice_request(self, **kwargs: Any) -> str:
        return "req_001"

    async def create_conversation_request(self, **kwargs: Any) -> str:
        return "req_002"

    async def wait_for_choice(self, request_id: str, **kwargs: Any) -> dict[str, Any]:
        from human_interaction.service import (
            InteractionCancelledError,
            InteractionDeniedError,
        )
        if self._deny:
            raise InteractionDeniedError(request_id, "用户拒绝")
        if self._cancel:
            raise InteractionCancelledError(request_id, "用户取消")
        return self._response or {}

    async def cancel_request(self, *args: Any, **kwargs: Any) -> bool:
        return True

    async def send_notification(self, **kwargs: Any) -> str:
        return "req_003"


def _result_to_dict(result: Any) -> dict[str, Any]:
    """将 ToolExecutionResult 转为字典，与评估引擎处理方式一致。"""
    return result.to_dict()


# ── Bug 2: Choice 模式评估逻辑 ────────────────────────────────

class TestChoiceModeEvaluation:
    """验证 Choice 模式的评估判定逻辑。"""

    @pytest.mark.asyncio
    async def test_choice_approve_selected_option_is_approve(self) -> None:
        """用户选"通过"→ data.selected_option == 'approve' → 评估应通过"""
        tool = _make_tool()
        svc = _FakeService(response={
            "response_type": "approved",
            "selected_option": "approve",
            "feedback": "确认通过",
        })

        result = await tool._execute_choice_mode(
            inputs={"mode": "choice", "title": "审批", "timeout_seconds": 60},
            service=svc,
            pipeline_id="test_pipeline",
        )
        d = _result_to_dict(result)

        assert d["success"] is True
        assert d["data"]["selected_option"] == "approve"

    @pytest.mark.asyncio
    async def test_choice_reject_explicit_selected_option(self) -> None:
        """用户拒绝→ data.selected_option 应为 'reject'（非 None）→ 评估不通过"""
        tool = _make_tool()
        svc = _FakeService(deny=True)

        result = await tool._execute_choice_mode(
            inputs={"mode": "choice", "title": "审批", "timeout_seconds": 60},
            service=svc,
            pipeline_id="test_pipeline",
        )
        d = _result_to_dict(result)

        # success=True（工具执行成功），但 selected_option="reject"
        assert d["success"] is True
        assert d["data"]["selected_option"] == "reject", (
            "拒绝时 selected_option 应明确为 'reject'，不能是 None"
        )
        assert d["data"]["status"] == "denied"

    @pytest.mark.asyncio
    async def test_choice_reject_evaluation_fails(self) -> None:
        """拒绝结果在 ExpectEvaluator 中应判定为不通过"""
        from evaluation.expect import ExpectEvaluator
        from evaluation.types import ExpectCondition, ExpectSpec

        evaluator = ExpectEvaluator()
        expect = ExpectSpec(
            conditions=[
                ExpectCondition(field="success", operator="is_true"),
                ExpectCondition(field="data.selected_option", operator="equals", value="approve"),
            ],
            logic="and",
        )

        # 模拟拒绝场景的工具输出
        output = {
            "success": True,
            "data": {"status": "denied", "selected_option": "reject", "reason": "用户拒绝"},
        }
        result = evaluator.evaluate(
            metric_id="human_review",
            expect=expect,
            output=output,
        )
        assert result.passed is False, "拒绝时应判定为不通过"

    @pytest.mark.asyncio
    async def test_choice_approve_evaluation_passes(self) -> None:
        """通过结果在 ExpectEvaluator 中应判定为通过"""
        from evaluation.expect import ExpectEvaluator
        from evaluation.types import ExpectCondition, ExpectSpec

        evaluator = ExpectEvaluator()
        expect = ExpectSpec(
            conditions=[
                ExpectCondition(field="success", operator="is_true"),
                ExpectCondition(field="data.selected_option", operator="equals", value="approve"),
            ],
            logic="and",
        )

        output = {
            "success": True,
            "data": {
                "status": "completed",
                "selected_option": "approve",
                "response_type": "approved",
            },
        }
        result = evaluator.evaluate(
            metric_id="human_review",
            expect=expect,
            output=output,
        )
        assert result.passed is True, "选择通过时应判定为通过"


# ── Bug 3: Conversation 模式不应自动通过 ───────────────────────

class TestConversationModeEvaluation:
    """验证 Conversation 模式不能自动给通过。"""

    @pytest.mark.asyncio
    async def test_conversation_user_arrived_has_conversation_mode_flag(self) -> None:
        """用户到达对话页面→ 返回数据应有 conversation_mode=True 且 selected_option=None"""
        tool = _make_tool()
        svc = _FakeService(response={
            "response_type": "approved",
            "feedback": "",
        })

        result = await tool._execute_conversation_mode(
            inputs={"mode": "conversation", "title": "对话", "timeout_seconds": 60},
            service=svc,
            pipeline_id="test_pipeline",
        )
        d = _result_to_dict(result)

        assert d["success"] is True
        assert d["data"]["conversation_mode"] is True
        assert d["data"]["selected_option"] is None

    @pytest.mark.asyncio
    async def test_conversation_mode_evaluation_fails(self) -> None:
        """对话模式到达在 ExpectEvaluator 中应判定为不通过"""
        from evaluation.expect import ExpectEvaluator
        from evaluation.types import ExpectCondition, ExpectSpec

        evaluator = ExpectEvaluator()
        # 完整的 human_review.yaml expect 条件（含 conversation_mode 检查）
        expect = ExpectSpec(
            conditions=[
                ExpectCondition(field="success", operator="is_true"),
                ExpectCondition(field="data.selected_option", operator="equals", value="approve"),
                ExpectCondition(field="data.conversation_mode", operator="is_false"),
            ],
            logic="and",
        )

        # 模拟对话模式到达的工具输出
        output = {
            "success": True,
            "data": {
                "status": "user_arrived",
                "conversation_mode": True,
                "selected_option": None,
            },
        }
        result = evaluator.evaluate(
            metric_id="human_review",
            expect=expect,
            output=output,
        )
        assert result.passed is False, "对话模式到达不应判定为通过"

    @pytest.mark.asyncio
    async def test_conversation_mode_denied_explicit_reject(self) -> None:
        """对话模式拒绝→ selected_option 应为 'reject'"""
        tool = _make_tool()
        svc = _FakeService(deny=True)

        result = await tool._execute_conversation_mode(
            inputs={"mode": "conversation", "title": "对话", "timeout_seconds": 60},
            service=svc,
            pipeline_id="test_pipeline",
        )
        d = _result_to_dict(result)

        assert d["data"]["selected_option"] == "reject"
        assert d["data"]["conversation_mode"] is True

    @pytest.mark.asyncio
    async def test_conversation_without_selected_option_still_fails(self) -> None:
        """即使没有 conversation_mode 条件，仅靠 selected_option 也应不通过"""
        from evaluation.expect import ExpectEvaluator
        from evaluation.types import ExpectCondition, ExpectSpec

        evaluator = ExpectEvaluator()
        # 简化条件（不含 conversation_mode）
        expect = ExpectSpec(
            conditions=[
                ExpectCondition(field="success", operator="is_true"),
                ExpectCondition(field="data.selected_option", operator="equals", value="approve"),
            ],
            logic="and",
        )

        # 对话模式到达 → selected_option=None
        output = {
            "success": True,
            "data": {
                "status": "user_arrived",
                "conversation_mode": True,
                "selected_option": None,
            },
        }
        result = evaluator.evaluate(
            metric_id="human_review",
            expect=expect,
            output=output,
        )
        assert result.passed is False, (
            "对话模式 selected_option=None 应导致 data.selected_option == 'approve' 不满足"
        )


# ── Regression: is_approved 不再包含 CONVERSATION_END ───────────

class TestIsApprovedRegression:
    """验证 InteractionResponse.is_approved 不再将 CONVERSATION_END 视为 approved。"""

    def test_conversation_end_is_not_approved(self) -> None:
        from core.human_interaction.models import InteractionResponse, ResponseType

        resp = InteractionResponse.create_conversation_end_response(
            request_id="req_001",
            result="对话结束",
            messages=[],
        )
        assert resp.response_type == ResponseType.CONVERSATION_END
        assert resp.is_approved is False, (
            "CONVERSATION_END 不应被视为 approved"
        )

    def test_approved_is_approved(self) -> None:
        from core.human_interaction.models import InteractionResponse, ResponseType

        resp = InteractionResponse.create_approval_response(
            request_id="req_001",
            approved=True,
        )
        assert resp.response_type == ResponseType.APPROVED
        assert resp.is_approved is True

    def test_denied_is_not_approved(self) -> None:
        from core.human_interaction.models import InteractionResponse, ResponseType

        resp = InteractionResponse.create_approval_response(
            request_id="req_001",
            approved=False,
        )
        assert resp.response_type == ResponseType.DENIED
        assert resp.is_approved is False

    def test_modified_is_approved(self) -> None:
        from core.human_interaction.models import InteractionResponse, ResponseType

        resp = InteractionResponse.create_approval_response(
            request_id="req_001",
            approved=True,
            modified_data={"key": "value"},
        )
        assert resp.response_type == ResponseType.MODIFIED
        assert resp.is_approved is True
