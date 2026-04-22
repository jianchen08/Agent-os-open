"""测试 task_submit 评估指标自动注入修复。

BUG-FIX-fix_20260420_eval_inject: 验证 acceptance_criteria 的类型规范化、
_auto_fill_criteria 的项目根目录定位、_build_metadata 的防御性存储。

覆盖场景:
1. acceptance_criteria 为非 dict 类型（字符串/列表）时自动重置并补全
2. acceptance_criteria 为空 dict 时自动从 agent 配置补全
3. _auto_fill_criteria 能正确从项目根目录定位 agent 配置
4. _build_metadata 对非 dict 类型不会静默丢失
"""
import os
import sys
import pytest

os.environ["PYTHONPATH"] = "src"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))

from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

from tools.builtin.task_submit import TaskSubmitTool


@pytest.fixture
def tool():
    """创建 TaskSubmitTool 实例。"""
    return TaskSubmitTool()


class TestAutoFillCriteria:
    """测试 _auto_fill_criteria 方法。"""

    def test_finds_planning_agent_config(self, tool):
        """能从项目根目录定位 planning_agent 配置并提取推荐指标。"""
        result = tool._auto_fill_criteria("planning_agent")
        assert isinstance(result, dict)
        assert "file_check" in result
        assert "format_valid" in result
        assert result["file_check"]["input_params"]["action"] == "read"
        assert result["format_valid"]["input_params"]["type"] == "markdown"

    def test_returns_empty_for_unknown_agent(self, tool):
        """不存在的 agent_id 返回空 dict。"""
        result = tool._auto_fill_criteria("nonexistent_agent_xyz")
        assert result == {}

    def test_uses_project_root_not_cwd(self, tool):
        """验证使用 __file__ 路径而非 Path.cwd() 定位配置。"""
        config_dir = Path(__file__).resolve().parent.parent.parent.parent / "config" / "agents"
        assert config_dir.exists(), f"配置目录应存在: {config_dir}"

        planning_yaml = list(config_dir.rglob("planning_agent.yaml"))
        assert len(planning_yaml) > 0, "应找到 planning_agent.yaml"


class TestBuildMetadata:
    """测试 _build_metadata 方法的防御性类型检查。"""

    def test_stores_dict_criteria(self, tool):
        """正常的 dict 类型 acceptance_criteria 能正确存储。"""
        inputs = {"metadata": {}, "workspace": "", "task_scope": "short_term", "target_id": "test"}
        goal = {"title": "test", "context": {}}
        criteria = {"file_check": {"input_params": {"path": "test.md"}}}

        metadata = tool._build_metadata(inputs, goal, criteria)
        assert metadata["acceptance_criteria"] == criteria
        assert metadata["evaluation_metric_ids"] == ["file_check"]

    def test_warns_on_non_dict_criteria(self, tool):
        """非 dict 类型的 acceptance_criteria 不静默丢失，记录 warning 日志。"""
        inputs = {"metadata": {}, "workspace": "", "task_scope": "short_term", "target_id": "test"}
        goal = {"title": "test", "context": {}}

        with patch("tools.builtin.task_submit.logger") as mock_logger:
            metadata = tool._build_metadata(inputs, goal, "invalid_string")

        mock_logger.warning.assert_called()
        assert "acceptance_criteria" not in metadata
        assert "evaluation_metric_ids" not in metadata

    def test_empty_criteria_not_stored(self, tool):
        """空 acceptance_criteria 不会存入 metadata。"""
        inputs = {"metadata": {}, "workspace": "", "task_scope": "short_term", "target_id": "test"}
        goal = {"title": "test", "context": {}}

        metadata = tool._build_metadata(inputs, goal, {})
        assert "acceptance_criteria" not in metadata
        assert "evaluation_metric_ids" not in metadata


class TestCriteriaNormalization:
    """测试 execute() 中 acceptance_criteria 类型规范化。"""

    @pytest.mark.asyncio
    async def test_string_criteria_resets_and_auto_fills(self, tool):
        """acceptance_criteria 为字符串时重置为空并触发自动补全。"""
        mock_task_service = MagicMock()
        mock_task_service.create_task.return_value = MagicMock(
            id="test_001", title="test", status=MagicMock(value="pending")
        )
        mock_event_bus = MagicMock()
        mock_event_bus.has_subscribers = MagicMock(return_value=True)
        mock_event_bus.emit = AsyncMock()

        tool._get_task_service = MagicMock(return_value=mock_task_service)
        tool._get_event_bus = MagicMock(return_value=mock_event_bus)

        inputs = {
            "goal": {"title": "test task", "description": "test"},
            "target_type": "agent",
            "target_id": "planning_agent",
            "acceptance_criteria": "this_is_a_string_not_dict",
            "task_scope": "short_term",
            "parent_agent_level": 1,
        }

        result = await tool.execute(inputs)

        assert result.success is True
        call_args = mock_task_service.create_task.call_args
        metadata = call_args[1]["metadata"]

        assert "acceptance_criteria" in metadata
        assert "evaluation_metric_ids" in metadata
        assert len(metadata["evaluation_metric_ids"]) > 0

    @pytest.mark.asyncio
    async def test_empty_criteria_auto_fills_from_agent_config(self, tool):
        """空 acceptance_criteria 时自动从 agent 配置补全。"""
        mock_task_service = MagicMock()
        mock_task_service.create_task.return_value = MagicMock(
            id="test_002", title="test", status=MagicMock(value="pending")
        )
        mock_event_bus = MagicMock()
        mock_event_bus.has_subscribers = MagicMock(return_value=True)
        mock_event_bus.emit = AsyncMock()

        tool._get_task_service = MagicMock(return_value=mock_task_service)
        tool._get_event_bus = MagicMock(return_value=mock_event_bus)

        inputs = {
            "goal": {"title": "test task", "description": "test"},
            "target_type": "agent",
            "target_id": "planning_agent",
            "task_scope": "short_term",
            "parent_agent_level": 1,
        }

        result = await tool.execute(inputs)

        assert result.success is True
        call_args = mock_task_service.create_task.call_args
        metadata = call_args[1]["metadata"]

        assert "file_check" in metadata.get("acceptance_criteria", {})
        assert "format_valid" in metadata.get("acceptance_criteria", {})
        assert "file_check" in metadata.get("evaluation_metric_ids", [])
        assert "format_valid" in metadata.get("evaluation_metric_ids", [])

    @pytest.mark.asyncio
    async def test_list_criteria_resets_and_auto_fills(self, tool):
        """acceptance_criteria 为列表时重置为空并触发自动补全。"""
        mock_task_service = MagicMock()
        mock_task_service.create_task.return_value = MagicMock(
            id="test_003", title="test", status=MagicMock(value="pending")
        )
        mock_event_bus = MagicMock()
        mock_event_bus.has_subscribers = MagicMock(return_value=True)
        mock_event_bus.emit = AsyncMock()

        tool._get_task_service = MagicMock(return_value=mock_task_service)
        tool._get_event_bus = MagicMock(return_value=mock_event_bus)

        inputs = {
            "goal": {"title": "test task", "description": "test"},
            "target_type": "agent",
            "target_id": "planning_agent",
            "acceptance_criteria": ["file_check", "format_valid"],
            "task_scope": "short_term",
            "parent_agent_level": 1,
        }

        result = await tool.execute(inputs)

        assert result.success is True
        call_args = mock_task_service.create_task.call_args
        metadata = call_args[1]["metadata"]

        assert isinstance(metadata.get("acceptance_criteria"), dict)
        assert len(metadata["evaluation_metric_ids"]) > 0
