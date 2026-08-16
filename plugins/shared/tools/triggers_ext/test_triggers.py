# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: none-local
"""triggers_ext 插件（触发器管理 + 设置工具）单元测试。

覆盖（对齐 plugins/shared/tools/triggers_ext/）：
1. triggers/manager.py —— 注册/注销、事件/条件评估、定时/延迟/周期检查、
   停止条件、事件总线订阅、触发消息注入（0.2 injector 路径 + 0.1 回退降级）
2. tool.py —— TriggerSetupTool 五种触发类型 + cancel/update + 参数校验
3. server.py —— on_load/on_unload 接线 + trigger_setup 工具转发

外部依赖：pipeline.condition_parser（0.2 不存在）用 sys.modules 伪模块注入
验证条件触发主路径；消息注入用伪 injector + 独立线程事件循环，不依赖内核。
"""

from __future__ import annotations

import asyncio
import datetime
import importlib.util
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent  # plugins/shared/tools/triggers_ext/
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

# 真实 triggers 子包（manager/types）经 sys.path 正常导入
from triggers.manager import TriggerManager  # noqa: E402
from triggers.types import TriggerConfig, TriggerStatus, TriggerType, parse_duration  # noqa: E402


def _load_tool() -> Any:
    """动态加载 tool.py（唯一模块名，避免与其它插件的 tool.py 撞名）。"""
    mod_name = "triggers_ext_tool_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "tool.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _load_server() -> Any:
    mod_name = "triggers_ext_server_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "server.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_config(**overrides: Any) -> TriggerConfig:
    base = {
        "trigger_id": "t1",
        "name": "测试触发器",
        "trigger_type": TriggerType.EVENT,
        "max_fires": 1,
        "message": "到点了",
        "pipeline_id": "pipe-1",
    }
    base.update(overrides)
    return TriggerConfig(**base)


class _LoopThread:
    """在独立线程运行事件循环（供 run_coroutine_threadsafe 使用）。"""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True)

    def __enter__(self) -> _LoopThread:
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5)
        self.loop.close()


# ═══════════════════════════════════════════════════════════
# TriggerManager：注册 / 注销 / 查询
# ═══════════════════════════════════════════════════════════


class TestManagerRegister:
    def test_register_sets_active_and_metadata(self) -> None:
        mgr = TriggerManager()
        try:
            cfg = _make_config()
            mgr.register(cfg)
            assert cfg.status == TriggerStatus.ACTIVE
            assert "register_time" in cfg.metadata
            assert cfg.metadata["last_fire_time"] is None
            assert mgr.get("t1") is cfg
        finally:
            mgr.stop_check_loop()

    def test_unregister(self) -> None:
        mgr = TriggerManager()
        cfg = _make_config()
        mgr.register(cfg)
        assert mgr.unregister("t1") is True
        assert mgr.unregister("t1") is False
        assert mgr.get("t1") is None

    def test_list_by_type_and_active(self) -> None:
        mgr = TriggerManager()
        try:
            mgr.register(_make_config(trigger_id="e1", trigger_type=TriggerType.EVENT))
            mgr.register(_make_config(trigger_id="d1", trigger_type=TriggerType.DELAY))
            assert {t.trigger_id for t in mgr.list_by_type(TriggerType.EVENT)} == {"e1"}
            assert {t.trigger_id for t in mgr.list_active()} == {"e1", "d1"}
        finally:
            mgr.stop_check_loop()

    def test_update_max_fires(self) -> None:
        mgr = TriggerManager()
        try:
            cfg = _make_config(max_fires=1)
            mgr.register(cfg)
            cfg.fire_count = 1  # 模拟已触发
            mgr.update_max_fires("t1", 3)
            assert cfg.max_fires == 3
            # FIRED → 自动重新 ACTIVE
            assert cfg.status == TriggerStatus.ACTIVE

            mgr.update_max_fires("t1", 5, max_time_seconds=100.0)
            assert cfg.max_time_seconds == 100.0

            assert mgr.update_max_fires("missing", 1) is False
        finally:
            mgr.stop_check_loop()

    def test_update_max_fires_cancelled_rejected(self) -> None:
        mgr = TriggerManager()
        try:
            cfg = _make_config()
            mgr.register(cfg)
            mgr.cancel("t1")
            assert mgr.update_max_fires("t1", 5) is False
        finally:
            mgr.stop_check_loop()

    def test_cancel(self) -> None:
        mgr = TriggerManager()
        try:
            cfg = _make_config()
            mgr.register(cfg)
            assert mgr.cancel("t1") is True
            assert cfg.status == TriggerStatus.CANCELLED
            assert mgr.cancel("t1") is False  # 已取消
            assert mgr.cancel("missing") is False
        finally:
            mgr.stop_check_loop()


# ═══════════════════════════════════════════════════════════
# 事件 / 条件评估
# ═══════════════════════════════════════════════════════════


class TestEvaluateEvent:
    def test_event_matches_and_fires(self) -> None:
        mgr = TriggerManager()
        cfg = _make_config(event_name="task_completed", event_filter={"task_id": "x-1"}, max_fires=2)
        mgr.register(cfg)
        fired = mgr.evaluate_event("task_completed", {"task_id": "x-1"})
        assert fired == ["t1"]
        assert cfg.fire_count == 1
        assert cfg.status == TriggerStatus.ACTIVE  # 未达 max_fires

    def test_event_reaches_max_fires(self) -> None:
        mgr = TriggerManager()
        cfg = _make_config(event_name="task_completed", max_fires=1)
        mgr.register(cfg)
        mgr.evaluate_event("task_completed", {})
        assert cfg.status == TriggerStatus.FIRED
        # FIRED 后不再触发
        assert mgr.evaluate_event("task_completed", {}) == []

    def test_event_filter_ops(self) -> None:
        mgr = TriggerManager()
        cfg = _make_config(event_name="e", event_filter={"prio": {"op": "gte", "value": 3}})
        mgr.register(cfg)
        assert mgr.evaluate_event("e", {"prio": 5}) == ["t1"]
        assert mgr.evaluate_event("e", {"prio": 2}) == []

    def test_event_skipped_on_name_type_status_mismatch(self) -> None:
        mgr = TriggerManager()
        mgr.register(_make_config(trigger_id="a", event_name="other"))
        inactive = _make_config(trigger_id="b", event_name="e")
        inactive.status = TriggerStatus.PENDING
        mgr._triggers["b"] = inactive
        assert mgr.evaluate_event("e", {}) == []

    def test_event_stop_condition_blocks(self) -> None:
        """max_time_seconds 已超 → 不触发。"""
        mgr = TriggerManager()
        past = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=2)).isoformat()
        cfg = _make_config(event_name="e", max_time_seconds=60, metadata={"register_time": past})
        mgr.register(cfg)
        assert mgr.evaluate_event("e", {}) == []

    def test_compare_operators(self) -> None:
        mgr = TriggerManager()
        assert mgr._compare(1, "eq", 1) is True
        assert mgr._compare(1, "ne", 2) is True
        assert mgr._compare(2, "gt", 1) is True
        assert mgr._compare(1, "lt", 2) is True
        assert mgr._compare(2, "gte", 2) is True
        assert mgr._compare(2, "lte", 2) is True
        assert mgr._compare("abc", "contains", "b") is True
        assert mgr._compare(1, "bogus", 1) is False


