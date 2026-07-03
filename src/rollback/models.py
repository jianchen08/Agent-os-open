"""
回滚机制数据模型

定义操作日志和检查点的数据结构
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class OperationType(str, Enum):
    """操作类型"""

    CREATE = "create"  # 创建
    UPDATE = "update"  # 更新
    DELETE = "delete"  # 删除
    EXECUTE = "execute"  # 执行


class OperationStatus(str, Enum):
    """操作状态"""

    EXECUTED = "executed"  # 已执行
    ROLLED_BACK = "rolled_back"  # 已回滚
    FAILED = "failed"  # 失败


@dataclass
class OperationLog:
    """操作日志"""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str = ""
    checkpoint_id: str | None = None
    tool_name: str = ""
    operation_type: OperationType = OperationType.EXECUTE
    target: str = ""  # 操作目标（文件路径/API地址等）
    params: dict[str, Any] = field(default_factory=dict)
    before_state: dict[str, Any] | None = None  # 操作前状态/快照
    after_state: dict[str, Any] | None = None  # 操作后状态
    reversible: bool = True  # 是否可逆
    reverse_action: dict[str, Any] | None = None  # 逆操作定义
    sequence: int = 0  # 操作序号
    status: OperationStatus = OperationStatus.EXECUTED
    error_message: str | None = None
    created_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "id": self.id,
            "task_id": self.task_id,
            "checkpoint_id": self.checkpoint_id,
            "tool_name": self.tool_name,
            "operation_type": self.operation_type.value,
            "target": self.target,
            "params": self.params,
            "before_state": self.before_state,
            "after_state": self.after_state,
            "reversible": self.reversible,
            "reverse_action": self.reverse_action,
            "sequence": self.sequence,
            "status": self.status.value,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OperationLog":
        """从字典创建"""
        return cls(
            id=data.get("id", str(uuid.uuid4())),
            task_id=data.get("task_id", ""),
            checkpoint_id=data.get("checkpoint_id"),
            tool_name=data.get("tool_name", ""),
            operation_type=OperationType(data.get("operation_type", "execute")),
            target=data.get("target", ""),
            params=data.get("params", {}),
            before_state=data.get("before_state"),
            after_state=data.get("after_state"),
            reversible=data.get("reversible", True),
            reverse_action=data.get("reverse_action"),
            sequence=data.get("sequence", 0),
            status=OperationStatus(data.get("status", "executed")),
            error_message=data.get("error_message"),
            created_at=(datetime.fromisoformat(data["created_at"]) if "created_at" in data else datetime.now()),
        )


@dataclass
class Checkpoint:
    """检查点"""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str = ""
    name: str | None = None
    description: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "id": self.id,
            "task_id": self.task_id,
            "name": self.name,
            "description": self.description,
            "metadata": self.metadata,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Checkpoint":
        """从字典创建"""
        return cls(
            id=data.get("id", str(uuid.uuid4())),
            task_id=data.get("task_id", ""),
            name=data.get("name"),
            description=data.get("description"),
            metadata=data.get("metadata", {}),
            created_at=(datetime.fromisoformat(data["created_at"]) if "created_at" in data else datetime.now()),
        )


@dataclass
class RollbackResult:
    """回滚结果"""

    success: bool = True
    rolled_back_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    operations: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "success": self.success,
            "rolled_back_count": self.rolled_back_count,
            "skipped_count": self.skipped_count,
            "failed_count": self.failed_count,
            "operations": self.operations,
            "warnings": self.warnings,
            "errors": self.errors,
        }
