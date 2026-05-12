"""
检查点系统

提供任务执行过程中的状态保存和恢复功能
"""

import json
import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class CheckpointType(Enum):
    """检查点类型"""

    AUTO = "auto"  # 自动检查点
    MANUAL = "manual"  # 手动检查点
    MILESTONE = "milestone"  # 里程碑检查点


@dataclass
class CheckpointMetadata:
    """检查点元数据"""

    checkpoint_id: str
    task_id: str
    created_at: datetime
    description: str
    version: str = "1.0"
    tags: list[str] = None
    size_bytes: int = 0
    compressed: bool = False

    def __post_init__(self):
        if self.tags is None:
            self.tags = []


@dataclass
class CheckpointData:
    """检查点数据"""

    metadata: CheckpointMetadata
    state: dict[str, Any]
    messages: list[dict[str, Any]] = None
    tool_calls: list[dict[str, Any]] = None
    variables: dict[str, Any] = None
    context: dict[str, Any] = None

    def __post_init__(self):
        if self.messages is None:
            self.messages = []
        if self.tool_calls is None:
            self.tool_calls = []
        if self.variables is None:
            self.variables = {}
        if self.context is None:
            self.context = {}


class CheckpointStorage(ABC):
    """检查点存储接口"""

    @abstractmethod
    async def save(self, checkpoint: CheckpointData) -> bool:
        """保存检查点"""

    @abstractmethod
    async def load(self, checkpoint_id: str) -> CheckpointData | None:
        """加载检查点"""

    @abstractmethod
    async def delete(self, checkpoint_id: str) -> bool:
        """删除检查点"""

    @abstractmethod
    async def list_checkpoints(
        self, task_id: str = None, limit: int = 100
    ) -> list[CheckpointData]:
        """列出检查点"""


class FileCheckpointStorage(CheckpointStorage):
    """文件系统检查点存储"""

    def __init__(self, storage_path: str = "checkpoints"):
        self.storage_path = Path(storage_path)
        self.storage_path.mkdir(parents=True, exist_ok=True)

    async def save(self, checkpoint: CheckpointData) -> bool:
        """保存检查点到文件"""
        try:
            file_path = self.storage_path / f"{checkpoint.metadata.checkpoint_id}.json"

            # 序列化数据
            data = {
                "metadata": asdict(checkpoint.metadata),
                "state": checkpoint.state,
                "messages": checkpoint.messages,
                "tool_calls": checkpoint.tool_calls,
                "variables": checkpoint.variables,
                "context": checkpoint.context,
            }

            # 写入文件
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=str)

            # 更新大小信息
            checkpoint.metadata.size_bytes = file_path.stat().st_size

            logger.debug(f"检查点已保存到文件: {file_path}")
            return True

        except Exception as e:
            logger.error(f"保存检查点失败 {checkpoint.metadata.checkpoint_id}: {e}")
            return False

    async def load(self, checkpoint_id: str) -> CheckpointData | None:
        """从文件加载检查点"""
        try:
            file_path = self.storage_path / f"{checkpoint_id}.json"

            if not file_path.exists():
                return None

            with open(file_path, encoding="utf-8") as f:
                data = json.load(f)

            # 重建对象
            metadata = CheckpointMetadata(**data["metadata"])
            metadata.created_at = (
                datetime.fromisoformat(metadata.created_at)
                if isinstance(metadata.created_at, str)
                else metadata.created_at
            )

            checkpoint = CheckpointData(
                metadata=metadata,
                state=data["state"],
                messages=data.get("messages", []),
                tool_calls=data.get("tool_calls", []),
                variables=data.get("variables", {}),
                context=data.get("context", {}),
            )

            logger.debug(f"检查点已从文件加载: {file_path}")
            return checkpoint

        except Exception as e:
            logger.error(f"加载检查点失败 {checkpoint_id}: {e}")
            return None

    async def delete(self, checkpoint_id: str) -> bool:
        """删除检查点文件"""
        try:
            file_path = self.storage_path / f"{checkpoint_id}.json"

            if file_path.exists():
                file_path.unlink()
                logger.debug(f"检查点文件已删除: {file_path}")
                return True

            return False

        except Exception as e:
            logger.error(f"删除检查点失败 {checkpoint_id}: {e}")
            return False

    async def list_checkpoints(
        self, task_id: str = None, limit: int = 100
    ) -> list[CheckpointData]:
        """列出检查点文件"""
        try:
            checkpoints = []

            for file_path in self.storage_path.glob("*.json"):
                if len(checkpoints) >= limit:
                    break

                checkpoint = await self.load(file_path.stem)
                if checkpoint:
                    if task_id is None or checkpoint.metadata.task_id == task_id:
                        checkpoints.append(checkpoint)

            # 按创建时间排序
            checkpoints.sort(key=lambda cp: cp.metadata.created_at, reverse=True)
            return checkpoints

        except Exception as e:
            logger.error(f"列出检查点失败: {e}")
            return []