class TestEvaluateCondition:
    def test_condition_with_local_parser(self) -> None:
        """本地 condition_parser（triggers/condition_parser.py，GAP-2 回移）→ 条件评估主路径。"""
        mgr = TriggerManager()
        cfg = _make_config(trigger_type=TriggerType.CONDITION, condition_expression="ready == true")
        mgr.register(cfg)
        assert mgr.evaluate_condition({"ready": True}) == ["t1"]
        assert cfg.fire_count == 1
        # 条件不满足 → 不触发（且不产生新边沿）
        assert mgr.evaluate_condition({"ready": False}) == []

    def test_condition_invalid_syntax_degrades(self) -> None:
        """非法表达式 → 求值安全兜底为 False，不崩溃不触发（GAP-2）。"""
        mgr = TriggerManager()
        cfg = _make_config(trigger_type=TriggerType.CONDITION, condition_expression="!!!invalid")
        mgr.register(cfg)
        assert mgr.evaluate_condition({"x": 1}) == []

    def test_condition_skipped_without_expression(self) -> None:
        mgr = TriggerManager()
        cfg = _make_config(trigger_type=TriggerType.CONDITION, condition_expression="")
        mgr.register(cfg)
        assert mgr.evaluate_condition({}) == []


# ═══════════════════════════════════════════════════════════
# 定时 / 延迟 / 周期检查
# ═══════════════════════════════════════════════════════════


class TestCheckScheduled:
    def _now(self) -> datetime.datetime:
        return datetime.datetime.now(datetime.UTC)

    def test_delay_due(self) -> None:
        mgr = TriggerManager()
        past = (self._now() - datetime.timedelta(seconds=30)).isoformat()
        cfg = _make_config(trigger_type=TriggerType.DELAY, delay_seconds=10, metadata={"register_time": past})
        mgr.register(cfg)
        assert mgr.check_scheduled(self._now()) == ["t1"]
        assert cfg.fire_count == 1

    def test_delay_not_due(self) -> None:
        mgr = TriggerManager()
        now = self._now()
        future = (now + datetime.timedelta(seconds=30)).isoformat()
        cfg = _make_config(trigger_type=TriggerType.DELAY, delay_seconds=10, metadata={"register_time": future})
        mgr.register(cfg)
        assert mgr.check_scheduled(now) == []

    def test_delay_invalid_state(self) -> None:
        mgr = TriggerManager()
        assert mgr._check_delay(_make_config(trigger_type=TriggerType.DELAY, delay_seconds=0), self._now()) is False
        cfg = _make_config(trigger_type=TriggerType.DELAY, delay_seconds=5, metadata={"register_time": "not-a-date"})
        mgr.register(cfg)
        assert mgr._check_delay(cfg, self._now()) is False
        # 非 DELAY 类型
        assert mgr._check_delay(_make_config(trigger_type=TriggerType.EVENT), self._now()) is False

    def test_scheduled_time(self) -> None:
        mgr = TriggerManager()
        now = self._now()
        past = (now - datetime.timedelta(minutes=1)).isoformat()
        cfg = _make_config(
            trigger_type=TriggerType.SCHEDULED,
            scheduled_at=datetime.datetime.fromisoformat(past),
        )
        mgr.register(cfg)
        assert mgr.check_scheduled(now) == ["t1"]
        # 未到时间
        future = (now + datetime.timedelta(hours=1)).isoformat()
        cfg2 = _make_config(
            trigger_id="t2",
            trigger_type=TriggerType.SCHEDULED,
            scheduled_at=datetime.datetime.fromisoformat(future),
        )
        mgr.register(cfg2)
        assert mgr.check_scheduled(now) == []

    def test_scheduled_naive_datetime_normalized(self) -> None:
        """naive scheduled_at 视为 UTC，不抛 TypeError。"""
        mgr = TriggerManager()
        cfg = _make_config(trigger_type=TriggerType.SCHEDULED, scheduled_at=datetime.datetime(2020, 1, 1))
        mgr.register(cfg)
        assert mgr.check_scheduled(self._now()) == ["t1"]
        assert mgr._check_scheduled_time(_make_config(trigger_type=TriggerType.SCHEDULED, scheduled_at=None), self._now()) is False
        assert mgr._check_scheduled_time(_make_config(trigger_type=TriggerType.EVENT), self._now()) is False

    def test_normalize_datetime(self) -> None:
        naive = datetime.datetime(2026, 1, 1, 12, 0, 0)
        aware_utc = TriggerManager._normalize_datetime(naive)
        assert aware_utc.tzinfo is not None and aware_utc.utcoffset() == datetime.timedelta(0)
        other = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
        assert TriggerManager._normalize_datetime(other).hour == 4  # +8 → UTC

    def test_interval_first_and_subsequent(self) -> None:
        mgr = TriggerManager()
        now = self._now()
        past = (now - datetime.timedelta(seconds=30)).isoformat()
        cfg = _make_config(
            trigger_type=TriggerType.INTERVAL,
            interval_seconds=20,
            max_fires=0,
            metadata={"register_time": past},
        )
        mgr.register(cfg)
        # 首次触发基于 register_time
        assert mgr.check_scheduled(now) == ["t1"]
        # 触发后基于 last_fire_time（刚触发 → 未到下次）
        assert mgr.check_scheduled(now) == []
        # 时间推进 → 再次触发
        later = now + datetime.timedelta(seconds=30)
        assert mgr.check_scheduled(later) == ["t1"]

    def test_interval_invalid(self) -> None:
        mgr = TriggerManager()
        assert mgr._check_interval(_make_config(trigger_type=TriggerType.INTERVAL, interval_seconds=0), self._now()) is False
        cfg = _make_config(
            trigger_type=TriggerType.INTERVAL,
            interval_seconds=60,
            metadata={"register_time": "bad"},
        )
        mgr.register(cfg)
        assert mgr._check_interval(cfg, self._now()) is False
        cfg2 = _make_config(trigger_id="t2", trigger_type=TriggerType.INTERVAL, interval_seconds=60, fire_count=1)
        mgr.register(cfg2)
        assert mgr._check_interval(cfg2, self._now()) is False  # 无 last_fire_time 且 fire_count>0

    def test_check_scheduled_stop_condition_marks_fired(self) -> None:
        """超过 max_time_seconds → 状态置 FIRED 并跳过。"""
        mgr = TriggerManager()
        past = (self._now() - datetime.timedelta(hours=2)).isoformat()
        cfg = _make_config(trigger_type=TriggerType.DELAY, delay_seconds=10, max_time_seconds=60, metadata={"register_time": past})
        mgr.register(cfg)
        assert mgr.check_scheduled(self._now()) == []
        assert cfg.status == TriggerStatus.FIRED

    def test_check_stop_conditions(self) -> None:
        mgr = TriggerManager()
        cfg = _make_config(max_time_seconds=0)
        assert mgr._check_stop_conditions(cfg) is True
        now = self._now()
        past = (now - datetime.timedelta(seconds=30)).isoformat()
        cfg2 = _make_config(max_time_seconds=10, metadata={"register_time": past})
        assert mgr._check_stop_conditions(cfg2, now) is False
        # register_time 非法 → 忽略超时检查
        cfg3 = _make_config(max_time_seconds=10, metadata={"register_time": "garbage"})
        assert mgr._check_stop_conditions(cfg3, now) is True


