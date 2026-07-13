"""测试 task_submit 的 inherit_workspace_from 参数。

覆盖场景:
1. 旧工作空间存在 → 继承成功，workspace 被设置为旧路径
2. 旧任务不存在 → 报错，提示去掉参数重新提交
3. 旧工作空间路径不存在 → 报错，提示去掉参数重新提交
4. 旧任务无 ws_meta → 报错
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ["PYTHONPATH"] = "src"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))

from unittest.mock import MagicMock, AsyncMock

from tools.builtin.task_submit import TaskSubmitTool


def _make_mock_task(task_id, ws_meta=None):
    """创建 mock 任务对象。"""
    task = MagicMock()
    task.id = task_id
    task.metadata = {"ws_meta": ws_meta} if ws_meta else None
    return task


def _container_tmpdir(prefix="inherit_test_"):
    """在当前容器目录下建临时目录（满足「同容器才能 inherit」校验）。

    返回路径字符串，调用方负责清理。
    """
    container_root = Path(__file__).resolve().parents[3]
    d = tempfile.mkdtemp(prefix=prefix, dir=str(container_root))
    return d



def _make_inputs(**overrides):
    """创建默认的 task_submit inputs。"""
    inputs = {
        "goal": {"title": "test inherit", "description": "test"},
        "target_type": "agent",
        "target_id": "general_agent",
        "acceptance_criteria": {"file_check": {"input_params": {"path": "t.txt"}}},
        "task_scope": "short_term",
        "parent_agent_level": 1,
        "inherit_workspace_from": "old_task_001",
    }
    inputs.update(overrides)
    return inputs


def _setup_tool(tool, task_service=None):
    """配置 tool 的依赖 mock。"""
    if task_service is None:
        task_service = MagicMock()
    tool._get_task_service = MagicMock(return_value=task_service)

    mock_event_bus = MagicMock()
    mock_event_bus.has_subscribers = MagicMock(return_value=True)
    mock_event_bus.emit = AsyncMock()
    tool._get_event_bus = MagicMock(return_value=mock_event_bus)

    return task_service


class TestInheritWorkspaceSuccess:
    """旧工作空间存在，继承成功。"""

    @pytest.mark.asyncio
    async def test_inherits_existing_workspace(self):
        """旧 worktree 仍有效（目录 + .git 都在）时，workspace 被设为旧路径，任务正常提交。"""
        tool = TaskSubmitTool()

        tmpdir = _container_tmpdir()
        try:
            old_ws_path = os.path.join(tmpdir, "old_workspace")
            os.makedirs(old_ws_path, exist_ok=True)
            # worktree 模式继承要求目录仍是合法 worktree（含 .git 引用文件）
            Path(old_ws_path, ".git").write_text(
                f"gitdir: {tmpdir}/.git/worktrees/old", encoding="utf-8"
            )

            old_task = _make_mock_task("old_task_001", ws_meta={
                "mode": "worktree",
                "path": old_ws_path,
                "branch": "task/old",
                "project_root": tmpdir,
            })

            ts = _setup_tool(tool)
            ts.get_task = MagicMock(return_value=old_task)
            ts.create_task = AsyncMock(return_value=MagicMock(
                id="new_task_001",
                title="test inherit",
                status=MagicMock(value="pending"),
            ))

            result = await tool.execute(_make_inputs())

            assert result.success is True
            # 验证 create_task 收到的 metadata 包含继承的 workspace
            call_kwargs = ts.create_task.call_args[1]
            assert call_kwargs["metadata"]["workspace"] == old_ws_path
        finally:
            import shutil  # noqa: PLC0415
            shutil.rmtree(tmpdir, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_inherit_worktree_stale_git_reports_error(self):
        """旧 worktree 目录在但 .git 已清理（源任务完成后被清理）→ 报错说明现状。

        产物文件可能还在裸目录里，但 git 身份失效无法作为 worktree 继承，
        报错让 agent 自行决定是否去该目录捞取产物（bdcd592d 串台事故根因）。
        """
        tool = TaskSubmitTool()

        tmpdir = _container_tmpdir()
        try:
            old_ws_path = os.path.join(tmpdir, "old_workspace")
            os.makedirs(old_ws_path, exist_ok=True)
            # 目录存在但无 .git（源 worktree 已被清理）

            old_task = _make_mock_task("old_task_001", ws_meta={
                "mode": "worktree",
                "path": old_ws_path,
                "branch": "task/old",
                "project_root": tmpdir,
            })

            ts = _setup_tool(tool)
            ts.get_task = MagicMock(return_value=old_task)

            result = await tool.execute(_make_inputs())

            assert result.success is False
            assert "git 身份已失效" in result.error
            assert "inherit_workspace_from" in result.error
        finally:
            import shutil  # noqa: PLC0415
            shutil.rmtree(tmpdir, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_inherit_cross_container_rejected(self):
        """源任务属于其它容器 → 拒绝跨容器继承（串台根因）。"""
        tool = TaskSubmitTool()

        # 用系统临时目录模拟「其它容器」的工作空间
        with tempfile.TemporaryDirectory() as other_container:
            old_ws_path = os.path.join(other_container, "old_workspace")
            os.makedirs(old_ws_path, exist_ok=True)

            old_task = _make_mock_task("old_task_001", ws_meta={
                "mode": "worktree",
                "path": old_ws_path,
                "branch": "task/old",
                "project_root": other_container,
            })

            ts = _setup_tool(tool)
            ts.get_task = MagicMock(return_value=old_task)

            result = await tool.execute(_make_inputs())

            assert result.success is False
            assert "其它容器" in result.error
            assert "inherit_workspace_from" in result.error

class TestInheritWorkspaceNotFound:
    """旧工作空间路径不存在，报错。"""

    @pytest.mark.asyncio
    async def test_workspace_path_does_not_exist(self):
        """旧工作空间路径不存在 → create_failure_result，提示去掉参数。"""
        tool = TaskSubmitTool()
        # project_root 指向当前容器（通过同容器校验），path 不存在
        container_root = Path(__file__).resolve().parents[3]

        old_task = _make_mock_task("old_task_001", ws_meta={
            "mode": "worktree",
            "path": "/nonexistent/path/that/does/not/exist",
            "branch": "task/old",
            "project_root": str(container_root),
        })

        ts = _setup_tool(tool)
        ts.get_task = MagicMock(return_value=old_task)

        result = await tool.execute(_make_inputs())

        assert result.success is False
        assert "不存在" in result.error
        assert "inherit_workspace_from" in result.error or "去掉" in result.error

    @pytest.mark.asyncio
    async def test_workspace_path_empty(self):
        """旧工作空间路径为空字符串 → 报错。"""
        tool = TaskSubmitTool()

        old_task = _make_mock_task("old_task_001", ws_meta={
            "mode": "worktree",
            "path": "",
            "branch": "task/old",
        })

        ts = _setup_tool(tool)
        ts.get_task = MagicMock(return_value=old_task)

        result = await tool.execute(_make_inputs())

        assert result.success is False


class TestInheritTaskNotFound:
    """旧任务不存在，报错。"""

    @pytest.mark.asyncio
    async def test_old_task_does_not_exist(self):
        """get_task 返回 None → 报错。"""
        tool = TaskSubmitTool()

        ts = _setup_tool(tool)
        ts.get_task = MagicMock(return_value=None)

        result = await tool.execute(_make_inputs())

        assert result.success is False
        assert "不存在" in result.error

    @pytest.mark.asyncio
    async def test_old_task_no_metadata(self):
        """旧任务没有 metadata → 报错。"""
        tool = TaskSubmitTool()

        old_task = MagicMock()
        old_task.metadata = None

        ts = _setup_tool(tool)
        ts.get_task = MagicMock(return_value=old_task)

        result = await tool.execute(_make_inputs())

        assert result.success is False

    @pytest.mark.asyncio
    async def test_old_task_no_ws_meta(self):
        """旧任务 metadata 中没有 ws_meta → 报错。"""
        tool = TaskSubmitTool()

        old_task = MagicMock()
        old_task.metadata = {"other_key": "value"}

        ts = _setup_tool(tool)
        ts.get_task = MagicMock(return_value=old_task)

        result = await tool.execute(_make_inputs())

        assert result.success is False
        assert "工作空间信息" in result.error


class TestInheritNoFallback:
    """验证不会回退到 project_root。"""

    @pytest.mark.asyncio
    async def test_no_fallback_to_project_root(self):
        """旧 ws_meta.path 不存在时，即使 project_root 存在也不回退。"""
        tool = TaskSubmitTool()

        tmpdir = _container_tmpdir()
        try:
            # project_root 存在（同容器内），但 path 不存在
            old_task = _make_mock_task("old_task_001", ws_meta={
                "mode": "worktree",
                "path": "/nonexistent/workspace",
                "branch": "task/old",
                "project_root": tmpdir,  # 这个存在
            })

            ts = _setup_tool(tool)
            ts.get_task = MagicMock(return_value=old_task)

            result = await tool.execute(_make_inputs())

            # 不应该回退到 project_root，应该报错
            assert result.success is False
            assert "不存在" in result.error
        finally:
            import shutil  # noqa: PLC0415
            shutil.rmtree(tmpdir, ignore_errors=True)
