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
        """旧工作空间路径存在时，workspace 被设为旧路径，任务正常提交。"""
        tool = TaskSubmitTool()

        with tempfile.TemporaryDirectory() as tmpdir:
            old_ws_path = os.path.join(tmpdir, "old_workspace")
            os.makedirs(old_ws_path, exist_ok=True)

            old_task = _make_mock_task("old_task_001", ws_meta={
                "mode": "worktree",
                "path": old_ws_path,
                "branch": "task/old",
                "project_root": tmpdir,
            })

            ts = _setup_tool(tool)
            ts.get_task = MagicMock(return_value=old_task)
            ts.create_task = MagicMock(return_value=MagicMock(
                id="new_task_001",
                title="test inherit",
                status=MagicMock(value="pending"),
            ))

            result = await tool.execute(_make_inputs())

            assert result.success is True
            # 验证 create_task 收到的 metadata 包含继承的 workspace
            call_kwargs = ts.create_task.call_args[1]
            assert call_kwargs["metadata"]["workspace"] == old_ws_path


class TestInheritWorkspaceNotFound:
    """旧工作空间路径不存在，报错。"""

    @pytest.mark.asyncio
    async def test_workspace_path_does_not_exist(self):
        """旧工作空间路径不存在 → create_failure_result，提示去掉参数。"""
        tool = TaskSubmitTool()

        old_task = _make_mock_task("old_task_001", ws_meta={
            "mode": "worktree",
            "path": "/nonexistent/path/that/does/not/exist",
            "branch": "task/old",
            "project_root": "/also/nonexistent",
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

        with tempfile.TemporaryDirectory() as tmpdir:
            # project_root 存在，但 path 不存在
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
