"""批注服务。

管理制品批注（Annotation）的创建、更新、删除和状态管理。
纯内存存储。
"""

from __future__ import annotations

import logging
from typing import Any

from artifacts.models import Annotation, AnnotationStatus, AnnotationTarget

logger = logging.getLogger(__name__)

# 全局单例
_annotation_service: AnnotationService | None = None


def get_annotation_service() -> AnnotationService:
    """获取全局批注服务单例。"""
    global _annotation_service  # noqa: PLW0603
    if _annotation_service is None:
        _annotation_service = AnnotationService()
    return _annotation_service


def reset_annotation_service() -> None:
    """重置全局单例（测试用）。"""
    global _annotation_service  # noqa: PLW0603
    _annotation_service = None


class AnnotationService:
    """批注服务（纯内存版）。

    使用内存 dict 存储 Annotation，支持 CRUD 和状态管理。
    """

    def __init__(self) -> None:
        self._annotations: dict[str, Annotation] = {}
        self._artifact_annotations: dict[str, list[str]] = {}

    async def create_annotation(
        self,
        artifact_id: str,
        target_type: str | AnnotationTarget,
        target_data: dict[str, Any],
        content: str,
        author_type: str = "user",
        author_id: str = "",
    ) -> Annotation:
        """创建批注。

        Args:
            artifact_id: 关联制品 ID
            target_type: 批注目标类型
            target_data: 批注位置数据
            content: 批注文本内容
            author_type: 作者类型（"user" / "agent"）
            author_id: 作者标识

        Returns:
            创建的 Annotation 实例
        """
        if isinstance(target_type, str):
            target_type = AnnotationTarget(target_type)

        annotation = Annotation(
            artifact_id=artifact_id,
            target_type=target_type,
            target_data=target_data,
            content=content,
            author_type=author_type,
            author_id=author_id,
        )

        self._annotations[annotation.id] = annotation
        self._artifact_annotations.setdefault(artifact_id, []).append(annotation.id)

        logger.info(
            "[AnnotationService] 创建批注 | id=%s | artifact_id=%s | type=%s",
            annotation.id,
            artifact_id,
            target_type.value,
        )
        return annotation

    async def get_annotation(self, annotation_id: str) -> Annotation | None:
        """获取单个批注。"""
        return self._annotations.get(annotation_id)

    async def list_annotations_by_artifact(
        self,
        artifact_id: str,
        status: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """获取制品的批注列表。

        Args:
            artifact_id: 制品 ID
            status: 按状态过滤（可选）
            limit: 返回数量上限

        Returns:
            {"items": [...], "total": int}
        """
        ids = self._artifact_annotations.get(artifact_id, [])
        items = []
        for aid in ids:
            annotation = self._annotations.get(aid)
            if annotation:
                if status and annotation.status.value != status:
                    continue
                items.append(annotation.to_dict())
                if len(items) >= limit:
                    break
        return {"items": items, "total": len(items)}

    async def update_annotation(
        self,
        annotation_id: str,
        content: str | None = None,
        target_data: dict[str, Any] | None = None,
    ) -> Annotation | None:
        """更新批注。"""
        annotation = self._annotations.get(annotation_id)
        if not annotation:
            return None

        if content is not None:
            annotation.content = content
        if target_data is not None:
            annotation.target_data = target_data

        logger.info("[AnnotationService] 更新批注 | id=%s", annotation_id)
        return annotation

    async def delete_annotation(self, annotation_id: str) -> bool:
        """删除批注。"""
        annotation = self._annotations.pop(annotation_id, None)
        if not annotation:
            return False

        artifact_ids = self._artifact_annotations.get(annotation.artifact_id, [])
        if annotation_id in artifact_ids:
            artifact_ids.remove(annotation_id)

        logger.info("[AnnotationService] 删除批注 | id=%s", annotation_id)
        return True

    async def resolve_annotation(self, annotation_id: str) -> Annotation | None:
        """标记批注为已解决。"""
        annotation = self._annotations.get(annotation_id)
        if not annotation:
            return None

        annotation.status = AnnotationStatus.RESOLVED
        from datetime import UTC, datetime  # noqa: PLC0415

        annotation.resolved_at = datetime.now(UTC).isoformat()

        logger.info("[AnnotationService] 标记批注已解决 | id=%s", annotation_id)
        return annotation
