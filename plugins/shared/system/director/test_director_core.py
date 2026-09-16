# @feature: FP-0.2.二 DirectorCore 挂起意图/插播队列/tick | @ci: python-coverage
"""director 核心测试：W2 五步链 / 看门狗 / 插播队列 / tick 推导 / status。"""

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


_sm = _load("state_machine.py", "state_machine")  # 真实导入名注册：core 裸名 import 命中同一模块
_cu = _load("cursor.py", "cursor")
_ag = _load("aggregator.py", "aggregator")
_core = _load("core.py", "director_core")

Trigger = _sm.Trigger
LiveStateMachine = _sm.LiveStateMachine
LiveState = _sm.LiveState
TimelineCursor = _cu.TimelineCursor
DanmakuAggregator = _ag.DanmakuAggregator
DirectorCore = _core.DirectorCore
DirectorMetrics = _core.DirectorMetrics
SegmentPipeline = _core.SegmentPipeline
parse_segment_script = _core.parse_segment_script
ScriptFormatError = _core.ScriptFormatError


def _script_json(transition: str = "cut", hook: str = "新钩子") -> str:
    import json

    return json.dumps({
        "transition": {"type": transition, "style": "硬切" if transition == "cut" else "无缝直续"},
        "scene": "场景描述",
        "shots": [{"sec": 5, "visual_prompt": "a cat, cinematic"}],
        "visual_prompts": ["a cat, cinematic"],
        "narration": "旁白",
        "subtitle": "字幕",
        "hook": hook,
        "world_updates": [{"path": "present", "value": "漂流者"}],
        "codex_entry": {"meme": "梗A", "type": "来客", "power": "梗力"},
        "intent_used": "变猫",
    })


class FakeScriptGen:
    def __init__(self, scripts: list[Any]) -> None:
        self.scripts = list(scripts)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, cursor_snapshot: dict, transition: str, intent: str, representatives: list[str]) -> str:
        self.calls.append({"snapshot": cursor_snapshot, "transition": transition, "intent": intent})
        item = self.scripts.pop(0)
        if isinstance(item, Exception):
            raise item
        return item if isinstance(item, str) else str(item)


class FakeFilter:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed
        self.calls = 0

    async def __call__(self, narration: str, subtitle: str, prompts: list[str]) -> tuple[bool, str]:
        self.calls += 1
        return self.allowed, "" if self.allowed else "HIT:敏感词"


class FakeRenderer:
    def __init__(self, fail_times: int = 0) -> None:
        self.fail_times = fail_times
        self.calls: list[tuple[list[str], str, str | None]] = []

    async def __call__(self, prompts: list[str], transition: str, start_frame: str | None) -> str:
        self.calls.append((prompts, transition, start_frame))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("comfy down")
        out = f"seg_{len(self.calls)}.mp4"
        Path(out).write_bytes(b"mp4")
        return out


class FakeExtractor:
    def __init__(self, fail_times: int = 0) -> None:
        self.fail_times = fail_times

    async def __call__(self, video_path: str) -> str:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("no frame")
        out = video_path + ".last.jpg"
        Path(out).write_bytes(b"jpeg")
        return out


class FakeLibrary:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def add(self, meta: dict[str, Any]) -> None:
        self.items.append(meta)


def _make_core(
    scripts: list[Any] | None = None,
    filter_allowed: bool = True,
    renderer: FakeRenderer | None = None,
    tmp: Path | None = None,
    max_continue: int = 5,
) -> tuple[Any, FakeScriptGen, FakeFilter, FakeRenderer, FakeLibrary]:
    tmp = tmp or Path(".")
    (tmp / "frames").mkdir(exist_ok=True)
    cursor = TimelineCursor(str(tmp / "cursor.json"), max_consecutive_continue=max_continue)
    sg = FakeScriptGen(scripts if scripts is not None else [_script_json()])
    fl = FakeFilter(filter_allowed)
    rd = renderer or FakeRenderer()
    ex = FakeExtractor()
    lib = FakeLibrary()
    metrics = DirectorMetrics()
    pipeline = SegmentPipeline(cursor, sg, fl, rd, ex, lib, metrics)
    core = DirectorCore(
        LiveStateMachine(now=0.0),
        DanmakuAggregator(min_distinct_users=2),
        cursor, pipeline, metrics, data_dir=str(tmp),
    )
    return core, sg, fl, rd, lib


