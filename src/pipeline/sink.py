"""输出目标抽象层。"""
from __future__ import annotations

import logging
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# 管道消息来源枚举

class EnvelopeSource(StrEnum):
    """管道消息来源标识。"""
    LLM = "llm"
    USER = "user"
    SYSTEM = "system"
    TRIGGER = "trigger"
    ENGINE = "engine"


# IOutputSink 协议与实现

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
    """定向输出目标，按 thread_id 直接路由事件到对应 WebSocket 连接。"""

    # 连续推送失败次数达到该阈值时，日志级别由 WARNING 升级为 ERROR
    _FAILURE_THRESHOLD = 5

    def __init__(
        self,
        notifier: Any,
        thread_id: str,
        *,
        pipeline_id: str = "",
    ) -> None:
        """初始化定向输出目标。"""
        self._notifier = notifier
        self._thread_id = thread_id
        self._pipeline_id = pipeline_id
        # 连续失败计数：累积到阈值时升级日志级别，便于发现持续不可用的 sink
        self._consecutive_failures: int = 0

    @property
    def sink_id(self) -> str:
        """返回定向发送标识。"""
        return f"targeted:{self._thread_id or 'no-thread'}"

    @property
    def is_dead(self) -> bool:
        """sink 是否已"死"——连续失败达到阈值即视为死。"""
        return self._consecutive_failures >= self._FAILURE_THRESHOLD

    def _resolve_thread_id(self) -> str:
        """动态解析当前应使用的 thread_id。"""
        if self._thread_id:
            return self._thread_id
        if not self._pipeline_id:
            return ""
        try:
            from pipeline.registry import get_engine_registry  # noqa: PLC0415
            entry = get_engine_registry().get(self._pipeline_id)
            if entry and entry.thread_id:
                # 缓存到 self 以便后续 send 复用，sink_id 日志也随之更新
                self._thread_id = entry.thread_id
                return entry.thread_id
        except Exception:
            logger.debug(
                "TargetedSink._resolve_thread_id: registry 查找失败 pipeline=%s",
                self._pipeline_id[:12], exc_info=True,
            )
        return ""

    def _record_failure(self, event: dict, *, exc_info: bool = False) -> None:
        """记录一次推送失败，连续超过阈值时升级为 ERROR 日志。"""
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
        """通过 WebSocket 推送事件。"""
        # 动态解析 thread_id：构造时为空 → 此刻从 registry 兜底
        # （见 ）
        target_tid = self._resolve_thread_id()
        try:
            ok = await self._notifier.send_to_thread(target_tid, event)
            if not ok:
                self._record_failure(event)
            else:
                self._record_success()
            return ok
        except Exception:
            self._record_failure(event, exc_info=True)
            return False


# MultiChannelSink — 多通道输出分发

def create_targeted_sink(
    notifier: Any,
    thread_id: str = "",
    *,
    pipeline_id: str = "",
) -> TargetedSink | None:
    """统一 TargetedSink 创建入口，消除散点。"""
    if not notifier:
        return None

    # 优先使用传入的 thread_id，仅当为空时从 registry 兜底
    if not thread_id and pipeline_id:
        try:
            from pipeline.registry import get_engine_registry  # noqa: PLC0415
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
            "create_targeted_sink: 无 thread_id (pipeline=%s)，sink 将在每次发送时动态查找",
            pipeline_id[:12] if pipeline_id else "(无)",
        )

    # 把 pipeline_id 传进 sink，便于 thread_id 后续到位后能动态解析
    # （见 ）
    return TargetedSink(notifier, thread_id, pipeline_id=pipeline_id)


class MultiChannelSink:
    """多渠道输出分发器。将 bridge 产出的内部事件分发给所有注册的通道。"""

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
