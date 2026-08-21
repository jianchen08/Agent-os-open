"""
回滚管理器

提供操作日志记录、检查点管理和回滚执行功能
"""

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Any

from models import (
    Checkpoint,
    OperationLog,
    OperationStatus,
    OperationType,
    RollbackResult,
)
from reversers import ReverserRegistry, get_reverser_registry
from sqlalchemy import select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class RollbackManager:
    """
    回滚管理器

    提供：
    - 检查点创建和管理
    - 操作日志记录
    - 回滚执行
    """

    def __init__(
        self,
        session: Session | None = None,
        reverser_registry: ReverserRegistry | None = None,
    ):
        """
        初始化回滚管理器

        Args:
            session: 数据库会话（可选，用于持久化）
            reverser_registry: 逆操作器注册表
        """
        self.session = session
        self.reverser_registry = reverser_registry or get_reverser_registry()

        # 内存存储（当没有数据库会话时使用）
        self._checkpoints: dict[str, Checkpoint] = {}
        self._operations: dict[str, list[OperationLog]] = {}  # task_id -> operations
        self._sequence_counters: dict[str, int] = {}  # task_id -> sequence

    # ==================== 检查点管理 ====================

    async def create_checkpoint(
        self,
        task_id: str,
        name: str | None = None,
        description: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """
        创建检查点

        Args:
            task_id: 任务 ID
            name: 检查点名称
            description: 检查点描述
            metadata: 元数据

        Returns:
            检查点 ID
        """
        checkpoint = Checkpoint(
            id=str(uuid.uuid4()),
            task_id=task_id,
            name=name or f"checkpoint_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
            description=description,
            metadata=metadata or {},
            created_at=datetime.now(),
            sequence=self._sequence_counters.get(task_id, 0),
        )

        if self.session:
            await asyncio.to_thread(self._save_checkpoint_to_db, checkpoint)
        else:
            self._checkpoints[checkpoint.id] = checkpoint

        logger.info(f"创建检查点: {checkpoint.id} (任务: {task_id})")
        return checkpoint.id

    async def get_checkpoint(self, checkpoint_id: str) -> Checkpoint | None:
        """
        获取检查点

        Args:
            checkpoint_id: 检查点 ID

        Returns:
            检查点对象
        """
        if self.session:
            return await asyncio.to_thread(self._load_checkpoint_from_db, checkpoint_id)
        return self._checkpoints.get(checkpoint_id)

    async def list_checkpoints(self, task_id: str) -> list[Checkpoint]:
        """
        列出任务的所有检查点

        Args:
            task_id: 任务 ID

        Returns:
            检查点列表
        """
        if self.session:
            return await asyncio.to_thread(self._list_checkpoints_from_db, task_id)

        return [cp for cp in self._checkpoints.values() if cp.task_id == task_id]

    async def delete_checkpoint(self, checkpoint_id: str) -> bool:
        """
        删除检查点

        Args:
            checkpoint_id: 检查点 ID

        Returns:
            是否成功
        """
        if self.session:
            return await asyncio.to_thread(self._delete_checkpoint_from_db, checkpoint_id)

        if checkpoint_id in self._checkpoints:
            del self._checkpoints[checkpoint_id]
            return True
        return False

    # ==================== 操作日志管理 ====================

    async def record_operation(
        self,
        task_id: str,
        tool_name: str,
        operation_type: OperationType,
        target: str,
        params: dict[str, Any],
        before_state: dict[str, Any] | None = None,
        after_state: dict[str, Any] | None = None,
        reversible: bool = True,
        reverse_action: dict[str, Any] | None = None,
        checkpoint_id: str | None = None,
    ) -> str:
        """
        记录操作日志

        Args:
            task_id: 任务 ID
            tool_name: 工具名称
            operation_type: 操作类型
            target: 操作目标
            params: 操作参数
            before_state: 操作前状态
            after_state: 操作后状态
            reversible: 是否可逆
            reverse_action: 逆操作定义
            checkpoint_id: 关联的检查点 ID

        Returns:
            操作日志 ID
        """
        # 获取序号
        sequence = self._get_next_sequence(task_id)

        operation = OperationLog(
            id=str(uuid.uuid4()),
            task_id=task_id,
            checkpoint_id=checkpoint_id,
            tool_name=tool_name,
            operation_type=operation_type,
            target=target,
            params=params,
            before_state=before_state,
            after_state=after_state,
            reversible=reversible,
            reverse_action=reverse_action,
            sequence=sequence,
            status=OperationStatus.EXECUTED,
            created_at=datetime.now(),
        )

        if self.session:
            await asyncio.to_thread(self._save_operation_to_db, operation)
        else:
            if task_id not in self._operations:
                self._operations[task_id] = []
            self._operations[task_id].append(operation)

        logger.debug(f"记录操作: {operation.id} (任务: {task_id}, 工具: {tool_name}, 类型: {operation_type.value})")
        return operation.id

    async def get_operation(self, operation_id: str) -> OperationLog | None:
        """
        获取操作日志

        Args:
            operation_id: 操作日志 ID

        Returns:
            操作日志对象
        """
        if self.session:
            return await asyncio.to_thread(self._load_operation_from_db, operation_id)

        for operations in self._operations.values():
            for op in operations:
                if op.id == operation_id:
                    return op
        return None

    async def list_operations(
        self,
        task_id: str,
        checkpoint_id: str | None = None,
        status: OperationStatus | None = None,
    ) -> list[OperationLog]:
        """
        列出任务的操作日志

        Args:
            task_id: 任务 ID
            checkpoint_id: 检查点 ID（可选，筛选该检查点之后的操作）
            status: 状态筛选

        Returns:
            操作日志列表（按序号排序）
        """
        if self.session:
            return await asyncio.to_thread(self._list_operations_from_db, task_id, checkpoint_id, status)

        operations = self._operations.get(task_id, [])

        # 筛选检查点之后的操作
        if checkpoint_id:
            checkpoint = await self.get_checkpoint(checkpoint_id)
            if checkpoint:
                operations = [op for op in operations if op.created_at >= checkpoint.created_at]

        # 筛选状态
        if status:
            operations = [op for op in operations if op.status == status]

        # 按序号排序
        return sorted(operations, key=lambda x: x.sequence)

    # ==================== 回滚执行 ====================

    async def rollback(
        self,
        task_id: str,
        to_checkpoint: str | None = None,
        steps: int | None = None,
    ) -> RollbackResult:
        """
        回滚任务操作

        Args:
            task_id: 任务 ID
            to_checkpoint: 回滚到指定检查点（可选）
            steps: 回滚最近 N 步操作（可选）

        Returns:
            回滚结果
        """
        result = RollbackResult()

        # 获取需要回滚的操作
        operations = await self._get_operations_to_rollback(task_id, to_checkpoint, steps)

        if not operations:
            result.warnings.append("没有需要回滚的操作")
            return result

        logger.info(f"开始回滚任务 {task_id}，共 {len(operations)} 个操作")

        # 按序号倒序执行逆操作
        for operation in reversed(operations):
            op_result = await self._rollback_single_operation(operation)
            result.operations.append(
                {
                    "operation_id": operation.id,
                    "tool_name": operation.tool_name,
                    "target": operation.target,
                    **op_result,
                }
            )

            if op_result["success"]:
                result.rolled_back_count += 1
            elif op_result.get("skipped"):
                result.skipped_count += 1
                if op_result.get("warning"):
                    result.warnings.append(op_result["warning"])
            else:
                result.failed_count += 1
                result.errors.append(op_result.get("message", "未知错误"))

        # 判断整体是否成功
        result.success = result.failed_count == 0

        logger.info(
            f"回滚完成: 成功 {result.rolled_back_count}, 跳过 {result.skipped_count}, 失败 {result.failed_count}"
        )

        return result

    async def _get_operations_to_rollback(
        self,
        task_id: str,
        to_checkpoint: str | None,
        steps: int | None,
    ) -> list[OperationLog]:
        """获取需要回滚的操作列表"""
        # 获取所有已执行的操作
        operations = await self.list_operations(task_id, status=OperationStatus.EXECUTED)

        if to_checkpoint:
            # 回滚到检查点
            checkpoint = await self.get_checkpoint(to_checkpoint)
            if checkpoint:
                # 按单调递增的操作序号筛选，而非墙钟 created_at：
                # Windows 下 datetime.now() 精度约 15ms，同测试内连续创建的 op 与
                # checkpoint 可能落在同一 tick，严格 `>` 会把它们全部过滤掉，
                # 导致回滚静默 no-op。
                operations = [op for op in operations if op.sequence > checkpoint.sequence]
        elif steps:
            # 回滚最近 N 步
            operations = operations[-steps:]

        return operations

    async def _rollback_single_operation(self, operation: OperationLog) -> dict[str, Any]:
        """回滚单个操作"""
        # F-GITREV-1 幂等：已回滚中/已回滚的操作直接跳过（令牌状态），不重复执行
        if operation.status in (OperationStatus.ROLLING_BACK, OperationStatus.ROLLED_BACK):
            return {
                "success": False,
                "skipped": True,
                "warning": f"操作已在回滚中或已回滚: {operation.tool_name} -> {operation.target}",
            }

        # 检查是否可逆
        if not operation.reversible:
            await self._update_operation_status(operation.id, OperationStatus.EXECUTED)
            return {
                "success": False,
                "skipped": True,
                "warning": f"操作不可逆: {operation.tool_name} -> {operation.target}",
            }

        # 获取逆操作器
        reverser = self.reverser_registry.get_reverser(operation.tool_name)

        if not reverser:
            return {
                "success": False,
                "skipped": True,
                "warning": f"未找到逆操作器: {operation.tool_name}",
            }

        # 执行逆操作
        # F-GITREV-1 幂等令牌：先置 ROLLING_BACK（持久化）。reverse 成功但状态落库
        # 失败时，op 停在 ROLLING_BACK，重试不再命中 EXECUTED 过滤——杜绝双回滚；
        # 令牌本身写不进去则拒绝执行（失败即停）。
        try:
            await self._update_operation_status(operation.id, OperationStatus.ROLLING_BACK)
        except Exception:
            return {
                "success": False,
                "message": f"置回滚中令牌失败，拒绝执行: {operation.tool_name} -> {operation.target}",
            }

        try:
            result = await reverser.reverse(operation)

            if result["success"]:
                await self._update_operation_status(operation.id, OperationStatus.ROLLED_BACK)

            return result

        except Exception as e:
            logger.error(f"逆操作执行异常: {e}")
            return {
                "success": False,
                "message": f"逆操作执行异常: {str(e)}",
            }

    async def _update_operation_status(self, operation_id: str, status: OperationStatus) -> None:
        """更新操作状态"""
        if self.session:
            await asyncio.to_thread(self._update_operation_status_in_db, operation_id, status)
        else:
            for operations in self._operations.values():
                for op in operations:
                    if op.id == operation_id:
                        op.status = status
                        return

    def _get_next_sequence(self, task_id: str) -> int:
        """获取下一个序号（DB 模式按库内最大序号续接，跨实例/重启保持单调）"""
        if self.session:
            # 持久化层必须保证 sequence 单调：换实例后内存计数器已重置，
            # 若从 1 重新计数会出现重复序号，回滚按序号定位将错乱。
            from _db_models import RollbackOperationLog  # noqa: PLC0415
            from sqlalchemy import func

            result = self.session.execute(
                select(func.max(RollbackOperationLog.sequence)).where(
                    RollbackOperationLog.task_id == task_id
                )
            )
            self._sequence_counters[task_id] = result.scalar() or 0

        if task_id not in self._sequence_counters:
            self._sequence_counters[task_id] = 0
        self._sequence_counters[task_id] += 1
        return self._sequence_counters[task_id]

    # ==================== 数据库操作（占位） ====================

    def _save_checkpoint_to_db(self, checkpoint: Checkpoint) -> None:
        """保存检查点到数据库"""
        assert self.session is not None  # 仅经 if self.session 分支的 to_thread 调用
        from _db_models import RollbackCheckpoint  # noqa: PLC0415

        db_checkpoint = RollbackCheckpoint(
            id=checkpoint.id,
            task_id=checkpoint.task_id,
            name=checkpoint.name,
            description=checkpoint.description,
            checkpoint_metadata=checkpoint.metadata,
            created_at=checkpoint.created_at,
            sequence=checkpoint.sequence,
        )
        self.session.add(db_checkpoint)
        self.session.commit()

    def _load_checkpoint_from_db(self, checkpoint_id: str) -> Checkpoint | None:
        """从数据库加载检查点"""
        assert self.session is not None  # 仅经 if self.session 分支的 to_thread 调用
        from _db_models import RollbackCheckpoint  # noqa: PLC0415

        result = self.session.execute(select(RollbackCheckpoint).where(RollbackCheckpoint.id == checkpoint_id))
        db_checkpoint = result.scalar_one_or_none()

        if db_checkpoint:
            return Checkpoint(
                id=db_checkpoint.id,
                task_id=db_checkpoint.task_id,
                name=db_checkpoint.name,
                description=db_checkpoint.description,
                metadata=db_checkpoint.checkpoint_metadata or {},
                created_at=db_checkpoint.created_at,
                sequence=db_checkpoint.sequence,
            )
        return None

    def _list_checkpoints_from_db(self, task_id: str) -> list[Checkpoint]:
        """从数据库列出检查点"""
        assert self.session is not None  # 仅经 if self.session 分支的 to_thread 调用
        from _db_models import RollbackCheckpoint  # noqa: PLC0415

        result = self.session.execute(
            select(RollbackCheckpoint)
            .where(RollbackCheckpoint.task_id == task_id)
            .order_by(RollbackCheckpoint.created_at)
        )
        db_checkpoints = result.scalars().all()

        return [
            Checkpoint(
                id=cp.id,
                task_id=cp.task_id,
                name=cp.name,
                description=cp.description,
                metadata=cp.checkpoint_metadata or {},
                created_at=cp.created_at,
            )
            for cp in db_checkpoints
        ]

    def _delete_checkpoint_from_db(self, checkpoint_id: str) -> bool:
        """从数据库删除检查点"""
        assert self.session is not None  # 仅经 if self.session 分支的 to_thread 调用
        from _db_models import RollbackCheckpoint  # noqa: PLC0415

        result = self.session.execute(select(RollbackCheckpoint).where(RollbackCheckpoint.id == checkpoint_id))
        db_checkpoint = result.scalar_one_or_none()

        if db_checkpoint:
            self.session.delete(db_checkpoint)
            self.session.commit()
            return True
        return False

    def _save_operation_to_db(self, operation: OperationLog) -> None:
        """保存操作日志到数据库"""
        assert self.session is not None  # 仅经 if self.session 分支的 to_thread 调用
        from _db_models import RollbackOperationLog  # noqa: PLC0415

        db_operation = RollbackOperationLog(
            id=operation.id,
            task_id=operation.task_id,
            checkpoint_id=operation.checkpoint_id,
            tool_name=operation.tool_name,
            operation_type=operation.operation_type.value,
            target=operation.target,
            params=operation.params,
            before_state=operation.before_state,
            after_state=operation.after_state,
            reversible=operation.reversible,
            reverse_action=operation.reverse_action,
            sequence=operation.sequence,
            status=operation.status.value,
            error_message=operation.error_message,
            created_at=operation.created_at,
        )
        self.session.add(db_operation)
        self.session.commit()

    def _load_operation_from_db(self, operation_id: str) -> OperationLog | None:
        """从数据库加载操作日志"""
        assert self.session is not None  # 仅经 if self.session 分支的 to_thread 调用
        from _db_models import RollbackOperationLog  # noqa: PLC0415

        result = self.session.execute(select(RollbackOperationLog).where(RollbackOperationLog.id == operation_id))
        db_op = result.scalar_one_or_none()

        if db_op:
            return OperationLog.from_dict(
                {
                    "id": db_op.id,
                    "task_id": db_op.task_id,
                    "checkpoint_id": db_op.checkpoint_id,
                    "tool_name": db_op.tool_name,
                    "operation_type": db_op.operation_type,
                    "target": db_op.target,
                    "params": db_op.params,
                    "before_state": db_op.before_state,
                    "after_state": db_op.after_state,
                    "reversible": db_op.reversible,
                    "reverse_action": db_op.reverse_action,
                    "sequence": db_op.sequence,
                    "status": db_op.status,
                    "error_message": db_op.error_message,
                    "created_at": db_op.created_at.isoformat(),
                }
            )
        return None

    def _list_operations_from_db(
        self,
        task_id: str,
        checkpoint_id: str | None,
        status: OperationStatus | None,
    ) -> list[OperationLog]:
        """从数据库列出操作日志"""
        assert self.session is not None  # 仅经 if self.session 分支的 to_thread 调用
        from _db_models import RollbackOperationLog  # noqa: PLC0415

        query = select(RollbackOperationLog).where(RollbackOperationLog.task_id == task_id)

        if checkpoint_id:
            # 获取检查点时间（同步直查，避免同步方法调 async 接口）
            from _db_models import RollbackCheckpoint  # noqa: PLC0415

            cp_result = self.session.execute(
                select(RollbackCheckpoint).where(RollbackCheckpoint.id == checkpoint_id)
            )
            db_cp = cp_result.scalar_one_or_none()
            if db_cp:
                query = query.where(RollbackOperationLog.created_at >= db_cp.created_at)

        if status:
            query = query.where(RollbackOperationLog.status == status.value)

        query = query.order_by(RollbackOperationLog.sequence)

        result = self.session.execute(query)
        db_operations = result.scalars().all()

        return [
            OperationLog.from_dict(
                {
                    "id": op.id,
                    "task_id": op.task_id,
                    "checkpoint_id": op.checkpoint_id,
                    "tool_name": op.tool_name,
                    "operation_type": op.operation_type,
                    "target": op.target,
                    "params": op.params,
                    "before_state": op.before_state,
                    "after_state": op.after_state,
                    "reversible": op.reversible,
                    "reverse_action": op.reverse_action,
                    "sequence": op.sequence,
                    "status": op.status,
                    "created_at": op.created_at.isoformat(),
                }
            )
            for op in db_operations
        ]

    def _update_operation_status_in_db(self, operation_id: str, status: OperationStatus) -> None:
        """更新数据库中的操作状态"""
        assert self.session is not None  # 仅经 if self.session 分支的 to_thread 调用
        from _db_models import RollbackOperationLog  # noqa: PLC0415

        result = self.session.execute(select(RollbackOperationLog).where(RollbackOperationLog.id == operation_id))
        db_op = result.scalar_one_or_none()

        if db_op:
            db_op.status = status.value
            self.session.commit()


# 全局回滚管理器实例
_global_rollback_manager: RollbackManager | None = None


def get_rollback_manager(session: Session | None = None) -> RollbackManager:
    """
    获取回滚管理器实例

    Args:
        session: 数据库会话

    Returns:
        回滚管理器实例
    """
    global _global_rollback_manager  # noqa: PLW0603

    if session:
        # 如果提供了会话，创建新实例
        return RollbackManager(session=session)

    if _global_rollback_manager is None:
        _global_rollback_manager = RollbackManager()

    return _global_rollback_manager