class DatabaseCheckpointStorage(CheckpointStorage):
    """数据库检查点存储"""

    def __init__(self, session):
        self.session = session

    async def save(self, checkpoint: CheckpointData) -> bool:
        """保存检查点到数据库"""
        try:
            data_json = json.dumps(
                {
                    "state": checkpoint.state,
                    "messages": checkpoint.messages,
                    "tool_calls": checkpoint.tool_calls,
                    "variables": checkpoint.variables,
                    "context": checkpoint.context,
                },
                ensure_ascii=False,
                default=str,
            )

            query = """
                INSERT INTO checkpoints (
                    checkpoint_id, task_id, created_at, description,
                    version, tags, size_bytes, compressed, data
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """

            await self.session.execute(
                query,
                (
                    checkpoint.metadata.checkpoint_id,
                    checkpoint.metadata.task_id,
                    checkpoint.metadata.created_at,
                    checkpoint.metadata.description,
                    checkpoint.metadata.version,
                    json.dumps(checkpoint.metadata.tags),
                    len(data_json),
                    checkpoint.metadata.compressed,
                    data_json,
                ),
            )

            await self.session.commit()
            logger.debug(f"检查点已保存到数据库: {checkpoint.metadata.checkpoint_id}")
            return True

        except Exception as e:
            logger.error(
                f"保存检查点到数据库失败 {checkpoint.metadata.checkpoint_id}: {e}"
            )
            return False

    async def load(self, checkpoint_id: str) -> CheckpointData | None:
        """从数据库加载检查点"""
        try:
            query = "SELECT * FROM checkpoints WHERE checkpoint_id = ?"
            result = await self.session.execute(query, (checkpoint_id,))
            row = result.fetchone()
            return self._row_to_checkpoint(row) if row else None

        except Exception as e:
            logger.error(f"从数据库加载检查点失败 {checkpoint_id}: {e}")
            return None

    async def delete(self, checkpoint_id: str) -> bool:
        """从数据库删除检查点"""
        try:
            query = "DELETE FROM checkpoints WHERE checkpoint_id = ?"
            result = await self.session.execute(query, (checkpoint_id,))
            await self.session.commit()
            logger.debug(f"检查点已从数据库删除: {checkpoint_id}")
            return result.rowcount > 0

        except Exception as e:
            logger.error(f"从数据库删除检查点失败 {checkpoint_id}: {e}")
            return False

    async def list_checkpoints(
        self, task_id: str = None, limit: int = 100
    ) -> list[CheckpointData]:
        """从数据库列出检查点"""
        try:
            if task_id:
                query = "SELECT * FROM checkpoints WHERE task_id = ? ORDER BY created_at DESC LIMIT ?"
                result = await self.session.execute(query, (task_id, limit))
            else:
                query = "SELECT * FROM checkpoints ORDER BY created_at DESC LIMIT ?"
                result = await self.session.execute(query, (limit,))

            return [
                cp for row in result.fetchall() if (cp := self._row_to_checkpoint(row))
            ]

        except Exception as e:
            logger.error(f"从数据库列出检查点失败: {e}")
            return []

    def _row_to_checkpoint(self, row) -> CheckpointData | None:
        """将数据库行转换为检查点对象"""
        try:
            metadata = CheckpointMetadata(
                checkpoint_id=row["checkpoint_id"],
                task_id=row["task_id"],
                created_at=row["created_at"],
                description=row["description"],
                version=row["version"],
                tags=json.loads(row["tags"]) if row["tags"] else [],
                size_bytes=row["size_bytes"],
                compressed=row["compressed"],
            )
            data = json.loads(row["data"])
            return CheckpointData(
                metadata=metadata,
                state=data["state"],
                messages=data.get("messages", []),
                tool_calls=data.get("tool_calls", []),
                variables=data.get("variables", {}),
                context=data.get("context", {}),
            )
        except Exception:
            return None


