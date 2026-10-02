"""触发器管理器。

管理触发器的注册、评估和执行，支持事件触发和定时触发（延迟/定时/周期）。
CONDITION 触发器的条件求值上收内核 trigger-svc（M1）：本管理器只负责
注册面转调与点火消费（trigger.fired → 动作分发 + ack），不再持有
条件表达式求值、边沿簿记与 state 轮询。

公共 API:

    TriggerManager: 触发器管理器类

    get_trigger_manager: 获取全局单例

"""

import asyncio
import datetime
import inspect
import json
import locale
import logging
import os
import signal
import subprocess
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .types import TriggerConfig, TriggerStatus, TriggerType

logger = logging.getLogger(__name__)

# POSIX 终止信号：SIGKILL（Windows 无此常量，getattr 兜底常值 9；仅 POSIX
# 分支运行期触达）。
_POSIX_SIGKILL = getattr(signal, "SIGKILL", 9)

_TRIGGER_CHECK_INTERVAL = 5.0

# 触发器注册表在目标管道 state 中的键前缀（task.* 是 pipeline-state.update
# 写面的任务域白名单前缀；每触发器一键，覆盖写免读改写竞态）。
# 一次性迁移依据（M1 求值上收内核）：dev 库 agentos_kernel.db 核查
# `task.trigger.registry.*` 行为 0 条（2026-10-02 只读查询），插件侧 CONDITION
# 求值退役无存量迁移面；注册表持久化机制本身保留（触发器定义仍归插件持久化）。
TRIGGER_STATE_KEY_PREFIX = "task.trigger.registry."

# 工作线程落 state 的阻塞等待上限（触发频度 ≥ 间隔秒级，远低于此）。
_STATE_WRITE_TIMEOUT = 10.0

# 自动父通知自激熔断参数（2026-09-29/30 夜实证：task=9677f0f1c58a 失败通知
# 以 18 秒周期重复注入 23 次——父层自动重派→子任务再失败→再通知闭环）。
# 同键 (parent_pipeline_id, task_id, event) 滑动窗内：第 1 次正常注入、
# 第 2 次注入附重复计数提示、第 3 次起熔断不再注入；窗滚过后重新计数。
_AUTO_NOTIFY_WINDOW_SECONDS = 600.0
_AUTO_NOTIFY_BREAK_THRESHOLD = 3
# 熔断窗口键 LRU 上限（防键空间无界增长；逐出即状态重置，重新从首报起算）。
_AUTO_NOTIFY_MAX_KEYS = 500

# 内核 trigger-svc reconcile 对账周期（秒）。可靠性职责：替代原 5s 条件轮询的
# 兜底重投（at-least-once），轻量 RPC 无 state 拉取。可经环境变量覆盖。
_RECONCILE_INTERVAL_SECS = 60.0
_RECONCILE_INTERVAL_ENV = "AGENTOS_TRIGGER_RECONCILE_SECS"

# 已处理点火 (trigger_id, fire_seq) 去重缓存上限（LRU；防对账重投重复分发）。
_DELIVERED_FIRE_CACHE_MAX = 1024


def _reconcile_interval_from_env() -> float:
    """读取对账周期配置：环境变量优先，非法/非正值回退默认。"""
    raw = os.environ.get(_RECONCILE_INTERVAL_ENV, "").strip()
    if not raw:
        return _RECONCILE_INTERVAL_SECS
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "[TriggerManager] %s=%r 非法，回退默认 %.0fs",
            _RECONCILE_INTERVAL_ENV,
            raw,
            _RECONCILE_INTERVAL_SECS,
        )
        return _RECONCILE_INTERVAL_SECS
    if value <= 0:
        logger.warning(
            "[TriggerManager] %s=%r 必须为正，回退默认 %.0fs",
            _RECONCILE_INTERVAL_ENV,
            raw,
            _RECONCILE_INTERVAL_SECS,
        )
        return _RECONCILE_INTERVAL_SECS
    return value


