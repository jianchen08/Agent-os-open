"""会话 → 任务继承链路测试。

验证主会话的工作空间与隔离模式经通用注入机制（param_inject）单向流入
task_submit 子任务 metadata（任务系统零改动、不感知"会话"概念）：
1. param_inject：state 有 workspace / isolation_level 时注入所有工具调用
2. task_submit._build_metadata：注入的 workspace / isolation_level 写入任务 metadata
"""
from __future__ import annotations

import pytest

from pipeline.plugin import PluginContext
from pipeline.types import StateKeys
from plugins.input.param_inject.plugin import ParamInjectPlugin

_SESSION_WS = r"D:\myproject\demo-app"


# ============================================================================
# 1. param_inject 注入 workspace / isolation_level
# ============================================================================


class TestParamInjectSessionContext:
    """param_inject 的会话上下文注入。"""

    async def _inject(self, state: dict) -> list[dict]:
        plugin = ParamInjectPlugin(config={})
        ctx = PluginContext(state=state, config={}, _services={})
        result = await plugin.execute(ctx)
        return result.state_updates.get(StateKeys.RAW_TOOL_CALLS, [])

    @pytest.mark.asyncio
    async def test_injects_workspace_and_isolation_level(self) -> None:
        """state 有 workspace + isolation_level 时，两参数都注入。"""
        state = {
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.RAW_TOOL_CALLS: [{"name": "task_submit", "args": {"goal_title": "x"}}],
            "workspace": _SESSION_WS,
            "isolation_level": "isolated",
        }
        calls = await self._inject(state)
        assert len(calls) == 1
        args = calls[0]["args"]
        assert args["workspace"] == _SESSION_WS
        assert args["isolation_level"] == "isolated"

    @pytest.mark.asyncio
    async def test_no_isolation_level_no_inject(self) -> None:
        """state 无 isolation_level 时不注入该参数（旧会话兼容）。"""
        state = {
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.RAW_TOOL_CALLS: [{"name": "task_submit", "args": {}}],
            "workspace": _SESSION_WS,
        }
        calls = await self._inject(state)
        assert "isolation_level" not in calls[0]["args"]

    @pytest.mark.asyncio
    async def test_explicit_args_not_overridden(self) -> None:
        """LLM 显式传参时系统值不覆盖（仅缺省注入）。"""
        state = {
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.RAW_TOOL_CALLS: [
                {"name": "task_submit", "args": {"workspace": r"D:\explicit", "isolation_level": "non_isolated"}},
            ],
            "workspace": _SESSION_WS,
            "isolation_level": "isolated",
        }
        calls = await self._inject(state)
        args = calls[0]["args"]
        assert args["workspace"] == r"D:\explicit"
        assert args["isolation_level"] == "non_isolated"


# ============================================================================
# 2. task_submit 将注入值写入任务 metadata
# ============================================================================


class TestTaskSubmitMetadataInherit:
    """task_submit._build_metadata 的继承写入。"""

    def _build_metadata(self, inputs: dict) -> dict:
        from tools.builtin.task_submit.tool import TaskSubmitTool  # noqa: PLC0415

        tool = TaskSubmitTool.__new__(TaskSubmitTool)
        goal = {"title": "测试", "description": ""}
        return tool._build_metadata(inputs, goal, {})

    def test_workspace_and_isolation_written(self) -> None:
        """主会话下注入的 workspace / isolation_level 写入 metadata。"""
        metadata = self._build_metadata(
            {
                "workspace": _SESSION_WS,
                "isolation_level": "non_isolated",
                "session_id": "sess123",
            }
        )
        assert metadata["workspace"] == _SESSION_WS
        assert metadata["isolation_level"] == "non_isolated"
        assert metadata["session_id"] == "sess123"

    def test_subtask_without_inherit_skips_workspace(self) -> None:
        """子任务（有 parent_task_id）未显式继承时不写入 workspace（既有语义）。"""
        metadata = self._build_metadata(
            {
                "workspace": _SESSION_WS,
                "isolation_level": "isolated",
                "parent_task_id": "parent001",
            }
        )
        assert "workspace" not in metadata
        assert metadata["isolation_level"] == "isolated"

    def test_no_values_empty_metadata(self) -> None:
        """无 workspace / isolation 时 metadata 不含对应键。"""
        metadata = self._build_metadata({})
        assert "workspace" not in metadata
        assert "isolation_level" not in metadata
