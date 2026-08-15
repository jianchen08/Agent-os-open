"""TaskEventReceiverPlugin 单元测试——任务终态事件接收与注入。

覆盖：订阅 task_service 回调、_on_state_changed 终态过滤（仅 completed/failed）、
子任务（有 parent_task_id / parent_pipeline_id）跳过、dict 与对象两种 task 形态、
事件文本注入到 user_input、处理后清空队列、shutdown 注销。
"""

from __future__ import annotations

from typing import Any

import pytest
from pipeline.plugin import PluginContext

pytestmark = pytest.mark.unit


# ============================================================
# 辅助
# ============================================================


def _ctx(state: dict[str, Any] | None = None, services: dict[str, Any] | None = None) -> PluginContext:
    return PluginContext(state=state or {}, config={}, _services=services or {})


class _FakeTaskService:
    """伪 TaskService：记录回调注册/注销，get_task 返回预设 dict。"""

    def __init__(self, task: Any = None) -> None:
        self._task = task
        self.registered: list[Any] = []
        self.unregistered: list[Any] = []

    def register_state_callback(self, cb: Any) -> None:
        self.registered.append(cb)

    def unregister_state_callback(self, cb: Any) -> None:
        self.unregistered.append(cb)

    def get_task(self, task_id: str) -> Any:
        return self._task


# ============================================================
# 配置与基本属性
# ============================================================


class TestConfig:
    def test_属性(self) -> None:
        from plugin import TaskEventReceiverPlugin

        p = TaskEventReceiverPlugin()
        assert p.name == "task_event_receiver"
        assert p.priority == 40

    def test_error_policy为SKIP(self) -> None:
        from pipeline.types import ErrorPolicy
        from plugin import TaskEventReceiverPlugin

        assert TaskEventReceiverPlugin.error_policy == ErrorPolicy.SKIP

    def test_初始无pending事件与未订阅(self) -> None:
        from plugin import TaskEventReceiverPlugin

        p = TaskEventReceiverPlugin()
        assert p._pending_events == []
        assert p._subscribed is False


# ============================================================
# 订阅
# ============================================================


class TestSubscription:
    @pytest.mark.asyncio
    async def test_首次execute订阅task_service回调(self) -> None:
        from plugin import TaskEventReceiverPlugin

        svc = _FakeTaskService()
        p = TaskEventReceiverPlugin()
        await p.execute(_ctx({"task_id": "t1"}, services={"task_service": svc}))

        assert p._subscribed is True
        assert p._task_service is svc
        assert len(svc.registered) == 1
        assert svc.registered[0] == p._on_state_changed
        assert p._current_task_id == "t1"

    @pytest.mark.asyncio
    async def test_无task_service时不订阅且不抛(self) -> None:
        from plugin import TaskEventReceiverPlugin

        p = TaskEventReceiverPlugin()
        await p.execute(_ctx({}))  # 无 task_service
        assert p._subscribed is False
        assert p._task_service is None

    @pytest.mark.asyncio
    async def test_订阅仅发生一次(self) -> None:
        from plugin import TaskEventReceiverPlugin

        svc = _FakeTaskService()
        p = TaskEventReceiverPlugin()
        for _ in range(3):
            await p.execute(_ctx({}, services={"task_service": svc}))
        assert len(svc.registered) == 1

    @pytest.mark.asyncio
    async def test_register抛异常时不订阅不抛(self) -> None:
        from plugin import TaskEventReceiverPlugin

        class _Bad:
            def register_state_callback(self, cb: Any) -> None:
                raise RuntimeError("nope")

        p = TaskEventReceiverPlugin()
        await p.execute(_ctx({}, services={"task_service": _Bad()}))
        assert p._subscribed is False


# ============================================================
# _on_state_changed —— 终态过滤与子任务跳过
# ============================================================


