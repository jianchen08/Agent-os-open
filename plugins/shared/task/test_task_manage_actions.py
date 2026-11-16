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
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parents[2]  # task → shared → plugins → 仓根（平铺布局）
_PLUGIN_PATHS = tuple(
    str(_d)
    for _d in (
        _HERE,
        # 平铺布局（ADR 2026-10-13）：任务域模块权威位 = plugins/shared/tasks/
        _PROJECT_ROOT / "plugins" / "shared" / "tasks",
        # _root_api / 共享平铺模块的包父目录
        _PROJECT_ROOT / "plugins" / "shared",
    )
)
for _d in _PLUGIN_PATHS:
    if _d not in sys.path:
        sys.path.insert(0, _d)
# 收集期 tool 槽位保护：确保解析到本目录的 tool.py（同 test_task_manage.py）。
sys.modules.pop("tool", None)

import tool as _task_mod  # noqa: E402
from tool import StateRowsReadError, TaskTool  # noqa: E402

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
        _server_py = _PROJECT_ROOT / "plugins" / "shared" / "tasks" / "server.py"
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


@pytest.fixture
def svc(tmp_path: Path) -> Any:
    """真实 TaskService（tmp 数据目录，每用例隔离）。"""
    from service import TaskService

    return TaskService(data_dir=str(tmp_path))


@pytest.fixture
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
    prev_writer = _task_mod._state_writer
    yield
    _task_mod._chat_sender = prev_chat
    _task_mod._state_reader = prev_state
    _task_mod._pipeline_executor = prev_exec
    _task_mod._state_writer = prev_writer


def _set_chat(sender: Any) -> None:
    _task_mod.set_chat_sender(sender)


def _set_exec(executor: Any) -> None:
    _task_mod.set_pipeline_executor(executor)


def _set_writer(writer: Any) -> None:
    _task_mod.set_state_writer(writer)


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
    # 队列语义如实声明：注入=受理入队、run 边界消费、停止会退回（防"已注入"被当"已消费"）
    assert data["semantics"] == "queued_until_run_boundary"
    sender.assert_awaited_once()
    assert captured["pipeline_id"] == task.id
    assert captured["background"] is True
    assert captured["message"] == "再检查一遍路径"