# ═══════════════════════════════════════════════════════════
# 事件总线桥接 + 消息注入
# ═══════════════════════════════════════════════════════════


class _FakeBus:
    def __init__(self, subscribe_error: bool = False) -> None:
        self.handler = None
        self._subscribe_error = subscribe_error

    def subscribe(self, handler, event_filter=None) -> None:
        if self._subscribe_error:
            raise RuntimeError("bus down")
        self.handler = handler


class TestEventBusBridge:
    def test_subscribe_and_forward(self) -> None:
        mgr = TriggerManager()
        bus = _FakeBus()
        cfg = _make_config(event_name="task_completed", max_fires=2)
        mgr.register(cfg)
        mgr.subscribe_to_event_bus(bus)
        assert bus.handler is not None
        # 触发总线处理器 → 转发为 task_completed 事件
        event = types.SimpleNamespace(data={"new_status": "completed", "task_id": "x"})
        _run(bus.handler(event))
        assert cfg.fire_count == 1
        # 无 new_status → 不转发
        _run(bus.handler(types.SimpleNamespace(data={})))
        assert cfg.fire_count == 1

    def test_subscribe_error_degrades(self) -> None:
        mgr = TriggerManager()
        mgr.subscribe_to_event_bus(_FakeBus(subscribe_error=True))  # 不抛异常

    def test_on_system_event(self) -> None:
        mgr = TriggerManager()
        cfg = _make_config(event_name="task_failed", max_fires=2)
        mgr.register(cfg)
        assert _run(mgr.on_system_event("task_failed", {})) == ["t1"]


class TestInjectTriggerMessage:
    def test_injector_path_success(self) -> None:
        """注入器存在 → 经 run_coroutine_threadsafe 投递到主循环。"""
        calls: list[tuple] = []

        async def fake_injector(pipeline_id: str, message: str, user_id: str) -> str:
            calls.append((pipeline_id, message, user_id))
            return "ok"

        with _LoopThread() as lt:
            mgr = TriggerManager()
            mgr.set_main_loop(lt.loop)
            mgr.set_injector(fake_injector)
            cfg = _make_config(name="定时提醒", max_fires=3, metadata={"user_id": "u-9"})
            mgr.register(cfg)
            mgr._inject_trigger_message(cfg)
            assert len(calls) == 1
            assert calls[0][0] == "pipe-1"
            assert "定时提醒" in calls[0][1]
            assert calls[0][2] == "u-9"
            mgr.stop_check_loop()

    def test_injector_path_error_handled(self) -> None:
        """注入器抛异常 → 记录错误不崩溃。"""
        async def bad_injector(pipeline_id: str, message: str, user_id: str) -> Any:
            raise RuntimeError("kernel down")

        with _LoopThread() as lt:
            mgr = TriggerManager()
            mgr.set_main_loop(lt.loop)
            mgr.set_injector(bad_injector)
            cfg = _make_config()
            mgr.register(cfg)
            mgr._inject_trigger_message(cfg)  # 不抛异常
            mgr.stop_check_loop()

    def test_no_loop_skips(self) -> None:
        """主循环未设置 → 跳过注入。"""
        mgr = TriggerManager()
        cfg = _make_config()
        mgr.register(cfg)
        mgr._inject_trigger_message(cfg)  # 不抛异常
        mgr.stop_check_loop()

    def test_fallback_without_injector(self) -> None:
        """注入器未设置 → 回退 0.1 message_bus 不存在（0.2 已删）→ 记录后返回。"""
        sys.modules.pop("pipeline.message_bus", None)
        with _LoopThread() as lt:
            mgr = TriggerManager()
            mgr.set_main_loop(lt.loop)
            cfg = _make_config()
            mgr.register(cfg)
            mgr._inject_trigger_message(cfg)  # 不抛异常
            mgr.stop_check_loop()

    def test_check_loop_sync_drives_injector(self) -> None:
        """后台检查循环：到期触发器经注入器投递消息。"""
        import time

        fired = threading.Event()
        received: list[str] = []

        async def fake_injector(pipeline_id: str, message: str, user_id: str) -> str:
            received.append(message)
            fired.set()
            return "ok"

        with _LoopThread() as lt:
            mgr = TriggerManager()
            mgr.set_main_loop(lt.loop)
            mgr.set_injector(fake_injector)
            past = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=60)).isoformat()
            cfg = _make_config(
                trigger_type=TriggerType.DELAY,
                delay_seconds=10,
                metadata={"register_time": past},
            )
            mgr.register(cfg)
            mgr._TRIGGER_CHECK_INTERVAL = 0.01  # type: ignore[misc]
            t = threading.Thread(target=mgr._check_loop_sync, daemon=True)
            t.start()
            assert fired.wait(timeout=5), "检查循环应在超时内投递消息"
            mgr.stop_check_loop()
            t.join(timeout=5)
            assert received and "到点了" in received[0]

    def test_check_loop_error_continues(self) -> None:
        """check_scheduled 抛异常 → 循环捕获后继续。"""
        mgr = TriggerManager()

        def _boom(now):
            raise RuntimeError("boom")

        mgr.check_scheduled = _boom  # type: ignore[method-assign]
        mgr._TRIGGER_CHECK_INTERVAL = 0.01  # type: ignore[misc]
        t = threading.Thread(target=mgr._check_loop_sync, daemon=True)
        t.start()
        mgr._running = True
        time.sleep(0.2)
        mgr.stop_check_loop()
        t.join(timeout=5)
        assert t.is_alive() is False


