"""审批服务。

管理审批请求（ReviewRequest）的创建、状态流转、反馈提交和超时处理。
纯内存存储，复用 HumanInteractionService 的 asyncio.Event 等待模式。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from review.models import ReviewFeedback, ReviewRequest, ReviewStatus

logger = logging.getLogger(__name__)

# 全局单例
_review_service: ReviewService | None = None


def get_review_service() -> ReviewService:
    """获取全局审批服务单例。"""
    global _review_service  # noqa: PLW0603
    if _review_service is None:
        _review_service = ReviewService()
    return _review_service


def reset_review_service() -> None:
    """重置全局单例（测试用）。"""
    global _review_service  # noqa: PLW0603
    _review_service = None


class ReviewService:
    """审批服务（纯内存版）。

    使用内存 dict 存储 ReviewRequest 和 ReviewFeedback，
    通过 asyncio.Event 实现审批等待机制。
    """

    def __init__(self, default_timeout: float = 86400.0) -> None:
        self._default_timeout = default_timeout
        self._reviews: dict[str, ReviewRequest] = {}
        self._feedbacks: dict[str, ReviewFeedback] = {}
        self._pending_events: dict[str, asyncio.Event] = {}
        self._timeout_tasks: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    async def create_review(
        self,
        task_id: str,
        thread_id: str,
        session_id: str,
        tab_id: str,
        title: str,
        description: str = "",
        artifact_ids: list[str] | None = None,
        priority: str = "normal",
        timeout_seconds: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ReviewRequest:
        """创建审批请求。

        Args:
            task_id: 关联任务 ID
            thread_id: 关联线程 ID
            session_id: 关联会话 ID
            tab_id: 前端目标 Tab ID
            title: 审批标题
            description: 审批描述
            artifact_ids: 关联制品 ID 列表
            priority: 优先级
            timeout_seconds: 超时时间（秒）
            metadata: 扩展元数据

        Returns:
            创建的 ReviewRequest 实例
        """
        timeout = timeout_seconds or self._default_timeout

        review = ReviewRequest(
            task_id=task_id,
            thread_id=thread_id,
            session_id=session_id,
            tab_id=tab_id,
            title=title,
            description=description,
            artifact_ids=artifact_ids or [],
            priority=priority,
            timeout_seconds=timeout,
            metadata=metadata or {},
        )

        self._reviews[review.id] = review

        async with self._lock:
            self._pending_events[review.id] = asyncio.Event()

        self._setup_timeout(review.id, timeout)

        logger.info(
            "[ReviewService] 创建审批 | id=%s | task_id=%s | title=%s",
            review.id,
            task_id,
            title,
        )
        return review

    async def get_review(self, review_id: str) -> ReviewRequest | None:
        """获取审批详情。"""
        return self._reviews.get(review_id)

    async def list_reviews_by_task(
        self,
        task_id: str,
        limit: int = 50,
    ) -> dict[str, Any]:
        """获取任务的审批列表。"""
        items = []
        for review in self._reviews.values():
            if review.task_id == task_id:
                items.append(review.to_dict())
            if len(items) >= limit:
                break
        return {"items": items, "total": len(items)}

    async def submit_feedback(
        self,
        review_id: str,
        response_type: str,
        overall_comment: str = "",
        annotations: list[dict[str, Any]] | None = None,
        user_id: str | None = None,
    ) -> ReviewFeedback | None:
        """提交审批反馈。

        Args:
            review_id: 审批请求 ID
            response_type: 响应类型（approved/denied/partially_approved）
            overall_comment: 整体评论
            annotations: 批注列表
            user_id: 用户标识

        Returns:
            ReviewFeedback 实例，审批不存在或状态不允许时返回 None
        """
        review = self._reviews.get(review_id)
        if not review:
            return None

        if review.status not in (ReviewStatus.PENDING, ReviewStatus.IN_REVIEW):
            logger.warning(
                "[ReviewService] 审批状态不允许反馈 | id=%s | status=%s",
                review_id,
                review.status.value,
            )
            return None

        feedback = ReviewFeedback(
            review_request_id=review_id,
            response_type=response_type,
            overall_comment=overall_comment,
            annotations=annotations or [],
            user_id=user_id,
        )

        self._feedbacks[review_id] = feedback

        # 更新审批状态
        from datetime import UTC, datetime  # noqa: PLC0415

        now = datetime.now(UTC).isoformat()

        if response_type == "approved":
            review.status = ReviewStatus.APPROVED
        elif response_type == "denied":
            review.status = ReviewStatus.REJECTED
        elif response_type == "partially_approved":
            review.status = ReviewStatus.PARTIALLY_APPROVED
        else:
            review.status = ReviewStatus.APPROVED

        review.updated_at = now
        review.completed_at = now

        # 触发等待事件
        async with self._lock:
            if review_id in self._pending_events:
                self._pending_events[review_id].set()
            if review_id in self._timeout_tasks:
                self._timeout_tasks[review_id].cancel()
                del self._timeout_tasks[review_id]

        logger.info(
            "[ReviewService] 提交反馈 | review_id=%s | type=%s",
            review_id,
            response_type,
        )
        return feedback

    async def get_feedback(self, review_id: str) -> ReviewFeedback | None:
        """获取审批反馈。"""
        return self._feedbacks.get(review_id)

    async def mark_as_viewed(self, review_id: str) -> bool:
        """标记审批为已查看（状态变为 in_review）。"""
        review = self._reviews.get(review_id)
        if not review or review.status != ReviewStatus.PENDING:
            return False

        from datetime import UTC, datetime  # noqa: PLC0415

        review.status = ReviewStatus.IN_REVIEW
        review.reviewed_at = datetime.now(UTC).isoformat()
        review.updated_at = datetime.now(UTC).isoformat()

        logger.info("[ReviewService] 标记已查看 | id=%s", review_id)
        return True

    async def cancel_review(
        self,
        review_id: str,
        reason: str | None = None,
    ) -> bool:
        """取消审批。"""
        review = self._reviews.get(review_id)
        if not review:
            return False

        if review.status in (ReviewStatus.APPROVED, ReviewStatus.REJECTED, ReviewStatus.TIMEOUT):
            return False

        from datetime import UTC, datetime  # noqa: PLC0415

        review.status = ReviewStatus.CANCELLED
        review.updated_at = datetime.now(UTC).isoformat()
        review.completed_at = datetime.now(UTC).isoformat()
        if reason:
            review.metadata["cancel_reason"] = reason

        async with self._lock:
            if review_id in self._pending_events:
                self._pending_events[review_id].set()
            if review_id in self._timeout_tasks:
                self._timeout_tasks[review_id].cancel()
                del self._timeout_tasks[review_id]

        logger.info("[ReviewService] 取消审批 | id=%s | reason=%s", review_id, reason)
        return True

    async def wait_for_review(
        self,
        review_id: str,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """等待审批结果。

        阻塞直到用户提交反馈或超时。

        Returns:
            包含反馈信息的字典
        """
        event = self._pending_events.get(review_id)
        if not event:
            review = self._reviews.get(review_id)
            if not review:
                raise ValueError(f"审批请求不存在: {review_id}")
            async with self._lock:
                self._pending_events[review_id] = asyncio.Event()
                event = self._pending_events[review_id]

        review = self._reviews.get(review_id)
        if not review:
            raise ValueError(f"审批请求不存在: {review_id}")

        effective_timeout = timeout or review.timeout_seconds or self._default_timeout

        try:
            await asyncio.wait_for(event.wait(), timeout=effective_timeout)
        except TimeoutError:
            await self._handle_timeout(review_id)
            return {
                "status": "timeout",
                "review_id": review_id,
                "message": f"审批在 {effective_timeout} 秒后超时",
            }

        feedback = self._feedbacks.get(review_id)
        if not feedback:
            return {
                "status": "timeout",
                "review_id": review_id,
                "message": "未收到反馈",
            }

        return {
            "status": "completed",
            "review_id": review_id,
            "response_type": feedback.response_type,
            "overall_comment": feedback.overall_comment,
            "annotations": feedback.annotations,
            "user_id": feedback.user_id,
        }

    def _setup_timeout(self, review_id: str, timeout_seconds: float) -> None:
        """设置超时任务。"""

        async def timeout_handler() -> None:
            try:
                await asyncio.sleep(timeout_seconds)
                review = self._reviews.get(review_id)
                if review and review.status in (ReviewStatus.PENDING, ReviewStatus.IN_REVIEW):
                    await self._handle_timeout(review_id)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error("[ReviewService] 超时处理失败 | error=%s", e)

        task = asyncio.create_task(timeout_handler())
        self._timeout_tasks[review_id] = task

    async def _handle_timeout(self, review_id: str) -> None:
        """处理审批超时。"""
        review = self._reviews.get(review_id)
        if review:
            from datetime import UTC, datetime  # noqa: PLC0415

            review.status = ReviewStatus.TIMEOUT
            review.updated_at = datetime.now(UTC).isoformat()
            review.completed_at = datetime.now(UTC).isoformat()

        async with self._lock:
            if review_id in self._pending_events:
                self._pending_events[review_id].set()

        logger.info("[ReviewService] 审批超时 | id=%s", review_id)
