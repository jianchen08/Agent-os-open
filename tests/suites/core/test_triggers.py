"""触发器系统测试。

覆盖：
1. types.py — 数据类创建、枚举值
2. manager.py — 注册/注销/事件评估/条件评估/定时检查/取消/过滤
"""

import datetime

import pytest

from triggers import (
    TriggerConfig,
    TriggerManager,
    TriggerStatus,
    TriggerType,
)


# ============================================================
# types.py 测试
# ============================================================


class TestTriggerType:
    """TriggerType 枚举测试。"""

    def test_delay_value(self) -> None:
        assert TriggerType.DELAY.value == "delay"

    def test_scheduled_value(self) -> None:
        assert TriggerType.SCHEDULED.value == "scheduled"

    def test_event_value(self) -> None:
        assert TriggerType.EVENT.value == "event"

    def test_condition_value(self) -> None:
        assert TriggerType.CONDITION.value == "condition"

    def test_interval_value(self) -> None:
        assert TriggerType.INTERVAL.value == "interval"

    def test_all_types(self) -> None:
        assert len(TriggerType) == 5


class TestTriggerStatus:
    """TriggerStatus 枚举测试。"""

    def test_pending_value(self) -> None:
        assert TriggerStatus.PENDING.value == "pending"

    def test_active_value(self) -> None:
        assert TriggerStatus.ACTIVE.value == "active"

    def test_fired_value(self) -> None:
        assert TriggerStatus.FIRED.value == "fired"

    def test_cancelled_value(self) -> None:
        assert TriggerStatus.CANCELLED.value == "cancelled"

    def test_expired_value(self) -> None:
        assert TriggerStatus.EXPIRED.value == "expired"

    def test_all_statuses(self) -> None:
        assert len(TriggerStatus) == 5


class TestTriggerConfig:
    """TriggerConfig 数据类测试。"""

    def test_default_values(self) -> None:
        config = TriggerConfig()
        assert config.trigger_id == ""
        assert config.trigger_type == TriggerType.EVENT
        assert config.status == TriggerStatus.PENDING
        assert config.delay_seconds == 0.0
        assert config.schedule_cron == ""
        assert config.scheduled_at is None
        assert config.event_name == ""
        assert config.event_filter == {}
        assert config.condition_expression == ""
        assert config.action == ""
        assert config.max_fires == 1
        assert config.fire_count == 0

    def test_custom_event_config(self) -> None:
        config = TriggerConfig(
            trigger_id="evt1",
            name="任务完成事件",
            trigger_type=TriggerType.EVENT,
            event_name="task.completed",
            action="notify",
            max_fires=3,
        )
        assert config.trigger_id == "evt1"
        assert config.event_name == "task.completed"
        assert config.max_fires == 3

    def test_custom_delay_config(self) -> None:
        config = TriggerConfig(
            trigger_id="delay1",
            trigger_type=TriggerType.DELAY,
            delay_seconds=60.0,
        )
        assert config.trigger_type == TriggerType.DELAY
        assert config.delay_seconds == 60.0

    def test_custom_scheduled_config(self) -> None:
        now = datetime.datetime(2026, 4, 11, 12, 0)
        config = TriggerConfig(
            trigger_id="sched1",
            trigger_type=TriggerType.SCHEDULED,
            scheduled_at=now,
        )
        assert config.scheduled_at == now

    def test_custom_condition_config(self) -> None:
        config = TriggerConfig(
            trigger_id="cond1",
            trigger_type=TriggerType.CONDITION,
            condition_expression="progress > 80",
        )
        assert config.condition_expression == "progress > 80"


# ============================================================
# manager.py 测试
# ============================================================


