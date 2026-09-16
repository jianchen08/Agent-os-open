# @feature: FP-0.2.二 导演逻辑 | @ci: python-coverage
"""director 纯逻辑三件套测试：状态机 / 时间线游标 / 弹幕聚合窗。

状态机：迁移表逐条守卫验证（≥2 组有区分度输入；非法迁移原态保持）；
游标：原子写/损坏回退/continue 计数与强制切/图鉴去重/prompt 快照；
聚合窗：独立用户门槛/节流/加权/ summarizer 端口与回落。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_PLUGIN_DIR = Path(__file__).parent
sys.path.insert(0, str(_PLUGIN_DIR))


def _load(filename: str, mod_name: str) -> Any:
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_sm = _load("state_machine.py", "director_state_machine")
_cu = _load("cursor.py", "director_cursor")
_ag = _load("aggregator.py", "director_aggregator")

LiveState = _sm.LiveState
Trigger = _sm.Trigger
LiveStateMachine = _sm.LiveStateMachine
TimelineCursor = _cu.TimelineCursor
DanmakuAggregator = _ag.DanmakuAggregator


# ══ 状态机 ═══════════════════════════════════════════════════════
def test_boot_path_offline_to_idle() -> None:
    sm = LiveStateMachine(now=0.0)
    ok, dst, actions = sm.transition(Trigger.BOOT, now=1.0)
    assert ok and dst is LiveState.BOOTING
    assert "bilibili_open" in actions and "gpu_on" in actions
    ok, dst, _ = sm.transition(Trigger.BOOT_READY, now=2.0)
    assert ok and dst is LiveState.IDLE


def test_boot_fail_goes_fault_with_actions() -> None:
    sm = LiveStateMachine(now=0.0)
    sm.transition(Trigger.BOOT, now=1.0)
    ok, dst, actions = sm.transition(Trigger.BOOT_FAIL, now=2.0)
    assert ok and dst is LiveState.FAULT
    assert "notify" in actions
    assert sm.fault_context != ""


def test_idle_to_live_requires_no_inflight() -> None:
    """同触发双规则分叉：inflight 时落入"意图丢弃"自环（IDLE，空动作）；
    空闲时进 LIVE 开段。"""
    sm = LiveStateMachine(now=0.0)
    sm.transition(Trigger.BOOT, now=0.1)
    sm.transition(Trigger.BOOT_READY, now=0.2)
    ok, dst, actions = sm.transition(
        Trigger.VALID_INTENT, ctx={"valid_intent": True, "inflight": True}, now=1.0
    )
    assert ok and dst is LiveState.IDLE and actions == ()  # 丢弃自环
    ok, dst, actions = sm.transition(
        Trigger.VALID_INTENT, ctx={"valid_intent": True, "inflight": False}, now=2.0
    )
    assert ok and dst is LiveState.LIVE
    assert actions == ("start_segment",)


def test_live_silence_requires_empty_queue() -> None:
    sm = _booted_live()
    ok, dst, _ = sm.transition(Trigger.SILENCE_30S, ctx={"queue_empty": False}, now=3.0)
    assert not ok and dst is LiveState.LIVE
    ok, dst, _ = sm.transition(Trigger.SILENCE_30S, ctx={"queue_empty": True}, now=3.0)
    assert ok and dst is LiveState.IDLE


def test_nobody_ladder_standby_then_closing() -> None:
    """D2：15min 待机、60min 自动下播（两条时间阈值分开触发）。"""
    sm = _booted_idle()
    ok, dst, actions = sm.transition(Trigger.NOBODY_15M, now=10.0)
    assert ok and dst is LiveState.STANDBY
    assert "stream_standby" in actions
    ok, dst, _ = sm.transition(Trigger.NOBODY_60M, now=20.0)
    assert ok and dst is LiveState.CLOSING
    ok, dst, actions = sm.transition(Trigger.SCHEDULE_END, now=30.0)
    assert not ok and dst is LiveState.CLOSING  # CLOSING 无后续规则


def _booted_idle() -> Any:
    sm = LiveStateMachine(now=0.0)
    sm.transition(Trigger.BOOT, now=0.1)
    sm.transition(Trigger.BOOT_READY, now=0.2)
    return sm


def _booted_live() -> Any:
    sm = _booted_idle()
    sm.transition(Trigger.VALID_INTENT, ctx={"valid_intent": True, "inflight": False}, now=1.0)
    return sm


def test_watchdog_trip_from_live_keeps_stream_standby() -> None:
    """链路故障类：切待机保播 + 通知（D3 内容事故类由执行面升级停播）。"""
    sm = _booted_live()
    ok, dst, actions = sm.transition(
        Trigger.WATCHDOG_TRIP,
        ctx={"has_fault_context": True, "fault_detail": "llm_consecutive_5"},
        now=5.0,
    )
    assert ok and dst is LiveState.FAULT
    assert "stream_standby" in actions and "notify" in actions
    # 自愈恢复回 IDLE 并清故障上下文
    ok, dst, _ = sm.transition(
        Trigger.RECOVER, ctx={"fault_auto_healed": True}, now=6.0
    )
    assert ok and dst is LiveState.IDLE
    assert sm.fault_context == ""


def test_invalid_transition_from_offline_is_rejected() -> None:
    sm = LiveStateMachine(now=0.0)
    ok, dst, _ = sm.transition(Trigger.NOBODY_15M, now=1.0)
    assert not ok and dst is LiveState.OFFLINE
    assert sm.rejected[-1] == (Trigger.NOBODY_15M, "no_rule")


def test_snapshot_roundtrip() -> None:
    sm = _booted_live()
    snap = sm.snapshot()
    assert snap.state is LiveState.LIVE
    assert snap.entered_at == 1.0


# ══ 时间线游标 ═══════════════════════════════════════════════════
class FakeExtractor:
    """伪末帧抽取：产出 <video>.last.jpg 并计数。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, video_path: str) -> str:
        self.calls.append(video_path)
        out = video_path + ".last.jpg"
        Path(out).write_bytes(b"jpeg")
        return out


