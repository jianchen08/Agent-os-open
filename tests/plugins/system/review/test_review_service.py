# @feature: FP-0.2.review P1-2 审批状态机 | @vision: V1 可进化 | @ci: python-coverage
"""审批状态机单元测试（P1-2 sidecar 化承接，src/review 0.1 语义回归）。

覆盖 ReviewService 全状态流转：
1. 创建（默认值/全字段/自定义超时）
2. 查询（详情/按任务列表/limit 截断/空列表）
3. mark_as_viewed（pending→in_review 一次生效，重复/终态拒绝）
4. submit_feedback（approved/denied/partially_approved/未知类型兜底 approved；
   不存在/状态不允许 → None）
5. cancel_review（pending 可取消+reason；终态/不存在拒绝）
6. wait_for_review（反馈完成路径 / 显式超时路径 / 未知 id 抛 ValueError）
7. 自动超时任务（_setup_timeout 到期置 TIMEOUT；反馈后任务取消）

纯内存实现、零外部依赖（asyncio 直驱），与 0.1 语义同构。

[来源: docs/working/channel_api插件拆迁方案_20260821.md 批次 5（review P1-2）]
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from models import ReviewStatus
from review_service import ReviewService, get_review_service, reset_review_service

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_service() -> None:
    """每个测试前重置全局单例（状态机测试全部用独立 ReviewService 实例）。"""
    reset_review_service()


def _run(coro: Any) -> Any:
    """在独立事件循环中执行协程。

    收尾时取消该 loop 上遗留的未完成任务（create_review 的 _setup_timeout
    后台任务默认睡 86400s——不清理会在 loop 关闭后产生
    "Task was destroyed but it is pending!" 销毁警告）。
    """
    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(coro)

        async def _cleanup() -> None:
            current = asyncio.current_task()
            pending = [t for t in asyncio.all_tasks(loop) if not t.done() and t is not current]
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.sleep(0)

        loop.run_until_complete(_cleanup())
        return result
    finally:
        loop.close()


def _create_basic(service: ReviewService, **overrides: Any) -> Any:
    """默认参数创建审批（同步便利）。"""
    params = {
        "task_id": "task-1",
        "thread_id": "thread-1",
        "session_id": "session-1",
        "tab_id": "tab-1",
        "title": "审批标题",
    }
    params.update(overrides)
    return _run(service.create_review(**params))


class TestCreateReview:
    def test_create_defaults(self) -> None:
        service = ReviewService()
        review = _create_basic(service)

        assert review.id
        assert review.status == ReviewStatus.PENDING
        assert review.task_id == "task-1"
        assert review.title == "审批标题"
        assert review.artifact_ids == []
        assert review.priority == "normal"
        assert review.timeout_seconds == 86400.0
        assert review.metadata == {}
        assert review.reviewed_at is None
        assert review.completed_at is None
        assert review.created_at
        assert review.updated_at

    def test_create_full_fields(self) -> None:
        service = ReviewService()
        review = _create_basic(
            service,
            description="描述",
            artifact_ids=["a1", "a2"],
            priority="high",
            timeout_seconds=120.0,
            metadata={"k": "v"},
        )

        assert review.description == "描述"
        assert review.artifact_ids == ["a1", "a2"]
        assert review.priority == "high"
        assert review.timeout_seconds == 120.0
        assert review.metadata == {"k": "v"}

    def test_create_custom_default_timeout(self) -> None:
        service = ReviewService(default_timeout=60.0)
        review = _create_basic(service)
        assert review.timeout_seconds == 60.0

    def test_create_ids_unique(self) -> None:
        service = ReviewService()
        r1 = _create_basic(service)
        r2 = _create_basic(service)
        assert r1.id != r2.id

    def test_to_dict_roundtrip(self) -> None:
        service = ReviewService()
        review = _create_basic(service, metadata={"m": 1})
        d = review.to_dict()
        assert d["id"] == review.id
        assert d["status"] == "pending"
        assert d["metadata"] == {"m": 1}
        assert "reviewed_at" not in d
        assert "completed_at" not in d


class TestQuery:
    def test_get_review_found(self) -> None:
        service = ReviewService()
        review = _create_basic(service)
        got = _run(service.get_review(review.id))
        assert got is not None
        assert got.id == review.id

    def test_get_review_missing(self) -> None:
        service = ReviewService()
        assert _run(service.get_review("nope")) is None

    def test_list_by_task(self) -> None:
        service = ReviewService()
        _create_basic(service, task_id="t1", title="a")
        _create_basic(service, task_id="t1", title="b")
        _create_basic(service, task_id="t2", title="c")

        result = _run(service.list_reviews_by_task("t1"))
        assert result["total"] == 2
        assert {item["title"] for item in result["items"]} == {"a", "b"}

    def test_list_limit(self) -> None:
        service = ReviewService()
        for i in range(5):
            _create_basic(service, task_id="t1", title=f"r{i}")
        result = _run(service.list_reviews_by_task("t1", limit=2))
        assert result["total"] == 2
        assert len(result["items"]) == 2

    def test_list_unknown_task_empty(self) -> None:
        service = ReviewService()
        _create_basic(service, task_id="t1")
        result = _run(service.list_reviews_by_task("t-unknown"))
        assert result == {"items": [], "total": 0}


class TestMarkAsViewed:
    def test_pending_to_in_review(self) -> None:
        service = ReviewService()
        review = _create_basic(service)
        assert _run(service.mark_as_viewed(review.id)) is True

        updated = _run(service.get_review(review.id))
        assert updated is not None
        assert updated.status == ReviewStatus.IN_REVIEW
        assert updated.reviewed_at is not None

    def test_repeat_called_false(self) -> None:
        service = ReviewService()
        review = _create_basic(service)
        _run(service.mark_as_viewed(review.id))
        assert _run(service.mark_as_viewed(review.id)) is False

    def test_missing_false(self) -> None:
        service = ReviewService()
        assert _run(service.mark_as_viewed("nope")) is False


class TestSubmitFeedback:
    def test_approved(self) -> None:
        service = ReviewService()
        review = _create_basic(service)
        feedback = _run(service.submit_feedback(review.id, "approved", overall_comment="ok"))

        assert feedback is not None
        assert feedback.response_type == "approved"
        assert feedback.overall_comment == "ok"
        assert _run(service.get_feedback(review.id)) is feedback

        updated = _run(service.get_review(review.id))
        assert updated is not None
        assert updated.status == ReviewStatus.APPROVED
        assert updated.completed_at is not None

    def test_denied(self) -> None:
        service = ReviewService()
        review = _create_basic(service)
        _run(service.submit_feedback(review.id, "denied"))
        updated = _run(service.get_review(review.id))
        assert updated is not None
        assert updated.status == ReviewStatus.REJECTED

    def test_partially_approved(self) -> None:
        service = ReviewService()
        review = _create_basic(service)
        _run(service.submit_feedback(review.id, "partially_approved"))
        updated = _run(service.get_review(review.id))
        assert updated is not None
        assert updated.status == ReviewStatus.PARTIALLY_APPROVED

    def test_unknown_response_type_falls_back_approved(self) -> None:
        service = ReviewService()
        review = _create_basic(service)
        _run(service.submit_feedback(review.id, "weird_type"))
        updated = _run(service.get_review(review.id))
        assert updated is not None
        assert updated.status == ReviewStatus.APPROVED

    def test_annotations_and_user(self) -> None:
        service = ReviewService()
        review = _create_basic(service)
        annotations = [{"artifact_id": "a1", "content": "x"}]
        feedback = _run(service.submit_feedback(review.id, "approved", annotations=annotations, user_id="u1"))
        assert feedback is not None
        assert feedback.annotations == annotations
        assert feedback.user_id == "u1"
        assert feedback.to_dict()["user_id"] == "u1"

    def test_after_viewed_allowed(self) -> None:
        service = ReviewService()
        review = _create_basic(service)
        _run(service.mark_as_viewed(review.id))
        feedback = _run(service.submit_feedback(review.id, "denied"))
        assert feedback is not None

    def test_missing_review_none(self) -> None:
        service = ReviewService()
        assert _run(service.submit_feedback("nope", "approved")) is None

    def test_terminal_status_rejected(self) -> None:
        service = ReviewService()
        review = _create_basic(service)
        _run(service.submit_feedback(review.id, "approved"))
        # 已终态（APPROVED）：再反馈被拒，不覆盖
        assert _run(service.submit_feedback(review.id, "denied")) is None
        updated = _run(service.get_review(review.id))
        assert updated is not None
        assert updated.status == ReviewStatus.APPROVED

    def test_after_cancel_rejected(self) -> None:
        service = ReviewService()
        review = _create_basic(service)
        _run(service.cancel_review(review.id))
        assert _run(service.submit_feedback(review.id, "approved")) is None


class TestCancel:
    def test_cancel_pending_with_reason(self) -> None:
        service = ReviewService()
        review = _create_basic(service)
        assert _run(service.cancel_review(review.id, reason="不需要了")) is True

        updated = _run(service.get_review(review.id))
        assert updated is not None
        assert updated.status == ReviewStatus.CANCELLED
        assert updated.metadata["cancel_reason"] == "不需要了"
        assert updated.completed_at is not None

    def test_cancel_missing_false(self) -> None:
        service = ReviewService()
        assert _run(service.cancel_review("nope")) is False

    def test_cancel_approved_false(self) -> None:
        service = ReviewService()
        review = _create_basic(service)
        _run(service.submit_feedback(review.id, "approved"))
        assert _run(service.cancel_review(review.id)) is False

    def test_cancel_rejected_false(self) -> None:
        service = ReviewService()
        review = _create_basic(service)
        _run(service.submit_feedback(review.id, "denied"))
        assert _run(service.cancel_review(review.id)) is False


class TestWaitForReview:
    def test_wait_completed_after_feedback(self) -> None:
        service = ReviewService()
        review = _create_basic(service)

        async def scenario() -> dict[str, Any]:
            waiter = asyncio.create_task(service.wait_for_review(review.id))
            await asyncio.sleep(0.01)
            await service.submit_feedback(review.id, "approved", overall_comment="done")
            return await waiter

        result = _run(scenario())
        assert result["status"] == "completed"
        assert result["review_id"] == review.id
        assert result["response_type"] == "approved"
        assert result["overall_comment"] == "done"

    def test_wait_timeout(self) -> None:
        service = ReviewService()
        review = _create_basic(service, timeout_seconds=1.0)

        result = _run(service.wait_for_review(review.id, timeout=0.05))
        assert result["status"] == "timeout"
        assert "超时" in result["message"]

        updated = _run(service.get_review(review.id))
        assert updated is not None
        assert updated.status == ReviewStatus.TIMEOUT

    def test_wait_missing_id_raises(self) -> None:
        service = ReviewService()
        with pytest.raises(ValueError, match="审批请求不存在"):
            _run(service.wait_for_review("nope", timeout=0.01))

    def test_wait_after_cancel_completed_semantics(self) -> None:
        """取消后事件已 set：wait 返回非 timeout（无反馈 → '未收到反馈' 分支）。"""
        service = ReviewService()
        review = _create_basic(service)
        _run(service.cancel_review(review.id))
        result = _run(service.wait_for_review(review.id, timeout=0.1))
        assert result["status"] == "timeout"
        assert result["message"] == "未收到反馈"


class TestAutoTimeout:
    def test_timeout_task_marks_review(self) -> None:
        """超时任务真实到期置 TIMEOUT。

        创建与等待必须同一事件循环（_run 的收尾清理会取消遗留超时任务，
        故本用例显式管理 loop：等待后再在 loop 内取消遗留任务）。
        """
        service = ReviewService()
        loop = asyncio.new_event_loop()
        try:
            review = loop.run_until_complete(
                service.create_review(
                    task_id="t",
                    thread_id="",
                    session_id="",
                    tab_id="",
                    title="x",
                    timeout_seconds=0.05,
                )
            )
            loop.run_until_complete(asyncio.sleep(0.2))
            updated = loop.run_until_complete(service.get_review(review.id))
        finally:

            async def _cancel() -> None:
                for task in list(service._timeout_tasks.values()):
                    if not task.done():
                        task.cancel()
                await asyncio.sleep(0)

            loop.run_until_complete(_cancel())
            loop.close()

        assert updated is not None
        assert updated.status == ReviewStatus.TIMEOUT
        assert updated.completed_at is not None

    def test_feedback_cancels_timeout_task(self) -> None:
        service = ReviewService()
        review = _create_basic(service, timeout_seconds=0.05)
        assert review.id in service._timeout_tasks

        _run(service.submit_feedback(review.id, "approved"))
        assert review.id not in service._timeout_tasks

        async def scenario() -> None:
            await asyncio.sleep(0.2)

        _run(scenario())
        updated = _run(service.get_review(review.id))
        assert updated is not None
        assert updated.status == ReviewStatus.APPROVED  # 未被超时任务改写


class TestModuleSingleton:
    def test_get_review_service_returns_singleton(self) -> None:
        reset_review_service()
        assert get_review_service() is get_review_service()

    def test_reset_review_service(self) -> None:
        first = get_review_service()
        reset_review_service()
        assert get_review_service() is not first
