"""流式桥接器统一出口（drain_loop 已删除）。

Phase 1 改造：drain_loop 独立协程已删除，engine 现在主动调
bridge.emit_start/chunk/finish/suspend/error 推送事件。

本文件仅保留 send_frontend_event 模块级统一出口函数，
供外部模块（如 task_notifier、triggers）通过 pipeline_id 查找 bridge 推送事件。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 模块级统一出口函数
# ---------------------------------------------------------------------------


async def send_frontend_event(
    pipeline_id: str,
    event: dict,
) -> bool:
    """通过统一出口发送前端事件（基于管道ID查找）。

    Args:
        pipeline_id: 管道 ID
        event: 要发送的事件字典

    Returns:
        发送成功返回 True，失败返回 False
    """
    if not pipeline_id:
        return False

    from pipeline.registry import get_engine_registry  # noqa: PLC0415

    registry = get_engine_registry()

    bridge = registry.get_bridge(pipeline_id)
    if bridge is not None:
        return await bridge.send_event(event)

    entry = registry.get(pipeline_id)
    if entry is None or not entry.thread_id:
        return False

    try:
        from channels.websocket.ws_handler import ws_interaction_notifier as _notifier  # noqa: PLC0415
    except Exception as _import_err:
        logger.debug(
            "send_frontend_event: ws_interaction_notifier 不可用 pipeline=%s err=%s",
            pipeline_id[:12],
            _import_err,
        )
        _notifier = None

    if not _notifier:
        return False

    from pipeline.sink import create_targeted_sink  # noqa: PLC0415

    sink = create_targeted_sink(_notifier, entry.thread_id)
    if sink is None:
        return False
    return await sink.send_event(event)