def _advance(cursor: Any, extractor: FakeExtractor, **kw: Any) -> Any:
    base = dict(
        transition_type="continue",
        video_path=str(kw.pop("video_path", _PLUGIN_DIR / "fake.mp4")),
        hook="钩子A",
        scene_brief="场景A",
        present=["漂流者"],
        realm="梗之气三段",
        codex_entry=None,
        timestamp="t1",
        extractor=extractor,
    )
    base.update(kw)
    return asyncio.run(cursor.advance(**base))


def test_cursor_advance_continue_increments_and_cut_resets(tmp_path: Path) -> None:
    cursor = TimelineCursor(str(tmp_path / "cursor.json"))
    video = tmp_path / "seg1.mp4"
    video.write_bytes(b"v")
    ex = FakeExtractor()
    _advance(cursor, ex, video_path=str(video), transition_type="continue")
    _advance(cursor, ex, video_path=str(video), transition_type="continue")
    assert cursor.consecutive_continue == 2
    assert cursor.data["seg_no"] == 2
    _advance(cursor, ex, video_path=str(video), transition_type="cut")
    assert cursor.consecutive_continue == 0
    assert ex.calls[-1] == str(video)
    # 落盘可回读
    reloaded = TimelineCursor(str(tmp_path / "cursor.json"))
    assert reloaded.data["seg_no"] == 3
    assert reloaded.data["last_hook"] == "钩子A"


def test_cursor_must_cut_at_threshold_and_next_transition_forces_cut(tmp_path: Path) -> None:
    cursor = TimelineCursor(str(tmp_path / "cursor.json"), max_consecutive_continue=2)
    video = tmp_path / "seg.mp4"
    video.write_bytes(b"v")
    ex = FakeExtractor()
    for _ in range(2):
        _advance(cursor, ex, video_path=str(video), transition_type="continue")
    assert cursor.must_cut()
    assert cursor.next_transition("continue") == "cut"
    assert cursor.next_transition("cut") == "cut"


def test_cursor_next_transition_cut_when_no_frame(tmp_path: Path) -> None:
    cursor = TimelineCursor(str(tmp_path / "cursor.json"))
    assert cursor.next_transition("continue") == "cut"


def test_cursor_codex_dedup_and_recent_window(tmp_path: Path) -> None:
    cursor = TimelineCursor(str(tmp_path / "cursor.json"))
    video = tmp_path / "seg.mp4"
    video.write_bytes(b"v")
    ex = FakeExtractor()
    _advance(cursor, ex, video_path=str(video), codex_entry={"meme": "酱板鸭"})
    _advance(cursor, ex, video_path=str(video), codex_entry={"meme": "酱板鸭"})
    _advance(cursor, ex, video_path=str(video), codex_entry={"meme": "言灵瓜摊"})
    assert cursor.data["codex_count"] == 2
    assert cursor.data["codex_recent"] == ["酱板鸭", "言灵瓜摊"]


def test_cursor_corrupt_file_falls_back_to_fresh(tmp_path: Path) -> None:
    p = tmp_path / "cursor.json"
    p.write_text("{not json", encoding="utf-8")
    cursor = TimelineCursor(str(p))
    assert cursor.data["seg_no"] == 0  # 损坏文件按全新游标起步，不抛异常


def test_cursor_prompt_snapshot_carries_must_cut(tmp_path: Path) -> None:
    cursor = TimelineCursor(str(tmp_path / "cursor.json"), max_consecutive_continue=1)
    video = tmp_path / "seg.mp4"
    video.write_bytes(b"v")
    _advance(cursor, FakeExtractor(), video_path=str(video), transition_type="continue")
    snap = cursor.snapshot_for_prompt()
    assert snap["must_cut"] is True


# ══ 弹幕聚合窗 ═══════════════════════════════════════════════════
def test_aggregator_requires_distinct_users() -> None:
    agg = DanmakuAggregator(min_distinct_users=3)
    agg.feed(0.0, "u1", "酱板鸭")
    agg.feed(1.0, "u2", "酱板鸭")
    result = asyncio.run(agg.flush(2.0))
    assert not result.valid


def test_aggregator_throttles_same_user_within_interval() -> None:
    agg = DanmakuAggregator(min_distinct_users=2)
    agg.feed(0.0, "u1", "甲")
    agg.feed(1.0, "u1", "乙")  # 同用户 60s 内第二条 → 节流丢弃
    agg.feed(2.0, "u2", "甲")
    result = asyncio.run(agg.flush(3.0))
    assert result.valid
    assert result.dropped_by_throttle == 1
    assert result.intent == "甲"


def test_aggregator_window_expiry() -> None:
    agg = DanmakuAggregator(window_secs=25, min_distinct_users=2)
    agg.feed(0.0, "u1", "甲")
    agg.feed(100.0, "u2", "乙")  # 甲已出窗
    result = asyncio.run(agg.flush(101.0))
    assert not result.valid


async def _fake_summarizer(danmaku: list[str]) -> str:
    return "全体要求：主角变猫"


def test_aggregator_uses_summarizer_port() -> None:
    agg = DanmakuAggregator(min_distinct_users=2, summarizer=_fake_summarizer)
    agg.feed(0.0, "u1", "变猫")
    agg.feed(1.0, "u2", "快变猫")
    result = asyncio.run(agg.flush(2.0))
    assert result.valid
    assert result.intent == "全体要求：主角变猫"
    assert result.representatives == ["变猫", "快变猫"]
