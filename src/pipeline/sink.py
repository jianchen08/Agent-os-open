"""输出目标抽象层。

定义 IOutputSink 协议、TargetedSink 定向推送和 MultiChannelSink 多通道分发。
将 engine 的输出抽象为统一的 Sink 接口，解耦 WebSocket 直连与多通道广播。
"""
from __future__ import annotations

import logging
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 管道消息来源枚举
# ---------------------------------------------------------------------------

class EnvelopeSource(StrEnum):
    """管道消息来源标识。"""
    LLM = "llm"
    USER = "user"
    SYSTEM = "system"
    TRIGGER = "trigger"
    ENGINE = "engine"


# ---------------------------------------------------------------------------
# IOutputSink 协议与实现
# ---------------------------------------------------------------------------

@runtime_checkable
class IOutputSink(Protocol):
    """输出目标协议，抽象 WebSocket 直连与广播两种发送方式。"""

    async def send_event(self, event: dict) -> bool:
        """发送事件到前端，成功返回 True，失败返回 False。"""
        ...

    @property
    def sink_id(self) -> str:
        """返回输出目标的唯一标识，用于日志和调试。"""
        ...


class TargetedSink:
    """定向输出目标，按 thread_id 直接路由事件到对应 WebSocket 连接。

    路由失败时记录错误并返回 False，不广播。
    广播是消息串扰的根因，已被删除。
    后端执行为主，前端断连不干预引擎运行。
    """

    # 连续推送失败次数达到该阈值时，日志级别由 WARNING 升级为 ERROR
    _FAILURE_THRESHOLD = 5

    def __init__(self, notifier: Any, thread_id: str) -> None:
        """初始化定向输出目标。

        Args:
            notifier: 具备 send_to_thread 和 send_to_user 方法的通知器对象
            thread_id: 目标会话的 ws_thread_id
        """
        self._notifier = notifier
        self._thread_id = thread_id
        # 连续失败计数：累积到阈值时升级日志级别，便于发现持续不可用的 sink
        self._consecutive_failures: int = 0

    @property
    def sink_id(self) -> str:
        """返回定向发送标识。"""
        return f"targeted:{self._thread_id or 'no-thread'}"

    def _record_failure(self, event: dict, *, exc_info: bool = False) -> None:
        """记录一次推送失败，连续超过阈值时升级为 ERROR 日志。

        Args:
            event: 触发失败的事件字典，用于日志上下文
            exc_info: 是否在日志中附带异常栈（异常路径应为 True）
        """
        self._consecutive_failures += 1
        level = (
            logging.ERROR
            if self._consecutive_failures >= self._FAILURE_THRESHOLD
            else logging.WARNING
        )
        logger.log(
            level,
            "sink 连续推送失败 %d 次 thread_id=%s type=%s",
            self._consecutive_failures,
            (self._thread_id or "(empty)")[:12],
            event.get("type", "?"),
            exc_info=exc_info,
        )

    def _record_success(self) -> None:
        """记录一次推送成功，若此前有失败则记录恢复并重置计数。"""
        if self._consecutive_failures > 0:
            logger.info(
                "sink 推送恢复 thread_id=%s",
                (self._thread_id or "(empty)")[:12],
            )
            self._consecutive_failures = 0

    async def send_event(self, event: dict) -> bool:
        """通过 WebSocket 推送事件。

        Args:
            event: 要发送的事件字典

        Returns:
            发送成功返回 True，失败返回 False
        """
        try:
            ok = await self._notifier.send_to_thread(self._thread_id, event)
            if not ok:
                self._record_failure(event)
            else:
                self._record_success()
            return ok
        except Exception:
            self._record_failure(event, exc_info=True)
            return False


# ---------------------------------------------------------------------------
# MultiChannelSink — 多通道输出分发
# ---------------------------------------------------------------------------

def create_targeted_sink(
    notifier: Any,
    thread_id: str = "",
    *,
    pipeline_id: str = "",
) -> TargetedSink | None:
    """统一 TargetedSink 创建入口，消除散点。

    当 notifier 为 None 或 thread_id 为空时返回 None（而非创建无效 sink）。
    优先使用调用方传入的 thread_id，仅当为空时从 registry 兜底读取。

    Args:
        notifier: 具备 send_to_thread 的通知器对象
        thread_id: 目标会话的 ws_thread_id
        pipeline_id: 管道 ID，仅当 thread_id 为空时用于从 registry 兜底查找

    Returns:
        TargetedSink 实例，创建失败返回 None
    """
    if not notifier:
        return None

    # 优先使用传入的 thread_id，仅当为空时从 registry 兜底
    if not thread_id and pipeline_id:
        try:
            from pipeline.registry import get_engine_registry
            entry = get_engine_registry().get(pipeline_id)
            if entry:
                thread_id = entry.thread_id
        except Exception:
            logger.debug(
                "create_targeted_sink: registry 查找失败 pipeline=%s",
                pipeline_id[:12], exc_info=True,
            )

    if not thread_id:
        logger.debug(
            "create_targeted_sink: 无 thread_id (pipeline=%s)",
            pipeline_id[:12] if pipeline_id else "(无)",
        )

    return TargetedSink(notifier, thread_id)


class MultiChannelSink:
    """多渠道输出分发器。将 bridge 产出的内部事件分发给所有注册的通道。

    每个通道实现 IOutputSink 协议，由 MultiChannelSink 统一管理。
    新增通道只需 register，不改 bridge 核心逻辑。
    """

    def __init__(self) -> None:
        self._channels: dict[str, IOutputSink] = {}

    def register(self, name: str, sink: IOutputSink) -> None:
        """注册一个通道。"""
        self._channels[name] = sink
        logger.info("[MultiChannel] registered channel: %s sink=%s", name, sink.sink_id)

    def unregister(self, name: str) -> None:
        """注销一个通道。"""
        self._channels.pop(name, None)

    @property
    def sink_id(self) -> str:
        """返回多通道聚合标识。"""
        return f"multi:{','.join(self._channels.keys())}" if self._channels else "multi:empty"

    async def send_event(self, event: dict) -> bool:
        """分发事件给所有通道。任一通道成功即返回 True。"""
        any_success = False
        for name, sink in list(self._channels.items()):
            try:
                if await sink.send_event(event):
                    any_success = True
            except Exception:
                # M2-fix: 不再静默吞掉通道错误，记录 warning
                logger.warning(
                    "MultiChannelSink: 通道 %s 发送异常 event_type=%s",
                    name, event.get("type", "?"),
                    exc_info=True,
                )
        return any_success
