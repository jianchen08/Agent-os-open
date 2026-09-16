"""director 核心：W2 五步段生成链 + 看门狗指标 + 插播队列（细化设计 §1.2/§3）。

端口全部 Protocol 注入（生产绑定见 runtime_ports.py 的真机联调校准点，
测试注入伪件）——本模块零 I/O、零内核依赖。
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from aggregator import AggregateResult, DanmakuAggregator
from cursor import TimelineCursor
from state_machine import LiveState, LiveStateMachine, Trigger


# ── 数据契约 ──────────────────────────────────────────────────────
@dataclass
class SegmentScript:
    """segment JSON 合同的解析形态（细化设计 §3.4）。"""

    transition: str = "cut"
    transition_style: str = ""
    scene: str = ""
    visual_prompts: list[str] = field(default_factory=list)
    narration: str = ""
    subtitle: str = ""
    hook: str = ""
    world_updates: list[dict[str, Any]] = field(default_factory=list)
    realm_updates: dict[str, Any] | None = None
    codex_entry: dict[str, Any] | None = None
    intent_used: str = ""


def parse_segment_script(raw: str | dict[str, Any]) -> SegmentScript:
    """解析剧本 JSON → SegmentScript；结构性缺失抛 ScriptFormatError。"""
    data = json.loads(raw) if isinstance(raw, str) else dict(raw)
    required = ("transition", "scene", "visual_prompts", "hook")
    missing = [k for k in required if k not in data]
    if missing:
        raise ScriptFormatError(f"missing fields: {missing}")
    transition = data["transition"]
    if isinstance(transition, dict):
        transition_type = str(transition.get("type", "cut"))
        style = str(transition.get("style", ""))
    else:
        transition_type, style = str(transition), ""
    prompts = data["visual_prompts"]
    if not isinstance(prompts, list) or not prompts:
        raise ScriptFormatError("visual_prompts must be non-empty list")
    return SegmentScript(
        transition=transition_type,
        transition_style=style,
        scene=str(data["scene"]),
        visual_prompts=[str(p) for p in prompts],
        narration=str(data.get("narration", "")),
        subtitle=str(data.get("subtitle", "")),
        hook=str(data["hook"]),
        world_updates=list(data.get("world_updates", []) or []),
        realm_updates=data.get("realm_updates"),
        codex_entry=data.get("codex_entry"),
        intent_used=str(data.get("intent_used", "")),
    )


class ScriptFormatError(Exception):
    """segment JSON 结构非法（schema 合法性判据的解析层）。"""


# ── 端口 ─────────────────────────────────────────────────────────
class ScriptGenerator(Protocol):
    """剧本生成端口：游标快照 + 衔接裁决 + 意图 → segment JSON（可抛异常）。"""

    async def __call__(
        self, cursor_snapshot: dict[str, Any], transition: str, intent: str,
        representatives: list[str],
    ) -> str | dict[str, Any]: ...


class ContentFilter(Protocol):
    """内容过滤端口：narration/subtitle/visual_prompts 双查。"""

    async def __call__(
        self, narration: str, subtitle: str, visual_prompts: list[str]
    ) -> tuple[bool, str]: ...


class VideoRenderer(Protocol):
    """视频渲染端口（video_gen 工具适配）。"""

    async def __call__(
        self, visual_prompts: list[str], transition: str, start_frame: str | None
    ) -> str: ...


class FrameExtractor(Protocol):
    """末帧抽取端口（ffmpeg / 测试伪件）。"""

    async def __call__(self, video_path: str) -> str: ...


class LibraryIndex(Protocol):
    """内容库索引端口：段元数据落 manifest。"""

    def add(self, meta: dict[str, Any]) -> None: ...


class Evaluator:
    """评估闸门（细化设计 §3.6 判据中可程序化的子集；LLM 承接判定留端口扩展）。"""

    def check(
        self, script: SegmentScript, prev_hook: str, must_cut: bool
    ) -> tuple[bool, str]:
        """schema 之外的硬判据：衔接合法 / 钩子存在 / 强制切服从。"""
        if script.transition not in ("continue", "cut"):
            return False, f"BAD_TRANSITION:{script.transition}"
        if must_cut and script.transition == "continue":
            return False, "MUST_CUT_VIOLATED"
        if not script.hook.strip():
            return False, "HOOK_EMPTY"
        if prev_hook and script.hook.strip() == prev_hook.strip():
            return False, "HOOK_UNCHANGED"
        return True, ""


# ── W2 五步链 ────────────────────────────────────────────────────
@dataclass
class SegmentOutcome:
    """一次段生成的结果（台账与执行面消费）。"""

    accepted: bool
    stage: str = ""  # script|filter|eval|render|persist|""
    reason: str = ""
    video_path: str = ""
    frame_path: str = ""
    seg_no: int = 0
    transition: str = ""


class SegmentPipeline:
    """W2：剧本生成 → 内容过滤 → 评估闸门 → 视频生成 → 抽末帧+落库+游标回写。"""

    def __init__(
        self,
        cursor: TimelineCursor,
        script_gen: ScriptGenerator,
        content_filter: ContentFilter,
        renderer: VideoRenderer,
        extractor: FrameExtractor,
        library: LibraryIndex,
        metrics: DirectorMetrics,
    ) -> None:
        self._cursor = cursor
        self._script_gen = script_gen
        self._filter = content_filter
        self._renderer = renderer
        self._extractor = extractor
        self._library = library
        self._metrics = metrics
        self._evaluator = Evaluator()

    async def run(
        self, intent: str, representatives: list[str], requested_transition: str
    ) -> SegmentOutcome:
        """跑五步；任一步失败即止并计数（游标一致性优先于播出）。"""
        transition = self._cursor.next_transition(requested_transition)

        # ① 剧本生成（LLM 连败计数在此累积/清零——看门狗 M=5 熔断输入）
        try:
            raw = await self._script_gen(
                self._cursor.snapshot_for_prompt(), transition, intent, representatives
            )
            script = parse_segment_script(raw)
        except Exception as exc:  # noqa: BLE001 —— 端口异常统一计入连败
            self._metrics.script_fail_streak += 1
            return SegmentOutcome(False, stage="script", reason=str(exc))
        self._metrics.script_fail_streak = 0

        # ② 内容过滤（双查）
        allowed, reason = await self._filter(
            script.narration, script.subtitle, script.visual_prompts
        )
        if not allowed:
            self._metrics.filtered_recent.append(1)
            return SegmentOutcome(False, stage="filter", reason=reason)
        self._metrics.filtered_recent.append(0)

        # ③ 评估闸门
        ok, reason = self._evaluator.check(
            script, str(self._cursor.data["last_hook"]), self._cursor.must_cut()
        )
        if not ok:
            self._metrics.eval_rejected += 1
            return SegmentOutcome(False, stage="eval", reason=reason)

        # ④ 视频生成
        start_frame = (
            str(self._cursor.data["last_frame"]) if transition == "continue" else None
        )
        try:
            video_path = await self._renderer(
                script.visual_prompts, transition, start_frame
            )
        except Exception as exc:  # noqa: BLE001
            self._metrics.render_fail_streak += 1
            return SegmentOutcome(False, stage="render", reason=str(exc))
        self._metrics.render_fail_streak = 0

        # ⑤ 抽末帧 + 落库 + 游标原子回写
        frame_path = await self._extractor(video_path)
        realm = str(
            (script.realm_updates or {}).get("to", self._cursor.data["realm"])
        )
        data = await self._cursor.advance(
            transition_type=transition,
            video_path=video_path,
            hook=script.hook,
            scene_brief=script.scene,
            present=[str(u) for u in (
                (script.world_updates[0].get("value", "").split("|"))
                if script.world_updates and isinstance(script.world_updates[0], dict)
                else []
            )],
            realm=realm,
            codex_entry=script.codex_entry,
            timestamp=f"ep{self._cursor.data['episode_no']}/seg{int(self._cursor.data['seg_no']) + 1}",
            extractor=self._extractor,
        )
        self._metrics.segments_ok += 1
        seg_no = int(data["seg_no"])
        self._library.add({
            "seg_no": seg_no,
            "video_path": video_path,
            "frame_path": frame_path,
            "transition": transition,
            "scene": script.scene,
            "hook": script.hook,
            "intent": script.intent_used,
        })
        return SegmentOutcome(
            True, video_path=video_path, frame_path=frame_path,
            seg_no=seg_no, transition=transition,
        )


# ── 看门狗指标（触发阈值判据：LLM 连败 M=5 / 渲染连败 / 拦截率突增）──
@dataclass
class DirectorMetrics:
    """台账指标 + 看门狗判据输入。"""

    script_fail_streak: int = 0
    render_fail_streak: int = 0
    eval_rejected: int = 0
    segments_ok: int = 0
    filtered_recent: deque[int] = field(default_factory=lambda: deque(maxlen=20))
    llm_fail_threshold: int = 5
    render_fail_threshold: int = 3
    filter_rate_threshold: float = 0.5

    def watchdog_reasons(self) -> list[str]:
        """当前越阈判据清单（非空 = WATCHDOG_TRIP 输入）。"""
        reasons: list[str] = []
        if self.script_fail_streak >= self.llm_fail_threshold:
            reasons.append(f"llm_consecutive_{self.script_fail_streak}")
        if self.render_fail_streak >= self.render_fail_threshold:
            reasons.append(f"render_consecutive_{self.render_fail_streak}")
        if len(self.filtered_recent) == self.filtered_recent.maxlen:
            rate = sum(self.filtered_recent) / len(self.filtered_recent)
            if rate >= self.filter_rate_threshold:
                reasons.append(f"filter_rate_{rate:.2f}")
        return reasons


# ── Director 门面（执行面/服务面操作的核心对象）────────────────────
class DirectorCore:
    """状态机 + 聚合窗 + 段管道 + 插播队列的组合入口。"""

    def __init__(
        self,
        sm: LiveStateMachine,
        aggregator: DanmakuAggregator,
        cursor: TimelineCursor,
        pipeline: SegmentPipeline,
        metrics: DirectorMetrics | None = None,
        data_dir: str = "data/livestream",
    ) -> None:
        self.sm = sm
        self.aggregator = aggregator
        self.cursor = cursor
        self.pipeline = pipeline
        self.metrics = metrics or DirectorMetrics()
        self._data_dir = Path(data_dir)
        self.insert_queue: deque[dict[str, Any]] = deque()
        self.pending_intent: str = ""
        self.pending_representatives: list[str] = []
        self.inflight = False

    def feed_danmaku(self, now: float, user: str, text: str) -> None:
        """弹幕入窗（channel_bilibili 轮询/直喂入口）。"""
        self.aggregator.feed(now, user, text)

    async def flush_aggregation(self, now: float) -> AggregateResult:
        """窗口冲刷；有效意图挂起等待 IDLE 执行。"""
        result = await self.aggregator.flush(now)
        if result.valid:
            self.pending_intent = result.intent
            self.pending_representatives = result.representatives
        return result

    async def run_pending_segment(self, requested_transition: str = "continue") -> SegmentOutcome | None:
        """IDLE 执行面调用：跑一次 W2；成片入插播队列。"""
        if not self.pending_intent:
            return None
        intent, reps = self.pending_intent, self.pending_representatives
        self.pending_intent, self.pending_representatives = "", []
        self.inflight = True
        try:
            outcome = await self.pipeline.run(intent, reps, requested_transition)
        finally:
            self.inflight = False
        if outcome.accepted:
            self.insert_queue.append({
                "video_path": outcome.video_path,
                "seg_no": outcome.seg_no,
                "transition": outcome.transition,
            })
        return outcome

    def tick(
        self,
        now: float,
        *,
        valid_intent: bool | None = None,
        queue_empty: bool | None = None,
        nobody_secs: float = 0.0,
        schedule_end: bool = False,
    ) -> tuple[bool, LiveState, tuple[str, ...]]:
        """执行面节拍：由观测推导触发器并驱动状态机；返回动作串。

        看门狗越阈优先于一切正常迁移（任意活动态 → FAULT）。
        """
        reasons = self.metrics.watchdog_reasons()
        if reasons and self.sm.state in (
            LiveState.BOOTING, LiveState.IDLE, LiveState.LIVE, LiveState.STANDBY
        ):
            return self.sm.transition(
                Trigger.WATCHDOG_TRIP,
                ctx={"has_fault_context": True, "fault_detail": reasons[0]},
                now=now,
            )
        if self.sm.state is LiveState.IDLE and self.pending_intent and not self.inflight:
            return self.sm.transition(
                Trigger.VALID_INTENT,
                ctx={"valid_intent": True, "inflight": False},
                now=now,
            )
        if self.sm.state is LiveState.LIVE and queue_empty:
            return self.sm.transition(
                Trigger.SILENCE_30S, ctx={"queue_empty": True}, now=now
            )
        if self.sm.state is LiveState.IDLE and nobody_secs >= 900:
            return self.sm.transition(Trigger.NOBODY_15M, now=now)
        if self.sm.state is LiveState.STANDBY:
            if nobody_secs >= 3600:
                return self.sm.transition(Trigger.NOBODY_60M, now=now)
            if nobody_secs == 0.0:
                return self.sm.transition(Trigger.SOMEBODY, now=now)
        if self.sm.state in (LiveState.IDLE, LiveState.STANDBY) and schedule_end:
            return self.sm.transition(Trigger.SCHEDULE_END, now=now)
        return False, self.sm.state, ()

    def status(self) -> dict[str, Any]:
        """面板/巡检快照（director.get_status 服务契约）。"""
        snap = self.sm.snapshot()
        return {
            "state": snap.state.value,
            "entered_at": snap.entered_at,
            "fault_context": snap.fault_context,
            "pending_intent": self.pending_intent,
            "inflight": self.inflight,
            "insert_queue_len": len(self.insert_queue),
            "insert_queue": list(self.insert_queue),
            "metrics": {
                "script_fail_streak": self.metrics.script_fail_streak,
                "render_fail_streak": self.metrics.render_fail_streak,
                "eval_rejected": self.metrics.eval_rejected,
                "segments_ok": self.metrics.segments_ok,
                "watchdog_reasons": self.metrics.watchdog_reasons(),
            },
            "aggregator": self.aggregator.status(),
            "cursor": {
                "seg_no": self.cursor.data["seg_no"],
                "realm": self.cursor.data["realm"],
                "codex_count": self.cursor.data["codex_count"],
                "consecutive_continue": self.cursor.consecutive_continue,
            },
        }
