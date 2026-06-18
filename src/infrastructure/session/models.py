"""会话模型定义。

会话（Session）只是一个标记/标签，维护属于同一个交互上下文的管道引用集合。
管道的创建、消息收发、ID 生成都由管道自身负责，会话不参与。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SessionModel:
    """会话模型 — 管道历史的引用集合。

    会话只是一个标记，记录哪些管道属于这个会话。
    不负责创建管道、生成 pipeline_id 或管理管道生命周期。

    Attributes:
        session_id: 会话标签，创建后固定不变
        channel_type: 来源通道 — "cli" 或 "web"
        channel_ref: 通道级引用
        pipeline_ids: 属于这个会话的 pipeline_run_id 引用列表
        active_pipeline_id: 最近一次使用的 pipeline_run_id（仅引用）
        created_at: 创建时间戳
        last_active_at: 最后活跃时间戳
        metadata: 扩展元数据
    """

    session_id: str = field(
        default_factory=lambda: uuid.uuid4().hex[:12]
    )
    channel_type: str = "cli"
    channel_ref: str = ""
    pipeline_ids: list[str] = field(default_factory=list)
    active_pipeline_id: str = ""
    created_at: float = field(default_factory=time.time)
    last_active_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def touch(self) -> None:
        """更新最后活跃时间戳。"""
        self.last_active_at = time.time()

    def register_pipeline(self, pipeline_id: str) -> None:
        """将一个管道 ID 注册到本会话的引用集合中。

        由管道运行完成后或运行前调用，会话只做记录，不创建管道。
        """
        if pipeline_id and pipeline_id not in self.pipeline_ids:
            self.pipeline_ids.append(pipeline_id)
        self.active_pipeline_id = pipeline_id
        self.touch()

    def clear(self) -> None:
        """清空管道引用列表。"""
        self.pipeline_ids.clear()
        self.active_pipeline_id = ""
        self.touch()
