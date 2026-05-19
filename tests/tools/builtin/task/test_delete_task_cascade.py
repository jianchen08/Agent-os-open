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


# ---------------------------------------------------------------------------
# 辅助工具
# ---------------------------------------------------------------------------

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
    return service


# ===========================================================================
# 测试类：_delete_task 级联清理
# ===========================================================================

class TestDeleteTaskCascade:
    """_delete_task 重构后的级联清理逻辑测试。"""

    # ---- 场景 4: 任务不存在 ----

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

    # ---- 场景 4: 缺少 task_id ----

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

    # ---- 场景 1: 容器任务删除（软删除路径） ----

    @pytest.mark.asyncio
    async def test_container_task_soft_delete(self):
        """容器任务删除：应软删除（标记 FAILED + soft_deleted），级联清理子任务。"""
        subtask1 = _make_task(
            "sub-1",
            metadata={"workspace": "/ws/sub1", "session_id": "sess-001"},
            pipeline_run_id="pipe-sub1",
        )
        subtask2 = _make_task(
            "sub-2",
            metadata={"workspace": "/ws/sub2", "session_id": "sess-001"},
        )

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

        tasks = {"container-1": container, "sub-1": subtask1, "sub-2": subtask2}
        subtasks_map = {"container-1": [subtask1, subtask2]}
        service = _make_service(tasks=tasks, subtasks_map=subtasks_map)
        tool = _make_tool()
        tool._task_service = service

        # Mock 辅助方法
        tool._cancel_pipeline_recursive = MagicMock()
        tool._cascade_cleanup_subtasks = AsyncMock(return_value={
            "subtasks_deleted": 2,
            "pipeline_files_cleaned": 1,
            "workspaces_cleaned": 2,
            "errors": [],
        })
        tool._cleanup_pipeline_file = MagicMock(return_value=True)

        inputs = {
            "task_id": "container-1",
            "action": "delete",
            "session_id": "sess-001",
        }
        result = await tool._delete_task(inputs, parent_agent_level=1)

        # 验证软删除结果
        assert result.success is True
        data = result.data
        assert data["soft_deleted"] is True
        assert data["deleted"] is False
        assert data["task_id"] == "container-1"

        # 验证状态被标记为 FAILED
        assert container.status == TaskStatus.FAILED
        assert container.metadata.get("soft_deleted") is True

        # 验证级联清理被调用，且传入容器的 workspace 作为保护路径
        tool._cascade_cleanup_subtasks.assert_called_once_with(
            service,
            "container-1",
            skip_workspace=False,
            container_workspace="/ws/container",
        )

        # 验证容器自身管道文件被清理
        tool._cleanup_pipeline_file.assert_called_once_with("pipe-container")
        assert data["pipeline_file_cleaned"] is True

        # 验证级联取消管道被调用
        tool._cancel_pipeline_recursive.assert_called_once_with("container-1")

    # ---- 场景 1: 容器任务有 cascaded_subtasks 计数 ----

    @pytest.mark.asyncio
    async def test_container_task_cascade_count(self):
        """容器任务删除时，cancel_task_cascade 返回值应反映在 cascaded_subtasks 中。"""
        container = _make_task(
            "c-1",
            metadata={"task_scope": "container", "session_id": "s-1"},
        )
        sub = _make_task(
            "s-1",
            parent_task_id="c-1",
            metadata={"session_id": "s-1"},
        )
        tasks = {"c-1": container, "s-1": sub}
        subtasks_map = {"c-1": [sub]}

        service = _make_service(tasks=tasks, subtasks_map=subtasks_map)
        service.cancel_task_cascade = AsyncMock(return_value=3)
        tool = _make_tool()
        tool._task_service = service

        tool._cancel_pipeline_recursive = MagicMock()
        tool._cascade_cleanup_subtasks = AsyncMock(return_value={
            "subtasks_deleted": 1, "pipeline_files_cleaned": 0,
            "workspaces_cleaned": 0, "errors": [],
        })
        tool._cleanup_pipeline_file = MagicMock(return_value=False)

        inputs = {"task_id": "c-1", "action": "delete", "session_id": "s-1"}
        result = await tool._delete_task(inputs, parent_agent_level=1)

        assert result.success is True
        assert result.data["cascaded_subtasks"] == 3

    # ---- 场景 2: 非容器任务删除（有子任务） ----

    @pytest.mark.asyncio
    async def test_non_container_task_with_subtasks(self):
        """非容器任务有子任务：应级联清理子任务 + 清理自身资源 + 硬删除。"""
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
        subtasks_map = {"root-1": [child1]}
        service = _make_service(tasks=tasks, subtasks_map=subtasks_map)
        service.get_root_task_id = MagicMock(return_value="root-1")

        tool = _make_tool()
        tool._task_service = service

        tool._cancel_pipeline_recursive = MagicMock()
        tool._cascade_cleanup_subtasks = AsyncMock(return_value={
            "subtasks_deleted": 1,
            "pipeline_files_cleaned": 1,
            "workspaces_cleaned": 1,
            "errors": [],
        })
        tool._cleanup_pipeline_file = MagicMock(return_value=True)
        tool._cleanup_task_resources = AsyncMock(return_value={"workspace_cleaned": True})

        inputs = {"task_id": "root-1", "action": "delete", "session_id": "s-1"}
        result = await tool._delete_task(inputs, parent_agent_level=1)

        # 验证硬删除结果
        assert result.success is True
        data = result.data
        assert data["deleted"] is True
        assert data["task_id"] == "root-1"
        assert "soft_deleted" not in data or data.get("soft_deleted") is not True

        # 验证级联清理被调用（非容器子任务 → skip_workspace=False）
        tool._cascade_cleanup_subtasks.assert_called_once_with(
            service, "root-1",
            skip_workspace=False,
            container_workspace="",
        )

        # 验证自身管道文件清理
        tool._cleanup_pipeline_file.assert_called_once_with("pipe-root")

        # 验证自身 workspace 清理
        tool._cleanup_task_resources.assert_called_once_with(
            task_id="root-1", workspace="/ws/root",
        )

        # 验证存储记录被删除
        service._storage.delete.assert_called_once_with("root-1")

    # ---- 场景 3: 无子任务的任务删除 ----

    @pytest.mark.asyncio
    async def test_task_without_subtasks(self):
        """无子任务的任务删除：只清理自身资源，不调用级联清理。"""
        task = _make_task(
            "solo-1",
            title="独立任务",
            status=TaskStatus.COMPLETED,
            metadata={"workspace": "/ws/solo", "session_id": "s-1"},
            pipeline_run_id="pipe-solo",
        )

        tasks = {"solo-1": task}
        service = _make_service(tasks=tasks, subtasks_map={})
        service.get_root_task_id = MagicMock(return_value="solo-1")

        tool = _make_tool()
        tool._task_service = service

        tool._cancel_pipeline_recursive = MagicMock()
        tool._cleanup_pipeline_file = MagicMock(return_value=True)
        tool._cleanup_task_resources = AsyncMock(return_value={"workspace_cleaned": True})
        tool._cascade_cleanup_subtasks = AsyncMock(return_value={
            "subtasks_deleted": 0, "pipeline_files_cleaned": 0,
            "workspaces_cleaned": 0, "errors": [],
        })

        inputs = {"task_id": "solo-1", "action": "delete", "session_id": "s-1"}
        result = await tool._delete_task(inputs, parent_agent_level=1)

        assert result.success is True
        data = result.data
        assert data["deleted"] is True

        # 级联清理不应被调用（无子任务时 list_subtasks 返回空）
        tool._cascade_cleanup_subtasks.assert_not_called()

        # 自身资源应被清理
        tool._cleanup_pipeline_file.assert_called_once_with("pipe-solo")
        tool._cleanup_task_resources.assert_called_once_with(
            task_id="solo-1", workspace="/ws/solo",
        )
        service._storage.delete.assert_called_once_with("solo-1")

    # ---- 场景 5: running 状态任务 ----

    @pytest.mark.asyncio
    async def test_running_task_cancel_pipeline_before_delete(self):
        """running 状态任务：应先取消管道再删除。"""
        task = _make_task(
            "running-1",
            title="运行中任务",
            status=TaskStatus.RUNNING,
            metadata={"workspace": "/ws/running", "session_id": "s-1"},
            pipeline_run_id="pipe-running",
        )

        tasks = {"running-1": task}
        service = _make_service(tasks=tasks, subtasks_map={})
        service.get_root_task_id = MagicMock(return_value="running-1")

        tool = _make_tool()
        tool._task_service = service

        tool._cancel_pipeline_recursive = MagicMock()
        tool._cleanup_pipeline_file = MagicMock(return_value=True)
        tool._cleanup_task_resources = AsyncMock(return_value={"workspace_cleaned": True})

        inputs = {"task_id": "running-1", "action": "delete", "session_id": "s-1"}
        result = await tool._delete_task(inputs, parent_agent_level=1)

        # 验证管道取消被调用
        tool._cancel_pipeline_recursive.assert_called_once_with("running-1")

        # 验证删除成功
        assert result.success is True
        assert result.data["deleted"] is True
        assert result.data["old_status"] == "running"

    # ---- 场景 6: 管道文件清理 ----

    @pytest.mark.asyncio
    async def test_pipeline_file_cleaned_when_present(self):
        """有 pipeline_run_id 时，_cleanup_pipeline_file 应被调用。"""
        task = _make_task(
            "pipe-task",
            metadata={"workspace": "/ws/p", "session_id": "s-1"},
            pipeline_run_id="pipe-123",
        )
        tasks = {"pipe-task": task}
        service = _make_service(tasks=tasks)
        service.get_root_task_id = MagicMock(return_value="pipe-task")

        tool = _make_tool()
        tool._task_service = service
        tool._cancel_pipeline_recursive = MagicMock()
        tool._cleanup_pipeline_file = MagicMock(return_value=True)
        tool._cleanup_task_resources = AsyncMock(return_value={"workspace_cleaned": False})

        inputs = {"task_id": "pipe-task", "action": "delete", "session_id": "s-1"}
        result = await tool._delete_task(inputs, parent_agent_level=1)

        tool._cleanup_pipeline_file.assert_called_once_with("pipe-123")
        assert result.data["pipeline_file_cleaned"] is True

    @pytest.mark.asyncio
    async def test_pipeline_file_skipped_when_no_pipeline_run_id(self):
        """无 pipeline_run_id 时，_cleanup_pipeline_file 不应被调用，结果为 False。"""
        task = _make_task(
            "no-pipe",
            metadata={"workspace": "/ws/n", "session_id": "s-1"},
        )
        tasks = {"no-pipe": task}
        service = _make_service(tasks=tasks)
        service.get_root_task_id = MagicMock(return_value="no-pipe")

        tool = _make_tool()
        tool._task_service = service
        tool._cancel_pipeline_recursive = MagicMock()
        tool._cleanup_pipeline_file = MagicMock(return_value=False)
        tool._cleanup_task_resources = AsyncMock(return_value={})

        inputs = {"task_id": "no-pipe", "action": "delete", "session_id": "s-1"}
        result = await tool._delete_task(inputs, parent_agent_level=1)

        # pipeline_run_id 为 None，不应调用清理
        tool._cleanup_pipeline_file.assert_not_called()
        assert result.data["pipeline_file_cleaned"] is False

    # ---- 场景 7: workspace 保护（容器子任务与容器 workspace 相同） ----

    @pytest.mark.asyncio
    async def test_workspace_protection_same_as_container(self):
        """容器删除时，子任务 workspace 与容器相同时应跳过清理。"""
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
        subtasks_map = {"container-ws": [subtask]}
        service = _make_service(tasks=tasks, subtasks_map=subtasks_map)

        tool = _make_tool()
        tool._task_service = service
        tool._cancel_pipeline_recursive = MagicMock()
        tool._cleanup_pipeline_file = MagicMock(return_value=False)

        # Mock 级联清理以验证参数传递
        tool._cascade_cleanup_subtasks = AsyncMock(return_value={
            "subtasks_deleted": 1,
            "pipeline_files_cleaned": 0,
            "workspaces_cleaned": 0,
            "errors": [],
        })

        inputs = {"task_id": "container-ws", "action": "delete", "session_id": "s-1"}
        result = await tool._delete_task(inputs, parent_agent_level=1)

        # 验证容器删除成功
        assert result.success is True

        # 验证 _cascade_cleanup_subtasks 被调用时传入了 container_workspace
        tool._cascade_cleanup_subtasks.assert_called_once_with(
            service,
            "container-ws",
            skip_workspace=False,
            container_workspace=shared_ws,
        )

    # ---- 非容器任务是容器子任务时跳过 workspace 清理 ----

    @pytest.mark.asyncio
    async def test_non_container_child_of_container_skips_workspace(self):
        """属于容器的非容器子任务删除时，skip_workspace=True，不清理 workspace。"""
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
        # _is_child_of_container 向上追溯发现根任务是 container
        service.get_root_task_id = MagicMock(return_value="parent-c")

        tool = _make_tool()
        tool._task_service = service
        tool._cancel_pipeline_recursive = MagicMock()
        tool._cleanup_pipeline_file = MagicMock(return_value=True)

        inputs = {"task_id": "child-c", "action": "delete", "session_id": "s-1"}
        result = await tool._delete_task(inputs, parent_agent_level=1)

        assert result.success is True
        data = result.data

        # 容器子任务的 cleanup 结果应标记 skipped
        assert data["cleanup"].get("skipped") is not None

    # ---- 权限检查 ----

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

    # ---- 异常兜底 ----

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


