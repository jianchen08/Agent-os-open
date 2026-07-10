"""
REQ-4 验证：Event 触发器修复测试

验证范围：
1. EventTrigger（旧系统）的事件匹配与过滤逻辑
2. TriggerManager（新系统）的事件评估与分发逻辑
3. 事件类型匹配、数据过滤、禁用状态处理
"""
import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.triggers.models import ActionConfig, ActionType, TriggerConfig, TriggerType


# ===========================================================================
# 1. EventTrigger（旧系统）测试
# ===========================================================================

# 检查 simpleeval 是否可用
simpleeval = pytest.importorskip("simpleeval", reason="simpleeval 未安装，跳过 EventTrigger 测试")


class TestEventTriggerMatching:
    """验证 EventTrigger 的事件匹配逻辑。"""

    def _make_event_trigger_config(
        self,
        event_type: str = "execution_start",
        filter_expression: str | None = None,
    ) -> TriggerConfig:
        """创建 EventTrigger 配置。"""
        event_config = {"type": event_type}
        if filter_expression:
            event_config["filter"] = filter_expression
        return TriggerConfig(
            id="test-event-001",
            name="test-event-trigger",
            trigger_type=TriggerType.EVENT,
            enabled=True,
            event=event_config,
            actions=[
                ActionConfig(
                    type=ActionType.NOTIFICATION,
                    config={"message": "event triggered"},
                    order=0,
                )
            ],
        )

    def _make_execution_event(self, event_type: str = "execution_start", data: dict | None = None):
        """创建 ExecutionEvent 对象。"""
        from src.core.event_bus.types import EventPriority, EventType, ExecutionEvent

        # 查找匹配的 EventType 枚举
        for et in EventType:
            if et.value == event_type:
                break
        else:
            # 自定义事件使用 CUSTOM
            et = EventType.CUSTOM

        return ExecutionEvent(
            event_id="evt-001",
            event_type=et,
            session_id="sess-001",
            data=data or {},
            timestamp=datetime.datetime.now(),
            priority=EventPriority.NORMAL,
        )

    def test_event_trigger_initialization(self):
        """验证 EventTrigger 正确初始化。"""
        from src.triggers.triggers.event_trigger import EventTrigger

        config = self._make_event_trigger_config()
        trigger = EventTrigger(config)

        assert trigger.event_type == "execution_start"
        assert trigger.enabled is True
        assert trigger.filter_expression is None

    def test_event_trigger_requires_event_config(self):
        """验证没有 event 配置时抛出 ValueError。"""
        from src.triggers.triggers.event_trigger import EventTrigger

        config = TriggerConfig(
            id="test-no-event",
            name="no-event",
            trigger_type=TriggerType.EVENT,
            enabled=True,
        )
        with pytest.raises(ValueError, match="必须包含 event"):
            EventTrigger(config)

    def test_event_trigger_requires_event_type(self):
        """验证没有 event.type 时抛出 ValueError。"""
        from src.triggers.triggers.event_trigger import EventTrigger

        config = TriggerConfig(
            id="test-no-type",
            name="no-type",
            trigger_type=TriggerType.EVENT,
            enabled=True,
            event={"other_field": "value"},  # 缺少 type
        )
        with pytest.raises(ValueError, match="必须包含 event.type"):
            EventTrigger(config)

    def test_event_trigger_wrong_type_raises(self):
        """验证错误触发器类型时抛出 ValueError。"""
        from src.triggers.triggers.event_trigger import EventTrigger

        config = TriggerConfig(
            id="test-wrong-type",
            name="wrong-type",
            trigger_type=TriggerType.TIME,  # 错误类型
            enabled=True,
        )
        with pytest.raises(ValueError, match="触发器类型必须是 EVENT"):
            EventTrigger(config)

    @pytest.mark.asyncio
    async def test_matching_event_type_triggers(self):
        """验证事件类型匹配时执行动作。"""
        from src.triggers.triggers.event_trigger import EventTrigger

        config = self._make_event_trigger_config(event_type="execution_start")
        trigger = EventTrigger(config)

        # Mock execute_actions
        mock_result = MagicMock()
        mock_result.success = True
        trigger.execute_actions = AsyncMock(return_value=mock_result)

        event = self._make_execution_event("execution_start")
        result = await trigger.execute(event)

        assert result.success is True
        trigger.execute_actions.assert_called_once()

    @pytest.mark.asyncio
    async def test_non_matching_event_type_skips(self):
        """验证事件类型不匹配时跳过执行。"""
        from src.triggers.triggers.event_trigger import EventTrigger

        config = self._make_event_trigger_config(event_type="execution_start")
        trigger = EventTrigger(config)

        trigger.execute_actions = AsyncMock()

        # 发送不匹配的事件
        event = self._make_execution_event("execution_error")
        result = await trigger.execute(event)

        assert result.success is False
        assert "不匹配" in result.message
        trigger.execute_actions.assert_not_called()

    @pytest.mark.asyncio
    async def test_disabled_trigger_skips(self):
        """验证禁用的触发器跳过执行。"""
        from src.triggers.triggers.event_trigger import EventTrigger

        config = self._make_event_trigger_config(event_type="execution_start")
        config.enabled = False
        trigger = EventTrigger(config)

        trigger.execute_actions = AsyncMock()

        event = self._make_execution_event("execution_start")
        result = await trigger.execute(event)

        assert result.success is False
        assert "已禁用" in result.message
        trigger.execute_actions.assert_not_called()

    def test_matches_event_method(self):
        """验证 matches_event 方法的匹配逻辑。"""
        from src.triggers.triggers.event_trigger import EventTrigger

        config = self._make_event_trigger_config(event_type="execution_start")
        trigger = EventTrigger(config)

        assert trigger.matches_event("execution_start") is True
        assert trigger.matches_event("execution_error") is False
        assert trigger.matches_event("") is False