# ═══════════════════════════════════════════════════════════
# TriggerSetupTool
# ═══════════════════════════════════════════════════════════


class TestSetupTool:
    @pytest.fixture
    def tool(self) -> Any:
        import triggers.manager as manager_mod

        mod = _load_tool()
        fresh = TriggerManager()
        # 每次测试用全新 manager（monkeypatch get_trigger_manager 返回它）
        mod.get_trigger_manager = lambda: fresh
        inst = mod.TriggerSetupTool()
        assert inst._manager is fresh
        yield inst
        fresh.stop_check_loop()

    def test_missing_trigger_type(self, tool: Any) -> None:
        r = _run(tool.execute({"action": "setup", "message": "m", "pipeline_id": "p"}))
        assert not r.success and r.error_code == "MISSING_TRIGGER_TYPE"

    def test_missing_message(self, tool: Any) -> None:
        r = _run(tool.execute({"trigger_type": "delay", "pipeline_id": "p"}))
        assert not r.success and r.error_code == "MISSING_MESSAGE"

    def test_missing_pipeline_id(self, tool: Any) -> None:
        r = _run(tool.execute({"trigger_type": "delay", "message": "m"}))
        assert not r.success and r.error_code == "MISSING_PIPELINE_ID"

    def test_invalid_trigger_type(self, tool: Any) -> None:
        r = _run(tool.execute({"trigger_type": "magic", "message": "m", "pipeline_id": "p"}))
        assert not r.success and r.error_code == "INVALID_TRIGGER_TYPE"

    def test_delay_setup_success(self, tool: Any) -> None:
        r = _run(tool.execute({"trigger_type": "delay", "delay_seconds": 60, "message": "m", "pipeline_id": "p"}))
        assert r.success
        assert r.output["trigger_type"] == "delay"
        assert "60" in r.output["message"]
        assert len(tool._manager._triggers) == 1

    def test_delay_validation(self, tool: Any) -> None:
        base = {"trigger_type": "delay", "message": "m", "pipeline_id": "p"}
        assert _run(tool.execute(base)).error_code == "MISSING_DELAY_SECONDS"
        assert _run(tool.execute({**base, "delay_seconds": "abc"})).error_code == "INVALID_DELAY_SECONDS"
        assert _run(tool.execute({**base, "delay_seconds": 0})).error_code == "INVALID_DELAY_SECONDS"
        assert _run(tool.execute({**base, "delay_seconds": 999999})).error_code == "DELAY_EXCEEDS_LIMIT"

    def test_schedule_setup_success(self, tool: Any, monkeypatch) -> None:
        # naive 时间按 APP_TIMEZONE（默认 Asia/Shanghai，+8）解释，
        # 需预留时区偏移，取 now+30h 保证解释后仍在未来
        future = (datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%S")
        r = _run(tool.execute({"trigger_type": "schedule", "schedule_time": future, "message": "m", "pipeline_id": "p"}))
        assert r.success
        assert r.output["trigger_type"] == "schedule"
        cfg = next(iter(tool._manager._triggers.values()))
        assert cfg.scheduled_at is not None and cfg.scheduled_at.tzinfo is not None

    def test_schedule_with_z_suffix(self, tool: Any) -> None:
        future = (datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        r = _run(tool.execute({"trigger_type": "schedule", "schedule_time": future, "message": "m", "pipeline_id": "p"}))
        assert r.success

    def test_schedule_invalid_timezone_falls_back_utc(self, tool: Any, monkeypatch) -> None:
        future = (datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")
        fake_settings = types.SimpleNamespace(timezone="Not/AZone")
        # 必须 patch fixture 加载的那份 tool 模块（sys.modules 中唯一实例）
        monkeypatch.setattr(sys.modules["triggers_ext_tool_test"], "get_settings", lambda: fake_settings)
        r = _run(tool.execute({"trigger_type": "schedule", "schedule_time": future, "message": "m", "pipeline_id": "p"}))
        assert r.success  # 回退 UTC 解释

    def test_schedule_validation(self, tool: Any) -> None:
        base = {"trigger_type": "schedule", "message": "m", "pipeline_id": "p"}
        assert _run(tool.execute(base)).error_code == "MISSING_SCHEDULE_TIME"
        assert _run(tool.execute({**base, "schedule_time": "not-a-date"})).error_code == "INVALID_SCHEDULE_TIME"
        past = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=1)).isoformat()
        assert _run(tool.execute({**base, "schedule_time": past})).error_code == "SCHEDULE_TIME_IN_PAST"
        far = (datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=30)).isoformat()
        assert _run(tool.execute({**base, "schedule_time": far})).error_code == "SCHEDULE_TIME_EXCEEDS_LIMIT"

    def test_interval_setup_success(self, tool: Any) -> None:
        r = _run(tool.execute({"trigger_type": "interval", "interval": "1h30m", "message": "m", "pipeline_id": "p", "max_count": 3, "max_time": "2d"}))
        assert r.success
        assert r.output["interval_seconds"] == 5400
        assert r.output["max_count"] == 3
        cfg = next(iter(tool._manager._triggers.values()))
        assert cfg.max_fires == 3 and cfg.max_time_seconds == 2 * 86400

    def test_interval_default_max_count_one(self, tool: Any) -> None:
        r = _run(tool.execute({"trigger_type": "interval", "interval": "10s", "message": "m", "pipeline_id": "p"}))
        assert r.success and r.output["max_count"] == 1

    def test_interval_validation(self, tool: Any) -> None:
        base = {"trigger_type": "interval", "message": "m", "pipeline_id": "p"}
        assert _run(tool.execute(base)).error_code == "MISSING_INTERVAL"
        assert _run(tool.execute({**base, "interval": "xyz"})).error_code == "INVALID_INTERVAL"
        assert _run(tool.execute({**base, "interval": "5s"})).error_code == "INTERVAL_TOO_SHORT"
        assert _run(tool.execute({**base, "interval": "31d"})).error_code == "INTERVAL_EXCEEDS_LIMIT"

    def test_event_setup_success(self, tool: Any) -> None:
        r = _run(tool.execute({"trigger_type": "event", "event_type": "task_completed", "message": "m", "pipeline_id": "p"}))
        assert r.success
        assert _run(tool.execute({"trigger_type": "event", "message": "m", "pipeline_id": "p"})).error_code == "MISSING_EVENT_TYPE"

    def test_condition_setup_success(self, tool: Any) -> None:
        r = _run(tool.execute({"trigger_type": "condition", "condition": "a == 1", "message": "m", "pipeline_id": "p"}))
        assert r.success
        assert _run(tool.execute({"trigger_type": "condition", "message": "m", "pipeline_id": "p"})).error_code == "MISSING_CONDITION"

    def test_trigger_limit_exceeded(self, tool: Any) -> None:
        """同管道超过 MAX_TRIGGERS_PER_SESSION → 拒绝。"""
        base = {"trigger_type": "delay", "delay_seconds": 3600, "message": "m", "pipeline_id": "p"}
        for _ in range(tool.MAX_TRIGGERS_PER_SESSION):
            assert _run(tool.execute(base)).success
        r = _run(tool.execute(base))
        assert not r.success and r.error_code == "TRIGGER_LIMIT_EXCEEDED"

    def test_auto_execution_id(self, tool: Any) -> None:
        """未注入 execution_id → 自动生成。"""
        r = _run(tool.execute({"trigger_type": "delay", "delay_seconds": 3600, "message": "m", "pipeline_id": "p"}))
        assert r.success
        cfg = next(iter(tool._manager._triggers.values()))
        assert cfg.metadata["execution_id"].startswith("exec_")

    def test_setup_exception_degrades(self, tool: Any, monkeypatch) -> None:
        """内部异常 → TRIGGER_SETUP_FAILED。"""
        async def _boom(self, inputs, execution_id, pipeline_id, message):
            raise RuntimeError("unexpected")

        monkeypatch.setattr(tool, "_setup_delay_trigger", _boom)
        r = _run(tool.execute({"trigger_type": "delay", "delay_seconds": 60, "message": "m", "pipeline_id": "p"}))
        assert not r.success and r.error_code == "TRIGGER_SETUP_FAILED"

    def test_cancel_flow(self, tool: Any) -> None:
        r = _run(tool.execute({"action": "cancel", "trigger_id": "ghost", "pipeline_id": "p"}))
        assert not r.success and r.error_code == "TRIGGER_NOT_FOUND"
        assert _run(tool.execute({"action": "cancel", "pipeline_id": "p"})).error_code == "MISSING_TRIGGER_ID"

        setup = _run(tool.execute({"trigger_type": "delay", "delay_seconds": 3600, "message": "m", "pipeline_id": "p"}))
        tid = setup.output["trigger_id"]
        # 管道不匹配
        r = _run(tool.execute({"action": "cancel", "trigger_id": tid, "pipeline_id": "other"}))
        assert not r.success and r.error_code == "TRIGGER_PIPELINE_MISMATCH"
        # 成功取消
        r = _run(tool.execute({"action": "cancel", "trigger_id": tid, "pipeline_id": "p"}))
        assert r.success and r.output["action"] == "cancel"
        # 已取消 → 再次取消失败
        r = _run(tool.execute({"action": "cancel", "trigger_id": tid, "pipeline_id": "p"}))
        assert not r.success and r.error_code == "TRIGGER_CANCEL_FAILED"

    def test_update_flow(self, tool: Any) -> None:
        assert _run(tool.execute({"action": "update", "pipeline_id": "p"})).error_code == "MISSING_TRIGGER_ID"
        assert _run(tool.execute({"action": "update", "trigger_id": "ghost", "pipeline_id": "p"})).error_code == "TRIGGER_NOT_FOUND"

        setup = _run(tool.execute({"trigger_type": "delay", "delay_seconds": 3600, "message": "m", "pipeline_id": "p"}))
        tid = setup.output["trigger_id"]
        r = _run(tool.execute({"action": "update", "trigger_id": tid, "pipeline_id": "other", "max_count": 5}))
        assert not r.success and r.error_code == "TRIGGER_PIPELINE_MISMATCH"
        r = _run(tool.execute({"action": "update", "trigger_id": tid, "pipeline_id": "p"}))
        assert not r.success and r.error_code == "MISSING_UPDATE_PARAMS"
        r = _run(tool.execute({"action": "update", "trigger_id": tid, "pipeline_id": "p", "max_count": 5, "max_time": "1h"}))
        assert r.success
        assert r.output["new_max_fires"] == 5
        assert r.output["new_max_time_seconds"] == 3600

    def test_parse_max_count(self, tool: Any) -> None:
        assert tool._parse_max_count(None) == 0
        assert tool._parse_max_count(3) == 3
        assert tool._parse_max_count(-1) == 0

    def test_get_tool_definition(self) -> None:
        _load_tool().TriggerSetupTool.get_tool_definition()  # 不抛异常


