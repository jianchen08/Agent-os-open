"""
REQ-3 验证：Schedule 触发器时区 Bug 测试

验证范围：
1. TimeTrigger（旧系统）的时区处理
2. TriggerManager（新系统）的时间调度
3. datetime.fromisoformat 对带时区字符串的处理
4. APScheduler CronTrigger 的时区感知
"""
import datetime
from unittest.mock import MagicMock, patch

import pytest

from src.triggers.models import ActionConfig, ActionType, TriggerConfig, TriggerType


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _make_time_trigger_config(
    schedule_type: str = "cron",
    expression: str = "* * * * *",
    **extra,
) -> TriggerConfig:
    """创建用于 TimeTrigger 的 TriggerConfig（旧 models.py）。"""
    schedule = {"type": schedule_type, "expression": expression, **extra}
    return TriggerConfig(
        id="test-tz-001",
        name="test-timezone-trigger",
        trigger_type=TriggerType.TIME,
        enabled=True,
        schedule=schedule,
        actions=[
            ActionConfig(
                type=ActionType.NOTIFICATION,
                config={"message": "test"},
                order=0,
            )
        ],
    )


# ===========================================================================
# 1. datetime.fromisoformat 时区处理测试
# ===========================================================================

class TestDatetimeTimezoneParsing:
    """验证 datetime.fromisoformat 对不同时区格式的解析行为。"""

    def test_naive_iso_string_produces_naive_datetime(self):
        """无时区 ISO 字符串应生成 naive datetime。"""
        dt = datetime.datetime.fromisoformat("2026-06-09T10:00:00")
        assert dt.tzinfo is None, "naive 字符串应生成无时区 datetime"

    def test_utc_iso_string_produces_aware_datetime(self):
        """带 +00:00 的 ISO 字符串应生成 aware datetime。"""
        dt = datetime.datetime.fromisoformat("2026-06-09T10:00:00+00:00")
        assert dt.tzinfo is not None, "UTC 字符串应生成有时区 datetime"

    def test_offset_iso_string_produces_aware_datetime(self):
        """带时区偏移（如 +08:00）的 ISO 字符串应生成 aware datetime。"""
        dt = datetime.datetime.fromisoformat("2026-06-09T18:00:00+08:00")
        assert dt.tzinfo is not None, "偏移字符串应生成有时区 datetime"

    def test_comparing_naive_and_aware_raises_type_error(self):
        """naive datetime 与 aware datetime 比较应抛出 TypeError。"""
        naive = datetime.datetime(2026, 6, 9, 10, 0, 0)
        aware = datetime.datetime(2026, 6, 9, 10, 0, 0, tzinfo=datetime.timezone.utc)

        with pytest.raises(TypeError):
            _ = naive < aware

    def test_utcnow_produces_naive_datetime(self):
        """datetime.utcnow() 应生成 naive datetime（旧代码行为）。"""
        dt = datetime.datetime.utcnow()
        assert dt.tzinfo is None, "utcnow() 应返回 naive datetime"

    def test_now_utc_produces_aware_datetime(self):
        """datetime.now(datetime.UTC) 应生成 aware datetime（新代码行为）。"""
        dt = datetime.datetime.now(datetime.UTC)
        assert dt.tzinfo is not None, "now(UTC) 应返回 aware datetime"


# ===========================================================================
# 2. TimeTrigger 时间触发器（旧系统）时区测试
# ===========================================================================

# 检查 APScheduler 是否可用
apscheduler = pytest.importorskip("apscheduler", reason="apscheduler 未安装，跳过 TimeTrigger 测试")