class TestTriggerManager:
    """TriggerManager 测试。"""

    @pytest.fixture
    def manager(self) -> TriggerManager:
        return TriggerManager()

    @pytest.fixture
    def event_config(self) -> TriggerConfig:
        return TriggerConfig(
            trigger_id="evt1",
            name="任务完成",
            trigger_type=TriggerType.EVENT,
            event_name="task.completed",
            action="notify",
        )

    @pytest.fixture
    def condition_config(self) -> TriggerConfig:
        return TriggerConfig(
            trigger_id="cond1",
            name="进度检查",
            trigger_type=TriggerType.CONDITION,
            condition_expression="progress > 80",
            action="send_report",
        )

    # --- 注册/注销 ---

    def test_register_sets_active(
        self, manager: TriggerManager, event_config: TriggerConfig
    ) -> None:
        """注册后状态变为 ACTIVE。"""
        manager.register(event_config)
        assert event_config.status == TriggerStatus.ACTIVE

    def test_register_and_get(
        self, manager: TriggerManager, event_config: TriggerConfig
    ) -> None:
        """注册后可获取。"""
        manager.register(event_config)
        result = manager.get("evt1")
        assert result is not None
        assert result.trigger_id == "evt1"

    def test_get_nonexistent(self, manager: TriggerManager) -> None:
        """获取不存在的触发器返回 None。"""
        assert manager.get("nonexistent") is None

    def test_unregister(
        self, manager: TriggerManager, event_config: TriggerConfig
    ) -> None:
        """注销触发器。"""
        manager.register(event_config)
        assert manager.unregister("evt1") is True
        assert manager.get("evt1") is None

    def test_unregister_nonexistent(self, manager: TriggerManager) -> None:
        """注销不存在的触发器返回 False。"""
        assert manager.unregister("nonexistent") is False

    # --- 事件触发 ---

    def test_evaluate_event_match(
        self, manager: TriggerManager, event_config: TriggerConfig
    ) -> None:
        """匹配的事件触发器被触发。"""
        manager.register(event_config)
        fired = manager.evaluate_event("task.completed", {})
        assert "evt1" in fired
        assert event_config.fire_count == 1

    def test_evaluate_event_no_match(
        self, manager: TriggerManager, event_config: TriggerConfig
    ) -> None:
        """不匹配的事件不会触发。"""
        manager.register(event_config)
        fired = manager.evaluate_event("task.failed", {})
        assert "evt1" not in fired

    def test_evaluate_event_max_fires(
        self, manager: TriggerManager
    ) -> None:
        """达到最大触发次数后状态变为 FIRED。"""
        config = TriggerConfig(
            trigger_id="evt_max",
            trigger_type=TriggerType.EVENT,
            event_name="test.event",
            max_fires=2,
        )
        manager.register(config)
        manager.evaluate_event("test.event", {})
        assert config.status == TriggerStatus.ACTIVE
        manager.evaluate_event("test.event", {})
        assert config.status == TriggerStatus.FIRED

    def test_evaluate_event_zero_max_fires_unlimited(
        self, manager: TriggerManager
    ) -> None:
        """max_fires=0 表示无限触发。"""
        config = TriggerConfig(
            trigger_id="evt_inf",
            trigger_type=TriggerType.EVENT,
            event_name="test.event",
            max_fires=0,
        )
        manager.register(config)
        for _ in range(5):
            manager.evaluate_event("test.event", {})
        assert config.fire_count == 5
        assert config.status == TriggerStatus.ACTIVE

    def test_evaluate_event_with_filter(
        self, manager: TriggerManager
    ) -> None:
        """带过滤条件的事件触发器。"""
        config = TriggerConfig(
            trigger_id="evt_filter",
            trigger_type=TriggerType.EVENT,
            event_name="task.completed",
            event_filter={"status": "success"},
        )
        manager.register(config)

        # 匹配
        fired = manager.evaluate_event(
            "task.completed", {"status": "success"}
        )
        assert "evt_filter" in fired

    def test_evaluate_event_filter_not_match(
        self, manager: TriggerManager
    ) -> None:
        """过滤条件不匹配时不触发。"""
        config = TriggerConfig(
            trigger_id="evt_filter2",
            trigger_type=TriggerType.EVENT,
            event_name="task.completed",
            event_filter={"status": "success"},
        )
        manager.register(config)
        fired = manager.evaluate_event(
            "task.completed", {"status": "failed"}
        )
        assert "evt_filter2" not in fired

    def test_evaluate_event_filter_operator(
        self, manager: TriggerManager
    ) -> None:
        """过滤条件支持操作符。"""
        config = TriggerConfig(
            trigger_id="evt_op",
            trigger_type=TriggerType.EVENT,
            event_name="metric.report",
            event_filter={"score": {"op": "gt", "value": 80}},
        )
        manager.register(config)
        fired = manager.evaluate_event("metric.report", {"score": 90})
        assert "evt_op" in fired
        fired = manager.evaluate_event("metric.report", {"score": 70})
        assert "evt_op" not in fired

    # --- 条件触发 ---

    def test_evaluate_condition_true(
        self, manager: TriggerManager, condition_config: TriggerConfig
    ) -> None:
        """条件为真时触发。"""
        manager.register(condition_config)
        fired = manager.evaluate_condition({"progress": 90})
        assert "cond1" in fired

    def test_evaluate_condition_false(
        self, manager: TriggerManager, condition_config: TriggerConfig
    ) -> None:
        """条件为假时不触发。"""
        manager.register(condition_config)
        fired = manager.evaluate_condition({"progress": 50})
        assert "cond1" not in fired

    def test_evaluate_condition_error(
        self, manager: TriggerManager
    ) -> None:
        """条件表达式执行出错时不触发。"""
        config = TriggerConfig(
            trigger_id="cond_err",
            trigger_type=TriggerType.CONDITION,
            condition_expression="undefined_var > 0",
        )
        manager.register(config)
        fired = manager.evaluate_condition({})
        assert "cond_err" not in fired

    def test_evaluate_condition_forbidden(
        self, manager: TriggerManager
    ) -> None:
        """危险条件表达式被拒绝。"""
        config = TriggerConfig(
            trigger_id="cond_danger",
            trigger_type=TriggerType.CONDITION,
            condition_expression="import os",
        )
        manager.register(config)
        fired = manager.evaluate_condition({})
        assert "cond_danger" not in fired

    # --- 定时触发 ---

    def test_check_scheduled_at_time(
        self, manager: TriggerManager
    ) -> None:
        """定时触发器到期时触发。"""
        scheduled_time = datetime.datetime(2026, 4, 11, 12, 0)
        config = TriggerConfig(
            trigger_id="sched1",
            trigger_type=TriggerType.SCHEDULED,
            scheduled_at=scheduled_time,
        )
        manager.register(config)
        now = datetime.datetime(2026, 4, 11, 13, 0)
        fired = manager.check_scheduled(now)
        assert "sched1" in fired

    def test_check_scheduled_not_yet(
        self, manager: TriggerManager
    ) -> None:
        """定时触发器未到期时不触发。"""
        scheduled_time = datetime.datetime(2026, 4, 11, 18, 0)
        config = TriggerConfig(
            trigger_id="sched2",
            trigger_type=TriggerType.SCHEDULED,
            scheduled_at=scheduled_time,
        )
        manager.register(config)
        now = datetime.datetime(2026, 4, 11, 12, 0)
        fired = manager.check_scheduled(now)
        assert "sched2" not in fired

    def test_check_delay_trigger(
        self, manager: TriggerManager
    ) -> None:
        """延迟触发器到期时触发。"""
        register_time = datetime.datetime(2026, 4, 11, 12, 0)
        config = TriggerConfig(
            trigger_id="delay1",
            trigger_type=TriggerType.DELAY,
            delay_seconds=60.0,
            metadata={"register_time": register_time.isoformat()},
        )
        manager.register(config)
        now = datetime.datetime(2026, 4, 11, 12, 1)
        fired = manager.check_scheduled(now)
        assert "delay1" in fired

    def test_check_delay_not_yet(
        self, manager: TriggerManager
    ) -> None:
        """延迟触发器未到期时不触发。"""
        register_time = datetime.datetime(2026, 4, 11, 12, 0)
        config = TriggerConfig(
            trigger_id="delay2",
            trigger_type=TriggerType.DELAY,
            delay_seconds=60.0,
            metadata={"register_time": register_time.isoformat()},
        )
        manager.register(config)
        now = datetime.datetime(2026, 4, 11, 12, 0, 30)
        fired = manager.check_scheduled(now)
        assert "delay2" not in fired

    # --- 查询 ---

    def test_list_by_type(
        self, manager: TriggerManager, event_config: TriggerConfig
    ) -> None:
        """按类型列出触发器。"""
        manager.register(event_config)
        manager.register(
            TriggerConfig(
                trigger_id="cond1",
                trigger_type=TriggerType.CONDITION,
                condition_expression="x > 0",
            )
        )
        events = manager.list_by_type(TriggerType.EVENT)
        conditions = manager.list_by_type(TriggerType.CONDITION)
        assert len(events) == 1
        assert len(conditions) == 1

    def test_list_active(
        self, manager: TriggerManager
    ) -> None:
        """列出活跃触发器。"""
        config = TriggerConfig(
            trigger_id="active1",
            trigger_type=TriggerType.EVENT,
            event_name="test",
        )
        manager.register(config)
        active = manager.list_active()
        assert len(active) == 1

    def test_cancel_trigger(
        self, manager: TriggerManager, event_config: TriggerConfig
    ) -> None:
        """取消触发器。"""
        manager.register(event_config)
        assert manager.cancel("evt1") is True
        assert event_config.status == TriggerStatus.CANCELLED

    def test_cancel_nonexistent(self, manager: TriggerManager) -> None:
        """取消不存在的触发器返回 False。"""
        assert manager.cancel("nonexistent") is False

    def test_cancel_already_fired(
        self, manager: TriggerManager
    ) -> None:
        """已触发的触发器不能取消。"""
        config = TriggerConfig(
            trigger_id="fired1",
            trigger_type=TriggerType.EVENT,
            event_name="test",
            max_fires=1,
        )
        manager.register(config)
        manager.evaluate_event("test", {})
        assert config.status == TriggerStatus.FIRED
        assert manager.cancel("fired1") is False

    def test_cancelled_trigger_not_fired(
        self, manager: TriggerManager
    ) -> None:
        """已取消的触发器不会被触发。"""
        config = TriggerConfig(
            trigger_id="cancelled1",
            trigger_type=TriggerType.EVENT,
            event_name="test",
        )
        manager.register(config)
        manager.cancel("cancelled1")
        fired = manager.evaluate_event("test", {})
        assert "cancelled1" not in fired
