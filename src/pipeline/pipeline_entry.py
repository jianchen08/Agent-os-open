"""管道引擎注册条目数据类。

封装 engine + bridge + thread_id + tags 的关联关系，
替代原先分散在 ServiceProvider 字符串 key、_GLOBAL_SUSPENDED_ENGINES、
_pipeline_thread_map 中的多套映射。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


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


MAX_TAGS_PER_PIPELINE = 8
"""每个管道允许的最大标签数量。"""
