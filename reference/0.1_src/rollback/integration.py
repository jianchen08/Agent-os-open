"""
回滚机制集成模块

将回滚机制与任务执行、Regenerate 功能集成
"""

import logging
from typing import Any

from src.rollback.manager import get_rollback_manager
from src.rollback.models import OperationType, RollbackResult

logger = logging.getLogger(__name__)


class TaskRollbackIntegration:
    """
    任务回滚集成

    在任务执行的关键节点自动创建检查点，
    支持 Regenerate 时回滚之前的操作
    """

    def __init__(self, session=None):
        """
        初始化

        Args:
            session: 数据库会话
        """
        self.manager = get_rollback_manager(session)
        # 会话 -> 检查点映射（用于 Regenerate）
        self._session_checkpoints: dict[str, list[str]] = {}
        # 消息 -> 检查点映射
        self._message_checkpoints: dict[str, str] = {}

    async def on_message_start(self, session_id: str, message_id: str) -> str:
        """
        消息处理开始时创建检查点

        在 Agent 开始处理用户消息前调用，
        创建检查点以便后续回滚

        Args:
            session_id: 会话 ID
            message_id: 消息 ID

        Returns:
            检查点 ID
        """
        checkpoint_id = await self.manager.create_checkpoint(
            task_id=session_id,
            name=f"before_message_{message_id[:8]}",
            description=f"消息 {message_id} 处理前的检查点",
            metadata={
                "message_id": message_id,
                "type": "message_start",
            },
        )

        # 记录映射
        if session_id not in self._session_checkpoints:
            self._session_checkpoints[session_id] = []
        self._session_checkpoints[session_id].append(checkpoint_id)
        self._message_checkpoints[message_id] = checkpoint_id

        logger.info(f"创建消息检查点 | session={session_id}, message={message_id}, checkpoint={checkpoint_id}")

        return checkpoint_id

    async def on_message_complete(self, session_id: str, message_id: str) -> None:
        """
        消息处理完成时的回调

        Args:
            session_id: 会话 ID
            message_id: 消息 ID
        """
        logger.debug(f"消息处理完成 | session={session_id}, message={message_id}")

    async def on_regenerate(
        self,
        session_id: str,
        original_message_id: str,
        rollback_operations: bool = True,
    ) -> RollbackResult | None:
        """
        Regenerate 时回滚之前的操作

        当用户请求重新生成时，回滚原消息执行的所有操作

        Args:
            session_id: 会话 ID
            original_message_id: 原消息 ID
            rollback_operations: 是否回滚操作

        Returns:
            回滚结果，如果不需要回滚则返回 None
        """
        if not rollback_operations:
            return None

        # 获取原消息的检查点
        checkpoint_id = self._message_checkpoints.get(original_message_id)

        if not checkpoint_id:
            logger.warning(f"未找到消息检查点 | message={original_message_id}")
            return None

        logger.info(
            f"开始回滚 Regenerate | session={session_id}, message={original_message_id}, checkpoint={checkpoint_id}"
        )

        # 回滚到检查点
        result = await self.manager.rollback(
            task_id=session_id,
            to_checkpoint=checkpoint_id,
        )

        logger.info(
            f"Regenerate 回滚完成 | 成功={result.rolled_back_count}, "
            f"跳过={result.skipped_count}, 失败={result.failed_count}"
        )

        return result

    async def record_tool_operation(
        self,
        session_id: str,
        tool_name: str,
        operation_type: OperationType,
        target: str,
        params: dict[str, Any],
        before_state: dict[str, Any] | None = None,
        after_state: dict[str, Any] | None = None,
        reversible: bool = True,
    ) -> str:
        """
        记录工具操作

        在工具执行后调用，记录操作以便回滚

        Args:
            session_id: 会话 ID（作为 task_id）
            tool_name: 工具名称
            operation_type: 操作类型
            target: 操作目标
            params: 操作参数
            before_state: 操作前状态
            after_state: 操作后状态
            reversible: 是否可逆

        Returns:
            操作日志 ID
        """
        return await self.manager.record_operation(
            task_id=session_id,
            tool_name=tool_name,
            operation_type=operation_type,
            target=target,
            params=params,
            before_state=before_state,
            after_state=after_state,
            reversible=reversible,
        )

    async def get_session_operations(self, session_id: str) -> list[dict[str, Any]]:
        """
        获取会话的所有操作

        Args:
            session_id: 会话 ID

        Returns:
            操作列表
        """
        operations = await self.manager.list_operations(session_id)
        return [op.to_dict() for op in operations]

    async def get_session_checkpoints(self, session_id: str) -> list[dict[str, Any]]:
        """
        获取会话的所有检查点

        Args:
            session_id: 会话 ID

        Returns:
            检查点列表
        """
        checkpoints = await self.manager.list_checkpoints(session_id)
        return [cp.to_dict() for cp in checkpoints]

    def clear_session(self, session_id: str) -> None:
        """
        清理会话数据

        Args:
            session_id: 会话 ID
        """
        # 清理内存中的映射
        if session_id in self._session_checkpoints:
            for cp_id in self._session_checkpoints[session_id]:
                # 清理消息映射
                for msg_id, cp in list(self._message_checkpoints.items()):
                    if cp == cp_id:
                        del self._message_checkpoints[msg_id]
            del self._session_checkpoints[session_id]


# 全局实例
_integration: TaskRollbackIntegration | None = None


def get_rollback_integration(session=None) -> TaskRollbackIntegration:
    """
    获取回滚集成实例

    Args:
        session: 数据库会话

    Returns:
        TaskRollbackIntegration 实例
    """
    global _integration  # noqa: PLW0603
    if _integration is None:
        _integration = TaskRollbackIntegration(session)
    return _integration