# ===========================================================================
# 测试类：_cascade_cleanup_subtasks 辅助方法
# ===========================================================================

class TestCascadeCleanupSubtasks:
    """_cascade_cleanup_subtasks 辅助方法的单元测试。"""

    @pytest.mark.asyncio
    async def test_cascade_no_descendants(self):
        """无后代任务时，返回空统计。"""
        tool = _make_tool()
        service = _make_service()
        tool._collect_all_descendant_ids = MagicMock(return_value=[])

        result = await tool._cascade_cleanup_subtasks(service, "task-1")

        assert result["subtasks_deleted"] == 0
        assert result["pipeline_files_cleaned"] == 0
        assert result["workspaces_cleaned"] == 0
        assert result["errors"] == []

    @pytest.mark.asyncio
    async def test_cascade_cleans_pipeline_files(self):
        """级联清理应清理每个后代的管道文件。"""
        desc1 = _make_task("d-1", pipeline_run_id="pipe-1")
        desc2 = _make_task("d-2", pipeline_run_id="pipe-2")

        tasks = {"d-1": desc1, "d-2": desc2}
        service = _make_service(tasks=tasks)

        tool = _make_tool()
        tool._collect_all_descendant_ids = MagicMock(return_value=["d-1", "d-2"])
        tool._cleanup_pipeline_file = MagicMock(return_value=True)
        tool._cleanup_task_resources = AsyncMock(return_value={"workspace_cleaned": False})

        result = await tool._cascade_cleanup_subtasks(
            service, "parent-1", skip_workspace=True,
        )

        assert result["pipeline_files_cleaned"] == 2
        assert result["subtasks_deleted"] == 2

    @pytest.mark.asyncio
    async def test_cascade_skips_workspace_same_as_container(self):
        """子任务 workspace 与容器相同时，应跳过清理。"""
        shared_ws = "/shared/path"
        desc = _make_task("d-1", metadata={"workspace": shared_ws})

        tasks = {"d-1": desc}
        service = _make_service(tasks=tasks)

        tool = _make_tool()
        tool._collect_all_descendant_ids = MagicMock(return_value=["d-1"])
        tool._cleanup_pipeline_file = MagicMock(return_value=False)
        tool._cleanup_task_resources = AsyncMock(return_value={"workspace_cleaned": True})

        result = await tool._cascade_cleanup_subtasks(
            service, "parent-1",
            skip_workspace=False,
            container_workspace=shared_ws,
        )

        # workspace 与容器相同，应跳过，不清理
        assert result["workspaces_cleaned"] == 0
        tool._cleanup_task_resources.assert_not_called()

    @pytest.mark.asyncio
    async def test_cascade_skips_none_descendant(self):
        """后代任务已被删除（get_task 返回 None）时，应跳过。"""
        service = _make_service(tasks={})

        tool = _make_tool()
        tool._collect_all_descendant_ids = MagicMock(return_value=["ghost-1"])
        tool._cleanup_pipeline_file = MagicMock(return_value=True)

        result = await tool._cascade_cleanup_subtasks(service, "parent-1")

        assert result["subtasks_deleted"] == 0
        assert result["pipeline_files_cleaned"] == 0

    @pytest.mark.asyncio
    async def test_cascade_delete_record_error_is_nonfatal(self):
        """删除存储记录失败时，应记入 errors 但不中断。"""
        desc = _make_task("d-1")
        tasks = {"d-1": desc}
        service = _make_service(tasks=tasks)
        service._storage.delete = MagicMock(side_effect=IOError("disk full"))

        tool = _make_tool()
        tool._collect_all_descendant_ids = MagicMock(return_value=["d-1"])
        tool._cleanup_pipeline_file = MagicMock(return_value=False)

        result = await tool._cascade_cleanup_subtasks(
            service, "parent-1", skip_workspace=True,
        )

        assert len(result["errors"]) == 1
        assert "d-1" in result["errors"][0]
        assert "disk full" in result["errors"][0]


