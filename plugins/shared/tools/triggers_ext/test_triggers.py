# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: python-coverage
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
import json
import logging
import os
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
    # 逐出与本插件同名、可能被其他插件测试运行期缓存的裸模块，强制本目录
    # server.py 的 `from tool/http_api import ...` 按本目录 sys.path 解析
    # （tasks 系测试运行期会把 tasks 版 http_api/server 残留 sys.modules）。
    for _bare in ("tool", "http_api", "server"):
        sys.modules.pop(_bare, None)
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

    def test_condition_invalid_syntax_rejected_at_register(self) -> None:
        """P4（兜底反模式审查）：语法错误注册期编译校验拒绝注册。

        旧缺陷：语法错误的 condition 被接受 → 每轮静默求值 False →
        触发器永不触发且零报错。
        """
        mgr = TriggerManager()
        cfg = _make_config(trigger_type=TriggerType.CONDITION, condition_expression="!!!invalid")
        with pytest.raises(ValueError, match="语法错误"):
            mgr.register(cfg)
        assert "t1" not in mgr._triggers, "被拒绝的触发器不得进入注册表"

        # 未闭合字符串同属语法错误（tokenize/parse 阶段可捕获）
        cfg2 = _make_config(trigger_id="t2", trigger_type=TriggerType.CONDITION,
                            condition_expression="name == 'foo")
        with pytest.raises(ValueError):
            mgr.register(cfg2)

    def test_condition_invalid_syntax_eval_degrades(self) -> None:
        """求值期语法异常安全兜底为 False（纵深防御；注册期已拦截）。"""
        from triggers.condition_parser import parse_condition

        assert parse_condition("!!!invalid", {"x": 1}) is False

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
        """注入器未设置 → 记录错误后放弃本轮注入，不抛异常。"""
        with _LoopThread() as lt:
            mgr = TriggerManager()
            mgr.set_main_loop(lt.loop)
            cfg = _make_config()
            mgr.register(cfg)
            mgr._inject_trigger_message(cfg)  # 不抛异常
            mgr.stop_check_loop()

    def test_check_loop_sync_drives_injector(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """后台检查循环：到期触发器经注入器投递消息。"""
        # _check_loop_sync 的 sleep 引用的是模块级常量（实例属性不生效）——
        # patch 模块级值才能加速轮询；否则每轮 sleep 5s 与 wait(5) 同长，
        # Linux CI 慢机上首轮检查未跑完即超时（临界竞态）。
        import triggers.manager as _tm_mod

        monkeypatch.setattr(_tm_mod, "_TRIGGER_CHECK_INTERVAL", 0.01)
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

    def test_task_completed_auto_notifies_parent_pipeline(self) -> None:
        """GAP-1：task_completed 带 parent_pipeline_id → 自动注入父管道。

        等效"提交任务后自动注册触发器"：task_submit 承诺"子任务完成后自动
        通知上级"，注册逻辑收敛在统一触发服务（triggers_ext），任务系统
        零触发代码——事件本身携带父管道锚点即触发通知，无需显式注册。
        """
        received: list[tuple] = []

        async def fake_injector(pipeline_id: str, message: str, user_id: str) -> str:
            received.append((pipeline_id, message, user_id))
            return "ok"

        mgr = TriggerManager()
        mgr.set_injector(fake_injector)
        try:
            fired = _run(
                mgr.handle_domain_event(
                    "task_completed",
                    {
                        "pipeline_id": "child_pipe",
                        "task_id": "t_child",
                        "parent_pipeline_id": "parent_pipe",
                        # 内核从子任务 state 的 task.submitted_by 带出（task_submit
                        # 创建时写入）——注入器必须透传，chat.send_message 硬校验
                        # user_id 非空，传空串会被内核 -32603 拒绝（2026-08-17 断点）
                        "user_id": "u_admin",
                    },
                )
            )
            # 无显式注册的触发器 → evaluate 不命中；自动父通知是独立注入
            assert fired == []
            assert len(received) == 1, "应自动向父管道注入一条通知"
            assert received[0][0] == "parent_pipe", "注入目标应为父管道"
            assert "t_child" in received[0][1], "通知应包含子任务标识"
            assert received[0][2] == "u_admin", "注入器应透传事件携带的 user_id"
        finally:
            mgr.stop_check_loop()

    def test_task_completed_rich_notification_content(self) -> None:
        """完成通知带 0.1 同款富内容：标题/评估结论/上下文使用率。"""
        received: list[tuple] = []

        async def fake_injector(pipeline_id: str, message: str, user_id: str) -> str:
            received.append((pipeline_id, message, user_id))
            return "ok"

        mgr = TriggerManager()
        mgr.set_injector(fake_injector)
        try:
            _run(
                mgr.handle_domain_event(
                    "task_completed",
                    {
                        "pipeline_id": "child_pipe",
                        "task_id": "t_child",
                        "parent_pipeline_id": "parent_pipe",
                        "user_id": "u_admin",
                        "title": "写周报",
                        "eval_summary": "全部 2 项指标通过",
                        "context_usage": {
                            "pct": 30.0,
                            "input_tokens": 38400,
                            "context_window": 128000,
                        },
                    },
                )
            )
            msg = received[0][1]
            assert "写周报" in msg, "通知应包含任务标题"
            assert "已完成 ✅" in msg
            assert "📋 评估结论: 全部 2 项指标通过" in msg, "通知应包含评估结论"
            assert "📊 上下文使用率: 30.0% (38,400/128,000 tokens)" in msg, "通知应包含上下文使用率"
            assert "不建议继续向此 Agent 继承提交" in msg, "30% 应命中 25-50% 档提示"
        finally:
            mgr.stop_check_loop()

    def test_task_failed_rich_notification_content(self) -> None:
        """失败通知带 0.1 同款富内容：标题/失败原因/重试计数。"""
        received: list[tuple] = []

        async def fake_injector(pipeline_id: str, message: str, user_id: str) -> str:
            received.append((pipeline_id, message, user_id))
            return "ok"

        mgr = TriggerManager()
        mgr.set_injector(fake_injector)
        try:
            _run(
                mgr.handle_domain_event(
                    "task_failed",
                    {
                        "pipeline_id": "child_pipe",
                        "task_id": "t_fail",
                        "parent_pipeline_id": "parent_pipe",
                        "user_id": "u_admin",
                        "title": "写周报",
                        "error": "评估未通过: 1/2 项指标通过",
                        "retry_count": 3,
                    },
                )
            )
            msg = received[0][1]
            assert "写周报" in msg, "通知应包含任务标题"
            assert "失败 ❌" in msg
            assert "(已重试 3 次)" in msg, "通知应包含重试计数"
            assert "评估未通过: 1/2 项指标通过" in msg, "通知应包含失败原因"
        finally:
            mgr.stop_check_loop()

    def test_task_failed_auto_notifies_parent_pipeline(self) -> None:
        """GAP-1：task_failed 同样自动通知父管道（失败也需上级知晓）。"""
        received: list[tuple] = []

        async def fake_injector(pipeline_id: str, message: str, user_id: str) -> str:
            received.append((pipeline_id, message, user_id))
            return "ok"

        mgr = TriggerManager()
        mgr.set_injector(fake_injector)
        try:
            _run(
                mgr.handle_domain_event(
                    "task_failed",
                    {
                        "pipeline_id": "child_pipe",
                        "task_id": "t_fail",
                        "parent_pipeline_id": "parent_pipe",
                        "error": "超时",
                    },
                )
            )
            assert len(received) == 1
            assert received[0][0] == "parent_pipe"
            assert "t_fail" in received[0][1]
            assert "超时" in received[0][1], "失败通知应携带原因"
        finally:
            mgr.stop_check_loop()

    def test_task_completed_without_parent_does_not_inject(self) -> None:
        """无 parent_pipeline_id（根任务/无 lineage）→ 不自动注入（无处可投）。"""
        received: list[tuple] = []

        async def fake_injector(pipeline_id: str, message: str, user_id: str) -> str:
            received.append((pipeline_id, message, user_id))
            return "ok"

        mgr = TriggerManager()
        mgr.set_injector(fake_injector)
        try:
            _run(
                mgr.handle_domain_event(
                    "task_completed",
                    {"pipeline_id": "child_pipe", "task_id": "t_root", "parent_pipeline_id": ""},
                )
            )
            assert received == [], "根任务完成不应自动注入"
        finally:
            mgr.stop_check_loop()

    def test_task_completed_explicit_trigger_and_auto_notify_coexist(self) -> None:
        """显式触发器命中 + parent_pipeline_id → 两条注入并存（2026-08-20 裁定）。

        推翻旧"互斥去重"定案：LLM 自设的 task_completed 测试触发器命中后，
        父管道只收到测试消息、永远等不到系统完成通知——显式触发器消息是
        用户自定义内容，无权顶替 task_submit 承诺的系统恢复锚点。
        """
        received: list[tuple] = []

        async def fake_injector(pipeline_id: str, message: str, user_id: str) -> str:
            received.append((pipeline_id, message, user_id))
            return "ok"

        mgr = TriggerManager()
        mgr.set_injector(fake_injector)
        # 显式触发器：注册在父管道上监听 task_completed（复刻实测场景）
        mgr.register(_make_config(event_name="task_completed", max_fires=3, metadata={"user_id": "u-7"}))
        try:
            fired = _run(
                mgr.handle_domain_event(
                    "task_completed",
                    {
                        "pipeline_id": "child_pipe",
                        "task_id": "t_child",
                        "parent_pipeline_id": "pipe-1",
                        "user_id": "u_admin",
                    },
                )
            )
            assert fired == ["t1"], "显式触发器照常命中"
            assert len(received) == 2, "触发器消息 + 系统父通知，两条并存"
            targets = [r[0] for r in received]
            assert targets == ["pipe-1", "pipe-1"], "本用例触发器与父管道同目标"
            # 系统通知先注入（含子任务标识 + 透传事件 user_id），触发器消息随后
            assert "t_child" in received[0][1], "系统通知应包含子任务标识"
            assert received[0][2] == "u_admin", "系统通知透传事件 user_id"
            assert "触发器通知" in received[1][1], "第二条为显式触发器消息"
        finally:
            mgr.stop_check_loop()

    def test_non_task_event_does_not_auto_notify(self) -> None:
        """非任务事件（run.completed 等）不触发自动父通知。"""
        received: list[tuple] = []

        async def fake_injector(pipeline_id: str, message: str, user_id: str) -> str:
            received.append((pipeline_id, message, user_id))
            return "ok"

        mgr = TriggerManager()
        mgr.set_injector(fake_injector)
        try:
            _run(
                mgr.handle_domain_event(
                    "run.completed",
                    {"pipeline_id": "p", "parent_pipeline_id": "parent"},
                )
            )
            assert received == [], "非任务事件不自动注入"
        finally:
            mgr.stop_check_loop()


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


# ═══════════════════════════════════════════════════════════
# 触发器动作：action=command（参数列表执行 + 审计留痕，U11 加固）
# ═══════════════════════════════════════════════════════════

_PLUGIN_SOURCE_FILES: list[Path] = [
    p for p in _PLUGIN_DIR.rglob("*.py")
    if ".venv" not in p.parts and "__pycache__" not in p.parts and not p.name.startswith("test_")
]


class TestCommandAction:
    def test_mechanical_no_shell_in_plugin_source(self) -> None:
        """机械闸：本插件生产源码 shell 执行面必须归零（U11 收敛为参数列表）。"""
        offenders = [
            str(p.relative_to(_PLUGIN_DIR))
            for p in _PLUGIN_SOURCE_FILES
            if "shell=True" in p.read_text(encoding="utf-8")
        ]
        assert offenders == [], f"shell 执行面残留: {offenders}"

    def test_default_action_routes_to_inject(self) -> None:
        """action 为 notify/缺省（""）→ 不分发 command，返回 False（调用方走注入）。"""
        mgr = TriggerManager()
        for action in ("", "notify"):
            assert mgr._dispatch_trigger_action(_make_config(action=action)) is False

    def _wait_for(self, cond: Any, timeout: float = 15.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cond():
                return True
            time.sleep(0.1)
        return False

    def test_command_action_executes_argv_list_with_env(self, tmp_path: Path, monkeypatch: Any) -> None:
        """command 动作按参数列表执行（不经 shell）；触发上下文经 AGENTOS_TRIGGER_* env 传递。

        元字符（管道/重定向/分号）必须作为字面参数直达子进程，不得被 shell 二次解释。
        """
        monkeypatch.chdir(tmp_path)  # 审计日志落 tmp_path/logs/triggers，不污染仓库
        out = tmp_path / "out.txt"
        code = (
            "import os;"
            f"open(r'{out}', 'w').write("
            "os.environ['AGENTOS_TRIGGER_ID'] + '|'"
            "+ os.environ.get('AGENTOS_EVENT_NAME', '') + '|'"
            "+ os.environ.get('AGENTOS_EVENT_TASK_ID', ''))"
        )
        mgr = TriggerManager()
        trigger = _make_config(
            action="command",
            action_params={"cmd": [sys.executable, "-c", code], "timeout_ms": 15000},
        )
        assert mgr._dispatch_trigger_action(trigger, "task_completed", {"task_id": "t9"}) is True
        assert self._wait_for(lambda: out.exists()), "command 动作未产出文件"
        content = out.read_text(encoding="utf-8").strip()
        assert "t1|task_completed|t9" in content, content

    def test_command_action_timeout_kills(self, monkeypatch: Any) -> None:
        """超时后命令被终止（timeout_ms 远小于命令耗时），且留痕 timed_out=true。"""
        mgr = TriggerManager()
        trigger = _make_config(
            action="command",
            action_params={"cmd": [sys.executable, "-c", "import time; time.sleep(30)"], "timeout_ms": 300},
        )
        start = time.time()
        assert mgr._dispatch_trigger_action(trigger) is True
        assert self._wait_for(
            lambda: bool(trigger.metadata.get("command_executions"))
        ), "超时执行未留痕"
        record = trigger.metadata["command_executions"][0]
        assert record["timed_out"] is True, record
        # daemon 线程内 wait(timeout) → 超时杀进程树；总耗时远小于命令自身时长
        elapsed = time.time() - start
        assert elapsed < 10, f"命令未被超时终止: {elapsed:.1f}s"

    def test_command_action_audit_trail_metadata_and_log_file(self, tmp_path: Path, monkeypatch: Any) -> None:
        """执行留痕可查：stdout/退出码/耗时/命令落 metadata（REST 面可查）+ 日志文件。"""
        monkeypatch.chdir(tmp_path)
        mgr = TriggerManager()
        trigger = _make_config(
            action="command",
            action_params={"cmd": [sys.executable, "-c", "print('audit-ok')"], "timeout_ms": 15000},
        )
        mgr.register(trigger)
        assert mgr._dispatch_trigger_action(trigger) is True
        assert self._wait_for(
            lambda: bool(trigger.metadata.get("command_executions"))
        ), "command 动作未留执行痕迹"

        record = trigger.metadata["command_executions"][0]
        assert record["cmd"] == [sys.executable, "-c", "print('audit-ok')"], record
        assert record["exit_code"] == 0, record
        assert record["timed_out"] is False, record
        assert isinstance(record["duration_ms"], int) and record["duration_ms"] >= 0, record
        assert record["fired_at"], record
        # 日志文件：bash 插件 logs/ 同款落盘范式，stdout 全文可查
        log_file = Path(record["log_file"])
        assert log_file.exists(), f"审计日志文件缺失: {log_file}"
        log_text = log_file.read_text(encoding="utf-8")
        assert "audit-ok" in log_text
        # Command 行以 JSON 记录 argv（反斜杠/引号无歧义），解析后须与实际执行的命令一致
        command_line = next(line for line in log_text.splitlines() if line.startswith("# Command:"))
        assert json.loads(command_line[len("# Command:"):]) == [sys.executable, "-c", "print('audit-ok')"]

    def test_command_action_audit_captures_nonzero_exit_and_stderr(self, tmp_path: Path, monkeypatch: Any) -> None:
        """失败执行同样留痕：非零退出码与 stderr 可查（不再双 DEVNULL）。"""
        monkeypatch.chdir(tmp_path)
        mgr = TriggerManager()
        trigger = _make_config(
            action="command",
            action_params={
                "cmd": [sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"],
                "timeout_ms": 15000,
            },
        )
        assert mgr._dispatch_trigger_action(trigger) is True
        assert self._wait_for(lambda: bool(trigger.metadata.get("command_executions")))
        record = trigger.metadata["command_executions"][0]
        assert record["exit_code"] == 3, record
        assert "boom" in record["stderr"], record
        assert "boom" in Path(record["log_file"]).read_text(encoding="utf-8")

    def test_command_action_audit_history_capped(self, tmp_path: Path, monkeypatch: Any) -> None:
        """metadata 执行历史有上限（防 metadata 无界膨胀，REST 序列化面可控）。"""
        monkeypatch.chdir(tmp_path)
        mgr = TriggerManager()
        trigger = _make_config(
            action="command",
            action_params={"cmd": [sys.executable, "-c", "pass"], "timeout_ms": 15000},
        )
        for _ in range(8):
            assert mgr._dispatch_trigger_action(trigger) is True
        assert self._wait_for(
            lambda: len(trigger.metadata.get("command_executions", [])) >= 5
        )
        time.sleep(0.5)  # 等剩余 daemon 线程落账
        assert len(trigger.metadata["command_executions"]) <= 5, trigger.metadata["command_executions"]

    def test_legacy_shell_string_command_explicitly_refused(self, tmp_path: Path, monkeypatch: Any) -> None:
        """存量 shell 字符串格式显式报停：不执行、不留半执行状态、拒绝原因可查。

        无法迁移的旧配置必须显式失败（不静默失效，也不经 shell 兜底执行）。
        """
        monkeypatch.chdir(tmp_path)
        popen_calls: list[Any] = []

        def _no_popen(*args: Any, **kwargs: Any) -> None:
            popen_calls.append((args, kwargs))
            raise AssertionError(f"旧格式不得启动进程: {args}")

        monkeypatch.setattr("subprocess.Popen", _no_popen)
        sentinel = tmp_path / "should_not_exist.txt"
        mgr = TriggerManager()
        trigger = _make_config(
            action="command",
            action_params={"command": f"echo fired > \"{sentinel}\""},
        )
        assert mgr._dispatch_trigger_action(trigger) is True  # 动作被 command 通道消费
        assert self._wait_for(lambda: bool(trigger.metadata.get("command_executions")))
        record = trigger.metadata["command_executions"][0]
        assert record["refused"] is True, record
        assert "shell" in record["reason"], record
        assert popen_calls == [], "旧 shell 字符串命令被静默执行"
        assert not sentinel.exists(), "旧 shell 字符串命令产生了副作用"

    def test_command_action_output_decoded_when_not_utf8(self, tmp_path: Path, monkeypatch: Any) -> None:
        """子进程输出非 UTF-8 字节 → 显式解码回退（不用 text=True，跨端兼容）。"""
        monkeypatch.chdir(tmp_path)
        mgr = TriggerManager()
        trigger = _make_config(
            action="command",
            action_params={
                "cmd": [sys.executable, "-c", "import sys; sys.stdout.buffer.write(bytes([0xff, 0x41, 0x42]))"],
                "timeout_ms": 15000,
            },
        )
        assert mgr._dispatch_trigger_action(trigger) is True
        assert self._wait_for(lambda: bool(trigger.metadata.get("command_executions")))
        record = trigger.metadata["command_executions"][0]
        assert record["exit_code"] == 0, record
        assert "AB" in record["stdout"], record  # 0x41 0x42 经回退解码保留

    def test_command_action_output_decode_final_fallback(self, tmp_path: Path, monkeypatch: Any) -> None:
        """首选编码不可名（LookupError）→ utf-8 replace 兜底，不炸 daemon 线程。"""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            "triggers.manager.locale.getpreferredencoding", lambda do_setlocale: "not-an-encoding"
        )
        mgr = TriggerManager()
        trigger = _make_config(
            action="command",
            action_params={
                "cmd": [sys.executable, "-c", "import sys; sys.stdout.buffer.write(bytes([0xff, 0x41]))"],
                "timeout_ms": 15000,
            },
        )
        assert mgr._dispatch_trigger_action(trigger) is True
        assert self._wait_for(lambda: bool(trigger.metadata.get("command_executions")))
        record = trigger.metadata["command_executions"][0]
        assert record["exit_code"] == 0, record
        assert "A" in record["stdout"], record

    def test_command_action_audit_metadata_summary_capped_but_log_full(self, tmp_path: Path, monkeypatch: Any) -> None:
        """超长输出：metadata 摘要截尾（有界），日志文件保留全文。"""
        monkeypatch.chdir(tmp_path)
        mgr = TriggerManager()
        trigger = _make_config(
            action="command",
            action_params={
                "cmd": [sys.executable, "-c", "print('x' * 5000)"],
                "timeout_ms": 15000,
            },
        )
        assert mgr._dispatch_trigger_action(trigger) is True
        assert self._wait_for(lambda: bool(trigger.metadata.get("command_executions")))
        record = trigger.metadata["command_executions"][0]
        assert "[已截断" in record["stdout"], record
        assert len(record["stdout"]) < 5000
        log_text = Path(record["log_file"]).read_text(encoding="utf-8")
        assert "x" * 100 in log_text  # 全文落日志
        assert log_text.count("x") >= 5000

    def test_command_action_audit_log_write_failure_does_not_block_record(self, tmp_path: Path, monkeypatch: Any) -> None:
        """审计日志落盘失败（目录被文件占用）→ 摘要留痕不受影响，log_file 为空串。"""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "logs").mkdir()
        (tmp_path / "logs" / "triggers").write_text("blocker", encoding="utf-8")  # 占位为文件 → mkdir 失败
        mgr = TriggerManager()
        trigger = _make_config(
            action="command",
            action_params={"cmd": [sys.executable, "-c", "print('ok')"], "timeout_ms": 15000},
        )
        assert mgr._dispatch_trigger_action(trigger) is True
        assert self._wait_for(lambda: bool(trigger.metadata.get("command_executions")))
        record = trigger.metadata["command_executions"][0]
        assert record["exit_code"] == 0, record
        assert record["log_file"] == "", record

    def test_command_action_missing_cmd_params_refused(self, tmp_path: Path, monkeypatch: Any) -> None:
        """action_params 为空（缺 cmd）→ 显式拒绝留痕，不执行。"""
        monkeypatch.chdir(tmp_path)
        mgr = TriggerManager()
        trigger = _make_config(action="command", action_params={})
        assert mgr._dispatch_trigger_action(trigger) is True
        assert self._wait_for(lambda: bool(trigger.metadata.get("command_executions")))
        record = trigger.metadata["command_executions"][0]
        assert record["refused"] is True and "cmd" in record["reason"], record

    def test_command_action_non_list_cmd_refused(self, tmp_path: Path, monkeypatch: Any) -> None:
        """cmd 非参数列表（字符串/含非字符串项）→ 显式拒绝留痕。"""
        monkeypatch.chdir(tmp_path)
        mgr = TriggerManager()
        for bad in ("echo hi", ["echo", 42], []):
            trigger = _make_config(action="command", action_params={"cmd": bad})
            assert mgr._dispatch_trigger_action(trigger) is True
            assert self._wait_for(
                lambda t=trigger: bool(t.metadata.get("command_executions"))
            )
            record = trigger.metadata["command_executions"][0]
            assert record["refused"] is True, (bad, record)

    def test_command_action_start_failure_refused(self, tmp_path: Path, monkeypatch: Any) -> None:
        """可执行文件不存在（OSError）→ 启动失败留痕，不静默丢。"""
        monkeypatch.chdir(tmp_path)
        mgr = TriggerManager()
        trigger = _make_config(
            action="command",
            action_params={"cmd": ["definitely-not-a-real-binary-xyz"], "timeout_ms": 15000},
        )
        assert mgr._dispatch_trigger_action(trigger) is True
        assert self._wait_for(lambda: bool(trigger.metadata.get("command_executions")))
        record = trigger.metadata["command_executions"][0]
        assert record["refused"] is True and "启动失败" in record["reason"], record

    def test_command_action_wait_failure_refused(self, tmp_path: Path, monkeypatch: Any) -> None:
        """等待异常（非超时）→ 留痕不炸 daemon 线程（subprocess 为外部依赖，注入故障）。"""
        monkeypatch.chdir(tmp_path)

        class _BrokenProc:
            pid = 0

            def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
                raise ValueError("pipe exploded")

        monkeypatch.setattr("subprocess.Popen", lambda *a, **k: _BrokenProc())
        mgr = TriggerManager()
        trigger = _make_config(
            action="command",
            action_params={"cmd": [sys.executable, "-c", "pass"], "timeout_ms": 15000},
        )
        assert mgr._dispatch_trigger_action(trigger) is True
        assert self._wait_for(lambda: bool(trigger.metadata.get("command_executions")))
        record = trigger.metadata["command_executions"][0]
        assert record["refused"] is True and "等待异常" in record["reason"], record

    def test_command_action_reap_failure_after_timeout(self, tmp_path: Path, monkeypatch: Any) -> None:
        """超时杀树后回收输出再失败（防御分支）→ 仍按超时留痕，daemon 线程不炸。

        subprocess 为外部依赖，注入故障（mock 仅限外部依赖）。
        """
        import subprocess

        monkeypatch.chdir(tmp_path)

        class _TimeoutThenBrokenProc:
            pid = 0
            returncode = None

            def __init__(self) -> None:
                self._calls = 0

            def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
                self._calls += 1
                if self._calls == 1:
                    raise subprocess.TimeoutExpired(cmd="broken", timeout=0.3)
                raise ValueError("reap failed")

        proc = _TimeoutThenBrokenProc()
        monkeypatch.setattr("subprocess.Popen", lambda *a, **k: proc)
        mgr = TriggerManager()
        trigger = _make_config(
            action="command",
            action_params={"cmd": [sys.executable, "-c", "pass"], "timeout_ms": 300},
        )
        assert mgr._dispatch_trigger_action(trigger) is True
        assert self._wait_for(lambda: bool(trigger.metadata.get("command_executions")))
        record = trigger.metadata["command_executions"][0]
        assert record["timed_out"] is True, record
        assert record["exit_code"] is None, record
        assert record["stdout"] == "" and record["stderr"] == "", record

    def test_domain_event_command_skips_injector(self, tmp_path: Path, monkeypatch: Any) -> None:
        """handle_domain_event 命中 command 触发器 → 执行命令且不注入消息。"""
        monkeypatch.chdir(tmp_path)
        injected: list[tuple[str, str]] = []
        out = tmp_path / "ev.txt"
        mgr = TriggerManager()
        mgr.set_injector(lambda pipeline_id, message, user_id="": injected.append((pipeline_id, message)))
        trigger = _make_config(
            trigger_id="cmd-ev",
            name="命令触发器",
            event_name="task_completed",
            action="command",
            action_params={"cmd": [sys.executable, "-c", f"open(r'{out}', 'w').write('fired')"], "timeout_ms": 15000},
        )
        mgr.register(trigger)
        fired = _run(mgr.handle_domain_event("task_completed", {"task_id": "t9"}))
        assert fired == ["cmd-ev"]
        assert injected == [], f"command 触发器不应注入消息: {injected}"
        assert self._wait_for(lambda: out.exists())
        mgr.stop_check_loop()


# ═══════════════════════════════════════════════════════════
# 审批闸：trigger_setup 写入 command 类动作须经 human-interaction 审批
# （notify 类安全动作免审批；拒绝的 command 不落库不执行）
# ═══════════════════════════════════════════════════════════


class _FakeHiCapability:
    """human-interaction capability 假件（外部依赖桩：记录请求、回放预设响应）。"""

    def __init__(self, wait_result: dict[str, Any]) -> None:
        self.wait_result = wait_result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        self.calls.append((method, dict(params)))
        if method == "create_choice":
            return {"request_id": "req-1"}
        if method == "wait_for_choice":
            return dict(self.wait_result)
        raise AssertionError(f"unexpected capability method: {method}")


class TestTriggerCommandApprovalGate:
    @pytest.fixture
    def gate(self, monkeypatch: Any) -> Any:
        """全新 manager 的 tool 模块 + 可换装的 capability provider。"""
        mod = _load_tool()
        fresh = TriggerManager()
        mod.get_trigger_manager = lambda: fresh
        holder: dict[str, Any] = {"provider": None}
        mod.set_capability_provider(lambda name: holder["provider"])
        yield {"mod": mod, "tool": mod.TriggerSetupTool(), "holder": holder, "manager": fresh}
        mod.set_capability_provider(None)
        fresh.stop_check_loop()

    def _argv(self, out: Path) -> list[str]:
        return [sys.executable, "-c", f"open(r'{out}', 'w').write('ran')"]

    def test_denied_command_not_registered_nor_executed(self, gate: Any, tmp_path: Path) -> None:
        """危险 command 未获批准：不落库（不注册）、不执行，审批请求确实发出。"""
        out = tmp_path / "denied.txt"
        hi = _FakeHiCapability({"response_type": "answered", "selected_option": "denied"})
        gate["holder"]["provider"] = hi
        r = _run(gate["tool"].execute({
            "trigger_type": "delay", "delay_seconds": 60, "message": "m",
            "pipeline_id": "p1", "trigger_action": "command", "command": self._argv(out),
        }))
        assert not r.success, "未批准的 command 触发器被注册"
        assert "审批" in (r.error or ""), r.error
        assert len(gate["manager"]._triggers) == 0, "未批准的 command 触发器落库"
        assert any(m == "create_choice" for m, _ in hi.calls), "审批请求未发起"
        time.sleep(0.5)
        assert not out.exists(), "未批准的 command 被执行"

    def test_command_gate_fails_closed_without_capability(self, gate: Any, tmp_path: Path) -> None:
        """审批通道不可用 → fail-closed 拒绝注册（禁止静默放行）。"""
        gate["holder"]["provider"] = None
        r = _run(gate["tool"].execute({
            "trigger_type": "delay", "delay_seconds": 60, "message": "m",
            "pipeline_id": "p1", "trigger_action": "command", "command": self._argv(tmp_path / "x.txt"),
        }))
        assert not r.success and "审批" in (r.error or ""), r.error
        assert len(gate["manager"]._triggers) == 0

    @pytest.mark.parametrize("selected", ["approved_once", "批准注册执行"], ids=["id", "label"])
    def test_approved_command_registers_template(self, gate: Any, selected: str) -> None:
        """批准后按动作模板落库：action=command + cmd 参数列表。"""
        hi = _FakeHiCapability({"response_type": "answered", "selected_option": selected})
        gate["holder"]["provider"] = hi
        r = _run(gate["tool"].execute({
            "trigger_type": "delay", "delay_seconds": 60, "message": "m",
            "pipeline_id": "p1", "trigger_action": "command",
            "command": [sys.executable, "-c", "print('ok')"],
        }))
        assert r.success, r.error
        assert len(gate["manager"]._triggers) == 1
        cfg = next(iter(gate["manager"]._triggers.values()))
        assert cfg.action == "command"
        assert cfg.action_params["cmd"] == [sys.executable, "-c", "print('ok')"]
        # 审批描述必须带上命令内容（用户看到的是什么就是什么）
        create_params = next(p for m, p in hi.calls if m == "create_choice")
        assert "print('ok')" in create_params["description"], create_params

    def test_approval_wait_timeout_treated_as_denied(self, gate: Any) -> None:
        """审批等待超时/异常 → 拒绝注册（fail-closed，不落库）。"""
        hi = _FakeHiCapability({"error": "交互超时: req-1 (超时时间: 86400秒)"})
        gate["holder"]["provider"] = hi
        r = _run(gate["tool"].execute({
            "trigger_type": "delay", "delay_seconds": 60, "message": "m",
            "pipeline_id": "p1", "trigger_action": "command",
            "command": [sys.executable, "-c", "print('ok')"],
        }))
        assert not r.success, "超时未批复的 command 被注册"
        assert len(gate["manager"]._triggers) == 0

    def test_notify_action_skips_approval(self, gate: Any) -> None:
        """notify 类安全动作免审批：不触碰 human-interaction，正常注册。"""

        class _MustNotCall:
            async def call(self, *args: Any, **kwargs: Any) -> Any:
                raise AssertionError("notify 动作不应发起审批")

        gate["holder"]["provider"] = _MustNotCall()
        r = _run(gate["tool"].execute({
            "trigger_type": "delay", "delay_seconds": 60, "message": "m", "pipeline_id": "p1",
        }))
        assert r.success, r.error
        cfg = next(iter(gate["manager"]._triggers.values()))
        assert cfg.action == "notify"
        assert cfg.action_params == {}

    def test_command_shape_validation_before_approval(self, gate: Any) -> None:
        """command 必须是非空字符串参数列表；形状非法直接拒绝，不发审批。"""

        class _MustNotCall:
            async def call(self, *args: Any, **kwargs: Any) -> Any:
                raise AssertionError("形状非法不应发起审批")

        gate["holder"]["provider"] = _MustNotCall()
        base = {"trigger_type": "delay", "delay_seconds": 60, "message": "m",
                "pipeline_id": "p1", "trigger_action": "command"}
        for bad in ("echo hi", [], ["echo", ""], [42], "not-a-list"):
            r = _run(gate["tool"].execute({**base, "command": bad}))
            assert not r.success and r.error_code == "INVALID_COMMAND", (bad, r.error)

    def test_approved_command_records_cwd(self, gate: Any) -> None:
        """cwd 可选参数随模板落库。"""
        hi = _FakeHiCapability({"response_type": "answered", "selected_option": "approved_once"})
        gate["holder"]["provider"] = hi
        r = _run(gate["tool"].execute({
            "trigger_type": "delay", "delay_seconds": 60, "message": "m", "pipeline_id": "p1",
            "trigger_action": "command", "command": [sys.executable, "-c", "print('ok')"],
            "cwd": "somewhere",
        }))
        assert r.success, r.error
        cfg = next(iter(gate["manager"]._triggers.values()))
        assert cfg.action_params["cwd"] == "somewhere", cfg.action_params

    def test_command_gate_fails_closed_when_provider_not_wired(self, gate: Any) -> None:
        """provider 未注入（模块级 None）→ fail-closed（区别于 provider 返回 None）。"""
        gate["mod"].set_capability_provider(None)
        r = _run(gate["tool"].execute({
            "trigger_type": "delay", "delay_seconds": 60, "message": "m",
            "pipeline_id": "p1", "trigger_action": "command",
            "command": [sys.executable, "-c", "print('ok')"],
        }))
        assert not r.success and "审批通道未接入" in (r.error or ""), r.error
        assert len(gate["manager"]._triggers) == 0

    def test_command_gate_fails_closed_when_capability_missing(self, gate: Any) -> None:
        """provider 查不到 human-interaction（KeyError）→ fail-closed。"""

        def _raising_provider(name: str) -> Any:
            raise KeyError(name)

        gate["mod"].set_capability_provider(_raising_provider)
        r = _run(gate["tool"].execute({
            "trigger_type": "delay", "delay_seconds": 60, "message": "m",
            "pipeline_id": "p1", "trigger_action": "command",
            "command": [sys.executable, "-c", "print('ok')"],
        }))
        assert not r.success and "审批能力不可用" in (r.error or ""), r.error
        assert len(gate["manager"]._triggers) == 0

    @pytest.mark.parametrize("fail_phase", ["create", "create_error", "wait"])
    def test_command_gate_fails_closed_on_capability_errors(self, gate: Any, fail_phase: str) -> None:
        """审批请求创建/等待任一环抛错或返回 error dict → 拒绝注册（fail-closed）。"""

        class _BrokenHi:
            async def call(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
                if method == "create_choice":
                    if fail_phase == "create":
                        raise RuntimeError("capability down")
                    return {"error": "service not initialized"}
                if fail_phase == "wait":
                    raise RuntimeError("bridge closed")
                return {"request_id": "req-1"}

        gate["holder"]["provider"] = _BrokenHi()
        r = _run(gate["tool"].execute({
            "trigger_type": "delay", "delay_seconds": 60, "message": "m",
            "pipeline_id": "p1", "trigger_action": "command",
            "command": [sys.executable, "-c", "print('ok')"],
        }))
        assert not r.success and "审批" in (r.error or ""), r.error
        assert len(gate["manager"]._triggers) == 0

    def test_invalid_trigger_action_rejected(self, gate: Any) -> None:
        r = _run(gate["tool"].execute({
            "trigger_type": "delay", "delay_seconds": 60, "message": "m",
            "pipeline_id": "p1", "trigger_action": "magic",
        }))
        assert not r.success and r.error_code == "INVALID_TRIGGER_ACTION"

    def test_legacy_shell_string_action_explicitly_rejected(self, gate: Any) -> None:
        """旧 shell 字符串形态（action=command + action_params.command）显式报停，
        不静默降级为 notify（存量生产者：dsh_adapter translate_hooks_config）。"""
        class _MustNotCall:
            async def call(self, *args: Any, **kwargs: Any) -> Any:
                raise AssertionError("旧形态不应发起审批")

        gate["holder"]["provider"] = _MustNotCall()
        r = _run(gate["tool"].execute({
            "action": "command", "trigger_type": "event", "event_type": "run.completed",
            "message": "m", "pipeline_id": "p1",
            "action_params": {"command": "node n.mjs"},
        }))
        assert not r.success and r.error_code == "INVALID_ACTION", r.error
        assert "trigger_action=command" in (r.error or ""), r.error
        assert len(gate["manager"]._triggers) == 0, "旧形态触发器被静默注册"


# ═══════════════════════════════════════════════════════════
# manifest：审批链接线（human-interaction 授权 + 长等待超时 + schema 同步）
# ═══════════════════════════════════════════════════════════


class TestCommandGateManifest:
    def _manifest(self) -> dict[str, Any]:
        import json

        return json.loads((_PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))

    def test_plugin_json_grants_human_interaction(self) -> None:
        """审批闸需要反向调用 human-interaction capability（G6 白名单）。"""
        grants = self._manifest().get("granted_capabilities", [])
        assert "human-interaction" in grants, grants

    def test_plugin_json_declares_long_wait_mcp_timeout(self) -> None:
        """审批等待可达 86400s，内核 MCP client 300s 默认会掐断——manifest 须显式声明。"""
        mcp = self._manifest().get("mcp", {})
        assert int(mcp.get("request_timeout_secs", 0)) >= 86500, mcp

    def test_plugin_json_input_schema_synced_with_backend(self) -> None:
        """manifest 内维护的 input_schema 必须与 tool.py 后端定义一致（防漂移）。"""
        mod = _load_tool()
        backend = mod.TriggerSetupTool.get_tool_definition().input_schema
        tools = self._manifest()["capabilities"]["tools"]
        declared = next(t for t in tools if t["name"] == "trigger_setup")
        assert declared["input_schema"] == backend

    def test_backend_schema_declares_action_template(self) -> None:
        mod = _load_tool()
        props = mod.TriggerSetupTool.get_tool_definition().input_schema["properties"]
        assert props["trigger_action"]["enum"] == ["notify", "command"]
        assert props["command"]["type"] == "array" and props["command"]["items"]["type"] == "string"


# ═══════════════════════════════════════════════════════════
# 触发器注册表持久化（P25）：state 是权威持久层，内存注册表是运行缓存。
# 哪个管道的 state 里有触发器字段，哪个管道就有对应的触发——sidecar
# 死亡/迁移/回收后 on_load 从 pipeline_state 全量重灌（根治 P21/P24）。
# ═══════════════════════════════════════════════════════════

_TRIGGER_STATE_PREFIX = "task.trigger.registry."


class _FakePipelineState:
    """内核 pipeline-state capability 假件（update 写面 + list 读面同构）。"""

    def __init__(self) -> None:
        self._tables: dict[str, dict[str, Any]] = {}

    async def write(self, pipeline_id: str, fields: dict[str, Any]) -> None:
        """pipeline-state.update 形状（fields 为扁平点号键 → 值）。"""
        self._tables.setdefault(pipeline_id, {}).update(fields)

    async def list_rows(self) -> list[dict[str, Any]]:
        """pipeline-state.list 形状（每活管道一行，仅出口 trigger 注册表键）。"""
        rows = []
        for pid, fields in self._tables.items():
            row = {k: v for k, v in fields.items() if k.startswith(_TRIGGER_STATE_PREFIX)}
            row["pipeline_id"] = pid
            rows.append(row)
        return rows

    def drop_pipeline(self, pipeline_id: str) -> None:
        """模拟管道清理（state 行整行消失——触发器字段天然随之 GC）。"""
        self._tables.pop(pipeline_id, None)

    def fields_of(self, pipeline_id: str) -> dict[str, Any]:
        return dict(self._tables.get(pipeline_id, {}))


class TestTriggerStatePersistence:
    def _wired(self, store: _FakePipelineState, *, with_provider: bool = False) -> tuple[_LoopThread, TriggerManager]:
        """带主循环 + state 写面的 manager（注册等入口从测试线程调，
        经 run_coroutine_threadsafe 同步落 store，确定性完成）。"""
        lt = _LoopThread()
        lt.__enter__()
        mgr = TriggerManager()
        mgr.set_main_loop(lt.loop)
        mgr.set_state_writer(store.write)
        if with_provider:
            mgr.set_state_provider(store.list_rows)
        return lt, mgr

    def test_register_persists_and_new_manager_rehydrates_and_fires(self) -> None:
        """核心场景：注册同步落 state → 全新实例（sidecar 重启/迁移）重灌 → 15m 周期触发器活到首触发。"""
        store = _FakePipelineState()
        lt, mgr1 = self._wired(store)
        try:
            past = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=20)).isoformat()
            scheduled = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2)
            mgr1.register(_make_config(
                trigger_id="trigger_interval_abc123def456",
                trigger_type=TriggerType.INTERVAL,
                interval_seconds=900,
                max_fires=3,
                metadata={"register_time": past, "user_id": "u-1"},
            ))
            mgr1.register(_make_config(
                trigger_id="trigger_schedule_abc123def456",
                trigger_type=TriggerType.SCHEDULED,
                scheduled_at=scheduled,
                metadata={"register_time": past},
            ))
            # 注册同步落 state：目标管道的 state 出现 trigger.registry 键
            fields = store.fields_of("pipe-1")
            assert "task.trigger.registry.trigger_interval_abc123def456" in fields
            assert "task.trigger.registry.trigger_schedule_abc123def456" in fields
        finally:
            mgr1.stop_check_loop()
        lt.__exit__()

        # 模拟 sidecar 重启：全新 manager 从 state 全量重灌
        lt2, mgr2 = self._wired(store, with_provider=True)
        try:
            assert _run(mgr2.load_from_state()) == 2
            got = mgr2.get("trigger_interval_abc123def456")
            assert got is not None
            assert got.trigger_type == TriggerType.INTERVAL
            assert got.interval_seconds == 900
            assert got.max_fires == 3
            assert got.message == "到点了"
            assert got.pipeline_id == "pipe-1"
            assert got.status == TriggerStatus.ACTIVE
            assert got.metadata.get("register_time") == past
            # 定时时间 datetime 形态还原（tz aware、时刻相等）
            got_s = mgr2.get("trigger_schedule_abc123def456")
            assert got_s is not None
            assert got_s.scheduled_at is not None
            assert got_s.scheduled_at.tzinfo is not None
            assert got_s.scheduled_at == scheduled
            # 重灌后 check_scheduled 能命中 fire（15m 触发器不再活不到首触发）
            now = datetime.datetime.now(datetime.UTC)
            assert mgr2.check_scheduled(now) == ["trigger_interval_abc123def456"]
            assert got.fire_count == 1
            # SCHEDULED 未到 → 不触发
            assert "trigger_schedule_abc123def456" not in mgr2.check_scheduled(now)
        finally:
            mgr2.stop_check_loop()
            lt2.__exit__()

    def test_fire_count_persists_max_semantics_survive_restart(self) -> None:
        """fire 计数落 state：重启不归零，max_fires 语义跨重启保持。"""
        store = _FakePipelineState()
        lt, mgr1 = self._wired(store)
        tid = "trigger_interval_abc123def456"
        past = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=20)).isoformat()
        try:
            mgr1.register(_make_config(
                trigger_id=tid,
                trigger_type=TriggerType.INTERVAL,
                interval_seconds=900,
                max_fires=2,
                metadata={"register_time": past},
            ))
            assert mgr1.check_scheduled(datetime.datetime.now(datetime.UTC)) == [tid]
        finally:
            mgr1.stop_check_loop()
        lt.__exit__()

        # 重启后：fire_count == 1（不归零），且紧随其后未到下次间隔（last_fire_time 生效）
        lt2, mgr2 = self._wired(store, with_provider=True)
        try:
            assert _run(mgr2.load_from_state()) == 1
            got = mgr2.get(tid)
            assert got is not None
            assert got.fire_count == 1
            now = datetime.datetime.now(datetime.UTC)
            assert mgr2.check_scheduled(now) == [], "重启后 last_fire_time 丢失会立即重复触发"
            # 间隔过后第 2 次触发 → 达 max_fires → FIRED（写面已接，终态落 state）
            assert mgr2.check_scheduled(now + datetime.timedelta(seconds=901)) == [tid]
            assert got.status == TriggerStatus.FIRED
        finally:
            mgr2.stop_check_loop()
        lt2.__exit__()

        # 再次重启：FIRED 终态不灌（不再复活）
        lt3, mgr3 = self._wired(store, with_provider=True)
        try:
            assert _run(mgr3.load_from_state()) == 0
            assert mgr3.get(tid) is None
        finally:
            mgr3.stop_check_loop()
            lt3.__exit__()

    def test_cancel_persists_and_rehydrated_manager_has_no_trigger(self) -> None:
        store = _FakePipelineState()
        lt, mgr1 = self._wired(store)
        tid = "trigger_delay_abc123def456"
        try:
            mgr1.register(_make_config(
                trigger_id=tid,
                trigger_type=TriggerType.DELAY,
                delay_seconds=3600,
                metadata={"register_time": datetime.datetime.now(datetime.UTC).isoformat()},
            ))
            assert mgr1.cancel(tid) is True
            stored = store.fields_of("pipe-1")[f"task.trigger.registry.{tid}"]
            assert stored["status"] == "cancelled"
        finally:
            mgr1.stop_check_loop()
        lt.__exit__()

        lt2, mgr2 = self._wired(store, with_provider=True)
        try:
            assert _run(mgr2.load_from_state()) == 0
            assert mgr2.get(tid) is None, "已取消的触发器重灌后不得复活"
        finally:
            mgr2.stop_check_loop()
            lt2.__exit__()

    def test_update_max_fires_persists_reactivation(self) -> None:
        """FIRED 后 update_max_fires 复活 → state 同步为 ACTIVE + 新上限。"""
        store = _FakePipelineState()
        lt, mgr1 = self._wired(store)
        tid = "trigger_delay_abc123def456"
        past = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=5)).isoformat()
        try:
            mgr1.register(_make_config(
                trigger_id=tid,
                trigger_type=TriggerType.DELAY,
                delay_seconds=10,
                max_fires=1,
                metadata={"register_time": past},
            ))
            assert mgr1.check_scheduled(datetime.datetime.now(datetime.UTC)) == [tid]
            assert mgr1.update_max_fires(tid, 3) is True
            stored = store.fields_of("pipe-1")[f"task.trigger.registry.{tid}"]
            assert stored["status"] == "active"
            assert stored["max_fires"] == 3
        finally:
            mgr1.stop_check_loop()
        lt.__exit__()

        lt2, mgr2 = self._wired(store, with_provider=True)
        try:
            assert _run(mgr2.load_from_state()) == 1
            got = mgr2.get(tid)
            assert got is not None
            assert got.status == TriggerStatus.ACTIVE
            assert got.max_fires == 3
        finally:
            mgr2.stop_check_loop()
            lt2.__exit__()

    def test_cleaned_pipeline_skipped_on_rehydrate(self) -> None:
        """目标管道已清理（state 行消失）→ 重灌跳过；残留字段随管道 state 天然 GC。"""
        store = _FakePipelineState()
        lt, mgr1 = self._wired(store)
        tid = "trigger_interval_abc123def456"
        try:
            mgr1.register(_make_config(
                trigger_id=tid,
                trigger_type=TriggerType.INTERVAL,
                interval_seconds=900,
                metadata={"register_time": datetime.datetime.now(datetime.UTC).isoformat()},
            ))
        finally:
            mgr1.stop_check_loop()
        lt.__exit__()
        store.drop_pipeline("pipe-1")

        lt2, mgr2 = self._wired(store, with_provider=True)
        try:
            # 内存里预置残留的陈旧触发器（重启前进程态），重灌以 state 为权威替换
            mgr2._triggers[tid] = _make_config(trigger_id=tid)
            assert _run(mgr2.load_from_state()) == 0
            assert mgr2.get(tid) is None, "管道已清理，陈旧触发器不得残留"
            assert mgr2.list_all() == []
        finally:
            mgr2.stop_check_loop()
            lt2.__exit__()

    def test_load_tolerates_json_string_and_malformed_values(self) -> None:
        """state 值为 JSON 字符串（跨边界序列化形态）可还原；腐败值跳过不炸。"""
        store = _FakePipelineState()
        good = _make_config(
            trigger_id="trigger_event_abc123def456",
            trigger_type=TriggerType.EVENT,
            event_name="task_completed",
            metadata={"register_time": datetime.datetime.now(datetime.UTC).isoformat()},
        )
        good_dict = good.to_state_dict()
        store._tables["pipe-1"] = {
            "task.trigger.registry.trigger_event_abc123def456": json.dumps(good_dict),
            "task.trigger.registry.trigger_broken_000000000001": "not-json{",
            "task.trigger.registry.trigger_wrong_0000000002": 42,
        }
        lt, mgr = self._wired(store, with_provider=True)
        try:
            assert _run(mgr.load_from_state()) == 1
            got = mgr.get("trigger_event_abc123def456")
            assert got is not None
            assert got.event_name == "task_completed"
            assert mgr.get("trigger_broken_000000000001") is None
            assert mgr.get("trigger_wrong_0000000002") is None
        finally:
            mgr.stop_check_loop()
            lt.__exit__()

    def test_load_without_provider_fails_visible(self) -> None:
        """state 聚合读面未注入 → 显式报错（不静默空灌）。"""
        mgr = TriggerManager()
        with pytest.raises(RuntimeError):
            _run(mgr.load_from_state())

    def test_register_without_writer_keeps_memory_behavior(self) -> None:
        """写面未接（桥未就绪/单测形态）→ 注册仍成立（内存运行缓存语义不回归）。"""
        mgr = TriggerManager()
        try:
            cfg = _make_config()
            mgr.register(cfg)
            assert mgr.get("t1") is cfg
        finally:
            mgr.stop_check_loop()

    def test_writer_failure_degrades_with_log_not_crash(self) -> None:
        """写面抛错（内核不可达）→ 记录留痕，注册/触发主流程不崩。"""
        store = _FakePipelineState()

        async def _broken_write(pipeline_id: str, fields: dict[str, Any]) -> None:
            raise RuntimeError("kernel down")

        lt, mgr = self._wired(store)
        try:
            mgr.set_state_writer(_broken_write)
            cfg = _make_config(trigger_type=TriggerType.DELAY, delay_seconds=1)
            mgr.register(cfg)  # 不抛异常
            assert mgr.get("t1") is cfg
        finally:
            mgr.stop_check_loop()
            lt.__exit__()

    def test_plugin_json_exports_trigger_registry_keys(self) -> None:
        """manifest 须声明 export_fields 出口（pipeline-state.list 才能带回注册表字段）。"""
        manifest = json.loads((_PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
        assert "task.trigger.registry.*" in manifest.get("export_fields", []), manifest.get("export_fields")


class TestStatePersistenceWiring:
    """server.py 接线：state 写面注入 + on_load 全量重灌。"""

    def test_state_writer_calls_pipeline_state_update(self) -> None:
        server = _load_server()

        class FakeState:
            def __init__(self) -> None:
                self.calls: list[tuple] = []

            async def call(self, method: str, params: dict) -> dict:
                self.calls.append((method, params))
                return {"status": "updated"}

        cap = FakeState()

        def _get_cap(_name: str) -> FakeState:
            return cap

        server.plugin.get_capability = _get_cap  # type: ignore[method-assign]
        writer = server._make_state_writer()
        _run(writer("p1", {"task.trigger.registry.t1": {"trigger_id": "t1"}}))
        assert cap.calls == [
            ("update", {"pipeline_id": "p1", "fields": {"task.trigger.registry.t1": {"trigger_id": "t1"}}})
        ]

    def test_on_load_injects_state_writer_and_rehydrates(self) -> None:
        import triggers.manager as manager_mod

        server = _load_server()
        real_mgr = manager_mod.get_trigger_manager()
        real_mgr.stop_check_loop()
        saved = (real_mgr._state_writer, real_mgr._state_provider, real_mgr._event_bridge_ready)
        try:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(server._on_load({}))
                assert real_mgr._state_writer is not None, "on_load 应注入 state 写面"
            finally:
                loop.close()
        finally:
            real_mgr._state_writer, real_mgr._state_provider, real_mgr._event_bridge_ready = saved
            real_mgr.stop_check_loop()


class _KillableProc:
    """带 pid 与可注入 kill 行为的假进程（仅覆盖外部终止边界）。"""

    def __init__(self, pid: int, *, kill_error: Exception | None = None) -> None:
        self.pid = pid
        self._kill_error = kill_error
        self.kill_called = False

    def kill(self) -> None:
        self.kill_called = True
        if self._kill_error is not None:
            raise self._kill_error


class TestKillProcessTree:
    """超时杀进程树平台对称：Windows taskkill /T /F；POSIX killpg 整组。
    失败必须留痕（pid 上下文），不得静默残留进程。"""

    def test_windows_taskkill_failure_logs_warning(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        import triggers.manager as manager_mod

        def _boom(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("taskkill unavailable")

        monkeypatch.setattr(manager_mod.subprocess, "run", _boom)
        with caplog.at_level("WARNING", logger=manager_mod.logger.name):
            TriggerManager._kill_process_tree(_KillableProc(4321))
        assert any("4321" in rec.message and rec.levelno >= logging.WARNING for rec in caplog.records), caplog.text

    def test_windows_taskkill_success_path_no_warning(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        """Windows 正常路径（taskkill 可用）：无告警（≥2 组区分输入）。"""
        import triggers.manager as manager_mod

        monkeypatch.setattr(manager_mod.os, "name", "nt")
        with caplog.at_level("WARNING", logger=manager_mod.logger.name):
            TriggerManager._kill_process_tree(_KillableProc(4326))
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_posix_uses_killpg_on_whole_group(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """POSIX：killpg(pid, SIGKILL) 整组终止——不再单 proc.kill() 留孤儿孙进程。"""
        import triggers.manager as manager_mod

        calls: list[tuple[int, Any]] = []

        def _fake_killpg(pid: int, sig: Any) -> None:
            calls.append((pid, sig))

        monkeypatch.setattr(manager_mod.os, "name", "posix")
        monkeypatch.setattr(manager_mod.os, "killpg", _fake_killpg, raising=False)
        proc = _KillableProc(4325)

        TriggerManager._kill_process_tree(proc)

        assert calls == [(4325, manager_mod._POSIX_SIGKILL)]
        assert proc.kill_called is False, "POSIX 分支应整组 killpg，不得单杀根进程"

    def test_posix_group_already_gone_is_success(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        """POSIX 进程组已退出（ProcessLookupError）= 目标已达成：不告警不抛。"""
        import triggers.manager as manager_mod

        def _gone(pid: int, sig: Any) -> None:
            raise ProcessLookupError("no such process")

        monkeypatch.setattr(manager_mod.os, "name", "posix")
        monkeypatch.setattr(manager_mod.os, "killpg", _gone, raising=False)
        with caplog.at_level("WARNING", logger=manager_mod.logger.name):
            TriggerManager._kill_process_tree(_KillableProc(4327))
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_posix_killpg_failure_logs_warning(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        """POSIX killpg 失败（权限等 OSError）：告警带 pid，不吞不抛。"""
        import triggers.manager as manager_mod

        def _denied(pid: int, sig: Any) -> None:
            raise OSError("operation not permitted")

        monkeypatch.setattr(manager_mod.os, "name", "posix")
        monkeypatch.setattr(manager_mod.os, "killpg", _denied, raising=False)
        with caplog.at_level("WARNING", logger=manager_mod.logger.name):
            TriggerManager._kill_process_tree(_KillableProc(4322))
        assert any("4322" in rec.message and rec.levelno >= logging.WARNING for rec in caplog.records), caplog.text


class TestCommandSpawnShape:
    """command 动作 spawn 形态：POSIX 独立进程组（killpg 前提），Windows 关闭。"""

    @pytest.mark.parametrize("platform_name,expected_session", [("nt", False), ("posix", True)])
    def test_spawn_sets_start_new_session_by_platform(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, platform_name: str, expected_session: bool
    ) -> None:
        """start_new_session 随平台收口：POSIX True（自成进程组）、Windows False。

        Windows 宿主无法真跑 POSIX spawn，按参数形态断言（分支纯参数差异，
        行为等价性由 TestKillProcessTree 的 killpg 形态断言补足）。以模块级
        os 垫片钉平台——不能全局改 os.name（Python 3.14 pathlib 构造会跟着
        平台漂移，PosixPath 在 Windows 宿主直接拒实例化）。
        """
        import triggers.manager as manager_mod

        monkeypatch.chdir(tmp_path)  # 审计日志落 tmp，不污染仓库
        monkeypatch.setattr(
            manager_mod, "os", types.SimpleNamespace(name=platform_name, environ=dict(os.environ))
        )
        captured: dict[str, Any] = {}

        class _FakeProc:
            pid = 1234
            returncode = 0

            def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
                return b"out", b""

            def kill(self) -> None:
                pass

        def _fake_popen(cmd: Any, **kwargs: Any) -> Any:
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return _FakeProc()

        monkeypatch.setattr(manager_mod.subprocess, "Popen", _fake_popen)
        mgr = TriggerManager()
        trigger = _make_config(
            action="command",
            action_params={"cmd": ["echo", "hi"], "timeout_ms": 1000},
        )

        mgr._run_command(trigger, "evt", {"k": "v"})

        assert captured["kwargs"]["start_new_session"] is expected_session
        assert captured["kwargs"]["shell"] is False
        assert captured["cmd"] == ["echo", "hi"]
        # 执行留痕进 metadata（读面可查）
        assert trigger.metadata["last_command_execution"]["exit_code"] == 0
