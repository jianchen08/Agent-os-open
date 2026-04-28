"""会话模型定义。

定义会话（Session）的数据结构。会话代表用户的一个持续交互上下文，
跨多次管道执行（pipeline run）持久存在。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SessionModel:
    """业务级会话模型。

    一个会话代表用户的持续交互上下文，跨多次 pipeline run 持久存在。

    设计要点：
    - session_id 在 CLI 进程/Web 线程生命周期内固定不变
    - /clear 只清空 conversation_history，不改变 session_id
    - 每次调用 engine.run() 前通过 generate_pipeline_id() 获取新的 pipeline_id

    Attributes:
        session_id: 会话唯一标识，创建后固定不变
        channel_type: 来源通道 — "cli" 或 "web"
        channel_ref: 通道级引用（CLI: session_dir；Web: thread_id）
        conversation_history: 对话消息列表
        active_pipeline_id: 当前/最近一次 pipeline run 的 ID
        turn_count: 已完成的用户轮次
        created_at: 会话创建时间戳（epoch seconds）
        last_active_at: 最后活跃时间戳
        metadata: 扩展元数据
    """

    session_id: str = field(
        default_factory=lambda: uuid.uuid4().hex[:12]
    )
    channel_type: str = "cli"
    channel_ref: str = ""
    conversation_history: list[dict[str, Any]] = field(
        default_factory=list
    )
    active_pipeline_id: str = ""
    turn_count: int = 0
    created_at: float = field(default_factory=time.time)
    last_active_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def touch(self) -> None:
        """更新最后活跃时间戳。"""
        self.last_active_at = time.time()

    def generate_pipeline_id(self) -> str:
        """生成新的 pipeline_id，用于下一次 engine.run() 调用。

        Returns:
            新的 pipeline_id 字符串
        """
        self.active_pipeline_id = uuid.uuid4().hex[:12]
        self.touch()
        return self.active_pipeline_id

    def clear_history(self) -> None:
        """清空对话历史并重置轮次计数。

        不改变 session_id。旧的 active_pipeline_id 也保留，
        以确保挂起的引擎和绑定的任务仍能找到正确的管道。
        """
        self.conversation_history.clear()
        self.turn_count = 0
        self.touch()
