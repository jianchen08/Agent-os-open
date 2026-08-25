"""P0-3 task_manage 权限覆盖修复测试（TDD）。

回归安全缺口：``TaskTool`` 已有 ``_check_permission``（L1 会话隔离 / L2 父任务归属），
但 6 个高危动作入口自身未调用——纵深防御缺失：

- ``_get_task_list``：L2 无 parent_task_id 时仍可能列出他人任务（submitted_by_level 缺失的遗留任务）。
- ``_change_status``：仅校验 L1 层级，未校验会话归属——L1 可改其他会话的容器任务。
- ``_inject_to_running`` / ``_resume_from_stopped`` / ``_retry_from_terminal``：
  依赖调用方 ``_continue_task`` 的检查，自身入口无防御——若被直接调用或新增调用路径即裸奔。
- ``_batch_tasks``：逐任务委派子动作，自身入口不预检权限。

契约：上述 6 个动作入口均显式调用 ``_check_permission``，无权限 →
``INSUFFICIENT_PERMISSION``（list 动作则过滤掉越权任务）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# ── 路径注入：task 工具目录 + system/tasks 权威目录（0.2 平铺模块）──
# 跨插件共享类型走 SDK（agentos_plugin_sdk，pip 安装，无需注入路径）。
# tool.py 顶部 `from service import …` / `from task_types import …` 直接解析到
# system/tasks 平铺模块；故注入 task 工具目录与 system/tasks 目录。
_REPO_ROOT = Path(__file__).resolve().parents[2]
_TASK_DIR = _REPO_ROOT / "plugins" / "shared" / "tools" / "task"
_TASKS_DIR = _REPO_ROOT / "plugins" / "shared" / "system" / "tasks"
for _p in (str(_TASK_DIR), str(_TASKS_DIR)):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)
# 整 suite 收集时弹出先前测试缓存的可能同名平铺模块，避免 tool.py 的
# `from service import …` / `from task_types import …` 命中错误缓存。
# task_types/agents_types 是 tasks 插件独占模块（全仓无同名竞争者），
# 不得弹出——收集期已绑定的 TaskStatus 实例依赖其驻留，重载会复制枚举类。
for _m in (
    "tool",
    "service",
    "state_machine",
):
    sys.modules.pop(_m, None)

import tool as task_tool  # noqa: E402

TaskTool = task_tool.TaskTool
TaskModel = task_tool.TaskModel
TaskStatus = task_tool.TaskStatus


def _make_service() -> MagicMock:
    """构造 TaskService mock（get_task/list_all/force_transition 等）。"""
    svc = MagicMock()
    svc.get_task.return_value = None
    svc.list_all = AsyncMock(return_value=[])
    svc.force_transition = AsyncMock()
    svc.save_task = AsyncMock()
    svc.resume_task = AsyncMock()
    svc.pause_task = AsyncMock()
    svc.delete_task = AsyncMock()
    svc.list_subtasks.return_value = []
    svc._cleanup_subtask_worktrees = AsyncMock(return_value={})
    return svc


def _make_tool() -> TaskTool:
    """构造 TaskTool，注入 mock 服务并屏蔽 execution_record_storage（避免触达 infrastructure）。"""
    t = TaskTool()
    t._task_service = _make_service()
    # list 路径会调 _get_latest_activity → _get_execution_record_storage；屏蔽之返回 None
    t._get_execution_record_storage = lambda: None  # type: ignore[method-assign]
    return t


def _task(
    *,
    tid: str = "t1",
    status: TaskStatus = TaskStatus.RUNNING,
    session_id: str | None = "session-other",
    submitted_by_level: int | None = None,
    task_scope: str | None = None,
    parent_task_id: str | None = None,
    pipeline_run_id: str | None = None,
) -> TaskModel:
    """构造 TaskModel fixture。"""
    md: dict = {}
    if session_id is not None:
        md["session_id"] = session_id
    if submitted_by_level is not None:
        md["submitted_by_level"] = submitted_by_level
    if task_scope is not None:
        md["task_scope"] = task_scope
    return TaskModel(
        id=tid,
        title="t",
        status=status,
        metadata=md,
        parent_task_id=parent_task_id,
        pipeline_run_id=pipeline_run_id,
    )


# ═══════════════════════════════════════════════════════════
# 1. _change_status：L1 跨会话改容器任务 → 拒绝
# ═══════════════════════════════════════════════════════════





# ═══════════════════════════════════════════════════════════
# 2. _get_task_list：L2 无 parent_task_id → 过滤掉遗留任务
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_task_list_filters_unauthorized_for_l2() -> None:
    """_get_task_list：L2 无 parent_task_id 时，遗留任务（submitted_by_level 缺失）应被过滤。

    RED：现有 L2 过滤仅在传 pipeline_id/parent_task_id 时剔除；遗留任务混入列表。
    GREEN：补 _check_permission → 遗留任务被拒，列表为空。
    """
    tool = _make_tool()
    legacy = _task(tid="legacy-1", status=TaskStatus.COMPLETED, submitted_by_level=None)
    tool._task_service.list_all = AsyncMock(return_value=[legacy])

    result = await tool._get_task_list({}, parent_agent_level=2)

    assert result.success
    rows = result.output["d"]
    assert rows == [], f"L2 无 parent_task_id 不应看到他人遗留任务，实际={rows}"


# ═══════════════════════════════════════════════════════════
# 3-5. continue 三子动作：直接调用、跨会话 → 拒绝（纵深防御）
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_inject_to_running_denied_cross_session() -> None:
    """_inject_to_running：直接调用 + 跨会话 → INSUFFICIENT_PERMISSION（不触达 emit）。"""
    tool = _make_tool()
    task = _task(status=TaskStatus.RUNNING, session_id="session-other")

    result = await tool._inject_to_running(
        task, "do something", 1, {"session_id": "session-mine"}
    )

    assert not result.success
    assert result.error_code == "INSUFFICIENT_PERMISSION"


@pytest.mark.asyncio
async def test_resume_from_stopped_denied_cross_session() -> None:
    """_resume_from_stopped：直接调用 + 跨会话 → INSUFFICIENT_PERMISSION（不触达 resume_task）。"""
    tool = _make_tool()
    task = _task(status=TaskStatus.STOPPED, session_id="session-other")

    result = await tool._resume_from_stopped(
        task, "", tool._task_service, 1, {"session_id": "session-mine"}
    )

    assert not result.success
    assert result.error_code == "INSUFFICIENT_PERMISSION"
    tool._task_service.resume_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_from_terminal_denied_cross_session() -> None:
    """_retry_from_terminal：直接调用 + 跨会话 → INSUFFICIENT_PERMISSION（不触达 force_transition）。"""
    tool = _make_tool()
    task = _task(status=TaskStatus.FAILED, session_id="session-other")

    result = await tool._retry_from_terminal(
        task, "", tool._task_service, 1, {"session_id": "session-mine"}
    )

    assert not result.success
    assert result.error_code == "INSUFFICIENT_PERMISSION"
    tool._task_service.force_transition.assert_not_awaited()


# ═══════════════════════════════════════════════════════════
# 6. _batch_tasks：越权任务在入口预检即拒绝，不委派子动作
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_batch_tasks_prechecks_permission_and_skips_dispatch() -> None:
    """_batch_tasks：越权任务在自身入口预检被拒，子动作（_continue_task）不被调用。

    RED：_batch_tasks 直接委派 _continue_task（其内部才检查）→ _continue_task 被调用。
    GREEN：_batch_tasks 入口先 _check_permission 预检 → 越权任务短路，不委派。
    """
    tool = _make_tool()
    tool._task_service.get_task.return_value = _task(
        status=TaskStatus.RUNNING, session_id="session-other"
    )
    # 监视子动作是否被委派
    tool._continue_task = AsyncMock(  # type: ignore[method-assign]
        return_value=task_tool.create_success_result({"task_id": "t1"})
    )

    result = await tool._batch_tasks(
        {"action": "continue", "task_ids": ["t1"], "session_id": "session-mine"},
        parent_agent_level=1,
    )

    assert result.success  # 批量本身成功（逐任务结果汇总）
    rows = result.output["results"]
    assert rows[0]["success"] is False
    assert "INSUFFICIENT_PERMISSION" in rows[0]["error"]
    # 关键：越权任务不应触达 _continue_task
    tool._continue_task.assert_not_awaited()


# ═══════════════════════════════════════════════════════════
# 回归：合法权限不破坏（L1 操作自己会话的任务仍可继续）
# ═══════════════════════════════════════════════════════════



