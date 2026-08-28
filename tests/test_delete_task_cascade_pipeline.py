"""delete_task 级联清理管道 — 契约测试。

契约：delete_task 统一委托 soft_delete_container / hard_delete_task，
完整覆盖：取消运行中管道 + 清理管道执行文件 + 级联清理子任务。
"""
from __future__ import annotations

import tempfile
from unittest.mock import AsyncMock, patch

import sys
from pathlib import Path

import pytest

# 0.2：tasks 插件位于 plugins/shared/system/tasks/，from tasks.service 需 system/ 父目录；
# service.py 内部平铺导入 _task_* 兄弟模块，需 tasks 目录自身。
_SYSTEM_DIR = Path(__file__).resolve().parent.parent / "plugins" / "shared" / "system"
_TASKS_DIR = _SYSTEM_DIR / "tasks"
for _d in (_SYSTEM_DIR, _TASKS_DIR):
    _s = str(_d)
    if _s not in sys.path:
        sys.path.insert(0, _s)

from tasks.service import TaskService


@pytest.fixture(autouse=True)
def _tasks_path_guard():
    """每个测试执行前锁定 tasks 插件目录为 sys.path[0]，结束后恢复。

    同 suites/core 的守卫：其他插件测试的 autouse fixture（如 multimodal）
    会把各自目录推到 sys.path[0] 且不恢复，tasks 测试运行期懒加载
    `from storage import TaskStorage` 依赖 sys.path 首位是本插件目录。
    用例结束后恢复原路径序——tasks 目录驻留会压制 system/workspace/ 的
    namespace 包（tasks/workspace.py 裸模块优先于包）。task_types 必须
    保留——测试模块收集期绑定的 TaskStatus 实例依赖其驻留。
    """
    _saved = sys.path[:]
    _s = str(_TASKS_DIR)
    if sys.path[0] != _s:
        # 先去重再插到最前：重复副本会让"移除一个副本"的测试（如
        # test_migration_batch3 的 workspace 包解压）残留另一副本，继续
        # 压制 namespace 包解析。
        sys.path[:] = [p for p in sys.path if p != _s]
        sys.path.insert(0, _s)
    sys.modules.pop("storage", None)
    yield
    sys.path[:] = _saved
    sys.modules.pop("storage", None)


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
        """删除父任务时级联清理子任务管道文件并删子任务记录（硬删口径）。"""
        svc = _make_service()
        parent = await svc.create_task(title="父任务")
        child = await svc.create_task(
            title="子任务", parent_task_id=parent.id,
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
            result = await svc.delete_task(parent.id)

        assert result is True
        # 父任务记录硬删除（容器软删分支已随容器语义退役）
        assert svc.get_task(parent.id) is None
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