# ══ W2 五步链 ════════════════════════════════════════════════════
def test_pipeline_happy_path_continue_uses_cursor_frame(tmp_path: Path) -> None:
    """直续：渲染收到上段末帧；游标/图鉴/台账同步推进；成片入插播队列。"""
    core, sg, _fl, rd, lib = _make_core(
        [_script_json("continue"), _script_json("cut", hook="下一个钩子")], tmp=tmp_path
    )
    seed_frame = tmp_path / "seed.jpg"
    seed_frame.write_bytes(b"jpeg")
    # 预置末帧（模拟上一段已落游标）
    core.cursor._data["last_frame"] = str(seed_frame)  # noqa: SLF001 测试预置

    outcome = asyncio.run(core.pipeline.run("变猫", ["弹幕1"], "continue"))

    assert outcome.accepted, outcome.reason
    assert outcome.transition == "continue"
    assert rd.calls[0][2] == str(seed_frame)
    assert lib.items[0]["seg_no"] == 1
    assert core.metrics.segments_ok == 1
    assert core.cursor.consecutive_continue == 1
    assert core.cursor.data["codex_recent"] == ["梗A"]
    # 通过 core 跑则入队
    core.pending_intent = "变猫"
    outcome2 = asyncio.run(core.run_pending_segment("cut"))
    assert outcome2 is not None and outcome2.accepted
    assert core.insert_queue[0]["seg_no"] == outcome2.seg_no


def test_pipeline_script_failure_counts_streak(tmp_path: Path) -> None:
    """剧本端口异常 → stage=script 且连败 +1；成功后清零（看门狗输入）。"""
    core, sg, _fl, _rd, _lib = _make_core(
        [RuntimeError("llm down"), _script_json()], tmp=tmp_path
    )
    o1 = asyncio.run(core.pipeline.run("梗", [], "cut"))
    assert not o1.accepted and o1.stage == "script"
    assert core.metrics.script_fail_streak == 1
    o2 = asyncio.run(core.pipeline.run("梗", [], "cut"))
    assert o2.accepted
    assert core.metrics.script_fail_streak == 0


def test_pipeline_llm_streak_five_trips_watchdog_tick(tmp_path: Path) -> None:
    """M=5 连败 → tick 优先推导 WATCHDOG_TRIP（任意活动态 → FAULT）。"""
    scripts = [RuntimeError("x") for _ in range(5)]
    core, _sg, _fl, _rd, _lib = _make_core(scripts, tmp=tmp_path)
    core.sm.transition(Trigger.BOOT, now=0.1)
    core.sm.transition(Trigger.BOOT_READY, now=0.2)
    for _ in range(5):
        asyncio.run(core.pipeline.run("梗", [], "cut"))
    assert core.metrics.script_fail_streak == 5
    ok, dst, actions = core.tick(now=10.0)
    assert ok and dst is LiveState.FAULT
    assert "notify" in actions


def test_pipeline_filter_reject_counts_rate(tmp_path: Path) -> None:
    core, _sg, fl, _rd, _lib = _make_core(
        [_script_json() for _ in range(21)], filter_allowed=False, tmp=tmp_path
    )
    for _ in range(20):
        outcome = asyncio.run(core.pipeline.run("梗", [], "cut"))
        assert outcome.stage == "filter"
    assert fl.calls == 20
    reasons = core.metrics.watchdog_reasons()
    assert any(r.startswith("filter_rate_") for r in reasons)