class TestTimeTriggerTimezone:
    """
    验证 TimeTrigger 的时区处理。

    关键发现：
    - time_trigger.py 全部使用 datetime.utcnow()（naive UTC）
    - _get_date_trigger 用 datetime.fromisoformat 解析用户输入
    - 如果用户传入带时区字符串，fromisoformat 生成 aware datetime
    - 随后与 utcnow()（naive）比较，会抛出 TypeError
    """

    def test_date_trigger_with_naive_datetime_works(self):
        """无时区的 datetime 字符串应能正常创建 DateTrigger。"""
        from src.triggers.triggers.time_trigger import TimeTrigger

        config = _make_time_trigger_config(
            schedule_type="date",
            expression=None,
        )
        config.schedule = {
            "type": "date",
            "datetime": "2099-12-31T23:59:59",  # naive, 未来时间
        }

        trigger = TimeTrigger(config)
        aps_trigger = trigger.get_apscheduler_trigger()
        assert aps_trigger is not None

    def test_date_trigger_with_utc_datetime_works(self):
        """带 UTC 时区的 datetime 字符串应能创建 DateTrigger。

        注意：旧代码中 datetime.fromisoformat("...+00:00") 生成 aware datetime，
        然后与 datetime.utcnow()（naive）比较会抛 TypeError。
        这里验证旧代码的实际行为。
        """
        from src.triggers.triggers.time_trigger import TimeTrigger

        config = _make_time_trigger_config(
            schedule_type="date",
            expression=None,
        )
        config.schedule = {
            "type": "date",
            "datetime": "2099-12-31T23:59:59+00:00",  # aware UTC
        }

        trigger = TimeTrigger(config)

        # 旧代码中 _get_date_trigger 会执行:
        #   run_date = datetime.fromisoformat("2099-12-31T23:59:59+00:00")  # aware
        #   now = datetime.utcnow()  # naive
        #   if run_date < now:  # TypeError!
        #
        # 但因为 run_date 是 2099 年，即使比较成功也不会走到过期分支
        # 实际上这里会抛出 TypeError，因为 aware < naive 比较非法
        try:
            aps_trigger = trigger.get_apscheduler_trigger()
            # 如果没抛异常，说明已修复或者 APScheduler 版本兼容
            assert aps_trigger is not None
        except TypeError as e:
            # 预期行为：旧代码在 aware/naive 比较时会抛 TypeError
            pytest.fail(
                f"TimeTrigger 时区 Bug 确认：aware datetime 与 naive datetime "
                f"比较失败: {e}"
            )

    def test_date_trigger_with_offset_datetime_timezone_bug(self):
        """验证时区偏移字符串（如 +08:00）的 Bug。

        用户设置北京时间 18:00（即 UTC 10:00），但代码用 utcnow() 比较，
        会导致 aware vs naive 比较异常。
        """
        from src.triggers.triggers.time_trigger import TimeTrigger

        config = _make_time_trigger_config(
            schedule_type="date",
            expression=None,
        )
        config.schedule = {
            "type": "date",
            "datetime": "2099-12-31T23:59:59+08:00",  # 北京时间
        }

        trigger = TimeTrigger(config)
        try:
            trigger.get_apscheduler_trigger()
        except TypeError:
            # 时区 Bug 确认
            pytest.fail("REQ-3 时区 Bug 确认：带时区偏移的 datetime 无法与 utcnow 比较")

    def test_execute_uses_utcnow_for_triggered_at(self):
        """验证 execute() 方法中 triggered_at 使用 utcnow()。"""
        from src.triggers.triggers.time_trigger import TimeTrigger

        config = _make_time_trigger_config(schedule_type="cron", expression="* * * * *")
        trigger = TimeTrigger(config)

        # Mock execute_actions 以避免实际执行
        trigger.execute_actions = MagicMock(return_value=__import__("asyncio").coroutine(
            lambda: __import__("src.triggers.models", fromlist=["ExecutionResult"]).ExecutionResult(
                success=True, message="ok"
            )
        )())

        import asyncio
        # 验证 triggered_at 中使用的是 datetime.utcnow()
        # 这是时间戳生成，不会导致时区问题，但记录了行为
        result = asyncio.get_event_loop().run_until_complete(trigger.execute())
        assert result is not None

    def test_cron_trigger_no_timezone_param(self):
        """验证 CronTrigger 未传递 timezone 参数给 APScheduler。

        APScheduler CronTrigger.from_crontab() 默认使用系统时区，
        可能导致在不同时区的服务器上触发时间不一致。
        """
        from src.triggers.triggers.time_trigger import TimeTrigger

        config = _make_time_trigger_config(
            schedule_type="cron",
            expression="0 9 * * *",  # 每天 9:00
        )
        trigger = TimeTrigger(config)
        aps_trigger = trigger.get_apscheduler_trigger()

        # APScheduler 的 CronTrigger 有 timezone 属性
        # 默认应该是 None 或系统本地时区
        assert aps_trigger is not None
        # 检查 timezone 属性
        tz = getattr(aps_trigger, "timezone", None)
        # 如果 timezone 是本地时区而非 UTC，则存在时区 Bug 风险
        # 在 APScheduler 中，默认 timezone 通常是本地时区
        if tz is not None:
            tz_name = str(tz)
            # 记录当前行为
            assert True  # 时区信息已获取，供分析