# ===========================================================================
# 测试类：_cleanup_pipeline_file 辅助方法
# ===========================================================================

class TestCleanupPipelineFile:
    """_cleanup_pipeline_file 辅助方法的单元测试。"""

    def test_empty_pipeline_run_id_returns_false(self):
        """空 pipeline_run_id 应返回 False。"""
        tool = _make_tool()
        assert tool._cleanup_pipeline_file("") is False
        assert tool._cleanup_pipeline_file(None) is False

    def test_storage_none_returns_false(self):
        """storage 为 None 时应返回 False。"""
        tool = _make_tool()
        tool._get_execution_record_storage = MagicMock(return_value=None)

        assert tool._cleanup_pipeline_file("pipe-123") is False

    def test_successful_cleanup_returns_true(self):
        """成功删除记录时应返回 True。"""
        tool = _make_tool()
        mock_storage = MagicMock()
        mock_storage.delete_by_session = MagicMock(return_value=5)
        tool._get_execution_record_storage = MagicMock(return_value=mock_storage)

        assert tool._cleanup_pipeline_file("pipe-123") is True
        mock_storage.delete_by_session.assert_called_once_with("pipe-123")

    def test_no_records_deleted_returns_false(self):
        """未删除任何记录时应返回 False。"""
        tool = _make_tool()
        mock_storage = MagicMock()
        mock_storage.delete_by_session = MagicMock(return_value=0)
        tool._get_execution_record_storage = MagicMock(return_value=mock_storage)

        assert tool._cleanup_pipeline_file("pipe-123") is False

    def test_exception_returns_false(self):
        """异常时应返回 False（non-fatal）。"""
        tool = _make_tool()
        mock_storage = MagicMock()
        mock_storage.delete_by_session = MagicMock(side_effect=Exception("boom"))
        tool._get_execution_record_storage = MagicMock(return_value=mock_storage)

        assert tool._cleanup_pipeline_file("pipe-123") is False


