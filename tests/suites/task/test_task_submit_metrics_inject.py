"""测试 task_submit 对 acceptance_criteria 的类型校验。

背景：历史上 acceptance_criteria 非 dict（如 list/字符串）时会被静默重置为空 dict
并继续提交，导致「LLM 填了验收标准」伪装成「没填」，task_evaluate 又对空 AC
「自动通过」，制造虚假合格信号。

修复后铁律：空（不传）→ 合法，无 AC 自动通过；传了 → 必须是 dict（key=指标ID，
value=配置对象），传 list/字符串等错误类型一律拒绝提交（INVALID_ACCEPTANCE_CRITERIA），
让 LLM 拿到明确反馈去修正。

覆盖场景:
1. 非 dict 类型（字符串/列表/数字）→ 拒绝提交，返回 INVALID_ACCEPTANCE_CRITERIA
2. 空 acceptance_criteria（不传或空 dict）→ 正常提交，无 AC
3. 合法 dict → 正常提交，AC 存入 metadata
4. _build_metadata 收到非 dict 时断言报错（第二道防线，不应被静默吞掉）
"""
from __future__ import annotations

import os
import sys

import pytest
from unittest.mock import AsyncMock, MagicMock

os.environ["PYTHONPATH"] = "src"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))

from tools.builtin.task_submit import TaskSubmitTool  # noqa: E402


@pytest.fixture
def tool():
    """创建 TaskSubmitTool 实例。"""
    return TaskSubmitTool()


@pytest.fixture
def mock_services(tool, monkeypatch):
    """为走到 create_task 的成功路径 mock 必要的 service。

    task_submit.execute 成功路径会：await create_task → get_service_provider().get("task_worker")
    → task_worker.submit_task。把这些外部依赖一并 mock 掉，使测试聚焦于
    acceptance_criteria 校验而非完整提交链路。monkeypatch 自动清理 patch。
    """
    mock_task_service = MagicMock()
    # create_task / hard_delete 是 async（代码用 await 调用），必须用 AsyncMock
    mock_task_service.create_task = AsyncMock(
        return_value=MagicMock(id="test_001", title="test", status=MagicMock(value="pending"))
    )
    mock_task_service.hard_delete = AsyncMock()
    mock_task_service.get_task.return_value = None  # 无父任务

    mock_event_bus = MagicMock()
    mock_event_bus.has_subscribers = MagicMock(return_value=True)
    mock_event_bus.emit = AsyncMock()

    mock_task_worker = MagicMock()
    mock_task_worker.submit_task = MagicMock(return_value=True)

    tool._get_task_service = MagicMock(return_value=mock_task_service)
    tool._get_event_bus = MagicMock(return_value=mock_event_bus)
    monkeypatch.setattr(
        "infrastructure.service_provider.get_service_provider",
        lambda: MagicMock(get=lambda _: mock_task_worker),
    )

    return mock_task_service