class CheckpointManager:
    """
    检查点管理器

    负责保存、恢复和回退执行状态。
    """

    def __init__(
        self,
        storage: CheckpointStorage = None,
        max_checkpoints_per_task: int = 10,
    ):
        """
        初始化检查点管理器

        Args:
            storage: 存储后端，默认使用文件存储
            max_checkpoints_per_task: 每个任务的最大检查点数量
        """
        self.storage = storage or FileCheckpointStorage()
        self.max_checkpoints_per_task = max_checkpoints_per_task

    async def create_checkpoint(
        self,
        task_id: str,
        state: dict[str, Any],
        description: str = "",
        tags: list[str] = None,
        compress: bool = False,
    ) -> str:
        """
        创建检查点

        Args:
            task_id: 任务ID
            state: 状态数据
            description: 描述
            tags: 标签
            compress: 是否压缩

        Returns:
            检查点ID
        """
        checkpoint_id = f"cp_{task_id}_{uuid.uuid4().hex[:8]}"

        metadata = CheckpointMetadata(
            checkpoint_id=checkpoint_id,
            task_id=task_id,
            created_at=datetime.now(),
            description=description or f"检查点 {checkpoint_id}",
            tags=tags or [],
            compressed=compress,
        )

        checkpoint = CheckpointData(
            metadata=metadata,
            state=state.copy(),
        )

        success = await self.storage.save(checkpoint)
        if success:
            # 清理旧检查点
            await self.cleanup_old_checkpoints(task_id)
            logger.info(f"检查点已创建: {checkpoint_id}")
            return checkpoint_id
        else:
            raise Exception(f"创建检查点失败: {checkpoint_id}")

    async def save_checkpoint(self, checkpoint: CheckpointData) -> bool:
        """
        保存检查点

        Args:
            checkpoint: 检查点数据

        Returns:
            保存是否成功
        """
        return await self.storage.save(checkpoint)

    async def save(
        self,
        step_id: str,
        state: dict[str, Any],
        checkpoint_type: CheckpointType = CheckpointType.AUTO,
        metadata: dict[str, Any] = None,
    ) -> str:
        """
        保存检查点（便捷方法，用于向后兼容）

        Args:
            step_id: 步骤ID
            state: 状态数据
            checkpoint_type: 检查点类型
            metadata: 额外的元数据

        Returns:
            检查点ID
        """
        task_id = metadata.get("task_id", "unknown") if metadata else "unknown"

        checkpoint_id = f"cp_{task_id}_{step_id}_{uuid.uuid4().hex[:8]}"

        cp_metadata = CheckpointMetadata(
            checkpoint_id=checkpoint_id,
            task_id=task_id,
            created_at=datetime.now(),
            description=f"{checkpoint_type.value} checkpoint for step {step_id}",
            tags=[checkpoint_type.value],
        )

        checkpoint = CheckpointData(
            metadata=cp_metadata,
            state=state.copy(),
            context=metadata or {},
        )

        success = await self.storage.save(checkpoint)
        if success:
            await self.cleanup_old_checkpoints(task_id)
            logger.debug(f"检查点已保存: {checkpoint_id}")
            return checkpoint_id
        else:
            raise Exception(f"保存检查点失败: {checkpoint_id}")

    async def load_checkpoint(self, checkpoint_id: str) -> CheckpointData | None:
        """
        加载检查点

        Args:
            checkpoint_id: 检查点ID

        Returns:
            检查点数据，如果不存在返回None
        """
        return await self.storage.load(checkpoint_id)

    async def get_checkpoint(self, checkpoint_id: str) -> CheckpointData | None:
        """
        获取检查点（load_checkpoint的别名）

        Args:
            checkpoint_id: 检查点ID

        Returns:
            检查点数据
        """
        return await self.load_checkpoint(checkpoint_id)

    async def get(self, checkpoint_id: str) -> CheckpointData | None:
        """
        获取检查点（get_checkpoint的别名，用于向后兼容）

        Args:
            checkpoint_id: 检查点ID

        Returns:
            检查点数据
        """
        return await self.get_checkpoint(checkpoint_id)

    async def restore_from_checkpoint(
        self, checkpoint_id: str
    ) -> dict[str, Any] | None:
        """
        从检查点恢复状态

        Args:
            checkpoint_id: 检查点ID

        Returns:
            恢复的状态数据
        """
        checkpoint = await self.load_checkpoint(checkpoint_id)
        if checkpoint:
            logger.info(f"从检查点恢复状态: {checkpoint_id}")
            return checkpoint.state
        return None

    async def delete_checkpoint(self, checkpoint_id: str) -> bool:
        """
        删除检查点

        Args:
            checkpoint_id: 检查点ID

        Returns:
            删除是否成功
        """
        success = await self.storage.delete(checkpoint_id)
        if success:
            logger.info(f"检查点已删除: {checkpoint_id}")
        return success

    async def list_checkpoints(
        self, task_id: str = None, limit: int = 100
    ) -> list[CheckpointData]:
        """
        列出检查点

        Args:
            task_id: 任务ID，如果为None则列出所有
            limit: 返回数量限制

        Returns:
            检查点列表
        """
        return await self.storage.list_checkpoints(task_id, limit)

    async def list_all(self, limit: int = 100) -> list[CheckpointData]:
        """
        列出所有检查点（便捷方法，用于向后兼容）

        Args:
            limit: 返回数量限制

        Returns:
            检查点列表
        """
        return await self.list_checkpoints(task_id=None, limit=limit)

    async def cleanup_old_checkpoints(
        self, task_id: str, keep_count: int = None
    ) -> int:
        """
        清理旧检查点

        Args:
            task_id: 任务ID
            keep_count: 保留数量，默认使用配置值

        Returns:
            删除的检查点数量
        """
        if keep_count is None:
            keep_count = self.max_checkpoints_per_task

        checkpoints = await self.list_checkpoints(task_id)

        if len(checkpoints) <= keep_count:
            return 0

        # 按创建时间排序，保留最新的
        checkpoints.sort(key=lambda cp: cp.metadata.created_at, reverse=True)
        to_delete = checkpoints[keep_count:]

        deleted_count = 0
        for checkpoint in to_delete:
            if await self.delete_checkpoint(checkpoint.metadata.checkpoint_id):
                deleted_count += 1

        logger.info(f"清理了 {deleted_count} 个旧检查点，任务: {task_id}")
        return deleted_count

    async def get_statistics(self) -> dict[str, Any]:
        """
        获取检查点统计信息

        Returns:
            统计信息
        """
        all_checkpoints = await self.list_checkpoints()

        # 按任务分组统计
        by_task = {}
        total_size = 0

        for checkpoint in all_checkpoints:
            task_id = checkpoint.metadata.task_id
            if task_id not in by_task:
                by_task[task_id] = 0
            by_task[task_id] += 1
            total_size += checkpoint.metadata.size_bytes

        return {
            "total_checkpoints": len(all_checkpoints),
            "checkpoints_by_task": by_task,
            "storage_usage": {
                "total_bytes": total_size,
                "total_mb": round(total_size / 1024 / 1024, 2),
            },
            "average_size_bytes": (
                total_size // len(all_checkpoints) if all_checkpoints else 0
            ),
        }
