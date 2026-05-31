"""自动确认通知器与 human_review 评估流程集成测试。

覆盖场景：
- AutoConfirmNotifier 在 choice 模式下返回正确的选项 ID（修复 approved/approve 不匹配）
- AutoConfirmNotifier 在 conversation 模式下正常工作
- ExpectEvaluator 对 human_review 指标的判定逻辑
- 端到端：AutoConfirm → submit_response → ExpectEvaluator → 通过/失败
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from auto_confirm_runner import AutoConfirmNotifier
from evaluation.expect import ExpectEvaluator
from evaluation.types import ExpectCondition, ExpectSpec


# ── AutoConfirmNotifier 单元测试 ─────────────────────────


class TestAutoConfirmNotifierChoiceMode:
    """AutoConfirmNotifier choice 模式测试。"""

    @pytest.mark.asyncio
    async def test_choice_mode_returns_first_option_id(self):
        """choice 模式自动确认返回第一个选项的 ID。"""
        notifier = AutoConfirmNotifier(confirm_delay=0)
        mock_service = AsyncMock()
        notifier.set_service(mock_service)

        request = {
            "id": "req-001",
            "message_data": {
                "title": "方案确认",
                "interaction_mode": "choice",
                "options": [
                    {"id": "approve", "label": "通过"},
                    {"id": "reject", "label": "不通过"},
                ],
            },
        }

        await notifier.notify_request(request)

        mock_service.submit_response.assert_called_once_with(
            request_id="req-001",
            response_type="approved",
            selected_option="approve",
            feedback="确认通过，请继续执行",
        )

    @pytest.mark.asyncio
    async def test_choice_mode_with_custom_option_ids(self):
        """choice 模式使用自定义选项 ID 时也能正确提取。"""
        notifier = AutoConfirmNotifier(confirm_delay=0)
        mock_service = AsyncMock()
        notifier.set_service(mock_service)

        request = {
            "id": "req-002",
            "message_data": {
                "title": "审批",
                "interaction_mode": "choice",
                "options": [
                    {"id": "confirm", "label": "确认"},
                    {"id": "deny", "label": "拒绝"},
                ],
            },
        }

        await notifier.notify_request(request)

        mock_service.submit_response.assert_called_once_with(
            request_id="req-002",
            response_type="approved",
            selected_option="confirm",
            feedback="确认通过，请继续执行",
        )

    @pytest.mark.asyncio
    async def test_choice_mode_no_options_falls_back_to_approve(self):
        """choice 模式无 options 时回退到默认值 'approve'。"""
        notifier = AutoConfirmNotifier(confirm_delay=0)
        mock_service = AsyncMock()
        notifier.set_service(mock_service)

        request = {
            "id": "req-003",
            "message_data": {
                "title": "确认",
                "interaction_mode": "choice",
            },
        }

        await notifier.notify_request(request)

        mock_service.submit_response.assert_called_once_with(
            request_id="req-003",
            response_type="approved",
            selected_option="approve",
            feedback="确认通过，请继续执行",
        )

    @pytest.mark.asyncio
    async def test_choice_mode_empty_options_falls_back_to_approve(self):
        """choice 模式 options 为空列表时回退到 'approve'。"""
        notifier = AutoConfirmNotifier(confirm_delay=0)
        mock_service = AsyncMock()
        notifier.set_service(mock_service)

        request = {
            "id": "req-004",
            "message_data": {
                "title": "确认",
                "interaction_mode": "choice",
                "options": [],
            },
        }

        await notifier.notify_request(request)

        mock_service.submit_response.assert_called_once_with(
            request_id="req-004",
            response_type="approved",
            selected_option="approve",
            feedback="确认通过，请继续执行",
        )

    @pytest.mark.asyncio
    async def test_confirmed_count_increments(self):
        """每次确认后计数器递增。"""
        notifier = AutoConfirmNotifier(confirm_delay=0)
        mock_service = AsyncMock()
        notifier.set_service(mock_service)

        for i in range(3):
            request = {
                "id": f"req-{i}",
                "message_data": {
                    "title": "确认",
                    "interaction_mode": "choice",
                    "options": [{"id": "approve", "label": "通过"}],
                },
            }
            await notifier.notify_request(request)

        assert notifier._confirmed_count == 3

    @pytest.mark.asyncio
    async def test_no_service_does_nothing(self):
        """无 service 时不调用 submit_response。"""
        notifier = AutoConfirmNotifier(confirm_delay=0)

        request = {
            "id": "req-005",
            "message_data": {
                "title": "确认",
                "interaction_mode": "choice",
                "options": [{"id": "approve", "label": "通过"}],
            },
        }

        result = await notifier.notify_request(request)
        assert result is True
        assert notifier._confirmed_count == 0


class TestAutoConfirmNotifierConversationMode:
    """AutoConfirmNotifier conversation 模式测试。"""

    @pytest.mark.asyncio
    async def test_conversation_mode_submits_answer(self):
        """conversation 模式提交确认回答。"""
        notifier = AutoConfirmNotifier(confirm_delay=0)
        mock_service = AsyncMock()
        notifier.set_service(mock_service)

        request = {
            "id": "req-006",
            "message_data": {
                "title": "方案讨论",
                "interaction_mode": "conversation",
            },
        }

        await notifier.notify_request(request)

        mock_service.submit_response.assert_called_once_with(
            request_id="req-006",
            response_type="answered",
            answers=["确认，请按照你的方案继续执行，不需要进一步澄清。"],
            feedback="确认通过，请继续执行",
        )


# ── ExpectEvaluator 对 human_review 的判定测试 ──────────────


class TestHumanReviewExpectEvaluation:
    """human_review 评估条件判定测试。"""

    def _make_human_review_expect(self) -> ExpectSpec:
        """构建与 human_review.yaml 一致的期望条件。"""
        return ExpectSpec(
            conditions=[
                ExpectCondition(field="success", operator="is_true"),
                ExpectCondition(
                    field="data.selected_option",
                    operator="in",
                    value=["approve", "通过"],
                ),
            ],
            logic="and",
            pass_message="审核通过",
            fail_message="审核未通过",
        )

    def test_approve_option_passes(self):
        """selected_option='approve' 时评估通过。"""
        evaluator = ExpectEvaluator()
        expect = self._make_human_review_expect()

        result = evaluator.evaluate(
            metric_id="human_review",
            expect=expect,
            output={
                "success": True,
                "data": {
                    "status": "completed",
                    "response_type": "approved",
                    "selected_option": "approve",
                    "feedback": "确认通过",
                },
            },
        )

        assert result.passed is True
        assert result.message == "审核通过"

    def test_label_option_passes(self):
        """selected_option='通过'（前端传 label）时评估通过。"""
        evaluator = ExpectEvaluator()
        expect = self._make_human_review_expect()

        result = evaluator.evaluate(
            metric_id="human_review",
            expect=expect,
            output={
                "success": True,
                "data": {
                    "status": "completed",
                    "response_type": "answered",
                    "selected_option": "通过",
                    "feedback": "用户确认通过",
                },
            },
        )

        assert result.passed is True

    def test_approved_option_fails(self):
        """selected_option='approved'（旧 Bug 值）时评估失败。"""
        evaluator = ExpectEvaluator()
        expect = self._make_human_review_expect()

        result = evaluator.evaluate(
            metric_id="human_review",
            expect=expect,
            output={
                "success": True,
                "data": {
                    "status": "completed",
                    "response_type": "approved",
                    "selected_option": "approved",
                    "feedback": "自动确认（前端未响应）",
                },
            },
        )

        assert result.passed is False
        assert result.message == "审核未通过"

    def test_reject_option_fails(self):
        """selected_option='reject' 时评估失败。"""
        evaluator = ExpectEvaluator()
        expect = self._make_human_review_expect()

        result = evaluator.evaluate(
            metric_id="human_review",
            expect=expect,
            output={
                "success": True,
                "data": {
                    "status": "completed",
                    "response_type": "approved",
                    "selected_option": "reject",
                    "feedback": "不通过",
                },
            },
        )

        assert result.passed is False
        assert result.message == "审核未通过"

    def test_missing_selected_option_fails(self):
        """selected_option 不存在时评估失败。"""
        evaluator = ExpectEvaluator()
        expect = self._make_human_review_expect()

        result = evaluator.evaluate(
            metric_id="human_review",
            expect=expect,
            output={
                "success": True,
                "data": {
                    "status": "completed",
                    "response_type": "approved",
                },
            },
        )

        assert result.passed is False

    def test_success_false_and_approve_fails(self):
        """success=False 即使 selected_option 正确也失败（and 逻辑）。"""
        evaluator = ExpectEvaluator()
        expect = self._make_human_review_expect()

        result = evaluator.evaluate(
            metric_id="human_review",
            expect=expect,
            output={
                "success": False,
                "data": {
                    "selected_option": "approve",
                },
            },
        )

        assert result.passed is False


# ── 端到端集成测试 ─────────────────────────────────────


class TestAutoConfirmToEndEvaluation:
    """端到端测试：AutoConfirm → ExpectEvaluator 完整链路。"""

    def _make_human_review_expect(self) -> ExpectSpec:
        """构建与 human_review.yaml 一致的期望条件。"""
        return ExpectSpec(
            conditions=[
                ExpectCondition(field="success", operator="is_true"),
                ExpectCondition(
                    field="data.selected_option",
                    operator="equals",
                    value="approve",
                ),
            ],
            logic="and",
            pass_message="审核通过",
            fail_message="审核未通过",
        )

    def _simulate_auto_confirm_flow(self, request: dict) -> dict:
        """模拟自动确认流程，返回评估引擎收到的 output。"""
        msg_data = request.get("message_data", {})
        options = msg_data.get("options", [])
        first_option_id = options[0]["id"] if options else "approve"

        return {
            "success": True,
            "data": {
                "status": "completed",
                "response_type": "approved",
                "selected_option": first_option_id,
                "feedback": "自动确认（前端未响应）",
            },
        }

    def test_auto_confirm_produces_correct_option_for_evaluation(self):
        """自动确认流程产出的 selected_option 能通过评估。"""
        request = {
            "id": "req-e2e-001",
            "message_data": {
                "title": "架构深度重构与功能对齐方案确认",
                "interaction_mode": "choice",
                "options": [
                    {"id": "approve", "label": "通过"},
                    {"id": "reject", "label": "不通过"},
                ],
            },
        }

        output = self._simulate_auto_confirm_flow(request)

        evaluator = ExpectEvaluator()
        expect = self._make_human_review_expect()
        result = evaluator.evaluate(
            metric_id="human_review",
            expect=expect,
            output=output,
        )

        assert result.passed is True
        assert result.message == "审核通过"

    def test_user_select_reject_produces_failed_evaluation(self):
        """用户选择 reject 时评估不通过。"""
        output = {
            "success": True,
            "data": {
                "status": "completed",
                "response_type": "approved",
                "selected_option": "reject",
                "feedback": "用户选择不通过",
            },
        }

        evaluator = ExpectEvaluator()
        expect = self._make_human_review_expect()
        result = evaluator.evaluate(
            metric_id="human_review",
            expect=expect,
            output=output,
        )

        assert result.passed is False
        assert result.message == "审核未通过"

    def test_regression_old_bug_approved_value_fails(self):
        """回归测试：旧 Bug 值 'approved' 不应通过评估。"""
        output = {
            "success": True,
            "data": {
                "status": "completed",
                "response_type": "approved",
                "selected_option": "approved",
                "feedback": "自动确认（前端未响应）",
            },
        }

        evaluator = ExpectEvaluator()
        expect = self._make_human_review_expect()
        result = evaluator.evaluate(
            metric_id="human_review",
            expect=expect,
            output=output,
        )

        assert result.passed is False
        assert "approve" in result.details["failed_conditions"][0]
