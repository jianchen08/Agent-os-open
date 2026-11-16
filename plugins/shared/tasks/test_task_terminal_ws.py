# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci: python-coverage
"""任务终态 WS 帧（frontend.emit 腿）契约测试（扫描 P-19/U-1 后端接线）。

前端 TASK_COMPLETED/TASK_CANCELLED 零订阅的根因 = 后端无投递腿：域事件
（event-bus.emit_domain）按设计不达前端 WS（capability_router emit_domain
注释钉死），插件到前端的一次性事件出口是 frontend.emit → 内核
session.emit_event（task_deleted 通知先例）。本文件钉两条契约：

- task_completed / task_failed：域事件派生点（events.handle_run_terminal_event）
  同批随发 WS 帧；帧路由坐标 = 任务自身管道（task_deleted 同款，run 启动时
  on_round_start 已注册 route_id → user 映射，目标 user 过滤由
  thread→user 反查承载）；task_failed 帧负载含失败归因（error +
  failure_class/failure_reason，state 已有键直接带出，P-129 失败通知）；
- task_cancelled：停止终态写面（_write_cancel_terminal /
  follow_write_cancel_terminal，写值 = stopped，ADR 2026-10-30 词汇收敛；
  帧名为前端事件词汇原样）终态落账成功后随发；已 stopped 幂等省写即去重，
  写面失败不发射（终态没落账不得谎报停止）。

task_cancelled 不在派生面双发（发射源单点在取消写面）。内核零任务域知识不动。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

# 与 test_tasks_gaps 同款隔离：tasks 目录 + 共享根（plugins/shared）入
# sys.path（模块导入期即垫——下方裸名导入在 collection 期执行），平铺裸名
# 逐出（不逐出会把本插件解析到别家同名实现）。
_HERE = Path(__file__).resolve().parent
_SHARED_ROOT = _HERE.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
if str(_SHARED_ROOT) not in sys.path:
    sys.path.insert(1, str(_SHARED_ROOT))
_EVICT_NAMES = (
    "storage",
    "service",
    "server",
    "http_api",
    "events",
    "state_fields",
)


@pytest.fixture(autouse=True)
def _isolate_tasks_plugin_modules():
    evicted: dict[str, ModuleType] = {}
    for m in _EVICT_NAMES:
        if m in sys.modules:
            evicted[m] = sys.modules.pop(m)
    yield
    for m in _EVICT_NAMES:
        if m in evicted:
            sys.modules[m] = evicted[m]
        else:
            sys.modules.pop(m, None)


import events  # noqa: E402 — sys.path 垫根后裸名导入
import http_api  # noqa: E402 — 同上（_root_api 裸名依赖共享根）


def _task_row(status: str = "completed", **extra: Any) -> dict:
    row = {
        "pipeline_id": "pipe_t1",
        "thread_id": "thread-1",
        "task.id": "pipe_t1",
        "task.goal": "写周报",
        "task.status": status,
        "lineage.parent_pipeline_id": "pipe_parent",
        "task.submitted_by": "admin",
    }
    row.update(extra)
    return row


class _FakeFrontendEmitter:
    """FrontendEmitter 替身：记录 emit 帧并按需抛错（外部依赖桩）。"""

    def __init__(self, available: bool = True, raise_on_emit: bool = False) -> None:
        self.frames: list[tuple[str, dict[str, Any]]] = []
        self._available = available
        self._raise_on_emit = raise_on_emit

    @property
    def available(self) -> bool:
        return self._available

    async def emit(self, event: str, payload: dict[str, Any]) -> None:
        if self._raise_on_emit:
            raise RuntimeError("channel closed")
        self.frames.append((event, payload))


class _FakeStateCap:
    def __init__(self, rows: list[dict], fail_update: bool = False) -> None:
        self._rows = rows
        self._fail_update = fail_update
        self.updates: list[dict] = []

    async def call(self, method: str, params: dict, timeout: float | None = None) -> Any:
        if method == "list":
            return self._rows
        if method == "update":
            if self._fail_update:
                raise RuntimeError("state write down")
            self.updates.append(params)
            return {"ok": True}
        raise AssertionError(f"未预期的能力调用: {method}")


class _FakeFrontendHandle:
    """frontend 能力句柄替身：记录 notify（frontend.emit 的传输面）。"""

    def __init__(self) -> None:
        self.notifies: list[tuple[str, dict[str, Any]]] = []

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        self.notifies.append((method, params))


# ════════════════════════════════════════════════════════════
# 域事件 → WS 帧映射（纯函数契约：负载字段 + 路由坐标）
# ════════════════════════════════════════════════════════════


def _derived_tags(status: str = "completed", **extra: Any) -> tuple[str, dict[str, Any]]:
    return events.derive_task_terminal_events("run.completed", _task_row(status, **extra))[0]


def test_task_completed_maps_to_ws_frame_with_route_keys():
    """task_completed 域事件 → WS 帧：负载含 task_id/title/status，路由坐标 =
    任务自身管道（thread_id=pipeline_id=task_id，task_deleted 先例），目标
    user 经 user_id 随帧带出（thread→user 反查 = user 过滤机制）。"""
    (name, tags) = _derived_tags()
    assert name == "task_completed"
    frame = events.task_terminal_ws_frame(name, tags)
    assert frame is not None
    (event, payload) = frame
    assert event == "task_completed"
    assert payload["task_id"] == "pipe_t1"
    assert payload["title"] == "写周报"
    assert payload["status"] == "completed"
    assert payload["user_id"] == "admin"
    # 路由键：thread 非空且 = 任务自身管道（send_to_thread 反查坐标）
    assert payload["thread_id"] == "pipe_t1"
    assert payload["pipeline_id"] == "pipe_t1"


def test_task_completed_frame_carries_error_only_when_present():
    """failure 信息如有：error 非空随帧带出，干净完成不携带空 error 键。"""
    (_, tags) = _derived_tags()
    frame = events.task_terminal_ws_frame("task_completed", tags)
    assert frame is not None
    assert "error" not in frame[1]
    failed_row = _task_row(
        "completed", **{"task.error": "一处合并备注告警"}
    )
    (_, tags) = events.derive_task_terminal_events("run.completed", failed_row)[0]
    frame = events.task_terminal_ws_frame("task_completed", tags)
    assert frame is not None
    assert frame[1]["error"] == "一处合并备注告警"


def test_unroutable_events_map_to_no_frame():
    """无路由坐标 / 非派生面事件不出帧：completed 与 failed 无 pipeline_id
    均不出帧（路由坐标语义保持）；task_cancelled 发射源在取消写面（不与
    派生面双发）。"""
    assert events.task_terminal_ws_frame("task_completed", {}) is None
    (_, failed_tags) = events.derive_task_terminal_events("run.completed", _task_row("failed"))[0]
    assert events.task_terminal_ws_frame("task_failed", {**failed_tags, "pipeline_id": ""}) is None
    assert events.task_terminal_ws_frame("task_cancelled", failed_tags) is None


def test_task_failed_maps_to_ws_frame_with_failure_attribution():
    """task_failed 域事件 → WS 帧（P-129 失败通知）：负载含任务标识/标题/
    status=failed，失败归因（error + failure_class/failure_reason）非空随帧
    带出，路由坐标 = 任务自身管道（与 task_completed 同款）。"""
    row = _task_row(
        "failed",
        **{
            "task.error": "评估未通过：3 处断言失败",
            "task.failure_class": "eval_rejected",
            "task.failure_reason": "3 处断言失败",
        },
    )
    (name, tags) = events.derive_task_terminal_events("run.failed", row)[0]
    assert name == "task_failed"
    frame = events.task_terminal_ws_frame(name, tags)
    assert frame is not None
    (event, payload) = frame
    assert event == "task_failed"
    assert payload["task_id"] == "pipe_t1"
    assert payload["title"] == "写周报"
    assert payload["status"] == "failed"
    assert payload["user_id"] == "admin"
    # 路由键：thread 非空且 = 任务自身管道（send_to_thread 反查坐标）
    assert payload["thread_id"] == "pipe_t1"
    assert payload["pipeline_id"] == "pipe_t1"
    assert payload["error"] == "评估未通过：3 处断言失败"
    assert payload["failure_class"] == "eval_rejected"
    assert payload["failure_reason"] == "3 处断言失败"


def test_task_failed_frame_omits_empty_attribution_keys():
    """归因缺键（state 无失败注记）：空串归因不进帧——载荷形态与 completed
    同款（空 error 不携带空键）。"""
    (name, tags) = events.derive_task_terminal_events("run.failed", _task_row("failed"))[0]
    frame = events.task_terminal_ws_frame(name, tags)
    assert frame is not None
    assert frame[1]["status"] == "failed"
    for key in ("error", "failure_class", "failure_reason"):
        assert key not in frame[1]


# ════════════════════════════════════════════════════════════
# 派生点随发（handle_run_terminal_event 的 WS 腿）
# ════════════════════════════════════════════════════════════


class _FakeBusCap:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, method: str, params: dict[str, Any]) -> Any:
        self.calls.append((method, params))
        return {"status": "emitted"}


def test_handle_completed_emits_ws_frame_alongside_domain():
    """run.completed 派生 task_completed：emit_domain 与 WS 帧同批，域腿失败
    不影响 WS 腿（独立投递）。"""
    row = _task_row("completed")
    bus = _FakeBusCap()
    emitter = _FakeFrontendEmitter()
    emitted = asyncio.run(
        events.handle_run_terminal_event(
            "run.completed",
            {"pipeline_id": "pipe_t1", "state": row},
            _FakeStateCap([row]),
            bus,
            frontend_emitter=emitter,
        )
    )
    assert emitted == 1
    assert bus.calls[0][0] == "emit_domain"
    assert emitter.frames == [
        (
            "task_completed",
            {
                "task_id": "pipe_t1",
                "title": "写周报",
                "thread_id": "pipe_t1",
                "pipeline_id": "pipe_t1",
                "user_id": "admin",
                "status": "completed",
            },
        )
    ]


def test_handle_task_failed_emits_ws_frame_alongside_domain():
    """run.failed 派生 task_failed（P-129）：域腿照旧，WS 腿随发失败帧
    （负载含任务标识与失败归因）——两腿独立投递同 completed。"""
    row = _task_row(
        "failed",
        **{
            "task.error": "引擎异常退出",
            "task.failure_class": "engine_error",
            "task.failure_reason": "引擎异常退出",
        },
    )
    bus = _FakeBusCap()
    emitter = _FakeFrontendEmitter()
    emitted = asyncio.run(
        events.handle_run_terminal_event(
            "run.failed",
            {"pipeline_id": "pipe_t1", "state": row},
            _FakeStateCap([row]),
            bus,
            frontend_emitter=emitter,
        )
    )
    assert emitted == 1
    assert bus.calls[0][1]["event"] == "task_failed"
    assert len(emitter.frames) == 1
    (event, payload) = emitter.frames[0]
    assert event == "task_failed"
    assert payload["task_id"] == "pipe_t1"
    assert payload["status"] == "failed"
    assert payload["failure_class"] == "engine_error"
    assert payload["failure_reason"] == "引擎异常退出"


def test_handle_emitter_missing_or_failing_is_silent():
    """emitter 缺席（旧内核降级）与发射抛错都不阻断派生主流程。"""
    row = _task_row("completed")
    bus = _FakeBusCap()
    emitted = asyncio.run(
        events.handle_run_terminal_event(
            "run.completed",
            {"pipeline_id": "pipe_t1", "state": row},
            _FakeStateCap([row]),
            bus,
            frontend_emitter=None,
        )
    )
    assert emitted == 1
    broken = _FakeFrontendEmitter(raise_on_emit=True)
    emitted = asyncio.run(
        events.handle_run_terminal_event(
            "run.completed",
            {"pipeline_id": "pipe_t1", "state": row},
            _FakeStateCap([row]),
            bus,
            frontend_emitter=broken,
        )
    )
    assert emitted == 1


# ════════════════════════════════════════════════════════════
# task_cancelled 发射源（取消终态写面，与取消端点同事务面）
# ════════════════════════════════════════════════════════════


def _caps(monkeypatch: pytest.MonkeyPatch, state: _FakeStateCap, frontend: _FakeFrontendHandle):
    handles = {"pipeline-state": state, "frontend": frontend}

    def _get(name: str):
        return handles[name]

    monkeypatch.setattr(http_api, "_capability", _get)


def test_write_cancel_terminal_emits_ws_after_terminal_write(monkeypatch):
    """REST 停止面：终态随写成功后随发 task_cancelled WS 帧（同事务面——
    写面异常不发射，见下一例）。词汇 = stopped（ADR 2026-10-30 词汇收敛；
    帧名 task_cancelled 为前端事件词汇原样保留）。"""
    state = _FakeStateCap([_task_row("running")])
    frontend = _FakeFrontendHandle()
    _caps(monkeypatch, state, frontend)
    asyncio.run(http_api._write_cancel_terminal("pipe_t1"))
    # 终态落账在先（stopped）
    assert state.updates[0]["fields"]["task.status"] == "stopped"
    assert state.updates[0]["pipeline_id"] == "pipe_t1"
    # WS 帧随后，路由与负载契约同 task_completed
    assert len(frontend.notifies) == 1
    (method, params) = frontend.notifies[0]
    assert method == "emit"
    assert params["event"] == "task_cancelled"
    payload = params["payload"]
    assert payload["task_id"] == "pipe_t1"
    assert payload["title"] == "写周报"
    assert payload["status"] == "stopped"
    assert payload["user_id"] == "admin"
    assert payload["thread_id"] == "pipe_t1"
    assert payload["pipeline_id"] == "pipe_t1"


def test_write_cancel_terminal_state_failure_emits_nothing(monkeypatch):
    """终态写失败（RuntimeError 上抛原契约）→ 无 WS 帧（不谎报取消成功）。"""
    state = _FakeStateCap([_task_row("running")], fail_update=True)
    frontend = _FakeFrontendHandle()
    _caps(monkeypatch, state, frontend)
    with pytest.raises(RuntimeError, match="state write down"):
        asyncio.run(http_api._write_cancel_terminal("pipe_t1"))
    assert frontend.notifies == []


def test_write_cancel_terminal_non_task_row_is_refused(monkeypatch):
    """REST 停止面身份门（ADR 2026-11-06 D5 N1b）：非任务管道行（仅提交者
    记账键的会话）拒写终态——主管道不该有 task.* 自身字段，停止动作本身
    不因此失败（INFO 留痕不抛错）。"""
    chat_row = {
        "pipeline_id": "pipe_chat",
        "task.owned.pipe_child.status": "running",
    }
    state = _FakeStateCap([chat_row])
    frontend = _FakeFrontendHandle()
    _caps(monkeypatch, state, frontend)
    asyncio.run(http_api._write_cancel_terminal("pipe_chat"))
    assert state.updates == [], "会话行拒写终态（不造幽灵任务标记）"
    assert frontend.notifies == []


def test_follow_write_cancel_skips_session_row(monkeypatch, caplog):
    """事件路径身份门（ADR 2026-11-06 D5 N1，09:02:01 事故根因）：run.cancelled
    回流命中会话主管道（无 task.id 无 lineage.*）→ 跳过随写 + INFO 留痕，
    幽灵 task.status=stopped 不再落会话。"""
    import logging

    chat_row = {
        "pipeline_id": "pipe_chat",
        "task.status": "completed",
        "task.owned.pipe_child.status": "running",
    }
    state = _FakeStateCap([chat_row])
    frontend = _FakeFrontendHandle()
    _caps(monkeypatch, state, frontend)
    with caplog.at_level(logging.INFO):
        asyncio.run(http_api.follow_write_cancel_terminal("pipe_chat"))
    assert state.updates == [], "会话行跳过停止终态随写"
    assert frontend.notifies == []
    assert any("跳过" in r.getMessage() for r in caplog.records), "跳过必须留痕"


def test_follow_write_cancel_missing_row_never_upserts_ghost(monkeypatch):
    """事件路径身份门第二输入：行缺失（聚合无此管道）→ 不写——写面对未知
    pipeline_id 是 upsert，bogus id 会凭空造出幽灵任务行。"""
    state = _FakeStateCap([])
    frontend = _FakeFrontendHandle()
    _caps(monkeypatch, state, frontend)
    asyncio.run(http_api.follow_write_cancel_terminal("pipe_ghost"))
    assert state.updates == [], "行缺失不得 upsert 造幽灵行"
    assert frontend.notifies == []


def test_follow_write_emits_only_on_real_transition(monkeypatch):
    """事件路径（run.cancelled 回流）：真转移才发射——已 stopped 幂等省写即去重
    （REST 面已发过一次），非任务行不发射。"""
    running = _FakeStateCap([_task_row("running")])
    frontend = _FakeFrontendHandle()
    _caps(monkeypatch, running, frontend)
    asyncio.run(http_api.follow_write_cancel_terminal("pipe_t1"))
    assert running.updates, "自身投影随写发生"
    assert frontend.notifies, "WS 帧随发"
    assert frontend.notifies[0][1]["event"] == "task_cancelled"

    already = _FakeStateCap([_task_row("stopped")])
    frontend2 = _FakeFrontendHandle()
    _caps(monkeypatch, already, frontend2)
    asyncio.run(http_api.follow_write_cancel_terminal("pipe_t1"))
    # 自身投影幂等省写（父管道 owned 镜像恒写是既有行为，不计入）
    assert not any(
        u["pipeline_id"] == "pipe_t1" and "task.status" in u["fields"] for u in already.updates
    )
    assert frontend2.notifies == []

    ghost = _FakeStateCap([])
    frontend3 = _FakeFrontendHandle()
    _caps(monkeypatch, ghost, frontend3)
    asyncio.run(http_api.follow_write_cancel_terminal("pipe_ghost"))
    assert frontend3.notifies == []


def test_follow_write_state_failure_emits_no_frame(monkeypatch):
    """事件路径写面故障（降级告警原契约）→ 不发射。"""
    state = _FakeStateCap([_task_row("running")], fail_update=True)
    frontend = _FakeFrontendHandle()
    _caps(monkeypatch, state, frontend)
    asyncio.run(http_api.follow_write_cancel_terminal("pipe_t1"))
    assert frontend.notifies == []


def test_write_cancel_terminal_frontend_absent_is_silent(monkeypatch):
    """frontend 能力未注入（旧内核降级）→ 取消主流程照走、不推送不报错。"""
    state = _FakeStateCap([_task_row("running")])

    def _get(name: str):
        if name == "frontend":
            raise KeyError(f"capability not injected: {name}")
        return {"pipeline-state": state}[name]

    monkeypatch.setattr(http_api, "_capability", _get)
    asyncio.run(http_api._write_cancel_terminal("pipe_t1"))
    assert state.updates, "终态随写不受 WS 腿缺席影响"


def test_write_cancel_terminal_notify_failure_is_silent(monkeypatch):
    """frontend.emit notify 抛错（通道关闭）→ 静默降级，取消主流程不阻断。"""

    class _BrokenFrontendHandle:
        async def notify(self, method: str, params: dict[str, Any]) -> None:
            raise RuntimeError("channel closed")

    state = _FakeStateCap([_task_row("running")])
    handles = {"pipeline-state": state, "frontend": _BrokenFrontendHandle()}
    monkeypatch.setattr(http_api, "_capability", lambda name: handles[name])
    asyncio.run(http_api._write_cancel_terminal("pipe_t1"))
    assert state.updates, "终态随写不受推送故障影响"
