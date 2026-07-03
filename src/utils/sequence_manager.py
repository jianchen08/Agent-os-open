"""
序列号管理器

为嵌套ID生成提供全局序列号管理
"""

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import ExecutionRecord, Session

logger = logging.getLogger(__name__)


class SequenceManager:
    """
    序列号管理器

    管理每个父ID下的子序列号，确保唯一性
    """

    def __init__(self):
        """初始化序列号管理器"""
        self._sequences: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._initialized = False

    async def initialize_from_db(self, db_session: AsyncSession):  # noqa: PLR0912,PLR0915
        """
        从数据库恢复各会话的当前序列号

        服务器重启后，从数据库中查询每个会话的最大序列号，
        并初始化内存中的序列号字典，避免 ID 重复。

        Args:
            db_session: 数据库会话
        """
        if self._initialized:
            logger.info("[SequenceManager] 已经初始化过，跳过重复初始化")
            return

        async with self._lock:
            try:
                # 从 src.utils.id_encoder 导入一次，避免重复导入
                from src.utils.id_encoder import parse_nested_id  # noqa: PLC0415

                # 1. 查询所有会话，恢复用户级别的会话序列号
                session_result = await db_session.execute(select(Session))
                sessions = session_result.scalars().all()

                user_session_sequences: dict[str, int] = {}

                for session in sessions:
                    try:
                        # 会话 ID 格式: thread-<sequence>
                        parsed = parse_nested_id(session.id)
                        sequences = parsed.get("sequences", [])

                        # 第一个序列号是会话级别的
                        if len(sequences) > 0:
                            # 使用用户 ID 作为 key（与创建会话时保持一致）
                            user_key = f"user-{session.user_id}"
                            if user_key not in user_session_sequences:
                                user_session_sequences[user_key] = 0
                            user_session_sequences[user_key] = max(user_session_sequences[user_key], sequences[0])
                    except Exception as e:
                        # 解析失败时跳过（可能是旧格式的 UUID）
                        logger.debug(f"解析会话ID失败 | id={session.id} | error={e}")

                # 2. 查询所有执行记录，恢复执行记录级别的序列号
                result = await db_session.execute(select(ExecutionRecord))
                records = result.scalars().all()

                # 从每个记录的 message_data.order.sequence 中提取最大序列号
                session_sequences: dict[str, int] = {}
                user_sequences: dict[str, int] = {}

                for record in records:
                    # 处理会话级别序列号
                    session_id = record.session_id
                    if record.message_data and "order" in record.message_data:
                        order = record.message_data.get("order", {})
                        seq = order.get("sequence", 0)
                        depth = order.get("depth", 0)

                        # 顶层记录（depth=0）的序列号
                        if depth == 0:
                            if session_id not in session_sequences:
                                session_sequences[session_id] = 0
                            session_sequences[session_id] = max(session_sequences[session_id], seq)

                        # 如果有父记录ID，也记录父子关系序列号
                        parent_id = record.parent_record_id
                        if parent_id:
                            key = parent_id
                            if key not in session_sequences:
                                session_sequences[key] = 0
                            session_sequences[key] = max(session_sequences[key], seq)

                # 从嵌套 ID 中提取用户级别序列号
                # ID 格式: exec-<user_seq>-<session_seq>-<record_seq>...
                for record in records:
                    try:
                        parsed = parse_nested_id(record.id)
                        sequences = parsed.get("sequences", [])

                        # 第一个序列号是用户级别的
                        if len(sequences) > 0:
                            # 使用会话ID作为用户序列号的key
                            user_key = f"user-{record.session_id}"
                            if user_key not in user_sequences:
                                user_sequences[user_key] = 0
                            user_sequences[user_key] = max(user_sequences[user_key], sequences[0])
                    except Exception as e:
                        # 解析失败时跳过
                        logger.debug(f"解析嵌套ID失败 | id={record.id} | error={e}")

                # 3. 恢复 __root__ 序列号（顶层记录，无父记录）
                root_max_sequence = 0
                for record in records:
                    if record.parent_record_id is None:
                        try:
                            parsed = parse_nested_id(record.id)
                            sequences = parsed.get("sequences", [])
                            if len(sequences) > 0:
                                root_max_sequence = max(root_max_sequence, sequences[0])
                        except Exception as e:
                            logger.debug(f"解析顶层记录ID失败 | id={record.id} | error={e}")

                if root_max_sequence > 0:
                    self._sequences["__root__"] = root_max_sequence
                    logger.info(f"[SequenceManager] 恢复 __root__ 序列号 | sequence={root_max_sequence}")

                # 合并所有序列号（注意：user_session_sequences 优先级更高，因为它是从 Session 表直接恢复的）
                self._sequences.update(session_sequences)
                self._sequences.update(user_sequences)
                self._sequences.update(user_session_sequences)  # 最后更新，确保覆盖

                self._initialized = True

                logger.info(
                    f"[SequenceManager] 从数据库恢复序列号 | "
                    f"会话数={len(session_sequences)} | "
                    f"用户数={len(user_sequences)} | "
                    f"用户会话数={len(user_session_sequences)} | "
                    f"__root__={root_max_sequence} | "
                    f"总序列数={len(self._sequences)}"
                )

            except Exception as e:
                logger.error(
                    f"[SequenceManager] 从数据库恢复序列号失败 | error={e}",
                    exc_info=True,
                )
                # 初始化失败不影响启动，使用空字典继续

    async def get_next_sequence(self, parent_id: str | None = None) -> int:
        """
        获取下一个序列号

        Args:
            parent_id: 父ID（None表示根序列）

        Returns:
            下一个序列号
        """
        async with self._lock:
            key = parent_id or "__root__"

            if key not in self._sequences:
                self._sequences[key] = 0

            self._sequences[key] += 1
            sequence = self._sequences[key]

            logger.debug(f"[SequenceManager] 生成序列号 | parent_id={parent_id} | sequence={sequence}")

            return sequence

    async def reset(self, parent_id: str | None = None):
        """
        重置序列号（异步版本）

        Args:
            parent_id: 父ID（None表示重置所有）
        """
        async with self._lock:
            if parent_id is None:
                self._sequences.clear()
                logger.info("[SequenceManager] 重置所有序列号")
            else:
                key = parent_id or "__root__"
                if key in self._sequences:
                    del self._sequences[key]
                    logger.info(f"[SequenceManager] 重置序列号 | parent_id={parent_id}")

    def get_current_sequence(self, parent_id: str | None = None) -> int:
        """
        获取当前序列号（不递增）

        Args:
            parent_id: 父ID

        Returns:
            当前序列号
        """
        key = parent_id or "__root__"
        return self._sequences.get(key, 0)


# 全局序列号管理器实例
_global_sequence_manager: SequenceManager | None = None


def get_sequence_manager() -> SequenceManager:
    """获取全局序列号管理器实例"""
    global _global_sequence_manager  # noqa: PLW0603

    if _global_sequence_manager is None:
        _global_sequence_manager = SequenceManager()
        logger.info("[SequenceManager] 创建全局序列号管理器")

    return _global_sequence_manager


async def get_next_sequence(parent_id: str | None = None) -> int:
    """
    获取下一个序列号（便捷函数）

    Args:
        parent_id: 父ID

    Returns:
        下一个序列号
    """
    manager = get_sequence_manager()
    return await manager.get_next_sequence(parent_id)
