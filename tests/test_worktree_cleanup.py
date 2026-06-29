"""容器完成时自动清理子任务 worktree 机制的单元测试。

覆盖场景：
1. 正常清理流程：多子任务 worktree 清理
2. 保护容器自身目录：跳过与容器相同的 workspace
3. 子任务无 worktree：跳过不报错
4. 容器无子任务：优雅跳过
5. 清理失败不阻塞：单个失败不影响后续
6. 分支清理失败：不阻塞流程
7. 安全校验：只删除属于容器子任务的 worktree
8. 日志记录：正确的日志输出

⚠ 重要：get_service_provider / get_isolation_manager / get_workspace_config_root
  均为延迟导入（在函数体内 import），mock 时必须 patch 源模块而非调用模块。
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tasks.types import TaskModel, TaskStatus


# ── 辅助函数 ──────────────────────────────────────────────────


def _make_task(
    task_id: str = "test-task-001",
    *,
    status: TaskStatus = TaskStatus.PENDING,
    workspace: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> TaskModel:
    """创建测试用的 TaskModel 实例。"""
    meta = metadata or {}
    if workspace is not None:
        meta["workspace"] = workspace
    return TaskModel(
        id=task_id,
        title=f"Task-{task_id}",
        status=status,
        metadata=meta,
    )


def _make_container(
    *,
    task_id: str = "container-001",
    workspace: str | None = None,
) -> TaskModel:
    """创建测试用的容器任务。"""
    meta = {"task_scope": "container"}
    if workspace:
        meta["workspace"] = workspace
    return TaskModel(
        id=task_id,
        title=f"Container-{task_id}",
        status=TaskStatus.PENDING,
        metadata=meta,
    )


def _make_subtask(
    task_id: str,
    *,
    workspace: str | None = None,
    status: TaskStatus = TaskStatus.COMPLETED,
) -> TaskModel:
    """创建测试用的子任务。"""
    meta = {}
    if workspace:
        meta["workspace"] = workspace
    return TaskModel(
        id=task_id,
        title=f"Sub-{task_id}",
        status=status,
        parent_task_id="container-001",
        metadata=meta,
    )


def _make_service() -> "TaskService":
    """创建 TaskService 实例（通过 mock 绕过初始化依赖）。"""
    from tasks.service import TaskService

    instance = TaskService.__new__(TaskService)
    instance.task_id = None
    instance._event_bus = None
    instance._storage = MagicMock()
    instance.logger = logging.getLogger("tasks.service")
    return instance


def _make_tool() -> "TaskTool":
    """创建 TaskTool 实例（通过 mock 绕过初始化依赖）。"""
    from tools.builtin.task.tool import TaskTool

    instance = TaskTool.__new__(TaskTool)
    instance.logger = logging.getLogger("tools.builtin.task.tool")
    instance.tool_name = "task"
    return instance


# ── 测试类 ──────────────────────────────────────────────────


class TestCleanupSubtaskWorktrees:
    """_cleanup_subtask_worktrees 方法的单元测试。"""

    @pytest.fixture
    def service(self):
        """创建 TaskService 实例。"""
        return _make_service()

    # ── 场景 1：正常清理流程 ──

    @pytest.mark.asyncio
    async def test_normal_cleanup_multiple_subtasks(self, service):
        """测试：容器有多个子任务，每个都有 worktree，lifecycle 成功清理。"""
        container = _make_container(workspace="/ws/container-001")

        subtasks = [
            _make_subtask("sub-001", workspace="/ws/worktrees/sub-001"),
            _make_subtask("sub-002", workspace="/ws/worktrees/sub-002"),
            _make_subtask("sub-003", workspace="/ws/worktrees/sub-003"),
        ]

        # Mock lifecycle 清理成功
        mock_lifecycle = MagicMock()
        mock_lifecycle.restore_ws_meta.return_value = None
        mock_lifecycle.cleanup_workspace.return_value = {
            "worktree_removed": True,
            "dir_removed": True,
        }

        mock_provider = MagicMock()
        mock_provider.get.return_value = mock_lifecycle

        with patch(
            "infrastructure.service_provider.get_service_provider",
            return_value=mock_provider,
        ):
            result = await service._cleanup_subtask_worktrees(container, subtasks)

        # 验证结果
        assert result["total_subtasks"] == 3
        assert result["cleaned_count"] == 3
        assert result["skipped_count"] == 0
        assert result["error_count"] == 0
        assert result["errors"] == []

        # 验证 lifecycle 被调用了 3 次（每个子任务一次）
        assert mock_lifecycle.restore_ws_meta.call_count == 3
        assert mock_lifecycle.cleanup_workspace.call_count == 3

    # ── 场景 2：保护容器自身目录 ──

    @pytest.mark.asyncio
    async def test_protect_container_own_workspace(self, service):
        """测试：子任务的 workspace 与容器相同时，跳过清理以保护容器工作目录。"""
        # 容器和某个子任务共享相同 workspace
        shared_ws = "/ws/shared-workspace"
        container = _make_container(workspace=shared_ws)

        subtasks = [
            _make_subtask("sub-001", workspace=shared_ws),  # 与容器相同
            _make_subtask("sub-002", workspace="/ws/worktrees/sub-002"),  # 不同
        ]

        # Mock lifecycle 成功清理
        mock_lifecycle = MagicMock()
        mock_lifecycle.cleanup_workspace.return_value = {
            "worktree_removed": True,
            "dir_removed": True,
        }
        mock_provider = MagicMock()
        mock_provider.get.return_value = mock_lifecycle

        with patch(
            "infrastructure.service_provider.get_service_provider",
            return_value=mock_provider,
        ):
            result = await service._cleanup_subtask_worktrees(container, subtasks)

        # 与容器相同 workspace 的子任务被跳过
        assert result["skipped_count"] == 1
        assert result["cleaned_count"] == 1  # 只有 sub-002 被清理
        assert result["error_count"] == 0

    # ── 场景 3：子任务无 worktree ──

    @pytest.mark.asyncio
    async def test_subtask_without_workspace_skipped(self, service):
        """测试：子任务没有 workspace_path 时，跳过该子任务，不报错。"""
        container = _make_container(workspace="/ws/container-001")

        subtasks = [
            _make_subtask("sub-no-ws"),  # 无 workspace
            _make_subtask("sub-has-ws", workspace="/ws/worktrees/sub-has-ws"),
        ]

        mock_lifecycle = MagicMock()
        mock_lifecycle.cleanup_workspace.return_value = {
            "worktree_removed": True,
            "dir_removed": True,
        }
        mock_provider = MagicMock()
        mock_provider.get.return_value = mock_lifecycle

        with patch(
            "infrastructure.service_provider.get_service_provider",
            return_value=mock_provider,
        ):
            result = await service._cleanup_subtask_worktrees(container, subtasks)

        assert result["total_subtasks"] == 2
        assert result["skipped_count"] == 1  # sub-no-ws 被跳过
        assert result["cleaned_count"] == 1  # sub-has-ws 被清理
        assert result["error_count"] == 0

    # ── 场景 4：容器无子任务 ──

    @pytest.mark.asyncio
    async def test_container_with_no_subtasks(self, service):
        """测试：容器没有任何子任务时，清理函数优雅跳过。"""
        container = _make_container(workspace="/ws/container-001")
        subtasks: list[TaskModel] = []

        result = await service._cleanup_subtask_worktrees(container, subtasks)

        assert result["total_subtasks"] == 0
        assert result["cleaned_count"] == 0
        assert result["skipped_count"] == 0
        assert result["error_count"] == 0
        assert result["errors"] == []

    # ── 场景 5：清理失败不阻塞 ──

    @pytest.mark.asyncio
    async def test_cleanup_failure_does_not_block_others(self, service):
        """测试：某个子任务清理失败时，不影响后续子任务的清理。"""
        container = _make_container(workspace="/ws/container-001")

        subtasks = [
            _make_subtask("sub-fail", workspace="/ws/worktrees/sub-fail"),
            _make_subtask("sub-ok", workspace="/ws/worktrees/sub-ok"),
            _make_subtask("sub-ok2", workspace="/ws/worktrees/sub-ok2"),
        ]

        # 第一个子任务清理失败，后两个成功
        def cleanup_side_effect(task_id):
            if task_id == "sub-fail":
                raise RuntimeError("模拟 worktree remove 失败")
            return {"worktree_removed": True, "dir_removed": True}

        mock_lifecycle = MagicMock()
        mock_lifecycle.restore_ws_meta.return_value = None
        mock_lifecycle.cleanup_workspace.side_effect = cleanup_side_effect

        mock_provider = MagicMock()
        mock_provider.get.return_value = mock_lifecycle

        with patch(
            "infrastructure.service_provider.get_service_provider",
            return_value=mock_provider,
        ):
            result = await service._cleanup_subtask_worktrees(container, subtasks)

        # sub-fail 失败，但 sub-ok 和 sub-ok2 成功
        assert result["cleaned_count"] == 2
        assert result["error_count"] == 1
        assert len(result["errors"]) == 1
        assert "sub-fail" in result["errors"][0]

    # ── 场景 6：lifecycle 失败回退到 _cleanup_task_resources ──

    @pytest.mark.asyncio
    async def test_fallback_to_cleanup_task_resources(self, service):
        """测试：lifecycle 不可用时，回退到 _cleanup_task_resources。"""
        container = _make_container(workspace="/ws/container-001")

        subtasks = [
            _make_subtask("sub-001", workspace="/ws/worktrees/sub-001"),
        ]

        # Mock lifecycle 不可用
        mock_provider = MagicMock()
        mock_provider.get.return_value = None  # lifecycle 为 None

        # Mock _cleanup_task_resources 成功
        mock_cleanup_result = {
            "container_destroyed": False,
            "workspace_cleaned": True,
            "errors": [],
        }

        with (
            patch(
                "infrastructure.service_provider.get_service_provider",
                return_value=mock_provider,
            ),
            patch.object(
                service,
                "_cleanup_task_resources",
                return_value=mock_cleanup_result,
                new_callable=AsyncMock,
            ) as mock_cleanup,
        ):
            result = await service._cleanup_subtask_worktrees(container, subtasks)

        assert result["cleaned_count"] == 1
        assert result["error_count"] == 0
        mock_cleanup.assert_called_once_with(
            task_id="sub-001",
            workspace="/ws/worktrees/sub-001",
        )

    @pytest.mark.asyncio
    async def test_lifecycle_exception_falls_back(self, service):
        """测试：lifecycle 抛异常时，回退到 _cleanup_task_resources。"""
        container = _make_container(workspace="/ws/container-001")

        subtasks = [
            _make_subtask("sub-001", workspace="/ws/worktrees/sub-001"),
        ]

        # Mock provider 抛异常
        mock_provider = MagicMock()
        mock_provider.get.side_effect = RuntimeError("provider 不存在")

        mock_cleanup_result = {
            "container_destroyed": False,
            "workspace_cleaned": True,
            "errors": [],
        }

        with (
            patch(
                "infrastructure.service_provider.get_service_provider",
                return_value=mock_provider,
            ),
            patch.object(
                service,
                "_cleanup_task_resources",
                return_value=mock_cleanup_result,
                new_callable=AsyncMock,
            ),
        ):
            result = await service._cleanup_subtask_worktrees(container, subtasks)

        assert result["cleaned_count"] == 1
        assert result["error_count"] == 0

    @pytest.mark.asyncio
    async def test_both_lifecycle_and_fallback_fail(self, service):
        """测试：lifecycle 和 fallback 都失败时，记录错误但不抛异常。"""
        container = _make_container(workspace="/ws/container-001")

        subtasks = [
            _make_subtask("sub-001", workspace="/ws/worktrees/sub-001"),
        ]

        mock_provider = MagicMock()
        mock_provider.get.return_value = None

        mock_cleanup_result = {
            "container_destroyed": False,
            "workspace_cleaned": False,
            "errors": ["git worktree remove 失败: some error"],
        }

        with (
            patch(
                "infrastructure.service_provider.get_service_provider",
                return_value=mock_provider,
            ),
            patch.object(
                service,
                "_cleanup_task_resources",
                return_value=mock_cleanup_result,
                new_callable=AsyncMock,
            ),
        ):
            result = await service._cleanup_subtask_worktrees(container, subtasks)

        assert result["cleaned_count"] == 0
        assert result["error_count"] == 1
        assert len(result["errors"]) == 1

    # ── 场景 7：安全校验 ──

    @pytest.mark.asyncio
    async def test_only_cleans_belonging_subtask_worktrees(self, service):
        """测试：清理函数只删除属于该容器子任务的 worktree，不误删其他路径。"""
        container = _make_container(workspace="/ws/container-001")

        subtasks = [
            _make_subtask("sub-001", workspace="/ws/worktrees/sub-001"),
            _make_subtask("sub-002", workspace="/ws/worktrees/sub-002"),
        ]

        cleaned_task_ids: list[str] = []

        def cleanup_side_effect(task_id):
            cleaned_task_ids.append(task_id)
            return {"worktree_removed": True, "dir_removed": True}

        mock_lifecycle = MagicMock()
        mock_lifecycle.restore_ws_meta.return_value = None
        mock_lifecycle.cleanup_workspace.side_effect = cleanup_side_effect

        mock_provider = MagicMock()
        mock_provider.get.return_value = mock_lifecycle

        with patch(
            "infrastructure.service_provider.get_service_provider",
            return_value=mock_provider,
        ):
            result = await service._cleanup_subtask_worktrees(container, subtasks)

        # 确认只清理了容器子任务的 worktree
        assert set(cleaned_task_ids) == {"sub-001", "sub-002"}
        assert result["cleaned_count"] == 2

    @pytest.mark.asyncio
    async def test_container_workspace_same_path_protection(self, service):
        """测试：容器 workspace 路径解析保护，防止路径误删。"""
        # 使用相对路径和绝对路径相同的情况
        container = _make_container(workspace="/data/workspaces/container-ws")

        # 子任务 1 的 workspace 指向容器目录
        # 子任务 2 的 workspace 完全不同
        subtasks = [
            _make_subtask("sub-protected", workspace="/data/workspaces/container-ws"),
            _make_subtask("sub-normal", workspace="/data/workspaces/other-ws"),
        ]

        mock_lifecycle = MagicMock()
        mock_lifecycle.cleanup_workspace.return_value = {
            "worktree_removed": True,
            "dir_removed": True,
        }
        mock_provider = MagicMock()
        mock_provider.get.return_value = mock_lifecycle

        with patch(
            "infrastructure.service_provider.get_service_provider",
            return_value=mock_provider,
        ):
            result = await service._cleanup_subtask_worktrees(container, subtasks)

        # sub-protected 被跳过，sub-normal 被清理
        assert result["skipped_count"] == 1
        assert result["cleaned_count"] == 1

    # ── 场景 8：日志记录 ──

    @pytest.mark.asyncio
    async def test_logging_on_normal_cleanup(self, service, caplog):
        """测试：正常清理过程中有正确的日志输出。"""
        container = _make_container(task_id="cnt-001", workspace="/ws/cnt-001")
        subtasks = [
            _make_subtask("sub-001", workspace="/ws/worktrees/sub-001"),
        ]

        mock_lifecycle = MagicMock()
        mock_lifecycle.cleanup_workspace.return_value = {
            "worktree_removed": True,
            "dir_removed": True,
        }
        mock_provider = MagicMock()
        mock_provider.get.return_value = mock_lifecycle

        with (
            patch(
                "infrastructure.service_provider.get_service_provider",
                return_value=mock_provider,
            ),
            caplog.at_level(logging.INFO, logger="tasks.service"),
        ):
            result = await service._cleanup_subtask_worktrees(container, subtasks)

        assert result["cleaned_count"] == 1

        # 验证关键日志信息
        log_messages = caplog.text
        assert "cnt-001" in log_messages  # 容器 ID

    @pytest.mark.asyncio
    async def test_logging_on_empty_subtasks(self, service, caplog):
        """测试：无子任务时有跳过日志。"""
        container = _make_container(task_id="cnt-empty", workspace="/ws/cnt-empty")
        subtasks: list[TaskModel] = []

        with caplog.at_level(logging.INFO, logger="tasks.service"):
            result = await service._cleanup_subtask_worktrees(container, subtasks)

        assert result["total_subtasks"] == 0
        log_messages = caplog.text
        assert "无子任务" in log_messages or "跳过" in log_messages

    @pytest.mark.asyncio
    async def test_logging_on_cleanup_error(self, service, caplog):
        """测试：清理失败时有警告日志。"""
        container = _make_container(task_id="cnt-err", workspace="/ws/cnt-err")
        subtasks = [
            _make_subtask("sub-err", workspace="/ws/worktrees/sub-err"),
        ]

        # lifecycle 抛异常，fallback 也失败
        mock_provider = MagicMock()
        mock_provider.get.side_effect = RuntimeError("no provider")

        with (
            patch(
                "infrastructure.service_provider.get_service_provider",
                return_value=mock_provider,
            ),
            patch.object(
                service,
                "_cleanup_task_resources",
                return_value={
                    "container_destroyed": False,
                    "workspace_cleaned": False,
                    "errors": ["worktree remove 失败"],
                },
                new_callable=AsyncMock,
            ),
            caplog.at_level(logging.WARNING, logger="tasks.service"),
        ):
            result = await service._cleanup_subtask_worktrees(container, subtasks)

        assert result["error_count"] == 1
        assert len(result["errors"]) == 1

    @pytest.mark.asyncio
    async def test_logging_on_skip_no_workspace(self, service, caplog):
        """测试：跳过无 workspace 的子任务时有 debug 日志。"""
        container = _make_container(task_id="cnt-skip", workspace="/ws/cnt-skip")
        subtasks = [
            _make_subtask("sub-no-ws"),  # 无 workspace
        ]

        with caplog.at_level(logging.DEBUG, logger="tasks.service"):
            result = await service._cleanup_subtask_worktrees(container, subtasks)

        assert result["skipped_count"] == 1
        log_messages = caplog.text
        assert "无 workspace" in log_messages or "sub-no-ws" in log_messages

    @pytest.mark.asyncio
    async def test_logging_final_summary(self, service, caplog):
        """测试：清理完成后有汇总日志。"""
        container = _make_container(task_id="cnt-summary", workspace="/ws/cnt-summary")
        subtasks = [
            _make_subtask("sub-001", workspace="/ws/wt-001"),
            _make_subtask("sub-002", workspace="/ws/wt-002"),
        ]

        mock_lifecycle = MagicMock()
        mock_lifecycle.cleanup_workspace.return_value = {
            "worktree_removed": True,
            "dir_removed": True,
        }
        mock_provider = MagicMock()
        mock_provider.get.return_value = mock_lifecycle

        with (
            patch(
                "infrastructure.service_provider.get_service_provider",
                return_value=mock_provider,
            ),
            caplog.at_level(logging.INFO, logger="tasks.service"),
        ):
            result = await service._cleanup_subtask_worktrees(container, subtasks)

        assert result["cleaned_count"] == 2
        log_messages = caplog.text
        # 验证汇总日志包含总计/已清理/跳过/失败
        assert "清理完成" in log_messages or "总计" in log_messages


class TestCleanupSubtaskWorktreesEdgeCases:
    """边界和异常场景的补充测试。"""

    @pytest.fixture
    def service(self):
        """创建 TaskService 实例。"""
        return _make_service()

    @pytest.mark.asyncio
    async def test_container_without_workspace_metadata(self, service):
        """测试：容器自身没有 workspace 元数据时，安全保护逻辑不崩溃。"""
        container = _make_container()  # 无 workspace
        assert "workspace" not in (container.metadata or {})

        subtasks = [
            _make_subtask("sub-001", workspace="/ws/worktrees/sub-001"),
        ]

        mock_lifecycle = MagicMock()
        mock_lifecycle.cleanup_workspace.return_value = {
            "worktree_removed": True,
            "dir_removed": True,
        }
        mock_provider = MagicMock()
        mock_provider.get.return_value = mock_lifecycle

        with patch(
            "infrastructure.service_provider.get_service_provider",
            return_value=mock_provider,
        ):
            result = await service._cleanup_subtask_worktrees(container, subtasks)

        # 容器无 workspace → 保护逻辑跳过，子任务正常清理
        assert result["cleaned_count"] == 1
        assert result["error_count"] == 0

    @pytest.mark.asyncio
    async def test_subtask_with_empty_string_workspace(self, service):
        """测试：子任务的 workspace 为空字符串时，视为无 workspace 跳过。"""
        container = _make_container(workspace="/ws/cnt-001")

        subtasks = [
            _make_subtask("sub-empty", workspace=""),  # 空字符串
            _make_subtask("sub-normal", workspace="/ws/worktrees/sub-normal"),
        ]

        mock_lifecycle = MagicMock()
        mock_lifecycle.cleanup_workspace.return_value = {
            "worktree_removed": True,
            "dir_removed": True,
        }
        mock_provider = MagicMock()
        mock_provider.get.return_value = mock_lifecycle

        with patch(
            "infrastructure.service_provider.get_service_provider",
            return_value=mock_provider,
        ):
            result = await service._cleanup_subtask_worktrees(container, subtasks)

        # 空字符串 workspace 被跳过
        assert result["skipped_count"] == 1
        assert result["cleaned_count"] == 1

    @pytest.mark.asyncio
    async def test_subtask_without_metadata(self, service):
        """测试：子任务无 metadata 字段时不崩溃。"""
        container = _make_container(workspace="/ws/cnt-001")

        # 创建一个 metadata 为空字典的子任务
        subtask_no_meta = TaskModel(
            id="sub-no-meta",
            title="Sub no meta",
            status=TaskStatus.COMPLETED,
        )
        subtask_no_meta.metadata = {}  # 确保 metadata 是空字典

        subtasks = [subtask_no_meta]

        with patch(
            "infrastructure.service_provider.get_service_provider",
            return_value=MagicMock(),
        ):
            result = await service._cleanup_subtask_worktrees(container, subtasks)

        # 无 workspace → 跳过
        assert result["skipped_count"] == 1
        assert result["error_count"] == 0

    @pytest.mark.asyncio
    async def test_outer_exception_caught(self, service):
        """测试：外层 try-except 捕获所有异常，不泄露到调用方。"""
        container = _make_container(workspace="/ws/cnt-001")

        subtasks = [
            _make_subtask("sub-001", workspace="/ws/worktrees/sub-001"),
        ]

        # Mock lifecycle 抛异常，外层 except 捕获
        mock_lifecycle = MagicMock()
        mock_lifecycle.restore_ws_meta.side_effect = Exception("unexpected error")
        mock_lifecycle.cleanup_workspace.side_effect = Exception("unexpected error")

        mock_provider = MagicMock()
        mock_provider.get.return_value = mock_lifecycle

        # 同时 _cleanup_task_resources 也抛异常
        with (
            patch(
                "infrastructure.service_provider.get_service_provider",
                return_value=mock_provider,
            ),
            patch.object(
                service,
                "_cleanup_task_resources",
                new_callable=AsyncMock,
                side_effect=RuntimeError("everything broken"),
            ),
        ):
            result = await service._cleanup_subtask_worktrees(container, subtasks)

        # 异常被捕获，记录到 error_count
        assert result["error_count"] == 1
        assert len(result["errors"]) == 1
        assert "sub-001" in result["errors"][0]

    @pytest.mark.asyncio
    async def test_mixed_subtasks_all_scenarios(self, service):
        """测试：混合场景——无 workspace / 与容器相同 / 正常 / 失败。"""
        shared_ws = "/ws/shared"
        container = _make_container(workspace=shared_ws)

        subtasks = [
            _make_subtask("sub-no-ws"),  # 无 workspace → 跳过
            _make_subtask("sub-same-ws", workspace=shared_ws),  # 与容器相同 → 跳过
            _make_subtask("sub-ok", workspace="/ws/worktrees/sub-ok"),  # 正常清理
            _make_subtask("sub-fail", workspace="/ws/worktrees/sub-fail"),  # 清理失败
        ]

        def cleanup_side_effect(task_id):
            if task_id == "sub-fail":
                raise RuntimeError("模拟清理失败")
            return {"worktree_removed": True, "dir_removed": True}

        mock_lifecycle = MagicMock()
        mock_lifecycle.restore_ws_meta.return_value = None
        mock_lifecycle.cleanup_workspace.side_effect = cleanup_side_effect

        mock_provider = MagicMock()
        mock_provider.get.return_value = mock_lifecycle

        with patch(
            "infrastructure.service_provider.get_service_provider",
            return_value=mock_provider,
        ):
            result = await service._cleanup_subtask_worktrees(container, subtasks)

        assert result["total_subtasks"] == 4
        assert result["skipped_count"] == 2  # 无 ws + 与容器相同
        assert result["cleaned_count"] == 1  # sub-ok
        assert result["error_count"] == 1  # sub-fail

    @pytest.mark.asyncio
    async def test_result_dict_structure(self, service):
        """测试：返回结果字典结构完整且字段类型正确。"""
        container = _make_container(workspace="/ws/cnt-001")
        subtasks = [
            _make_subtask("sub-001", workspace="/ws/worktrees/sub-001"),
        ]

        mock_lifecycle = MagicMock()
        mock_lifecycle.cleanup_workspace.return_value = {
            "worktree_removed": True,
            "dir_removed": True,
        }
        mock_provider = MagicMock()
        mock_provider.get.return_value = mock_lifecycle

        with patch(
            "infrastructure.service_provider.get_service_provider",
            return_value=mock_provider,
        ):
            result = await service._cleanup_subtask_worktrees(container, subtasks)

        # 验证返回结构
        assert isinstance(result, dict)
        assert "total_subtasks" in result
        assert "cleaned_count" in result
        assert "skipped_count" in result
        assert "error_count" in result
        assert "errors" in result

        assert isinstance(result["total_subtasks"], int)
        assert isinstance(result["cleaned_count"], int)
        assert isinstance(result["skipped_count"], int)
        assert isinstance(result["error_count"], int)
        assert isinstance(result["errors"], list)


class TestRemoveWorktree:
    """_remove_worktree 方法的单元测试。"""

    @pytest.fixture
    def service(self):
        """创建 TaskService 实例。"""
        return _make_service()

    def test_remove_worktree_success(self, service, tmp_path):
        """测试：正常读取 .git 文件并执行 git worktree remove + 删除关联分支。

        BUG-FIX-fix_20260628_remove_worktree_branch_leak 配套:
        _remove_worktree 现在会先反查 worktree 当前分支，remove 成功后删分支。
        本测试验证完整三步调用：rev-parse 反查 → worktree remove → branch -D。
        """
        # 模拟 worktree 目录结构
        ws_dir = tmp_path / "worktree-ws"
        ws_dir.mkdir()

        # 创建 .git 文件指向主仓库
        main_repo = tmp_path / "main-repo"
        main_repo.mkdir()
        git_worktrees_dir = main_repo / ".git" / "worktrees" / "wt-001"
        git_worktrees_dir.mkdir(parents=True)

        git_file = ws_dir / ".git"
        git_file.write_text(f"gitdir: {git_worktrees_dir}", encoding="utf-8")

        cleanup_results: dict[str, Any] = {"errors": []}

        with patch("subprocess.run") as mock_run:
            # rev-parse 反查返回真实分支名 → 触发 branch -D
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="task/abc123\n", stderr=""),  # rev-parse
                MagicMock(returncode=0, stdout="", stderr=""),               # worktree remove
                MagicMock(returncode=0, stdout="", stderr=""),               # branch -D
            ]
            service._remove_worktree(ws_dir, cleanup_results)

        # 验证三次调用：反查分支 → worktree remove → 删分支
        assert mock_run.call_count == 3
        cmds = [c.args[0] for c in mock_run.call_args_list]
        assert cmds[0] == ["git", "rev-parse", "--abbrev-ref", "HEAD"]
        assert "worktree" in cmds[1] and "remove" in cmds[1] and str(ws_dir) in cmds[1]
        assert cmds[2] == ["git", "branch", "-D", "task/abc123"]
        assert cleanup_results["workspace_cleaned"] is True

    def test_remove_worktree_detached_skips_branch_delete(self, service, tmp_path):
        """测试：detach 状态(无分支名)时只 remove worktree，不调 branch -D。"""
        ws_dir = tmp_path / "worktree-ws"
        ws_dir.mkdir()
        main_repo = tmp_path / "main-repo"
        main_repo.mkdir()
        git_worktrees_dir = main_repo / ".git" / "worktrees" / "wt-001"
        git_worktrees_dir.mkdir(parents=True)
        git_file = ws_dir / ".git"
        git_file.write_text(f"gitdir: {git_worktrees_dir}", encoding="utf-8")

        cleanup_results: dict[str, Any] = {"errors": []}

        with patch("subprocess.run") as mock_run:
            # rev-parse 返回 HEAD = detach 状态，无分支可删
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="HEAD\n", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),
            ]
            service._remove_worktree(ws_dir, cleanup_results)

        assert mock_run.call_count == 2  # 反查 + remove，无 branch -D
        cmds = [c.args[0] for c in mock_run.call_args_list]
        assert "worktree" in cmds[1] and "remove" in cmds[1]
        assert cleanup_results["workspace_cleaned"] is True

    def test_remove_worktree_git_failure(self, service, tmp_path):
        """测试：git worktree remove 失败时记录错误但不抛异常。"""
        ws_dir = tmp_path / "worktree-ws"
        ws_dir.mkdir()

        git_file = ws_dir / ".git"
        git_file.write_text("gitdir: /some/path", encoding="utf-8")

        cleanup_results: dict[str, Any] = {"errors": []}

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                1, "git", stderr="worktree remove failed"
            )
            service._remove_worktree(ws_dir, cleanup_results)

        # workspace_cleaned 未设置
        assert "workspace_cleaned" not in cleanup_results or not cleanup_results.get("workspace_cleaned")
        assert len(cleanup_results["errors"]) > 0
        assert any("worktree remove 失败" in e for e in cleanup_results["errors"])

    def test_remove_worktree_gitfile_not_found(self, service, tmp_path):
        """测试：.git 文件不存在时的异常处理。"""
        ws_dir = tmp_path / "worktree-ws"
        ws_dir.mkdir()
        # 不创建 .git 文件

        cleanup_results: dict[str, Any] = {"errors": []}

        # 读取不存在的文件会抛异常，被 _remove_worktree 捕获
        service._remove_worktree(ws_dir, cleanup_results)

        assert len(cleanup_results["errors"]) > 0
        assert any("清理 worktree 失败" in e for e in cleanup_results["errors"])

    def test_remove_worktree_plain_gitdir(self, service, tmp_path):
        """测试：.git 不是 gitdir 格式时，使用 parent 作为 main_repo。"""
        ws_dir = tmp_path / "worktree-ws"
        ws_dir.mkdir()

        # .git 文件内容不是 gitdir 格式
        git_file = ws_dir / ".git"
        git_file.write_text("some other content", encoding="utf-8")

        cleanup_results: dict[str, Any] = {"errors": []}

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            service._remove_worktree(ws_dir, cleanup_results)

        # 使用 workspace 的 parent 作为 main_repo
        call_args = mock_run.call_args
        assert call_args[1].get("cwd") == str(ws_dir.parent)
        assert cleanup_results["workspace_cleaned"] is True


class TestCleanupTaskResources:
    """_cleanup_task_resources 方法的单元测试。"""

    @pytest.fixture
    def service(self):
        """创建 TaskService 实例。"""
        return _make_service()

    @pytest.mark.asyncio
    async def test_cleanup_with_no_workspace(self, service):
        """测试：无 workspace 时只清理隔离环境。"""
        mock_manager = AsyncMock()
        mock_manager.destroy_environment.return_value = False

        mock_lifecycle = MagicMock()
        mock_lifecycle.cleanup_workspace.return_value = None

        mock_provider = MagicMock()
        mock_provider.get.return_value = mock_lifecycle

        with (
            patch("isolation.manager.get_isolation_manager", return_value=mock_manager),
            patch("infrastructure.service_provider.get_service_provider", return_value=mock_provider),
        ):
            result = await service._cleanup_task_resources("task-001", workspace=None)

        assert result["container_destroyed"] is False
        assert result["workspace_cleaned"] is False

    @pytest.mark.asyncio
    async def test_cleanup_workspace_not_exists(self, service):
        """测试：workspace 路径不存在时，优雅跳过。"""
        mock_manager = AsyncMock()
        mock_manager.destroy_environment.return_value = False

        # lifecycle 不可用
        mock_provider = MagicMock()
        mock_provider.get.return_value = None

        with (
            patch("isolation.manager.get_isolation_manager", return_value=mock_manager),
            patch("infrastructure.service_provider.get_service_provider", return_value=mock_provider),
            patch("isolation.workspace.get_workspace_config_root", return_value="/ws-root"),
        ):
            result = await service._cleanup_task_resources(
                "task-001", workspace="/nonexistent/path"
            )

        # workspace 不存在，workspace_cleaned 保持 False
        assert result["workspace_cleaned"] is False

    @pytest.mark.asyncio
    async def test_cleanup_result_dict_structure(self, service):
        """测试：返回结构包含必要字段。"""
        mock_manager = AsyncMock()
        mock_manager.destroy_environment.return_value = False

        mock_provider = MagicMock()
        mock_provider.get.return_value = None

        with (
            patch("isolation.manager.get_isolation_manager", return_value=mock_manager),
            patch("infrastructure.service_provider.get_service_provider", return_value=mock_provider),
        ):
            result = await service._cleanup_task_resources("task-001", workspace=None)

        assert "container_destroyed" in result
        assert "workspace_cleaned" in result
        assert "errors" in result
        assert isinstance(result["errors"], list)


class TestCompleteContainerIntegration:
    """_change_status(status=completed) 调用清理的集成测试。

    change action 替代了旧的 complete/fail，仅对容器任务生效。
    status=completed 时调用 _cleanup_subtask_worktrees。
    """

    @pytest.fixture
    def tool(self):
        """创建 TaskTool 实例。"""
        return _make_tool()

    @pytest.mark.asyncio
    async def test_change_completed_calls_cleanup_before_transition(self, tool):
        """测试：change(status=completed) 时在状态转换之前调用 worktree 清理。"""
        container = _make_container(task_id="cnt-001", workspace="/ws/cnt-001")
        subtasks = [
            _make_subtask("sub-001", workspace="/ws/worktrees/sub-001"),
        ]

        mock_service = MagicMock()
        mock_service.get_task.return_value = container
        mock_service.list_subtasks.return_value = subtasks
        mock_service.force_transition = AsyncMock()
        mock_service.save_task = AsyncMock()

        # Mock 清理函数
        cleanup_result = {
            "total_subtasks": 1,
            "cleaned_count": 1,
            "skipped_count": 0,
            "error_count": 0,
            "errors": [],
        }

        with (
            patch.object(tool, "_get_task_service", return_value=mock_service),
            patch.object(
                mock_service,
                "_cleanup_subtask_worktrees",
                return_value=cleanup_result,
                new_callable=AsyncMock,
            ) as mock_cleanup,
        ):
            result = await tool._change_status(
                {"task_id": "cnt-001", "status": "completed"}, parent_agent_level=1
            )

        assert result.success is True
        # 验证清理被调用
        mock_cleanup.assert_called_once_with(container, subtasks)
        # 验证状态转换被调用
        mock_service.force_transition.assert_called_once()

    @pytest.mark.asyncio
    async def test_change_completed_cleanup_failure_non_fatal(self, tool):
        """测试：清理失败不阻塞容器完成。"""
        container = _make_container(task_id="cnt-002", workspace="/ws/cnt-002")
        subtasks = [
            _make_subtask("sub-001", workspace="/ws/worktrees/sub-001"),
        ]

        mock_service = MagicMock()
        mock_service.get_task.return_value = container
        mock_service.list_subtasks.return_value = subtasks
        mock_service.force_transition = AsyncMock()
        mock_service.save_task = AsyncMock()

        with (
            patch.object(tool, "_get_task_service", return_value=mock_service),
            patch.object(
                mock_service,
                "_cleanup_subtask_worktrees",
                side_effect=RuntimeError("清理严重异常"),
                new_callable=AsyncMock,
            ),
        ):
            result = await tool._change_status(
                {"task_id": "cnt-002", "status": "completed"}, parent_agent_level=1
            )

        # 即使清理失败，状态转换仍被调用
        assert result.success is True
        mock_service.force_transition.assert_called_once()

    @pytest.mark.asyncio
    async def test_change_permission_denied(self, tool):
        """测试：非 L1 Agent 不能执行容器状态变更。"""
        result = await tool._change_status(
            {"task_id": "cnt-001", "status": "completed"}, parent_agent_level=2
        )

        # L2 无权限
        assert result.success is False
        assert "PERMISSION_DENIED" in (result.error_code or "")

    @pytest.mark.asyncio
    async def test_change_missing_task_id(self, tool):
        """测试：缺少 task_id 参数时报错。"""
        result = await tool._change_status(
            {"status": "completed"}, parent_agent_level=1
        )

        assert result.success is False
        assert "MISSING_TASK_ID" in (result.error_code or "")

    @pytest.mark.asyncio
    async def test_change_missing_status(self, tool):
        """测试：change 操作缺少 status 参数时报错。"""
        container = _make_container(task_id="cnt-006")
        mock_service = MagicMock()
        mock_service.get_task.return_value = container

        with patch.object(tool, "_get_task_service", return_value=mock_service):
            result = await tool._change_status(
                {"task_id": "cnt-006"}, parent_agent_level=1
            )

        assert result.success is False
        assert "MISSING_STATUS" in (result.error_code or "")

    @pytest.mark.asyncio
    async def test_change_task_not_found(self, tool):
        """测试：任务不存在时报错。"""
        mock_service = MagicMock()
        mock_service.get_task.return_value = None

        with patch.object(tool, "_get_task_service", return_value=mock_service):
            result = await tool._change_status(
                {"task_id": "nonexistent", "status": "completed"}, parent_agent_level=1
            )

        assert result.success is False
        assert "TASK_NOT_FOUND" in (result.error_code or "")

    @pytest.mark.asyncio
    async def test_change_rejects_non_container(self, tool):
        """测试：非容器任务（task_scope != container）不能使用 change 操作。

        修复回归：此前用 list_subtasks 是否为空判断容器，空容器被误判。
        现改用 task_scope 字段判断。本测试验证：非容器任务（即使有子任务）也被拒绝。
        """
        # 非容器任务：task_scope 不是 container（但有子任务，模拟旧 bug 场景）
        non_container = _make_container(task_id="cnt-003")
        non_container.metadata = {"task_scope": "non_container"}  # 改为非容器
        mock_service = MagicMock()
        mock_service.get_task.return_value = non_container
        mock_service.list_subtasks.return_value = [_make_subtask("sub-001")]  # 有子任务

        with patch.object(tool, "_get_task_service", return_value=mock_service):
            result = await tool._change_status(
                {"task_id": "cnt-003", "status": "completed"}, parent_agent_level=1
            )

        assert result.success is False
        assert "NOT_A_CONTAINER" in (result.error_code or "")

    @pytest.mark.asyncio
    async def test_change_empty_container_ok(self, tool):
        """测试：空容器（无子任务但 task_scope=container）可正常 change。

        这是核心 bug 修复验证：旧逻辑用 list_subtasks 判断容器，空容器被误报
        NOT_A_CONTAINER。新逻辑用 task_scope 判断，空容器能正常变更状态。
        """
        container = _make_container(task_id="cnt-empty")  # task_scope=container
        mock_service = MagicMock()
        mock_service.get_task.return_value = container
        mock_service.list_subtasks.return_value = []  # 无子任务
        mock_service.force_transition = AsyncMock()
        mock_service.save_task = AsyncMock()

        with patch.object(tool, "_get_task_service", return_value=mock_service):
            result = await tool._change_status(
                {"task_id": "cnt-empty", "status": "failed"}, parent_agent_level=1
            )

        assert result.success is True
        mock_service.force_transition.assert_called_once()

    @pytest.mark.asyncio
    async def test_change_any_status_allowed(self, tool):
        """测试：容器可从任意状态变更到任意状态（不再有 PENDING/RUNNING 白名单）。

        取代旧的 test_complete_container_wrong_status（原断言 INVALID_STATUS）。
        现在已 completed 的容器仍能再次 change。
        """
        container = _make_container(task_id="cnt-004")
        container.status = TaskStatus.COMPLETED  # 已经完成
        subtasks = [_make_subtask("sub-001")]

        mock_service = MagicMock()
        mock_service.get_task.return_value = container
        mock_service.list_subtasks.return_value = subtasks
        mock_service.force_transition = AsyncMock()
        mock_service.save_task = AsyncMock()

        with (
            patch.object(tool, "_get_task_service", return_value=mock_service),
            patch.object(
                mock_service,
                "_cleanup_subtask_worktrees",
                return_value={
                    "total_subtasks": 1, "cleaned_count": 0, "skipped_count": 0,
                    "error_count": 0, "errors": [],
                },
                new_callable=AsyncMock,
            ),
        ):
            result = await tool._change_status(
                {"task_id": "cnt-004", "status": "completed"}, parent_agent_level=1
            )

        # 已完成容器再次 change 成功（不再拒绝）
        assert result.success is True

    @pytest.mark.asyncio
    async def test_change_completed_cleanup_info_in_result(self, tool):
        """测试：容器完成结果中包含清理信息。"""
        container = _make_container(task_id="cnt-005", workspace="/ws/cnt-005")
        subtasks = [
            _make_subtask("sub-001", workspace="/ws/worktrees/sub-001"),
        ]

        mock_service = MagicMock()
        mock_service.get_task.return_value = container
        mock_service.list_subtasks.return_value = subtasks
        mock_service.force_transition = AsyncMock()
        mock_service.save_task = AsyncMock()

        cleanup_info = {
            "total_subtasks": 1,
            "cleaned_count": 1,
            "skipped_count": 0,
            "error_count": 0,
            "errors": [],
        }

        with (
            patch.object(tool, "_get_task_service", return_value=mock_service),
            patch.object(
                mock_service,
                "_cleanup_subtask_worktrees",
                return_value=cleanup_info,
                new_callable=AsyncMock,
            ),
        ):
            result = await tool._change_status(
                {"task_id": "cnt-005", "status": "completed"}, parent_agent_level=1
            )

        assert result.success is True
        # 结果中包含清理信息
        assert "cleanup" in result.output
        assert result.output["cleanup"]["cleaned_count"] == 1
