"""hard_delete_task 对容器子任务 worktree 清理的单元测试。

BUG 背景：
  hard_delete_task 原先用 `skip_workspace = is_child_of_container` 一刀切跳过
  容器子任务的 workspace 清理。但容器子任务在 worktree 模式下拥有独立 worktree
  （container_path.parent / _safe_ws_name(...)，分支 task/<id>），并不共享容器
  空间。一刀切跳过导致 worktree 目录与 task/<id> 分支永久残留。

修复后行为：
  - 容器子任务 + worktree 模式 → skip_workspace=False，走 _cleanup_task_resources
  - 容器子任务 + shared/plain/无 ws_meta → skip_workspace=True，保护共享空间
  - 非容器子任务 → skip_workspace=False（行为不变）

测试不依赖真实 git/docker，通过 patch _cleanup_task_resources 验证调用与否，
通过 _has_independent_worktree 直接验证判断逻辑。
"""

from __future__ import annotations

import tempfile
from unittest.mock import AsyncMock, patch

import pytest

from tasks.service import TaskService
from tasks.types import TaskStatus


# ── 辅助 ──────────────────────────────────────────────


def _make_service() -> TaskService:
    """创建使用临时目录的 TaskService 实例。"""
    tmp_dir = tempfile.mkdtemp(prefix="test_hard_delete_wt_")
    return TaskService(data_dir=tmp_dir)


def _ws_meta(mode: str, path: str = "/ws/sub_worktree") -> dict:
    """构造 ws_meta 字典。"""
    return {"mode": mode, "path": path, "branch": "task/sub_001", "project_root": "/ws/container_001"}


# ── _has_independent_worktree 单元测试 ──────────────────


class TestHasIndependentWorktree:
    """_has_independent_worktree 判断逻辑测试。"""

    def setup_method(self) -> None:
        self.svc = _make_service()

    @pytest.mark.asyncio
    async def test_worktree_mode_is_independent(self) -> None:
        """ws_meta.mode == 'worktree' 视为独立 worktree。"""
        task = await self.svc.create_task(
            title="子任务",
            metadata={"ws_meta": _ws_meta("worktree")},
        )
        assert self.svc._has_independent_worktree(task) is True

    @pytest.mark.asyncio
    async def test_shared_mode_not_independent(self) -> None:
        """shared 模式（共享父空间）非独立。"""
        task = await self.svc.create_task(
            title="子任务",
            metadata={"ws_meta": _ws_meta("shared")},
        )
        assert self.svc._has_independent_worktree(task) is False

    @pytest.mark.asyncio
    async def test_plain_mode_not_independent(self) -> None:
        """plain 模式非独立。"""
        task = await self.svc.create_task(
            title="子任务",
            metadata={"ws_meta": _ws_meta("plain")},
        )
        assert self.svc._has_independent_worktree(task) is False

    @pytest.mark.asyncio
    async def test_no_ws_meta_not_independent(self) -> None:
        """无 ws_meta 视为共享（保守跳过，避免误删）。"""
        task = await self.svc.create_task(title="子任务", metadata={})
        assert self.svc._has_independent_worktree(task) is False

    @pytest.mark.asyncio
    async def test_none_metadata_not_independent(self) -> None:
        """metadata 为 None 时安全返回 False。"""
        task = await self.svc.create_task(title="子任务")
        task.metadata = None
        assert self.svc._has_independent_worktree(task) is False

    @pytest.mark.asyncio
    async def test_malformed_ws_meta_not_independent(self) -> None:
        """ws_meta 非 dict（脏数据）安全返回 False。"""
        task = await self.svc.create_task(
            title="子任务",
            metadata={"ws_meta": "not-a-dict"},
        )
        assert self.svc._has_independent_worktree(task) is False


# ── hard_delete_task 行为测试 ──────────────────


