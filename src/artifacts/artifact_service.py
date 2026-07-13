"""制品服务。

管理制品（Artifact）的 CRUD 和版本追踪。
纯内存存储，与 HumanInteractionService 保持一致的存储模式。
"""

from __future__ import annotations

import difflib
import logging
from typing import Any

from artifacts.models import Artifact, ArtifactType

logger = logging.getLogger(__name__)

# 全局单例
_artifact_service: ArtifactService | None = None


def get_artifact_service() -> ArtifactService:
    """获取全局制品服务单例。"""
    global _artifact_service  # noqa: PLW0603
    if _artifact_service is None:
        _artifact_service = ArtifactService()
    return _artifact_service


def reset_artifact_service() -> None:
    """重置全局单例（测试用）。"""
    global _artifact_service  # noqa: PLW0603
    _artifact_service = None


class ArtifactService:
    """制品服务（纯内存版）。

    使用内存 dict 存储 Artifact，支持 CRUD 和版本管理。
    """

    def __init__(self) -> None:
        self._artifacts: dict[str, Artifact] = {}
        self._task_artifacts: dict[str, list[str]] = {}

    async def create_artifact(
        self,
        task_id: str,
        title: str,
        artifact_type: str | ArtifactType,
        content: str = "",
        file_path: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Artifact:
        """创建制品。

        Args:
            task_id: 关联任务 ID
            title: 制品标题
            artifact_type: 制品类型
            content: 制品内容
            file_path: 沙盒文件路径（可选）
            metadata: 扩展元数据

        Returns:
            创建的 Artifact 实例
        """
        if isinstance(artifact_type, str):
            artifact_type = ArtifactType(artifact_type)

        artifact = Artifact(
            task_id=task_id,
            title=title,
            artifact_type=artifact_type,
            content=content,
            file_path=file_path,
            metadata=metadata or {},
        )

        self._artifacts[artifact.id] = artifact
        self._task_artifacts.setdefault(task_id, []).append(artifact.id)

        logger.info(
            "[ArtifactService] 创建制品 | id=%s | task_id=%s | type=%s",
            artifact.id,
            task_id,
            artifact_type.value,
        )
        return artifact

    async def get_artifact(self, artifact_id: str) -> Artifact | None:
        """获取单个制品。"""
        return self._artifacts.get(artifact_id)

    async def list_artifacts_by_task(
        self,
        task_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """获取任务下的制品列表。

        Returns:
            {"items": [...], "total": int}
        """
        ids = self._task_artifacts.get(task_id, [])
        total = len(ids)
        items = []
        for aid in ids[offset : offset + limit]:
            artifact = self._artifacts.get(aid)
            if artifact:
                items.append(artifact.to_dict())
        return {"items": items, "total": total}

    async def update_artifact(
        self,
        artifact_id: str,
        content: str | None = None,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Artifact | None:
        """更新制品（创建新版本）。

        通过创建新的 Artifact 并设置 parent_artifact_id 构成版本链。

        Returns:
            新版本的 Artifact，如果原制品不存在则返回 None
        """
        old = self._artifacts.get(artifact_id)
        if not old:
            return None

        new_artifact = Artifact(
            task_id=old.task_id,
            title=title or old.title,
            artifact_type=old.artifact_type,
            content=content if content is not None else old.content,
            file_path=old.file_path,
            version=old.version + 1,
            parent_artifact_id=old.id,
            metadata={**old.metadata, **(metadata or {})},
        )

        self._artifacts[new_artifact.id] = new_artifact
        self._task_artifacts.setdefault(old.task_id, []).append(new_artifact.id)

        logger.info(
            "[ArtifactService] 更新制品 | new_id=%s | old_id=%s | version=%d",
            new_artifact.id,
            artifact_id,
            new_artifact.version,
        )
        return new_artifact

    async def delete_artifact(self, artifact_id: str) -> bool:
        """删除制品。"""
        artifact = self._artifacts.pop(artifact_id, None)
        if not artifact:
            return False

        task_ids = self._task_artifacts.get(artifact.task_id, [])
        if artifact_id in task_ids:
            task_ids.remove(artifact_id)

        logger.info("[ArtifactService] 删除制品 | id=%s", artifact_id)
        return True

    async def get_version_history(
        self,
        artifact_id: str,
    ) -> dict[str, Any]:
        """获取制品版本历史。

        通过 parent_artifact_id 向上追溯版本链。

        Returns:
            {"items": [...], "total": int}
        """
        current = self._artifacts.get(artifact_id)
        if not current:
            return {"items": [], "total": 0}

        versions = [current.to_dict()]

        # 向上追溯版本链
        seen = {artifact_id}
        parent_id = current.parent_artifact_id
        while parent_id and parent_id not in seen:
            seen.add(parent_id)
            parent = self._artifacts.get(parent_id)
            if not parent:
                break
            versions.append(parent.to_dict())
            parent_id = parent.parent_artifact_id

        # 按版本号降序
        versions.sort(key=lambda v: v["version"], reverse=True)
        return {"items": versions, "total": len(versions)}

    async def get_version_diff(
        self,
        artifact_id: str,
        from_version: int,
        to_version: int,
    ) -> dict[str, Any]:
        """获取两个版本之间的差异。

        Args:
            artifact_id: 制品 ID
            from_version: 起始版本号
            to_version: 目标版本号

        Returns:
            {"diff": str, "from_version": int, "to_version": int}
        """
        # 收集所有版本
        history = await self.get_version_history(artifact_id)
        version_map = {v["version"]: v for v in history["items"]}

        from_content = version_map.get(from_version, {}).get("content", "")
        to_content = version_map.get(to_version, {}).get("content", "")

        diff_lines = list(
            difflib.unified_diff(
                from_content.splitlines(keepends=True),
                to_content.splitlines(keepends=True),
                fromfile=f"v{from_version}",
                tofile=f"v{to_version}",
            )
        )

        return {
            "diff": "".join(diff_lines),
            "from_version": from_version,
            "to_version": to_version,
        }
