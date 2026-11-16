# @feature: FP-0.2.〇 僵尸投影 cancel 终态随写 | @ci: python-coverage
"""cancel 终态随写行为测试（谁终态谁随写——僵尸投影 cancel 无效修复）。

缺陷实证（2026-10-08 装机版二度实锤）：孤儿任务（state 行 task.status=
running、引擎侧运行簿记已消失）cancel 返回 200 cancelled=true，复查 GET
status 依旧 running——cancel 只向引擎发停止信号，漏掉「state 行终态随写」
半步（F1 runs 退役收尾「谁终态谁随写」原则的反面案例：cancel 是合法终态
写面之一，必须随写）。

缺陷 I 二度实证（R2 移动端 2026-10-15）：随写词汇曾是可重入态争点。
词汇收敛（ADR 2026-10-30-task-status-authority-vocabulary-convergence）
后取消终态统一写 ``stopped``（用户停止与等子任务/重启排空三态同词，
可重入由恢复守卫接管：真终态 completed/failed 拒绝唤醒，stopped 可被
自然唤醒是设计意图；run-cancelled 事件回流走 follow_write 的幂等省写）。

行为契约（断输入→输出/副作用，不钉实现）：

- 孤儿投影（引擎侧无活跃运行，suspend 幂等返回空 run_id）→ 200 且
  task.status=stopped 随写 state 行（含 task.ended_at）。
- 活任务（suspend 返回 run_id）→ 引擎停止 + 终态随写两步都发生。
- 引擎停止失败 → 503 如实报错，不落终态、不谎报 cancelled。
- 终态随写失败 → 503 如实报错（引擎已停也不谎报 cancelled）。
- completed（评估闸门/用户侧裁决终态）→ 409 拒绝，引擎与写面零调用。
- state 行缺席 → 404 且引擎/写面零调用（写面对未知 pipeline_id 是
  upsert，不设存在性闸会为 bogus id 凭空造幽灵任务行）。
- 级联：父 + lineage 子管道都停止且都随写终态。

走完整 handle_http 分发；伪对象只桩内核 capability 句柄面（外部依赖）。
模块隔离与 test_tasks_http_api.py 同范式（裸名逐出 + 代际还原）。
"""
from __future__ import annotations

import base64
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parents[4] / "plugins" / "shared" / "tasks"

# 任务域写面七态词汇（ADR 2026-10-30-task-status-authority-vocabulary-
# convergence：cancelled 退役；取消终态写 ``stopped``，与等子任务/重启
# 排空同词，可重入由恢复守卫 + follow_write 幂等省写接管）。
_WRITE_FACE_VOCABULARY = {
    "pending", "running", "evaluating", "stopped",
    "completed", "failed", "timeout",
}
_CANCEL_TERMINAL = "stopped"


@pytest.fixture(autouse=True)
def _isolate_tasks_plugin_modules():
    """逐出同名裸模块，强制按本插件目录解析（与 test_tasks_http_api.py 同款）。"""
    d = str(_PLUGIN_DIR)
    # 共享根（plugins/shared 平铺模块：state_fields/time_iso/task_birth 等）
    # ——对齐生产 sidecar sys.path 形态（自身目录 + shared 根），单文件可跑。
    # parents[0] = plugins/shared（旧值 parents[1] = plugins 错层，单文件
    # 直跑即 ModuleNotFoundError——此前靠同目录兄弟文件的 sys.path 残留掩盖）
    shared_root = str(_PLUGIN_DIR.parents[0])
    _was_present = d in sys.path
    _added_shared_root = False
    if d in sys.path:
        sys.path.remove(d)
    sys.path.insert(0, d)
    if shared_root not in sys.path:
        sys.path.insert(1, shared_root)
        _added_shared_root = True
    _evict_names = (
        "task_types",
        "state_machine",
        "storage",
        "service",
        "timer_manager",
        "agents_types",
        "enum_utils",
        "workspace",
        "service_access",
        "project_registry",
        "_task_cleanup",
        "_task_crud",
        "_task_state",
        "http_api",
        "server",
    )
    _evicted: dict[str, object] = {}
    for m in _evict_names:
        if m in sys.modules:
            _evicted[m] = sys.modules.pop(m)
    yield
    if d in sys.path:
        sys.path.remove(d)
    if _was_present:
        sys.path.insert(0, d)
    if _added_shared_root:
        sys.path.remove(shared_root)
    for m in _evict_names:
        if m in _evicted:
            sys.modules[m] = _evicted[m]
        else:
            sys.modules.pop(m, None)


