# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: python-coverage
"""triggers_ext 自动父通知投递目标（血缘冒泡）单元测试。

ADR 2026-11-06-task-settlement-notify-guard-identity-and-lineage-bubble（D3/D6
注入侧刀6）：tasks 插件守卫跳过已终态任务父时沿血缘冒泡，事件新增
``wakeup_target_pipeline_id`` 标签 = 实际投递目标。注入侧契约：

- 投递目标读 ``wakeup_target_pipeline_id``，缺省回落 ``parent_pipeline_id``
  （旧事件兼容不破，ADR 2026-08-20 注入分工不变：tasks 只算目标，注入仍归
  triggers_ext 唯一持有）；
- 熔断键保持 ``(parent_pipeline_id, task_id, event)`` 三维不变（键稳定，
  ADR 2026-09-30 契约保留）；
- target ≠ 直接父时消息**尾部**追加「（经由已终态父任务 <parent> 上送）」——
  ``[系统通知]`` 前缀与 ``(ID: <task_id>)`` 锚原样保留在前（llm_core 出站
  衰减契约，ADR 2026-09-30）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent  # plugins/shared/triggers_ext/
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from triggers.manager import TriggerManager  # noqa: E402


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class BubbleHarness:
    """冒泡投递驱动器：发一条带 wakeup_target 的域事件，收集注入。"""

    def __init__(self) -> None:
        self.received: list[tuple[str, str, str]] = []
        self.mgr = TriggerManager()

        async def fake_injector(pipeline_id: str, message: str, user_id: str, state: dict | None = None) -> str:
            self.received.append((pipeline_id, message, user_id))
            return "ok"

        self.mgr.set_injector(fake_injector)

    def fire(self, event: str = "task_completed", **overrides: Any) -> None:
        data = {
            "pipeline_id": "child_pipe",
            "task_id": "t_bubble",
            "parent_pipeline_id": "pipe_parent",
            "user_id": "u_admin",
            "title": "写周报",
        }
        data.update(overrides)
        _run(self.mgr.handle_domain_event(event, data))


class TestWakeupTargetRouting:
    """投递目标 = wakeup_target_pipeline_id（缺省回落直接父）。"""

    def test_injector_receives_wakeup_target(self) -> None:
        """㉔ 事件带 wakeup_target_pipeline_id → 注入器收到冒泡目标（非直接父）。"""
        h = BubbleHarness()
        h.fire(wakeup_target_pipeline_id="pipe_session")
        assert len(h.received) == 1
        assert h.received[0][0] == "pipe_session", "投递目标 = 冒泡终点（会话祖先）"
        assert h.received[0][2] == "u_admin", "user_id 透传不变"

    def test_missing_wakeup_target_falls_back_to_parent(self) -> None:
        """旧事件兼容：无 wakeup_target 标签 → 回落 parent_pipeline_id 直送。"""
        h = BubbleHarness()
        h.fire()
        assert len(h.received) == 1
        assert h.received[0][0] == "pipe_parent"

    def test_empty_wakeup_target_falls_back_to_parent(self) -> None:
        """空串标签（根任务等无目标形态）同样回落直接父。"""
        h = BubbleHarness()
        h.fire(wakeup_target_pipeline_id="")
        assert len(h.received) == 1
        assert h.received[0][0] == "pipe_parent"


class TestBubbleMessageContract:
    """文案契约：前缀/锚在前，上送提示在尾（llm_core 出站衰减可识别）。"""

    def test_prefix_and_anchor_preserved_with_tail_note(self) -> None:
        """㉕ target≠parent：``[系统通知]`` 前缀与 ``(ID: <task_id>)`` 锚原样
        在前，尾部追加「（经由已终态父任务 <parent> 上送）」。"""
        h = BubbleHarness()
        h.fire(wakeup_target_pipeline_id="pipe_session")
        message = h.received[0][1]
        assert message.startswith("[系统通知]"), "前缀必须保留在最前（出站衰减契约）"
        assert "(ID: t_bubble)" in message, "任务 ID 锚必须保留"
        note = "（经由已终态父任务 pipe_parent 上送）"
        assert note in message, "冒泡上送提示必须在场"
        assert message.index(note) > message.index("(ID: t_bubble)"), (
            "上送提示在尾部（不破坏前缀/锚的形态）"
        )

    def test_direct_delivery_has_no_tail_note(self) -> None:
        """对照（区分度输入）：直送（target=parent）不带 上送提示。"""
        h = BubbleHarness()
        h.fire()
        message = h.received[0][1]
        assert "经由已终态父任务" not in message
        assert message.startswith("[系统通知]")


class TestBreakerKeyStability:
    """熔断键保持 (parent, task, event) 三维不变（ADR 2026-09-30 契约）。"""

    def test_breaker_key_uses_parent_not_target(self) -> None:
        """㉖ 同 parent 同 task 换 target 连发 → 第 2 次带重复提示（键未变），
        第 3 次起熔断；target 不参与定键。"""
        h = BubbleHarness()
        h.fire(wakeup_target_pipeline_id="pipe_session")
        h.fire(wakeup_target_pipeline_id="pipe_other_target")
        assert len(h.received) == 2, "同键前两次都注入"
        assert "第 2 次" in h.received[1][1], "第 2 次注入带重复计数提示（熔断键=直接父维）"
        h.fire(wakeup_target_pipeline_id="pipe_third")
        assert len(h.received) == 2, "第 3 次同键熔断（target 维不稀释键）"

    def test_breaker_window_rollover_restarts_with_targets(self) -> None:
        """窗滚过后重新计数（冒泡路径同受熔断保护）。"""
        clock = _FakeClock()
        received: list[tuple[str, str, str]] = []
        mgr = TriggerManager(clock=clock)

        async def fake_injector(pipeline_id: str, message: str, user_id: str, state: dict | None = None) -> str:
            received.append((pipeline_id, message, user_id))
            return "ok"

        mgr.set_injector(fake_injector)
        data = {
            "pipeline_id": "child_pipe",
            "task_id": "t_roll",
            "parent_pipeline_id": "pipe_parent",
            "user_id": "u",
            "wakeup_target_pipeline_id": "pipe_session",
        }
        _run(mgr.handle_domain_event("task_failed", dict(data)))
        _run(mgr.handle_domain_event("task_failed", dict(data)))
        assert len(received) == 2
        clock.advance(601.0)
        _run(mgr.handle_domain_event("task_failed", dict(data)))
        assert len(received) == 3
        assert "第 2 次" not in received[2][1], "窗滚过后重新计数为首报"


class _FakeClock:
    """可推进单调钟（时序断言显式推进，禁 sleep）。"""

    def __init__(self) -> None:
        self.now = 1000.0

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def __call__(self) -> float:
        return self.now
