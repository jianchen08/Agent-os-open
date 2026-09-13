# @feature: FP-0.2.〇 管道引擎 | @ci: python-coverage
"""triggers/manager.py 行覆盖缺口补充测试。

聚焦既有 test_triggers.py 未触达的守卫分支：事件过滤不匹配/类型守卫、
条件评估跳过族、persist 序列化失败、load_from_state 脏数据跳过族、
检查循环与条件轮询的注入守卫、域事件桥的注入器缺失/竞态注销、
自动父通知的上下文分档提示、定时/周期检查的脏时间戳兜底。
外部依赖（注入器/state 读写面/调度检查）沿用同款假件手法，断言
可观察行为（返回值/state 副作用/投递内容），不断言调用计数等内部细节。
"""

from __future__ import annotations

import asyncio
import datetime
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent  # plugins/shared/tools/triggers_ext/
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from triggers.manager import TriggerManager  # noqa: E402
from triggers.types import TriggerConfig, TriggerStatus, TriggerType  # noqa: E402


def _make_config(**overrides: Any) -> TriggerConfig:
    base: dict[str, Any] = {
        "trigger_id": "t1",
        "name": "测试触发器",
        "trigger_type": TriggerType.EVENT,
        "max_fires": 1,
        "message": "到点了",
        "pipeline_id": "pipe-1",
    }
    base.update(overrides)
    return TriggerConfig(**base)


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _wait_for(cond: Any, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


# ═══════════════════════════════════════════════════════════
# 事件触发评估：类型守卫 / 过滤不匹配（触发器须保持 ACTIVE）
# ═══════════════════════════════════════════════════════════


class TestEvaluateEventGuards:
    def test_non_event_type_skipped(self) -> None:
        """非 EVENT 类型触发器不参与事件评估（守卫 continue）。"""
        mgr = TriggerManager()
        try:
            mgr.register(_make_config(
                trigger_id="t-delay", trigger_type=TriggerType.DELAY,
                delay_seconds=3600, event_name="e", max_fires=3,
            ))
            assert mgr.evaluate_event("e", {}) == []
            delay_cfg = mgr.get("t-delay")
            assert delay_cfg is not None
            assert delay_cfg.fire_count == 0
        finally:
            mgr.stop_check_loop()

    def test_scalar_filter_mismatch_skips(self) -> None:
        """标量过滤（actual != expected）不匹配 → 不触发且计数不变。"""
        mgr = TriggerManager()
        try:
            cfg = _make_config(event_name="e", event_filter={"task_id": "x-1"}, max_fires=3)
            mgr.register(cfg)
            assert mgr.evaluate_event("e", {"task_id": "other"}) == []
            assert cfg.fire_count == 0
            assert mgr.evaluate_event("e", {"task_id": "x-1"}) == ["t1"]  # 命中对照
        finally:
            mgr.stop_check_loop()

    def test_operator_filter_mismatch_skips(self) -> None:
        """操作符过滤（op/value 形态）不匹配 → 不触发且计数不变。"""
        mgr = TriggerManager()
        try:
            cfg = _make_config(event_name="e", event_filter={"prio": {"op": "gt", "value": 3}}, max_fires=3)
            mgr.register(cfg)
            assert mgr.evaluate_event("e", {"prio": 2}) == []
            assert mgr.evaluate_event("e", {"prio": 3}) == []  # 边界值不满足 gt
            assert cfg.fire_count == 0
            assert mgr.evaluate_event("e", {"prio": 4}) == ["t1"]  # 命中对照
        finally:
            mgr.stop_check_loop()


# ═══════════════════════════════════════════════════════════
# 条件触发评估：类型/停止条件守卫
# ═══════════════════════════════════════════════════════════


class TestEvaluateConditionGuards:
    def test_non_condition_and_expired_skipped(self) -> None:
        """非 CONDITION 类型与超 max_time 的条件触发器均不参与评估。"""
        mgr = TriggerManager()
        try:
            mgr.register(_make_config(trigger_id="t-event", event_name="e", max_fires=3))
            past = (_utc_now() - datetime.timedelta(hours=2)).isoformat()
            stopped = _make_config(
                trigger_id="t-cond", trigger_type=TriggerType.CONDITION,
                condition_expression="x == 1", max_fires=3,
                max_time_seconds=60, metadata={"register_time": past},
            )
            mgr.register(stopped)
            assert mgr.evaluate_condition({"x": 1}) == []
            assert stopped.fire_count == 0
            assert stopped.metadata.get("cond_last_value") is None, "被跳过的触发器不得记录电平"
        finally:
            mgr.stop_check_loop()


class TestFireManually:
    def test_unknown_trigger_id_returns_false(self) -> None:
        mgr = TriggerManager()
        assert mgr.fire_manually("missing") is False


# ═══════════════════════════════════════════════════════════
# persist：空管道跳过 / 序列化失败守卫（state 是外部持久层，假件记录写面）
# ═══════════════════════════════════════════════════════════


class TestPersistGuards:
    def test_persist_skips_empty_pipeline_id(self) -> None:
        """pipeline_id 为空 → 不触碰内核写面，内存注册仍成立。"""
        writes: list[tuple[str, dict]] = []

        async def writer(pipeline_id: str, fields: dict) -> None:
            writes.append((pipeline_id, fields))

        mgr = TriggerManager()
        mgr.set_state_writer(writer)
        cfg = _make_config(pipeline_id="")
        mgr.register(cfg)
        try:
            assert writes == []
            assert mgr.get("t1") is cfg
        finally:
            mgr.stop_check_loop()

    def test_persist_serialization_failure_keeps_memory_registration(self) -> None:
        """畸形内存态（scheduled_at 非 datetime）→ 序列化失败留痕，state 未落、注册仍成立。"""
        writes: list[tuple[str, dict]] = []

        async def writer(pipeline_id: str, fields: dict) -> None:
            writes.append((pipeline_id, fields))

        mgr = TriggerManager()
        mgr.set_state_writer(writer)
        cfg = _make_config(trigger_type=TriggerType.SCHEDULED, scheduled_at="not-a-datetime")
        mgr.register(cfg)  # 不抛异常
        try:
            assert writes == [], "序列化失败的触发器不得落 state"
            assert mgr.get("t1") is cfg
        finally:
            mgr.stop_check_loop()


# ═══════════════════════════════════════════════════════════
# load_from_state：脏数据逐键跳过族（state provider 为外部读面假件）
# ═══════════════════════════════════════════════════════════


class TestLoadFromStateGuards:
    @staticmethod
    def _mgr_with_rows(rows: list[dict[str, Any]]) -> TriggerManager:
        mgr = TriggerManager()
        mgr.set_state_provider(lambda: rows)
        return mgr

    def test_row_without_pipeline_id_skipped(self) -> None:
        """聚合行缺 pipeline_id（无名管道行）→ 整行跳过，不灌任何触发器。"""
        good = _make_config(
            trigger_id="trigger_event_aaaaaaaaaa1", event_name="task_completed",
        ).to_state_dict()
        mgr = self._mgr_with_rows([
            {"task.trigger.registry.trigger_event_aaaaaaaaaa1": good},
        ])
        assert _run(mgr.load_from_state()) == 0
        assert mgr.list_all() == []

    def test_bad_type_empty_id_and_corrupt_json_skipped(self) -> None:
        """枚举非法/空 trigger_id/腐败 JSON → 逐键跳过留痕，合法键照常灌入。"""
        good = _make_config(
            trigger_id="trigger_event_bbbbbbbbbb2",
            trigger_type=TriggerType.EVENT,
            event_name="task_completed",
        ).to_state_dict()
        rows = [{
            "pipeline_id": "p1",
            # trigger_type 枚举非法 → from_state_dict 抛 ValueError
            "task.trigger.registry.trigger_event_cccccccccc3": {"trigger_id": "x", "trigger_type": "bogus"},
            # 合法 dict 但 trigger_id 为空 → 跳过
            "task.trigger.registry.trigger_event_dddddddddd4": {"trigger_type": "delay"},
            # 以 "{" 开头但非合法 JSON → 还原失败跳过
            "task.trigger.registry.trigger_event_eeeeeeeeee5": "{corrupted",
            "task.trigger.registry.trigger_event_bbbbbbbbbb2": good,
        }]
        mgr = self._mgr_with_rows(rows)
        assert _run(mgr.load_from_state()) == 1
        restored = mgr.get("trigger_event_bbbbbbbbbb2")
        assert restored is not None
        assert restored.pipeline_id == "p1"
        assert mgr.get("trigger_event_cccccccccc3") is None
        assert mgr.get("trigger_event_dddddddddd4") is None
        assert mgr.get("trigger_event_eeeeeeeeee5") is None


# ═══════════════════════════════════════════════════════════
# 检查循环 / 条件轮询的守卫族（check_scheduled 为既有 harness 认可的
# 调度接缝；注入器为外部依赖假件；事件驱动退出，无大等待）
# ═══════════════════════════════════════════════════════════


class _LoopThread:
    """在独立线程运行事件循环（供 run_coroutine_threadsafe 使用）。"""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True)

    def __enter__(self) -> "_LoopThread":
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5)
        self.loop.close()