class TestEventTriggerFilter:
    """验证 EventTrigger 的过滤条件逻辑。"""

    def _make_trigger(self, filter_expr: str | None = None):
        from src.triggers.triggers.event_trigger import EventTrigger

        event_config = {"type": "execution_start"}
        if filter_expr:
            event_config["filter"] = filter_expr

        config = TriggerConfig(
            id="test-filter-001",
            name="test-filter",
            trigger_type=TriggerType.EVENT,
            enabled=True,
            event=event_config,
            actions=[],
        )
        return EventTrigger(config)

    def test_no_filter_passes_all(self):
        """无过滤条件时所有事件都通过。"""
        trigger = self._make_trigger(filter_expr=None)
        assert trigger._check_filter({"status": "ok"}) is True
        assert trigger._check_filter({}) is True

    def test_simple_filter_passes(self):
        """简单条件表达式匹配时通过。"""
        trigger = self._make_trigger(filter_expr="status == 'ok'")
        assert trigger._check_filter({"status": "ok"}) is True

    def test_simple_filter_blocks(self):
        """简单条件表达式不匹配时阻止。"""
        trigger = self._make_trigger(filter_expr="status == 'ok'")
        assert trigger._check_filter({"status": "error"}) is False

    def test_numeric_comparison_filter(self):
        """数值比较过滤条件。"""
        trigger = self._make_trigger(filter_expr="count > 5")
        assert trigger._check_filter({"count": 10}) is True
        assert trigger._check_filter({"count": 3}) is False

    def test_invalid_filter_expression_fails_safely(self):
        """无效过滤表达式安全失败（不触发）。"""
        trigger = self._make_trigger(filter_expr="invalid!!!syntax")
        # 应安全返回 False，不抛异常
        assert trigger._check_filter({"key": "value"}) is False

    def test_filter_with_missing_key_fails_safely(self):
        """过滤条件引用不存在的键时安全失败。"""
        trigger = self._make_trigger(filter_expr="nonexistent > 5")
        assert trigger._check_filter({"other_key": 10}) is False


# ===========================================================================
# 2. TriggerManager（新系统）事件分发测试
# ===========================================================================

