# @feature: FP-0.2.二 mode_personal 到点触发器同步 | @ci: python-coverage
"""PimTriggerSync 单测：注册/注销/迁移/再武装/无目标挂起（fake capability caller）。"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PKG = Path(__file__).resolve().parents[1]
_SHARED = _PKG.parent


def _load(name: str, mod_name: str) -> Any:
    for d in (_PKG, _SHARED):
        if str(d) not in sys.path:
            sys.path.insert(0, str(d))
    path = _PKG / f"{name}.py"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


pim_store = _load("pim_store", "mode_personal_test_sync_store")
pim_trigger_sync = _load("pim_trigger_sync", "mode_personal_test_trigger_sync")

NOW = datetime(2026, 10, 12, 9, 0, 0)


class FakeCaller:
    """最小 trigger-control 服务面（provides 白名单短名；记录调用 + 维护触发器表）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.triggers: dict[str, dict[str, Any]] = {}

    async def __call__(self, method: str, params: dict[str, Any]) -> Any:
        self.calls.append((method, params))
        if method == "list":
            item_id = str(params.get("item_id") or "")
            items = [
                dict(trigger)
                for trigger in self.triggers.values()
                if not item_id
                or str((trigger.get("metadata") or {}).get("item_id") or "") == item_id
            ]
            return {"success": True, "items": items}
        if method == "register":
            trigger_id = str(params["trigger_id"])
            self.triggers[trigger_id] = {**params, "status": "active"}
            return {"success": True, "trigger_id": trigger_id}
        if method == "unregister":
            trigger = self.triggers.get(str(params["trigger_id"]))
            if trigger is not None:
                trigger["status"] = "cancelled"
            return {"success": True}
        if method == "retarget":
            trigger = self.triggers.get(str(params["trigger_id"]))
            if trigger is not None:
                trigger["pipeline_id"] = params["pipeline_id"]
            return {"success": True}
        return {"success": False, "error": f"unknown method: {method}"}

    def methods(self) -> list[str]:
        return [name for name, _ in self.calls]

    def register_payloads(self) -> list[dict[str, Any]]:
        return [params for name, params in self.calls if name == "register"]