class TestParseDuration:
    def test_parse_duration_formats(self) -> None:
        assert parse_duration("30s") == 30
        assert parse_duration("5m") == 300
        assert parse_duration("2h") == 7200
        assert parse_duration("3d") == 259200
        assert parse_duration("1h30m") == 5400
        assert parse_duration("2d 6h") == 2 * 86400 + 6 * 3600

    def test_parse_duration_invalid(self) -> None:
        with pytest.raises(ValueError):
            parse_duration("")
        with pytest.raises(ValueError):
            parse_duration("xyz")
        with pytest.raises(ValueError):
            parse_duration("0s")


# ═══════════════════════════════════════════════════════════
# server.py：on_load 接线 + 工具转发
# ═══════════════════════════════════════════════════════════


class TestServer:
    def test_on_load_and_unload_wiring(self) -> None:
        import triggers.manager as manager_mod

        server = _load_server()
        real_mgr = manager_mod.get_trigger_manager()
        real_mgr.stop_check_loop()
        real_mgr._triggers.clear()
        try:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(server._on_load({}))
                assert real_mgr._injector is not None
                assert real_mgr._main_loop is loop
                assert real_mgr._check_thread is not None
                loop.run_until_complete(server._on_unload({}))
            finally:
                loop.close()
            assert real_mgr._running is False
        finally:
            real_mgr.stop_check_loop()

    def test_trigger_injector_calls_chat_capability(self) -> None:
        server = _load_server()

        class FakeChat:
            def __init__(self) -> None:
                self.calls: list[tuple] = []

            async def call(self, method: str, params: dict) -> dict:
                self.calls.append((method, params))
                return {"ok": True}

        chat = FakeChat()
        server.plugin.get_capability = lambda name: chat  # type: ignore[method-assign]
        injector = server._make_trigger_injector()
        result = _run(injector("pipe-1", "hello", "u-1"))
        assert result == {"ok": True}
        assert chat.calls == [("send_message", {"pipeline_id": "pipe-1", "message": "hello", "user_id": "u-1"})]

    def test_trigger_setup_tool_forward(self) -> None:
        server = _load_server()
        result = _run(server.trigger_setup(trigger_type="delay", delay_seconds=3600, message="m", pipeline_id="p"))
        assert result.get("success") is True
        # 失败路径 → 返回 {"error": ...}
        result = _run(server.trigger_setup(trigger_type="delay", message="m"))
        assert "error" in result


