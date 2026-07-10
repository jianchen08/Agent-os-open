"""测试 task_submit 目标 Agent 校验：存在性、级别、is_active 启用态。

BUG-FIX: is_active 历史上在派发链路完全不校验，导致派发给已禁用 Agent（如
programming_orchestrator_agent_v2 曾 is_active=false）仍会正常执行。本测试锁死
_validate_target_agent 对 is_active=false 的拦截行为，覆盖 registry 与磁盘兜底两条路径。
"""
import os
import sys

import pytest

os.environ["PYTHONPATH"] = "src"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))

from types import SimpleNamespace
from unittest.mock import patch

from tools.builtin.task_submit import TaskSubmitTool


@pytest.fixture
def tool():
    """创建 TaskSubmitTool 实例。"""
    return TaskSubmitTool()


def _config(level: str, is_active: bool):
    """构造模拟的 AgentConfig（registry 返回的对象形态）。"""
    return SimpleNamespace(level=level, is_active=is_active)


class TestValidateIsActiveRegistry:
    """registry 路径下的 is_active 校验。"""

    def test_active_agent_passes(self, tool):
        """registry 命中且 is_active=true 的 L2 agent 校验通过。"""
        tool._get_agent_config_from_registry = lambda tid: _config("L2", True)
        valid, msg, code = tool._validate_target_agent("live_agent", 1)
        assert (valid, code) == (True, "")
        assert msg == ""

    def test_inactive_agent_rejected(self, tool):
        """registry 命中但 is_active=false 的 agent 被拦截，错误码 TARGET_AGENT_INACTIVE。"""
        tool._get_agent_config_from_registry = lambda tid: _config("L2", False)
        valid, msg, code = tool._validate_target_agent("dead_agent", 1)
        assert valid is False
        assert code == "TARGET_AGENT_INACTIVE"
        assert "已禁用" in msg


class TestValidateIsActiveDisk:
    """磁盘兜底路径下的 is_active 校验。"""

    def test_lookup_from_disk_returns_is_active(self):
        """_lookup_agent_from_disk 返回 4 元组，第 4 个为 is_active。"""
        found, level_str, level, is_active = (
            TaskSubmitTool._lookup_agent_from_disk("code_writer_agent")
        )
        assert found is True
        assert level_str == "L3"
        assert level == 3
        assert is_active is True

    def test_active_agent_via_disk_passes(self, tool):
        """registry 未命中、磁盘返回 is_active=true 时校验通过。"""
        tool._get_agent_config_from_registry = lambda tid: None
        with patch.object(
            TaskSubmitTool,
            "_lookup_agent_from_disk",
            staticmethod(lambda tid: (True, "L2", 2, True)),
        ):
            valid, msg, code = tool._validate_target_agent("live_agent", 1)
        assert (valid, code) == (True, "")
        assert msg == ""

    def test_inactive_agent_via_disk_rejected(self, tool):
        """registry 未命中、磁盘返回 is_active=false 时被拦截。"""
        tool._get_agent_config_from_registry = lambda tid: None
        with patch.object(
            TaskSubmitTool,
            "_lookup_agent_from_disk",
            staticmethod(lambda tid: (True, "L2", 2, False)),
        ):
            valid, msg, code = tool._validate_target_agent("dead_agent", 1)
        assert valid is False
        assert code == "TARGET_AGENT_INACTIVE"


class TestValidateLevelRegression:
    """回归：is_active 新校验不破坏既有的级别校验。"""

    def test_l1_agent_still_rejected(self, tool):
        """active 的 L1 agent 仍被 TARGET_AGENT_IS_L1 拦截（级别优先于 is_active）。"""
        tool._get_agent_config_from_registry = lambda tid: _config("L1", True)
        valid, _msg, code = tool._validate_target_agent("l1_agent", 1)
        assert valid is False
        assert code == "TARGET_AGENT_IS_L1"

    def test_nonexistent_agent_still_rejected(self, tool):
        """registry 与磁盘都找不到时仍返回 TARGET_AGENT_NOT_FOUND。"""
        tool._get_agent_config_from_registry = lambda tid: None
        with patch.object(
            TaskSubmitTool,
            "_lookup_agent_from_disk",
            staticmethod(lambda tid: (False, "", 0, True)),
        ):
            valid, _msg, code = tool._validate_target_agent("ghost_agent", 1)
        assert valid is False
        assert code == "TARGET_AGENT_NOT_FOUND"