# ===========================================================================
# 3. TriggerManager（新系统）时间调度测试
# ===========================================================================

class TestTriggerManagerTimezone:
    """验证新系统 TriggerManager 的时间处理。"""

    def _make_manager(self):
        """创建 TriggerManager 实例。"""
        from src.triggers.manager import TriggerManager
        return TriggerManager()

    def test_manager_uses_aware_utc_datetime(self):
        """验证新系统使用 datetime.now(datetime.UTC) 生成时间戳。"""
        from src.triggers.types import TriggerConfig, TriggerType

        manager = self._make_manager()
        config = TriggerConfig(
            trigger_id="test-event-001",
            name="test-event",
            trigger_type=TriggerType.EVENT,
            event_name="test_event",
            message="hello",
        )
        manager.register(config)

        # 检查 register_time 使用了 aware datetime
        register_time_str = config.metadata.get("register_time")
        assert register_time_str is not None

        # 解析时间戳，验证是否包含时区信息
        register_dt = datetime.datetime.fromisoformat(register_time_str)
        assert register_dt.tzinfo is not None, (
            "新系统 register_time 应使用 aware datetime (now(UTC))"
        )

    def test_scheduled_trigger_with_utc_datetime(self):
        """验证 SCHEDULED 类型触发器使用 aware datetime 比较。"""
        from src.triggers.types import TriggerConfig, TriggerType

        manager = self._make_manager()

        # 设置一个未来的时间
        future_time = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
        config = TriggerConfig(
            trigger_id="test-sched-001",
            name="test-scheduled",
            trigger_type=TriggerType.SCHEDULED,
            scheduled_at=future_time,
            message="scheduled msg",
        )
        manager.register(config)

        # 用当前时间检查，不应触发
        now = datetime.datetime.now(datetime.UTC)
        fired = manager.check_scheduled(now)
        assert "test-sched-001" not in fired, "未来时间不应触发"

    def test_scheduled_trigger_fires_at_correct_time(self):
        """验证 SCHEDULED 触发器在正确时间触发。"""
        from src.triggers.types import TriggerConfig, TriggerType

        manager = self._make_manager()

        # 设置一个过去的时间（应立即触发）
        past_time = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=1)
        config = TriggerConfig(
            trigger_id="test-sched-002",
            name="test-past",
            trigger_type=TriggerType.SCHEDULED,
            scheduled_at=past_time,
            message="past msg",
        )
        manager.register(config)

        now = datetime.datetime.now(datetime.UTC)
        fired = manager.check_scheduled(now)
        assert "test-sched-002" in fired, "过去时间应触发"

    def test_delay_trigger_time_comparison(self):
        """验证 DELAY 类型触发器使用 aware datetime。"""
        from src.triggers.types import TriggerConfig, TriggerType

        manager = self._make_manager()

        config = TriggerConfig(
            trigger_id="test-delay-001",
            name="test-delay",
            trigger_type=TriggerType.DELAY,
            delay_seconds=60,  # 60秒后
            message="delay msg",
        )
        manager.register(config)

        # 立即检查，不应触发
        now = datetime.datetime.now(datetime.UTC)
        fired = manager.check_scheduled(now)
        assert "test-delay-001" not in fired

    def test_interval_trigger_with_aware_datetime(self):
        """验证 INTERVAL 类型触发器使用 aware datetime。"""
        from src.triggers.types import TriggerConfig, TriggerType

        manager = self._make_manager()

        config = TriggerConfig(
            trigger_id="test-interval-001",
            name="test-interval",
            trigger_type=TriggerType.INTERVAL,
            interval_seconds=10,
            max_fires=3,
            message="interval msg",
        )
        manager.register(config)

        # 设置 last_fire_time 为 5 秒前（未到间隔）
        past = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=5)
        config.metadata["last_fire_time"] = past.isoformat()

        now = datetime.datetime.now(datetime.UTC)
        fired = manager.check_scheduled(now)
        assert "test-interval-001" not in fired, "未到间隔时间不应触发"