def test_pipeline_eval_rejects_must_cut_violation(tmp_path: Path) -> None:
    """游标 must_cut 但剧本仍回 continue → 评估闸门拒绝。"""
    core, sg, _fl, _rd, _lib = _make_core(
        [_script_json("continue")], tmp=tmp_path, max_continue=1
    )
    # 预置连续直续已到上限
    core.cursor._data["consecutive_continue"] = 1  # noqa: SLF001
    core.cursor._data["last_frame"] = "nonexist.jpg"  # noqa: SLF001
    core.cursor._data["last_hook"] = "旧钩子"  # noqa: SLF001

    outcome = asyncio.run(core.pipeline.run("梗", [], "continue"))

    assert not outcome.accepted and outcome.stage == "eval"
    assert outcome.reason == "MUST_CUT_VIOLATED"
    assert core.metrics.eval_rejected == 1


def test_pipeline_eval_rejects_unchanged_hook(tmp_path: Path) -> None:
    core, _sg, _fl, _rd, _lib = _make_core([_script_json(hook="旧钩子")], tmp=tmp_path)
    core.cursor._data["last_hook"] = "旧钩子"  # noqa: SLF001
    outcome = asyncio.run(core.pipeline.run("梗", [], "cut"))
    assert not outcome.accepted and outcome.stage == "eval"
    assert outcome.reason == "HOOK_UNCHANGED"


def test_pipeline_render_failure_stage(tmp_path: Path) -> None:
    core, _sg, _fl, _rd, _lib = _make_core(
        [_script_json()], renderer=FakeRenderer(fail_times=1), tmp=tmp_path
    )
    outcome = asyncio.run(core.pipeline.run("梗", [], "cut"))
    assert not outcome.accepted and outcome.stage == "render"
    assert core.metrics.render_fail_streak == 1


def test_cursor_arbitration_forces_cut_in_script_call(tmp_path: Path) -> None:
    """无末帧时请求 continue → 仲裁为 cut 传给剧本端口。"""
    core, sg, _fl, _rd, _lib = _make_core([_script_json()], tmp=tmp_path)
    asyncio.run(core.pipeline.run("梗", [], "continue"))
    assert sg.calls[0]["transition"] == "cut"


def test_parse_segment_script_rejects_missing_fields() -> None:
    with pytest.raises(ScriptFormatError):
        parse_segment_script({"scene": "x", "hook": "y"})


# ══ 聚合→挂起→tick 推导 ══════════════════════════════════════════
def test_tick_pending_intent_drives_idle_to_live(tmp_path: Path) -> None:
    core, _sg, _fl, _rd, _lib = _make_core([_script_json()], tmp=tmp_path)
    core.sm.transition(Trigger.BOOT, now=0.1)
    core.sm.transition(Trigger.BOOT_READY, now=0.2)
    core.feed_danmaku(1.0, "u1", "变猫")
    core.feed_danmaku(2.0, "u2", "变猫")
    result = asyncio.run(core.flush_aggregation(3.0))
    assert result.valid
    ok, dst, actions = core.tick(now=4.0)
    assert ok and dst is LiveState.LIVE
    assert actions == ("start_segment",)


def test_tick_nobody_ladder(tmp_path: Path) -> None:
    core, _sg, _fl, _rd, _lib = _make_core(tmp=tmp_path)
    core.sm.transition(Trigger.BOOT, now=0.1)
    core.sm.transition(Trigger.BOOT_READY, now=0.2)
    ok, dst, _ = core.tick(now=1.0, nobody_secs=901)
    assert ok and dst is LiveState.STANDBY
    ok, dst, _ = core.tick(now=2.0, nobody_secs=3601)
    assert ok and dst is LiveState.CLOSING


def test_status_shape(tmp_path: Path) -> None:
    core, _sg, _fl, _rd, _lib = _make_core(tmp=tmp_path)
    status = core.status()
    assert status["state"] == "OFFLINE"
    assert "metrics" in status and "cursor" in status and "aggregator" in status
