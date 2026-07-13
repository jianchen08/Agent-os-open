"""容器完成时自动清理子任务 worktree 机制的单元测试。

测试覆盖：
1. 正常清理子任务 worktree
2. 子任务无 workspace_path 时跳过
3. 保护容器自身 workspace 不被删除
4. 清理失败不阻塞容器完成
5. 容器无子任务时跳过清理
6. worktree/分支已不存在的优雅处理
7. 在 change(status=completed) 流程中集成清理
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tasks.types import TaskModel, TaskStatus
from tools.builtin.task.tool import TaskTool


# ── 辅助 ──────────────────────────────────────────────


def _make_task(
    task_id: str = "task_001",
    title: str = "Test Task",
    status: TaskStatus = TaskStatus.COMPLETED,
    parent_task_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    **kwargs: Any,
) -> TaskModel:
    """构造测试任务。"""
    return TaskModel(
        id=task_id,
        title=title,
        status=status,
        parent_task_id=parent_task_id,
        metadata=metadata or {},
        **kwargs,
    )


def _make_container_with_subtasks(
    container_id: str = "container_001",
    subtask_workspaces: list[str | None] | None = None,
    container_workspace: str = "/ws/container_001",
) -> tuple[TaskModel, list[TaskModel]]:
    """构造容器任务及其子任务。

    Args:
        container_id: 容器任务 ID
        subtask_workspaces: 每个子任务的 workspace 路径列表（None 表示无 workspace）
        container_workspace: 容器自身的 workspace 路径

    Returns:
        (容器任务, 子任务列表)
    """
    container = _make_task(
        task_id=container_id,
        title="Container Task",
        status=TaskStatus.PENDING,
        metadata={
            "task_scope": "container",
            "workspace": container_workspace,
        },
    )

    if subtask_workspaces is None:
        subtask_workspaces = ["/ws/sub_001", "/ws/sub_002"]

    subtasks = []
    for i, ws in enumerate(subtask_workspaces):
        meta: dict[str, Any] = {}
        if ws is not None:
            meta["workspace"] = ws
        subtask = _make_task(
            task_id=f"sub_{i:03d}",
            title=f"Subtask {i}",
            status=TaskStatus.COMPLETED,
            parent_task_id=container_id,
            metadata=meta,
        )
        subtasks.append(subtask)

    return container, subtasks


def _mock_task_service(
    tasks: dict[str, TaskModel] | None = None,
    subtasks_map: dict[str, list[TaskModel]] | None = None,
) -> MagicMock:
    """创建 Mock TaskService。

    Args:
        tasks: 预设任务字典 {task_id: TaskModel}
        subtasks_map: 父子关系 {parent_id: [子任务列表]}

    Returns:
        配置好的 Mock TaskService
    """
    if tasks is None:
        tasks = {}
    if subtasks_map is None:
        subtasks_map = {}

    svc = MagicMock()

    def get_task(task_id: str) -> TaskModel | None:
        return tasks.get(task_id)

    svc.get_task.side_effect = get_task

    def list_subtasks(parent_id: str) -> list[TaskModel]:
        return subtasks_map.get(parent_id, [])

    svc.list_subtasks.side_effect = list_subtasks

    # async methods
    svc.force_transition = AsyncMock()
    svc.save_task = AsyncMock()

    return svc


# ── _cleanup_subtask_worktrees 单元测试 ──────────────────


class TestCleanupSubtaskWorktrees:
    """_cleanup_subtask_worktrees 方法测试。"""

    def test_cleanup_all_subtask_worktrees(self) -> None:
        """正常清理所有子任务的 worktree。"""
        container, subtasks = _make_container_with_subtasks(
            subtask_workspaces=["/ws/sub_001", "/ws/sub_002"],
        )
        tool = TaskTool()

        with patch.object(tool, "_cleanup_task_resources", new_callable=AsyncMock) as mock_cleanup:
            mock_cleanup.return_value = {"workspace_cleaned": True, "errors": []}

            result = asyncio.get_event_loop().run_until_complete(
                tool._cleanup_subtask_worktrees(container, subtasks)
            )

        assert result["total_subtasks"] == 2
        assert result["cleaned_count"] == 2
        assert result["skipped_count"] == 0
        assert result["errors"] == []
        assert mock_cleanup.call_count == 2

    def test_skip_subtask_without_workspace(self) -> None:
        """子任务无 workspace_path 时跳过。"""
        container, subtasks = _make_container_with_subtasks(
            subtask_workspaces=["/ws/sub_001", None, "/ws/sub_003"],
        )
        tool = TaskTool()

        with patch.object(tool, "_cleanup_task_resources", new_callable=AsyncMock) as mock_cleanup:
            mock_cleanup.return_value = {"workspace_cleaned": True, "errors": []}

            result = asyncio.get_event_loop().run_until_complete(
                tool._cleanup_subtask_worktrees(container, subtasks)
            )

        assert result["total_subtasks"] == 3
        assert result["cleaned_count"] == 2
        assert result["skipped_count"] == 1
        assert mock_cleanup.call_count == 2

    def test_protect_container_workspace(self) -> None:
        """保护容器自身的 workspace 不被删除。"""
        container_workspace = "/ws/container_001"
        container, subtasks = _make_container_with_subtasks(
            container_workspace=container_workspace,
            subtask_workspaces=[container_workspace, "/ws/sub_001"],
        )
        tool = TaskTool()

        with patch.object(tool, "_cleanup_task_resources", new_callable=AsyncMock) as mock_cleanup:
            mock_cleanup.return_value = {"workspace_cleaned": True, "errors": []}

            result = asyncio.get_event_loop().run_until_complete(
                tool._cleanup_subtask_worktrees(container, subtasks)
            )

        # 容器自身的 workspace 不应被清理
        assert result["total_subtasks"] == 2
        assert result["skipped_count"] == 1
        # 只有 sub_001 被清理（sub_000 的 workspace 等于容器的 workspace，被跳过）
        assert mock_cleanup.call_count == 1

    def test_cleanup_failure_does_not_block(self) -> None:
        """单个清理失败不阻塞后续清理。"""
        container, subtasks = _make_container_with_subtrees_with_errors()
        tool = TaskTool()

        with patch.object(tool, "_cleanup_task_resources", new_callable=AsyncMock) as mock_cleanup:
            # 第一个子任务清理失败，第二个成功
            mock_cleanup.side_effect = [
                {"workspace_cleaned": False, "errors": ["git worktree remove 失败"]},
                {"workspace_cleaned": True, "errors": []},
            ]

            result = asyncio.get_event_loop().run_until_complete(
                tool._cleanup_subtask_worktrees(container, subtasks)
            )

        assert result["total_subtasks"] == 2
        assert result["cleaned_count"] == 1
        assert result["error_count"] == 1
        assert len(result["errors"]) == 1
        assert mock_cleanup.call_count == 2

    def test_no_subtasks_skips_cleanup(self) -> None:
        """容器无子任务时跳过清理。"""
        container = _make_task(
            task_id="container_empty",
            metadata={"task_scope": "container", "workspace": "/ws/container"},
        )
        tool = TaskTool()

        result = asyncio.get_event_loop().run_until_complete(
            tool._cleanup_subtask_worktrees(container, [])
        )

        assert result["total_subtasks"] == 0
        assert result["cleaned_count"] == 0
        assert result["skipped_count"] == 0

    def test_workspace_lifecycle_cleanup_used(self) -> None:
        """优先使用 workspace_lifecycle 进行清理。"""
        container, subtasks = _make_container_with_subtasks(
            subtask_workspaces=["/ws/sub_001"],
        )
        tool = TaskTool()

        mock_lifecycle = MagicMock()
        mock_lifecycle.cleanup_workspace.return_value = {
            "worktree_removed": True,
            "branch_deleted": True,
        }
        mock_provider = MagicMock()
        mock_provider.get.return_value = mock_lifecycle

        with patch(
            "infrastructure.service_provider.get_service_provider",
            return_value=mock_provider,
        ):
            result = asyncio.get_event_loop().run_until_complete(
                tool._cleanup_subtask_worktrees(container, subtasks)
            )

        assert result["total_subtasks"] == 1
        assert result["cleaned_count"] == 1

    def test_worktree_not_exists_graceful_handling(self) -> None:
        """worktree 已不存在时优雅处理。"""
        container, subtasks = _make_container_with_subtasks(
            subtask_workspaces=["/ws/sub_001"],
        )
        tool = TaskTool()

        with patch.object(tool, "_cleanup_task_resources", new_callable=AsyncMock) as mock_cleanup:
            # 模拟 workspace 不存在时的返回
            mock_cleanup.return_value = {
                "workspace_cleaned": False,
                "errors": [],
            }

            result = asyncio.get_event_loop().run_until_complete(
                tool._cleanup_subtask_worktrees(container, subtasks)
            )

        assert result["total_subtasks"] == 1
        # 虽然没有实际清理，但不应该报错
        assert result["error_count"] == 0

    def test_exception_in_cleanup_caught(self) -> None:
        """清理函数抛出异常时被捕获，不传播到外层。"""
        container, subtasks = _make_container_with_subtasks(
            subtask_workspaces=["/ws/sub_001", "/ws/sub_002"],
        )
        tool = TaskTool()

        with patch.object(tool, "_cleanup_task_resources", new_callable=AsyncMock) as mock_cleanup:
            # 第一个抛异常，第二个正常
            mock_cleanup.side_effect = [
                Exception("unexpected error"),
                {"workspace_cleaned": True, "errors": []},
            ]

            result = asyncio.get_event_loop().run_until_complete(
                tool._cleanup_subtask_worktrees(container, subtasks)
            )

        assert result["error_count"] == 1
        assert result["cleaned_count"] == 1
        assert "unexpected error" in result["errors"][0]

    def test_subtask_with_empty_workspace_string_skipped(self) -> None:
        """子任务 workspace 为空字符串时跳过。"""
        container, subtasks = _make_container_with_subtasks(
            subtask_workspaces=["", "/ws/sub_001"],
        )
        tool = TaskTool()

        with patch.object(tool, "_cleanup_task_resources", new_callable=AsyncMock) as mock_cleanup:
            mock_cleanup.return_value = {"workspace_cleaned": True, "errors": []}

            result = asyncio.get_event_loop().run_until_complete(
                tool._cleanup_subtask_worktrees(container, subtasks)
            )

        assert result["skipped_count"] == 1
        assert result["cleaned_count"] == 1


# ── _change_status 集成测试 ──────────────────────────


class TestCompleteContainerIntegration:
    """_change_status(status=completed) 集成清理步骤的测试。

    change action 替代旧 complete_container，status=completed 时调用清理。
    """

    @pytest.mark.asyncio
    async def test_change_completed_calls_cleanup(self) -> None:
        """change(status=completed) 在完成前调用子任务 worktree 清理。"""
        container, subtasks = _make_container_with_subtasks(
            subtask_workspaces=["/ws/sub_001"],
        )

        svc = _mock_task_service(
            tasks={container.id: container, **{s.id: s for s in subtasks}},
            subtasks_map={container.id: subtasks},
        )

        tool = TaskTool()
        tool._task_service = svc

        with patch.object(svc, "_cleanup_subtask_worktrees", new_callable=AsyncMock) as mock_cleanup:
            mock_cleanup.return_value = {
                "total_subtasks": 1,
                "cleaned_count": 1,
                "skipped_count": 0,
                "error_count": 0,
                "errors": [],
            }

            result = await tool._change_status(
                inputs={
                    "task_id": container.id,
                    "status": "completed",
                    "container_reason": "所有子任务完成",
                },
                parent_agent_level=1,
            )

        assert result.success is True
        mock_cleanup.assert_called_once_with(container, subtasks)

    @pytest.mark.asyncio
    async def test_change_completed_cleanup_failure_does_not_block(self) -> None:
        """清理失败不影响容器完成。"""
        container, subtasks = _make_container_with_subtasks(
            subtask_workspaces=["/ws/sub_001"],
        )

        svc = _mock_task_service(
            tasks={container.id: container, **{s.id: s for s in subtasks}},
            subtasks_map={container.id: subtasks},
        )

        tool = TaskTool()
        tool._task_service = svc

        with patch.object(svc, "_cleanup_subtask_worktrees", new_callable=AsyncMock) as mock_cleanup:
            # 清理函数返回错误，但不应阻塞容器完成
            mock_cleanup.return_value = {
                "total_subtasks": 1,
                "cleaned_count": 0,
                "skipped_count": 0,
                "error_count": 1,
                "errors": ["清理失败"],
            }

            result = await tool._change_status(
                inputs={"task_id": container.id, "status": "completed", "container_reason": "测试"},
                parent_agent_level=1,
            )

        # 容器仍然成功完成
        assert result.success is True

    @pytest.mark.asyncio
    async def test_change_completed_cleanup_exception_does_not_block(self) -> None:
        """清理函数抛异常不影响容器完成。"""
        container, subtasks = _make_container_with_subtasks(
            subtask_workspaces=["/ws/sub_001"],
        )

        svc = _mock_task_service(
            tasks={container.id: container, **{s.id: s for s in subtasks}},
            subtasks_map={container.id: subtasks},
        )

        tool = TaskTool()
        tool._task_service = svc

        with patch.object(svc, "_cleanup_subtask_worktrees", new_callable=AsyncMock) as mock_cleanup:
            # 清理函数抛出未预期异常
            mock_cleanup.side_effect = RuntimeError("unexpected")

            result = await tool._change_status(
                inputs={"task_id": container.id, "status": "completed", "container_reason": "测试"},
                parent_agent_level=1,
            )

        # 容器仍然成功完成
        assert result.success is True

    @pytest.mark.asyncio
    async def test_change_completed_returns_cleanup_info(self) -> None:
        """change(status=completed) 返回清理信息。"""
        container, subtasks = _make_container_with_subtasks(
            subtask_workspaces=["/ws/sub_001", "/ws/sub_002"],
        )

        svc = _mock_task_service(
            tasks={container.id: container, **{s.id: s for s in subtasks}},
            subtasks_map={container.id: subtasks},
        )

        tool = TaskTool()
        tool._task_service = svc

        with patch.object(svc, "_cleanup_subtask_worktrees", new_callable=AsyncMock) as mock_cleanup:
            mock_cleanup.return_value = {
                "total_subtasks": 2,
                "cleaned_count": 2,
                "skipped_count": 0,
                "error_count": 0,
                "errors": [],
            }

            result = await tool._change_status(
                inputs={"task_id": container.id, "status": "completed"},
                parent_agent_level=1,
            )

        assert result.success is True
        # 返回数据中包含清理信息
        assert result.output["cleanup"]["total_subtasks"] == 2
        assert result.output["cleanup"]["cleaned_count"] == 2


def _make_container_with_subtrees_with_errors() -> tuple[TaskModel, list[TaskModel]]:
    """构造一个包含两个子任务的容器（用于测试清理失败场景）。"""
    container = _make_task(
        task_id="container_err",
        status=TaskStatus.PENDING,
        metadata={"task_scope": "container", "workspace": "/ws/container_err"},
    )
    subtasks = [
        _make_task(
            task_id="sub_err_001",
            status=TaskStatus.COMPLETED,
            parent_task_id="container_err",
            metadata={"workspace": "/ws/sub_err_001"},
        ),
        _make_task(
            task_id="sub_err_002",
            status=TaskStatus.COMPLETED,
            parent_task_id="container_err",
            metadata={"workspace": "/ws/sub_err_002"},
        ),
    ]
    return container, subtasks
