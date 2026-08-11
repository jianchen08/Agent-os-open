"""触发器管理器。



管理触发器的注册、评估和执行，支持事件触发、条件触发和定时触发。

通过 ServiceProvider 获取管道引擎实例，触发时使用 inject_message 唤醒管道。



公共 API:

    TriggerManager: 触发器管理器类

    get_trigger_manager: 获取全局单例

"""

import asyncio
import datetime
import logging
import threading
import time
from typing import Any

from .types import TriggerConfig, TriggerStatus, TriggerType

logger = logging.getLogger(__name__)


_TRIGGER_CHECK_INTERVAL = 5.0


class TriggerManager:
    """触发器管理器。



    支持：

    - 注册/注销触发器

    - 评估事件触发器

    - 评估条件触发器

    - 检查定时/延迟/周期触发器

    - 按类型/状态查询触发器

    - 后台定期检查并唤醒管道

    """

    def __init__(self) -> None:
        """初始化管理器。"""

        self._triggers: dict[str, TriggerConfig] = {}

        self._check_thread: threading.Thread | None = None

        self._running = False

        self._main_loop: asyncio.AbstractEventLoop | None = None

    def register(self, config: TriggerConfig) -> None:
        """注册触发器。



        注册后自动将状态设为 ACTIVE，并启动后台检查循环（如果未运行）。



        Args:

            config: 触发器配置。

        """

        config.status = TriggerStatus.ACTIVE

        if "register_time" not in config.metadata:
            config.metadata["register_time"] = datetime.datetime.now(datetime.UTC).isoformat()

        if "last_fire_time" not in config.metadata:
            config.metadata["last_fire_time"] = None

        self._triggers[config.trigger_id] = config

        logger.info(
            f"注册触发器: {config.trigger_id} - {config.name} "
            f"(type={config.trigger_type.value}, max_fires={config.max_fires}, "
            f"max_time={config.max_time_seconds}s)"
        )

        self._ensure_check_loop()

    def unregister(self, trigger_id: str) -> bool:
        """注销触发器。



        Args:

            trigger_id: 触发器 ID。



        Returns:

            是否成功注销（False 表示触发器不存在）。

        """

        if trigger_id in self._triggers:
            del self._triggers[trigger_id]

            logger.info(f"注销触发器: {trigger_id}")

            return True

        return False

    def evaluate_event(self, event_name: str, event_data: dict[str, Any]) -> list[str]:
        """评估事件触发器。



        遍历所有 EVENT 类型的触发器，检查事件名称和数据是否匹配。

        匹配的触发器将 fire_count +1，达到 max_fires 时状态变为 FIRED。



        Args:

            event_name: 事件名称。

            event_data: 事件数据字典。



        Returns:

            被触发的 trigger_id 列表。

        """

        fired: list[str] = []

        for trigger in self._triggers.values():
            if trigger.trigger_type != TriggerType.EVENT:
                continue

            if trigger.status != TriggerStatus.ACTIVE:
                continue

            if trigger.event_name != event_name:
                continue

            if not self._match_event_filter(trigger, event_data):
                continue

            if not self._check_stop_conditions(trigger):
                continue

            trigger.fire_count += 1

            trigger.metadata["last_fire_time"] = datetime.datetime.now(datetime.UTC).isoformat()

            fired.append(trigger.trigger_id)

            if self._is_max_fires_reached(trigger):
                trigger.status = TriggerStatus.FIRED

            logger.debug(f"事件触发器触发: {trigger.trigger_id} (事件: {event_name}, 第 {trigger.fire_count} 次)")

        return fired

    def evaluate_condition(self, context: dict[str, Any]) -> list[str]:
        """评估条件触发器。



        在 context 命名空间中执行条件表达式，求值为 True 时触发。



        Args:

            context: 上下文变量字典，作为条件表达式的求值环境。



        Returns:

            被触发的 trigger_id 列表。

        """

        fired: list[str] = []

        for trigger in self._triggers.values():
            if trigger.trigger_type != TriggerType.CONDITION:
                continue

            if trigger.status != TriggerStatus.ACTIVE:
                continue

            if not trigger.condition_expression:
                continue

            if not self._check_stop_conditions(trigger):
                continue

            try:
                result = self._eval_condition(trigger.condition_expression, context)

                if result:
                    trigger.fire_count += 1

                    trigger.metadata["last_fire_time"] = datetime.datetime.now(datetime.UTC).isoformat()

                    fired.append(trigger.trigger_id)

                    if self._is_max_fires_reached(trigger):
                        trigger.status = TriggerStatus.FIRED

                    logger.info(f"条件触发器触发: {trigger.trigger_id} (表达式: {trigger.condition_expression})")

            except Exception as e:
                logger.warning(f"条件评估失败: {trigger.trigger_id}, 表达式: {trigger.condition_expression}, 错误: {e}")

        return fired

    def check_scheduled(self, now: datetime.datetime) -> list[str]:
        """检查定时/延迟/周期触发器。



        对于 DELAY 类型，检查从注册时刻起是否已过 delay_seconds。

        对于 SCHEDULED 类型，检查 scheduled_at 是否已到。

        对于 INTERVAL 类型，检查距离上次触发是否已过 interval_seconds。



        Args:

            now: 当前时间。



        Returns:

            被触发的 trigger_id 列表。

        """

        fired: list[str] = []

        for trigger in self._triggers.values():
            if trigger.status != TriggerStatus.ACTIVE:
                continue

            if not self._check_stop_conditions(trigger, now):
                trigger.status = TriggerStatus.FIRED

                continue

            should_fire = False

            if trigger.trigger_type == TriggerType.DELAY:
                should_fire = self._check_delay(trigger, now)

            elif trigger.trigger_type == TriggerType.SCHEDULED:
                should_fire = self._check_scheduled_time(trigger, now)

            elif trigger.trigger_type == TriggerType.INTERVAL:
                should_fire = self._check_interval(trigger, now)

            if should_fire:
                trigger.fire_count += 1

                trigger.metadata["last_fire_time"] = now.isoformat()

                fired.append(trigger.trigger_id)

                if self._is_max_fires_reached(trigger):
                    trigger.status = TriggerStatus.FIRED

                logger.debug(
                    f"触发器触发: {trigger.trigger_id} (type={trigger.trigger_type.value}, 第 {trigger.fire_count} 次)"
                )

        return fired

    def get(self, trigger_id: str) -> TriggerConfig | None:
        """按 ID 获取触发器。



        Args:

            trigger_id: 触发器 ID。



        Returns:

            触发器配置，不存在时返回 None。

        """

        return self._triggers.get(trigger_id)

    def list_by_type(self, trigger_type: TriggerType) -> list[TriggerConfig]:
        """按类型列出触发器。



        Args:

            trigger_type: 触发器类型。



        Returns:

            匹配的触发器列表。

        """

        return [t for t in self._triggers.values() if t.trigger_type == trigger_type]

    def list_active(self) -> list[TriggerConfig]:
        """列出所有活跃触发器。



        Returns:

            状态为 ACTIVE 的触发器列表。

        """

        return [t for t in self._triggers.values() if t.status == TriggerStatus.ACTIVE]

    def update_max_fires(self, trigger_id: str, max_fires: int, max_time_seconds: float | None = None) -> bool:
        """更新触发器的最大触发次数和最长运行时间。



        当多个任务共用同一个触发器时，可通过此方法延长触发器的生命周期。

        如果触发器已达到 FIRED 状态，会自动重新激活为 ACTIVE。



        Args:

            trigger_id: 触发器 ID。

            max_fires: 新的最大触发次数，0 表示无限。

            max_time_seconds: 新的最长运行时间（秒），None 表示不更新。



        Returns:

            是否成功更新。

        """

        trigger = self._triggers.get(trigger_id)

        if trigger is None:
            return False

        if trigger.status == TriggerStatus.CANCELLED:
            return False

        trigger.max_fires = max_fires

        if max_time_seconds is not None:
            trigger.max_time_seconds = max_time_seconds

        if trigger.status == TriggerStatus.FIRED:
            trigger.status = TriggerStatus.ACTIVE

        logger.info(
            f"更新触发器: {trigger_id} - "
            f"max_fires={max_fires}, max_time={max_time_seconds}s, "
            f"fire_count={trigger.fire_count}, status={trigger.status.value}"
        )

        return True

    def cancel(self, trigger_id: str) -> bool:
        """取消触发器。



        将状态设为 CANCELLED。



        Args:

            trigger_id: 触发器 ID。



        Returns:

            是否成功取消。

        """

        trigger = self._triggers.get(trigger_id)

        if trigger is None:
            return False

        if trigger.status in (TriggerStatus.FIRED, TriggerStatus.CANCELLED):
            return False

        trigger.status = TriggerStatus.CANCELLED

        return True

    def set_main_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """设置主事件循环引用。



        当触发器注册发生在 asyncio.run() 创建的临时事件循环中时，

        _ensure_check_loop 需要通过主循环的 call_soon_threadsafe

        将检查任务调度到主循环上执行，避免临时循环关闭后任务被取消。



        Args:

            loop: 应用主事件循环

        """

        self._main_loop = loop

        logger.info("[TriggerManager] 主事件循环已设置")

    def start_check_loop(self) -> None:
        """启动后台触发器检查循环。



        安全重复调用，已有运行中的任务时不重复创建。

        """

        self._ensure_check_loop()

    def stop_check_loop(self) -> None:
        """停止后台触发器检查循环。"""

        self._running = False

        self._check_thread = None

        logger.info("[TriggerManager] 后台检查循环已停止")

    def _check_loop_sync(self) -> None:
        """后台定期检查触发器，到期后通过 send_pipeline_message 注入消息。



        使用独立线程 + time.sleep，不依赖任何事件循环。

        send_pipeline_message 内部自动处理管道所有状态（运行中/挂起/已关闭）。

        _check_loop 运行在独立 threading.Thread + time.sleep 上，完全独立于事件循环，
        避免 trigger_setup 工具在临时事件循环上启动的 async task 随循环关闭而被取消。
        """

        logger.info("[TriggerManager] 后台检查循环已启动(线程)")

        self._running = True

        while self._running:
            time.sleep(_TRIGGER_CHECK_INTERVAL)

            if not self._running:
                break

            try:
                now = datetime.datetime.now(datetime.UTC)

                fired_ids = self.check_scheduled(now)

                for trigger_id in fired_ids:
                    trigger = self._triggers.get(trigger_id)

                    if trigger is None:
                        continue

                    if not trigger.pipeline_id or not trigger.message:
                        continue

                    try:
                        self._inject_trigger_message(trigger)

                    except Exception as e:
                        logger.error(
                            f"[TriggerManager] 注入消息异常: {e}",
                            exc_info=True,
                        )

            except Exception as e:
                logger.error(f"[TriggerManager] 检查循环异常: {e}", exc_info=True)

        self._running = False

        logger.info("[TriggerManager] 后台检查循环已退出(线程)")

    def _inject_trigger_message(self, trigger: TriggerConfig) -> None:
        """构造触发消息并通过 send_pipeline_message 统一注入。



        只需要 pipeline_id 和 message，所有状态处理由 send_pipeline_message 完成。



        Args:

            trigger: 已触发的触发器配置

        """

        loop = self._main_loop

        if loop is None or loop.is_closed():
            logger.warning(
                "[TriggerManager] 主事件循环不可用，跳过: trigger=%s pipeline=%s",
                trigger.trigger_id,
                trigger.pipeline_id,
            )

            return

        fire_info = f"[触发器通知] 触发器 '{trigger.name or trigger.trigger_id}' 已触发 (第 {trigger.fire_count} 次"

        if trigger.max_fires > 0:
            fire_info += f"/共 {trigger.max_fires} 次"

        fire_info += f")\n{trigger.message}"

        # ★ 获取 output_sink 以确保前端能收到事件

        _output_sink = None

        try:
            from pipeline.registry import get_engine_registry  # noqa: PLC0415

            _reg = get_engine_registry()

            _entry = _reg.get(trigger.pipeline_id)

            if _entry and _entry.bridge:
                _output_sink = _entry.bridge.output_sink

            if _output_sink is None:
                from pipeline.message_bus import _create_sink  # noqa: PLC0415

                _output_sink = _create_sink(trigger.pipeline_id)

        except Exception:
            pass

        from pipeline.message_bus import send_pipeline_message  # noqa: PLC0415
        from pipeline.message_types import MessageType, PipelineMessage  # noqa: PLC0415

        _trig_msg = PipelineMessage(
            type=MessageType.CHAT,
            content=fire_info,
            pipeline_id=trigger.pipeline_id,
            metadata={"source": "trigger", "trigger_id": trigger.trigger_id},
        )

        future = asyncio.run_coroutine_threadsafe(
            send_pipeline_message(
                _trig_msg,
                output_sink=_output_sink,
            ),
            loop,
        )

        try:
            result = future.result(timeout=30)

            if result.success:
                logger.info(
                    "[TriggerManager] 消息已注入: pipeline=%s method=%s trigger=%s fire_count=%d",
                    trigger.pipeline_id,
                    result.method,
                    trigger.trigger_id,
                    trigger.fire_count,
                )

            else:
                logger.warning(
                    "[TriggerManager] 消息注入失败: pipeline=%s trigger=%s error=%s",
                    trigger.pipeline_id,
                    trigger.trigger_id,
                    result.error,
                )

        except Exception as e:
            logger.error(
                "[TriggerManager] 消息注入异常: pipeline=%s trigger=%s error=%s",
                trigger.pipeline_id,
                trigger.trigger_id,
                e,
            )

    async def on_system_event(self, event_name: str, event_data: dict[str, Any]) -> list[str]:
        """接收系统事件并评估事件触发器。



        BUG-FIX-REQ-4:

        问题根因: evaluate_event 方法存在但无人调用。事件总线发布事件后

          没有桥接代码将事件转发给 TriggerManager.evaluate_event，

          导致 EVENT 类型触发器永远无法触发。

        修复方案: 提供统一的入口方法，供事件总线订阅处理器调用。

          同时提供 subscribe_to_event_bus 便捷方法自动桥接。

        影响范围: 所有 EVENT 类型的触发器（如 task_completed）。



        Args:

            event_name: 事件名称（如 task_completed, task_failed）

            event_data: 事件数据字典



        Returns:

            被触发的 trigger_id 列表

        """

        return self.evaluate_event(event_name, event_data)

    def subscribe_to_event_bus(self, event_bus: Any) -> None:
        """订阅事件总线，自动将状态变更事件桥接到事件触发器。



        将 STATE_CHANGE 类型的事件转换为事件名称（如 task_completed），

        然后调用 evaluate_event 评估匹配的触发器。



        Args:

            event_bus: 事件总线实例（需提供 subscribe 方法）

        """

        async def _on_state_change(event: Any) -> None:
            """状态变更事件处理器"""

            data = event.data if hasattr(event, "data") else {}

            new_status = data.get("new_status", "")

            if new_status:
                event_name = f"task_{new_status}"

                await self.on_system_event(event_name, data)

        try:
            event_bus.subscribe(
                handler=_on_state_change,
                event_filter=None,
            )

            logger.info("[TriggerManager] 已订阅事件总线")

        except Exception as e:
            logger.warning("[TriggerManager] 订阅事件总线失败: %s", e)

    def _ensure_check_loop(self) -> None:
        """确保后台检查线程正在运行。"""

        if self._check_thread is not None and self._check_thread.is_alive():
            return

        self._check_thread = threading.Thread(
            target=self._check_loop_sync,
            daemon=True,
            name="trigger-check",
        )

        self._check_thread.start()

    def _check_stop_conditions(self, trigger: TriggerConfig, now: datetime.datetime | None = None) -> bool:
        """检查触发器是否仍满足继续触发的条件。



        检查 max_time_seconds（最长运行时间）是否已超。



        Args:

            trigger: 触发器配置。

            now: 当前时间，None 时使用 now(datetime.UTC)。



        Returns:

            True 表示仍可继续触发，False 表示应停止。

        """

        if trigger.max_time_seconds > 0:
            register_time_str = trigger.metadata.get("register_time")

            if register_time_str:
                try:
                    register_time = datetime.datetime.fromisoformat(register_time_str)

                    check_time = now or datetime.datetime.now(datetime.UTC)

                    elapsed = (check_time - register_time).total_seconds()

                    if elapsed >= trigger.max_time_seconds:
                        logger.info(
                            f"[TriggerManager] 触发器 {trigger.trigger_id} "
                            f"已达最长运行时间 ({trigger.max_time_seconds}s)"
                        )

                        return False

                except (ValueError, TypeError):
                    pass

        return True

    def _is_max_fires_reached(self, trigger: TriggerConfig) -> bool:
        """检查是否达到最大触发次数。



        Args:

            trigger: 触发器配置。



        Returns:

            True 表示已达最大次数。

        """

        return trigger.max_fires > 0 and trigger.fire_count >= trigger.max_fires

    def _match_event_filter(self, trigger: TriggerConfig, event_data: dict[str, Any]) -> bool:
        """检查事件数据是否匹配过滤条件。



        Args:

            trigger: 触发器配置。

            event_data: 事件数据。



        Returns:

            是否匹配。

        """

        if not trigger.event_filter:
            return True

        for key, expected in trigger.event_filter.items():
            actual = event_data.get(key)

            if isinstance(expected, dict):
                op = expected.get("op", "eq")

                value = expected.get("value")

                if not self._compare(actual, op, value):
                    return False

            elif actual != expected:
                return False

        return True

    def _compare(  # noqa: PLR0911
        self, actual: Any, op: str, value: Any
    ) -> bool:
        """比较操作。



        支持 eq, ne, gt, lt, gte, lte, contains 操作符。



        Args:

            actual: 实际值。

            op: 操作符。

            value: 期望值。



        Returns:

            比较结果。

        """

        if op == "eq":
            return actual == value

        if op == "ne":
            return actual != value

        if op == "gt":
            return actual > value

        if op == "lt":
            return actual < value

        if op == "gte":
            return actual >= value

        if op == "lte":
            return actual <= value

        if op == "contains":
            return value in str(actual)

        return False

    def _eval_condition(self, expression: str, context: dict[str, Any]) -> bool:
        """安全地评估条件表达式。



        使用 condition_parser 替代 eval()，杜绝代码注入风险。



        Args:

            expression: 条件表达式字符串。

            context: 上下文变量字典。



        Returns:

            表达式求值结果。

        """

        from pipeline.condition_parser import parse_condition  # noqa: PLC0415

        return parse_condition(expression, context)

    def _check_delay(self, trigger: TriggerConfig, now: datetime.datetime) -> bool:
        """检查延迟触发器是否到期。



        通过 metadata 中的 register_time 计算是否已过 delay_seconds。



        Args:

            trigger: 触发器配置。

            now: 当前时间。



        Returns:

            是否到期。

        """

        if trigger.trigger_type != TriggerType.DELAY:
            return False

        if trigger.delay_seconds <= 0:
            return False

        register_time_str = trigger.metadata.get("register_time")

        if not register_time_str:
            return False

        try:
            register_time = datetime.datetime.fromisoformat(register_time_str)

            elapsed = (now - register_time).total_seconds()

            return elapsed >= trigger.delay_seconds

        except (ValueError, TypeError):
            return False

    def _check_scheduled_time(self, trigger: TriggerConfig, now: datetime.datetime) -> bool:
        """检查定时触发器是否到期。



        比较 scheduled_at 与当前时间。



        BUG-FIX-REQ-3:

        问题根因: scheduled_at 可能是 offset-naive 或 offset-aware，

          now 始终是 offset-aware（UTC），直接比较会抛出

          TypeError: can't compare offset-naive and offset-aware datetimes。

        修复方案: 统一归一化为 UTC aware datetime 后比较。

        影响范围: 所有 SCHEDULED 类型的触发器。



        Args:

            trigger: 触发器配置。

            now: 当前时间（UTC aware）。



        Returns:

            是否到期。

        """

        if trigger.trigger_type != TriggerType.SCHEDULED:
            return False

        if trigger.scheduled_at is None:
            return False

        scheduled = trigger.scheduled_at

        # 时区归一化：统一为 UTC aware

        now_normalized = self._normalize_datetime(now)

        scheduled_normalized = self._normalize_datetime(scheduled)

        return now_normalized >= scheduled_normalized

    @staticmethod
    def _normalize_datetime(dt: datetime.datetime) -> datetime.datetime:
        """将 datetime 归一化为 UTC aware。



        - naive → 视为 UTC，添加时区信息

        - aware → 转换为 UTC



        Args:

            dt: 输入 datetime



        Returns:

            UTC aware datetime

        """

        if dt.tzinfo is None:
            return dt.replace(tzinfo=datetime.timezone.utc)

        return dt.astimezone(datetime.timezone.utc)

    def _check_interval(self, trigger: TriggerConfig, now: datetime.datetime) -> bool:
        """检查周期触发器是否到期。



        基于 last_fire_time + interval_seconds 计算下次触发时间。

        首次触发基于 register_time + interval_seconds。



        Args:

            trigger: 触发器配置。

            now: 当前时间。



        Returns:

            是否到期。

        """

        if trigger.trigger_type != TriggerType.INTERVAL:
            return False

        if trigger.interval_seconds <= 0:
            return False

        last_fire_str = trigger.metadata.get("last_fire_time")

        if trigger.fire_count == 0 or not last_fire_str:
            reference_str = trigger.metadata.get("register_time")

            if not reference_str:
                return False

            try:
                reference_time = datetime.datetime.fromisoformat(reference_str)

            except (ValueError, TypeError):
                return False

        else:
            try:
                reference_time = datetime.datetime.fromisoformat(last_fire_str)

            except (ValueError, TypeError):
                return False

        next_fire_time = reference_time + datetime.timedelta(seconds=trigger.interval_seconds)

        return now >= next_fire_time


_trigger_manager: TriggerManager | None = None


def get_trigger_manager() -> TriggerManager:
    """获取全局 TriggerManager 单例。



    Returns:

        TriggerManager 实例

    """

    global _trigger_manager  # noqa: PLW0603

    if _trigger_manager is None:
        _trigger_manager = TriggerManager()

    return _trigger_manager