# ═══════════════════════════════════════════════════════════
# GAP-2：EVENT/CONDITION 触发器接线
# - condition_parser 本地回移（triggers/condition_parser.py，无动态求值）
# - 边沿检测（false→true 翻转才触发，持续满足不重复）
# - state 聚合轮询（set_state_provider + _poll_conditions）
# - 域事件桥（handle_domain_event + server on_domain_event 接线）
# - 注册防御（桥未就绪 → 明确警告，不静默）
# ═══════════════════════════════════════════════════════════


class TestConditionParserGAP2:
    """本地安全条件求值器（Rust engine/condition.rs 的 Python 回移 + 扁平键支持）。"""

    def test_flat_dotted_keys(self) -> None:
        """state 聚合行是扁平点号键（task.status / track.total_tokens 同款）。"""
        from triggers.condition_parser import parse_condition

        ctx = {"task.status": "failed", "task.goal": "喝水提醒"}
        assert parse_condition("task.status == 'failed'", ctx) is True
        assert parse_condition("task.status == 'completed'", ctx) is False
        assert parse_condition("task.goal == '喝水提醒'", ctx) is True

    def test_nested_dotted_path(self) -> None:
        """嵌套 dict 点链访问（与 0.1/Rust 版一致的解析语义）。"""
        from triggers.condition_parser import parse_condition

        ctx = {"execution_context": {"workspace": {"mode": "worktree"}}}
        assert parse_condition("execution_context.workspace.mode == 'worktree'", ctx) is True
        assert parse_condition("execution_context.workspace.mode == 'plain'", ctx) is False

    def test_operators_and_logic(self) -> None:
        from triggers.condition_parser import parse_condition

        ctx = {"n": 5, "a": True, "b": False, "xs": [1, 2, 3]}
        assert parse_condition("n > 3", ctx) is True
        assert parse_condition("n >= 5 and n <= 5", ctx) is True
        assert parse_condition("n < 3 or a == true", ctx) is True
        assert parse_condition("not b", ctx) is True
        assert parse_condition("xs == [1, 2, 3]", ctx) is True
        assert parse_condition("xs != []", ctx) is True

    def test_missing_var_and_literals(self) -> None:
        from triggers.condition_parser import parse_condition

        assert parse_condition("missing == None", {}) is True
        assert parse_condition("missing == 'x'", {}) is False
        assert parse_condition("", {}) is True  # 空表达式恒真（与 0.1 一致）
        assert parse_condition("True", {}) is True

    def test_invalid_or_unsafe_expression_is_false(self) -> None:
        """语法错误 / 注入尝试 → 安全兜底 False，绝不 eval。"""
        from triggers.condition_parser import parse_condition

        assert parse_condition("!!!invalid", {}) is False
        assert parse_condition("__import__('os').system('rm -rf')", {}) is False
        assert parse_condition("a == ", {}) is False

    def test_flat_key_preferred_over_nested(self) -> None:
        """同名时扁平键优先（STATE_SUMMARY_KEYS 约定），嵌套结构仍可回退。"""
        from triggers.condition_parser import parse_condition

        ctx = {"task.status": "running", "task": {"status": "failed"}}
        assert parse_condition("task.status == 'running'", ctx) is True


