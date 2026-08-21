# @feature: FP-0.2.〇 任务执行驱动 | @ci: python-coverage
"""容器任务 goal 闸门单测（mypy 收紧批配套：_execute_long_term 镜像 execute() 基础校验）。

意图（WHY）：
- 2026-08-21 治理批次为 _execute_long_term 补 goal 前置闸门（原实现 None 漂到
  _dispatch_task_pipeline 处 TypeError 崩溃，mypy 以 index/union-attr 报出）。
- 契约：goal 缺失/非 dict/缺 title → MISSING_GOAL 显式失败，不进入容器创建流程。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

_TASK_SUBMIT_DIR = Path(__file__).resolve().parent.parent / "task_submit"
_TASKS_DIR = Path(__file__).resolve().parent.parent.parent / "system" / "tasks"
for _d in (str(_TASK_SUBMIT_DIR), str(_TASKS_DIR)):
    if _d not in sys.path:
        sys.path.insert(0, _d)

_spec = importlib.util.spec_from_file_location(
    "task_submit_tool_container_guard_test", _TASK_SUBMIT_DIR / "tool.py"
)
assert _spec is not None and _spec.loader is not None
_tool_mod = importlib.util.module_from_spec(_spec)
sys.modules["task_submit_tool_container_guard_test"] = _tool_mod
_spec.loader.exec_module(_tool_mod)

TaskSubmitTool = _tool_mod.TaskSubmitTool

pytestmark = pytest.mark.unit


def _err_code(inputs: dict) -> str:
    result = asyncio.run(TaskSubmitTool()._execute_long_term(inputs))
    return result.error_code or ""


class TestContainerGoalGuard:
    def test_no_goal_at_all(self) -> None:
        assert _err_code({}) == "MISSING_GOAL"

    def test_goal_title_empty(self) -> None:
        assert _err_code({"goal": {"title": ""}}) == "MISSING_GOAL"

    def test_goal_not_dict(self) -> None:
        assert _err_code({"goal": "just-a-string"}) == "MISSING_GOAL"

    def test_valid_goal_passes_gate(self) -> None:
        """goal 合法 → 过闸门，进入后续层级校验（L1 闸门拦住非 L1 提交者）。"""
        result = asyncio.run(
            TaskSubmitTool()._execute_long_term(
                {"goal": {"title": "容器任务"}, "parent_agent_level": 2}
            )
        )
        assert result.error_code == "L2_CANNOT_SUBMIT_CONTAINER"
