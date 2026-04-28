"""会话模型定义。

会话（Session）只是一个"筐"，用来装属于同一个交互上下文的 pipeline 执行记录。
对话历史来自执行记录，不由会话管理。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SessionModel:
    """会话模型 — 装管道历史的筐。

    session_id 固定不变，pipeline_ids 记录所有属于这个筐的 pipeline run。

    Attributes:
        session_id: 筐的标签，创建后固定不变
        channel_type: 来源通道 — "cli" 或 "web"
        channel_ref: 通道级引用
        pipeline_ids: 属于这个筐的所有 pipeline_run_id
        active_pipeline_id: 当前正在用的 pipeline_run_id
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
        self.last_active_at = time.time()

    def generate_pipeline_id(self) -> str:
        """生成新 pipeline_id 并加到 pipeline_ids。"""
        pid = uuid.uuid4().hex[:12]
        self.active_pipeline_id = pid
        self.pipeline_ids.append(pid)
        self.touch()
        return pid

    def clear(self) -> None:
        """清空管道列表，保留当前 active_pipeline_id。

        session_id 和 active_pipeline_id 均不变。
        """
        self.pipeline_ids.clear()
        self.touch()