# ═══════════════════════════════════════════════════════════
# 测试脚手架（与 test_tasks_http_api.py 同款 fake 句柄面）
# ═══════════════════════════════════════════════════════════


class _FakeCapability:
    """fake 内核能力句柄：按 method 返回预置响应（未预置 → KeyError 语义）。"""

    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(
        self, method: str, params: dict[str, Any], timeout: float | None = None
    ) -> Any:
        self.calls.append((method, params))
        if method not in self._responses:
            raise KeyError(f"unexpected capability method: {method}")
        value = self._responses[method]
        if isinstance(value, Exception):
            raise value
        return value


class _FakeCapabilityHub:
    """fake http_api._capability 入口：按能力名返回 fake 句柄。"""

    def __init__(self, responses: dict[str, dict[str, Any]] | None = None) -> None:
        self._responses: dict[str, dict[str, Any]] = responses or {}
        self.handles: dict[str, _FakeCapability] = {}

    def get(self, name: str) -> _FakeCapability:
        if name not in self.handles:
            self.handles[name] = _FakeCapability(self._responses.get(name, {}))
        return self.handles[name]


@pytest.fixture
def hub() -> _FakeCapabilityHub:
    return _FakeCapabilityHub()


def _install(monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub) -> None:
    import http_api

    monkeypatch.setattr(http_api, "_capability", hub.get)


def _seed_state(hub: _FakeCapabilityHub, rows: list[dict[str, Any]]) -> None:
    """播种 state 聚合原始行（pipeline-state.list 响应）+ 正常 update 回执。"""
    hub._responses["pipeline-state"] = {
        "list": rows,
        "update": {"status": "updated"},
    }


def _state_row(pid: str, status: str, **extra: Any) -> dict[str, Any]:
    """构造一条任务执行管道 state 聚合原始行（task.* 扁平键）。

    task.id 出生协议必写（= pipeline_id，ADR 2026-11-06 D1 身份判据基座）。
    """
    row = {
        "pipeline_id": pid,
        "task.id": pid,
        "task.goal": f"任务 {pid}",
        "task.status": status,
        "task.submitted_by": "u-1",
        "thread_id": pid,
    }
    row.update(extra)
    return row


async def _cancel(monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub,
                  task_id: str) -> dict[str, Any]:
    """走完整 handle_http 分发 POST /{id}/cancel，返回 {status, payload}。"""
    import http_api

    _install(monkeypatch, hub)
    result = await http_api.handle_http(
        f"/ext/task_service/tasks/{task_id}/cancel", "POST", "{}", None, None,
    )
    assert result.get("success") is True, f"handle_http 失败: {result}"
    data = result["data"]
    payload = json.loads(base64.b64decode(data["body"]).decode("utf-8"))
    return {"status": data["status"], "payload": payload}


def _terminal_writes(hub: _FakeCapabilityHub) -> dict[str, dict[str, Any]]:
    """收集 pipeline-state.update 的终态随写：pipeline_id → fields。

    只收带 ``task.status`` 键的写；非终态写单列 :func:`_mirror_writes`
    （零写面负断言收集器），混收会同管道互相覆盖丢终态形状。
    """
    handle = hub.handles.get("pipeline-state")
    writes: dict[str, dict[str, Any]] = {}
    for method, params in (handle.calls if handle else []):
        if method == "update" and "task.status" in params["fields"]:
            writes[params["pipeline_id"]] = params["fields"]
    return writes


def _mirror_writes(hub: _FakeCapabilityHub) -> dict[str, dict[str, Any]]:
    """收集非终态 update 写（无 ``task.status`` 键）：pipeline_id → 合并 fields。

    owned 镜像已退役（ADR 2026-11-02-state-key-governance）：任务状态单一
    真源 = 子管道行 ``task.status``，取消/级联面不应有任何非终态写——本
    收集器现役用途 = 零写面负断言（若镜像写面回归，此处即红）。
    """
    handle = hub.handles.get("pipeline-state")
    mirrors: dict[str, dict[str, Any]] = {}
    for method, params in (handle.calls if handle else []):
        if method == "update" and "task.status" not in params["fields"]:
            fields = mirrors.setdefault(params["pipeline_id"], {})
            fields.update(params["fields"])
    return mirrors