class TestAcceptanceCriteriaTypeGuard:
    """execute() 入口对 acceptance_criteria 类型的硬校验。"""

    @pytest.mark.asyncio
    @pytest.mark.task
    async def test_list_criteria_rejected(self, tool):
        """acceptance_criteria 为 list → 拒绝提交，不静默重置为空。"""
        inputs = {
            "goal": {"title": "test task", "description": "test"},
            "target_type": "agent",
            "target_id": "general_agent",
            "acceptance_criteria": ["file_check", "bash_check"],
            "task_scope": "non_container",
            "parent_agent_level": 1,
        }

        result = await tool.execute(inputs)

        assert result.success is False
        assert result.error_code == "INVALID_ACCEPTANCE_CRITERIA"

    @pytest.mark.asyncio
    @pytest.mark.task
    async def test_string_criteria_rejected(self, tool):
        """acceptance_criteria 为字符串 → 拒绝提交。"""
        inputs = {
            "goal": {"title": "test task", "description": "test"},
            "target_type": "agent",
            "target_id": "general_agent",
            "acceptance_criteria": "file_check",
            "task_scope": "non_container",
            "parent_agent_level": 1,
        }

        result = await tool.execute(inputs)

        assert result.success is False
        assert result.error_code == "INVALID_ACCEPTANCE_CRITERIA"

    @pytest.mark.asyncio
    @pytest.mark.task
    async def test_none_criteria_allowed(self, tool, mock_services):
        """不传 acceptance_criteria（None）→ 合法，正常提交，无 AC。"""
        inputs = {
            "goal": {"title": "test task", "description": "test"},
            "target_type": "agent",
            "target_id": "general_agent",
            "task_scope": "non_container",
            "parent_agent_level": 1,
        }

        result = await tool.execute(inputs)

        assert result.success is True
        metadata = mock_services.create_task.call_args[1]["metadata"]
        assert "acceptance_criteria" not in metadata
        assert "evaluation_metric_ids" not in metadata

    @pytest.mark.asyncio
    @pytest.mark.task
    async def test_empty_dict_criteria_allowed(self, tool, mock_services):
        """acceptance_criteria 为空 dict → 合法，正常提交，无 AC。"""
        inputs = {
            "goal": {"title": "test task", "description": "test"},
            "target_type": "agent",
            "target_id": "general_agent",
            "acceptance_criteria": {},
            "task_scope": "non_container",
            "parent_agent_level": 1,
        }

        result = await tool.execute(inputs)

        assert result.success is True
        metadata = mock_services.create_task.call_args[1]["metadata"]
        assert "acceptance_criteria" not in metadata
        assert "evaluation_metric_ids" not in metadata

    @pytest.mark.asyncio
    @pytest.mark.task
    async def test_valid_dict_criteria_stored(self, tool, mock_services):
        """合法 dict acceptance_criteria → 正常提交，AC 存入 metadata。"""
        criteria = {"file_check": {"input_params": {"path": "src/main.py"}}}
        inputs = {
            "goal": {"title": "test task", "description": "test"},
            "target_type": "agent",
            "target_id": "general_agent",
            "acceptance_criteria": criteria,
            "task_scope": "non_container",
            "parent_agent_level": 1,
        }

        result = await tool.execute(inputs)

        assert result.success is True
        metadata = mock_services.create_task.call_args[1]["metadata"]
        assert metadata["acceptance_criteria"] == criteria
        assert metadata["evaluation_metric_ids"] == ["file_check"]


class TestBuildMetadataDefensiveAssert:
    """_build_metadata 第二道防线：非 dict 断言报错，不静默吞掉。"""

    def test_stores_dict_criteria(self, tool):
        """正常的 dict 类型 acceptance_criteria 能正确存储。"""
        inputs = {"metadata": {}, "workspace": "", "task_scope": "non_container", "target_id": "test"}
        goal = {"title": "test", "context": {}}
        criteria = {"file_check": {"input_params": {"path": "test.md"}}}

        metadata = tool._build_metadata(inputs, goal, criteria)
        assert metadata["acceptance_criteria"] == criteria
        assert metadata["evaluation_metric_ids"] == ["file_check"]

    def test_empty_criteria_not_stored(self, tool):
        """空 acceptance_criteria 不会存入 metadata。"""
        inputs = {"metadata": {}, "workspace": "", "task_scope": "non_container", "target_id": "test"}
        goal = {"title": "test", "context": {}}

        metadata = tool._build_metadata(inputs, goal, {})
        assert "acceptance_criteria" not in metadata
        assert "evaluation_metric_ids" not in metadata

    def test_non_dict_criteria_raises_assertion(self, tool):
        """非 dict 的 acceptance_criteria 触发断言报错（execute 入口已拦截，此处不应到达）。"""
        inputs = {"metadata": {}, "workspace": "", "task_scope": "non_container", "target_id": "test"}
        goal = {"title": "test", "context": {}}

        with pytest.raises(AssertionError):
            tool._build_metadata(inputs, goal, ["file_check"])

    def test_non_dict_string_criteria_raises_assertion(self, tool):
        """字符串类型的 acceptance_criteria 触发断言报错。"""
        inputs = {"metadata": {}, "workspace": "", "task_scope": "non_container", "target_id": "test"}
        goal = {"title": "test", "context": {}}

        with pytest.raises(AssertionError):
            tool._build_metadata(inputs, goal, "invalid_string")
