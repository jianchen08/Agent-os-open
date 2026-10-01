# @feature: FP-0.2.五 审批闭环 | @ci: python-coverage
"""approval.created 事件发射链路测试（改动 A 后端）。

覆盖：
1. create_choice 成功创建 human 交互后 → emit approval.created（payload 对齐前端）
2. event-bus capability 未注入 → 不抛异常（fire-and-forget 韧性）
3. create_choice 失败 → 不 emit（只在创建成功后发）
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

# SDK 路径（agentos_plugin_sdk 未安装时）
_SDK_DIR = Path(__file__).resolve().parents[4] / "sdk" / "src"
if str(_SDK_DIR) not in sys.path:
    sys.path.insert(0, str(_SDK_DIR))


def _load_server() -> Any:
    """动态加载 server.py（每次新建，避免模块级 plugin 状态跨测试污染）。"""
    mod_name = "approval_server_test"
    module_path = _PLUGIN_DIR / "server.py"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, module_path)
    assert spec is not None, "Cannot load server.py"
    assert spec.loader is not None, "Cannot load server.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


class FakeBus:
    """记录 emit 调用的伪 event-bus capability handle。"""

    def __init__(self) -> None:
        self.emits: list[tuple[str, dict]] = []

    async def notify(self, method: str, params: dict) -> None:
        self.emits.append((method, params))


class FakeHi:
    """伪 human-interaction capability handle。"""

    def __init__(self, create_result: dict, wait_result: dict | None = None) -> None:
        self._create = create_result
        self._wait = wait_result or {"selected_option": "批准"}
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method: str, params: dict) -> dict:
        self.calls.append((method, params))
        if method == "create_choice":
            return self._create
        if method == "wait_for_choice":
            return self._wait
        return {}


def _inject_capability(module: Any, name: str, handle: Any) -> None:
    """把伪 capability 注入插件（覆盖 _get_cap 的返回）。"""
    original = module.plugin.get_capability
    module.plugin.get_capability = lambda n: handle if n == name else original(n)  # type: ignore[method-assign]


def test_create_choice_emits_approval_created() -> None:
    """create_choice 成功 → emit approval.created，payload 含 request_id/options/mode/run_id。"""
    mod = _load_server()
    bus = FakeBus()
    _inject_capability(mod, "event-bus", bus)
    hi = FakeHi(create_result={"request_id": "req-abc", "status": "pending"})
    _inject_capability(mod, "human-interaction", hi)
    mod._suspended.clear()
    mod._decisions.clear()

    result = _run(mod.create_choice(title="是否批准", options=["批准", "拒绝"], run_id="run-42"))

    assert result.get("status") == "resolved"
    # emit 记录：bus.notify("emit", {event, payload, thread_id})
    assert len(bus.emits) == 1
    method, params = bus.emits[0]
    assert method == "emit"
    assert params["event"] == "approval.created"
    payload = params["payload"]
    assert payload["request_id"] == "req-abc"
    assert payload["title"] == "是否批准"
    assert payload["options"] == ["批准", "拒绝"]
    assert payload["mode"] == "choice"
    assert payload["run_id"] == "run-42"
    assert params["thread_id"] == "run-42"


def test_emit_skipped_when_no_event_bus() -> None:
    """event-bus 未注入 → 不发事件、不抛异常（fire-and-forget 韧性）。"""
    mod = _load_server()
    # 不注入 event-bus（get_capability 抛 KeyError → _get_cap 返回 None）
    hi = FakeHi(create_result={"request_id": "req-x"})
    _inject_capability(mod, "human-interaction", hi)
    mod._suspended.clear()
    mod._decisions.clear()

    result = _run(mod.create_choice(title="t", options=["a"], run_id="run-1"))

    assert result.get("status") == "resolved"  # 主链路不受影响


def test_no_emit_when_create_fails() -> None:
    """human create_choice 失败 → 不 emit approval.created（仅创建成功后发）。"""
    mod = _load_server()
    bus = FakeBus()
    _inject_capability(mod, "event-bus", bus)
    hi = FakeHi(create_result={"error": "human unavailable"})
    _inject_capability(mod, "human-interaction", hi)

    result = _run(mod.create_choice(title="t", options=["a"], run_id="run-2"))

    assert "error" in result
    assert bus.emits == []


# ═══════════════════════════════════════════════════════════
# 来源上下文（2026-10-01 用户裁定）：审批卡必须可见发起 agent/会话/管道/时间
# ═══════════════════════════════════════════════════════════


def _make_origin_db(tmp_path: Path, entries: list[dict[str, str]]) -> Path:
    """pipeline_state 最小形态：run 锚行 + 指定管道的来源标量键行。"""
    db_path = tmp_path / "agentos_kernel.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE pipeline_state (pipeline_id TEXT, field_key TEXT, value TEXT,"
        " value_kind TEXT DEFAULT 'str', tenant_id TEXT, updated_at TEXT,"
        " PRIMARY KEY (pipeline_id, field_key, tenant_id))"
    )
    for e in entries:
        conn.execute(
            "INSERT INTO pipeline_state (pipeline_id, field_key, value, value_kind,"
            " tenant_id, updated_at) VALUES (?, ?, ?, 'str', 'tenant-a',"
            " '2026-01-01T00:00:00')",
            (e["pipeline_id"], e["field_key"], e["value"]),
        )
    conn.commit()
    conn.close()
    return db_path


def test_emit_payload_carries_origin_fields(tmp_path, monkeypatch) -> None:
    """run 可反查：approval.created payload 带齐 agent/会话/管道来源 + created_at。"""
    monkeypatch.setenv(
        "AGENTOS_DB_PATH",
        str(_make_origin_db(tmp_path, [
            {"pipeline_id": "pipe-9", "field_key": "run_id", "value": "run-42"},
            {"pipeline_id": "pipe-9", "field_key": "session_id", "value": "th-77"},
            {"pipeline_id": "pipe-9", "field_key": "agent.id", "value": "coding_dev_agent"},
            {"pipeline_id": "pipe-9", "field_key": "agent_level", "value": "L3"},
            {"pipeline_id": "pipe-9", "field_key": "context.agent_name", "value": "编码开发代理"},
        ])),
    )
    mod = _load_server()
    bus = FakeBus()
    _inject_capability(mod, "event-bus", bus)
    hi = FakeHi(create_result={"request_id": "req-org", "status": "pending"})
    _inject_capability(mod, "human-interaction", hi)
    mod._suspended.clear()
    mod._decisions.clear()

    result = _run(mod.create_choice(title="是否批准", options=["批准"], run_id="run-42"))

    assert result.get("status") == "resolved"
    # human 建卡透传来源四元组（交互记录/交互卡同数据面）
    hi_call = next(p for m, p in hi.calls if m == "create_choice")
    assert hi_call["agent_id"] == "coding_dev_agent"
    assert hi_call["agent_level"] == "L3"
    assert hi_call["pipeline_id"] == "pipe-9"
    assert hi_call["agent_name"] == "编码开发代理"
    # 全屏审批浮层事件 payload 携带来源 + 时间
    _method, params = bus.emits[0]
    assert params["event"] == "approval.created"
    payload = params["payload"]
    assert payload["thread_id"] == "th-77"
    assert payload["agent_id"] == "coding_dev_agent"
    assert payload["agent_level"] == "L3"
    assert payload["pipeline_id"] == "pipe-9"
    assert payload["agent_name"] == "编码开发代理"
    assert payload["created_at"]
    # 事件路由键回落真实会话线程（可投递到会话 WS 通道）
    assert params["thread_id"] == "th-77"


def test_emit_payload_without_origin_when_run_unattributable(tmp_path, monkeypatch) -> None:
    """run 不可反查（库无锚）：payload 无来源键但不阻塞审批主链路。"""
    monkeypatch.setenv(
        "AGENTOS_DB_PATH", str(_make_origin_db(tmp_path, []))
    )
    mod = _load_server()
    bus = FakeBus()
    _inject_capability(mod, "event-bus", bus)
    hi = FakeHi(create_result={"request_id": "req-noorg", "status": "pending"})
    _inject_capability(mod, "human-interaction", hi)
    mod._suspended.clear()
    mod._decisions.clear()

    result = _run(mod.create_choice(title="t", options=["a"], run_id="run-missing"))

    assert result.get("status") == "resolved"
    _method, params = bus.emits[0]
    payload = params["payload"]
    assert payload["created_at"]
    for key in ("thread_id", "agent_id", "agent_level", "pipeline_id", "agent_name"):
        assert key not in payload
    assert params["thread_id"] == "run-missing"  # 路由键回落 run 坐标


def _run(coro: Any) -> Any:
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ═══════════════════════════════════════════════════════════
# approval.taken 结算广播（ADR 2026-10-01 多前端连接决策 5）
# ═══════════════════════════════════════════════════════════


class FakePipelineExecutor:
    """伪 pipeline-executor：resume 成功正常返回；失败以异常表达
    （_resume_pipeline 对 call 不抛异常即视为成功）。"""

    def __init__(self, resume_ok: bool = True) -> None:
        self.resume_ok = resume_ok
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method: str, params: dict) -> dict:
        self.calls.append((method, params))
        if method == "suspend":
            return {"run_id": params.get("run_id"), "branch_id": "b0", "seq": 3}
        if method == "resume" and not self.resume_ok:
            raise RuntimeError("resume failed")
        return {"ok": self.resume_ok}


def test_submit_success_emits_approval_taken() -> None:
    """submit 成功（resume 成功）→ emit approval.taken：request_id + 结算摘要，
    路由键回落 run 坐标（与 approval.created 同构）。"""
    mod = _load_server()
    bus = FakeBus()
    _inject_capability(mod, "event-bus", bus)
    pipeline = FakePipelineExecutor(resume_ok=True)
    _inject_capability(mod, "pipeline-executor", pipeline)
    mod._suspended.clear()
    mod._decisions.clear()
    mod._suspended["req-take"] = {
        "suspend_handle": {"run_id": "run-9", "branch_id": "b0", "seq": 3},
        "run_id": "run-9",
        "created_at": 0.0,
    }

    result = _run(mod.submit(request_id="req-take", result="批准"))

    assert result.get("status") == "resolved" and result.get("resumed") is True
    taken = [p for _m, p in bus.emits if p["event"] == "approval.taken"]
    assert len(taken) == 1, "submit 成功路径必须广播一次 approval.taken"
    params = taken[0]
    assert params["payload"]["request_id"] == "req-take"
    assert params["payload"]["status"] == "resolved"
    assert params["payload"]["resumed"] is True
    assert params["payload"]["result"] == "批准"
    assert params["thread_id"] == "run-9", "thread_id 空时回落 run 会话坐标"


def test_submit_no_handle_emits_approval_taken() -> None:
    """submit 成功（无句柄直落终态）→ 同样广播 approval.taken（resumed=False）。"""
    mod = _load_server()
    bus = FakeBus()
    _inject_capability(mod, "event-bus", bus)
    mod._suspended.clear()
    mod._decisions.clear()
    mod._suspended["req-nohandle"] = {
        "suspend_handle": None,
        "run_id": "run-8",
        "created_at": 0.0,
    }

    result = _run(mod.submit(request_id="req-nohandle", result="拒绝"))

    assert result.get("status") == "resolved" and result.get("resumed") is False
    taken = [p for _m, p in bus.emits if p["event"] == "approval.taken"]
    assert len(taken) == 1
    assert taken[0]["payload"]["request_id"] == "req-nohandle"
    assert taken[0]["payload"]["resumed"] is False
    assert taken[0]["thread_id"] == "run-8"


def test_submit_failure_paths_do_not_emit_taken() -> None:
    """resume 失败（审批仍挂起）与无记录：不广播 approval.taken（非结算）。"""
    mod = _load_server()
    bus = FakeBus()
    _inject_capability(mod, "event-bus", bus)
    pipeline = FakePipelineExecutor(resume_ok=False)
    _inject_capability(mod, "pipeline-executor", pipeline)
    mod._suspended.clear()
    mod._decisions.clear()
    mod._suspended["req-still"] = {
        "suspend_handle": {"run_id": "run-7"},
        "run_id": "run-7",
        "created_at": 0.0,
    }

    failed = _run(mod.submit(request_id="req-still", result="批准"))
    missing = _run(mod.submit(request_id="req-ghost", result="批准"))

    assert failed.get("status") == "resume_failed"
    assert "error" in missing
    assert [p for _m, p in bus.emits if p["event"] == "approval.taken"] == []


def test_submit_taken_skipped_when_no_event_bus() -> None:
    """event-bus 未注入：submit 主链路不受影响（fire-and-forget 韧性）。"""
    mod = _load_server()
    mod._suspended.clear()
    mod._decisions.clear()
    mod._suspended["req-nobus"] = {
        "suspend_handle": None,
        "run_id": "run-6",
        "created_at": 0.0,
    }

    result = _run(mod.submit(request_id="req-nobus", result="批准"))

    assert result.get("status") == "resolved"


def test_emit_payload_degrades_gracefully_when_db_corrupt(tmp_path, monkeypatch) -> None:
    """库损坏（查询抛 sqlite3.Error）：来源反查降级全空，审批主链路不受阻。"""
    bad_db = tmp_path / "agentos_kernel.db"
    bad_db.write_bytes(b"not a sqlite database" * 8)
    monkeypatch.setenv("AGENTOS_DB_PATH", str(bad_db))
    mod = _load_server()
    bus = FakeBus()
    _inject_capability(mod, "event-bus", bus)
    hi = FakeHi(create_result={"request_id": "req-bad", "status": "pending"})
    _inject_capability(mod, "human-interaction", hi)
    mod._suspended.clear()
    mod._decisions.clear()

    result = _run(mod.create_choice(title="t", options=["a"], run_id="run-bad"))

    assert result.get("status") == "resolved"
    _method, params = bus.emits[0]
    payload = params["payload"]
    assert payload["created_at"]
    for key in ("pipeline_id", "thread_id", "agent_id", "agent_level", "agent_name"):
        assert key not in payload
