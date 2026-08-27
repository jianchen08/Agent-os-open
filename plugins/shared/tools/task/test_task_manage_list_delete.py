# @feature: FP-0.2.二 内部模块 manifest（task_manage 读面：get 列表/详情 + delete 双路径） | @ci: python-coverage
"""task_manage 读面与删除操作测试（行覆盖补全）。

聚焦：get 列表过滤矩阵（状态/会话/层级/管道/父任务/task_scope/project/limit）、
get 详情（含 evaluation_summary/elapsed/activities 组装）、
delete 双路径（YAML 任务 / state 任务删管道）、耗时/活动摘要辅助函数。

真实依赖：TaskService（tmp 数据目录）+ 真实 TaskStorage；仅 mock 跨进程
capability（pipeline-executor / state 聚合 / 0.1 execution_record_storage
sidecar 存储——本 0.2 架构该服务已废弃返回 None，测试用假存储直连覆盖组装面）。
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

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
sys.modules.pop("tool", None)

import tool as _task_mod  # noqa: E402
from tool import TaskTool  # noqa: E402

# 模块级绑定（收集期路径已就位）：函数内裸名 import 在共跑车道里会因
# sys.modules["state_machine"] 被前序套件替换而命中错误实现。
from state_machine import InvalidTransitionError  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _ensure_plugin_paths():
    """平铺串扰自持：同 test_task_manage.py。"""
    for _d in _PLUGIN_PATHS:
        if _d in sys.path:
            sys.path.remove(_d)
        sys.path.insert(0, _d)
    _saved = {n: sys.modules.get(n) for n in ("service", "http_api")}
    for n in _saved:
        sys.modules.pop(n, None)
    try:
        _server_py = _PROJECT_ROOT / "plugins" / "shared" / "system" / "tasks" / "server.py"
        _spec = importlib.util.spec_from_file_location("tasks_server_preheat_listdel", _server_py)
        if _spec is not None and _spec.loader is not None:
            _mod = importlib.util.module_from_spec(_spec)
            _spec.loader.exec_module(_mod)
    except Exception:  # noqa: BLE001
        pass
    yield
    for n, m in _saved.items():
        sys.modules.pop(n, None)
        if m is not None:
            sys.modules[n] = m


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


@pytest.fixture()
def svc(tmp_path: Path) -> Any:
    from service import TaskService

    return TaskService(data_dir=str(tmp_path))


@pytest.fixture()
def tool(svc: Any) -> TaskTool:
    t = TaskTool()
    t._task_service = svc
    return t


def _state_row(**overrides: Any) -> dict[str, Any]:
    """典型任务管道聚合行（内核 STATE_SUMMARY_KEYS 白名单形状）。"""
    row: dict[str, Any] = {
        "pipeline_id": "pipe-shape",
        "task.status": "running",
        "task.goal": "全形状回归",
        "task.submitted_by": "u1",
        "task.scope": "non_container",
        "task.ended_at": "2026-08-23T10:00:00",
        "lineage.origin_session_id": "sess-shape",
        "lineage.parent_pipeline_id": "pipe-parent",
        "workspace": "workspace/pipe-shape",
        "thread_id": "thread-shape",
    }
    row.update(overrides)
    return row


def _set_reader(tool: TaskTool, rows: list[dict[str, Any]] | None) -> None:
    tool._read_state_rows = AsyncMock(return_value=rows)  # type: ignore[method-assign]


# ─────────────────────────── get 列表过滤矩阵 ───────────────────────────


async def test_get_list_state_priority_and_elapsed(tool: TaskTool) -> None:
    """列表从 state 组装：priority 列取值、latest 动作降级 '-'、耗时 '-'。"""
    _set_reader(tool, [_state_row()])
    result = await tool.execute({"action": "get", "parent_agent_level": 1})
    assert result.success, result.error
    rows = result.output["d"]
    assert len(rows) == 1
    # 简表行形状：[短id, 标题, 状态, 优先级, target_name, 最新动作, 耗时]
    assert rows[0][1] == "全形状回归"
    assert rows[0][2] == "running"
    assert rows[0][3] == 5, "默认 NORMAL 优先级数值=5"
    assert rows[0][5] == "-"
    assert rows[0][6] == "-"


async def test_get_list_status_filter(tool: TaskTool) -> None:
    """status 过滤：不匹配的行剔除、匹配的保留。"""
    _set_reader(
        tool,
        [_state_row(pipeline_id="p-running"), _state_row(**{"pipeline_id": "p-completed", "task.status": "completed"})],
    )
    result = await tool.execute({"action": "get", "status": "completed", "parent_agent_level": 1})
    assert result.success, result.error
    rows = result.output["d"]
    assert len(rows) == 1
    assert rows[0][0] == "p-completed"
    assert rows[0][2] == "completed"


async def test_get_list_l1_session_filter(tool: TaskTool) -> None:
    """L1 会话过滤：任务 session 与当前不符 → 剔除。"""
    _set_reader(
        tool,
        [_state_row(pipeline_id="p-me"), _state_row(**{"pipeline_id": "p-other", "lineage.origin_session_id": "sess-other"})],
    )
    result = await tool.execute(
        {"action": "get", "session_id": "sess-shape", "parent_agent_level": 1}
    )
    assert result.success, result.error
    ids = {r[0] for r in result.output["d"]}
    assert ids == {"p-me"[:12]}


async def test_get_list_l1_show_all_uses_submitted_by_level(tool: TaskTool, svc: Any) -> None:
    """L1 默认只显示自己提交（submitted_by_level≠1 剔除）；show_all 放行。"""
    await svc.create_task(title="L1任务", metadata={"submitted_by_level": 1})
    await svc.create_task(title="L2任务", metadata={"submitted_by_level": 2})
    _set_reader(tool, None)  # state 桥未就绪 → 回落 YAML（metadata 才有 submitted_by_level）
    result = await tool.execute({"action": "get", "parent_agent_level": 1})
    assert result.success, result.error
    assert {r[1] for r in result.output["d"]} == {"L1任务"}
    _set_reader(tool, None)
    result = await tool.execute({"action": "get", "show_all": True, "parent_agent_level": 1})
    assert result.success, result.error
    assert {r[1] for r in result.output["d"]} == {"L1任务", "L2任务"}


async def test_get_list_l2_filters(tool: TaskTool) -> None:
    """L2 过滤：pipeline 归属 / parent_task_id / 无上下文拒绝。"""
    rows = [
        _state_row(pipeline_id="pipe-a"),
        _state_row(**{"pipeline_id": "pipe-child", "lineage.parent_pipeline_id": "pipe-a"}),
        _state_row(**{"pipeline_id": "pipe-other", "lineage.parent_pipeline_id": "pipe-x"}),
    ]
    _set_reader(tool, rows)
    result = await tool.execute(
        {"action": "get", "pipeline_id": "pipe-a", "parent_agent_level": 2}
    )
    assert result.success, result.error
    assert len(result.output["d"]) == 2
    _set_reader(tool, rows)
    result = await tool.execute(
        {"action": "get", "parent_task_id": "pipe-a", "parent_agent_level": 2}
    )
    assert result.success, result.error
    assert len(result.output["d"]) == 1
    _set_reader(tool, rows)
    result = await tool.execute({"action": "get", "parent_agent_level": 2})
    assert result.success, result.error
    assert len(result.output["d"]) == 0, "L2 无上下文 → _check_permission 拒绝全部"


async def test_get_list_project_filter(tool: TaskTool) -> None:
    """project_id 过滤（user parent_task_id 过滤在 actions 文件已测）。"""
    rows = [
        _state_row(pipeline_id="p-a"),
        _state_row(pipeline_id="p-b"),
    ]
    _set_reader(tool, rows)
    result = await tool.execute(
        {"action": "get", "project_id": "proj-1", "parent_agent_level": 1}
    )
    assert result.success, result.error
    assert len(result.output["d"]) == 0, "metadata 无 project_id → 全剔除"


async def test_get_list_limit_truncates_tail(tool: TaskTool) -> None:
    """limit 末端截断：全过滤通过后只留最新 N 条。"""
    _set_reader(tool, [_state_row(pipeline_id=f"p{i}") for i in range(6)])
    result = await tool.execute({"action": "get", "limit": 2, "parent_agent_level": 1})
    assert result.success, result.error
    assert len(result.output["d"]) == 2


async def test_get_list_row_without_pipeline_id_skipped(tool: TaskTool) -> None:
    """聚合行缺 pipeline_id（task.* 字段存在）→ 跳过不崩。"""
    _set_reader(tool, [{"task.status": "running", "task.goal": "无 id 行"}])
    result = await tool.execute({"action": "get", "parent_agent_level": 1})
    assert result.success, result.error
    assert result.output["d"] == []


async def test_get_list_state_unavailable_falls_back_to_service(
    tool: TaskTool, svc: Any
) -> None:
    """聚合桥未就绪（None）→ 回落旧 service 存储（全量拉取）。"""
    await svc.create_task(title="A")
    await svc.create_task(title="B")
    _set_reader(tool, None)
    result = await tool.execute({"action": "get", "parent_agent_level": 1})
    assert result.success, result.error
    assert {r[1] for r in result.output["d"]} == {"A", "B"}


async def test_get_list_exception_returns_list_failed(tool: TaskTool) -> None:
    """列表读取异常 → LIST_FAILED 结构化错误。"""
    async def boom() -> list:
        raise RuntimeError("state down")

    tool._list_all_tasks_sorted = boom  # type: ignore[method-assign]
    result = await tool.execute({"action": "get", "parent_agent_level": 1})
    assert not result.success
    assert result.error_code == "LIST_FAILED"
    assert "state down" in (result.error or "")


# ─────────────────────────── get 详情 ───────────────────────────


async def test_get_detail_rich_assembly(tool: TaskTool, svc: Any) -> None:
    """详情富组装：evaluation_history/fail_reason/ws_meta/计数全走真实任务对象。"""
    task = await svc.create_task(
        title="富详情",
        metadata={
            "workspace": "ws/1",
            "ws_meta": {"path": "/data/ws"},
            "fail_reason": "步骤 2 失败",
            "retry_count": 2,
            "max_retries": 4,
            "evaluation_history": [
                {"passed": False, "summary": "不通过", "evidence": ["e1"], "score": 0.3, "suggestions": ["重写"], "metrics": ["m1"]},
                {"passed": True, "summary": "通过", "evidence": ["e2"], "score": 0.9, "suggestions": [], "metrics": ["m2"]},
            ],
        },
    )
    _set_reader(tool, None)  # state 桥未就绪 → 回落 YAML
    result = await tool.execute(
        {"action": "get", "task_id": task.id, "include_details": True, "parent_agent_level": 1}
    )
    assert result.success, result.error
    data = result.output
    assert data["workspace"] == "ws/1"
    assert data["resolved_workspace"] == "/data/ws"
    assert data["fail_reason"] == "步骤 2 失败"
    assert data["retry_count"] == 2
    assert data["max_retries"] == 4
    ev = data["evaluation_summary"]
    assert ev["passed"] is True
    assert ev["summary"] == "通过"
    assert ev["attempt_count"] == 2
    assert ev["score"] == 0.9
    assert data["hint"] is not None
    assert data["elapsed_seconds"] is None, "无 started_at → 耗时 None"


async def test_get_detail_task_not_found(tool: TaskTool) -> None:
    """详情任务不存在（state/YAML 双缺）→ TASK_NOT_FOUND。"""
    _set_reader(tool, [])
    result = await tool.execute(
        {"action": "get", "task_id": "ghost-123456", "parent_agent_level": 1}
    )
    assert not result.success
    assert result.error_code == "TASK_NOT_FOUND"


async def test_get_detail_permission_denied(tool: TaskTool, svc: Any) -> None:
    """详情越权：L2 访问非本管道任务 → INSUFFICIENT_PERMISSION。"""
    task = await svc.create_task(title="L1 私有")
    _set_reader(tool, None)
    result = await tool.execute(
        {
            "action": "get",
            "task_id": task.id,
            "parent_agent_level": 2,
            "pipeline_id": "other-pipe",
        }
    )
    assert not result.success
    assert result.error_code == "INSUFFICIENT_PERMISSION"


async def test_get_detail_exception_returns_get_failed(tool: TaskTool) -> None:
    """详情读取异常 → GET_FAILED 结构化错误。"""
    async def broken(task_id: str) -> None:
        raise RuntimeError("read broken")

    tool._get_task_from_state = broken  # type: ignore[method-assign]
    result = await tool.execute(
        {"action": "get", "task_id": "x", "parent_agent_level": 1}
    )
    assert not result.success
    assert result.error_code == "GET_FAILED"
    assert "read broken" in (result.error or "")


# ─────────────────────────── 活动摘要（fake sidecar 存储） ───────────────────────────


class _FakeRecord:
    def __init__(self, iteration: int, name: str, rtype: str, content: str) -> None:
        self.iteration = iteration
        self.name = name
        self.type = rtype
        self.content = content
        self.created_at = f"2026-08-25T10:00:{iteration:02d}"


class _FakeStorage:
    def __init__(self, records: list[Any]) -> None:
        self._records = records

    def list_by_pipeline(self, pipeline_id: str) -> list[list[Any]]:
        return [self._records]


async def test_latest_and_recent_activities_assembly(tool: TaskTool) -> None:
    """活动摘要：latest 取末条；recent 限长反转、ai+无name→thinking 映射、摘要截 100。"""
    records = [
        _FakeRecord(i, f"act{i}", "ai" if i % 2 else "tool", "x" * 120) for i in range(6)
    ]
    # 无 name 的 ai 记录 → action 回退 "thinking"（覆盖三元取型分支）
    records.append(_FakeRecord(6, "", "ai", "x" * 120))
    fake = _FakeStorage(records)
    tool._get_execution_record_storage = lambda: fake  # type: ignore[method-assign]
    task = _set_model()
    latest = tool._get_latest_activity(task)
    assert latest is not None
    assert latest["iteration"] == 6
    assert latest["action"] == "ai", "latest 用 name or type 回退（无 thinking 映射）"
    assert len(latest["summary"]) == 100
    assert latest["at"] == "2026-08-25T10:00:06"

    recent = tool._get_recent_activities(task, limit=5)
    assert len(recent) == 5
    assert recent[0]["iteration"] == 6, "recent 倒序（最新在前）"
    assert recent[-1]["iteration"] == 2
    assert any(r["action_type"] == "ai" and r["action"] == "thinking" for r in recent)
    assert any(r["action_type"] == "tool" and r["action"] == "act2" for r in recent)

    wide = tool._get_recent_activities(task, limit=10)
    assert len(wide) == 7, "记录数 ≤ limit 时不截断"


async def test_activities_without_storage_or_pipeline(tool: TaskTool) -> None:
    """存储缺失 / 无 pipeline_run_id → 摘要 None / 空列表（优雅降级）。"""
    assert tool._get_latest_activity(_set_model()) is None
    assert tool._get_recent_activities(_set_model()) == []
    bare = _set_model(pipeline_run_id=None)
    assert tool._get_latest_activity(bare) is None
    assert tool._get_recent_activities(bare) == []


async def test_get_detail_include_agent_calls_filters(tool: TaskTool, svc: Any) -> None:
    """include_agent_calls → 仅 tool 类型活动（自动启用详情）。"""
    records = [_FakeRecord(1, "toolcall", "tool", "run"), _FakeRecord(2, "思考", "ai", "think")]
    tool._get_execution_record_storage = lambda: _FakeStorage(records)  # type: ignore[method-assign]
    task = await svc.create_task(title="调用过滤")
    await svc.bind_pipeline_run(task.id, "pipe-run")
    _set_reader(tool, None)
    result = await tool.execute(
        {"action": "get", "task_id": task.id, "include_agent_calls": True, "parent_agent_level": 1}
    )
    assert result.success, result.error
    acts = result.output["recent_activities"]
    assert len(acts) == 1
    assert acts[0]["action_type"] == "tool"


# ─────────────────────────── 耗时与展示格式 ───────────────────────────


async def test_elapsed_seconds_calculation(tool: TaskTool, monkeypatch: Any) -> None:
    """_calc_elapsed_seconds：无 started_at → None；完成任务用完成时间差；运行中用当前时间。"""
    import datetime as dt

    from task_types import TaskModel

    done = TaskModel(started_at="2026-08-25T10:00:00", completed_at="2026-08-25T10:00:30")
    assert TaskTool._calc_elapsed_seconds(done) == 30.0
    assert TaskTool._calc_elapsed_seconds(TaskModel()) is None
    running = TaskModel(started_at="2026-08-25T10:00:00")

    fake_now = dt.datetime(2026, 8, 25, 10, 1, 0)
    monkeypatch.setattr(
        "datetime.datetime",
        type("FakeDT", (), {"now": staticmethod(lambda: fake_now), "fromisoformat": staticmethod(dt.datetime.fromisoformat)}),
    )
    assert TaskTool._calc_elapsed_seconds(running) == 60.0


def test_format_elapsed_cases() -> None:
    """_format_elapsed 全分支：None/秒/分/小时+余分。"""
    assert TaskTool._format_elapsed(None) == "-"
    assert TaskTool._format_elapsed(0) == "0s"
    assert TaskTool._format_elapsed(59.7) == "59s"
    assert TaskTool._format_elapsed(60) == "1m"
    assert TaskTool._format_elapsed(3599) == "59m"
    assert TaskTool._format_elapsed(3600) == "1h0m"
    assert TaskTool._format_elapsed(3660) == "1h1m"
    assert TaskTool._format_elapsed(7260) == "2h1m"


# ─────────────────────────── delete 双路径 ───────────────────────────


async def test_delete_missing_task_id(tool: TaskTool) -> None:
    """delete 缺 task_id → MISSING_TASK_ID。"""
    result = await tool.execute({"action": "delete", "parent_agent_level": 1})
    assert not result.success
    assert result.error_code == "MISSING_TASK_ID"


async def test_delete_non_container_task(tool: TaskTool, svc: Any) -> None:
    """delete YAML 非容器任务 → hard_delete_task 全链路（记录删除）。"""
    task = await svc.create_task(title="硬删")
    result = await tool.execute(
        {"action": "delete", "task_id": task.id, "reason": "清理", "parent_agent_level": 1}
    )
    assert result.success, result.error
    assert result.output["deleted"] is True
    assert svc.get_task(task.id) is None


async def test_delete_container_metadata_task_hard_deletes(tool: TaskTool, svc: Any) -> None:
    """遗留容器元数据行同样走硬删（软删路径已随容器任务退役）。"""
    task = await svc.create_task(title="遗留容器", metadata={"task_scope": "container"})
    result = await tool.execute(
        {"action": "delete", "task_id": task.id, "parent_agent_level": 1}
    )
    assert result.success, result.error
    assert result.output["deleted"] is True
    assert svc.get_task(task.id) is None


async def test_delete_state_task_via_pipeline_executor(tool: TaskTool) -> None:
    """delete state 任务（YAML 无记录）→ 调 delete_pipeline 删管道数据。"""
    _set_reader(tool, [_state_row(pipeline_id="pipe-state")])
    executor = AsyncMock()
    _task_mod.set_pipeline_executor(executor)
    result = await tool.execute(
        {"action": "delete", "task_id": "pipe-state", "parent_agent_level": 1}
    )
    assert result.success, result.error
    assert result.output["deleted"] is True
    executor.assert_awaited_once()
    assert executor.await_args is not None
    call_params = executor.await_args.args[0]
    assert call_params["method"] == "delete_pipeline"
    assert call_params["params"]["pipeline_id"] == "pipe-state"


async def test_delete_state_task_without_executor(tool: TaskTool) -> None:
    """state 任务删除但 executor 未注入 → SERVICE_UNAVAILABLE。"""
    _set_reader(tool, [_state_row(pipeline_id="pipe-x")])
    _task_mod.set_pipeline_executor(None)
    result = await tool.execute(
        {"action": "delete", "task_id": "pipe-x", "parent_agent_level": 1}
    )
    assert not result.success
    assert result.error_code == "SERVICE_UNAVAILABLE"


async def test_delete_state_task_executor_raises(tool: TaskTool) -> None:
    """delete_pipeline 抛异常 → DELETE_FAILED 留痕。"""
    _set_reader(tool, [_state_row(pipeline_id="pipe-y")])
    _task_mod.set_pipeline_executor(AsyncMock(side_effect=RuntimeError("kernel down")))
    result = await tool.execute(
        {"action": "delete", "task_id": "pipe-y", "parent_agent_level": 1}
    )
    assert not result.success
    assert result.error_code == "DELETE_FAILED"
    assert "kernel down" in (result.error or "")


async def test_delete_permission_denied(tool: TaskTool, svc: Any) -> None:
    """delete 越权 → INSUFFICIENT_PERMISSION。"""
    task = await svc.create_task(title="私有")
    result = await tool.execute(
        {
            "action": "delete",
            "task_id": task.id,
            "parent_agent_level": 2,
            "pipeline_id": "other-pipe",
        }
    )
    assert not result.success
    assert result.error_code == "INSUFFICIENT_PERMISSION"


async def test_delete_service_exception_returns_delete_failed(tool: TaskTool, svc: Any) -> None:
    """删除过程异常 → DELETE_FAILED（服务层真实异常被结构化包装）。"""
    task = await svc.create_task(title="删失败")

    async def boom(task_id: str, reason: str = "用户请求删除") -> dict[str, Any]:
        raise RuntimeError("disk error")

    import unittest.mock as mock

    with mock.patch.object(svc, "hard_delete_task", boom):
        result = await tool.execute(
            {"action": "delete", "task_id": task.id, "parent_agent_level": 1}
        )
    assert not result.success
    assert result.error_code == "DELETE_FAILED"
    assert "disk error" in (result.error or "")


# ─────────────────────────── 其它控制面分支 ───────────────────────────


async def test_continue_permission_denied(tool: TaskTool, svc: Any) -> None:
    """continue 越权 → INSUFFICIENT_PERMISSION。"""
    task = await svc.create_task(title="私有")
    result = await tool.execute(
        {
            "action": "continue",
            "task_id": task.id,
            "parent_agent_level": 2,
            "pipeline_id": "other-pipe",
        }
    )
    assert not result.success
    assert result.error_code == "INSUFFICIENT_PERMISSION"


async def test_stop_permission_denied(tool: TaskTool, svc: Any) -> None:
    """stop 越权（state 任务）→ INSUFFICIENT_PERMISSION。"""
    task = await svc.create_task(title="私有")
    _set_reader(tool, [_state_row(pipeline_id=task.id, task_scope="non_container")])
    result = await tool.execute(
        {
            "action": "stop",
            "task_id": task.id,
            "parent_agent_level": 2,
            "pipeline_id": "other-pipe",
        }
    )
    assert not result.success
    assert result.error_code == "INSUFFICIENT_PERMISSION"


async def test_continue_invalid_transition_wrapped(tool: TaskTool) -> None:
    """continue 链内抛 InvalidTransitionError → INVALID_TRANSITION 结构化错误。"""

    async def broken(task_id: str) -> None:
        raise InvalidTransitionError("running", "pending")

    tool._get_task_from_state = broken  # type: ignore[method-assign]
    result = await tool.execute(
        {"action": "continue", "task_id": "t1", "parent_agent_level": 1}
    )
    assert not result.success
    assert result.error_code == "INVALID_TRANSITION"
    assert "状态转换不合法" in (result.error or "")


async def test_inject_permission_denied_direct(tool: TaskTool) -> None:
    """_inject_to_running 入口显式校验：越权直接调用也不裸奔。"""
    from task_types import TaskModel, TaskStatus

    task = TaskModel(id="t-inj", title="注入", status=TaskStatus.RUNNING, metadata={})
    result = await tool._inject_to_running(task, "消息", 2, {"pipeline_id": "x"})
    assert not result.success
    assert result.error_code == "INSUFFICIENT_PERMISSION"


async def test_resume_permission_denied_direct(tool: TaskTool, svc: Any) -> None:
    """_resume_from_stopped 入口前置校验：越权直接调用被拒。"""
    from task_types import TaskModel, TaskStatus

    task = TaskModel(id="t-resume", title="恢复", status=TaskStatus.STOPPED, metadata={})
    result = await tool._resume_from_stopped(task, "", svc, 2, {"pipeline_id": "x"})
    assert not result.success
    assert result.error_code == "INSUFFICIENT_PERMISSION"


async def test_retry_permission_denied_direct(tool: TaskTool, svc: Any) -> None:
    """_retry_from_terminal 入口前置校验：越权直接调用被拒。"""
    from task_types import TaskModel, TaskStatus

    task = TaskModel(id="t-retry", title="重试", status=TaskStatus.FAILED, metadata={})
    result = await tool._retry_from_terminal(task, "", svc, 2, {"pipeline_id": "x"})
    assert not result.success
    assert result.error_code == "INSUFFICIENT_PERMISSION"


async def test_resume_with_none_metadata_and_message(tool: TaskTool) -> None:
    """metadata=None 的 stopped 任务带 message 恢复 → 不崩且 message_injected。"""
    from task_types import TaskModel, TaskStatus

    task = TaskModel(id="t-meta", title="无元数据", status=TaskStatus.STOPPED, metadata=None)
    _task_mod.set_pipeline_executor(AsyncMock())
    result = await tool._resume_from_stopped(task, "恢复提示", MagicMock(), 1, {})
    assert result.success, result.error
    assert task.metadata == {"retry_message": "恢复提示"}
    assert result.output["message_injected"] is True


async def test_retry_chat_sender_raises_warns(tool: TaskTool, caplog: Any) -> None:
    """重试时 chat 注入抛异常 → warning 留痕 + 结果含 warning 字段。"""
    from task_types import TaskModel, TaskStatus

    task = TaskModel(id="t-rc", title="重试崩", status=TaskStatus.FAILED, metadata={})
    _task_mod.set_chat_sender(AsyncMock(side_effect=RuntimeError("chat down")))
    with caplog.at_level(logging.WARNING):
        result = await tool._retry_from_terminal(task, "", MagicMock(), 1, {})
    assert result.success, result.error
    assert "chat down" in result.output["warning"]
    assert any("retry 注入失败" in r.getMessage() for r in caplog.records)


async def test_stop_parent_suspend_raises_warning(tool: TaskTool, svc: Any, caplog: Any) -> None:
    """父管道挂起失败 → warning 留痕，继续级联。"""
    parent = await svc.create_task(title="父")
    _set_reader(
        tool,
        [
            {"pipeline_id": parent.id, "task.status": "running", "task.goal": "父"},
            {"pipeline_id": "child-1", "task.status": "running", "lineage.parent_pipeline_id": parent.id},
        ],
    )
    calls: list[str] = []

    async def flaky(params: dict[str, Any]) -> dict[str, Any]:
        calls.append(params["params"]["pipeline_id"])
        if params["params"]["pipeline_id"] == parent.id:
            raise RuntimeError("suspend down")
        return {}

    _task_mod.set_pipeline_executor(flaky)
    with caplog.at_level(logging.WARNING):
        result = await tool.execute(
            {"action": "stop", "task_id": parent.id, "parent_agent_level": 1}
        )
    assert result.success, result.error
    assert result.output["cascaded_subtasks"] == 1
    assert calls == [parent.id, "child-1"]
    assert any("suspend_pipeline 失败" in r.getMessage() for r in caplog.records)


async def test_stop_child_row_without_pipeline_id_skipped(tool: TaskTool, svc: Any) -> None:
    """级联行缺 pipeline_id → 跳过（不调 executor）。"""
    parent = await svc.create_task(title="父")
    _set_reader(
        tool,
        [
            {"pipeline_id": parent.id, "task.status": "running", "task.goal": "父"},
            {"lineage.parent_pipeline_id": parent.id, "task.status": "running"},
        ],
    )
    executor = AsyncMock()
    _task_mod.set_pipeline_executor(executor)
    result = await tool.execute(
        {"action": "stop", "task_id": parent.id, "parent_agent_level": 1}
    )
    assert result.success, result.error
    assert result.output.get("cascaded_subtasks", 0) == 0
    assert executor.await_count == 1, "仅父管道一次挂起"


async def test_stop_generic_exception_wrapped(tool: TaskTool) -> None:
    """stop 未知异常 → STOP_FAILED 结构化错误。"""
    async def broken(task_id: str) -> None:
        raise RuntimeError("state broken")

    tool._get_task_from_state = broken  # type: ignore[method-assign]
    result = await tool.execute({"action": "stop", "task_id": "x", "parent_agent_level": 1})
    assert not result.success
    assert result.error_code == "STOP_FAILED"
    assert "state broken" in (result.error or "")


async def test_change_suspend_executor_raises(tool: TaskTool) -> None:
    """change 挂起 executor 抛异常 → INVALID_TRANSITION。"""
    _task_mod.set_pipeline_executor(AsyncMock(side_effect=RuntimeError("suspend down")))
    result = await tool.execute(
        {"action": "change", "task_id": "t1", "target_status": "suspended", "parent_agent_level": 1}
    )
    assert not result.success
    assert result.error_code == "INVALID_TRANSITION"
    assert "挂起任务失败" in (result.error or "")


async def test_change_resume_executor_missing(tool: TaskTool) -> None:
    """change 恢复但 executor 未注入 → CONTINUE_FAILED。"""
    _task_mod.set_pipeline_executor(None)
    result = await tool.execute(
        {"action": "change", "task_id": "t1", "target_status": "running", "parent_agent_level": 1}
    )
    assert not result.success
    assert result.error_code == "CONTINUE_FAILED"


# ─────────────────────────── 其它内部入口 ───────────────────────────


async def test_read_state_rows_exception_raises_read_error(
    tool: TaskTool, caplog: Any
) -> None:
    """state 读面抛异常 → StateRowsReadError（与"无任务"显式区分）+ warning 留痕。"""

    async def broken() -> list[dict[str, Any]]:
        raise RuntimeError("read rows broken")

    _task_mod.set_state_reader(broken)
    with caplog.at_level(logging.WARNING), pytest.raises(
        _task_mod.StateRowsReadError
    ) as exc_info:
        await tool._read_state_rows()
    assert "read rows broken" in str(exc_info.value)
    assert any("state 聚合读取失败" in r.getMessage() for r in caplog.records)


async def test_read_state_rows_non_list_raises_read_error(tool: TaskTool) -> None:
    """读面返回非 list（如 dict）→ 形状违约按读取故障处理，不降级成"无任务"。"""
    _task_mod.set_state_reader(lambda: {"pipeline_id": "x"})
    with pytest.raises(_task_mod.StateRowsReadError):
        await tool._read_state_rows()


async def test_get_all_tasks_uses_service(tool: TaskTool, svc: Any) -> None:
    """_get_all_tasks：service 拉取（limit + reverse）。"""
    await svc.create_task(title="A")
    await svc.create_task(title="B")
    tasks = await tool._get_all_tasks(limit=5)
    assert {t.title for t in tasks} == {"A", "B"}
    assert len(tasks) == 2


async def test_list_all_tasks_fallback_to_service(tool: TaskTool, svc: Any) -> None:
    """_list_all_tasks_sorted：state 桥未就绪回落 service（limit=10_000）。"""
    await svc.create_task(title="C")
    _set_reader(tool, None)
    tasks = await tool._list_all_tasks_sorted()
    assert [t.title for t in tasks] == ["C"]


async def test_get_task_service_init_failure(tool: TaskTool, monkeypatch: Any) -> None:
    """_get_task_service 初始化失败 → RuntimeError（execute 层转 SERVICE_UNAVAILABLE）。"""
    import service_access

    monkeypatch.setattr(service_access, "get_task_service", lambda: None)
    fresh = TaskTool()
    with pytest.raises(RuntimeError):
        fresh._get_task_service()


async def test_resolve_ambiguous_via_execute(tool: TaskTool) -> None:
    """execute 入口：短 id 多命中 → AMBIGUOUS_TASK_ID。"""
    _set_reader(
        tool,
        [
            {"pipeline_id": "pipe-shared-aaaa", "task.status": "running"},
            {"pipeline_id": "pipe-shared-bbbb", "task.status": "running"},
        ],
    )
    result = await tool.execute(
        {"action": "get", "task_id": "pipe-shared", "parent_agent_level": 1}
    )
    assert not result.success
    assert result.error_code == "AMBIGUOUS_TASK_ID"
    assert "匹配到多个任务" in (result.error or "")


async def test_resolve_task_ids_non_str_passthrough(tool: TaskTool) -> None:
    """task_ids 列表含非 str 元素 → 原样透传（不崩）。"""
    _set_reader(tool, [{"pipeline_id": "p1", "task.status": "running"}])
    inputs = {"task_ids": [123, "p1"]}
    err = await tool._resolve_input_ids(inputs)
    assert err is None
    assert inputs["task_ids"] == [123, "p1"]


def test_state_reader_setter_registered() -> None:
    """set_state_reader 接线：全局读面被替换。"""
    _task_mod.set_state_reader(lambda: [{"pipeline_id": "x"}])
    assert _task_mod._state_reader is not None
    assert _task_mod._state_reader() == [{"pipeline_id": "x"}]


# ─────────────────────────── 末段补全（行覆盖 98% 余分支） ───────────────────────────


def test_get_tool_definition_shape() -> None:
    """get_tool_definition：静态定义形状（name/action 枚举/权限级别限制）。"""
    from agentos_plugin_sdk import ToolCategory, ToolLevel, ToolSource

    tdef = TaskTool.get_tool_definition()
    assert tdef.name == "task_manage"
    assert tdef.category == ToolCategory.TASK
    assert tdef.level == ToolLevel.SYSTEM
    assert tdef.source == ToolSource.CODE
    assert "action" in tdef.input_schema["properties"]
    assert tdef.param_level_restrictions["action"]["enum_restrictions"]["change"] == 1


async def test_get_list_user_parent_task_id_filter(tool: TaskTool) -> None:
    """user parent_task_id（L1）过滤：父不匹配的行被剔除。"""
    rows = [
        _state_row(**{"pipeline_id": "p-a", "lineage.parent_pipeline_id": "root-a"}),
        _state_row(**{"pipeline_id": "p-b", "lineage.parent_pipeline_id": "root-b"}),
    ]
    _set_reader(tool, rows)
    result = await tool.execute(
        {"action": "get", "parent_task_id": "root-a", "parent_agent_level": 1}
    )
    assert result.success, result.error
    ids = {r[0] for r in result.output["d"]}
    assert ids == {"p-a"[:12]}


async def test_get_detail_from_state_with_ws_meta(tool: TaskTool) -> None:
    """state 详情：ws_meta dict 透传进 metadata → resolved_workspace。"""
    _set_reader(
        tool,
        [_state_row(**{"ws_meta": {"path": "/data/ws2", "id": 1}})],
    )
    result = await tool.execute(
        {"action": "get", "task_id": "pipe-shape", "parent_agent_level": 1}
    )
    assert result.success, result.error
    assert result.output["resolved_workspace"] == "/data/ws2"


async def test_get_list_from_state_with_ws_meta(tool: TaskTool) -> None:
    """state 列表组装时 ws_meta dict 进 metadata（简表不展示但组装不崩）。"""
    _set_reader(tool, [_state_row(**{"ws_meta": {"path": "/data/ws"}})])
    result = await tool.execute({"action": "get", "parent_agent_level": 1})
    assert result.success, result.error
    assert len(result.output["d"]) == 1


async def test_latest_activity_empty_records(tool: TaskTool) -> None:
    """存储有行但记录为空列表 → latest 返回 None。"""
    tool._get_execution_record_storage = lambda: _FakeStorage([])  # type: ignore[method-assign]
    assert tool._get_latest_activity(_set_model()) is None


async def test_continue_generic_exception_wrapped(tool: TaskTool) -> None:
    """continue 链内未知异常 → CONTINUE_FAILED（不透传裸异常）。"""
    async def broken(task_id: str) -> None:
        raise ValueError("boom")

    tool._get_task_from_state = broken  # type: ignore[method-assign]
    result = await tool.execute(
        {"action": "continue", "task_id": "t1", "parent_agent_level": 1}
    )
    assert not result.success
    assert result.error_code == "CONTINUE_FAILED"
    assert "boom" in (result.error or "")


async def test_stop_invalid_transition_wrapped(tool: TaskTool) -> None:
    """stop 链内抛 InvalidTransitionError → INVALID_TRANSITION。"""

    _set_reader(tool, [])  # resolve 阶段读面正常

    async def broken(task_id: str) -> None:
        raise InvalidTransitionError("running", "stopped")

    tool._get_task_from_state = broken  # type: ignore[method-assign]
    result = await tool.execute(
        {"action": "stop", "task_id": "t1", "parent_agent_level": 1}
    )
    assert not result.success
    assert result.error_code == "INVALID_TRANSITION"
    assert "状态转换不合法" in (result.error or "")


async def test_delete_state_task_not_found(tool: TaskTool) -> None:
    """delete 双缺（YAML 无记录 + state 无行）→ TASK_NOT_FOUND。"""
    _set_reader(tool, [])
    result = await tool.execute(
        {"action": "delete", "task_id": "ghost-123456", "parent_agent_level": 1}
    )
    assert not result.success
    assert result.error_code == "TASK_NOT_FOUND"


def _set_model(**overrides: Any) -> Any:
    from task_types import TaskModel, TaskStatus

    return TaskModel(
        id="t-full",
        title="活动任务",
        status=TaskStatus.RUNNING,
        metadata={},
        pipeline_run_id=overrides.pop("pipeline_run_id", "pipe-run"),
        **overrides,
    )