class TestOnStateChanged:
    @pytest.mark.asyncio
    async def test_非终态状态不入队(self) -> None:
        from plugin import TaskEventReceiverPlugin

        p = TaskEventReceiverPlugin()
        p._task_service = _FakeTaskService(task={"title": "t"})
        for status in ("pending", "running", "paused"):
            await p._on_state_changed("t1", "running", status)
        assert p._pending_events == []

    @pytest.mark.asyncio
    async def test_completed终态入队(self) -> None:
        from plugin import TaskEventReceiverPlugin

        p = TaskEventReceiverPlugin()
        p._task_service = _FakeTaskService(
            task={"title": "我的任务", "parent_task_id": "", "parent_pipeline_id": ""}
        )
        await p._on_state_changed("t1", "running", "completed")

        assert len(p._pending_events) == 1
        evt = p._pending_events[0]
        assert evt["type"] == "task_completed"
        assert evt["title"] == "我的任务"
        assert evt["status"] == "completed"

    @pytest.mark.asyncio
    async def test_failed终态入队带error(self) -> None:
        from plugin import TaskEventReceiverPlugin

        p = TaskEventReceiverPlugin()
        p._task_service = _FakeTaskService(
            task={"title": "t", "error": "boom", "parent_task_id": ""}
        )
        await p._on_state_changed("t1", "running", "failed")

        evt = p._pending_events[0]
        assert evt["type"] == "task_failed"
        assert evt["error"] == "boom"

    @pytest.mark.asyncio
    async def test_有parent_task_id的子任务跳过(self) -> None:
        from plugin import TaskEventReceiverPlugin

        p = TaskEventReceiverPlugin()
        p._task_service = _FakeTaskService(
            task={"title": "t", "parent_task_id": "parent-1"}
        )
        await p._on_state_changed("child", "running", "completed")
        assert p._pending_events == []

    @pytest.mark.asyncio
    async def test_有parent_pipeline_id的子任务跳过(self) -> None:
        from plugin import TaskEventReceiverPlugin

        p = TaskEventReceiverPlugin()
        p._task_service = _FakeTaskService(
            task={"title": "t", "parent_task_id": "", "parent_pipeline_id": "pp-1"}
        )
        await p._on_state_changed("child", "running", "completed")
        assert p._pending_events == []

    @pytest.mark.asyncio
    async def test_task为对象形态也能提取字段(self) -> None:
        from plugin import TaskEventReceiverPlugin

        class _TaskObj:
            parent_task_id = ""
            parent_pipeline_id = ""
            title = "对象任务"
            error = ""

        svc = _FakeTaskService(task=_TaskObj())
        p = TaskEventReceiverPlugin()
        p._task_service = svc
        await p._on_state_changed("t1", "running", "completed")

        assert len(p._pending_events) == 1
        assert p._pending_events[0]["title"] == "对象任务"

    @pytest.mark.asyncio
    async def test_task为None时用默认值入队(self) -> None:
        from plugin import TaskEventReceiverPlugin

        p = TaskEventReceiverPlugin()
        p._task_service = _FakeTaskService(task=None)
        await p._on_state_changed("t1", "running", "completed")

        evt = p._pending_events[0]
        assert evt["title"] == "未知任务"

    @pytest.mark.asyncio
    async def test_无task_service也接受终态事件用默认值(self) -> None:
        from plugin import TaskEventReceiverPlugin

        p = TaskEventReceiverPlugin()
        # task_service 为 None
        await p._on_state_changed("t1", "running", "completed")
        assert len(p._pending_events) == 1
        assert p._pending_events[0]["title"] == "未知任务"


# ============================================================
# execute 注入 user_input
# ============================================================


class TestEventInjection:
    @pytest.mark.asyncio
    async def test_无pending事件返回空(self) -> None:
        from plugin import TaskEventReceiverPlugin

        p = TaskEventReceiverPlugin()
        result = await p.execute(_ctx({"user_input": "hi"}))
        assert result.state_updates == {}

    @pytest.mark.asyncio
    async def test_completed事件注入到user_input前置(self) -> None:
        from plugin import TaskEventReceiverPlugin

        p = TaskEventReceiverPlugin()
        p._pending_events = [
            {
                "type": "task_completed",
                "title": "构建",
                "parent_task_id": "",
            }
        ]
        result = await p.execute(_ctx({"user_input": "继续"}))

        injected = result.state_updates["user_input"]
        assert "[系统通知]" in injected
        assert "构建" in injected
        assert "已完成" in injected
        assert "继续" in injected
        # 通知在前
        assert injected.index("系统通知") < injected.index("继续")

    @pytest.mark.asyncio
    async def test_failed事件带error与parent容器提示(self) -> None:
        from plugin import TaskEventReceiverPlugin

        p = TaskEventReceiverPlugin()
        p._pending_events = [
            {
                "type": "task_failed",
                "title": "部署",
                "error": "超时",
                "parent_task_id": "容器A",
            }
        ]
        result = await p.execute(_ctx({"user_input": ""}))

        injected = result.state_updates["user_input"]
        assert "部署" in injected
        assert "超时" in injected
        assert "[容器 容器A]" in injected

    @pytest.mark.asyncio
    async def test_处理后清空pending队列(self) -> None:
        from plugin import TaskEventReceiverPlugin

        p = TaskEventReceiverPlugin()
        p._pending_events = [
            {"type": "task_completed", "title": "a", "parent_task_id": ""},
            {"type": "task_failed", "title": "b", "error": "e", "parent_task_id": ""},
        ]
        await p.execute(_ctx({"user_input": "x"}))
        assert p._pending_events == []

    @pytest.mark.asyncio
    async def test_多事件合并为一段文本(self) -> None:
        from plugin import TaskEventReceiverPlugin

        p = TaskEventReceiverPlugin()
        p._pending_events = [
            {"type": "task_completed", "title": "a", "parent_task_id": ""},
            {"type": "task_completed", "title": "b", "parent_task_id": ""},
        ]
        result = await p.execute(_ctx({"user_input": ""}))
        injected = result.state_updates["user_input"]
        assert "a" in injected and "b" in injected
        # 两条通知 + 原 user_input（空）→ strip 后无尾部空行
        assert injected.count("[系统通知]") == 2


# ============================================================
# shutdown
# ============================================================


class TestShutdown:
    def test_订阅后shutdown注销回调(self) -> None:
        from plugin import TaskEventReceiverPlugin

        svc = _FakeTaskService()
        p = TaskEventReceiverPlugin()
        p._task_service = svc
        p._subscribed = True
        p._pending_events = [{"type": "task_completed"}]

        p.shutdown()

        assert len(svc.unregistered) == 1
        assert p._pending_events == []

    def test_未订阅shutdown不抛(self) -> None:
        from plugin import TaskEventReceiverPlugin

        p = TaskEventReceiverPlugin()
        # 未订阅、无 task_service
        p.shutdown()  # 不应抛异常
