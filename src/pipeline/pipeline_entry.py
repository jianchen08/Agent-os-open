"""管道引擎注册条目数据类。

封装 engine + bridge + thread_id + tags 的关联关系，
替代原先分散在 ServiceProvider 字符串 key、_GLOBAL_SUSPENDED_ENGINES、
_pipeline_thread_map 中的多套映射。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class PipelineEntry:
    """管道引擎注册条目。

    Attributes:
        engine: PipelineEngine 实例
        bridge: 当前活跃的 PipelineStreamBridge（可为 None）
        drain_task: 后台 drain_loop 任务引用
        engine_task: 引擎主循环 Task
        thread_id: 对应的 WebSocket thread_id
        tags: 通用关联标签，如 {"task_id": "xxx"}
        created_at: 条目创建时间
        msg_sequence: 共享消息 sequence 计数器
        _seq_lock: 保护 msg_sequence 的锁（跨线程安全递增）
    """

    engine: Any  # PipelineEngine（用 Any 避免循环导入）
    bridge: Any | None = None  # PipelineStreamBridge | None
    drain_task: Any | None = None  # asyncio.Task | None
    engine_task: Any | None = None  # asyncio.Task | None
    thread_id: str = ""
    tags: dict[str, str] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)
    msg_sequence: int = 0
    _seq_lock: threading.Lock = field(default_factory=threading.Lock)

    def next_sequence(self) -> int:
        """线程安全地递增，返回下一个消息级别的 sequence。

        所有 WS 事件推送（stream_start、stream_chunk、tool_start、
        system_notification、pipeline_received、new_message 等）
        都通过此方法获取 sequence，保证跨模块全局单调递增。

        该方法会被主事件循环和 executor 线程并发调用（on_chunk 路径），
        因此用锁保证递增的原子性，避免丢号/重号导致前端消息乱序。

        Returns:
            递增后的 sequence 值
        """
        with self._seq_lock:
            self.msg_sequence += 1
            return self.msg_sequence

    def init_sequence(self, max_seq: int) -> None:
        """从已有记录续接 sequence（管道恢复/重启时使用）。

        Args:
            max_seq: 已有记录中的最大 sequence 值
        """
        with self._seq_lock:
            self.msg_sequence = max(self.msg_sequence, max_seq)

    @property
    def is_engine_alive(self) -> bool:
        """engine_task 是否存活（I3：引擎死亡判定）。"""
        return self.engine_task is not None and not self.engine_task.done()

    def ensure_engine(self, *, provider: Any | None = None) -> None:
        """I3：保证 entry 内部引擎对象可用。死了就 lazy 重建。

        注册表内部自治方法，不被 send/dispatcher 外部触发。
        外部调用者拿到的 entry 永远保证 engine 对象可用。
        仅负责"建引擎对象"，不加载历史（历史是 engine.run 的职责）。

        Args:
            provider: ServiceProvider 实例，用于取四件套；为 None 时尝试全局获取。
        """
        if self.is_engine_alive:
            return
        new_engine = _build_engine_from_tags(self.tags, provider)
        if new_engine is None:
            logger.warning(
                "[Entry] lazy 重建失败（缺少四件套）: pipeline=%s",
                self.tags.get("pipeline_id", "")[:12],
            )
            return
        self.engine = new_engine
        self.engine_task = None  # 待 send 启动
        logger.info(
            "[Entry] 引擎 lazy 重建 | pipeline=%s",
            self.tags.get("pipeline_id", "")[:12],
        )


MAX_TAGS_PER_PIPELINE = 8
"""每个管道允许的最大标签数量。"""


def _build_engine_from_tags(
    tags: dict[str, str],
    provider: Any | None,
) -> Any | None:
    """从 tags + provider 构造一个新的 PipelineEngine 对象（I3 lazy 重建）。

    四件套（input/output_route_table、plugin_registry、services）从全局
    ServiceProvider 取；pipeline_id 从 tags 恢复以保持 entry 身份不变。

    Args:
        tags: entry.tags，需含 pipeline_id（可选 agent_id 等上下文）
        provider: ServiceProvider 实例；为 None 时尝试全局获取。

    Returns:
        新的 PipelineEngine 实例；四件套缺失返回 None。
    """
    if provider is None:
        try:
            from infrastructure.service_provider import get_service_provider  # noqa: PLC0415

            provider = get_service_provider()
        except Exception:
            return None
    if provider is None:
        return None

    input_route_table = provider.get("input_route_table") if provider else None
    output_route_table = provider.get("output_route_table") if provider else None
    plugin_registry = provider.get("plugin_registry") if provider else None
    services = provider.get_all_services() if provider else {}

    if not input_route_table or not output_route_table or not plugin_registry:
        return None

    from pipeline.engine import PipelineEngine  # noqa: PLC0415

    checkpoint_mgr = services.get("checkpoint_manager") if services else None
    engine = PipelineEngine(
        input_route_table=input_route_table,
        output_route_table=output_route_table,
        plugin_registry=plugin_registry,
        services=services,
        checkpoint_manager=checkpoint_mgr,
    )
    # 保持 entry 身份不变：pipeline_id 从 tags 恢复
    pid = tags.get("pipeline_id", "")
    if pid:
        engine.pipeline_id = pid
    return engine