class TriggerManager:
    """触发器管理器。



    支持：

    - 注册/注销触发器

    - 评估事件触发器

    - CONDITION 触发器：注册转调内核 trigger-svc，点火消费（trigger.fired）

    - 检查定时/延迟/周期触发器

    - 按类型/状态查询触发器

    - 后台定期检查并唤醒管道

    """

    def __init__(
        self,
        clock: Callable[[], float] | None = None,
        reconcile_interval: float | None = None,
    ) -> None:
        """初始化管理器。

        ``clock`` 供时序测试注入可推进单调钟，默认 time.monotonic；
        ``reconcile_interval`` 供测试注入对账周期（秒），默认读环境变量
        （``AGENTOS_TRIGGER_RECONCILE_SECS``），未配置取 60。
        """

        self._triggers: dict[str, TriggerConfig] = {}

        self._check_thread: threading.Thread | None = None

        self._running = False

        self._main_loop: asyncio.AbstractEventLoop | None = None

        # loop 内直接调度的注入任务（http REST 手动触发路径），持引用防 GC 取消。
        self._loop_tasks: set[asyncio.Task[Any]] = set()

        # sidecar 注入器：经内核 chat.send_message capability 投递触发消息并跑管道。
        # server.py on_load 时注入；为 None 时进程内回退不可用
        # （pipeline.message_bus 已删，注入时显式记录错误后放弃）。
        self._injector: Callable[..., Any] | None = None

        # state 聚合行读面（server.py on_load 注入，经内核 pipeline-state
        # capability 读 /api/v1/pipelines/state 同构数据）。M1 起仅供
        # load_from_state 重灌与 http_api GET /pipelines 消费——CONDITION
        # 求值已上收内核 trigger-svc，不再服务条件轮询。
        # 返回 list[dict]（扁平点号键行），可为 sync 或 async 可调用。
        # None = 桥未就绪（重灌 fail-visible 报错）。
        self._state_provider: Callable[..., Any] | None = None

        # P25 state 权威持久化写面（server.py on_load 注入，经内核
        # pipeline-state.update capability 写目标管道 state）。约定签名：
        # ``async def writer(pipeline_id: str, fields: dict[str, Any]) -> None``。
        # None = 桥未就绪（内存注册仍成立，重启后退化为进程内注册表）。
        self._state_writer: Callable[..., Any] | None = None

        # GAP-2 EVENT：域事件桥就绪标记（server.py on_load 注册 on_domain_event
        # 处理器后置 True——manifest 声明 domain_event hook 内核才会推送）。
        self._event_bridge_ready: bool = False

        # 自动父通知熔断状态：键 (parent_pipeline_id, task_id, event) → 窗内
        # 注入时刻 deque。内存态即可（sidecar 进程内；跨重启丢失可接受——
        # 重启后重新从第 1 次起算，影响有限）。OrderedDict 兼作 LRU（上限
        # _AUTO_NOTIFY_MAX_KEYS），deque 老化按滑动窗 _AUTO_NOTIFY_WINDOW_SECONDS。
        self._auto_notify_windows: OrderedDict[tuple[str, str, str], deque[float]] = OrderedDict()
        self._clock: Callable[[], float] = clock or time.monotonic

        # M1 求值上收内核：trigger-svc capability 提供者（server.py on_load 注入，
        # 约定 ``provider() -> handle | None``，句柄懒解析）。None = 未接线，
        # CONDITION 注册转调/点火 ack 一律 fail-visible 报错。
        self._trigger_svc_provider: Callable[[], Any] | None = None
        # 内核注册的归属插件 id（register.owner_plugin_id，内核按此归因/鉴权）。
        self._trigger_svc_owner: str = ""

        # 已处理点火缓存：(trigger_id, fire_seq) → None（OrderedDict 兼作 LRU）。
        # 分发成功（或判定无需分发）才标记；命中即跳过分发、只补 ack。
        self._delivered_fires: OrderedDict[tuple[str, int], None] = OrderedDict()

        # reconcile 对账循环（主事件循环上的 asyncio 任务；60s 可配）。
        self._reconcile_task: asyncio.Task[Any] | None = None
        self._reconcile_running = False
        self._reconcile_interval = (
            _reconcile_interval_from_env() if reconcile_interval is None else reconcile_interval
        )

    def register(self, config: TriggerConfig) -> None:
        """注册触发器。

        注册后自动将状态设为 ACTIVE，并启动后台检查循环（如果未运行）。
        CONDITION 触发器的条件表达式校验与求值在内核 trigger-svc
        （注册期由 ``register_kernel_condition`` 转调内核完成，语法错误在
        注册点显式报错），本方法不再做本地编译校验。

        Args:

            config: 触发器配置。
        """

        config.status = TriggerStatus.ACTIVE

        if "register_time" not in config.metadata:
            config.metadata["register_time"] = datetime.datetime.now(datetime.UTC).isoformat()

        if "last_fire_time" not in config.metadata:
            config.metadata["last_fire_time"] = None

        self._triggers[config.trigger_id] = config

        self.persist(config)

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
            # 写面无键删除语义：注销以 CANCELLED 终态落 state（重灌跳过终态）
            self._triggers[trigger_id].status = TriggerStatus.CANCELLED
            self.persist(self._triggers[trigger_id])
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

            self.persist(trigger)

            logger.debug(f"事件触发器触发: {trigger.trigger_id} (事件: {event_name}, 第 {trigger.fire_count} 次)")

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

                self.persist(trigger)

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

    def list_all(self) -> list[TriggerConfig]:
        """全量触发器（注册序）。"""
        return list(self._triggers.values())

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

        self.persist(trigger)

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

        self.persist(trigger)

        return True

    def fire_manually(self, trigger_id: str) -> bool:
        """手动触发一次（REST /trigger 端点）。

        与检查循环的到期触发互相独立：立即执行注入投递并累计
        fire_count，不校验最大次数/时长等停止条件（语义为"现在就跑一次"）。
        """
        trigger = self._triggers.get(trigger_id)
        if trigger is None:
            return False
        trigger.fire_count += 1
        self.persist(trigger)
        self._inject_trigger_message(trigger)
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

    def set_injector(self, injector: Callable[..., Any]) -> None:
        """注入 0.2 sidecar 触发消息投递器。

        到期触发时，``_inject_trigger_message`` 会经此注入器把消息投给内核
        （``chat.send_message`` capability），复用前端 WS 派发路径唤醒 agent。
        约定签名：``async def injector(pipeline_id: str, message: str, user_id: str) -> Any``；
        成功返回任意值，失败抛异常（由调用方记录）。sidecar 未注入时回退 0.1
        进程内 ``pipeline.message_bus``（0.2 下不可用，仅作兼容）。

        Args:
            injector: async 投递协程工厂。
        """
        self._injector = injector
        logger.info("[TriggerManager] 触发消息注入器已设置 (chat.send_message)")

    def set_state_provider(self, provider: Callable[..., Any]) -> None:
        """注入 state 聚合行读面（M1 起仅供注册表重灌与 REST 管道选项消费）。

        CONDITION 求值已上收内核 trigger-svc，state 读面不再服务条件轮询；
        现存消费方：``load_from_state``（sidecar 重启后从 pipeline_state 表
        重灌注册表）与 http_api ``GET /pipelines``（目标管道下拉选项）。
        约定签名：``provider() -> list[dict]``（sync 或 async；行为扁平点号
        键行，如 ``{"pipeline_id": ..., "task.status": ...}``）。

        Args:

            provider: state 聚合行提供者。

        """
        self._state_provider = provider
        logger.info("[TriggerManager] state 聚合读面已设置 (pipeline-state.list)")

    def is_state_provider_ready(self) -> bool:
        """state 读面是否就绪（重灌与 REST 管道选项依赖）。"""
        return self._state_provider is not None

    def set_state_writer(self, writer: Callable[..., Any]) -> None:
        """注入 state 权威持久化写面（P25：注册表落目标管道 state）。

        约定签名：``async def writer(pipeline_id: str, fields: dict[str, Any]) -> None``；
        生产形态为内核 ``pipeline-state.update`` capability（server.py on_load
        接线，任务域 ``task.*`` 键白名单）。写面未注入时注册表退化为纯内存
        （重启后丢失，注册期 warning 留痕）。

        Args:
            writer: state 写面协程工厂。

        """
        self._state_writer = writer
        logger.info("[TriggerManager] state 持久化写面已设置 (pipeline-state.update)")

    # ── M1 求值上收内核：trigger-svc 注册转调 + 点火消费 + 对账兜底 ─────
    #
    # 契约（capability "trigger-svc"，与内核侧共同钉死）：
    #   register   {trigger_id, condition, pipeline_id?, watched_keys?, owner_plugin_id}
    #              → {ok, seed_fired}（注册时种子求值即命中 → seed_fired=true）
    #   unregister {trigger_id} → {ok}
    #   reconcile  {} → {fires: [{trigger_id, fire_seq, fired_at, keys}]}（未 ack 的火，老→新）
    #   ack        {fire_ids: [i64]} → {ok}
    #   点火通知：域事件 "trigger.fired"，tags {trigger_id, fire_seq, view, keys}
    # 语义：边沿 false→true 才点火（内核注册时种子求值）；求值异常内核侧视为
    # False + debug 留痕。watched_keys 需解析表达式提取键——Python 侧解析器已随
    # 求值上收退休，M1 不传（交给内核默认求值节奏，键级索引优化属内核侧）。
    # M1 已知限制：CONDITION 求值仅 committed 视图（staged 视图属 M2）。

    def set_trigger_svc_provider(self, provider: Callable[[], Any] | None, owner_plugin_id: str) -> None:
        """注入内核 trigger-svc 能力提供者与归属插件 id（server.py on_load 接线）。

        Args:
            provider: ``provider() -> handle | None``（句柄懒解析；内核未注入
                该 capability 时返回 None）。
            owner_plugin_id: 本插件 manifest id（register.owner_plugin_id）。
        """
        self._trigger_svc_provider = provider
        self._trigger_svc_owner = owner_plugin_id
        logger.info("[TriggerManager] trigger-svc 能力通道已接线 (owner=%s)", owner_plugin_id)

    def is_trigger_svc_ready(self) -> bool:
        """trigger-svc 通道是否接线（provider 已注入）。"""
        return self._trigger_svc_provider is not None

    def _trigger_svc_handle(self) -> Any:
        """解析 trigger-svc 能力句柄；未接线/内核未注入一律 fail-visible 抛错。"""
        if self._trigger_svc_provider is None:
            raise RuntimeError(
                "trigger-svc 通道未接线（server.py on_load 缺 set_trigger_svc_provider）"
            )
        handle = self._trigger_svc_provider()
        if handle is None:
            raise RuntimeError(
                "内核未注入 trigger-svc capability（请检查 plugin.json granted_capabilities "
                "与内核版本）"
            )
        return handle

    async def register_kernel_condition(self, trigger_id: str, condition: str, pipeline_id: str) -> bool:
        """CONDITION 触发器创建：转调内核 trigger-svc.register。

        内核在注册期做表达式语法校验并对当前 state 种子求值一次；表达式非法
        或内核不可用 → 抛 RuntimeError（调用方把注册失败报给调用者，绝不静默
        注册一个永不触发的触发器）。

        Args:
            trigger_id: 触发器 ID。
            condition: 条件表达式（内核 condition.rs 文法）。
            pipeline_id: 目标管道 ID（内核按管道 state 求值）。

        Returns:
            seed_fired：注册时种子求值即命中（条件当前已为真，内核已按边沿
            语义点火一次，随 trigger.fired 通知送达）。
        """
        params: dict[str, Any] = {
            "trigger_id": trigger_id,
            "condition": condition,
            "owner_plugin_id": self._trigger_svc_owner,
        }
        if pipeline_id:
            params["pipeline_id"] = pipeline_id
        result = await self._trigger_svc_handle().call("register", params)
        if not isinstance(result, dict) or not result.get("ok"):
            raise RuntimeError(f"trigger-svc.register 返回异常: {result!r}")
        return bool(result.get("seed_fired"))

    async def unregister_kernel_condition(self, trigger_id: str) -> None:
        """CONDITION 触发器取消/删除：转调内核 trigger-svc.unregister。

        失败抛 RuntimeError——调用方中止本地取消（两侧保持一致，用户可重试），
        避免留下"本地已取消、内核继续点火"的静默漏注册。
        """
        result = await self._trigger_svc_handle().call("unregister", {"trigger_id": trigger_id})
        if not isinstance(result, dict) or not result.get("ok"):
            raise RuntimeError(f"trigger-svc.unregister 返回异常: {result!r}")

    async def _ack_fires(self, fire_ids: list[int]) -> None:
        """确认内核点火（fire log 出队，终止 reconcile 重投）。"""
        result = await self._trigger_svc_handle().call("ack", {"fire_ids": fire_ids})
        if not isinstance(result, dict) or not result.get("ok"):
            raise RuntimeError(f"trigger-svc.ack 返回异常: {result!r}")

    def _is_fire_delivered(self, key: tuple[str, int]) -> bool:
        return key in self._delivered_fires

    def _mark_fire_delivered(self, key: tuple[str, int]) -> None:
        """标记点火已处理（LRU 有界；逐出后如遇重投将再次尝试分发+ack）。"""
        self._delivered_fires[key] = None
        self._delivered_fires.move_to_end(key)
        while len(self._delivered_fires) > _DELIVERED_FIRE_CACHE_MAX:
            self._delivered_fires.popitem(last=False)

    async def handle_trigger_fired(self, params: dict[str, Any]) -> None:
        """内核 ``trigger.fired`` 点火消费：动作分发 + 成功后 ack（at-least-once）。

        通知 tags / reconcile fire 记录同形：``{trigger_id, fire_seq, fired_at?,
        keys?, view?}``。分发语义与既有检查循环一致：command → daemon 线程
        （``_dispatch_trigger_action``），notify → await 注入器。

        ack 时机：分发成功（或判定无需/无法经重投自愈：触发器已消失、已达
        上限、缺投递信息）后 ack；注入失败不 ack 不标记——reconcile 周期重投，
        语义至少触发一次（重复注入在 at-least-once 语义内）。去重缓存命中
        （同 fire_seq 已分发）只补 ack，不重复分发。

        Raises:
            RuntimeError: fire_seq 缺失/不可解析、注入器未设置、注入失败、
                trigger-svc 不可用——调用方留痕，reconcile 下一轮重投。
        """
        trigger_id = str(params.get("trigger_id") or "")
        fire_seq_raw = params.get("fire_seq")
        try:
            fire_seq = int(fire_seq_raw)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"trigger.fired 缺少可解析的 fire_seq: {fire_seq_raw!r}") from exc

        key = (trigger_id, fire_seq)
        if self._is_fire_delivered(key):
            # 同一火经通知与 reconcile 双通道到达：只补 ack，不重复分发
            await self._ack_fires([fire_seq])
            return

        trigger = self._triggers.get(trigger_id)
        if trigger is None:
            # 本地注册表无此触发器（已取消/未重灌）：重投不会自愈，ack 终结
            logger.warning(
                "[TriggerManager] trigger.fired 指向未知触发器，ack 终结重投: trigger=%s fire_seq=%s",
                trigger_id,
                fire_seq,
            )
            self._mark_fire_delivered(key)
            await self._ack_fires([fire_seq])
            return

        if not self._check_stop_conditions(trigger):
            trigger.status = TriggerStatus.FIRED
            self.persist(trigger)
            self._mark_fire_delivered(key)
            await self._ack_fires([fire_seq])
            return

        if trigger.status != TriggerStatus.ACTIVE or self._is_max_fires_reached(trigger):
            # 停用/已达上限：不计数不分发，ack 终结重投（内核无插件侧停止语义）
            self._mark_fire_delivered(key)
            await self._ack_fires([fire_seq])
            return

        if not self._dispatch_trigger_action(trigger):
            if not trigger.pipeline_id or not trigger.message:
                logger.error(
                    "[TriggerManager] 触发器缺 pipeline_id/message，无法投递，ack 终结重投: "
                    "trigger=%s fire_seq=%s",
                    trigger_id,
                    fire_seq,
                )
                self._mark_fire_delivered(key)
                await self._ack_fires([fire_seq])
                return
            if self._injector is None:
                raise RuntimeError(
                    "注入器未设置，trigger.fired 无法投递（不 ack，留待 reconcile 重投）"
                )
            user_id = (trigger.metadata or {}).get("user_id", "")
            try:
                await self._injector(
                    trigger.pipeline_id,
                    self._format_fire_info(trigger),
                    user_id,
                )
            except Exception as exc:
                raise RuntimeError(
                    f"触发消息注入失败（不 ack，留待 reconcile 重投）: {exc}"
                ) from exc
            logger.info(
                "[TriggerManager] 条件触发消息已注入: pipeline=%s trigger=%s fire_seq=%s",
                trigger.pipeline_id,
                trigger_id,
                fire_seq,
            )

        # 分发成功：簿记 + 持久化 + 去重标记 + ack
        trigger.fire_count += 1
        trigger.metadata["last_fire_time"] = datetime.datetime.now(datetime.UTC).isoformat()
        if self._is_max_fires_reached(trigger):
            trigger.status = TriggerStatus.FIRED
        self.persist(trigger)
        self._mark_fire_delivered(key)
        await self._ack_fires([fire_seq])

    async def reconcile_once(self) -> int:
        """对账兜底单轮：拉取内核未 ack 的火并逐条补分发（去重后）。

        轻量 RPC，无 state 拉取——替代原 5s 条件轮询的可靠性职责。单条火
        处理失败留痕继续（下一轮 reconcile 重投），返回本轮内核给出的火数。
        """
        result = await self._trigger_svc_handle().call("reconcile", {})
        fires = result.get("fires") if isinstance(result, dict) else None
        if not isinstance(fires, list):
            raise RuntimeError(f"trigger-svc.reconcile 返回形状异常: {result!r}")
        for fire in fires:
            if not isinstance(fire, dict):
                logger.warning("[TriggerManager] reconcile 火记录形状非法，跳过: %r", fire)
                continue
            try:
                await self.handle_trigger_fired(fire)
            except Exception as exc:
                logger.error(
                    "[TriggerManager] reconcile 补投失败（下一轮重试）: fire=%s error=%s",
                    fire,
                    exc,
                )
        return len(fires)

    def start_reconcile_loop(self) -> None:
        """启动 reconcile 对账循环（主事件循环上的 asyncio 任务；安全重复调用）。"""
        if self._reconcile_task is not None and not self._reconcile_task.done():
            return
        loop = self._main_loop
        if loop is None or loop.is_closed():
            logger.warning("[TriggerManager] 主事件循环未设置，reconcile 对账循环未启动")
            return
        self._reconcile_running = True
        self._reconcile_task = loop.create_task(self._reconcile_loop())

    def stop_reconcile_loop(self) -> None:
        """停止 reconcile 对账循环（取消在途任务，尽快退出）。"""
        self._reconcile_running = False
        task = self._reconcile_task
        self._reconcile_task = None
        if task is not None and not task.done() and not task.get_loop().is_closed():
            task.cancel()

    def is_reconcile_loop_running(self) -> bool:
        """对账循环是否在运行（on_load 启动 / on_unload 停止）。"""
        return self._reconcile_running

    async def _reconcile_loop(self) -> None:
        """对账循环主体：周期调 reconcile_once，失败留痕下一轮重试。"""
        logger.info(
            "[TriggerManager] reconcile 对账循环已启动 (间隔 %.0fs)", self._reconcile_interval
        )
        while self._reconcile_running:
            await asyncio.sleep(self._reconcile_interval)
            if not self._reconcile_running:
                break
            try:
                count = await self.reconcile_once()
                if count:
                    logger.info(
                        "[TriggerManager] reconcile 对账: %d 条未 ack 火已补分发", count
                    )
            except Exception as e:
                logger.error(
                    "[TriggerManager] reconcile 对账失败（下一轮重试）: %s", e
                )
        logger.info("[TriggerManager] reconcile 对账循环已退出")

    def persist(self, config: TriggerConfig) -> None:
        """触发器配置同步落目标管道 state（权威持久层，覆盖写单键）。

        state 是权威层（pipeline_state 表跨 sidecar 重启/迁移/回收），内存
        注册表是运行缓存：register/cancel/update_max_fires/fire 计数变更后
        调用本方法保持两侧一致。写面未接通或写失败时内存注册仍成立（运行
        真值），降级留 error 痕迹——重启后该触发器退回上次持久化值。
        sync/async 上下文自适应：已在事件循环内（tool/REST 主循环）调度不
        阻塞；工作线程（检查循环）经 run_coroutine_threadsafe 等待落盘。

        Args:
            config: 待持久化的触发器配置。

        """
        writer = self._state_writer
        if writer is None:
            return
        pipeline_id = config.pipeline_id
        if not pipeline_id:
            return
        try:
            fields = {f"{TRIGGER_STATE_KEY_PREFIX}{config.trigger_id}": config.to_state_dict()}
        except Exception as e:
            logger.error(
                "[TriggerManager] 触发器序列化失败，state 未落: trigger=%s error=%s",
                config.trigger_id,
                e,
            )
            return

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None

        if running is not None:
            # 已在事件循环内：阻塞等待会同循环自锁，调度后台任务（完成时留痕）
            def _on_state_write_done(task: asyncio.Task[Any]) -> None:
                self._loop_tasks.discard(task)
                if not task.cancelled() and task.exception() is not None:
                    logger.error(
                        "[TriggerManager] 触发器 state 落盘异常: trigger=%s error=%s",
                        config.trigger_id,
                        task.exception(),
                    )

            task = running.create_task(writer(pipeline_id, fields))
            self._loop_tasks.add(task)
            task.add_done_callback(_on_state_write_done)
            return

        loop = self._main_loop
        if loop is None or loop.is_closed():
            logger.warning(
                "[TriggerManager] 主事件循环不可用，state 暂缓落盘: trigger=%s",
                config.trigger_id,
            )
            return
        try:
            asyncio.run_coroutine_threadsafe(
                writer(pipeline_id, fields), loop
            ).result(timeout=_STATE_WRITE_TIMEOUT)
        except Exception as e:
            logger.error(
                "[TriggerManager] 触发器 state 落盘失败（内存注册仍成立，重启后退回"
                "上次持久化值）: trigger=%s pipeline=%s error=%s",
                config.trigger_id,
                pipeline_id,
                e,
            )

    async def load_from_state(self) -> int:
        """从管道 state 全量重灌触发器注册表（on_load / 初始化时调用）。

        扫描 pipeline-state.list 聚合行的 ``task.trigger.registry.*`` 键：
        行存在即目标管道仍有活键 → 灌入；终态（CANCELLED/FIRED/EXPIRED）
        不灌（不复活死触发器，随管道清理天然 GC——残留字段与管道 state 同
        生共死，无独立删除面）；state 值为 JSON 字符串（跨边界序列化形态）
        或腐败值时按统一读取契约还原/跳过留痕。重灌以 state 为权威整体替换
        内存注册表（含清理已消失管道的陈旧内存项）。

        Returns:
            灌入的触发器数量。

        Raises:
            RuntimeError: state 聚合读面未注入（fail-visible，不静默空灌）。

        """
        rows = await self.collect_state_rows()

        restored: dict[str, TriggerConfig] = {}
        for row in rows:
            pipeline_id = str(row.get("pipeline_id") or "")
            if not pipeline_id:
                continue
            for key, value in row.items():
                if not key.startswith(TRIGGER_STATE_KEY_PREFIX):
                    continue
                data = self._coerce_state_value(value)
                if data is None:
                    logger.warning(
                        "[TriggerManager] 触发器 state 字段形态非法，跳过: pipeline=%s key=%s",
                        pipeline_id,
                        key,
                    )
                    continue
                try:
                    cfg = TriggerConfig.from_state_dict(data)
                except Exception as e:
                    logger.warning(
                        "[TriggerManager] 触发器 state 反序列化失败，跳过: pipeline=%s key=%s error=%s",
                        pipeline_id,
                        key,
                        e,
                    )
                    continue
                if not cfg.trigger_id:
                    continue
                cfg.pipeline_id = pipeline_id  # 存储管道即权威目标管道
                if cfg.status in (TriggerStatus.CANCELLED, TriggerStatus.FIRED, TriggerStatus.EXPIRED):
                    continue
                restored[cfg.trigger_id] = cfg

        self._triggers = restored
        logger.info(
            "[TriggerManager] state 重灌完成: %d 个触发器（扫描 %d 行）",
            len(restored),
            len(rows),
        )
        return len(restored)

    @staticmethod
    def _coerce_state_value(value: Any) -> dict[str, Any] | None:
        """state 字段值 → dict（原生 dict 直收；JSON 字符串还原；其余 None）。

        与 plugins/shared/state_fields.py 的统一读取契约同构（本插件不依赖
        plugins/shared 包路径，就地最小实现）。
        """
        if isinstance(value, dict):
            return value
        if isinstance(value, str) and value.strip().startswith("{"):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return None
            if isinstance(parsed, dict):
                return parsed
        return None

    def set_event_bridge_ready(self) -> None:
        """标记域事件桥就绪（server.py 注册 on_domain_event 处理器后调用）。

        manifest 声明 ``domain_event`` lifecycle hook + server.py 注册处理器
        后，内核才会把域事件点对点推给本插件——两步都完成才调用本方法。
        """
        self._event_bridge_ready = True
        logger.info("[TriggerManager] 域事件桥已就绪 (domain_event)")

    def is_event_bridge_ready(self) -> bool:
        """EVENT 桥是否就绪（域事件处理器已接线）。"""
        return self._event_bridge_ready

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

                    # 动作分发：command 走 daemon 线程（不依赖注入字段）
                    if self._dispatch_trigger_action(trigger):
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

    async def collect_state_rows(self) -> list[dict[str, Any]]:
        """事件循环内直调 state provider 拉管道 state 聚合行。

        M1 起的消费方：``load_from_state``（注册表重灌）与 http_api
        ``GET /pipelines``（目标管道下拉选项）。本方法在插件主事件循环内
        执行（http.handle / on_load 上下文），awaitable provider 直接 await。
        桥未接通/返回形状异常抛错（fail-visible，由调用方转 5xx，不静默
        空列表）。
        """
        if self._state_provider is None:
            raise RuntimeError("state provider 未注入（server.py on_load 接线缺失）")
        result = self._state_provider()
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, list):
            raise RuntimeError(f"state provider 返回非列表: {type(result).__name__}")
        return [row for row in result if isinstance(row, dict)]

    def _format_fire_info(self, trigger: TriggerConfig) -> str:
        """构造触发通知消息体（[触发器通知] 前缀 + 触发计数 + 用户消息）。"""
        fire_info = (
            f"[触发器通知] 触发器 '{trigger.name or trigger.trigger_id}' "
            f"已触发 (第 {trigger.fire_count} 次"
        )

        if trigger.max_fires > 0:
            fire_info += f"/共 {trigger.max_fires} 次"

        fire_info += f")\n{trigger.message}"

        return fire_info

    # ── 触发器动作（action/action_params 通用机制） ─────────────────────
    #
    # 触发后的动作由 trigger.action 决定："notify"（含缺省 ""）= 注入消息唤醒
    # 管道；"command" = 参数列表执行宿主命令（shell=False，元字符不经 shell
    # 二次解析）。command 动作的 action_params：
    #   {"cmd": list[str], "timeout_ms": int(默认 10000), "cwd"?: str}
    # 注册写入面（trigger_setup 工具）对 command 动作另有 human-interaction
    # 审批闸；本执行器只消费已注册配置。执行 fire-and-forget（daemon 线程），
    # 超时杀进程树，绝不阻塞触发主流程；触发上下文经环境变量传递
    # （AGENTOS_TRIGGER_*），不拼进命令字符串（防注入）。
    # 执行留痕：stdout/stderr 全文落 logs/triggers/<id>_<时间>_<次数>.log
    # （bash 插件 logs/ 落盘同款范式），摘要（命令/退出码/耗时/输出尾部）
    # 进 trigger.metadata["command_executions"]（既有 REST triggers 读面
    # 序列化 metadata，可查）。旧 shell 字符串格式（action_params["command"]:
    # str）显式报停：不执行、拒绝原因留痕——不允许静默失效，也不允许经
    # shell 兜底执行（元字符二次解析的注入面不得回流）。

    # metadata 执行历史上限与单条输出摘要长度（防 metadata 无界膨胀）
    _AUDIT_HISTORY_LIMIT = 5
    _AUDIT_TEXT_LIMIT = 2000

    def _command_env(
        self,
        trigger: TriggerConfig,
        event_name: str | None = None,
        event_data: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        """构造命令执行环境：继承进程环境 + AGENTOS_TRIGGER_* 触发上下文。

        事件触发的载荷键以 AGENTOS_EVENT_<KEY> 透传（仅标量；事件名恒有）。
        """
        env = dict(os.environ)
        env["AGENTOS_TRIGGER_ID"] = trigger.trigger_id
        env["AGENTOS_TRIGGER_NAME"] = trigger.name or ""
        env["AGENTOS_TRIGGER_TYPE"] = trigger.trigger_type.value
        env["AGENTOS_PIPELINE_ID"] = trigger.pipeline_id or ""
        env["AGENTOS_FIRE_COUNT"] = str(trigger.fire_count)
        if event_name:
            env["AGENTOS_EVENT_NAME"] = event_name
        for key, value in (event_data or {}).items():
            if isinstance(value, (str, int, float, bool)) and value is not None:
                env[f"AGENTOS_EVENT_{str(key).upper()}"] = str(value)
        return env

    @staticmethod
    def _kill_process_tree(proc: subprocess.Popen) -> None:
        """超时杀进程树，平台对称：Windows taskkill /T /F；POSIX killpg 整组。

        失败留痕（残留进程可排查）；POSIX 已退出（ProcessLookupError）=
        终止目标已达成，不算失败。
        """
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/pid", str(proc.pid), "/T", "/F"],
                    capture_output=True,
                    timeout=10,
                )
            except Exception as exc:  # noqa: BLE001 - 杀进程失败仅记录
                logger.warning("[TriggerManager] taskkill 终止进程树失败（可能残留进程）| pid=%s | error=%s", proc.pid, exc)
        else:
            # spawn 时 start_new_session ⇒ 子进程自成一进程组（pgid == pid），
            # killpg 整组收掉含孙进程——单 proc.kill() 会把孙进程留成孤儿。
            try:
                os.killpg(proc.pid, _POSIX_SIGKILL)
            except ProcessLookupError:
                pass  # 进程组已退出，终止目标已达成
            except OSError as exc:
                logger.warning("[TriggerManager] killpg 终止进程组失败（可能残留进程）| pid=%s | error=%s", proc.pid, exc)

    @staticmethod
    def _decode_output(data: bytes | None) -> str:
        """子进程输出解码：UTF-8 优先，回退系统首选编码，坏字节替换。

        不用 text=True（其编码随平台漂移，跨端兼容已立卡）；按字节捕获后
        显式解码，保证 Windows 中文环境下输出可读。
        """
        if not data:
            return ""
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            pass
        try:
            return data.decode(locale.getpreferredencoding(False), errors="replace")
        except (LookupError, TypeError, ValueError):
            return data.decode("utf-8", errors="replace")

    @classmethod
    def _cap_text(cls, text: str) -> str:
        """metadata 摘要截尾（输出语义多在尾部）；全文在审计日志文件。"""
        if len(text) <= cls._AUDIT_TEXT_LIMIT:
            return text
        return "\n[已截断，全文见审计日志文件]\n" + text[-cls._AUDIT_TEXT_LIMIT:]

    def _refuse_execution(self, trigger: TriggerConfig, cmd: Any, reason: str) -> None:
        """拒绝执行的留痕（旧格式报停/参数形状非法/启动失败），不启动任何进程。"""
        self._record_execution(trigger, {
            "fired_at": datetime.datetime.now(datetime.UTC).isoformat(),
            "cmd": cmd,
            "exit_code": None,
            "timed_out": False,
            "duration_ms": 0,
            "refused": True,
            "reason": reason,
        })
        logger.error(
            "[TriggerManager] command 动作拒绝执行: trigger=%s cmd=%r reason=%s",
            trigger.trigger_id,
            cmd,
            reason,
        )

    def _record_execution(self, trigger: TriggerConfig, record: dict[str, Any]) -> None:
        """执行留痕进 metadata（有界历史，最新在前）+ 结构化日志。

        trigger.metadata 经既有 REST triggers list/get 面序列化，执行记录
        由该读面消费，不新增查询端点。
        """
        history = list(trigger.metadata.get("command_executions") or [])
        history.insert(0, record)
        trigger.metadata["command_executions"] = history[: self._AUDIT_HISTORY_LIMIT]
        trigger.metadata["last_command_execution"] = record
        logger.info(
            "[TriggerManager] command 动作执行留痕: trigger=%s cmd=%s exit_code=%s "
            "timed_out=%s duration_ms=%s refused=%s",
            trigger.trigger_id,
            record.get("cmd"),
            record.get("exit_code"),
            record.get("timed_out"),
            record.get("duration_ms"),
            record.get("refused", False),
        )

    def _write_execution_log(
        self,
        trigger: TriggerConfig,
        record: dict[str, Any],
        stdout_full: str,
        stderr_full: str,
    ) -> str:
        """stdout/stderr 全文落审计日志文件，返回路径串（落盘失败返回空串，
        不阻断执行主流程——metadata 摘要留痕不受影响）。"""
        try:
            log_dir = Path("logs") / "triggers"
            log_dir.mkdir(parents=True, exist_ok=True)
            stamp = record["fired_at"].replace("-", "").replace(":", "")
            path = log_dir / f"{trigger.trigger_id}_{stamp}_{trigger.fire_count}.log"
            header = (
                f"# Trigger: {trigger.trigger_id} ({trigger.name})\n"
                f"# Fired at: {record['fired_at']}\n"
                f"# Command: {json.dumps(record['cmd'], ensure_ascii=False)}\n"
                f"# Exit code: {record['exit_code']}\n"
                f"# Timed out: {record['timed_out']}\n"
                f"# Duration ms: {record['duration_ms']}\n"
            )
            path.write_text(
                header
                + "\n# ── stdout ──\n" + stdout_full
                + "\n# ── stderr ──\n" + stderr_full,
                encoding="utf-8",
            )
            return str(path)
        except OSError as e:
            logger.warning(
                "[TriggerManager] 审计日志落盘失败（摘要留痕不受影响）: trigger=%s error=%s",
                trigger.trigger_id,
                e,
            )
            return ""

    def _run_command(self, trigger: TriggerConfig, event_name: str | None, event_data: dict[str, Any] | None) -> None:
        """daemon 线程执行命令动作（fire-and-forget：失败/超时留痕不重试）。"""
        params = trigger.action_params or {}
        cmd = params.get("cmd")
        if cmd is None:
            legacy = params.get("command")
            if isinstance(legacy, str) and legacy.strip():
                self._refuse_execution(
                    trigger,
                    legacy,
                    "旧格式为 shell 字符串命令，已停用（shell 执行面已移除）；"
                    "请改为 cmd 参数列表并重新注册",
                )
                return
            self._refuse_execution(trigger, cmd, "command 动作缺 cmd 参数列表")
            return
        if not isinstance(cmd, list) or not cmd or not all(isinstance(c, str) and c.strip() for c in cmd):
            self._refuse_execution(trigger, cmd, "cmd 必须是非空字符串参数列表")
            return
        timeout_ms = int(params.get("timeout_ms") or 10000)
        env = self._command_env(trigger, event_name, event_data)
        cwd = params.get("cwd")
        fired_at = datetime.datetime.now(datetime.UTC).isoformat()
        started = time.monotonic()
        try:
            proc = subprocess.Popen(
                cmd,
                shell=False,
                env=env,
                cwd=str(cwd) if cwd else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                # POSIX 下子进程自成一 session/进程组（pgid == pid）：超时
                # killpg 才能整组收掉（含孙进程）；Windows 不支持，传 False
                # （Windows 分支走 taskkill /T 按父子关系遍历）。
                start_new_session=(os.name == "posix"),
            )
        except OSError as e:
            self._refuse_execution(trigger, cmd, f"启动失败: {e}")
            return
        timed_out = False
        try:
            out_bytes, err_bytes = proc.communicate(timeout=timeout_ms / 1000)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._kill_process_tree(proc)
            try:
                out_bytes, err_bytes = proc.communicate(timeout=5)
            except (subprocess.TimeoutExpired, OSError, ValueError):
                out_bytes, err_bytes = b"", b""
        except Exception as e:  # noqa: BLE001 - 等待失败仅留痕
            self._refuse_execution(trigger, cmd, f"等待异常: {e}")
            return
        record: dict[str, Any] = {
            "fired_at": fired_at,
            "cmd": cmd,
            "exit_code": proc.returncode,
            "timed_out": timed_out,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "stdout": self._cap_text(self._decode_output(out_bytes)),
            "stderr": self._cap_text(self._decode_output(err_bytes)),
        }
        stdout_full = self._decode_output(out_bytes)
        stderr_full = self._decode_output(err_bytes)
        record["log_file"] = self._write_execution_log(trigger, record, stdout_full, stderr_full)
        self._record_execution(trigger, record)

    def _dispatch_trigger_action(
        self,
        trigger: TriggerConfig,
        event_name: str | None = None,
        event_data: dict[str, Any] | None = None,
    ) -> bool:
        """分发触发器动作。command → daemon 线程执行并返回 True（已处理）；
        notify（含缺省 ""）→ 返回 False（调用方走注入消息路径）。"""
        action = (trigger.action or "").strip()
        if action == "command":
            threading.Thread(
                target=self._run_command,
                args=(trigger, event_name, event_data),
                daemon=True,
            ).start()
            return True
        return False

    def _inject_trigger_message(self, trigger: TriggerConfig) -> None:
        """触发器到期后把消息注入所属管道（唤醒 agent 跑一轮）。

        经注入器（``self._injector``）调内核 ``chat.send_message`` capability，
        复用前端 WS 派发路径（dispatch_user_input → process_via_engine），agent 处理后流式回复。
        注入器未设置时记录错误并放弃本轮注入（0.2 需 set_injector）。

        Args:
            trigger: 已触发的触发器配置。
        """

        loop = self._main_loop

        if loop is None or loop.is_closed():
            logger.warning(
                "[TriggerManager] 主事件循环不可用，跳过: trigger=%s pipeline=%s",
                trigger.trigger_id,
                trigger.pipeline_id,
            )

            return

        fire_info = self._format_fire_info(trigger)

        user_id = (trigger.metadata or {}).get("user_id", "")

        # ── 0.2 sidecar 路径：内核 chat.send_message capability（主路径） ──
        if self._injector is not None:
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is loop:
                # 已在主循环内调用（http REST 手动触发等）：直接调度不阻塞等待；
                # run_coroutine_threadsafe+future.result 会自我投递死锁满 60s 超时。
                task = running.create_task(self._injector(trigger.pipeline_id, fire_info, user_id))
                self._loop_tasks.add(task)
                task.add_done_callback(self._loop_tasks.discard)
                return
            future = asyncio.run_coroutine_threadsafe(
                self._injector(trigger.pipeline_id, fire_info, user_id),
                loop,
            )
            try:
                future.result(timeout=60)
                logger.info(
                    "[TriggerManager] 消息已注入: pipeline=%s method=chat.send_message trigger=%s fire_count=%d",
                    trigger.pipeline_id,
                    trigger.trigger_id,
                    trigger.fire_count,
                )
            except Exception as e:
                logger.error(
                    "[TriggerManager] 消息注入异常: pipeline=%s trigger=%s error=%s",
                    trigger.pipeline_id,
                    trigger.trigger_id,
                    e,
                )
            return

        logger.error(
            "[TriggerManager] 注入器未设置，放弃本轮注入（0.2 需 set_injector）: "
            "pipeline=%s trigger=%s",
            trigger.pipeline_id,
            trigger.trigger_id,
        )

    async def handle_domain_event(self, event_name: str, event_data: dict[str, Any]) -> list[str]:
        """域事件桥入口（GAP-2 EVENT 接线）：评估 + 注入。

        server.py 的 ``on_domain_event`` 生命周期处理器收到内核推送的域事件
        （run 终态派生的 ``task_completed`` / ``task_failed`` 等）后调用本方法：
        ``evaluate_event`` 匹配触发器，命中后经
        注入器直接投递（async 上下文，无需 run_coroutine_threadsafe）。

        子任务自动通知（GAP-1）：任务事件携带 ``parent_pipeline_id`` 时，
        向父管道注入一条完成/失败通知——等效"任务系统提交后自动注册触发器"，
        注册逻辑收敛在统一触发服务，任务系统零触发代码。
        自动父通知与显式触发器**并存不去重**：显式触发器消息是用户自定义
        内容，系统通知是 task_submit
        承诺的父管道恢复锚点——语义不等价，任何显式触发器都无权顶替
        系统通知（实测：LLM 自设 task_completed 测试触发器命中后，父管道
        只收到测试消息，永远等不到完成通知）。

        Args:

            event_name: 事件名（如 task_completed / run.failed）。

            event_data: 事件载荷（pipeline_id / task_id / parent_pipeline_id 等标签）。

        Returns:

            被触发的 trigger_id 列表（注入失败不影响评估结果，仅记录）。

        """
        fired = self.evaluate_event(event_name, event_data or {})

        # GAP-1：task_completed/task_failed + parent_pipeline_id 非空 → 无条件
        # 自动父通知（与显式触发器并存）。注入失败仅记录，不阻断。
        await self._auto_notify_parent(event_name, event_data or {})

        for trigger_id in fired:
            trigger = self._triggers.get(trigger_id)

            if trigger is None:
                continue

            # 动作分发：command 走 daemon 线程（fire-and-forget，不依赖注入字段）；
            # 默认注入消息
            if self._dispatch_trigger_action(trigger, event_name, event_data or {}):
                continue

            if not trigger.pipeline_id or not trigger.message:
                continue

            if self._injector is None:
                logger.warning(
                    "[TriggerManager] 注入器未设置，域事件触发无法投递: trigger=%s event=%s",
                    trigger.trigger_id,
                    event_name,
                )
                continue

            try:
                user_id = (trigger.metadata or {}).get("user_id", "")
                await self._injector(
                    trigger.pipeline_id,
                    self._format_fire_info(trigger),
                    user_id,
                )
                logger.info(
                    "[TriggerManager] 域事件消息已注入: pipeline=%s event=%s trigger=%s",
                    trigger.pipeline_id,
                    event_name,
                    trigger.trigger_id,
                )
            except Exception as e:
                logger.error(
                    "[TriggerManager] 域事件消息注入异常: pipeline=%s trigger=%s error=%s",
                    trigger.pipeline_id,
                    trigger.trigger_id,
                    e,
                )

        return fired

    def _auto_notify_gate(self, key: tuple[str, str, str]) -> tuple[bool, int]:
        """自动父通知自激熔断闸：返回 (是否放行, 本事件为窗内第几次)。

        同键滑动窗（_AUTO_NOTIFY_WINDOW_SECONDS）内第 1/2 次放行、第 3 次
        （_AUTO_NOTIFY_BREAK_THRESHOLD）起熔断。熔断事件不入窗——窗内只记
        实际注入时刻，窗内全部时刻老化后自然重新计数。键按访问序 LRU 淘汰
        （上限 _AUTO_NOTIFY_MAX_KEYS，逐出即该键熔断状态重置）。
        """
        now = self._clock()
        window = self._auto_notify_windows.get(key)
        if window is None:
            window = deque()
            self._auto_notify_windows[key] = window
        while window and now - window[0] >= _AUTO_NOTIFY_WINDOW_SECONDS:
            window.popleft()
        self._auto_notify_windows.move_to_end(key)
        if len(self._auto_notify_windows) > _AUTO_NOTIFY_MAX_KEYS:
            self._auto_notify_windows.popitem(last=False)
        attempt = len(window) + 1
        if attempt >= _AUTO_NOTIFY_BREAK_THRESHOLD:
            return False, attempt
        window.append(now)
        return True, attempt

    async def _auto_notify_parent(self, event_name: str, event_data: dict[str, Any]) -> None:
        """GAP-1：子任务终态自动通知父管道（等效"提交后自动注册触发器"）。

        契约：task_completed / task_failed 事件携带非空 ``parent_pipeline_id``
        （内核从子任务管道 state 的 ``lineage.parent_pipeline_id`` 扁平键带出）
        时，向父管道注入一条完成/失败通知——兑现 task_submit "子任务完成后
        系统会自动通知你并恢复执行"的承诺。任务系统零触发代码：注册逻辑
        收敛在统一触发服务（triggers_ext），事件本身携带父锚点即触发。

        通知内容为富文本：标题（task.goal）、失败原因
        （task.error）、重试计数（task.eval_total_calls）、评估结论
        （task.eval_summary）、上下文使用率（track.llm_usage + context_window）
        ——由 tasks 插件事件派生时从 state 摘要行带出，缺键空串兜底。

        自激熔断：同键 (parent, task, event) 滑动窗（10 分钟）内第 1 次正常
        注入、第 2 次注入附重复计数提示、第 3 次起不再注入（WARNING 留痕），
        窗滚过后重新计数——防"失败通知→自动重派→再失败→再通知"闭环
        （ADR 2026-09-30）。编排上下文侧的对应衰减在 llm_core 出站装配
        （_decay_system_notifications，每 task 至多存活末条未消费通知）。

        Returns:
            None（注入失败仅记录，不阻断域事件桥主流程）。
        """
        if event_name not in ("task_completed", "task_failed"):
            return
        parent_pipeline_id = str(event_data.get("parent_pipeline_id") or "")
        if not parent_pipeline_id:
            return
        task_id = str(event_data.get("task_id") or "")
        title = str(event_data.get("title") or "") or task_id
        error = str(event_data.get("error") or "")
        retry_count = str(event_data.get("retry_count") or "")
        eval_summary = str(event_data.get("eval_summary") or "")
        # 子任务提交者（task_submit 写入子任务初始 state 的 task.submitted_by，
        # 内核 task_completed/task_failed 事件随 parent_pipeline_id 一并带出）。
        # chat.send_message 硬校验 user_id 非空（tenant 反查）——传空串会被内核
        # 拒绝（-32603 缺少 user_id）。
        user_id = str(event_data.get("user_id") or "")

        # 上下文使用率（遥测提示；缺数据不拼）
        context_usage_text = ""
        cu = event_data.get("context_usage")
        if isinstance(cu, dict):
            pct = cu.get("pct", 0)
            input_tokens = cu.get("input_tokens", 0)
            window = cu.get("context_window", 0)
            if pct and window:
                if pct > 50:
                    hint = "⚠️ 建议优先创建新任务（上下文已超过50%，继续派发可能触发压缩或截断）"
                elif pct > 25:
                    hint = "⚠️ 不建议继续向此 Agent 继承提交（上下文已超25%，建议新建任务以保留充足余量）"
                else:
                    hint = "✅ 可直接继续向此 Agent 派发任务（上下文充足）"
                context_usage_text = (
                    f"\n📊 上下文使用率: {pct}% ({input_tokens:,}/{window:,} tokens)\n{hint}"
                )

        if event_name == "task_completed":
            conclusion_hint = f"\n📋 评估结论: {eval_summary[:200]}" if eval_summary else ""
            message = (
                f"[系统通知] 子任务 '{title}' (ID: {task_id}) 已完成 ✅"
                f"{conclusion_hint}{context_usage_text}\n"
                "请查阅子任务产出与评估结论后决定下一步。"
            )
        else:
            err_hint = f": {error[:300]}" if error else ""
            retry_hint = f" (已重试 {retry_count} 次)" if retry_count else ""
            message = (
                f"[系统通知] 子任务 '{title}' (ID: {task_id}) 失败 ❌"
                f"{retry_hint}{err_hint}{context_usage_text}\n"
                "请根据失败情况决定后续操作（重试/替代方案/标记失败）。"
            )

        if self._injector is None:
            logger.warning(
                "[TriggerManager] 注入器未设置，子任务自动通知无法投递: parent=%s event=%s",
                parent_pipeline_id,
                event_name,
            )
            return

        # 自激熔断：同键 (parent, task, event) 滑动窗内第 3 次起不再注入
        # （缺陷实证：失败通知→自动重派→再失败的 18 秒自激圈）。task_id 为空
        # 无法定维度，退回无熔断行为（与修复前一致，不误伤无法归键的通知）。
        if task_id:
            allowed, attempt = self._auto_notify_gate((parent_pipeline_id, task_id, event_name))
            if not allowed:
                logger.warning(
                    "[TriggerManager] 子任务自动通知熔断: 同键(parent=%s task=%s event=%s) "
                    "%d 分钟窗内第 %d 次事件达到阈值 %d，不再注入（自动重派自激防护；窗滚过后重新计数）",
                    parent_pipeline_id,
                    task_id,
                    event_name,
                    int(_AUTO_NOTIFY_WINDOW_SECONDS // 60),
                    attempt,
                    _AUTO_NOTIFY_BREAK_THRESHOLD,
                )
                return
            if attempt == 2:
                message += (
                    f"\n⚠️ 系统提示: 这是 {int(_AUTO_NOTIFY_WINDOW_SECONDS // 60)} 分钟内"
                    f"同一子任务的第 {attempt} 次重复通知"
                    f"（自动重派自激防护: 第 {_AUTO_NOTIFY_BREAK_THRESHOLD} 次起将暂停注入直至窗口滚过）。"
                )

        try:
            await self._injector(parent_pipeline_id, message, user_id)
            logger.info(
                "[TriggerManager] 子任务自动通知已注入: parent=%s event=%s task=%s",
                parent_pipeline_id,
                event_name,
                task_id,
            )
        except Exception as e:
            logger.error(
                "[TriggerManager] 子任务自动通知注入异常: parent=%s event=%s error=%s",
                parent_pipeline_id,
                event_name,
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

                except (ValueError, TypeError) as exc:
                    logger.debug(
                        "[TriggerManager] register_time 不可解析，max_time 上限失效（按未达上限继续）| trigger=%s raw=%s error=%s",
                        trigger.trigger_id, register_time_str, exc,
                    )

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
            return dt.replace(tzinfo=datetime.UTC)

        return dt.astimezone(datetime.UTC)

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