# ===========================================================================
# 测试类：_collect_all_descendant_ids 辅助方法
# ===========================================================================

class TestCollectAllDescendantIds:
    """_collect_all_descendant_ids 递归收集后代任务的测试。"""

    def test_no_subtasks(self):
        """无子任务时返回空列表。"""
        service = _make_service(subtasks_map={"t-1": []})
        from tools.builtin.task.tool import TaskTool
        result = TaskTool._collect_all_descendant_ids(service, "t-1")
        assert result == []

    def test_flat_subtasks(self):
        """只有一级子任务时，返回所有子任务 ID。"""
        s1 = _make_task("s-1")
        s2 = _make_task("s-2")
        service = _make_service(subtasks_map={"t-1": [s1, s2], "s-1": [], "s-2": []})

        from tools.builtin.task.tool import TaskTool
        result = TaskTool._collect_all_descendant_ids(service, "t-1")

        assert result == ["s-1", "s-2"]

    def test_nested_subtasks_depth_first(self):
        """多层嵌套时，应深度优先、叶子在前。"""
        ss1 = _make_task("ss-1")
        ss2 = _make_task("ss-2")
        s1 = _make_task("s-1")
        s2 = _make_task("s-2")

        subtasks_map = {
            "t-1": [s1, s2],
            "s-1": [ss1, ss2],
            "s-2": [],
            "ss-1": [],
            "ss-2": [],
        }
        service = _make_service(subtasks_map=subtasks_map)

        from tools.builtin.task.tool import TaskTool
        result = TaskTool._collect_all_descendant_ids(service, "t-1")

        # 深度优先：先 s-1 的子树 → ss-1, ss-2, s-1，然后 s-2
        assert result == ["ss-1", "ss-2", "s-1", "s-2"]
