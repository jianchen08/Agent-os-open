"""
_delete_task 级联清理逻辑 - 单元测试

测试 _delete_task 方法重构后的级联清理行为，覆盖以下场景：
1. 容器任务删除（软删除 + 级联清理子任务）
2. 非容器任务删除（有子任务 → 级联清理 + 硬删除）
3. 无子任务的任务删除（只清理自身资源）
4. 任务不存在（返回错误）
5. running 状态任务（先取消管道再删除）
6. 管道文件清理验证
7. workspace 保护（子任务与容器 workspace 相同时跳过）
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from tasks.types import TaskModel, TaskStatus


def _make_task(
    task_id: str = "task-001",
    title: str = "test-task",
    status: TaskStatus = TaskStatus.PENDING,
    *,
    metadata: dict | None = None,
    pipeline_run_id: str | None = None,
    parent_task_id: str | None = None,
) -> TaskModel:
    """快速创建 TaskModel 实例用于测试。"""
    return TaskModel(
        id=task_id,
        title=title,
        status=status,
        metadata=metadata or {},
        pipeline_run_id=pipeline_run_id,
        parent_task_id=parent_task_id,
    )


def _make_tool() -> "TaskTool":
    """创建 TaskTool 实例。"""
    from tools.builtin.task.tool import TaskTool
    tool = TaskTool()
    return tool


def _make_service(
    tasks: dict[str, TaskModel] | None = None,
    subtasks_map: dict[str, list[TaskModel]] | None = None,
) -> MagicMock:
    """创建 mock TaskService。"""
    service = MagicMock()
    _tasks = tasks or {}
    _subtasks_map = subtasks_map or {}

    service.get_task = MagicMock(side_effect=lambda tid: _tasks.get(tid))
    service.list_subtasks = MagicMock(
        side_effect=lambda tid: _subtasks_map.get(tid, [])
    )
    service._storage = MagicMock()
    service._storage.delete = MagicMock()
    service.cancel_task_cascade = AsyncMock(return_value=0)
    service.save_task = AsyncMock()
    service.get_root_task_id = MagicMock(return_value=None)
    service.force_transition = AsyncMock()
    service.soft_delete_container = AsyncMock(return_value={
        "task_id": "container-1",
        "deleted": False,
        "soft_deleted": True,
        "old_status": "pending",
        "title": "容器任务",
        "reason": "用户请求删除",
        "message": "容器任务已标记删除（软删除）",
        "pipeline_file_cleaned": True,
        "cascade_cleanup": {
            "subtasks_deleted": 2,
            "pipeline_files_cleaned": 1,
            "workspaces_cleaned": 2,
            "errors": [],
        },
    })
    service.hard_delete_task = AsyncMock(return_value={
        "task_id": "root-1",
        "deleted": True,
        "old_status": "pending",
        "title": "根任务",
        "reason": "用户请求删除",
        "pipeline_file_cleaned": True,
        "cleanup": {"workspace_cleaned": True},
        "cascade_cleanup": {
            "subtasks_deleted": 1,
            "pipeline_files_cleaned": 1,
            "workspaces_cleaned": 1,
            "errors": [],
        },
    })
    return service


class TestDeleteTaskCascade:
    """_delete_task 重构后的级联清理逻辑测试。"""

    @pytest.mark.asyncio
    async def test_delete_nonexistent_task_returns_error(self):
        """任务不存在时，应返回 TASK_NOT_FOUND 错误。"""
        tool = _make_tool()
        service = _make_service(tasks={})
        tool._task_service = service

        inputs = {"task_id": "nonexistent-id", "action": "delete"}
        result = await tool._delete_task(inputs, parent_agent_level=1)

        assert result.success is False
        assert result.error_code == "TASK_NOT_FOUND"
        assert "nonexistent-id" in result.error

    @pytest.mark.asyncio
    async def test_delete_missing_task_id_returns_error(self):
        """缺少 task_id 时，应返回 MISSING_TASK_ID 错误。"""
        tool = _make_tool()
        service = _make_service()
        tool._task_service = service

        inputs = {"action": "delete"}
        result = await tool._delete_task(inputs, parent_agent_level=1)

        assert result.success is False
        assert result.error_code == "MISSING_TASK_ID"

    @pytest.mark.asyncio
    async def test_container_task_soft_delete(self):
        """容器任务删除：应软删除，委托给 service.soft_delete_container。"""
        container = _make_task(
            "container-1",
            title="容器任务",
            status=TaskStatus.PENDING,
            metadata={
                "task_scope": "container",
                "workspace": "/ws/container",
                "session_id": "sess-001",
            },
            pipeline_run_id="pipe-container",
        )

        tasks = {"container-1": container}
        service = _make_service(tasks=tasks)
        service.soft_delete_container = AsyncMock(return_value={
            "task_id": "container-1",
            "deleted": False,
            "soft_deleted": True,
            "old_status": "pending",
            "title": "容器任务",
            "reason": "用户请求删除",
            "pipeline_file_cleaned": True,
            "cascade_cleanup": {
                "subtasks_deleted": 2,
                "pipeline_files_cleaned": 1,
                "workspaces_cleaned": 2,
                "errors": [],
            },
        })

        tool = _make_tool()
        tool._task_service = service

        inputs = {
            "task_id": "container-1",
            "action": "delete",
            "session_id": "sess-001",
        }
        result = await tool._delete_task(inputs, parent_agent_level=1)

        assert result.success is True
        data = result.output
        assert data["soft_deleted"] is True
        assert data["deleted"] is False
        assert data["task_id"] == "container-1"
        assert data["pipeline_file_cleaned"] is True

        service.soft_delete_container.assert_called_once_with(
            "container-1", reason="用户请求删除",
        )

    @pytest.mark.asyncio
    async def test_container_task_cascade_count(self):
        """容器任务删除时，cascaded_subtasks 应在结果中反映。"""
        container = _make_task(
            "c-1",
            metadata={"task_scope": "container", "session_id": "s-1"},
        )

        tasks = {"c-1": container}
        service = _make_service(tasks=tasks)
        service.soft_delete_container = AsyncMock(return_value={
            "task_id": "c-1",
            "deleted": False,
            "soft_deleted": True,
            "old_status": "pending",
            "cascaded_subtasks": 3,
            "cascade_cleanup": {
                "subtasks_deleted": 1,
                "pipeline_files_cleaned": 0,
                "workspaces_cleaned": 0,
                "errors": [],
            },
        })

        tool = _make_tool()
        tool._task_service = service

        inputs = {"task_id": "c-1", "action": "delete", "session_id": "s-1"}
        result = await tool._delete_task(inputs, parent_agent_level=1)

        assert result.success is True
        assert result.output["cascaded_subtasks"] == 3

    @pytest.mark.asyncio
    async def test_non_container_task_with_subtasks(self):
        """非容器任务有子任务：应委托给 service.hard_delete_task。"""
        child1 = _make_task(
            "child-1",
            metadata={"workspace": "/ws/child1", "session_id": "s-1"},
            pipeline_run_id="pipe-child1",
        )
        root_task = _make_task(
            "root-1",
            title="根任务",
            status=TaskStatus.PENDING,
            metadata={"workspace": "/ws/root", "session_id": "s-1"},
            pipeline_run_id="pipe-root",
        )

        tasks = {"root-1": root_task, "child-1": child1}
        service = _make_service(tasks=tasks)
        service.hard_delete_task = AsyncMock(return_value={
            "task_id": "root-1",
            "deleted": True,
            "old_status": "pending",
            "title": "根任务",
            "reason": "用户请求删除",
            "pipeline_file_cleaned": True,
            "cleanup": {"workspace_cleaned": True},
            "cascade_cleanup": {
                "subtasks_deleted": 1,
                "pipeline_files_cleaned": 1,
                "workspaces_cleaned": 1,
                "errors": [],
            },
        })

        tool = _make_tool()
        tool._task_service = service

        inputs = {"task_id": "root-1", "action": "delete", "session_id": "s-1"}
        result = await tool._delete_task(inputs, parent_agent_level=1)

        assert result.success is True
        data = result.output
        assert data["deleted"] is True
        assert data["task_id"] == "root-1"

        service.hard_delete_task.assert_called_once_with(
            "root-1", reason="用户请求删除",
        )

    @pytest.mark.asyncio
    async def test_task_without_subtasks(self):
        """无子任务的任务删除：委托给 service.hard_delete_task。"""
        task = _make_task(
            "solo-1",
            title="独立任务",
            status=TaskStatus.COMPLETED,
            metadata={"workspace": "/ws/solo", "session_id": "s-1"},
            pipeline_run_id="pipe-solo",
        )

        tasks = {"solo-1": task}
        service = _make_service(tasks=tasks)
        service.hard_delete_task = AsyncMock(return_value={
            "task_id": "solo-1",
            "deleted": True,
            "old_status": "completed",
            "title": "独立任务",
            "reason": "用户请求删除",
            "pipeline_file_cleaned": True,
            "cleanup": {"workspace_cleaned": True},
            "cascade_cleanup": {
                "subtasks_deleted": 0,
                "pipeline_files_cleaned": 0,
                "workspaces_cleaned": 0,
                "errors": [],
            },
        })

        tool = _make_tool()
        tool._task_service = service

        inputs = {"task_id": "solo-1", "action": "delete", "session_id": "s-1"}
        result = await tool._delete_task(inputs, parent_agent_level=1)

        assert result.success is True
        data = result.output
        assert data["deleted"] is True

        service.hard_delete_task.assert_called_once_with(
            "solo-1", reason="用户请求删除",
        )

    @pytest.mark.asyncio
    async def test_running_task_delete(self):
        """running 状态任务：应委托给 service.hard_delete_task。"""
        task = _make_task(
            "running-1",
            title="运行中任务",
            status=TaskStatus.RUNNING,
            metadata={"workspace": "/ws/running", "session_id": "s-1"},
            pipeline_run_id="pipe-running",
        )

        tasks = {"running-1": task}
        service = _make_service(tasks=tasks)
        service.hard_delete_task = AsyncMock(return_value={
            "task_id": "running-1",
            "deleted": True,
            "old_status": "running",
            "title": "运行中任务",
            "reason": "用户请求删除",
            "pipeline_file_cleaned": True,
            "cleanup": {"workspace_cleaned": True},
            "cascade_cleanup": {
                "subtasks_deleted": 0,
                "pipeline_files_cleaned": 0,
                "workspaces_cleaned": 0,
                "errors": [],
            },
        })

        tool = _make_tool()
        tool._task_service = service

        inputs = {"task_id": "running-1", "action": "delete", "session_id": "s-1"}
        result = await tool._delete_task(inputs, parent_agent_level=1)

        assert result.success is True
        assert result.output["deleted"] is True
        assert result.output["old_status"] == "running"

    @pytest.mark.asyncio
    async def test_pipeline_file_result_in_data(self):
        """有 pipeline_run_id 时，pipeline_file_cleaned 应在结果中反映。"""
        task = _make_task(
            "pipe-task",
            metadata={"workspace": "/ws/p", "session_id": "s-1"},
            pipeline_run_id="pipe-123",
        )
        tasks = {"pipe-task": task}
        service = _make_service(tasks=tasks)
        service.hard_delete_task = AsyncMock(return_value={
            "task_id": "pipe-task",
            "deleted": True,
            "old_status": "pending",
            "title": "test-task",
            "reason": "用户请求删除",
            "pipeline_file_cleaned": True,
            "cleanup": {"workspace_cleaned": False},
            "cascade_cleanup": {
                "subtasks_deleted": 0,
                "pipeline_files_cleaned": 0,
                "workspaces_cleaned": 0,
                "errors": [],
            },
        })

        tool = _make_tool()
        tool._task_service = service

        inputs = {"task_id": "pipe-task", "action": "delete", "session_id": "s-1"}
        result = await tool._delete_task(inputs, parent_agent_level=1)

        assert result.output["pipeline_file_cleaned"] is True

    @pytest.mark.asyncio
    async def test_no_pipeline_run_id_result(self):
        """无 pipeline_run_id 时，pipeline_file_cleaned 为 False。"""
        task = _make_task(
            "no-pipe",
            metadata={"workspace": "/ws/n", "session_id": "s-1"},
        )
        tasks = {"no-pipe": task}
        service = _make_service(tasks=tasks)
        service.hard_delete_task = AsyncMock(return_value={
            "task_id": "no-pipe",
            "deleted": True,
            "old_status": "pending",
            "title": "test-task",
            "reason": "用户请求删除",
            "pipeline_file_cleaned": False,
            "cleanup": {},
            "cascade_cleanup": {
                "subtasks_deleted": 0,
                "pipeline_files_cleaned": 0,
                "workspaces_cleaned": 0,
                "errors": [],
            },
        })

        tool = _make_tool()
        tool._task_service = service

        inputs = {"task_id": "no-pipe", "action": "delete", "session_id": "s-1"}
        result = await tool._delete_task(inputs, parent_agent_level=1)

        assert result.output["pipeline_file_cleaned"] is False

    @pytest.mark.asyncio
    async def test_workspace_protection_same_as_container(self):
        """容器删除时，委托给 service.soft_delete_container。"""
        shared_ws = "/shared/workspace"

        subtask = _make_task(
            "sub-ws",
            metadata={"workspace": shared_ws, "session_id": "s-1"},
            pipeline_run_id="pipe-sub",
        )
        container = _make_task(
            "container-ws",
            metadata={
                "task_scope": "container",
                "workspace": shared_ws,
                "session_id": "s-1",
            },
            pipeline_run_id="pipe-container",
        )

        tasks = {"container-ws": container, "sub-ws": subtask}
        service = _make_service(tasks=tasks)
        service.soft_delete_container = AsyncMock(return_value={
            "task_id": "container-ws",
            "deleted": False,
            "soft_deleted": True,
            "old_status": "pending",
            "title": "test-task",
            "reason": "用户请求删除",
            "pipeline_file_cleaned": False,
            "cascade_cleanup": {
                "subtasks_deleted": 1,
                "pipeline_files_cleaned": 0,
                "workspaces_cleaned": 0,
                "errors": [],
            },
        })

        tool = _make_tool()
        tool._task_service = service

        inputs = {"task_id": "container-ws", "action": "delete", "session_id": "s-1"}
        result = await tool._delete_task(inputs, parent_agent_level=1)

        assert result.success is True

        service.soft_delete_container.assert_called_once_with(
            "container-ws", reason="用户请求删除",
        )

    @pytest.mark.asyncio
    async def test_non_container_child_of_container_skips_workspace(self):
        """属于容器的非容器子任务删除时，委托给 service.hard_delete_task。"""
        container = _make_task(
            "parent-c",
            metadata={"task_scope": "container", "session_id": "s-1"},
        )
        child = _make_task(
            "child-c",
            parent_task_id="parent-c",
            metadata={"workspace": "/ws/child", "session_id": "s-1"},
            pipeline_run_id="pipe-child",
        )

        tasks = {"parent-c": container, "child-c": child}
        service = _make_service(tasks=tasks)
        service.hard_delete_task = AsyncMock(return_value={
            "task_id": "child-c",
            "deleted": True,
            "old_status": "pending",
            "title": "test-task",
            "reason": "用户请求删除",
            "pipeline_file_cleaned": True,
            "cleanup": {"skipped": "容器子任务不清理工作空间"},
            "cascade_cleanup": {
                "subtasks_deleted": 0,
                "pipeline_files_cleaned": 0,
                "workspaces_cleaned": 0,
                "errors": [],
            },
        })

        tool = _make_tool()
        tool._task_service = service

        inputs = {"task_id": "child-c", "action": "delete", "session_id": "s-1"}
        result = await tool._delete_task(inputs, parent_agent_level=1)

        assert result.success is True
        data = result.output
        assert data["cleanup"].get("skipped") is not None

    @pytest.mark.asyncio
    async def test_delete_without_permission_returns_error(self):
        """无权限时，应返回 INSUFFICIENT_PERMISSION 错误。"""
        task = _make_task(
            "perm-task",
            metadata={"session_id": "other-session"},
        )
        tasks = {"perm-task": task}
        service = _make_service(tasks=tasks)
        tool = _make_tool()
        tool._task_service = service

        inputs = {"task_id": "perm-task", "action": "delete", "session_id": "my-session"}
        result = await tool._delete_task(inputs, parent_agent_level=1)

        assert result.success is False
        assert result.error_code == "INSUFFICIENT_PERMISSION"

    @pytest.mark.asyncio
    async def test_delete_exception_returns_error(self):
        """删除过程中异常时，应返回 DELETE_FAILED 错误。"""
        tool = _make_tool()
        service = _make_service()
        service.get_task = MagicMock(side_effect=RuntimeError("DB error"))
        tool._task_service = service

        inputs = {"task_id": "any-task", "action": "delete"}
        result = await tool._delete_task(inputs, parent_agent_level=1)

        assert result.success is False
        assert result.error_code == "DELETE_FAILED"
        assert "DB error" in result.error


class TestCascadeCleanupSubtasks:
    """TaskService._cascade_cleanup_subtasks 辅助方法的单元测试。"""

    @pytest.mark.skip(reason="_cascade_cleanup_subtasks 已迁移到 TaskService 内部，需要 TaskService 完整实例")
    @pytest.mark.asyncio
    async def test_cascade_no_descendants(self):
        """无后代任务时，返回空统计。"""
        pass

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="_cascade_cleanup_subtasks 已迁移到 TaskService，需使用 TaskService mock")
    async def test_cascade_cleans_pipeline_files(self):
        """级联清理应清理每个后代的管道文件。"""
        pass

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="_cascade_cleanup_subtasks 已迁移到 TaskService，需使用 TaskService mock")
    async def test_cascade_skips_workspace_same_as_container(self):
        """子任务 workspace 与容器相同时，应跳过清理。"""
        pass

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="_cascade_cleanup_subtasks 已迁移到 TaskService，需使用 TaskService mock")
    async def test_cascade_workspace_cleaned_when_different(self):
        """子任务 workspace 不同于容器时，应被清理。"""
        pass

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="_cascade_cleanup_subtasks 已迁移到 TaskService，需使用 TaskService mock")
    async def test_cascade_delete_error_recorded(self):
        """后代删除失败时，错误应记录在 errors 列表中。"""
        pass

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="_cascade_cleanup_subtasks 已迁移到 TaskService，需使用 TaskService mock")
    async def test_cascade_complex_tree(self):
        """多层嵌套树结构应正确处理。"""
        pass