class TestConditionEdgeDetection:
    """CONDITION 边沿检测：仅 false→true 翻转触发一次（GAP-2 定案）。"""

    def _cond_mgr(self) -> tuple[TriggerManager, TriggerConfig]:
        mgr = TriggerManager()
        cfg = _make_config(
            trigger_type=TriggerType.CONDITION,
            condition_expression="task.status == 'failed'",
            max_fires=0,
        )
        mgr.register(cfg)
        return mgr, cfg

    def test_unknown_to_true_fires_once(self) -> None:
        """注册时已为真 → 计一次边沿（追溯性）。"""
        mgr, cfg = self._cond_mgr()
        try:
            assert mgr.evaluate_condition({"task.status": "failed"}) == ["t1"]
            # 持续满足 → 不重复注入
            assert mgr.evaluate_condition({"task.status": "failed"}) == []
            assert mgr.evaluate_condition({"task.status": "failed"}) == []
            assert cfg.fire_count == 1
        finally:
            mgr.stop_check_loop()

    def test_false_to_true_edge_fires(self) -> None:
        mgr, cfg = self._cond_mgr()
        try:
            assert mgr.evaluate_condition({"task.status": "running"}) == []
            assert mgr.evaluate_condition({"task.status": "failed"}) == ["t1"]
            # true → false → true：新边沿，再次触发（max_fires=0 不限次）
            assert mgr.evaluate_condition({"task.status": "running"}) == []
            assert mgr.evaluate_condition({"task.status": "failed"}) == ["t1"]
            assert cfg.fire_count == 2
        finally:
            mgr.stop_check_loop()

    def test_rows_any_match_single_edge(self) -> None:
        """多管道聚合行：任一行满足即真，仍按触发器粒度计一次边沿。"""
        mgr, cfg = self._cond_mgr()
        try:
            rows = [
                {"pipeline_id": "p1", "task.status": "running"},
                {"pipeline_id": "p2", "task.status": "failed"},
            ]
            assert mgr.evaluate_condition_rows(rows) == ["t1"]
            rows2 = [
                {"pipeline_id": "p1", "task.status": "failed"},
                {"pipeline_id": "p2", "task.status": "failed"},
            ]
            # 两行都满足仍是同一电平 → 不重复
            assert mgr.evaluate_condition_rows(rows2) == []
            assert cfg.fire_count == 1
        finally:
            mgr.stop_check_loop()


class TestConditionPolling:
    """检查循环条件轮询：state provider 注入 + _poll_conditions 驱动注入。"""

    def test_poll_conditions_fires_and_injects(self) -> None:
        received: list[tuple] = []

        async def fake_injector(pipeline_id: str, message: str, user_id: str) -> str:
            received.append((pipeline_id, message, user_id))
            return "ok"

        def provider() -> list[dict]:
            return [{"pipeline_id": "p9", "task.status": "failed", "task.goal": "修 bug"}]

        with _LoopThread() as lt:
            mgr = TriggerManager()
            mgr.set_main_loop(lt.loop)
            mgr.set_injector(fake_injector)
            mgr.set_state_provider(provider)
            cfg = _make_config(
                trigger_type=TriggerType.CONDITION,
                condition_expression="task.status == 'failed'",
                max_fires=0,
                metadata={"user_id": "u-1"},
            )
            mgr.register(cfg)
            try:
                mgr._poll_conditions()
                assert len(received) == 1
                assert received[0][0] == "pipe-1"
                assert "[触发器通知]" in received[0][1]
                assert received[0][2] == "u-1"
                # 持续满足 → 不重复注入
                mgr._poll_conditions()
                assert len(received) == 1
            finally:
                mgr.stop_check_loop()

    def test_async_provider_via_main_loop(self) -> None:
        """async provider（server.py 生产形态）→ 经 run_coroutine_threadsafe 求值。"""

        async def provider() -> list[dict]:
            return [{"task.status": "completed"}]

        calls: list[tuple] = []

        async def fake_injector(pipeline_id: str, message: str, user_id: str) -> str:
            calls.append((pipeline_id, message))
            return "ok"

        with _LoopThread() as lt:
            mgr = TriggerManager()
            mgr.set_main_loop(lt.loop)
            mgr.set_injector(fake_injector)
            mgr.set_state_provider(provider)
            cfg = _make_config(
                trigger_type=TriggerType.CONDITION,
                condition_expression="task.status == 'completed'",
                max_fires=1,
            )
            mgr.register(cfg)
            try:
                mgr._poll_conditions()
                assert len(calls) == 1
            finally:
                mgr.stop_check_loop()

    def test_no_condition_triggers_skips_provider(self) -> None:
        """无活跃 CONDITION 触发器 → 不调 provider（省一次内核往返）。"""
        provider_calls: list[int] = []

        def provider() -> list[dict]:
            provider_calls.append(1)
            return []

        mgr = TriggerManager()
        mgr.set_state_provider(provider)
        cfg = _make_config(trigger_type=TriggerType.DELAY, delay_seconds=3600)
        mgr.register(cfg)
        try:
            mgr._poll_conditions()
            assert provider_calls == []
        finally:
            mgr.stop_check_loop()

    def test_provider_error_degrades(self) -> None:
        def bad_provider() -> list[dict]:
            raise RuntimeError("kernel down")

        mgr = TriggerManager()
        mgr.set_state_provider(bad_provider)
        cfg = _make_config(trigger_type=TriggerType.CONDITION, condition_expression="x == 1")
        mgr.register(cfg)
        try:
            mgr._poll_conditions()  # 不抛异常
            assert cfg.fire_count == 0
        finally:
            mgr.stop_check_loop()

    def test_provider_not_set_degrades(self) -> None:
        mgr = TriggerManager()
        cfg = _make_config(trigger_type=TriggerType.CONDITION, condition_expression="x == 1")
        mgr.register(cfg)
        try:
            mgr._poll_conditions()  # 不抛异常
            assert cfg.fire_count == 0
            assert mgr.is_state_provider_ready() is False
        finally:
            mgr.stop_check_loop()

    def test_provider_non_list_return_degrades(self) -> None:
        mgr = TriggerManager()
        mgr.set_state_provider(lambda: {"not": "a list"})
        cfg = _make_config(trigger_type=TriggerType.CONDITION, condition_expression="x == 1")
        mgr.register(cfg)
        try:
            mgr._poll_conditions()
            assert cfg.fire_count == 0
        finally:
            mgr.stop_check_loop()


