"""
验证 task_submit 事件链路修复（回归测试）。

根因：
1. emit() 不做点号→下划线归一化，导致 "task.submitted" 被当作 CUSTOM 事件
2. task_submit 使用 pipeline.event_bus.EventBus 而非 core.event_bus，事件发到错误实例

修复：
1. emit() 增加 normalize 逻辑，与 subscribe_simple() 保持对称
2. task_submit._get_event_bus() 改用 core.event_bus.get_event_bus()
"""
import pytest

from src.core.event_bus.types import EventFilter, EventType, ExecutionEvent
from src.core.event_bus.memory import InMemoryEventBus


class TestEmitNormalize:
    """验证 emit() 对事件名的归一化处理"""

    @pytest.mark.asyncio
    async def test_emit_dot_format_matches_enum(self):
        """emit("task.submitted") 应被归一化为 EventType.TASK_SUBMITTED"""
        bus = InMemoryEventBus()
        await bus.connect()

        received: list[ExecutionEvent] = []

        async def handler(event: ExecutionEvent) -> None:
            received.append(event)

        bus.subscribe(handler, filter=EventFilter(event_types=[EventType.TASK_SUBMITTED]))
        await bus.emit("task.submitted", {"task_id": "test-001"})

        # PYTEST_CURRENT_TEST 环境下批处理立即执行
        import asyncio
        await asyncio.sleep(0.05)

        assert len(received) == 1, (
            f"点号格式 task.submitted 应匹配 EventType.TASK_SUBMITTED，"
            f"但收到 {len(received)} 个事件"
        )
        assert received[0].event_type == EventType.TASK_SUBMITTED
        assert received[0].data["task_id"] == "test-001"

    @pytest.mark.asyncio
    async def test_emit_underscore_format_matches_enum(self):
        """emit("task_submitted") 下划线格式也应正常匹配"""
        bus = InMemoryEventBus()
        await bus.connect()

        received: list[ExecutionEvent] = []

        async def handler(event: ExecutionEvent) -> None:
            received.append(event)

        bus.subscribe(handler, filter=EventFilter(event_types=[EventType.TASK_SUBMITTED]))
        await bus.emit("task_submitted", {"task_id": "test-002"})

        import asyncio
        await asyncio.sleep(0.05)

        assert len(received) == 1
        assert received[0].event_type == EventType.TASK_SUBMITTED

    @pytest.mark.asyncio
    async def test_emit_ready_for_scheduling_dot_format(self):
        """emit("task.ready_for_scheduling") 应匹配 TASK_READY_FOR_SCHEDULING"""
        bus = InMemoryEventBus()
        await bus.connect()

        received: list[ExecutionEvent] = []

        async def handler(event: ExecutionEvent) -> None:
            received.append(event)

        bus.subscribe(handler, filter=EventFilter(event_types=[EventType.TASK_READY_FOR_SCHEDULING]))
        await bus.emit("task.ready_for_scheduling", {"task_id": "test-003"})

        import asyncio
        await asyncio.sleep(0.05)

        assert len(received) == 1
        assert received[0].event_type == EventType.TASK_READY_FOR_SCHEDULING

    @pytest.mark.asyncio
    async def test_emit_unknown_event_still_custom(self):
        """未知事件名仍应回退为 CUSTOM"""
        bus = InMemoryEventBus()
        await bus.connect()

        received: list[ExecutionEvent] = []

        async def handler(event: ExecutionEvent) -> None:
            received.append(event)

        bus.subscribe(handler, filter=EventFilter(event_types=[EventType.CUSTOM]))
        await bus.emit("completely.unknown.event", {"data": "test"})

        import asyncio
        await asyncio.sleep(0.05)

        assert len(received) == 1
        assert received[0].event_type == EventType.CUSTOM
        assert received[0].data.get("custom_event_type") == "completely.unknown.event"


class TestFullEventChain:
    """验证完整事件链路：task_submit → TaskOrchestrator → Scheduler"""

    @pytest.mark.asyncio
    async def test_dot_emit_reaches_subscriber(self):
        """emit("task.submitted") + subscribe_simple("task_ready_for_scheduling") 链路"""
        bus = InMemoryEventBus()
        await bus.connect()

        submitted_received: list[ExecutionEvent] = []
        ready_received: list[ExecutionEvent] = []

        async def on_submitted(event: ExecutionEvent) -> None:
            submitted_received.append(event)
            # 模拟 TaskOrchestrator：无依赖 → 发布 ready_for_scheduling
            await bus.emit("task.ready_for_scheduling", {
                "task_id": event.data["task_id"],
            })

        async def on_ready(event: ExecutionEvent) -> None:
            ready_received.append(event)

        # TaskOrchestrator 订阅（EventFilter 方式）
        bus.subscribe(
            on_submitted,
            filter=EventFilter(event_types=[EventType.TASK_SUBMITTED]),
        )
        # Scheduler 订阅（subscribe_simple 方式，下划线格式）
        bus.subscribe_simple("task_ready_for_scheduling", on_ready)

        # task_submit 发布（点号格式）
        await bus.emit("task.submitted", {
            "task_id": "chain-test-001",
            "target_type": "agent",
        })

        import asyncio
        await asyncio.sleep(0.15)

        assert len(submitted_received) == 1, (
            f"TaskOrchestrator 应收到 task.submitted 事件，实际收到 {len(submitted_received)}"
        )
        assert len(ready_received) == 1, (
            f"Scheduler 应收到 task.ready_for_scheduling 事件，实际收到 {len(ready_received)}"
        )
        assert ready_received[0].data["task_id"] == "chain-test-001"
