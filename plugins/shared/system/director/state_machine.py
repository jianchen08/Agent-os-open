"""直播状态机（细化设计 §2.1/§2.2，D2/D3/D5）。

七态：OFFLINE / BOOTING / IDLE / LIVE / STANDBY / CLOSING / FAULT。
数据驱动迁移表；守卫纯函数（观测值进、布尔出）；动作以事件记录返回，
副作用（推流/GPU/通知）由执行面完成——状态机本体零 I/O、零时钟依赖
（时间比较用注入的观测值），可穷举测试。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class LiveState(str, Enum):
    """直播状态集（细化设计 §2.1）。"""

    OFFLINE = "OFFLINE"
    BOOTING = "BOOTING"
    IDLE = "IDLE"
    LIVE = "LIVE"
    STANDBY = "STANDBY"
    CLOSING = "CLOSING"
    FAULT = "FAULT"


class Trigger(str, Enum):
    """迁移触发器（执行面从观测/事件推导后投喂）。"""

    BOOT = "BOOT"
    BOOT_READY = "BOOT_READY"
    BOOT_FAIL = "BOOT_FAIL"
    VALID_INTENT = "VALID_INTENT"
    SILENCE_30S = "SILENCE_30S"
    NOBODY_15M = "NOBODY_15M"
    SOMEBODY = "SOMEBODY"
    NOBODY_60M = "NOBODY_60M"
    SCHEDULE_END = "SCHEDULE_END"
    MANUAL_STOP = "MANUAL_STOP"
    WATCHDOG_TRIP = "WATCHDOG_TRIP"
    RECOVER = "RECOVER"


@dataclass(frozen=True)
class Transition:
    """单条迁移规则：源态 + 触发器 + 守卫 → 目标态。"""

    src: LiveState
    trigger: Trigger
    guard: str  # 守卫名， TRANSITION_TABLE 内引用 guard 函数
    dst: LiveState
    actions: tuple[str, ...] = ()


# ── 守卫（纯函数：观测上下文 → 布尔）───────────────────────────────
# ctx 键契约（执行面填充，缺键按 False fail-closed）：
#   valid_intent / inflight / queue_empty / has_fault_context /
#   fault_auto_healed / self_healing_allowed


def _g_not_inflight(ctx: dict[str, Any]) -> bool:
    return bool(ctx.get("valid_intent")) and not ctx.get("inflight", False)


def _g_queue_empty(ctx: dict[str, Any]) -> bool:
    return bool(ctx.get("queue_empty", False))


def _g_has_fault_context(ctx: dict[str, Any]) -> bool:
    return bool(ctx.get("has_fault_context", False))


def _g_auto_healed(ctx: dict[str, Any]) -> bool:
    return bool(ctx.get("fault_auto_healed", False))


_GUARDS = {
    "not_inflight": _g_not_inflight,
    "always": lambda _ctx: True,
    "queue_empty": _g_queue_empty,
    "has_fault_context": _g_has_fault_context,
    "auto_healed": _g_auto_healed,
}


# ── 迁移表（细化设计 §2.2 逐条对应；动作串由执行面解释）──────────────
TRANSITION_TABLE: tuple[Transition, ...] = (
    Transition(LiveState.OFFLINE, Trigger.BOOT, "always", LiveState.BOOTING,
               ("bilibili_open", "stream_start_standby", "gpu_on", "danmaku_connect")),
    Transition(LiveState.BOOTING, Trigger.BOOT_READY, "always", LiveState.IDLE, ("log_opened",)),
    Transition(LiveState.BOOTING, Trigger.BOOT_FAIL, "always", LiveState.FAULT,
               ("notify", "stream_stop")),
    Transition(LiveState.IDLE, Trigger.VALID_INTENT, "not_inflight", LiveState.LIVE,
               ("start_segment",)),
    Transition(LiveState.LIVE, Trigger.VALID_INTENT, "not_inflight", LiveState.LIVE,
               ("start_segment",)),
    Transition(LiveState.LIVE, Trigger.SILENCE_30S, "queue_empty", LiveState.IDLE,
               ("drain_queue",)),
    Transition(LiveState.IDLE, Trigger.VALID_INTENT, "always", LiveState.IDLE,
               ()),  # 在飞被守卫拦截时意图丢弃并计数（执行面）
    Transition(LiveState.IDLE, Trigger.NOBODY_15M, "always", LiveState.STANDBY,
               ("stream_standby",)),
    Transition(LiveState.STANDBY, Trigger.SOMEBODY, "always", LiveState.IDLE,
               ("stream_resume_playlist",)),
    Transition(LiveState.STANDBY, Trigger.NOBODY_60M, "always", LiveState.CLOSING,
               ("save_cursor", "stream_stop", "bilibili_close", "gpu_off")),
    Transition(LiveState.IDLE, Trigger.NOBODY_60M, "always", LiveState.CLOSING,
               ("save_cursor", "stream_stop", "bilibili_close", "gpu_off")),
    Transition(LiveState.IDLE, Trigger.SCHEDULE_END, "always", LiveState.CLOSING,
               ("save_cursor", "stream_stop", "bilibili_close", "gpu_off")),
    Transition(LiveState.STANDBY, Trigger.SCHEDULE_END, "always", LiveState.CLOSING,
               ("save_cursor", "stream_stop", "bilibili_close", "gpu_off")),
    Transition(LiveState.LIVE, Trigger.MANUAL_STOP, "always", LiveState.CLOSING,
               ("save_cursor", "stream_stop", "bilibili_close", "gpu_off")),
    Transition(LiveState.IDLE, Trigger.MANUAL_STOP, "always", LiveState.CLOSING,
               ("save_cursor", "stream_stop", "bilibili_close", "gpu_off")),
    Transition(LiveState.STANDBY, Trigger.MANUAL_STOP, "always", LiveState.CLOSING,
               ("save_cursor", "stream_stop", "bilibili_close", "gpu_off")),
    Transition(LiveState.BOOTING, Trigger.WATCHDOG_TRIP, "has_fault_context", LiveState.FAULT,
               ("notify",)),
    Transition(LiveState.IDLE, Trigger.WATCHDOG_TRIP, "has_fault_context", LiveState.FAULT,
               ("notify", "stream_standby")),
    Transition(LiveState.LIVE, Trigger.WATCHDOG_TRIP, "has_fault_context", LiveState.FAULT,
               ("notify", "stream_standby")),
    Transition(LiveState.STANDBY, Trigger.WATCHDOG_TRIP, "has_fault_context", LiveState.FAULT,
               ("notify",)),
    Transition(LiveState.FAULT, Trigger.RECOVER, "has_fault_context", LiveState.IDLE,
               ("log_recovered",)),
    Transition(LiveState.FAULT, Trigger.RECOVER, "auto_healed", LiveState.IDLE,
               ("log_recovered",)),
)


@dataclass
class StateMachineSnapshot:
    """状态机快照（落库/前端面板契约）。"""

    state: LiveState
    entered_at: float
    fault_context: str = ""
    consecutive_continue: int = 0
    extra: dict[str, object] = field(default_factory=dict)


class LiveStateMachine:
    """七态直播状态机：transition() 应用迁移表，非法迁移原态返回并计数。"""

    def __init__(self, now: float) -> None:
        self.state = LiveState.OFFLINE
        self.entered_at = now
        self.fault_context = ""
        self.rejected: list[tuple[Trigger, str]] = []

    def transition(
        self, trigger: Trigger, ctx: dict[str, Any] | None = None, now: float = 0.0
    ) -> tuple[bool, LiveState, tuple[str, ...]]:
        """尝试迁移。返回 (是否迁移, 目标态, 动作串)。

        同 (src, trigger) 存在多条规则时按表序扫描：守卫失败继续看下一条
        （如 FAULT+RECOVER 的 has_fault_context/auto_healed 双守卫）；
        全部规则耗尽才记拒绝并保持原态。
        """
        ctx = ctx or {}
        guard_failed = ""
        for rule in TRANSITION_TABLE:
            if rule.src is self.state and rule.trigger is trigger:
                if not _GUARDS[rule.guard](ctx):
                    guard_failed = rule.guard
                    continue
                self.state = rule.dst
                self.entered_at = now
                if rule.dst is LiveState.FAULT:
                    self.fault_context = str(ctx.get("fault_detail", "unspecified"))
                if rule.dst is LiveState.IDLE and trigger is Trigger.RECOVER:
                    self.fault_context = ""
                return True, self.state, rule.actions
        self.rejected.append((trigger, f"guard:{guard_failed}" if guard_failed else "no_rule"))
        return False, self.state, ()

    def snapshot(self) -> StateMachineSnapshot:
        """导出快照（含故障上下文与游标接力字段）。"""
        return StateMachineSnapshot(
            state=self.state,
            entered_at=self.entered_at,
            fault_context=self.fault_context,
        )