class TestHardDeleteContainerSubtaskWorktree:
    """hard_delete_task 对容器子任务 worktree 清理的行为测试。

    通过 patch _cleanup_task_resources 验证是否调用（即 skip_workspace 取值），
    不依赖真实 git/docker。
    """

    @pytest.mark.asyncio
    async def test_container_subtask_worktree_mode_is_cleaned(self) -> None:
        """容器子任务 worktree 模式：skip_workspace=False，调用 _cleanup_task_resources。

        这是本次 bug 的修复目标——独立 worktree 不再被跳过。
        """
        svc = _make_service()
        container = await svc.create_task(
            title="容器",
            metadata={"task_scope": "container"},
        )
        subtask = await svc.create_task(
            title="子任务",
            parent_task_id=container.id,
            metadata={"ws_meta": _ws_meta("worktree")},
        )

        with (
            patch.object(
                svc, "_cleanup_task_resources", new_callable=AsyncMock
            ) as mock_cleanup,
            patch.object(svc, "_cancel_pipeline_recursive"),
            patch("infrastructure.service_provider.get_service_provider", side_effect=RuntimeError("no provider")),
        ):
            mock_cleanup.return_value = {"workspace_cleaned": True, "errors": []}
            await svc.hard_delete_task(subtask.id)

        mock_cleanup.assert_called_once()
        # 容器子任务 worktree 模式下，workspace 参数应来自 task.metadata.get("workspace")
        # （此处为 None，但 _cleanup_task_resources 内部走 lifecycle 路径从 ws_meta 取 path）
        _called_args, called_kwargs = mock_cleanup.call_args
        assert called_kwargs["task_id"] == subtask.id

    @pytest.mark.asyncio
    async def test_container_subtask_shared_mode_skipped(self) -> None:
        """容器子任务 shared 模式：skip_workspace=True，不调用 _cleanup_task_resources。

        保护与容器共享的工作空间（_is_child_of_container 原保护对象）。
        """
        svc = _make_service()
        container = await svc.create_task(
            title="容器",
            metadata={"task_scope": "container"},
        )
        subtask = await svc.create_task(
            title="子任务",
            parent_task_id=container.id,
            metadata={"ws_meta": _ws_meta("shared")},
        )

        with (
            patch.object(
                svc, "_cleanup_task_resources", new_callable=AsyncMock
            ) as mock_cleanup,
            patch.object(svc, "_cancel_pipeline_recursive"),
            patch("infrastructure.service_provider.get_service_provider", side_effect=RuntimeError("no provider")),
        ):
            await svc.hard_delete_task(subtask.id)

        mock_cleanup.assert_not_called()

    @pytest.mark.asyncio
    async def test_container_subtask_no_ws_meta_skipped(self) -> None:
        """容器子任务无 ws_meta：skip_workspace=True（保守跳过，行为不变）。"""
        svc = _make_service()
        container = await svc.create_task(
            title="容器",
            metadata={"task_scope": "container"},
        )
        subtask = await svc.create_task(
            title="子任务",
            parent_task_id=container.id,
            metadata={},
        )

        with (
            patch.object(
                svc, "_cleanup_task_resources", new_callable=AsyncMock
            ) as mock_cleanup,
            patch.object(svc, "_cancel_pipeline_recursive"),
            patch("infrastructure.service_provider.get_service_provider", side_effect=RuntimeError("no provider")),
        ):
            await svc.hard_delete_task(subtask.id)

        mock_cleanup.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_container_task_is_cleaned(self) -> None:
        """非容器子任务（普通根任务）：skip_workspace=False（行为不变）。"""
        svc = _make_service()
        root = await svc.create_task(
            title="普通根任务",
            metadata={"ws_meta": _ws_meta("worktree")},
        )

        with (
            patch.object(
                svc, "_cleanup_task_resources", new_callable=AsyncMock
            ) as mock_cleanup,
            patch.object(svc, "_cancel_pipeline_recursive"),
            patch("infrastructure.service_provider.get_service_provider", side_effect=RuntimeError("no provider")),
        ):
            mock_cleanup.return_value = {"workspace_cleaned": True, "errors": []}
            await svc.hard_delete_task(root.id)

        mock_cleanup.assert_called_once()

    @pytest.mark.asyncio
    async def test_container_subtask_worktree_actually_deleted(self) -> None:
        """端到端验证：容器子任务 worktree 模式删除后任务记录消失。"""
        svc = _make_service()
        container = await svc.create_task(
            title="容器",
            metadata={"task_scope": "container"},
        )
        subtask = await svc.create_task(
            title="子任务",
            parent_task_id=container.id,
            metadata={"ws_meta": _ws_meta("worktree")},
        )
        assert svc.get_task(subtask.id) is not None

        with (
            patch.object(
                svc, "_cleanup_task_resources", new_callable=AsyncMock
            ),
            patch.object(svc, "_cancel_pipeline_recursive"),
            patch("infrastructure.service_provider.get_service_provider", side_effect=RuntimeError("no provider")),
        ):
            result = await svc.hard_delete_task(subtask.id)

        assert result["deleted"] is True
        assert svc.get_task(subtask.id) is None
