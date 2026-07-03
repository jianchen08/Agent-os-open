"""Drain 生命周期管理（Phase 1 管道重构后已精简）。"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# Sink 创建


def create_sink(pipeline_id: str, thread_id: str = "") -> Any | None:
    """从 registry 获取 thread_id 创建 TargetedSink。"""
    try:
        from channels.websocket.ws_handler import ws_interaction_notifier  # noqa: PLC0415
        from pipeline.stream_bridge import create_targeted_sink  # noqa: PLC0415

        if not ws_interaction_notifier:
            logger.warning(
                "[DrainMgr] create_sink: notifier is None | pipeline=%s",
                pipeline_id[:12],
            )
            return None

        # 优先使用传入的 thread_id，仅当为空时从 registry 兜底
        if not thread_id:
            from pipeline.registry import get_engine_registry  # noqa: PLC0415

            registry = get_engine_registry()
            entry = registry.get(pipeline_id)
            thread_id = entry.thread_id if entry else ""

        if not thread_id:
            logger.warning(
                "[DrainMgr] create_sink: no thread_id | pipeline=%s",
                pipeline_id[:12],
            )

        return create_targeted_sink(ws_interaction_notifier, thread_id, pipeline_id=pipeline_id)
    except Exception as _cs_err:
        logger.warning(
            "[DrainMgr] create_sink FAILED: pipeline=%s error=%s",
            pipeline_id[:12],
            _cs_err,
        )
        return None


# Drain 启动/重启（Phase 1 后兼容空实现）
# engine 现在主动调用 bridge.emit_* 推送事件，不再需要独立 drain 协程消费
# bridge 队列。以下函数保留签名仅为兼容现有导入链（message_bus、
# pipeline_reviver、task_executor 等），调用时不再产生任何副作用。


def start_bg_drain(
    pipeline_id: str,
    bridge: Any,
    engine: Any,
    engine_task: Any | None = None,
) -> None:
    """兼容空实现：drain_loop 已在 Phase 1 删除。"""
    logger.debug(
        "[DRAIN] start_bg_drain no-op (Phase 1): pipeline=%s",
        pipeline_id[:12],
    )


def restart_drain(
    pipeline_id: str,
    bridge: Any | None = None,
    engine: Any | None = None,
    engine_task: Any = None,
    *,
    client_message_id: str = "",
) -> None:
    """兼容空实现：drain_loop 已在 Phase 1 删除。"""
    logger.debug(
        "[DRAIN] restart_drain no-op (Phase 1): pipeline=%s",
        pipeline_id[:12],
    )