class TestTriggerManagerEventDispatch:
    """验证新系统 TriggerManager 的事件分发逻辑。"""

    def _make_manager(self):
        from src.triggers.manager import TriggerManager
        return TriggerManager()

    def _make_event_config(
        self,
        trigger_id: str = "evt-mgr-001",
        event_name: str = "task_completed",
        event_filter: dict | None = None,
    ):
        from src.triggers.types import TriggerConfig, TriggerType

        return TriggerConfig(
            trigger_id=trigger_id,
            name=f"event-{event_name}",
            trigger_type=TriggerType.EVENT,
            event_name=event_name,
            event_filter=event_filter or {},
            message="test message",
        )

    def test_matching_event_fires_trigger(self):
        """事件名称匹配时应触发。"""
        manager = self._make_manager()
        config = self._make_event_config(event_name="task_completed")
        manager.register(config)

        fired = manager.evaluate_event("task_completed", {"task_id": "t-001"})
        assert "evt-mgr-001" in fired

    def test_non_matching_event_does_not_fire(self):
        """事件名称不匹配时不应触发。"""
        manager = self._make_manager()
        config = self._make_event_config(event_name="task_completed")
        manager.register(config)

        fired = manager.evaluate_event("task_failed", {"task_id": "t-001"})
        assert "evt-mgr-001" not in fired

    def test_multiple_triggers_same_event(self):
        """同一事件可以触发多个触发器。"""
        manager = self._make_manager()
        manager.register(self._make_event_config("t1", "task_completed"))
        manager.register(self._make_event_config("t2", "task_completed"))
        manager.register(self._make_event_config("t3", "task_started"))

        fired = manager.evaluate_event("task_completed", {})
        assert "t1" in fired
        assert "t2" in fired
        assert "t3" not in fired

    def test_max_fires_limit(self):
        """达到 max_fires 后触发器状态变为 FIRED。"""
        from src.triggers.types import TriggerStatus

        manager = self._make_manager()
        config = self._make_event_config("t-max", "task_completed")
        config.max_fires = 2
        manager.register(config)

        # 第一次触发
        fired1 = manager.evaluate_event("task_completed", {})
        assert "t-max" in fired1
        assert config.status == TriggerStatus.ACTIVE

        # 第二次触发 → 达到上限
        fired2 = manager.evaluate_event("task_completed", {})
        assert "t-max" in fired2
        assert config.status == TriggerStatus.FIRED

        # 第三次不应触发
        fired3 = manager.evaluate_event("task_completed", {})
        assert "t-max" not in fired3

    def test_cancelled_trigger_does_not_fire(self):
        """已取消的触发器不应触发。"""
        from src.triggers.types import TriggerStatus

        manager = self._make_manager()
        config = self._make_event_config("t-cancel", "task_completed")
        manager.register(config)
        manager.cancel("t-cancel")

        fired = manager.evaluate_event("task_completed", {})
        assert "t-cancel" not in fired

    def test_event_filter_matching(self):
        """事件数据过滤器匹配时触发。"""
        manager = self._make_manager()
        config = self._make_event_config(
            "t-filter",
            "task_completed",
            event_filter={"status": "success"},
        )
        manager.register(config)

        # 匹配的数据
        fired_match = manager.evaluate_event("task_completed", {"status": "success"})
        assert "t-filter" in fired_match

    def test_unregister_removes_trigger(self):
        """注销后触发器不再响应事件。"""
        manager = self._make_manager()
        config = self._make_event_config("t-unreg", "task_completed")
        manager.register(config)

        manager.unregister("t-unreg")
        fired = manager.evaluate_event("task_completed", {})
        assert "t-unreg" not in fired

    def test_fire_count_increments(self):
        """每次触发 fire_count 递增。"""
        manager = self._make_manager()
        config = self._make_event_config("t-count", "task_completed")
        config.max_fires = 0  # 无限
        manager.register(config)

        assert config.fire_count == 0
        manager.evaluate_event("task_completed", {})
        assert config.fire_count == 1
        manager.evaluate_event("task_completed", {})
        assert config.fire_count == 2

    def test_last_fire_time_updated(self):
        """触发后 last_fire_time 被更新。"""
        manager = self._make_manager()
        config = self._make_event_config("t-time", "task_completed")
        config.max_fires = 0
        manager.register(config)

        assert config.metadata.get("last_fire_time") is None
        manager.evaluate_event("task_completed", {})
        assert config.metadata.get("last_fire_time") is not None


# ===========================================================================
# 3. 事件触发器集成验证
# ===========================================================================

class TestEventTriggerIntegration:
    """事件触发器新旧系统集成验证。"""

    def test_old_and_new_system_coexist(self):
        """验证旧系统 EventTrigger 和新系统 TriggerManager 可以共存。"""
        from src.triggers.manager import TriggerManager
        from src.triggers.triggers.event_trigger import EventTrigger
        from src.triggers.types import TriggerConfig as NewTriggerConfig
        from src.triggers.types import TriggerType as NewTriggerType

        # 旧系统
        old_config = TriggerConfig(
            id="old-evt",
            name="old-event",
            trigger_type=TriggerType.EVENT,
            enabled=True,
            event={"type": "execution_start"},
            actions=[],
        )
        old_trigger = EventTrigger(old_config)
        assert old_trigger.event_type == "execution_start"

        # 新系统
        manager = TriggerManager()
        new_config = NewTriggerConfig(
            trigger_id="new-evt",
            name="new-event",
            trigger_type=NewTriggerType.EVENT,
            event_name="execution_start",
            message="test",
        )
        manager.register(new_config)

        # 新系统可以处理事件
        fired = manager.evaluate_event("execution_start", {})
        assert "new-evt" in fired

    @pytest.mark.asyncio
    async def test_event_trigger_with_execution_event(self):
        """验证 EventTrigger 能正确处理 ExecutionEvent。"""
        from src.core.event_bus.types import EventPriority, EventType, ExecutionEvent
        from src.triggers.triggers.event_trigger import EventTrigger

        config = TriggerConfig(
            id="exec-evt",
            name="exec-event",
            trigger_type=TriggerType.EVENT,
            enabled=True,
            event={"type": "tool_call_start"},
            actions=[],
        )
        trigger = EventTrigger(config)
        trigger.execute_actions = AsyncMock(
            return_value=MagicMock(success=True, message="ok")
        )

        event = ExecutionEvent(
            event_id="e-001",
            event_type=EventType.TOOL_CALL_START,
            session_id="s-001",
            data={"tool_name": "file_read"},
            timestamp=datetime.datetime.now(),
            priority=EventPriority.NORMAL,
        )

        result = await trigger.execute(event)
        assert result.success is True
        trigger.execute_actions.assert_called_once()