def _assert_cancelled_terminal(fields: dict[str, Any]) -> None:
    """随写形状契约：写面词汇终态 + 可解析 ISO ended_at（性质断言）。"""
    assert fields["task.status"] == _CANCEL_TERMINAL
    assert fields["task.status"] in _WRITE_FACE_VOCABULARY
    # ended_at 可解析为真实时刻（ISO 8601，含时区）
    ended = datetime.fromisoformat(fields["task.ended_at"])
    assert ended.tzinfo is not None


# ═══════════════════════════════════════════════════════════
# 行为测试
# ═══════════════════════════════════════════════════════════


class TestCancelTerminalWrite:
    async def test_orphan_projection_cancel_writes_terminal(
            self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub) -> None:
        """①孤儿投影（缺陷本体形态）：state 行 running、引擎侧无活跃运行
        （suspend 幂等返回空 run_id）→ cancel 200 且 state 行终态已写。"""
        # 缺陷实证行形态：running 投影 + 无 run 簿记（行内无 run_status 键）
        _seed_state(hub, [_state_row("t-orphan-1", "running")])
        # 内核 suspend_pipeline 无匹配 run 的真实响应形状：ok + 空 run_id
        hub._responses["pipeline-executor"] = {
            "suspend_pipeline": {"status": "suspended", "pipeline_id": "t-orphan-1", "run_id": ""},
        }
        resp = await _cancel(monkeypatch, hub, "t-orphan-1")

        assert resp["status"] == 200
        assert resp["payload"]["cancelled"] is True
        writes = _terminal_writes(hub)
        assert "t-orphan-1" in writes, "孤儿投影 cancel 必须随写 state 行终态"
        _assert_cancelled_terminal(writes["t-orphan-1"])

    async def test_active_task_cancel_stops_engine_and_writes_terminal(
            self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub) -> None:
        """②活任务：引擎停止（suspend 命中 run）+ 终态随写两步都发生。"""
        _seed_state(hub, [_state_row("t-live-1", "running")])
        hub._responses["pipeline-executor"] = {
            "suspend_pipeline": {"status": "suspended", "run_id": "run-abc"},
        }
        resp = await _cancel(monkeypatch, hub, "t-live-1")

        assert resp["status"] == 200
        assert resp["payload"]["cancelled"] is True
        stop_calls = [(m, p) for m, p in hub.handles["pipeline-executor"].calls
                      if m == "suspend_pipeline"]
        assert len(stop_calls) == 1
        assert stop_calls[0][1]["pipeline_id"] == "t-live-1"
        writes = _terminal_writes(hub)
        _assert_cancelled_terminal(writes["t-live-1"])

    async def test_engine_stop_failure_honest_error_no_terminal_write(
            self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub) -> None:
        """③引擎停止失败 → 503 如实报错，不落终态、不谎报 cancelled。"""
        _seed_state(hub, [_state_row("t-live-2", "running")])
        hub._responses["pipeline-executor"] = {
            "suspend_pipeline": RuntimeError("引擎调用失败"),
        }
        resp = await _cancel(monkeypatch, hub, "t-live-2")

        assert resp["status"] == 503
        assert "引擎停止失败" in resp["payload"]["detail"]
        assert _terminal_writes(hub) == {}, "停止失败不得随写终态"

    async def test_capability_missing_is_engine_stop_failure(
            self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub) -> None:
        """引擎能力缺席 = 停止失败的同一诚实语义（503，不写终态）。"""
        _seed_state(hub, [_state_row("t-live-3", "running")])
        resp = await _cancel(monkeypatch, hub, "t-live-3")

        assert resp["status"] == 503
        assert _terminal_writes(hub) == {}

    async def test_terminal_write_failure_honest_error(
            self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub) -> None:
        """④终态随写失败 → 5xx 如实报错（引擎已停也不谎报 cancelled），
        否则响应 cancelled=true 而 GET 依旧 running = 缺陷本体换处复现。"""
        _seed_state(hub, [_state_row("t-live-4", "running")])
        hub._responses["pipeline-executor"] = {
            "suspend_pipeline": {"run_id": "run-xyz"},
        }
        state_handle = hub.get("pipeline-state")
        original_call = state_handle.call

        async def failing_update(method: str, params: dict[str, Any],
                                 timeout: float | None = None) -> Any:
            if method == "update":
                raise RuntimeError("state 写面故障")
            return await original_call(method, params, timeout)

        monkeypatch.setattr(state_handle, "call", failing_update)
        resp = await _cancel(monkeypatch, hub, "t-live-4")

        assert resp["status"] == 503
        assert "cancelled" not in resp["payload"], "写失败不得谎报取消成功"
        assert "随写" in resp["payload"]["detail"] or "state" in resp["payload"]["detail"]

    async def test_completed_task_reject_cancel_409(
            self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub) -> None:
        """⑤completed 是评估闸门/用户侧裁决终态：取消不得推翻（409，零写面）。"""
        _seed_state(hub, [_state_row("t-done-1", "completed")])
        resp = await _cancel(monkeypatch, hub, "t-done-1")

        assert resp["status"] == 409
        assert not hub.handles.get("pipeline-executor"), "拒绝路径不得触达引擎"
        assert _terminal_writes(hub) == {}

    async def test_missing_task_404_no_ghost_row(
            self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub) -> None:
        """⑥state 行缺席 → 404，引擎与写面零调用（写面对未知 pipeline_id
        是 upsert，不设闸会为 bogus id 凭空造幽灵任务行）。"""
        _seed_state(hub, [_state_row("t-other", "running")])
        resp = await _cancel(monkeypatch, hub, "no-such-task")

        assert resp["status"] == 404
        assert not hub.handles.get("pipeline-executor"), "不存在任务不得触达引擎"
        assert _terminal_writes(hub) == {}

    @pytest.mark.parametrize("residue", [None, "stopped", "failed"],
                             ids=["bookkeeping-only", "stopped-residue", "failed-residue"])
    async def test_cancel_rejects_session_row_404(
            self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub,
            residue: str | None) -> None:
        """cancel 前置身份门（ADR 2026-11-06 D5 N7）：会话主管道行（无 task.id
        无 lineage.*，可带误写残留）不是任务 → 404 拒绝，引擎与写面零调用。
        装机事故（2026-11-06，主管道 35ee45bf1da2）：残留形态若放行，停止链路
        会在会话上继续放大幽灵任务标记。"""
        session: dict[str, Any] = {
            "pipeline_id": "pipe_chat",
            "thread_id": "pipe_chat",
            "task.owned.t-child.title": "子任务",
            "task.subtasks_pending.t-child": "2026-11-06T08:00:00+00:00",
        }
        if residue is not None:
            session["task.status"] = residue
        _seed_state(hub, [session])
        resp = await _cancel(monkeypatch, hub, "pipe_chat")

        assert resp["status"] == 404
        assert not hub.handles.get("pipeline-executor"), "会话行不得触达引擎停止"
        assert _terminal_writes(hub) == {}, "会话行不落停止终态"

    @pytest.mark.parametrize("unreadable", [
        {"list": {"not": "a list"}},   # 响应形状非法
        {"list": RuntimeError("读面故障")},  # 读面异常
    ], ids=["non-list-rows", "read-error"])
    async def test_state_unreadable_404_no_write(
            self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub,
            unreadable: dict[str, Any]) -> None:
        """state 读面不可用 → 存在性无法确权，404（delete_task 同款语义），
        不触引擎不随写——不确定对象存在就落终态 = 盲写。"""
        hub._responses["pipeline-state"] = unreadable
        resp = await _cancel(monkeypatch, hub, "t-any")

        assert resp["status"] == 404
        assert not hub.handles.get("pipeline-executor")
        assert _terminal_writes(hub) == {}


