"""
生命周期管理器 - 负责检查点、恢复和经验沉淀

职责：
- 管理检查点的创建和恢复
- 处理经验沉淀
- 管理任务执行回调
- 处理后台任务追踪
"""

import asyncio
import logging
import uuid
from typing import Any

from src.agents.interfaces import IEmbeddingService
from src.agents.types import ToolCallRecord

logger = logging.getLogger(__name__)


class LifecycleManager:
    """
    生命周期管理器

    负责检查点管理、经验沉淀和后台任务追踪
    """

    def __init__(
        self,
        user_id: str | None = None,
        session_id: str | None = None,
        db_session: Any | None = None,
        embedding_service: IEmbeddingService | None = None,
        enable_learning: bool = True,
    ):
        """
        初始化生命周期管理器

        Args:
            user_id: 用户 ID
            session_id: 会话 ID
            db_session: 数据库会话
            embedding_service: 嵌入服务
            enable_learning: 是否启用学习功能
        """
        self.user_id = user_id
        self.session_id = session_id
        self._db_session = db_session
        self._embedding_service = embedding_service
        self.enable_learning = enable_learning

        # 任务管理器：追踪所有后台任务，防止内存泄漏
        self._pending_tasks: set = set()

    async def store_experience(
        self,
        intent: str,
        result: str,
        iterations: int,
        tool_calls: list[ToolCallRecord],
        tags: list[str] | None = None,
    ) -> None:
        """
        存储成功经验

        Args:
            intent: 用户意图
            result: 执行结果
            iterations: 迭代次数
            tool_calls: 工具调用记录
            tags: 标签
        """
        if not self.enable_learning:
            return

        if not self._db_session or not self.user_id:
            return

        try:
            intent_vector = None
            if self._embedding_service:
                try:
                    intent_vector = await self._embedding_service.embed_text(intent)
                except Exception:
                    pass

            from src.db.models import EpisodesMemory

            episode = EpisodesMemory(
                user_id=uuid.UUID(self.user_id),
                session_id=uuid.UUID(self.session_id) if self.session_id else None,
                intent_text=intent,
                intent_vector=intent_vector,
                execution_summary=f"使用 {len(tool_calls)} 个工具，迭代 {iterations} 次",
                final_score=1.0,
                tags=tags,
            )

            self._db_session.add(episode)
            await self._db_session.commit()

            logger.debug(f"[LifecycleManager] 经验已存储 | intent={intent[:100]}...")

        except Exception as e:
            logger.warning(f"[LifecycleManager] 存储经验失败: {e}")

    def schedule_experience_storage(
        self,
        intent: str,
        result: str,
        iterations: int,
        tool_calls: list[ToolCallRecord],
        tags: list[str] | None = None,
    ) -> None:
        """
        调度经验存储（异步执行）

        Args:
            intent: 用户意图
            result: 执行结果
            iterations: 迭代次数
            tool_calls: 工具调用记录
            tags: 标签
        """
        if not self.enable_learning:
            return

        task = asyncio.create_task(
            self.store_experience(intent, result, iterations, tool_calls, tags)
        )
        # 添加到追踪集合，完成后自动移除
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    async def cleanup_tasks(self) -> None:
        """
        清理已完成的任务

        移除已完成的任务，防止内存泄漏
        """
        if not self._pending_tasks:
            return

        # 收集已完成的任务
        done = {task for task in self._pending_tasks if task.done()}
        # 从集合中移除
        self._pending_tasks.difference_update(done)

        if done:
            logger.debug(f"[LifecycleManager] 清理已完成任务 | count={len(done)}")

    async def cleanup(self) -> None:
        """
        清理所有后台任务

        取消所有待处理的后台任务，释放资源
        """
        # 取消所有待处理任务
        if self._pending_tasks:
            for task in list(self._pending_tasks):
                if not task.done():
                    task.cancel()
            # 等待所有任务完成或被取消
            await asyncio.gather(*self._pending_tasks, return_exceptions=True)
            self._pending_tasks.clear()

        logger.debug("[LifecycleManager] 生命周期管理器资源已清理")