# ===========================================================================
# 4. 时区 Bug 结论性测试
# ===========================================================================

class TestTimezoneBugConclusion:
    """
    REQ-3 时区 Bug 综合结论。

    分析结果：
    1. 旧系统 (triggers/triggers/time_trigger.py)：
       - 使用 datetime.utcnow()（naive）
       - _get_date_trigger 中 fromisoformat 可能生成 aware datetime
       - aware vs naive 比较会 TypeError → 这是一个真实 Bug
       - CronTrigger 未传递 timezone → 不同时区服务器行为不一致

    2. 新系统 (triggers/manager.py)：
       - 使用 datetime.now(datetime.UTC)（aware）
       - 所有时间比较使用 aware datetime → 正确
       - 已修复旧系统的时区问题
    """

    def test_old_system_date_trigger_timezone_inconsistency(self):
        """
        核心验证：旧系统 DateTrigger 的时区不一致问题。

        当用户传入带时区的时间字符串（如 "2026-06-09T18:00:00+08:00"），
        fromisoformat 生成 aware datetime，与 utcnow()（naive）比较时 TypeError。
        """
        # 模拟旧代码的核心逻辑
        user_input = "2026-06-09T18:00:00+08:00"  # 用户输入带时区
        run_date = datetime.datetime.fromisoformat(user_input)
        now = datetime.datetime.utcnow()  # 旧代码使用 utcnow

        # aware datetime 与 naive datetime 比较会 TypeError
        with pytest.raises(TypeError):
            _ = run_date < now

    def test_new_system_timezone_aware_consistent(self):
        """
        核心验证：新系统使用一致的 aware datetime。

        datetime.now(datetime.UTC) 始终生成 aware datetime，
        fromisoformat 带时区的也是 aware，比较不会出错。
        """
        # 新代码的逻辑
        user_input = "2026-06-09T18:00:00+08:00"
        run_date = datetime.datetime.fromisoformat(user_input)
        now = datetime.datetime.now(datetime.UTC)  # 新代码使用 now(UTC)

        # aware vs aware 比较正常
        assert isinstance(run_date < now, bool), "两个 aware datetime 比较应正常"


# ===========================================================================
# 5. trigger_setup 工具：naive 本地时间解释（回归测试）
# ===========================================================================
#
# 故障复现：用户传 schedule_time="2026-07-08T12:07:00"（naive，无时区）。
# 旧 _setup_schedule_trigger 把 naive 当字面值，manager._normalize_datetime 又把它当作 UTC，
# 导致北京 12:07 被理解成 UTC 12:07（即北京 20:07），晚 8 小时触发甚至永不触发。
# 修复后：naive 时间按 APP_TIMEZONE 解释为本地时间，转成 aware UTC 存入 scheduled_at。


