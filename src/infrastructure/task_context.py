"""任务执行上下文。

封装单个任务从提交到终态的完整生命周期状态，
提供统一的清理和回滚接口，替代 TaskWorker 中 9 个散列字典的隐式耦合。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

logger = logging.getLogger(__name__)


class TaskExecutionContext:
    """单个任务的执行上下文。

    每个任务创建一个独立实例，封装该任务的全部运行时状态。
    支持异步并行：多个任务的 ctx 互不影响，失败隔离。

    Attributes:
        task_id: 任务 ID
        terminal_event: 终态信号，completed/failed 时 set
        wake_event: 唤醒信号，子任务完成或 idle 超时时 set
        active: 是否处于活跃执行状态
        bg_task: 后台 asyncio.Task 引用
        suspended_engine: 挂起的 PipelineEngine 引用
        resume_requested: 是否请求恢复
        idle_timer_registered: 是否已注册 idle 计时器
        workspace: 已解析的工作空间路径
        ws_meta: 工作空间元数据
        full_input: 已构建的完整输入字符串
        isolation_mode: 隔离模式
        has_explicit_workspace: 是否有显式工作空间
        agent_config_validated: AgentConfig 是否已验证
        lifecycle: 工作空间生命周期管理器引用
    """

    def __init__(self, task_id: str) -> None:
        self.task_id: str = task_id

        self.terminal_event: asyncio.Event = asyncio.Event()
        self.wake_event: asyncio.Event = asyncio.Event()

        self.active: bool = False
        self.bg_task: asyncio.Task | None = None
        self.suspended_engine: Any = None
        self.resume_requested: bool = False
        self.idle_timer_registered: bool = False
        # 任务总超时硬墙的 asyncio.TimerHandle 引用，cleanup 时取消。
        # 由 task_executor 在启动阶段按 agent_level 设置（L1=None 不创建）。
        self.total_timeout_handle: Any = None

        self.workspace: str = ""
        self.ws_meta: dict[str, Any] = {}
        self.full_input: str = ""
        self.isolation_mode: str = ""
        self.has_explicit_workspace: bool = False
        self.agent_config_validated: bool = False

        self.lifecycle: Any = None

    def cleanup(self, timer_manager: Any = None) -> None:
        """统一清理：重置执行状态 + 取消计时器。"""
        self.active = False
        self.suspended_engine = None
        self.resume_requested = False
        if timer_manager and self.idle_timer_registered:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(timer_manager.cancel_timer(self.task_id))
            except RuntimeError:
                pass
        self.idle_timer_registered = False
        # 取消任务总超时硬墙计时器（不依赖 timer_manager，直接 cancel handle）
        if self.total_timeout_handle is not None:
            with contextlib.suppress(Exception):
                self.total_timeout_handle.cancel()
            self.total_timeout_handle = None

    def rollback(self, task_service: Any = None) -> None:
        """准备阶段失败时的完整回滚。

        清理已创建的工作空间 + 删除任务记录 + 重置状态。
        """
        if self.lifecycle and self.ws_meta:
            with contextlib.suppress(Exception):
                self.lifecycle.on_task_failed(self.workspace, self.ws_meta)
        if task_service and self.task_id:
            with contextlib.suppress(Exception):
                task_service.hard_delete_sync(self.task_id)
        self.cleanup()

    def set_terminal(self) -> None:
        """标记终态。"""
        self.terminal_event.set()
        self.active = False

    def __repr__(self) -> str:
        return (
            f"TaskExecutionContext(task_id={self.task_id!r}, "
            f"active={self.active}, "
            f"suspended={self.suspended_engine is not None})"
        )