async def test_continue_acceptance_criteria_override_writes_state(tool: TaskTool, svc: Any) -> None:
    """continue + acceptance_criteria → 整组覆盖/清评估进度/留痕三键落 state，
    注入消息带覆盖告知（2026-10-23 用户裁定：直接覆盖 + 留痕告知）。"""
    task = await _make_task(svc, submitted_by_level=1)
    await _set_running(svc, task.id)
    await svc.bind_pipeline_run(task.id, task.id)
    writes: list[tuple[str, dict[str, Any]]] = []

    async def writer(pipeline_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        writes.append((pipeline_id, fields))
        return {"success": True}

    _set_writer(writer)
    captured: dict[str, Any] = {}
    sender = AsyncMock(side_effect=lambda params: captured.update(params) or {"ok": True})
    _set_chat(sender)
    new_metrics = {"metrics": [{"metric_id": "m_new", "description": "新口径"}]}
    result = await tool.execute(
        {
            "action": "continue",
            "task_id": task.id,
            "message": "按新口径收尾",
            "acceptance_criteria": new_metrics,
            "parent_agent_level": 1,
        }
    )
    assert result.success, result.error
    assert len(writes) == 1
    target, fields = writes[0]
    assert target == task.id
    assert fields["task.acceptance_criteria"] == new_metrics
    assert fields["task.evaluation"] is None
    assert fields["task.eval_total_calls"] == 0
    assert "L1" in fields["task.acceptance_criteria_overridden"]
    # 覆盖必须让执行面知情，且新指标本体直接拼进注入消息（执行者以消息为准，
    # 不依赖其主动回查 state）
    assert "验收指标已整组覆盖" in captured["message"]
    assert "新验收指标如下" in captured["message"]
    assert '"m_new"' in captured["message"]


async def test_continue_acceptance_criteria_override_permission_gated(
    tool: TaskTool, svc: Any
) -> None:
    """管理面=自己的下级（2026-10-23 用户裁定）：L2 对 L1 提交的任务无管理权，
    不得覆盖指标——自评口子由权限闸闭合。"""
    task = await _make_task(svc, submitted_by_level=1)
    await _set_running(svc, task.id)
    await svc.bind_pipeline_run(task.id, task.id)
    writes: list[Any] = []

    async def writer(pipeline_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        writes.append((pipeline_id, fields))
        return {"success": True}

    _set_writer(writer)
    result = await tool.execute(
        {
            "action": "continue",
            "task_id": task.id,
            "message": "改指标",
            "acceptance_criteria": {"metrics": []},
            "parent_agent_level": 2,
        }
    )
    assert not result.success
    assert result.error_code == "INSUFFICIENT_PERMISSION"
    assert writes == []


async def test_continue_without_acceptance_criteria_no_state_write(
    tool: TaskTool, svc: Any
) -> None:
    """缺省 acceptance_criteria = 不改动（一字不增）：零 state 写、消息不带覆盖提示。"""
    task = await _make_task(svc, submitted_by_level=1)
    await _set_running(svc, task.id)
    await svc.bind_pipeline_run(task.id, task.id)
    writes: list[Any] = []

    async def writer(pipeline_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        writes.append((pipeline_id, fields))
        return {"success": True}

    _set_writer(writer)
    captured: dict[str, Any] = {}
    sender = AsyncMock(side_effect=lambda params: captured.update(params) or {"ok": True})
    _set_chat(sender)
    result = await tool.execute(
        {
            "action": "continue",
            "task_id": task.id,
            "message": "继续",
            "parent_agent_level": 1,
        }
    )
    assert result.success, result.error
    assert writes == []
    assert "验收指标已整组覆盖" not in captured["message"]


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
    """运行中任务注入但 chat 未接线 → INJECT_FAILED 显式失败（消息未入队）。

    假成功禁令（20261019 事故②）：硬编码 injected:true 曾让上级拿谎报去
    汇报成功，纠正消息实际丢失——capability 缺席必须显式报错。
    """
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
    assert not result.success
    assert result.error_code == "INJECT_FAILED"
    assert "未入队" in (result.error or "")
    assert (result.output or {}).get("injected") is not True


async def test_continue_running_chat_sender_raises(tool: TaskTool, svc: Any) -> None:
    """注入时 chat 抛异常 → INJECT_FAILED 显式失败，禁吞异常回假成功。"""
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
    assert not result.success
    assert result.error_code == "INJECT_FAILED"
    assert "sender down" in (result.error or "")
    assert (result.output or {}).get("injected") is not True


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
    """resume_pipeline 抛异常 → 显式失败（不谎报 resumed=True/RUNNING）。"""
    task = await _make_task(svc)
    await svc.pause_task(task.id)
    _set_exec(AsyncMock(side_effect=RuntimeError("pipe down")))
    with caplog.at_level(logging.ERROR):
        result = await tool.execute(
            {"action": "continue", "task_id": task.id, "parent_agent_level": 1}
        )
    assert result.success is False
    assert "resume_pipeline 失败" in result.error
    assert result.metadata.get("resumed") is False
    assert any("resume_pipeline 失败" in r.getMessage() for r in caplog.records)


class _RacingStateReader:
    """竞态模拟读面：前 2 次读（入参解析读 + 分派读）返回 stopped，之后读返回
    指定终态——模拟分派读到 stopped 与恢复派发之间，权威投影被外部写面
    （评估闸门/reconcile 补随写）推到真终态。"""

    def __init__(self, task_id: str, terminal_status: str) -> None:
        self._task_id = task_id
        self._terminal = terminal_status
        self.calls = 0

    def __call__(self) -> list[dict[str, Any]]:
        self.calls += 1
        status = "stopped" if self.calls <= 2 else self._terminal
        return [
            {
                "pipeline_id": self._task_id,
                "task.id": self._task_id,
                "task.goal": "竞态任务",
                "task.status": status,
            }
        ]


class _BoomAfterFirstReader:
    """前 2 次读成功（stopped）、第 3 次起抛 StateRowsReadError
    （守卫读面故障形态：分派读健康、守卫读失败）。"""

    def __init__(self, task_id: str) -> None:
        self._task_id = task_id
        self.calls = 0

    def __call__(self) -> list[dict[str, Any]]:
        self.calls += 1
        if self.calls <= 2:
            return [
                {
                    "pipeline_id": self._task_id,
                    "task.id": self._task_id,
                    "task.goal": "降级任务",
                    "task.status": "stopped",
                }
            ]
        raise StateRowsReadError("state 聚合读取失败")


@pytest.mark.parametrize("terminal_status", ["completed", "failed"])
async def test_continue_stopped_blocked_when_projection_true_terminal(
    tool: TaskTool, svc: Any, terminal_status: str, caplog
) -> None:
    """resume 入口守卫（ADR 2026-10-30 §4.3）：权威投影已是真终态
    （completed/failed）→ 拒绝恢复并留痕，不派发 resume_pipeline
    （completed 裸恢复是幽灵轮形态；failed 重试走 retry/submit 职责面）。"""
    task = await _make_task(svc)
    await svc.pause_task(task.id)
    _task_mod.set_state_reader(_RacingStateReader(task.id, terminal_status))
    executor = AsyncMock()
    _set_exec(executor)
    with caplog.at_level(logging.WARNING):
        result = await tool.execute(
            {"action": "continue", "task_id": task.id, "parent_agent_level": 1}
        )
    assert result.success is False
    assert result.error_code == "INVALID_STATUS"
    assert f"终态 {terminal_status}" in (result.error or "")
    # 真终态拒绝恢复：resume_pipeline 不得派发
    executor.assert_not_awaited()
    assert any(
        "拒绝恢复真终态任务" in r.getMessage() for r in caplog.records
    ), "守卫拒绝必须留 warning 痕"


async def test_continue_stopped_guard_read_failure_degrades_to_allow(
    tool: TaskTool, svc: Any, caplog
) -> None:
    """守卫读面故障 → 降级放行（恢复链是主路径，投影读失败不得拦死恢复；
    tasks http_api._read_task_status 同风格）。"""
    task = await _make_task(svc)
    await svc.pause_task(task.id)
    _task_mod.set_state_reader(_BoomAfterFirstReader(task.id))
    executor = AsyncMock()
    _set_exec(executor)
    with caplog.at_level(logging.WARNING):
        result = await tool.execute(
            {"action": "continue", "task_id": task.id, "parent_agent_level": 1}
        )
    assert result.success, result.error
    assert result.output["resumed"] is True
    executor.assert_awaited_once()
    assert any(
        "恢复守卫读面故障" in r.getMessage() for r in caplog.records
    ), "读面故障降级放行必须留痕"


class _RowVanishingReader:
    """前 2 次读返回 stopped 行、第 3 次起返回不含该任务的行——守卫读面
    「行缺席」形态（聚合翻页/行被清扫窗口）。"""

    def __init__(self, task_id: str) -> None:
        self._task_id = task_id
        self.calls = 0

    def __call__(self) -> list[dict[str, Any]]:
        self.calls += 1
        if self.calls <= 2:
            return [
                {
                    "pipeline_id": self._task_id,
                    "task.id": self._task_id,
                    "task.goal": "翻页任务",
                    "task.status": "stopped",
                }
            ]
        return [{"pipeline_id": "other-task", "task.status": "running"}]


async def test_continue_stopped_guard_row_absent_degrades_to_allow(
    tool: TaskTool, svc: Any
) -> None:
    """守卫读面「权威行缺席」→ 降级放行（行缺席 = 无判据，不拦死恢复）。"""
    task = await _make_task(svc)
    await svc.pause_task(task.id)
    _task_mod.set_state_reader(_RowVanishingReader(task.id))
    executor = AsyncMock()
    _set_exec(executor)
    result = await tool.execute(
        {"action": "continue", "task_id": task.id, "parent_agent_level": 1}
    )
    assert result.success, result.error
    assert result.output["resumed"] is True
    executor.assert_awaited_once()


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


async def test_continue_failed_no_synthetic_user(tool: TaskTool, svc: Any) -> None:
    """1017 事故回归：continue 反调禁止合成用户，身份交内核反调注入。

    无 user_id 形参 → 注入 params 不带 user_id 键（内核以桥 scope 注入身份
    派发）；有真实 user_id 形参（tool_args_inject 注入的调用者身份）→ 如实
    转发（旧内核兼容提示，注入面被内核覆盖）。
    """
    task = await _make_task(svc)
    await svc.fail_task(task.id, reason="失败")
    captured: dict[str, Any] = {}
    _set_chat(AsyncMock(side_effect=lambda p: captured.update(p) or {}))
    await tool.execute(
        {"action": "continue", "task_id": task.id, "parent_agent_level": 1}
    )
    assert "user_id" not in captured, "无真实身份时禁止合成用户（1017 回归）"

    await tool.execute(
        {
            "action": "continue",
            "task_id": task.id,
            "user_id": "u-caller",
            "parent_agent_level": 1,
        }
    )
    assert captured["user_id"] == "u-caller", "真实调用者身份如实转发"


async def test_continue_running_no_synthetic_user(tool: TaskTool, svc: Any) -> None:
    """运行中注入同规：不手写 user_id（合成 "task_manage" 是 1017 根因）。"""
    task = await _make_task(svc)
    await _set_running(svc, task.id)
    await svc.bind_pipeline_run(task.id, task.id)
    captured: dict[str, Any] = {}
    _set_chat(AsyncMock(side_effect=lambda p: captured.update(p) or {}))
    result = await tool.execute(
        {
            "action": "continue",
            "task_id": task.id,
            "message": "先跑回归再收尾",
            "parent_agent_level": 1,
        }
    )
    assert result.success, result.error
    assert captured["message"] == "先跑回归再收尾"
    assert "user_id" not in captured, "注入分支同样禁止合成用户"


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
    """completed 任务无 message continue → INVALID_STATUS（返工必须携带指令）。"""
    task = await _make_task(svc)
    await svc.complete_task(task.id)
    writer = AsyncMock()
    sender = AsyncMock()
    _set_writer(writer)
    _set_chat(sender)
    result = await tool.execute(
        {"action": "continue", "task_id": task.id, "parent_agent_level": 1}
    )
    assert not result.success
    assert result.error_code == "INVALID_STATUS"
    assert "completed" in (result.error or "")
    assert "message" in (result.error or "")
    # 无指令的返工没有意义：不翻转写面、不派发消息（裸恢复正是幽灵轮形态）
    writer.assert_not_awaited()
    sender.assert_not_awaited()


# ─────────────────── continue：completed 返工重开（④A） ───────────────────


def _completed_rows(task_id: str) -> list[dict[str, Any]]:
    """state 聚合里的 completed 任务行（带终态注记，供翻转快照/回滚断言）。"""
    return [
        {
            "pipeline_id": task_id,
            "task.id": task_id,  # 出生协议必写（ADR 2026-11-06 D1 行解析门）
            "task.status": "completed",
            "task.goal": "已完成任务",
            "task.ended_at": "2026-10-19T10:20:32.324Z",
        }
    ]


# 终态翻转写面（retry 与返工重开同款，ADR 2026-10-28 D4/D4-1）：翻 running
# + 清终态注记 + 清上一轮完成证据（返工与重试都是「新的一次尝试」，上一轮
# 评估结论/计数不得对新轮生效——计数残留会让新轮未跑先撞上限）。
_FLIP_FIELDS = {
    "task.status": "running",
    "task.ended_at": None,
    "task.failure_class": None,
    "task.failure_reason": None,
    "task.evaluation": None,
    "task.eval_summary": None,
    "task.eval_total_calls": 0,
    "task.eval_retry_count": None,
}

# 返工重开写面 = 同一批翻转字段（保留别名：两条路径语义同族，断言可读）
_REOPEN_FLIP_FIELDS = dict(_FLIP_FIELDS)

_EVIDENCE_CLEAR_OVERLAY = {
    "task_evaluation_completed": None,
    "evaluation.detected_result": None,
    "evaluate_reminder_total": None,
}


async def test_continue_completed_with_message_reopens(
    tool: TaskTool, svc: Any
) -> None:
    """completed + message → 返工重开：先写面翻转、消息带证据清空 overlay。

    时序契约（与修复③）：写面翻 running 必须先于派发——返工轮闸门读到的
    DB 终态须已是 running，否则会被 DB 检查误收束。
    """
    tool._read_state_rows = AsyncMock(  # type: ignore[method-assign]
        return_value=_completed_rows("reopen-task-0001")
    )
    order: list[tuple[str, dict[str, Any]]] = []

    async def writer_fn(pipeline_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        order.append(("write", dict(fields)))
        return {}

    async def sender_fn(params: dict[str, Any]) -> dict[str, Any]:
        order.append(("send", dict(params)))
        return {}

    _set_writer(writer_fn)
    _set_chat(sender_fn)
    result = await tool.execute(
        {
            "action": "continue",
            "task_id": "reopen-task-0001",
            "message": "素材用错了，改用新版重做",
            "parent_agent_level": 1,
        }
    )
    assert result.success, result.error
    data = result.output
    assert data["reopened"] is True
    assert data["old_status"] == "completed"
    assert data["new_status"] == "running"
    assert data["injected"] is True
    assert data["semantics"] == "queued_until_run_boundary"
    # 三步时序：翻转 → 派发（写面先行）
    assert [step for step, _ in order] == ["write", "send"]
    assert order[0][1] == _REOPEN_FLIP_FIELDS
    send_params = order[1][1]
    assert send_params["pipeline_id"] == "reopen-task-0001"
    assert send_params["background"] is True
    assert send_params["message"] == "素材用错了，改用新版重做"
    # 证据清空 overlay：完成证据键会从恢复快照复活，不清则闸门信号②当轮收束
    assert send_params["state"] == _EVIDENCE_CLEAR_OVERLAY


async def test_continue_completed_reopen_dispatch_failure_rolls_back(
    tool: TaskTool, svc: Any
) -> None:
    """返工派发失败 → 回滚写回翻转前原终态 + INJECT_FAILED 显式失败。"""
    tool._read_state_rows = AsyncMock(  # type: ignore[method-assign]
        return_value=_completed_rows("rollback-task-01")
    )
    writes: list[tuple[str, dict[str, Any]]] = []

    async def writer_fn(pipeline_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        writes.append((pipeline_id, dict(fields)))
        return {}

    _set_writer(writer_fn)
    _set_chat(AsyncMock(side_effect=RuntimeError("dispatch down")))
    result = await tool.execute(
        {
            "action": "continue",
            "task_id": "rollback-task-01",
            "message": "返工指令",
            "parent_agent_level": 1,
        }
    )
    assert not result.success
    assert result.error_code == "INJECT_FAILED"
    assert "dispatch down" in (result.error or "")
    # 两次写面：翻转 → 回滚（原终态原值写回，ended_at 恢复为翻转前读到的值；
    # 完成证据键同批回滚——回滚是真回滚，不留半清状态）
    assert len(writes) == 2
    assert writes[0] == ("rollback-task-01", _REOPEN_FLIP_FIELDS)
    assert writes[1] == (
        "rollback-task-01",
        {
            "task.status": "completed",
            "task.ended_at": "2026-10-19T10:20:32.324Z",
            "task.failure_class": None,
            "task.failure_reason": None,
            "task.evaluation": None,
            "task.eval_summary": None,
            "task.eval_total_calls": None,
            "task.eval_retry_count": None,
        },
    )


async def test_continue_completed_reopen_writer_flip_failure_no_dispatch(
    tool: TaskTool, svc: Any
) -> None:
    """写面翻转失败 → 显式失败（任务保持 completed），不派发消息。"""
    tool._read_state_rows = AsyncMock(  # type: ignore[method-assign]
        return_value=_completed_rows("flipfail-task-01")
    )
    _set_writer(AsyncMock(side_effect=RuntimeError("state write face down")))
    sender = AsyncMock()
    _set_chat(sender)
    result = await tool.execute(
        {
            "action": "continue",
            "task_id": "flipfail-task-01",
            "message": "返工指令",
            "parent_agent_level": 1,
        }
    )
    assert not result.success
    assert "state write face down" in (result.error or "")
    sender.assert_not_awaited()


async def test_continue_completed_reopen_writer_absent_fails(
    tool: TaskTool, svc: Any
) -> None:
    """写面未接线 → 显式失败：不翻转就派发会被闸门③按 DB=completed 误收束。"""
    tool._read_state_rows = AsyncMock(  # type: ignore[method-assign]
        return_value=_completed_rows("nowriter-task-01")
    )
    _set_writer(None)
    sender = AsyncMock()
    _set_chat(sender)
    result = await tool.execute(
        {
            "action": "continue",
            "task_id": "nowriter-task-01",
            "message": "返工指令",
            "parent_agent_level": 1,
        }
    )
    assert not result.success
    assert "未注入" in (result.error or "")
    sender.assert_not_awaited()


# ─────────────── retry 写面对齐：failed/timeout → running（④A 顺手收敛） ───────────────


async def test_continue_failed_retry_flips_db_status_via_writer(
    tool: TaskTool, svc: Any
) -> None:
    """failed 重试（写面已接线）→ 先翻转 task.status=running 再派发，DB 不再留 failed。"""
    task = await _make_task(svc)
    await svc.fail_task(task.id, reason="初始失败")
    writes: list[tuple[str, dict[str, Any]]] = []

    async def writer_fn(pipeline_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        writes.append((pipeline_id, dict(fields)))
        return {}

    _set_writer(writer_fn)
    _set_chat(AsyncMock())
    result = await tool.execute(
        {"action": "continue", "task_id": task.id, "parent_agent_level": 1}
    )
    assert result.success, result.error
    data = result.output
    assert data["retried"] is True
    assert data["new_status"] == "running"
    # 同族分裂收敛：重试派发前 DB 终态已翻 running
    assert writes == [(task.id, _FLIP_FIELDS)]


async def test_continue_failed_retry_dispatch_failure_rolls_back(
    tool: TaskTool, svc: Any
) -> None:
    """重试派发失败（写面已接线）→ 回滚写回 failed 原终态 + 显式失败。"""
    task = await _make_task(svc)
    await svc.fail_task(task.id, reason="初始失败")
    writes: list[tuple[str, dict[str, Any]]] = []

    async def writer_fn(pipeline_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        writes.append((pipeline_id, dict(fields)))
        return {}

    _set_writer(writer_fn)
    _set_chat(AsyncMock(side_effect=RuntimeError("retry dispatch down")))
    result = await tool.execute(
        {"action": "continue", "task_id": task.id, "parent_agent_level": 1}
    )
    assert not result.success
    assert result.error_code == "INJECT_FAILED"
    assert "retry dispatch down" in (result.error or "")
    assert len(writes) == 2
    assert writes[0][1] == _FLIP_FIELDS
    # 聚合桥未接线：注记键读不到 → 回滚只写回读到的 status 原值（不虚构 null）
    assert writes[1] == (task.id, {"task.status": "failed"})


async def test_continue_failed_retry_flip_failure_no_dispatch(
    tool: TaskTool, svc: Any
) -> None:
    """重试写面翻转失败 → 显式失败（任务保持 failed），不派发消息。"""
    task = await _make_task(svc)
    await svc.fail_task(task.id, reason="初始失败")
    _set_writer(AsyncMock(side_effect=RuntimeError("retry write face down")))
    sender = AsyncMock()
    _set_chat(sender)
    result = await tool.execute(
        {"action": "continue", "task_id": task.id, "parent_agent_level": 1}
    )
    assert not result.success
    assert "retry write face down" in (result.error or "")
    assert "任务保持 failed" in (result.error or "")
    sender.assert_not_awaited()


async def test_continue_failed_retry_chat_absent_with_flip_rolls_back(
    tool: TaskTool, svc: Any
) -> None:
    """重试已翻转但 chat 未接线 → 回滚原终态（任务不得停在无人驱动的 running）。"""
    task = await _make_task(svc)
    await svc.fail_task(task.id, reason="初始失败")
    writes: list[tuple[str, dict[str, Any]]] = []

    async def writer_fn(pipeline_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        writes.append((pipeline_id, dict(fields)))
        return {}

    _set_writer(writer_fn)
    _set_chat(None)
    result = await tool.execute(
        {"action": "continue", "task_id": task.id, "parent_agent_level": 1}
    )
    assert not result.success
    assert result.error_code == "INJECT_FAILED"
    assert "未接线" in (result.error or "")
    assert len(writes) == 2
    assert writes[0][1] == _FLIP_FIELDS
    assert writes[1] == (task.id, {"task.status": "failed"})


async def test_continue_failed_retry_clears_evidence_and_reregisters_parent(
    tool: TaskTool, svc: Any
) -> None:
    """failed 重试 = 新的一次尝试：清上一轮评估结论/计数 + 父管道重新挂号。

    用户裁定 2026-10-28：失败任务重新 continue 必须清掉之前的评估状态。
    计数残留（task.eval_total_calls / task.eval_retry_count）会让重试轮未跑
    先撞上限；父挂号不重写则上级不等本轮终态、终态回执也无锚可查。
    """
    task = await _make_task(svc)
    await svc.fail_task(task.id, reason="初始失败")
    _rows = _completed_rows_with_parent(task.id, "parent-pipe-01")
    _rows[0]["task.status"] = "failed"
    tool._read_state_rows = AsyncMock(  # type: ignore[method-assign]
        return_value=_rows
    )
    writes: list[tuple[str, dict[str, Any]]] = []

    async def writer_fn(pipeline_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        writes.append((pipeline_id, dict(fields)))
        return {}

    _set_writer(writer_fn)
    _set_chat(AsyncMock())
    result = await tool.execute(
        {"action": "continue", "task_id": task.id, "parent_agent_level": 1}
    )
    assert result.success, result.error
    # ① 子任务自身写面：翻 running + 清终态注记 + 清完成证据
    assert writes[0][0] == task.id
    assert writes[0][1] == _FLIP_FIELDS
    assert writes[0][1]["task.eval_total_calls"] == 0
    assert writes[0][1]["task.evaluation"] is None
    # ② 父管道重新挂号（上级重新进入等待语义；owned 镜像退役，
    # ADR 2026-11-02-state-key-governance——无镜像写面）
    assert writes[1][0] == "parent-pipe-01"
    assert writes[1][1][f"task.subtasks_pending.{task.id}"]
    assert not any(k.startswith("task.owned.") for k in writes[1][1]), "owned 镜像退役"


async def test_continue_failed_retry_dispatch_failure_clears_registration(
    tool: TaskTool, svc: Any
) -> None:
    """重试派发失败 → 撤销父挂号（父不得空等）+ 回滚原终态 + 显式失败。"""
    task = await _make_task(svc)
    await svc.fail_task(task.id, reason="初始失败")
    _rows = _completed_rows_with_parent(task.id, "parent-pipe-02")
    _rows[0]["task.status"] = "failed"
    tool._read_state_rows = AsyncMock(  # type: ignore[method-assign]
        return_value=_rows
    )
    writes: list[tuple[str, dict[str, Any]]] = []

    async def writer_fn(pipeline_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        writes.append((pipeline_id, dict(fields)))
        return {}

    _set_writer(writer_fn)
    _set_chat(AsyncMock(side_effect=RuntimeError("retry dispatch down")))
    result = await tool.execute(
        {"action": "continue", "task_id": task.id, "parent_agent_level": 1}
    )
    assert not result.success
    assert "retry dispatch down" in (result.error or "")
    # 写面顺序：翻转 → 挂号 → 撤销挂号 → 回滚（owned 镜像退役，撤销只清挂号）
    assert writes[0][0] == task.id
    assert writes[1][0] == "parent-pipe-02"
    assert writes[2][1][f"task.subtasks_pending.{task.id}"] is None
    assert writes[3][0] == task.id and writes[3][1]["task.status"] == "failed"


# ─────────────── 返工重开守卫分支（含纵深防御直连） ───────────────


async def test_continue_pending_status_rejected(tool: TaskTool, svc: Any) -> None:
    """pending 任务 continue → INVALID_STATUS 走兜底拒绝（completed 已独立成场景）。"""
    task = await _make_task(svc)
    result = await tool.execute(
        {"action": "continue", "task_id": task.id, "parent_agent_level": 1}
    )
    assert not result.success
    assert result.error_code == "INVALID_STATUS"
    assert "pending" in (result.error or "")


async def test_continue_completed_reopen_without_pipeline_binding(
    tool: TaskTool, svc: Any
) -> None:
    """completed 任务未绑定执行管道 → MISSING_PIPELINE_ID，不翻转不派发。"""
    task = await _make_task(svc)
    await svc.complete_task(task.id)
    writer = AsyncMock()
    sender = AsyncMock()
    _set_writer(writer)
    _set_chat(sender)
    result = await tool.execute(
        {
            "action": "continue",
            "task_id": task.id,
            "message": "返工指令",
            "parent_agent_level": 1,
        }
    )
    assert not result.success
    assert result.error_code == "MISSING_PIPELINE_ID"
    writer.assert_not_awaited()
    sender.assert_not_awaited()


async def test_reopen_completed_internal_permission_guard(tool: TaskTool) -> None:
    """纵深防御直连：_reopen_completed 自校验归属，绕过 _continue_task 也不裸奔。"""
    from task_types import TaskModel, TaskStatus

    task = TaskModel(
        id="direct-reopen-01",
        title="直连返工",
        status=TaskStatus.COMPLETED,
        metadata={},
        pipeline_run_id="direct-reopen-01",
    )
    writer = AsyncMock()
    _set_writer(writer)
    result = await tool._reopen_completed(task, "指令", 2, {})
    assert not result.success
    assert result.error_code == "INSUFFICIENT_PERMISSION"
    writer.assert_not_awaited()


async def test_continue_completed_reopen_snapshot_read_error_degrades(
    tool: TaskTool, svc: Any
) -> None:
    """快照读取晚故障（翻转前读行失败）→ 仅 status 兜底快照，重开流程照常走。

    前两次读取（入参解析/任务装配）正常返回行，第三次（快照）抛
    StateRowsReadError——降级不得阻断重开（status 是回滚承重键，仍可恢复）。
    """
    rows = _completed_rows("snapfail-task-01")
    tool._read_state_rows = AsyncMock(  # type: ignore[method-assign]
        side_effect=[list(rows), list(rows), StateRowsReadError("late boom")]
    )
    writes: list[tuple[str, dict[str, Any]]] = []

    async def writer_fn(pipeline_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        writes.append((pipeline_id, dict(fields)))
        return {}

    _set_writer(writer_fn)
    _set_chat(AsyncMock())
    result = await tool.execute(
        {
            "action": "continue",
            "task_id": "snapfail-task-01",
            "message": "返工指令",
            "parent_agent_level": 1,
        }
    )
    assert result.success, result.error
    assert result.output["reopened"] is True
    # 快照降级不阻断：翻转与派发照常完成
    assert writes[0][1] == _REOPEN_FLIP_FIELDS


async def test_continue_completed_reopen_rollback_write_also_fails(
    tool: TaskTool, svc: Any
) -> None:
    """返工派发失败且回滚写面也失败 → 显式失败带双因现场（禁吞谎报成功）。"""
    tool._read_state_rows = AsyncMock(  # type: ignore[method-assign]
        return_value=_completed_rows("rbfail-task-001")
    )
    _set_writer(AsyncMock(side_effect=[None, RuntimeError("rollback down")]))
    _set_chat(AsyncMock(side_effect=RuntimeError("dispatch down")))
    result = await tool.execute(
        {
            "action": "continue",
            "task_id": "rbfail-task-001",
            "message": "返工指令",
            "parent_agent_level": 1,
        }
    )
    assert not result.success
    assert result.error_code == "INJECT_FAILED"
    assert "dispatch down" in (result.error or "")
    assert "rollback down" in (result.error or "")
    assert "回滚也失败" in (result.error or "")


# ─────────────── 返工重开：新尝试隔离（D4-1）+ 父管道重新挂号（D4-2） ───────────────
# ADR 2026-10-28 D4：返工重开 = 一次新的尝试——上一轮的完成证据不得对新轮
# 生效，且必须重新挂号父管道（否则上级既不等它、也收不到它的完成通知）。


def _completed_rows_with_parent(
    task_id: str, parent_pipeline_id: str = ""
) -> list[dict[str, Any]]:
    """completed 任务行（带血缘父锚点 + 上一轮完成证据）。

    parent_pipeline_id="" → 根任务（无父锚点，不得凭空造父管道键）。
    """
    row: dict[str, Any] = {
        "pipeline_id": task_id,
        "task.id": task_id,  # 出生协议必写（ADR 2026-11-06 D1 行解析门）
        "task.status": "completed",
        "task.goal": "已完成任务",
        "task.ended_at": "2026-10-19T10:20:32.324Z",
        # 上一轮评估写面（task_evaluate 落库）
        "task.evaluation": {"overall_passed": True, "score": 100},
        "task.eval_summary": "上一轮评估通过",
        "task.eval_total_calls": 3,
        "task.eval_retry_count": {"m1": 2},
    }
    if parent_pipeline_id:
        row["lineage.parent_pipeline_id"] = parent_pipeline_id
    return [row]


async def test_continue_completed_reopen_clears_previous_evaluation_evidence(
    tool: TaskTool, svc: Any
) -> None:
    """返工重开同批清零上一轮完成证据（D4-1）：新一轮不得顶着旧评估结论。

    上一轮 task_evaluate 落的四个证据键（结论/摘要/调用计数/逐指标重试计数）
    必须与终态翻转同批清零——计数归零使新一轮的评估从 0 起（带旧重试计数
    会让新轮直接撞上限 → 误判 failed）。
    """
    tool._read_state_rows = AsyncMock(  # type: ignore[method-assign]
        return_value=_completed_rows_with_parent("evidence-task-01")
    )
    writes: list[tuple[str, dict[str, Any]]] = []

    async def writer_fn(pipeline_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        writes.append((pipeline_id, dict(fields)))
        return {}

    _set_writer(writer_fn)
    _set_chat(AsyncMock())
    result = await tool.execute(
        {
            "action": "continue",
            "task_id": "evidence-task-01",
            "message": "返工指令",
            "parent_agent_level": 1,
        }
    )
    assert result.success, result.error
    flip = writes[0][1]
    assert flip["task.status"] == "running"
    assert flip["task.evaluation"] is None
    assert flip["task.eval_summary"] is None
    assert flip["task.eval_total_calls"] == 0
    assert flip["task.eval_retry_count"] is None


async def test_continue_completed_reopen_registers_parent_pending_subtask(
    tool: TaskTool, svc: Any
) -> None:
    """返工重开向父管道重新挂号（D4-2）：上级重新进入等待语义。

    挂号写面与 task_submit 提交面同款：``task.subtasks_pending.<子管道 id>``
    = 提交时间戳 ISO（性质断言：可解析的 ISO 串，非固定字面值）+
    ``task.owned.<子管道 id>.status`` = running（父侧镜像）。挂号必须先于
    派发——派发后子任务可能瞬间终态，终态事件会把父挂号写 null（回执）。
    """
    tool._read_state_rows = AsyncMock(  # type: ignore[method-assign]
        return_value=_completed_rows_with_parent("parented-task-01", "parent-pipe-01")
    )
    order: list[tuple[str, str, dict[str, Any]]] = []

    async def writer_fn(pipeline_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        order.append(("write", pipeline_id, dict(fields)))
        return {}

    async def sender_fn(params: dict[str, Any]) -> dict[str, Any]:
        order.append(("send", str(params.get("pipeline_id")), dict(params)))
        return {}

    _set_writer(writer_fn)
    _set_chat(sender_fn)
    result = await tool.execute(
        {
            "action": "continue",
            "task_id": "parented-task-01",
            "message": "返工指令",
            "parent_agent_level": 1,
        }
    )
    assert result.success, result.error
    # 时序：翻转 → 父管道挂号 → 派发
    assert [step for step, _, _ in order] == ["write", "write", "send"]
    _, target_pipeline, fields = order[1]
    assert target_pipeline == "parent-pipe-01"
    pending_key = "task.subtasks_pending.parented-task-01"
    assert pending_key in fields
    registered_at = fields[pending_key]
    assert isinstance(registered_at, str)
    assert registered_at
    assert datetime.fromisoformat(registered_at) is not None
    # owned 镜像已退役（ADR 2026-11-02-state-key-governance）：挂号写面
    # 只持挂号键，无镜像写
    assert not any(k.startswith("task.owned.") for k in fields), "owned 镜像退役"


async def test_continue_completed_reopen_without_parent_anchor_skips_registration(
    tool: TaskTool, svc: Any
) -> None:
    """无父锚点（根任务）→ 跳过重新挂号，不凭空造父管道键。"""
    tool._read_state_rows = AsyncMock(  # type: ignore[method-assign]
        return_value=_completed_rows_with_parent("root-task-01")
    )
    writes: list[tuple[str, dict[str, Any]]] = []

    async def writer_fn(pipeline_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        writes.append((pipeline_id, dict(fields)))
        return {}

    _set_writer(writer_fn)
    _set_chat(AsyncMock())
    result = await tool.execute(
        {
            "action": "continue",
            "task_id": "root-task-01",
            "message": "返工指令",
            "parent_agent_level": 1,
        }
    )
    assert result.success, result.error
    # 只写子管道自身（无父管道写面）
    assert [pid for pid, _ in writes] == ["root-task-01"]
    assert not any(
        key.startswith("task.subtasks_pending.")
        for key in writes[0][1]
    )


async def test_continue_completed_reopen_registration_failure_rolls_back(
    tool: TaskTool, svc: Any
) -> None:
    """父管道挂号写失败 → 显式失败（CONTINUE_FAILED）+ 回滚原终态，不派发。

    取舍：挂号失败 = 上级既不等它、也收不到它的终态回执 → 整轮重开不成立
    （返工重开的前提就是「上级重新进入等待语义」）；显式失败让上级知情并可
    重试，不把子任务留在无人等待的 running。
    """
    tool._read_state_rows = AsyncMock(  # type: ignore[method-assign]
        return_value=_completed_rows_with_parent("regfail-task-01", "parent-pipe-02")
    )
    writes: list[tuple[str, dict[str, Any]]] = []

    async def writer_fn(pipeline_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        writes.append((pipeline_id, dict(fields)))
        if pipeline_id == "parent-pipe-02":
            raise RuntimeError("parent registration down")
        return {}

    sender = AsyncMock()
    _set_writer(writer_fn)
    _set_chat(sender)
    result = await tool.execute(
        {
            "action": "continue",
            "task_id": "regfail-task-01",
            "message": "返工指令",
            "parent_agent_level": 1,
        }
    )
    assert not result.success
    assert result.error_code == "CONTINUE_FAILED"
    assert "parent registration down" in (result.error or "")
    # 派发未发生（挂号失败即中止，不让子任务跑在无人等待的状态里）
    sender.assert_not_awaited()
    # 子任务回滚到原终态（写序：翻转 → 父挂号失败 → 回滚）
    assert writes[-1][0] == "regfail-task-01"
    assert writes[-1][1]["task.status"] == "completed"


async def test_continue_completed_reopen_dispatch_failure_clears_parent_registration(
    tool: TaskTool, svc: Any
) -> None:
    """派发失败 → 父管道挂号随撤销（父不得空等一个不会跑的子任务）。"""
    tool._read_state_rows = AsyncMock(  # type: ignore[method-assign]
        return_value=_completed_rows_with_parent("dispatchfail-01", "parent-pipe-03")
    )
    writes: list[tuple[str, dict[str, Any]]] = []

    async def writer_fn(pipeline_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        writes.append((pipeline_id, dict(fields)))
        return {}

    _set_writer(writer_fn)
    _set_chat(AsyncMock(side_effect=RuntimeError("dispatch down")))
    result = await tool.execute(
        {
            "action": "continue",
            "task_id": "dispatchfail-01",
            "message": "返工指令",
            "parent_agent_level": 1,
        }
    )
    assert not result.success
    assert result.error_code == "INJECT_FAILED"
    # 父管道写面 = 挂号撤销（null 回执；owned 镜像退役无回写面）；子任务
    # 回滚在其后（子任务不得停在无人驱动的 running）
    parent_writes = [(pid, f) for pid, f in writes if pid == "parent-pipe-03"]
    assert len(parent_writes) == 2
    fields = parent_writes[-1][1]
    assert fields["task.subtasks_pending.dispatchfail-01"] is None
    assert not any(k.startswith("task.owned.") for k in fields), "owned 镜像退役"
    assert writes[-1][0] == "dispatchfail-01"
    assert writes[-1][1]["task.status"] == "completed"


async def test_continue_completed_reopen_dispatch_and_cleanup_both_fail(
    tool: TaskTool, svc: Any
) -> None:
    """派发失败 + 父管道挂号撤销也失败 → 双因现场入错误文本（禁吞清理失败）。"""
    tool._read_state_rows = AsyncMock(  # type: ignore[method-assign]
        return_value=_completed_rows_with_parent("doublefail-01", "parent-pipe-04")
    )
    calls: list[str] = []

    async def writer_fn(pipeline_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        calls.append(pipeline_id)
        # 第三次写 = 派发失败后的父管道挂号撤销（该次失败）
        if len(calls) == 3:
            raise RuntimeError("cleanup down")
        return {}

    _set_writer(writer_fn)
    _set_chat(AsyncMock(side_effect=RuntimeError("dispatch down")))
    result = await tool.execute(
        {
            "action": "continue",
            "task_id": "doublefail-01",
            "message": "返工指令",
            "parent_agent_level": 1,
        }
    )
    assert not result.success
    assert result.error_code == "INJECT_FAILED"
    assert "dispatch down" in (result.error or "")
    assert "cleanup down" in (result.error or "")


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
        return_value=[{"pipeline_id": task.id, "task.id": task.id, "task.status": "completed", "task.goal": "已完成"}]
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
            "task.id": parent.id,
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
        return_value=[{"pipeline_id": task.id, "task.id": task.id, "task.status": "running", "task.goal": "运行中"}]
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
        {"pipeline_id": parent.id, "task.id": parent.id, "task.status": "running", "task.goal": "父"},
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
        {
            "pipeline_id": parent.id,
            "task.id": parent.id,
            "task.status": "running",
            "task.goal": "父",
        },
        {
            "pipeline_id": child.id,
            "task.id": child.id,
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
