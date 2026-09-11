"""
回滚管理器

提供操作日志记录、检查点管理和回滚执行功能。

存储形态为进程内内存账本（生产构造点均为无参构造，检查点/操作日志随插件
生命周期存续）；回滚的持久化由 git reverser 落真实版本库承载。
"""

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

logger = logging.getLogger(__name__)


class RollbackManager:
    """
    回滚管理器

    提供：
    - 检查点创建和管理
    - 操作日志记录
    - 回滚执行
    """

    def __init__(self, reverser_registry: ReverserRegistry | None = None):
        """
        初始化回滚管理器

        Args:
            reverser_registry: 逆操作器注册表
        """
        self.reverser_registry = reverser_registry or get_reverser_registry()

        # 内存存储
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
        return self._checkpoints.get(checkpoint_id)

    async def list_checkpoints(self, task_id: str) -> list[Checkpoint]:
        """
        列出任务的所有检查点

        Args:
            task_id: 任务 ID

        Returns:
            检查点列表
        """
        return [cp for cp in self._checkpoints.values() if cp.task_id == task_id]

    async def delete_checkpoint(self, checkpoint_id: str) -> bool:
        """
        删除检查点

        Args:
            checkpoint_id: 检查点 ID

        Returns:
            是否成功
        """
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
        # F-GITREV-1 幂等令牌：先置 ROLLING_BACK（落账）。reverse 成功但状态落账
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
        for operations in self._operations.values():
            for op in operations:
                if op.id == operation_id:
                    op.status = status
                    return

    def _get_next_sequence(self, task_id: str) -> int:
        """获取下一个序号"""
        if task_id not in self._sequence_counters:
            self._sequence_counters[task_id] = 0
        self._sequence_counters[task_id] += 1
        return self._sequence_counters[task_id]


# 全局回滚管理器实例
_global_rollback_manager: RollbackManager | None = None


def get_rollback_manager() -> RollbackManager:
    """
    获取回滚管理器实例

    Returns:
        回滚管理器实例
    """
    global _global_rollback_manager  # noqa: PLW0603

    if _global_rollback_manager is None:
        _global_rollback_manager = RollbackManager()

    return _global_rollback_manager
