# @feature: FP-0.2.〇 触发器 | @ci: python-coverage
"""trigger.fired 前端 WS 帧派生测试（X24-F1/P-71 异常交付通知）。

契约（先例 = tasks/events.py task_terminal_ws_frame 同款模式）：
- 帧派生纯函数 ``trigger_fired_ws_frame``：通知面只报异常交付——
  delivery=sent 不出帧（正常交付已由聊天注入通道可见，重复触达只是噪音），
  failed/skipped 出帧（载荷含 trigger_id/name/pipeline_id/thread_id/
  delivery，failure 非空才携带）；无路由坐标（pipeline_id 空）不出帧。
- manager 两腿独立：域总线腿照旧全量广播 trigger.fired（事项域订阅方消费），
  WS 腿只对异常交付经 frontend.emit 推前端帧；出口缺席（旧内核）降级不炸。
"""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent  # plugins/shared/triggers_ext/
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from triggers.manager import TriggerManager, trigger_fired_ws_frame  # noqa: E402
from triggers.types import TriggerConfig, TriggerType  # noqa: E402


def _make_config(**overrides: Any) -> TriggerConfig:
    base = {
        "trigger_id": "trigger_schedule_0123456789ab",
        "name": "每日提醒",
        "trigger_type": TriggerType.SCHEDULED,
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

    def __enter__(self) -> "_LoopThread":
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5)
        self.loop.close()


class _StubFrontendEmitter:
    """FrontendEmitter 桩：available + async emit 记录调用。"""

    def __init__(self) -> None:
        self.frames: list[tuple[str, dict[str, Any]]] = []
        self._seen = threading.Event()
        self.available = True

    async def emit(self, event: str, payload: dict[str, Any]) -> None:
        self.frames.append((event, payload))
        self._seen.set()

    def wait(self, timeout: float = 2.0) -> bool:
        return self._seen.wait(timeout)


# ══ 帧派生纯函数（区分度输入：sent/failed/skipped × 有无 failure × 有无坐标）══


class TestTriggerFiredWsFrame:
    def test_sent_returns_none(self) -> None:
        """delivery=sent → 不出帧：正常交付聊天注入通道已可见（防噪音设计）。"""
        assert (
            trigger_fired_ws_frame(
                trigger_id="trigger_schedule_0123456789ab",
                trigger_name="每日提醒",
                pipeline_id="pipe-1",
                delivery="sent",
            )
            is None
        )

    def test_failed_frame_carries_identity_and_failure(self) -> None:
        """delivery=failed → 帧非 None，载荷含触发器标识与失败原因（字面值断言）。"""
        frame = trigger_fired_ws_frame(
            trigger_id="trigger_schedule_0123456789ab",
            trigger_name="每日提醒",
            pipeline_id="pipe-1",
            delivery="failed",
            failure="连接被拒绝",
        )
        assert frame is not None
        event, payload = frame
        assert event == "trigger_fired"
        assert payload["trigger_id"] == "trigger_schedule_0123456789ab"
        assert payload["name"] == "每日提醒"
        assert payload["pipeline_id"] == "pipe-1"
        assert payload["thread_id"] == "pipe-1"
        assert payload["delivery"] == "failed"
        assert payload["failure"] == "连接被拒绝"

    def test_skipped_frame_without_failure_omits_key(self) -> None:
        """delivery=skipped、无 failure → 帧非 None 且不携带 failure 键（空不携带）。"""
        frame = trigger_fired_ws_frame(
            trigger_id="trigger_delay_0123456789ab",
            trigger_name="",
            pipeline_id="pipe-2",
            delivery="skipped",
        )
        assert frame is not None
        event, payload = frame
        assert event == "trigger_fired"
        assert payload["delivery"] == "skipped"
        assert "failure" not in payload
        assert payload["name"] == ""  # name 缺席照出帧，前端降级短 id 文案

    def test_frame_none_without_pipeline_coordinate(self) -> None:
        """pipeline_id 空 → None（内核 frontend.emit 对 thread_id 空直接丢弃）。"""
        assert (
            trigger_fired_ws_frame(
                trigger_id="trigger_interval_0123456789ab",
                trigger_name="周期任务",
                pipeline_id="",
                delivery="failed",
                failure="boom",
            )
            is None
        )


# ══ manager 两腿接线（域总线全量 + WS 只报异常，出口缺席降级）══


class TestEmitTriggerFiredWsLeg:
    def test_failed_delivery_emits_ws_frame_and_domain_event(self) -> None:
        """failed：域事件照发（事项域消费不受扰）+ WS 帧带失败原因同批出。"""
        domain_events: list[tuple[str, dict[str, Any]]] = []
        domain_seen = threading.Event()

        async def domain_emitter(event: str, tags: dict[str, Any]) -> None:
            domain_events.append((event, tags))
            domain_seen.set()

        frontend = _StubFrontendEmitter()
        with _LoopThread() as lt:
            mgr = TriggerManager()
            mgr.set_main_loop(lt.loop)
            mgr.set_domain_emitter(domain_emitter)
            mgr.set_frontend_emitter(frontend)
            cfg = _make_config()
            mgr._emit_trigger_fired(cfg, "failed", failure="消息注入异常: boom")
            assert domain_seen.wait(2)
            assert frontend.wait(2)
            mgr.stop_check_loop()

        assert domain_events[0][0] == "trigger.fired"
        assert domain_events[0][1]["delivery"] == "failed"
        assert len(frontend.frames) == 1
        event, payload = frontend.frames[0]
        assert event == "trigger_fired"
        assert payload["trigger_id"] == "trigger_schedule_0123456789ab"
        assert payload["name"] == "每日提醒"
        assert payload["pipeline_id"] == "pipe-1"
        assert payload["delivery"] == "failed"
        assert payload["failure"] == "消息注入异常: boom"

    def test_sent_delivery_emits_domain_event_only(self) -> None:
        """sent：域事件照发、WS 零帧（sent 不通知——聊天注入通道已可见）。"""
        domain_events: list[tuple[str, dict[str, Any]]] = []
        domain_seen = threading.Event()

        async def domain_emitter(event: str, tags: dict[str, Any]) -> None:
            domain_events.append((event, tags))
            domain_seen.set()

        frontend = _StubFrontendEmitter()
        with _LoopThread() as lt:
            mgr = TriggerManager()
            mgr.set_main_loop(lt.loop)
            mgr.set_domain_emitter(domain_emitter)
            mgr.set_frontend_emitter(frontend)
            mgr._emit_trigger_fired(_make_config(), "sent")
            assert domain_seen.wait(2)
            mgr.stop_check_loop()

        assert domain_events[0][1]["delivery"] == "sent"
        assert frontend.frames == []

    def test_frontend_emitter_absent_degrades_silently(self) -> None:
        """出口缺席（旧内核降级 None）：异常交付出帧路径静默跳过不炸。"""
        domain_seen = threading.Event()

        async def domain_emitter(event: str, tags: dict[str, Any]) -> None:
            domain_seen.set()

        with _LoopThread() as lt:
            mgr = TriggerManager()
            mgr.set_main_loop(lt.loop)
            mgr.set_domain_emitter(domain_emitter)
            mgr.set_frontend_emitter(None)
            mgr._emit_trigger_fired(_make_config(), "skipped", failure="注入器未设置")
            assert domain_seen.wait(2)
            mgr.stop_check_loop()