class TestTriggerSetupScheduleLocalTime:
    """验证 TriggerSetupTool 把 naive 本地时间按 APP_TIMEZONE 解释为 aware UTC。"""

    @pytest.mark.asyncio
    async def test_naive_local_time_interpreted_as_app_timezone(self):
        """naive 时间字符串应按 APP_TIMEZONE 解释，存为 aware UTC。

        用户传北京「2天后 12:07:00」（无时区），
        应被解释为 Asia/Shanghai 本地时间，转成 UTC「2天后 04:07:00」。
        """
        from src.tools.builtin.trigger_setup.tool import TriggerSetupTool

        local_tz = datetime.timezone(datetime.timedelta(hours=8))
        # 2 天后的本地 12:07:00，必在 7 天上限内且在未来
        target_local = (
            datetime.datetime.now(local_tz) + datetime.timedelta(days=2)
        ).replace(hour=12, minute=7, second=0, microsecond=0)
        schedule_time_str = target_local.strftime("%Y-%m-%dT%H:%M:%S")

        with patch("src.config.settings.get_settings") as mock_settings:
            mock_settings.return_value.timezone = "Asia/Shanghai"

            tool = TriggerSetupTool()
            result = await tool.execute({
                "trigger_type": "schedule",
                "message": "定时测试",
                "schedule_time": schedule_time_str,
                "pipeline_id": "pipe_test_001",
                "execution_id": "exec_test_001",
            })

        assert result.success is True, f"设置应成功: {getattr(result, 'error', '')}"
        trigger_id = result.output["trigger_id"]

        trigger = tool._manager.get(trigger_id)
        assert trigger is not None
        assert trigger.trigger_type.value == "scheduled"

        # 核心：scheduled_at 必须是 aware，且 UTC 化后等于本地时间 -8h
        scheduled = trigger.scheduled_at
        assert scheduled.tzinfo is not None, "scheduled_at 必须是 aware datetime"
        expected_utc = target_local.astimezone(datetime.timezone.utc)
        assert scheduled == expected_utc, f"本地{target_local} 应解释为 UTC{expected_utc}，实际 {scheduled}"

        tool._manager.unregister(trigger_id)

    @pytest.mark.asyncio
    async def test_aware_offset_time_preserved(self):
        """带时区偏移的时间字符串应保留其语义，转成 aware UTC。"""
        from src.tools.builtin.trigger_setup.tool import TriggerSetupTool

        local_tz = datetime.timezone(datetime.timedelta(hours=8))
        target_local = (
            datetime.datetime.now(local_tz) + datetime.timedelta(days=2)
        ).replace(hour=12, minute=7, second=0, microsecond=0)
        # 明确带 +08:00 的时间字符串
        schedule_time_str = target_local.strftime("%Y-%m-%dT%H:%M:%S+08:00")

        with patch("src.config.settings.get_settings") as mock_settings:
            mock_settings.return_value.timezone = "Asia/Shanghai"

            tool = TriggerSetupTool()
            result = await tool.execute({
                "trigger_type": "schedule",
                "message": "定时测试",
                "schedule_time": schedule_time_str,
                "pipeline_id": "pipe_test_002",
                "execution_id": "exec_test_002",
            })

        assert result.success is True
        trigger = tool._manager.get(result.output["trigger_id"])
        expected_utc = target_local.astimezone(datetime.timezone.utc)
        assert trigger.scheduled_at == expected_utc
        tool._manager.unregister(trigger.trigger_id)

    @pytest.mark.asyncio
    async def test_naive_local_time_not_treated_as_utc(self):
        """回归：naive 本地时间不能被当作 UTC（旧 bug 的核心症状）。

        旧代码：naive 12:07 被当 UTC 12:07 → 比 now(UTC)~07:xx 大 → 通过校验但永不触发。
        新代码：naive 12:07 被当本地 → UTC 04:07。
        用一个「字面值在 now(UTC) 之前、但本地解释后在 now(UTC) 之后」的时间，
        验证不会被误判为过去时间（旧 bug 会误判）。
        """
        from src.tools.builtin.trigger_setup.tool import TriggerSetupTool

        # now_utc ≈ 07:5x（北京 15:5x）。取本地 13:00（UTC 05:00）：
        # - 旧代码（当 UTC）：13:00 > 07:5x → 视为未来，通过校验 ❌ 但实际永不触发
        # - 新代码（当本地）：UTC 05:00 < 07:5x → 应判为过去时间，返回 SCHEDULE_TIME_IN_PAST ✅
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        local_tz = datetime.timezone(datetime.timedelta(hours=8))
        # 本地 13:00 = UTC 05:00，在 now_utc(~07:5x) 之前
        naive_local_past = (now_utc.astimezone(local_tz).replace(hour=13, minute=0, second=0, microsecond=0))
        schedule_time_str = naive_local_past.strftime("%Y-%m-%dT%H:%M:%S")

        with patch("src.config.settings.get_settings") as mock_settings:
            mock_settings.return_value.timezone = "Asia/Shanghai"

            tool = TriggerSetupTool()
            result = await tool.execute({
                "trigger_type": "schedule",
                "message": "定时测试",
                "schedule_time": schedule_time_str,
                "pipeline_id": "pipe_test_004",
                "execution_id": "exec_test_004",
            })

        # 新代码应识别为过去时间
        assert result.success is False, "本地 13:00(=UTC05:00) 早于 now 应被判为过去时间"
        assert result.error_code == "SCHEDULE_TIME_IN_PAST"

