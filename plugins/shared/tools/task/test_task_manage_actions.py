# @feature: FP-0.2.二 内部模块 manifest（task_manage 控制面：continue/stop/change/batch） | @ci: python-coverage
"""task_manage 控制面操作测试（行覆盖补全）。

与 test_task_manage.py（0.2 接线回归）互补：本文件聚焦
execute 分派、continue 四场景、stop 挂起/级联、change 状态映射、
批量操作与权限收口。

真实依赖：TaskService（tmp 数据目录）+ 真实 TaskStorage；仅 mock
跨进程 capability（chat.send_message / pipeline-executor / state 聚合）。
capability 以模块级全局（set_chat_sender/set_pipeline_executor）注入，
与 server.py on_load 接线方式一致。
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent.parent.parent.parent
_PLUGIN_PATHS = tuple(
    str(_d)
    for _d in (
        _HERE,
        _PROJECT_ROOT / "plugins" / "shared" / "system" / "tasks",
        _PROJECT_ROOT / "plugins" / "shared" / "system",
    )
)
for _d in _PLUGIN_PATHS:
    if _d not in sys.path:
        sys.path.insert(0, _d)
# 收集期 tool 槽位保护：确保解析到本目录的 tool.py（同 test_task_manage.py）。
sys.modules.pop("tool", None)

import tool as _task_mod  # noqa: E402
from tool import TaskTool  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _ensure_plugin_paths():
    """平铺串扰自持：同 test_task_manage.py（用例前重插路径 + 快照还原）。"""
    for _d in _PLUGIN_PATHS:
        if _d in sys.path:
            sys.path.remove(_d)
        sys.path.insert(0, _d)
    _saved = {n: sys.modules.get(n) for n in ("service", "http_api")}
    for n in _saved:
        sys.modules.pop(n, None)
    try:
        _server_py = _PROJECT_ROOT / "plugins" / "shared" / "system" / "tasks" / "server.py"
        _spec = importlib.util.spec_from_file_location("tasks_server_preheat_actions", _server_py)
        if _spec is not None and _spec.loader is not None:
            _mod = importlib.util.module_from_spec(_spec)
            _spec.loader.exec_module(_mod)
    except Exception:  # noqa: BLE001 —— 预热失败不阻断用例（复由用例自身报错）
        pass
    yield
    for n, m in _saved.items():
        sys.modules.pop(n, None)
        if m is not None:
            sys.modules[n] = m


@pytest.fixture()
def svc(tmp_path: Path) -> Any:
    """真实 TaskService（tmp 数据目录，每用例隔离）。"""
    from service import TaskService

    return TaskService(data_dir=str(tmp_path))


@pytest.fixture()
def tool(svc: Any) -> TaskTool:
    t = TaskTool()
    t._task_service = svc
    return t


@pytest.fixture(autouse=True)
def _capabilities():
    """capability 全局注入点：接线用 setter，用例结束还原为 None。"""
    prev_chat = _task_mod._chat_sender
    prev_state = _task_mod._state_reader
    prev_exec = _task_mod._pipeline_executor
    yield
    _task_mod._chat_sender = prev_chat
    _task_mod._state_reader = prev_state
    _task_mod._pipeline_executor = prev_exec


def _set_chat(sender: Any) -> None:
    _task_mod.set_chat_sender(sender)


def _set_exec(executor: Any) -> None:
    _task_mod.set_pipeline_executor(executor)


async def _make_task(svc: Any, **meta: Any) -> Any:
    return await svc.create_task(title=meta.pop("title", "任务"), metadata=meta or None)


async def _set_running(svc: Any, task_id: str) -> None:
    from task_types import TaskStatus

    await svc.force_transition(task_id, TaskStatus.RUNNING)


# ─────────────────────────── execute 分派与守卫 ───────────────────────────


async def test_execute_missing_parent_agent_level_fails() -> None:
    """注入参数缺失：parent_agent_level 未注入 → MISSING_INJECTED_PARAM。"""
    result = await TaskTool().execute({"action": "get"})
    assert not result.success
    assert result.error_code == "MISSING_INJECTED_PARAM"
    assert "parent_agent_level" in (result.error or "")


async def test_execute_service_unavailable() -> None:
    """TaskService 初始化失败 → 结构化 SERVICE_UNAVAILABLE（不再裸崩）。"""
    t = TaskTool()

    def boom() -> Any:
        raise RuntimeError("任务服务初始化失败")

    t._get_task_service = boom  # type: ignore[method-assign]
    result = await t.execute({"action": "get", "parent_agent_level": 1})
    assert not result.success
    assert result.error_code == "SERVICE_UNAVAILABLE"
    assert "任务服务初始化失败" in (result.error or "")


async def test_execute_unknown_action_fails(tool: TaskTool) -> None:
    """未知名 action → INVALID_ACTION（可枚举输入取未定义值）。"""
    result = await tool.execute({"action": "explode", "parent_agent_level": 1})
    assert not result.success
    assert result.error_code == "INVALID_ACTION"
    assert "explode" in (result.error or "")


# ─────────────────────────── continue：四场景 ───────────────────────────


async def test_continue_missing_task_id(tool: TaskTool) -> None:
    """continue 缺 task_id → MISSING_TASK_ID。"""
    result = await tool.execute({"action": "continue", "parent_agent_level": 1})
    assert not result.success
    assert result.error_code == "MISSING_TASK_ID"


async def test_continue_task_not_found(tool: TaskTool) -> None:
    """continue 不存在任务 → TASK_NOT_FOUND（state 桥与 YAML 双缺）。"""
    result = await tool.execute(
        {"action": "continue", "task_id": "nope-999999", "parent_agent_level": 1}
    )
    assert not result.success
    assert result.error_code == "TASK_NOT_FOUND"


async def test_continue_running_without_message_fails(tool: TaskTool, svc: Any) -> None:
    """运行中任务 continue 缺 message → MISSING_MESSAGE（不注入不派发）。"""
    task = await _make_task(svc)
    await _set_running(svc, task.id)
    sender = AsyncMock()
    _set_chat(sender)
    result = await tool.execute(
        {"action": "continue", "task_id": task.id, "parent_agent_level": 1}
    )
    assert not result.success
    assert result.error_code == "MISSING_MESSAGE"
    sender.assert_not_awaited()


async def test_continue_running_injects_via_chat_sender(tool: TaskTool, svc: Any) -> None:
    """运行中任务 + message → chat.send_message 注入（task_id=pipeline_id）。"""
    task = await _make_task(svc)
    await _set_running(svc, task.id)
    await svc.bind_pipeline_run(task.id, task.id)
    captured: dict[str, Any] = {}
    sender = AsyncMock(side_effect=lambda params: captured.update(params) or {"ok": True})
    _set_chat(sender)
    result = await tool.execute(
        {
            "action": "continue",
            "task_id": task.id,
            "message": "再检查一遍路径",
            "parent_agent_level": 1,
        }
    )
    assert result.success, result.error
    data = result.output
    assert data["injected"] is True
    assert data["target_pipeline_id"] == task.id
    assert data["message_preview"] == "再检查一遍路径"
    sender.assert_awaited_once()
    assert captured["pipeline_id"] == task.id
    assert captured["background"] is True
    assert captured["message"] == "再检查一遍路径"


async def test_continue_running_without_pipeline_binding(tool: TaskTool, svc: Any) -> None:
    """运行中任务但 pipeline_run_id 未绑定 → MISSING_PIPELINE_ID，不注入。"""
    task = await _make_task(svc)
    await _set_running(svc, task.id)
    sender = AsyncMock()
    _set_chat(sender)
    result = await tool.execute(
        {
            "action": "continue",
            "task_id": task.id,
            "message": "检查一下",
            "parent_agent_level": 1,
        }
    )
    assert not result.success
    assert result.error_code == "MISSING_PIPELINE_ID"
    sender.assert_not_awaited()


async def test_continue_running_chat_sender_absent(tool: TaskTool, svc: Any) -> None:
    """运行中任务注入但 chat 未接线 → trigger=failed 的降级成功结果。"""
    task = await _make_task(svc)
    await _set_running(svc, task.id)
    await svc.bind_pipeline_run(task.id, task.id)
    _set_chat(None)
    result = await tool.execute(
        {
            "action": "continue",
            "task_id": task.id,
            "message": "检查一下",
            "parent_agent_level": 1,
        }
    )
    assert result.success, result.error
    assert result.output["trigger"] == "failed"
    assert "chat capability 未注入" in result.output["error"]


async def test_continue_running_chat_sender_raises(tool: TaskTool, svc: Any) -> None:
    """注入时 chat 抛异常 → 失败留痕在结果，不抛到调用方。"""
    task = await _make_task(svc)
    await _set_running(svc, task.id)
    await svc.bind_pipeline_run(task.id, task.id)
    _set_chat(AsyncMock(side_effect=RuntimeError("sender down")))
    result = await tool.execute(
        {
            "action": "continue",
            "task_id": task.id,
            "message": "检查一下",
            "parent_agent_level": 1,
        }
    )
    assert result.success, result.error
    assert result.output["trigger"] == "failed"
    assert "sender down" in result.output["error"]


async def test_continue_stopped_resumes_status(tool: TaskTool, svc: Any) -> None:
    """stopped 任务 continue → 状态恢复为 running，结果含 old/new 状态。"""
    task = await _make_task(svc)
    await svc.pause_task(task.id)
    executor = AsyncMock()
    _set_exec(executor)
    result = await tool.execute(
        {"action": "continue", "task_id": task.id, "parent_agent_level": 1}
    )
    assert result.success, result.error
    data = result.output
    assert data["resumed"] is True
    assert data["old_status"] == "stopped"
    assert data["new_status"] == "running"
    executor.assert_awaited_once()
    assert executor.await_args is not None
    call_params = executor.await_args.args[0]
    assert call_params["method"] == "resume_pipeline"
    assert call_params["params"]["pipeline_id"] == task.id
    after = svc.get_task(task.id)
    assert after is not None, "resume 不删除任务记录"


async def test_continue_stopped_with_message_marks_injected(tool: TaskTool, svc: Any) -> None:
    """stopped + message → message_injected=True（retry_message 仅内存标记）。"""
    task = await _make_task(svc)
    await svc.pause_task(task.id)
    _set_exec(AsyncMock())
    result = await tool.execute(
        {
            "action": "continue",
            "task_id": task.id,
            "message": "换个思路重来",
            "parent_agent_level": 1,
        }
    )
    assert result.success, result.error
    assert result.output["message_injected"] is True


async def test_continue_stopped_resume_executor_raises(tool: TaskTool, svc: Any, caplog) -> None:
    """resume_pipeline 抛异常 → warning 留痕，仍返回恢复成功。"""
    task = await _make_task(svc)
    await svc.pause_task(task.id)
    _set_exec(AsyncMock(side_effect=RuntimeError("pipe down")))
    with caplog.at_level(logging.WARNING):
        result = await tool.execute(
            {"action": "continue", "task_id": task.id, "parent_agent_level": 1}
        )
    assert result.success, result.error
    assert any("resume_pipeline 失败" in r.getMessage() for r in caplog.records)


async def test_continue_failed_retries(tool: TaskTool, svc: Any) -> None:
    """failed 任务 continue → chat 注入重试消息，结果含 retry 计数。"""
    task = await _make_task(svc)
    await svc.fail_task(task.id, reason="初始失败")
    captured: dict[str, Any] = {}
    _set_chat(AsyncMock(side_effect=lambda p: captured.update(p) or {"ok": True}))
    result = await tool.execute(
        {"action": "continue", "task_id": task.id, "parent_agent_level": 1}
    )
    assert result.success, result.error
    data = result.output
    assert data["retried"] is True
    assert data["old_status"] == "failed"
    assert data["new_status"] == "pending"
    # 工具自增后输出 retry_count+1（首次重试显示 1，语义为本次尝试序数+1）
    assert data["retry_count"] == 2
    assert data["max_retries"] == 6
    assert captured["pipeline_id"] == task.id
    assert "重新执行任务" in captured["message"]


async def test_continue_failed_with_message_appends_correction(tool: TaskTool, svc: Any) -> None:
    """failed + message → 注入消息追加纠正信息。"""
    task = await _make_task(svc)
    await svc.fail_task(task.id, reason="失败")
    captured: dict[str, Any] = {}
    _set_chat(AsyncMock(side_effect=lambda p: captured.update(p) or {}))
    result = await tool.execute(
        {
            "action": "continue",
            "task_id": task.id,
            "message": "参数错了，改用 B 方案",
            "parent_agent_level": 1,
        }
    )
    assert result.success, result.error
    assert "纠正信息：参数错了，改用 B 方案" in captured["message"]


async def test_continue_failed_chat_absent_warns(tool: TaskTool, svc: Any) -> None:
    """重试但 chat 未接线 → warning 字段提示未派发执行。"""
    task = await _make_task(svc)
    await svc.fail_task(task.id, reason="失败")
    _set_chat(None)
    result = await tool.execute(
        {"action": "continue", "task_id": task.id, "parent_agent_level": 1}
    )
    assert result.success, result.error
    assert "chat capability 未注入" in result.output["warning"]


async def test_continue_timeout_retries(tool: TaskTool, svc: Any) -> None:
    """timeout 任务 continue → 同样走重试路径（与 failed 对称）。"""
    from task_types import TaskStatus

    task = await _make_task(svc)
    await _set_running(svc, task.id)
    await svc.force_transition(task.id, TaskStatus.TIMEOUT)
    _set_chat(AsyncMock())
    result = await tool.execute(
        {"action": "continue", "task_id": task.id, "parent_agent_level": 1}
    )
    assert result.success, result.error
    assert result.output["old_status"] == "timeout"
    assert result.output["new_status"] == "pending"


async def test_continue_completed_rejected(tool: TaskTool, svc: Any) -> None:
    """completed 任务 continue → INVALID_STATUS（可枚举状态取不支持值）。"""
    task = await _make_task(svc)
    await svc.complete_task(task.id)
    result = await tool.execute(
        {"action": "continue", "task_id": task.id, "parent_agent_level": 1}
    )
    assert not result.success
    assert result.error_code == "INVALID_STATUS"
    assert "completed" in (result.error or "")


async def test_continue_max_retries_exceeded(tool: TaskTool, svc: Any) -> None:
    """retry_count 达上限 → MAX_RETRIES_EXCEEDED，不再派发。"""
    task = await _make_task(svc, retry_count=6)
    await svc.fail_task(task.id, reason="失败")
    sender = AsyncMock()
    _set_chat(sender)
    result = await tool.execute(
        {"action": "continue", "task_id": task.id, "parent_agent_level": 1}
    )
    assert not result.success
    assert result.error_code == "MAX_RETRIES_EXCEEDED"
    assert "6/6" in (result.error or "")
    sender.assert_not_awaited()


# ─────────────────────────── stop：挂起 + 级联 ───────────────────────────


async def test_stop_missing_task_id(tool: TaskTool) -> None:
    """stop 缺 task_id → MISSING_TASK_ID。"""
    result = await tool.execute({"action": "stop", "parent_agent_level": 1})
    assert not result.success
    assert result.error_code == "MISSING_TASK_ID"


async def test_stop_task_not_found(tool: TaskTool) -> None:
    """stop 任务不存在（state 聚合可用但无此行）→ TASK_NOT_FOUND。"""
    tool._read_state_rows = AsyncMock(return_value=[])  # type: ignore[method-assign]
    result = await tool.execute(
        {"action": "stop", "task_id": "ghost-9999", "parent_agent_level": 1}
    )
    assert not result.success
    assert result.error_code == "TASK_NOT_FOUND"


async def test_stop_not_stoppable_status(tool: TaskTool, svc: Any) -> None:
    """completed 任务不可 stop → INVALID_STATUS（仅 pending/running/suspended）。"""
    task = await _make_task(svc)
    await svc.complete_task(task.id)
    tool._read_state_rows = AsyncMock(  # type: ignore[method-assign]
        return_value=[{"pipeline_id": task.id, "task.status": "completed", "task.goal": "已完成"}]
    )
    result = await tool.execute(
        {"action": "stop", "task_id": task.id, "parent_agent_level": 1}
    )
    assert not result.success
    assert result.error_code == "INVALID_STATUS"
    assert "completed" in (result.error or "")


async def test_stop_suspends_pipeline_and_cascades(tool: TaskTool, svc: Any) -> None:
    """stop → suspend_pipeline 挂起自身 + 级联挂起 lineage 子管道。"""
    parent = await _make_task(svc, title="父")
    child = await _make_task(svc, title="子")
    rows = [
        {
            "pipeline_id": parent.id,
            "task.status": "running",
            "task.goal": "父",
            "lineage.origin_session_id": "sess-p",
            "thread_id": "sess-p",
        },
        {
            "pipeline_id": child.id,
            "task.status": "running",
            "task.goal": "子",
            "lineage.parent_pipeline_id": parent.id,
            "lineage.origin_session_id": "sess-c",
            "thread_id": "sess-c",
        },
    ]
    tool._read_state_rows = AsyncMock(return_value=rows)  # type: ignore[method-assign]
    calls: list[dict[str, Any]] = []

    async def _record(params: dict[str, Any]) -> dict[str, Any]:
        calls.append(params)
        return {}

    _set_exec(AsyncMock(side_effect=_record))
    result = await tool.execute(
        {"action": "stop", "task_id": parent.id, "reason": "用户叫停", "parent_agent_level": 1}
    )
    assert result.success, result.error
    data = result.output
    assert data["stopped"] is True
    assert data["old_status"] == "running"
    assert data["new_status"] == "suspended"
    assert data["reason"] == "用户叫停"
    assert data["cascaded_subtasks"] == 1
    assert [c["method"] for c in calls] == ["suspend_pipeline", "suspend_pipeline"]
    assert calls[0]["params"]["pipeline_id"] == parent.id
    assert calls[1]["params"]["pipeline_id"] == child.id


async def test_stop_no_pipeline_executor(tool: TaskTool, svc: Any) -> None:
    """pipeline-executor 未接线 → 拒绝停止（CONTINUE_FAILED）。"""
    task = await _make_task(svc)
    tool._read_state_rows = AsyncMock(  # type: ignore[method-assign]
        return_value=[{"pipeline_id": task.id, "task.status": "running", "task.goal": "运行中"}]
    )
    _set_exec(None)
    result = await tool.execute(
        {"action": "stop", "task_id": task.id, "parent_agent_level": 1}
    )
    assert not result.success
    assert result.error_code == "CONTINUE_FAILED"
    assert "未注入" in (result.error or "")


async def test_stop_cascade_child_failure_still_succeeds(tool: TaskTool, svc: Any, caplog) -> None:
    """子管道挂起失败 → warning 留痕，父停止结果仍成功。"""
    parent = await _make_task(svc, title="父")
    rows = [
        {"pipeline_id": parent.id, "task.status": "running", "task.goal": "父"},
        {"pipeline_id": "child-1", "task.status": "running", "lineage.parent_pipeline_id": parent.id},
        {"pipeline_id": "child-2", "task.status": "running", "lineage.parent_pipeline_id": parent.id},
    ]
    tool._read_state_rows = AsyncMock(return_value=rows)  # type: ignore[method-assign]
    calls: list[dict[str, Any]] = []

    async def flaky(params: dict[str, Any]) -> dict[str, Any]:
        calls.append(params)
        if params["params"]["pipeline_id"] == "child-2":
            raise RuntimeError("child pipe down")
        return {}

    _set_exec(flaky)
    with caplog.at_level(logging.WARNING):
        result = await tool.execute(
            {"action": "stop", "task_id": parent.id, "parent_agent_level": 1}
        )
    assert result.success, result.error
    assert result.output["cascaded_subtasks"] == 1
    assert any("级联挂起子管道失败" in r.getMessage() for r in caplog.records)


# ─────────────────────────── change：容器状态变更（L1） ───────────────────────────


async def test_change_rejects_l2(tool: TaskTool) -> None:
    """change 仅限 L1：L2 调用 → PERMISSION_DENIED。"""
    result = await tool.execute(
        {"action": "change", "task_id": "t1", "parent_agent_level": 2}
    )
    assert not result.success
    assert result.error_code == "PERMISSION_DENIED"


async def test_change_missing_task_id(tool: TaskTool) -> None:
    """change 缺 task_id → MISSING_TASK_ID。"""
    result = await tool.execute(
        {"action": "change", "parent_agent_level": 1, "target_status": "running"}
    )
    assert not result.success
    assert result.error_code == "MISSING_TASK_ID"


async def test_change_target_status_missing(tool: TaskTool) -> None:
    """change 缺 target_status → INVALID_STATUS（空串提示）。"""
    result = await tool.execute({"action": "change", "task_id": "t1", "parent_agent_level": 1})
    assert not result.success
    assert result.error_code == "INVALID_STATUS"
    assert "(空)" in (result.error or "")


async def test_change_suspend_mapping_aliases(tool: TaskTool) -> None:
    """suspended/stopped/paused 三别名 → suspend_pipeline。"""
    executor = AsyncMock()
    _set_exec(executor)
    for alias in ("suspended", "stopped", "paused"):
        result = await tool.execute(
            {"action": "change", "task_id": "t1", "target_status": alias, "parent_agent_level": 1}
        )
        assert result.success, f"{alias} 应可挂起: {result.error}"
        assert result.output["new_status"] == "suspended"
    assert executor.await_count == 3
    for call in executor.await_args_list:
        assert call.args[0]["method"] == "suspend_pipeline"


async def test_change_suspend_executor_missing(tool: TaskTool) -> None:
    """挂起别名但 executor 未注入 → CONTINUE_FAILED。"""
    _set_exec(None)
    result = await tool.execute(
        {"action": "change", "task_id": "t1", "target_status": "suspended", "parent_agent_level": 1}
    )
    assert not result.success
    assert result.error_code == "CONTINUE_FAILED"


async def test_change_resume_mapping_aliases(tool: TaskTool) -> None:
    """running/resumed 两别名 → resume_pipeline。"""
    executor = AsyncMock()
    _set_exec(executor)
    for alias in ("running", "resumed"):
        result = await tool.execute(
            {"action": "change", "task_id": "t1", "target_status": alias, "parent_agent_level": 1}
        )
        assert result.success, f"{alias} 应可恢复: {result.error}"
        assert result.output["new_status"] == "running"
    assert executor.await_count == 2
    for call in executor.await_args_list:
        assert call.args[0]["method"] == "resume_pipeline"


async def test_change_resume_executor_raises(tool: TaskTool) -> None:
    """resume_pipeline 抛异常 → INVALID_TRANSITION。"""
    _set_exec(AsyncMock(side_effect=RuntimeError("pipe down")))
    result = await tool.execute(
        {"action": "change", "task_id": "t1", "target_status": "running", "parent_agent_level": 1}
    )
    assert not result.success
    assert result.error_code == "INVALID_TRANSITION"
    assert "恢复任务失败" in (result.error or "")


async def test_change_terminal_statuses_rejected(tool: TaskTool) -> None:
    """completed/failed/pending/timeout 终态不可手动设置 → INVALID_STATUS。"""
    for target in ("completed", "failed", "pending", "timeout"):
        result = await tool.execute(
            {"action": "change", "task_id": "t1", "target_status": target, "parent_agent_level": 1}
        )
        assert not result.success
        assert result.error_code == "INVALID_STATUS"
        assert target in (result.error or "")


# ─────────────────────────── 权限矩阵（_check_permission） ───────────────────────────


def _perm_task(**meta: Any) -> Any:
    from task_types import TaskModel, TaskStatus

    return TaskModel(
        id="perm-task",
        title="权限任务",
        status=TaskStatus.PENDING,
        metadata=meta,
        parent_pipeline_id="pipe-parent",
        parent_task_id="pipe-parent",
    )


def test_permission_l1_session_mismatch() -> None:
    """L1：任务 session 与当前会话不符 → 拒绝。"""
    ok, msg = TaskTool._check_permission(
        _perm_task(session_id="sess-other"), 1, {"session_id": "sess-me"}
    )
    assert ok is False
    assert "不属于当前会话" in (msg or "")


def test_permission_l1_session_match_or_absent() -> None:
    """L1：会话一致 / 未传 session_id → 放行。"""
    assert TaskTool._check_permission(
        _perm_task(session_id="sess-me"), 1, {"session_id": "sess-me"}
    ) == (True, None)
    assert TaskTool._check_permission(_perm_task(session_id="sess-other"), 1, {}) == (True, None)


def test_permission_l2_submitted_by_mismatch() -> None:
    """L2：任务由 L1 提交（submitted_by_level=1）→ 拒绝。"""
    ok, msg = TaskTool._check_permission(
        _perm_task(submitted_by_level=1), 2, {"pipeline_id": "pipe-parent"}
    )
    assert ok is False
    assert "L1" in (msg or "") and "L2" in (msg or "")


def test_permission_l2_submitted_by_match() -> None:
    """L2：submitted_by_level=2 → 放行（不校验管道归属）。"""
    assert TaskTool._check_permission(
        _perm_task(submitted_by_level=2), 2, {"pipeline_id": "somewhere-else"}
    ) == (True, None)


def test_permission_l2_pipeline_mismatch() -> None:
    """L2：无 submitted_by 时校验 pipeline 归属，不属于 → 拒绝。"""
    ok, msg = TaskTool._check_permission(_perm_task(), 2, {"pipeline_id": "pipe-else"})
    assert ok is False
    assert "不属于当前管道" in (msg or "")


def test_permission_l2_pipeline_match() -> None:
    """L2：pipeline_id 命中 parent_pipeline_id → 放行。"""
    assert TaskTool._check_permission(
        _perm_task(), 2, {"pipeline_id": "pipe-parent"}
    ) == (True, None)


def test_permission_l2_parent_task_id_match() -> None:
    """L2：parent_task_id 命中 → 放行。"""
    assert TaskTool._check_permission(_perm_task(), 2, {"parent_task_id": "pipe-parent"}) == (
        True,
        None,
    )


def test_permission_l2_parent_task_id_mismatch() -> None:
    """L2：parent_task_id 不符 → 拒绝。"""
    ok, msg = TaskTool._check_permission(_perm_task(), 2, {"parent_task_id": "other-task"})
    assert ok is False
    assert "L2 只能管理自己提交的子任务" in (msg or "")


def test_permission_l2_no_context() -> None:
    """L2：无 pipeline_id 也无 parent_task_id → 拒绝（缺参数）。"""
    ok, msg = TaskTool._check_permission(_perm_task(), 2, {})
    assert ok is False
    assert "缺少 parent_task_id" in (msg or "")


def test_permission_unknown_level() -> None:
    """非 L1/L2 层级 → 拒绝。"""
    ok, msg = TaskTool._check_permission(_perm_task(), 3, {})
    assert ok is False
    assert "L3" in (msg or "")


# ─────────────────────────── _resolve_input_ids（短 id 前缀解析） ───────────────────────────


async def test_resolve_input_ids_resolves_all_keys(tool: TaskTool) -> None:
    """task_id / task_ids / parent_task_id 三入口统一前缀解析回全 id。"""
    rows = [
        {"pipeline_id": "abcdef123456", "task.status": "running"},
        {"pipeline_id": "abcdef654321", "task.status": "running"},
        {"pipeline_id": "zzzzzzzzzzzz", "task.status": "pending", "task.owned.own-123456789.x": 1},
    ]
    tool._read_state_rows = AsyncMock(return_value=rows)  # type: ignore[method-assign]
    inputs: dict[str, Any] = {
        "task_id": "abcdef1234",
        "task_ids": ["abcdef6543", "zzzzzzzzzzzz"],
        "parent_task_id": "own-1234567",
    }
    err = await tool._resolve_input_ids(inputs)
    assert err is None
    assert inputs["task_id"] == "abcdef123456"
    assert inputs["task_ids"] == ["abcdef654321", "zzzzzzzzzzzz"]
    assert inputs["parent_task_id"] == "own-123456789"


async def test_resolve_input_ids_ambiguous(tool: TaskTool) -> None:
    """短前缀多命中 → 歧义错误（task_id / task_ids / parent_task_id 三入口）。"""
    rows = [
        {"pipeline_id": "pipe-shared-aaaa", "task.status": "running"},
        {"pipeline_id": "pipe-shared-bbbb", "task.status": "running"},
    ]
    tool._read_state_rows = AsyncMock(return_value=rows)  # type: ignore[method-assign]
    for key in ("task_id", "task_ids", "parent_task_id"):
        inputs: dict[str, Any] = {key: "pipe-shared-" if key != "task_ids" else ["pipe-shared-"]}
        err = await tool._resolve_input_ids(inputs)
        assert err is not None
        assert "匹配到多个任务" in err
        assert "完整 ID" in err


async def test_resolve_input_ids_state_bridge_unavailable(tool: TaskTool) -> None:
    """聚合桥未就绪（None）→ 原样放行，不报歧义。"""
    tool._read_state_rows = AsyncMock(return_value=None)  # type: ignore[method-assign]
    inputs = {"task_id": "short-prefix"}
    assert await tool._resolve_input_ids(inputs) is None
    assert inputs["task_id"] == "short-prefix"


# ─────────────────────────── 批量操作（continue/stop/delete） ───────────────────────────


async def test_batch_continue_mixed_results(tool: TaskTool, svc: Any) -> None:
    """批量 continue：合法任务重试成功 + 不存在任务逐项失败，汇总计数正确。"""
    task = await _make_task(svc, title="批量重试")
    await svc.fail_task(task.id, reason="失败")
    _set_chat(AsyncMock())
    missing = "missing-123456"
    result = await tool.execute(
        {
            "action": "continue",
            "task_ids": [task.id, missing],
            "parent_agent_level": 1,
        }
    )
    assert result.success, result.error
    data = result.output
    assert data["summary"] == {"total": 2, "success": 1, "failed": 1}
    by_id = {r["task_id"]: r for r in data["results"]}
    assert by_id[task.id]["success"] is True
    assert by_id[task.id]["data"]["retried"] is True
    assert by_id[missing]["success"] is False
    assert by_id[missing]["error"] == "任务不存在: missing-123456"


async def test_batch_unsupported_action(tool: TaskTool) -> None:
    """批量内不支持的 action → 逐项 INVALID_ACTION。"""
    result = await tool._batch_tasks(
        {"action": "explode", "task_ids": ["t1"], "parent_agent_level": 1}, 1
    )
    assert result.success, result.error
    assert result.output["summary"] == {"total": 1, "success": 0, "failed": 1}
    assert result.output["results"][0]["error"] == "不支持的批量操作: explode"


async def test_batch_permission_violation_short_circuits(tool: TaskTool, svc: Any) -> None:
    """批量逐任务预检：越权任务短路返回 [INSUFFICIENT_PERMISSION]，不委派子动作。"""
    task = await _make_task(svc, title="L1 任务")
    executor = AsyncMock()
    _set_exec(executor)
    result = await tool.execute(
        {
            "action": "stop",
            "task_ids": [task.id],
            "parent_agent_level": 2,
            "pipeline_id": "pipe-else",
        }
    )
    assert result.success, result.error
    row = result.output["results"][0]
    assert row["success"] is False
    assert "[INSUFFICIENT_PERMISSION]" in row["error"]
    assert row["data"] is None
    executor.assert_not_awaited()


async def test_batch_stop_success(tool: TaskTool, svc: Any) -> None:
    """批量 stop：两个 running 任务都成功挂起，cascaded 各自独立。"""
    parent = await _make_task(svc, title="父")
    child = await _make_task(svc, title="子")
    rows = [
        {"pipeline_id": parent.id, "task.status": "running", "task.goal": "父"},
        {
            "pipeline_id": child.id,
            "task.status": "running",
            "task.goal": "子",
            "lineage.parent_pipeline_id": parent.id,
        },
    ]
    tool._read_state_rows = AsyncMock(return_value=rows)  # type: ignore[method-assign]
    _set_exec(AsyncMock())
    result = await tool.execute(
        {"action": "stop", "task_ids": [parent.id, child.id], "parent_agent_level": 1}
    )
    assert result.success, result.error
    data = result.output
    assert data["summary"]["total"] == 2
    assert data["summary"]["success"] == 2
    rows_out = {r["task_id"]: r for r in data["results"]}
    assert rows_out[parent.id]["data"]["cascaded_subtasks"] == 1
    assert rows_out[child.id]["data"].get("cascaded_subtasks", 0) == 0


async def test_batch_delete_success(tool: TaskTool, svc: Any) -> None:
    """批量 delete：真实 YAML 任务逐个删除。"""
    t1 = await _make_task(svc, title="A")
    t2 = await _make_task(svc, title="B")
    result = await tool.execute(
        {"action": "delete", "task_ids": [t1.id, t2.id], "parent_agent_level": 1}
    )
    assert result.success, result.error
    data = result.output
    assert data["summary"] == {"total": 2, "success": 2, "failed": 0}
    assert all(r["success"] for r in data["results"])
    assert svc.get_task(t1.id) is None
    assert svc.get_task(t2.id) is None


# ──────────────── state 聚合读面故障：显式区分"读不可用"与"不存在" ────────────────


async def test_get_state_read_error_returns_service_unavailable(
    tool: TaskTool, svc: Any
) -> None:
    """state 桥已注入但读取抛错 → SERVICE_UNAVAILABLE。

    读取故障不得走"任务不存在"路径误导 LLM 重建任务；错误文案不出现
    "不存在"字样。
    """

    async def boom() -> Any:
        raise RuntimeError("pipeline-state 桥故障")

    _task_mod.set_state_reader(boom)

    result = await tool.execute(
        {"action": "get", "task_id": "nope-999999", "parent_agent_level": 1}
    )
    assert not result.success
    assert result.error_code == "SERVICE_UNAVAILABLE"
    assert result.error is not None
    assert "不存在" not in result.error


async def test_get_state_malformed_rows_treated_as_read_error(
    tool: TaskTool, svc: Any
) -> None:
    """state 桥返回非列表形态 → 按"读面形状违约"显式报错，同读不可用处理。"""

    _task_mod.set_state_reader(lambda: "junk-not-a-list")

    result = await tool.execute(
        {"action": "get", "task_id": "nope-999999", "parent_agent_level": 1}
    )
    assert not result.success
    assert result.error_code == "SERVICE_UNAVAILABLE"
    assert result.error is not None
    assert "不存在" not in result.error


async def test_state_bridge_absent_keeps_legacy_fallback(
    tool: TaskTool, svc: Any
) -> None:
    """冻结契约：桥未注入（injection 未接线）→ 回落旧 service，仍报任务不存在。"""
    assert _task_mod._state_reader is None

    result = await tool.execute(
        {"action": "get", "task_id": "ghost-id-000001", "parent_agent_level": 1}
    )
    assert not result.success
    assert result.error_code == "TASK_NOT_FOUND"