class TestCheckLoopGuards:
    def test_loop_survives_check_error_and_dispatch_guards(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """检查异常/幽灵 ID/命令分发/缺投递信息/注入故障 均不炸循环。"""
        import triggers.manager as tm_mod

        monkeypatch.chdir(tmp_path)  # 审计日志落 tmp
        monkeypatch.setattr(tm_mod, "_TRIGGER_CHECK_INTERVAL", 0.01)

        out_cmd = tmp_path / "cmd_out.txt"
        code = f"open(r'{out_cmd}', 'w').write('fired')"
        injected: list[str] = []

        def broken_injector(pipeline_id: str, message: str, user_id: str) -> Any:
            injected.append(pipeline_id)
            raise RuntimeError("kernel down")  # 畸形注入器（外部依赖故障注入）

        done = threading.Event()
        calls = {"n": 0}

        def fake_check_scheduled(now: Any) -> list[str]:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("check boom once")  # 第一轮检查异常 → 循环须存活
            if calls["n"] == 2:
                done.set()
                return ["t-ghost", "t-cmd", "t-noinfo", "t-badinject"]
            return []  # 后续轮空转，等待主线程停循环

        mgr = TriggerManager()
        mgr.check_scheduled = fake_check_scheduled  # type: ignore[method-assign]
        now = _utc_now().isoformat()
        cmd_cfg = _make_config(
            trigger_id="t-cmd", action="command",
            action_params={"cmd": [sys.executable, "-c", code], "timeout_ms": 10000},
            metadata={"register_time": now},
        )
        noinfo_cfg = _make_config(trigger_id="t-noinfo", pipeline_id="", message="")
        bad_cfg = _make_config(trigger_id="t-badinject", pipeline_id="p-bad", message="m")
        for cfg in (cmd_cfg, noinfo_cfg, bad_cfg):
            cfg.status = TriggerStatus.ACTIVE
            mgr._triggers[cfg.trigger_id] = cfg

        with _LoopThread() as lt:
            mgr.set_main_loop(lt.loop)
            mgr.set_injector(broken_injector)
            t = threading.Thread(target=mgr._check_loop_sync, daemon=True)
            t.start()
            try:
                assert done.wait(timeout=5), "检查循环未在超时内完成第二轮"
                assert _wait_for(lambda: out_cmd.exists()), "command 触发器未执行"
            finally:
                mgr.stop_check_loop()
                t.join(timeout=5)
        assert not t.is_alive(), "检查循环未退出"
        # 守卫生效：缺投递信息的触发器未触碰注入器；注入故障的到达过注入器
        assert "" not in injected, "缺 pipeline_id 的触发器不得触碰注入器"
        assert "p-bad" in injected, "注入故障触发器应到达注入器并留痕"


class TestPollConditionsGuards:
    def test_command_noinfo_and_inject_failure_guards(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """条件轮询：命令分发/缺投递信息跳过/注入故障留痕，互不妨碍。"""
        monkeypatch.chdir(tmp_path)
        out = tmp_path / "cond_cmd.txt"
        code = f"open(r'{out}', 'w').write('fired')"
        injected: list[str] = []

        def broken_injector(pipeline_id: str, message: str, user_id: str) -> Any:
            injected.append(pipeline_id)
            raise RuntimeError("kernel down")

        mgr = TriggerManager()
        mgr.set_injector(broken_injector)
        mgr.set_state_provider(lambda: [{"ready": True, "x": 1}])
        now = _utc_now().isoformat()
        cmd_cfg = _make_config(
            trigger_id="t-cmd", trigger_type=TriggerType.CONDITION,
            condition_expression="ready == true", max_fires=0, action="command",
            action_params={"cmd": [sys.executable, "-c", code], "timeout_ms": 10000},
            metadata={"register_time": now},
        )
        noinfo_cfg = _make_config(
            trigger_id="t-noinfo", trigger_type=TriggerType.CONDITION,
            condition_expression="x == 1", max_fires=0, pipeline_id="", message="",
            metadata={"register_time": now},
        )
        bad_cfg = _make_config(
            trigger_id="t-badinject", trigger_type=TriggerType.CONDITION,
            condition_expression="x == 1", max_fires=0, pipeline_id="p-bad", message="m",
            metadata={"register_time": now},
        )
        for cfg in (cmd_cfg, noinfo_cfg, bad_cfg):
            mgr.register(cfg)
        with _LoopThread() as lt:
            mgr.set_main_loop(lt.loop)
            try:
                mgr._poll_conditions()  # 不抛异常
                assert _wait_for(lambda: out.exists()), "command 条件触发器未执行命令"
            finally:
                mgr.stop_check_loop()
        # 三个触发器均计一次边沿；守卫决定后续动作路径
        assert cmd_cfg.fire_count == 1
        assert noinfo_cfg.fire_count == 1
        assert bad_cfg.fire_count == 1
        assert "" not in injected, "缺 pipeline_id 的触发器不得触碰注入器"
        assert "p-bad" in injected, "注入故障触发器应到达注入器并留痕"

    @pytest.mark.parametrize("loop_mode", ["none", "closed"], ids=["no-loop", "closed-loop"])
    def test_async_provider_without_usable_loop_skips(self, loop_mode: str) -> None:
        """async provider 但主循环缺失/已关闭 → 跳过本轮，不触发不炸。"""
        async def provider() -> list[dict]:
            return [{"x": 1}]

        mgr = TriggerManager()
        if loop_mode == "closed":
            loop = asyncio.new_event_loop()
            loop.close()
            mgr.set_main_loop(loop)
        mgr.set_state_provider(provider)
        cfg = _make_config(
            trigger_type=TriggerType.CONDITION, condition_expression="x == 1", max_fires=0,
            metadata={"register_time": _utc_now().isoformat()},
        )
        mgr.register(cfg)
        try:
            mgr._poll_conditions()
            assert cfg.fire_count == 0, "主循环不可用时条件轮询必须跳过"
        finally:
            mgr.stop_check_loop()


class TestCollectStateRows:
    def test_non_list_raises(self) -> None:
        """provider 返回非列表 → fail-visible RuntimeError。"""
        mgr = TriggerManager()
        mgr.set_state_provider(lambda: {"not": "a list"})
        with pytest.raises(RuntimeError, match="非列表"):
            _run(mgr.collect_state_rows())

    def test_awaitable_non_list_raises(self) -> None:
        async def provider() -> Any:
            return "not-a-list"

        mgr = TriggerManager()
        mgr.set_state_provider(provider)
        with pytest.raises(RuntimeError, match="非列表"):
            _run(mgr.collect_state_rows())

    def test_filters_non_dict_rows(self) -> None:
        """列表内非 dict 行被过滤（正路对照）。"""
        mgr = TriggerManager()
        mgr.set_state_provider(lambda: [{"x": 1}, "junk", 42])
        assert _run(mgr.collect_state_rows()) == [{"x": 1}]


# ═══════════════════════════════════════════════════════════
# 域事件桥：注入器缺失 / 投递信息缺失 / await 期间竞态注销 / 父通知分档
# ═══════════════════════════════════════════════════════════


class TestDomainEventGuards:
    def test_trigger_unregistered_during_auto_notify_not_injected(self) -> None:
        """自动父通知 await 期间触发器被注销 → 注入阶段跳过（真实竞态窗口）。"""
        received: list[tuple[str, str]] = []

        async def injector(pipeline_id: str, message: str, user_id: str) -> str:
            received.append((pipeline_id, message))
            mgr.unregister("t1")  # 系统通知 await 期间注销显式触发器
            return "ok"

        mgr = TriggerManager()
        mgr.set_injector(injector)
        mgr.register(_make_config(event_name="task_completed", max_fires=3))
        try:
            fired = _run(mgr.handle_domain_event(
                "task_completed", {"parent_pipeline_id": "parent", "user_id": "u1"},
            ))
            assert fired == ["t1"], "评估发生在注销前，命中结果不受影响"
            assert len(received) == 1, "只应投递系统父通知一条"
            assert received[0][0] == "parent", "投递目标应为父管道"
            assert "触发器通知" not in received[0][1], "已注销触发器的消息不得投递"
        finally:
            mgr.stop_check_loop()

    def test_trigger_without_delivery_info_skips_inject(self) -> None:
        """命中触发器缺 pipeline_id/message → 评估照常命中，注入跳过。"""
        received: list[tuple[str, str]] = []

        async def injector(pipeline_id: str, message: str, user_id: str) -> str:
            received.append((pipeline_id, message))
            return "ok"

        mgr = TriggerManager()
        mgr.set_injector(injector)
        mgr.register(_make_config(
            trigger_id="t-noinfo", event_name="task_completed",
            pipeline_id="", message="", max_fires=3,
        ))
        try:
            assert _run(mgr.handle_domain_event("task_completed", {})) == ["t-noinfo"]
            assert received == []
        finally:
            mgr.stop_check_loop()

    def test_injector_not_set_skips_delivery(self) -> None:
        """注入器未设置（server 接线缺失）→ 命中留痕跳过，不炸。"""
        mgr = TriggerManager()
        mgr.register(_make_config(event_name="run.failed", max_fires=3))
        try:
            assert _run(mgr.handle_domain_event("run.failed", {})) == ["t1"]
        finally:
            mgr.stop_check_loop()


class TestAutoNotifyParentGuards:
    @staticmethod
    def _event(pct: float | None) -> dict[str, Any]:
        data: dict[str, Any] = {
            "parent_pipeline_id": "parent", "task_id": "t9", "user_id": "u1",
            "title": "写周报",
        }
        if pct is not None:
            data["context_usage"] = {"pct": pct, "input_tokens": 1000, "context_window": 10000}
        return data

    @pytest.mark.parametrize(
        ("pct", "hint"),
        [(60.0, "建议优先创建新任务"), (30.0, "不建议继续向此 Agent 继承提交"), (10.0, "可直接继续向此 Agent 派发任务")],
        ids=["over-50", "25-50", "under-25"],
    )
    def test_context_usage_hint_bands(self, pct: float, hint: str) -> None:
        """上下文使用率三档提示全覆盖（>50 / 25-50 / ≤25）。"""
        received: list[tuple[str, str]] = []

        async def injector(pipeline_id: str, message: str, user_id: str) -> str:
            received.append((pipeline_id, message))
            return "ok"

        mgr = TriggerManager()
        mgr.set_injector(injector)
        try:
            _run(mgr.handle_domain_event("task_completed", self._event(pct)))
            assert received and received[0][0] == "parent"
            assert hint in received[0][1], received[0][1]
        finally:
            mgr.stop_check_loop()

    def test_context_usage_without_pct_omits_block(self) -> None:
        """无 context_usage 数据 → 不拼上下文段（对照组）。"""
        received: list[tuple[str, str]] = []

        async def injector(pipeline_id: str, message: str, user_id: str) -> str:
            received.append((pipeline_id, message))
            return "ok"

        mgr = TriggerManager()
        mgr.set_injector(injector)
        try:
            _run(mgr.handle_domain_event("task_completed", self._event(None)))
            assert received and "上下文使用率" not in received[0][1]
        finally:
            mgr.stop_check_loop()

    @pytest.mark.parametrize("event_name", ["task_completed", "task_failed"])
    def test_injector_not_set_parent_notify_skips(self, event_name: str) -> None:
        """注入器未设置 → 父通知留痕放弃，域事件桥主流程不炸。"""
        mgr = TriggerManager()
        try:
            assert _run(mgr.handle_domain_event(
                event_name,
                {"parent_pipeline_id": "parent", "task_id": "t9", "user_id": "u1"},
            )) == []
        finally:
            mgr.stop_check_loop()

    def test_inject_failure_does_not_break_bridge_or_evaluation(self) -> None:
        """父通知注入失败（内核侧异常）→ 留痕不外抛，显式触发器照常命中。"""
        async def bad_injector(pipeline_id: str, message: str, user_id: str) -> str:
            raise RuntimeError("kernel down")

        mgr = TriggerManager()
        mgr.set_injector(bad_injector)
        mgr.register(_make_config(
            trigger_id="t-explicit", event_name="task_completed",
            max_fires=3, metadata={"user_id": "u2"},
        ))
        try:
            fired = _run(mgr.handle_domain_event(
                "task_completed",
                {"parent_pipeline_id": "parent", "task_id": "t9", "user_id": "u1"},
            ))
            assert fired == ["t-explicit"], "注入失败不影响评估结果"
        finally:
            mgr.stop_check_loop()


# ═══════════════════════════════════════════════════════════
# 定时/周期检查的时间戳兜底（_check_* 直调与既有 test_triggers.py 同款手法）
# ═══════════════════════════════════════════════════════════


class TestScheduleTimeGuards:
    def test_delay_without_register_time_not_due(self) -> None:
        """DELAY 缺 register_time → 视为未到期（不抛异常）。"""
        mgr = TriggerManager()
        cfg = _make_config(trigger_type=TriggerType.DELAY, delay_seconds=10, metadata={})
        assert mgr._check_delay(cfg, _utc_now()) is False

    def test_interval_non_interval_type_not_due(self) -> None:
        """非 INTERVAL 类型 → 类型守卫返回 False（与 _check_delay 同款防御）。"""
        mgr = TriggerManager()
        assert mgr._check_interval(_make_config(trigger_type=TriggerType.EVENT), _utc_now()) is False

    def test_interval_without_register_time_not_due(self) -> None:
        """INTERVAL 首触发参考（register_time）缺失 → 视为未到期。"""
        mgr = TriggerManager()
        cfg = _make_config(trigger_type=TriggerType.INTERVAL, interval_seconds=60, metadata={})
        assert mgr._check_interval(cfg, _utc_now()) is False

    def test_interval_corrupt_last_fire_time_not_due(self) -> None:
        """last_fire_time 腐败 → 防御性跳过本轮（不触发、计数不变）。"""
        mgr = TriggerManager()
        try:
            cfg = _make_config(
                trigger_type=TriggerType.INTERVAL, interval_seconds=60, max_fires=0,
                metadata={
                    "register_time": _utc_now().isoformat(),
                    "last_fire_time": "not-a-date",
                },
            )
            cfg.fire_count = 1
            mgr.register(cfg)
            assert mgr.check_scheduled(_utc_now()) == []
            assert cfg.fire_count == 1
            assert cfg.status == TriggerStatus.ACTIVE
        finally:
            mgr.stop_check_loop()