class TestCancelCascadeTerminal:
    async def test_cascade_writes_terminal_for_parent_and_children(
            self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub) -> None:
        """⑦级联：父 + lineage 子管道都停止且都随写终态（级联只挂起不随写 =
        同一刀留下批量僵尸投影）。"""
        _seed_state(hub, [
            _state_row("t-parent", "running"),
            _state_row("t-child-a", "running",
                       **{"lineage.parent_pipeline_id": "t-parent"}),
            _state_row("t-child-b", "suspended",
                       **{"lineage.parent_pipeline_id": "t-parent"}),
            # 别家子任务不得误伤
            _state_row("t-unrelated", "running",
                       **{"lineage.parent_pipeline_id": "other-parent"}),
        ])
        hub._responses["pipeline-executor"] = {
            "suspend_pipeline": {"run_id": "run-1"},
        }
        resp = await _cancel(monkeypatch, hub, "t-parent")

        assert resp["status"] == 200
        assert resp["payload"]["cancelled"] is True
        assert resp["payload"]["cascaded_subtasks"] == 2
        stopped = [p["pipeline_id"] for m, p in hub.handles["pipeline-executor"].calls
                   if m == "suspend_pipeline"]
        assert sorted(stopped) == ["t-child-a", "t-child-b", "t-parent"]
        writes = _terminal_writes(hub)
        assert sorted(writes) == ["t-child-a", "t-child-b", "t-parent"], \
            "父与被级联子任务都必须随写终态"
        for fields in writes.values():
            _assert_cancelled_terminal(fields)
        assert "t-unrelated" not in writes
        # owned 镜像已退役（ADR 2026-11-02-state-key-governance）：任务状态
        # 单一真源 = 子管道行 task.status，父会话面板 join 子行取真值——
        # 级联取消零父镜像写（非终态 update 面 = 空）
        assert _mirror_writes(hub) == {}, (
            f"父镜像写面已退役，不得有 owned 随写：{_mirror_writes(hub)!r}"
        )

    async def test_cascade_child_write_failure_does_not_block(
            self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub) -> None:
        """级联面既有吞错风格：单子任务随写失败仅告警，不阻断父任务取消。"""
        _seed_state(hub, [
            _state_row("t-parent2", "running"),
            _state_row("t-child-x", "running",
                       **{"lineage.parent_pipeline_id": "t-parent2"}),
        ])
        hub._responses["pipeline-executor"] = {
            "suspend_pipeline": {"run_id": "run-1"},
        }
        state_handle = hub.get("pipeline-state")
        original_call = state_handle.call

        async def flaky_update(method: str, params: dict[str, Any],
                               timeout: float | None = None) -> Any:
            if method == "update" and params.get("pipeline_id") == "t-child-x":
                raise RuntimeError("子任务写面故障")
            return await original_call(method, params, timeout)

        monkeypatch.setattr(state_handle, "call", flaky_update)
        resp = await _cancel(monkeypatch, hub, "t-parent2")

        assert resp["status"] == 200
        assert resp["payload"]["cancelled"] is True
        writes = _terminal_writes(hub)
        assert "t-parent2" in writes, "父任务终态不受子任务写失败影响"
        assert "t-child-x" not in writes

    @pytest.mark.parametrize("second_list", [
        {"not": "a list"},
        RuntimeError("级联列举故障"),
    ], ids=["non-list-rows", "list-error"])
    async def test_cascade_scan_failure_parent_still_cancelled(
            self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub,
            second_list: Any) -> None:
        """级联列举面故障（非列表/异常）→ 级联 0 但父任务取消与终态随写不受影响
        （级联面既有吞错风格：观测/级联故障不阻断主操作）。"""
        _seed_state(hub, [_state_row("t-parent3", "running")])
        hub._responses["pipeline-executor"] = {
            "suspend_pipeline": {"run_id": "run-1"},
        }
        state_handle = hub.get("pipeline-state")
        original_call = state_handle.call
        list_calls = {"n": 0}

        async def degraded_list(method: str, params: dict[str, Any],
                                timeout: float | None = None) -> Any:
            result = await original_call(method, params, timeout)
            if method == "list":
                list_calls["n"] += 1
                # list 序：#1 = 存在性仲裁（父行在册）、#2 = 终态随写面行读
                # （ADR 2026-11-06 N1b 身份门：行缺失拒写——不得注入故障）、
                # 其后 = 级联列举（注入故障）
                if list_calls["n"] > 2:
                    return second_list() if isinstance(second_list, Exception) else second_list
            return result

        monkeypatch.setattr(state_handle, "call", degraded_list)
        resp = await _cancel(monkeypatch, hub, "t-parent3")

        assert resp["status"] == 200
        assert resp["payload"]["cancelled"] is True
        assert resp["payload"]["cascaded_subtasks"] == 0
        _assert_cancelled_terminal(_terminal_writes(hub)["t-parent3"])