class TestDomainEventBridge:
    """域事件桥：内核 notifications/domain_event → evaluate_event → 注入。"""

    def test_handle_domain_event_evaluates_and_injects(self) -> None:
        received: list[tuple] = []

        async def fake_injector(pipeline_id: str, message: str, user_id: str) -> str:
            received.append((pipeline_id, message, user_id))
            return "ok"

        mgr = TriggerManager()
        mgr.set_injector(fake_injector)
        cfg = _make_config(
            event_name="task_completed",
            max_fires=3,
            metadata={"user_id": "u-7"},
        )
        mgr.register(cfg)
        try:
            fired = _run(mgr.handle_domain_event("task_completed", {"pipeline_id": "p1", "task_id": "t9"}))
            assert fired == ["t1"]
            assert len(received) == 1
            assert received[0][0] == "pipe-1"
            assert "task_completed" not in received[0][1]  # 消息体是 fire_info+用户消息
            assert received[0][2] == "u-7"
        finally:
            mgr.stop_check_loop()

    def test_handle_domain_event_unmatched_no_inject(self) -> None:
        received: list[tuple] = []

        async def fake_injector(pipeline_id: str, message: str, user_id: str) -> str:
            received.append((pipeline_id, message, user_id))
            return "ok"

        mgr = TriggerManager()
        mgr.set_injector(fake_injector)
        mgr.register(_make_config(event_name="task_completed", max_fires=3))
        try:
            assert _run(mgr.handle_domain_event("session.created", {})) == []
            assert received == []
        finally:
            mgr.stop_check_loop()

    def test_handle_domain_event_injector_error_no_crash(self) -> None:
        async def bad_injector(pipeline_id: str, message: str, user_id: str) -> str:
            raise RuntimeError("kernel down")

        mgr = TriggerManager()
        mgr.set_injector(bad_injector)
        mgr.register(_make_config(event_name="run.failed", max_fires=3))
        try:
            fired = _run(mgr.handle_domain_event("run.failed", {}))
            assert fired == ["t1"]  # 评估成功（注入失败仅记录）
        finally:
            mgr.stop_check_loop()

    def test_event_bridge_ready_flag(self) -> None:
        mgr = TriggerManager()
        assert mgr.is_event_bridge_ready() is False
        mgr.set_event_bridge_ready()
        assert mgr.is_event_bridge_ready() is True


class TestSetupToolBridgeWarnings:
    """注册防御：桥未就绪 → 成功结果携带明确 warning（不静默注册）。"""

    def test_event_without_bridge_warns(self) -> None:
        mod = _load_tool()
        fresh = TriggerManager()
        mod.get_trigger_manager = lambda: fresh  # 与 TestSetupTool.fixture 同式（tool 模块自身绑定）
        try:
            inst = mod.TriggerSetupTool()
            r = _run(inst.execute({"trigger_type": "event", "event_type": "task_completed", "message": "m", "pipeline_id": "p"}))
            assert r.success
            assert "warning" in r.output
            assert "域事件" in r.output["warning"] or "桥" in r.output["warning"]
            # 桥就绪 → 无警告
            fresh.set_event_bridge_ready()
            r2 = _run(inst.execute({"trigger_type": "event", "event_type": "task_completed", "message": "m", "pipeline_id": "p2"}))
            assert r2.success
            assert "warning" not in r2.output
        finally:
            fresh.stop_check_loop()

    def test_condition_without_provider_warns(self) -> None:
        mod = _load_tool()
        fresh = TriggerManager()
        mod.get_trigger_manager = lambda: fresh
        try:
            inst = mod.TriggerSetupTool()
            r = _run(inst.execute({"trigger_type": "condition", "condition": "a == 1", "message": "m", "pipeline_id": "p"}))
            assert r.success
            assert "warning" in r.output
            # provider 就绪 → 无警告
            fresh.set_state_provider(lambda: [])
            r2 = _run(inst.execute({"trigger_type": "condition", "condition": "a == 1", "message": "m", "pipeline_id": "p2"}))
            assert r2.success
            assert "warning" not in r2.output
        finally:
            fresh.stop_check_loop()


class TestServerGAP2Wiring:
    """server.py：domain_event 钩子注册 + state provider 注入 + 桥就绪标记。"""

    def test_on_load_sets_state_provider_and_bridge(self) -> None:
        import triggers.manager as manager_mod

        server = _load_server()
        real_mgr = manager_mod.get_trigger_manager()
        real_mgr.stop_check_loop()
        saved = (real_mgr._state_provider, real_mgr._event_bridge_ready)
        try:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(server._on_load({}))
                assert real_mgr._state_provider is not None
                assert real_mgr.is_event_bridge_ready() is True
            finally:
                loop.close()
        finally:
            real_mgr._state_provider, real_mgr._event_bridge_ready = saved
            real_mgr.stop_check_loop()

    def test_state_provider_calls_pipeline_state_capability(self) -> None:
        server = _load_server()

        class FakeState:
            def __init__(self) -> None:
                self.calls: list[tuple] = []

            async def call(self, method: str, params: dict) -> list[dict]:
                self.calls.append((method, params))
                return [{"pipeline_id": "p1", "task.status": "running"}]

        cap = FakeState()
        server.plugin.get_capability = lambda name: cap  # type: ignore[method-assign]
        provider = server._make_state_provider()
        rows = _run(provider())
        assert rows == [{"pipeline_id": "p1", "task.status": "running"}]
        assert cap.calls and cap.calls[0][0] == "list"

    def test_domain_event_handler_registered_and_forwards(self) -> None:
        """plugin 注册了 on_domain_event 处理器，且转发到 manager.handle_domain_event。"""
        server = _load_server()
        handler = server.plugin._lifecycle_handlers.get("domain_event")
        assert handler is not None, "server.py 应注册 domain_event 生命周期处理器"

        import triggers.manager as manager_mod

        mgr = manager_mod.get_trigger_manager()
        mgr.stop_check_loop()
        forwarded: list[tuple] = []

        async def fake_handle(event_name: str, event_data: dict) -> list[str]:
            forwarded.append((event_name, dict(event_data)))
            return []

        mgr.handle_domain_event = fake_handle  # type: ignore[method-assign]
        try:
            _run(handler({"event": "task_completed", "pipeline_id": "p1", "task_id": "t9"}))
            assert forwarded == [("task_completed", {"event": "task_completed", "pipeline_id": "p1", "task_id": "t9"})]
        finally:
            del mgr.handle_domain_event
            mgr.stop_check_loop()

    def test_plugin_json_declares_domain_event_hook(self) -> None:
        """manifest 补 domain_event lifecycle hook（内核才会点对点推送域事件）。"""
        import json

        manifest = json.loads((_PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
        hooks = manifest.get("capabilities", {}).get("lifecycle_hooks", [])
        assert "domain_event" in hooks
