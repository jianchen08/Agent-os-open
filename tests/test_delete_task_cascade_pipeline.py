"""delete_task 级联清理管道 — 回归测试。

锁定 BUG-FIX-delete_task_pipeline_cascade 修复的根因：
原 delete_task 仅删除任务记录，不清理 task.pipeline_run_id 对应的
管道执行文件、不取消运行中管道、容器任务也不级联清理子任务管道。

修复后 delete_task 统一委托 soft_delete_container / hard_delete_task，
完整覆盖：取消运行中管道 + 清理管道执行文件 + 级联清理子任务。
"""
from __future__ import annotations

import tempfile
from unittest.mock import AsyncMock, patch

import pytest

from tasks.service import TaskService


def _make_service() -> TaskService:
    """创建使用临时目录的 TaskService 实例。"""
    tmp_dir = tempfile.mkdtemp(prefix="test_delete_pipeline_")
    return TaskService(data_dir=tmp_dir)


class TestDeleteTaskCascadePipeline:
    """delete_task 级联清理管道的回归测试。"""

    @pytest.mark.asyncio
    async def test_delete_task_cancels_running_pipeline(self) -> None:
        """删除任务时应取消运行中的管道引擎。"""
        svc = _make_service()
        task = await svc.create_task(title="带管道的任务")
        await svc.start_task(task.id)

        with patch.object(
            svc, "_cancel_pipeline_recursive",
        ) as mock_cancel:
            result = await svc.delete_task(task.id)

        assert result is True
        mock_cancel.assert_called_once_with(task.id)

    @pytest.mark.asyncio
    async def test_delete_task_cleans_pipeline_execution_records(self) -> None:
        """非容器任务删除时应清理 task.pipeline_run_id 的管道执行文件。"""
        svc = _make_service()
        task = await svc.create_task(title="带管道的任务")
        await svc.start_task(task.id)
        await svc.bind_pipeline_run(task.id, "pipe-run-001")

        with patch.object(
            svc, "_cleanup_pipeline_file", return_value=True,
        ) as mock_cleanup:
            result = await svc.delete_task(task.id)

        assert result is True
        mock_cleanup.assert_called_once_with("pipe-run-001")
        # 任务记录已硬删除
        assert svc.get_task(task.id) is None

    @pytest.mark.asyncio
    async def test_delete_container_task_cascades_child_pipelines(self) -> None:
        """容器任务软删除时应级联清理子任务的管道文件。"""
        svc = _make_service()
        container = await svc.create_task(
            title="容器", metadata={"task_scope": "container"},
        )
        child = await svc.create_task(
            title="子任务", parent_task_id=container.id,
        )
        await svc.start_task(child.id)
        await svc.bind_pipeline_run(child.id, "child-pipe-001")

        cleaned_pipelines: list[str] = []

        def _track_cleanup(pipeline_run_id: str) -> bool:
            cleaned_pipelines.append(pipeline_run_id)
            return True

        with patch.object(
            svc, "_cleanup_pipeline_file", side_effect=_track_cleanup,
        ):
            result = await svc.delete_task(container.id)

        assert result is True
        # 容器任务被软删除（保留记录）
        fetched = svc.get_task(container.id)
        assert fetched is not None
        assert fetched.metadata.get("soft_deleted") is True
        # 子任务管道文件被级联清理
        assert "child-pipe-001" in cleaned_pipelines
        # 子任务记录被硬删除
        assert svc.get_task(child.id) is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_false(self) -> None:
        """删除不存在的任务返回 False，且不触发任何清理。"""
        svc = _make_service()

        with patch.object(
            svc, "_cleanup_pipeline_file",
        ) as mock_cleanup, patch.object(
            svc, "_cancel_pipeline_recursive",
        ) as mock_cancel:
            result = await svc.delete_task("不存在")

        assert result is False
        mock_cleanup.assert_not_called()
        mock_cancel.assert_not_called()