class FakeStateCaller:
    """最小 pipeline-state 标准能力面（独立通道：短名 list + 行表）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.rows: list[dict[str, Any]] = []

    async def __call__(self, method: str, params: dict[str, Any]) -> Any:
        self.calls.append((method, params))
        if method == "list":
            return self.rows
        return {"success": False, "error": f"unknown method: {method}"}

    def methods(self) -> list[str]:
        return [name for name, _ in self.calls]


@pytest.fixture
def store(tmp_path: Path) -> Any:
    return pim_store.PimStore(tmp_path / "data", clock=lambda: NOW)


@pytest.fixture
def caller() -> FakeCaller:
    return FakeCaller()


@pytest.fixture
def state() -> FakeStateCaller:
    fake = FakeStateCaller()
    fake.rows = [{"pipeline_id": "pipe-1", "agent.id": "agentos"}]
    return fake


def _make_sync(store: Any, caller: FakeCaller, state: FakeStateCaller | None = None) -> Any:
    sync = pim_trigger_sync.PimTriggerSync(
        store, caller, clock=lambda: NOW, state_caller=state
    )
    store.set_on_change(sync.on_change)
    return sync


async def _settle(sync: Any) -> None:
    """等待 store 观察者调度的 flush 收敛，再显式跑一轮（测试确定性）。"""
    task = sync._flush_task
    if task is not None:
        await task
    await sync._flush()


async def _activate(sync: Any, pipeline_id: str = "pipe-1") -> None:
    await sync.on_domain_event(
        {
            "event": "session.created",
            "pipeline_id": pipeline_id,
            "session_id": "thread-1",
        }
    )


class TestRegistration:
    async def test_future_due_registers_schedule(self, store: Any, caller: FakeCaller) -> None:
        sync = _make_sync(store, caller)
        await _activate(sync)
        todo, _ = store.create("todo", {"title": "交周报", "due": "2026-10-12 10:00", "reminder_mode": "agent_classified"})
        await _settle(sync)
        payloads = caller.register_payloads()
        assert len(payloads) == 1
        payload = payloads[0]
        assert payload["trigger_type"] == "schedule"
        assert payload["trigger_id"] == pim_trigger_sync.trigger_id_for("todo", todo["id"])
        assert payload["metadata"]["item_id"] == todo["id"]
        assert payload["metadata"]["occurrence"] == "2026-10-12 10:00"
        assert "10:00" in payload["scheduled_at"]
        assert "无需打扰" in payload["message"]
        assert "human_interaction" not in payload["message"]

    async def test_past_due_registers_delay(self, store: Any, caller: FakeCaller) -> None:
        sync = _make_sync(store, caller)
        await _activate(sync)
        store.create("todo", {"title": "逾期", "due": "2026-10-12 08:00", "reminder_mode": "agent_classified"})
        await _settle(sync)
        payload = caller.register_payloads()[0]
        assert payload["trigger_type"] == "delay"
        assert payload["delay_seconds"] == 1

    async def test_no_target_is_pending_until_session(self, store: Any, caller: FakeCaller) -> None:
        sync = _make_sync(store, caller)
        store.create("todo", {"title": "A", "due": "2026-10-12 10:00", "reminder_mode": "agent_classified"})
        await _settle(sync)
        assert caller.register_payloads() == []
        await _activate(sync)
        assert len(caller.register_payloads()) == 1

    async def test_task_pipeline_ignored(
        self, store: Any, caller: FakeCaller, state: FakeStateCaller
    ) -> None:
        sync = _make_sync(store, caller, state)
        state.rows = [{"pipeline_id": "task-1", "agent.id": "general_agent"}]
        await sync.on_domain_event(
            {"event": "session.active_changed", "pipeline_id": "task-1", "session_id": "t"}
        )
        store.create("todo", {"title": "A", "due": "2026-10-12 10:00", "reminder_mode": "agent_classified"})
        await _settle(sync)
        assert caller.register_payloads() == []


class TestCapabilityMethodContract:
    """capability 调用名契约：trigger-control 走 provides 短名，state 走独立通道。

    回归锚（装机 11-05 A2 事故）：旧实现传 tool_prefix 名（trigger.list 等），
    内核 method 白名单只收短名 → 全部拒绝、提醒全天静默瘫痪。
    """

    async def test_capability_calls_use_short_names(
        self, store: Any, caller: FakeCaller, state: FakeStateCaller
    ) -> None:
        sync = _make_sync(store, caller, state)
        await _activate(sync)
        store.create("todo", {"title": "交周报", "due": "2026-10-12 10:00", "reminder_mode": "agent_classified"})
        await _settle(sync)
        assert caller.methods(), "必须存在 trigger-control 调用"
        assert all(
            not name.startswith(("trigger.", "pipeline-state.")) for name in caller.methods()
        ), f"禁带 tool_prefix 名（内核白名单只收短名）: {caller.methods()}"
        assert {"register", "list"} <= set(caller.methods())
        assert state.methods(), "state 查询必须走独立 pipeline-state 通道"
        assert all(
            not name.startswith("pipeline-state.") for name in state.methods()
        ), "state 通道调用必须用短名"
        assert "list" in state.methods()

    async def test_state_channel_absent_degrades_allow(
        self, store: Any, caller: FakeCaller
    ) -> None:
        """状态通道未注入（旧装配/能力缺席）→ 核验放行，不阻断同步主路径。"""
        sync = _make_sync(store, caller, state=None)
        await _activate(sync)
        store.create("todo", {"title": "A", "due": "2026-10-12 10:00", "reminder_mode": "agent_classified"})
        await _settle(sync)
        assert len(caller.register_payloads()) == 1

    async def test_state_channel_error_degrades_allow(
        self, store: Any, caller: FakeCaller
    ) -> None:
        """状态通道故障（调用抛错）→ 核验放行，不阻断同步主路径。"""

        async def broken_state(method: str, params: dict[str, Any]) -> Any:
            raise RuntimeError("pipeline-state 通道故障")

        sync = pim_trigger_sync.PimTriggerSync(
            store, caller, clock=lambda: NOW, state_caller=broken_state
        )
        store.set_on_change(sync.on_change)
        await _activate(sync)
        store.create("todo", {"title": "A", "due": "2026-10-12 10:00", "reminder_mode": "agent_classified"})
        await _settle(sync)
        assert len(caller.register_payloads()) == 1

    async def test_reconcile_failure_logs_exception_detail(
        self,
        store: Any,
        caller: FakeCaller,
        state: FakeStateCaller,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """trigger-control 服务故障 → 警告日志带异常详情（排障面，A2 同刀）。"""

        async def broken_call(method: str, params: dict[str, Any]) -> Any:
            raise RuntimeError("trigger-control 不可用")

        sync = pim_trigger_sync.PimTriggerSync(
            store, broken_call, clock=lambda: NOW, state_caller=state
        )
        with caplog.at_level("WARNING", logger="pim_trigger_sync"):
            await sync._reconcile_all()
        assert any(
            "trigger-control.list 不可用" in rec.message and "trigger-control 不可用" in rec.message
            for rec in caplog.records
        ), "警告必须携带异常详情"


class TestUnregisterAndRetarget:
    async def test_complete_unregisters(self, store: Any, caller: FakeCaller) -> None:
        sync = _make_sync(store, caller)
        await _activate(sync)
        todo, _ = store.create("todo", {"title": "A", "due": "2026-10-12 10:00", "reminder_mode": "agent_classified"})
        await _settle(sync)
        store.complete_todo(todo["id"])
        await _settle(sync)
        assert "unregister" in caller.methods()

    async def test_accept_event_unregisters(self, store: Any, caller: FakeCaller) -> None:
        sync = _make_sync(store, caller)
        await _activate(sync)
        event, _ = store.create("event", {"title": "例会", "at": "2026-10-12 10:00", "reminder_mode": "agent_classified"})
        await _settle(sync)
        store.accept("event", event["id"])
        await _settle(sync)
        assert "unregister" in caller.methods()

    async def test_defer_reregisters_new_time(self, store: Any, caller: FakeCaller) -> None:
        sync = _make_sync(store, caller)
        await _activate(sync)
        todo, _ = store.create("todo", {"title": "A", "due": "2026-10-12 10:00", "reminder_mode": "agent_classified"})
        await _settle(sync)
        store.defer_todo(todo["id"], "2026-10-12 11:00")
        await _settle(sync)
        payloads = caller.register_payloads()
        assert payloads[-1]["metadata"]["occurrence"] == "2026-10-12 11:00"
        assert payloads[-1]["trigger_id"] == payloads[0]["trigger_id"]

    async def test_source_pipeline_pins_registration(
        self, store: Any, caller: FakeCaller, state: FakeStateCaller
    ) -> None:
        """挂载语义（ADR 2026-11-03-pim-trigger-anchor）：来源会话在场 → 直接
        注册到创建事项的管道，**不需要任何活跃会话**；后续会话切换不 retarget
        （钉住创建会话——提醒坐标 = 日程诞生地，不随活跃面漂移）。"""
        sync = _make_sync(store, caller, state)
        store.create(
            "todo",
            {"title": "A", "due": "2026-10-12 10:00", "source_pipeline_id": "pipe-src", "reminder_mode": "agent_classified"},
        )
        await _settle(sync)
        payloads = caller.register_payloads()
        assert len(payloads) == 1
        assert payloads[0]["pipeline_id"] == "pipe-src"
        # 会话切换（无来源漂移面）：注册钉在 pipe-src 不动
        state.rows = [{"pipeline_id": "pipe-2", "agent.id": "agentos"}]
        await sync.on_domain_event(
            {"event": "session.active_changed", "pipeline_id": "pipe-2", "session_id": "t2"}
        )
        await _settle(sync)
        assert caller.register_payloads()[-1]["pipeline_id"] == "pipe-src"

    async def test_no_session_falls_back_to_session_creator(self, store: Any, caller: FakeCaller) -> None:
        """兜底（ADR 2026-11-03-pim-trigger-anchor）：无来源会话且无活跃会话 →
        经 session.create 新建承载管道注册，并把来源管道记回 record（幂等锚）。"""
        created: list[str] = []

        async def creator(title: str) -> dict[str, Any]:
            created.append(title)
            return {"pipeline_ids": ["pipe-new-1"]}

        sync = pim_trigger_sync.PimTriggerSync(
            store, caller, clock=lambda: NOW, session_creator=creator
        )
        store.set_on_change(sync.on_change)
        store.create("todo", {"title": "A", "due": "2026-10-12 10:00", "reminder_mode": "agent_classified"})
        await _settle(sync)
        payloads = caller.register_payloads()
        assert len(payloads) == 1
        assert payloads[0]["pipeline_id"] == "pipe-new-1"
        assert created, "session.create 兜底必须被调用"
        record = store.get("todo", payloads[0]["metadata"]["item_id"])
        assert record.get("source_pipeline_id") == "pipe-new-1", (
            "兜底新建的承载管道必须记回 source_pipeline_id（幂等锚）"
        )

    async def test_session_creator_absent_keeps_pending(self, store: Any, caller: FakeCaller) -> None:
        """creator 缺席（旧内核 session 能力不在声明面）→ 保持 pending（现状
        降级，不抛不炸）。"""
        sync = _make_sync(store, caller)
        store.create("todo", {"title": "A", "due": "2026-10-12 10:00", "reminder_mode": "agent_classified"})
        await _settle(sync)
        assert caller.register_payloads() == []

    async def test_session_change_retargets_legacy_items_without_source(
        self, store: Any, caller: FakeCaller, state: FakeStateCaller
    ) -> None:
        """无 source 的存量事项（历史行）：保持跟随活跃会话的既有语义
        （兼容存量数据；新数据 capture 即带 source，钉住不再跟随）。"""
        sync = _make_sync(store, caller, state)
        await _activate(sync, "pipe-1")
        store.create("todo", {"title": "A", "due": "2026-10-12 10:00", "reminder_mode": "agent_classified"})
        await _settle(sync)
        state.rows = [{"pipeline_id": "pipe-2", "agent.id": "agentos"}]
        await sync.on_domain_event(
            {"event": "session.active_changed", "pipeline_id": "pipe-2", "session_id": "thread-1"}
        )
        assert "retarget" in caller.methods()
        moved = [p for name, p in caller.calls if name == "retarget"]
        assert moved[-1]["pipeline_id"] == "pipe-2"

    async def test_session_deleted_clears_target(self, store: Any, caller: FakeCaller) -> None:
        sync = _make_sync(store, caller)
        await _activate(sync)
        await sync.on_domain_event({"event": "session.deleted", "session_id": "thread-1"})
        store.create("todo", {"title": "A", "due": "2026-10-12 10:00", "reminder_mode": "agent_classified"})
        await _settle(sync)
        assert caller.register_payloads() == []


class TestFiredAndIdempotence:
    async def test_repeat_fired_advances_and_marks(self, store: Any, caller: FakeCaller) -> None:
        sync = _make_sync(store, caller)
        await _activate(sync)
        todo, _ = store.create(
            "todo", {"title": "每日站会", "due": "2026-10-12 10:00", "repeat": "daily", "reminder_mode": "agent_classified"}
        )
        await _settle(sync)
        await sync.on_domain_event(
            {
                "event": "trigger.fired",
                "trigger_source": "mode_personal",
                "entity": "todo",
                "item_id": todo["id"],
                "occurrence": "2026-10-12 10:00",
            }
        )
        await _settle(sync)
        record = store.get("todo", todo["id"])
        assert record is not None
        assert record["due"] == "2026-10-13 10:00"
        assert record["last_reminded_for"] == "2026-10-12 10:00"
        payloads = caller.register_payloads()
        assert payloads[-1]["metadata"]["occurrence"] == "2026-10-13 10:00"

    async def test_non_pim_fired_ignored(self, store: Any, caller: FakeCaller) -> None:
        sync = _make_sync(store, caller)
        await _activate(sync)
        todo, _ = store.create(
            "todo", {"title": "每日站会", "due": "2026-10-12 10:00", "repeat": "daily", "reminder_mode": "agent_classified"}
        )
        await sync.on_domain_event(
            {
                "event": "trigger.fired",
                "trigger_source": "someone_else",
                "entity": "todo",
                "item_id": todo["id"],
                "occurrence": "2026-10-12 10:00",
            }
        )
        assert store.get("todo", todo["id"]).get("last_reminded_for") is None

    async def test_same_occurrence_not_registered_twice(
        self, store: Any, caller: FakeCaller
    ) -> None:
        sync = _make_sync(store, caller)
        await _activate(sync)
        todo, _ = store.create("todo", {"title": "A", "due": "2026-10-12 10:00", "reminder_mode": "agent_classified"})
        await _settle(sync)
        store.mark_reminded("todo", todo["id"], "2026-10-12 10:00")
        store.update("todo", todo["id"], {"title": "改名"})
        await _settle(sync)
        assert len(caller.register_payloads()) == 1



def test_trigger_id_is_deterministic() -> None:
    assert pim_trigger_sync.trigger_id_for("todo", "abc") == pim_trigger_sync.trigger_id_for(
        "todo", "abc"
    )
    assert pim_trigger_sync.trigger_id_for("todo", "abc") != pim_trigger_sync.trigger_id_for(
        "event", "abc"
    )


class TestRepeatAutoOccurrence:
    """P0①：repeat 卡无结构化时刻 → 按周期合成到点坐标注册（合理默认当日 09:00，
    已过取下一次发生）。时间来源=卡片时间字段（缺）或该默认。"""

    async def test_daily_no_due_registers_next_day_9(
        self, store: Any, caller: FakeCaller
    ) -> None:
        sync = _make_sync(store, caller)
        await _activate(sync)
        store.create(
            "todo",
            {"title": "华为刷题", "repeat": "daily", "reminder_mode": "agent_classified"},
        )
        await _settle(sync)
        payloads = caller.register_payloads()
        assert len(payloads) == 1
        assert payloads[0]["trigger_type"] == "schedule"
        # NOW=2026-10-12（周一）09:00 整已过 → 次日 09:00（scheduled_at 为 ISO T 分隔）
        assert payloads[0]["metadata"]["occurrence"] == "2026-10-13 09:00"
        assert "2026-10-13T09:00" in payloads[0]["scheduled_at"]

    async def test_weekly_no_due_registers_next_weekday(
        self, store: Any, caller: FakeCaller
    ) -> None:
        sync = _make_sync(store, caller)
        await _activate(sync)
        store.create(
            "todo",
            {"title": "周会", "repeat": "weekly:1", "reminder_mode": "agent_classified"},
        )
        await _settle(sync)
        payloads = caller.register_payloads()
        assert len(payloads) == 1
        assert payloads[0]["trigger_type"] == "schedule"
        # 周一 NOW → weekly:1（周二）= 次日 09:00（scheduled_at 为 ISO T 分隔）
        assert "2026-10-13T09:00" in payloads[0]["scheduled_at"]

    async def test_daily_before_9am_registers_today(
        self, tmp_path: Path, caller: FakeCaller
    ) -> None:
        """AC-3：当日 09:00 未过 → 当日（clock=08:00）。"""
        early = datetime(2026, 10, 12, 8, 0, 0)
        store2 = pim_store.PimStore(tmp_path / "data2", clock=lambda: early)
        sync = pim_trigger_sync.PimTriggerSync(store2, caller, clock=lambda: early)
        store2.set_on_change(sync.on_change)
        await _activate(sync)
        store2.create(
            "todo",
            {"title": "晨报", "repeat": "daily", "reminder_mode": "agent_classified"},
        )
        await _settle(sync)
        payloads = caller.register_payloads()
        assert len(payloads) == 1
        assert "2026-10-12T09:00" in payloads[0]["scheduled_at"]

    async def test_occurrence_deterministic_per_clock(self, store: Any) -> None:
        caller = FakeCaller()
        sync = _make_sync(store, caller)
        record = {"id": "x", "title": "t", "repeat": "daily"}
        a = sync._occurrence("todo", record)
        b = sync._occurrence("todo", record)
        assert a == b == "2026-10-13 09:00"

    async def test_non_repeat_no_due_not_registered(
        self, store: Any, caller: FakeCaller
    ) -> None:
        """AC-2 现状保持：非 repeat 卡无时间坐标 → 不注册。"""
        sync = _make_sync(store, caller)
        await _activate(sync)
        store.create("todo", {"title": "无期事项"})
        await _settle(sync)
        assert caller.register_payloads() == []


class TestReminderModeRouting:
    """P0④分流：direct（含缺省）到点直接弹卡不进 LLM 管道 → 不注册触发器，
    已有触发器注销；agent_classified 保持事实消息注册链路。"""

    async def test_agent_classified_registers(
        self, store: Any, caller: FakeCaller
    ) -> None:
        sync = _make_sync(store, caller)
        await _activate(sync)
        store.create(
            "todo",
            {"title": "A", "due": "2026-10-12 10:00", "reminder_mode": "agent_classified"},
        )
        await _settle(sync)
        assert len(caller.register_payloads()) == 1

    async def test_default_direct_skips_registration(
        self, store: Any, caller: FakeCaller
    ) -> None:
        sync = _make_sync(store, caller)
        await _activate(sync)
        store.create("todo", {"title": "A", "due": "2026-10-12 10:00"})
        await _settle(sync)
        assert caller.register_payloads() == []

    async def test_direct_unregisters_existing_trigger(
        self, store: Any, caller: FakeCaller
    ) -> None:
        """缺省 direct 卡已有触发器（先 agent_classified 后改回）→ 注销。"""
        sync = _make_sync(store, caller)
        await _activate(sync)
        todo, _ = store.create(
            "todo",
            {"title": "A", "due": "2026-10-12 10:00", "reminder_mode": "agent_classified"},
        )
        await _settle(sync)
        assert len(caller.register_payloads()) == 1
        store.update("todo", todo["id"], {"reminder_mode": "direct"})
        await _settle(sync)
        assert "unregister" in caller.methods()
