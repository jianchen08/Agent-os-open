"""
生成审计日志模块

记录所有自生成操作（工具、Agent、工作流）的历史
包括生成、评估、回滚等操作的完整审计追踪
"""

import json
import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from src.core.constants import QueryLimits


class AuditAction(str, Enum):
    """审计操作类型"""

    GENERATE = "generate"  # 生成
    EVALUATE = "evaluate"  # 评估
    APPROVE = "approve"  # 批准
    REJECT = "reject"  # 拒绝
    ROLLBACK = "rollback"  # 回滚
    DEPRECATE = "deprecate"  # 弃用
    ACTIVATE = "activate"  # 激活
    MODIFY = "modify"  # 修改
    DELETE = "delete"  # 删除


class EntityType(str, Enum):
    """实体类型"""

    TOOL = "tool"
    AGENT = "agent"
    WORKFLOW = "workflow"


class AuditStatus(str, Enum):
    """审计状态"""

    PENDING = "pending"  # 待处理
    IN_PROGRESS = "in_progress"  # 进行中
    SUCCESS = "success"  # 成功
    FAILED = "failed"  # 失败
    CANCELLED = "cancelled"  # 取消


class AuditRecord(BaseModel):
    """审计记录"""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    action: AuditAction
    entity_type: EntityType
    entity_id: str
    entity_name: str
    status: AuditStatus

    # 操作详情
    operator: str | None = None  # 操作者（Agent 或用户 ID）
    version: str | None = None  # 版本号
    description: str | None = None  # 操作描述

    # 生成相关
    generation_prompt: str | None = None  # 生成提示词
    generation_context: dict[str, Any] | None = None  # 生成上下文
    generation_result: dict[str, Any] | None = None  # 生成结果

    # 评估相关
    evaluation_score: float | None = None  # 评估分数
    evaluation_details: dict[str, Any] | None = None  # 评估详情
    evaluation_criteria: list[str] | None = None  # 评估标准

    # 回滚相关
    rollback_from: str | None = None  # 从哪个版本回滚
    rollback_to: str | None = None  # 回滚到哪个版本
    rollback_reason: str | None = None  # 回滚原因

    # 错误信息
    error_message: str | None = None  # 错误消息
    error_details: dict[str, Any] | None = None  # 错误详情

    # 性能指标
    duration_ms: float | None = None  # 操作耗时（毫秒）
    resource_usage: dict[str, Any] | None = None  # 资源使用情况

    # 元数据
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return self.model_dump()

    def to_json(self) -> str:
        """转换为 JSON"""
        return self.model_dump_json(indent=2)


class AuditLogger:
    """
    审计日志记录器

    记录所有自生成操作的审计信息
    """

    def __init__(self):
        self._records: list[AuditRecord] = []
        self._entity_index: dict[str, list[str]] = {}  # entity_id -> [record_ids]
        self._action_index: dict[AuditAction, list[str]] = {}  # action -> [record_ids]
        self._operator_index: dict[str, list[str]] = {}  # operator -> [record_ids]

    def log(
        self,
        action: AuditAction,
        entity_type: EntityType,
        entity_id: str,
        entity_name: str,
        status: AuditStatus = AuditStatus.PENDING,
        operator: str | None = None,
        version: str | None = None,
        description: str | None = None,
        **kwargs,
    ) -> AuditRecord:
        """
        记录审计日志

        Args:
            action: 操作类型
            entity_type: 实体类型
            entity_id: 实体 ID
            entity_name: 实体名称
            status: 状态
            operator: 操作者
            version: 版本号
            description: 描述
            **kwargs: 其他字段

        Returns:
            审计记录
        """
        record = AuditRecord(
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_name=entity_name,
            status=status,
            operator=operator,
            version=version,
            description=description,
            **kwargs,
        )

        self._add_record(record)
        return record

    def log_generation(
        self,
        entity_type: EntityType,
        entity_id: str,
        entity_name: str,
        prompt: str,
        context: dict[str, Any],
        result: dict[str, Any],
        operator: str | None = None,
        duration_ms: float | None = None,
    ) -> AuditRecord:
        """记录生成操作"""
        return self.log(
            action=AuditAction.GENERATE,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_name=entity_name,
            status=AuditStatus.SUCCESS,
            operator=operator,
            generation_prompt=prompt,
            generation_context=context,
            generation_result=result,
            duration_ms=duration_ms,
        )

    def log_evaluation(
        self,
        entity_type: EntityType,
        entity_id: str,
        entity_name: str,
        score: float,
        details: dict[str, Any],
        criteria: list[str],
        passed: bool,
        operator: str | None = None,
        duration_ms: float | None = None,
    ) -> AuditRecord:
        """记录评估操作"""
        return self.log(
            action=AuditAction.EVALUATE,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_name=entity_name,
            status=AuditStatus.SUCCESS if passed else AuditStatus.FAILED,
            operator=operator,
            evaluation_score=score,
            evaluation_details=details,
            evaluation_criteria=criteria,
            duration_ms=duration_ms,
        )

    def log_approval(
        self,
        entity_type: EntityType,
        entity_id: str,
        entity_name: str,
        approved: bool,
        operator: str | None = None,
        reason: str | None = None,
    ) -> AuditRecord:
        """记录批准操作"""
        action = AuditAction.APPROVE if approved else AuditAction.REJECT
        return self.log(
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_name=entity_name,
            status=AuditStatus.SUCCESS,
            operator=operator,
            description=reason,
        )

    def log_rollback(
        self,
        entity_type: EntityType,
        entity_id: str,
        entity_name: str,
        from_version: str,
        to_version: str,
        reason: str | None = None,
        operator: str | None = None,
        duration_ms: float | None = None,
    ) -> AuditRecord:
        """记录回滚操作"""
        return self.log(
            action=AuditAction.ROLLBACK,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_name=entity_name,
            status=AuditStatus.SUCCESS,
            operator=operator,
            version=to_version,
            rollback_from=from_version,
            rollback_to=to_version,
            rollback_reason=reason,
            duration_ms=duration_ms,
        )

    def log_error(
        self,
        action: AuditAction,
        entity_type: EntityType,
        entity_id: str,
        entity_name: str,
        error_message: str,
        error_details: dict[str, Any] | None = None,
        operator: str | None = None,
    ) -> AuditRecord:
        """记录错误"""
        return self.log(
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_name=entity_name,
            status=AuditStatus.FAILED,
            operator=operator,
            error_message=error_message,
            error_details=error_details,
        )

    def update_record(
        self,
        record_id: str,
        **kwargs,
    ) -> AuditRecord | None:
        """更新审计记录"""
        record = self.get_record(record_id)
        if not record:
            return None

        # 更新字段
        for key, value in kwargs.items():
            if hasattr(record, key):
                setattr(record, key, value)

        return record

    def get_record(self, record_id: str) -> AuditRecord | None:
        """获取审计记录"""
        for record in self._records:
            if record.id == record_id:
                return record
        return None

    def query_by_entity(
        self,
        entity_id: str,
        entity_type: EntityType | None = None,
        limit: int = 100,
    ) -> list[AuditRecord]:
        """按实体查询审计记录"""
        record_ids = self._entity_index.get(entity_id, [])
        records = [self.get_record(rid) for rid in record_ids]
        records = [r for r in records if r is not None]

        if entity_type:
            records = [r for r in records if r.entity_type == entity_type]

        # 按时间倒序
        records.sort(key=lambda r: r.timestamp, reverse=True)
        return records[:limit]

    def query_by_action(
        self,
        action: AuditAction,
        entity_type: EntityType | None = None,
        limit: int = 100,
    ) -> list[AuditRecord]:
        """按操作类型查询审计记录"""
        record_ids = self._action_index.get(action, [])
        records = [self.get_record(rid) for rid in record_ids]
        records = [r for r in records if r is not None]

        if entity_type:
            records = [r for r in records if r.entity_type == entity_type]

        # 按时间倒序
        records.sort(key=lambda r: r.timestamp, reverse=True)
        return records[:limit]

    def query_by_operator(
        self,
        operator: str,
        limit: int = 100,
    ) -> list[AuditRecord]:
        """按操作者查询审计记录"""
        record_ids = self._operator_index.get(operator, [])
        records = [self.get_record(rid) for rid in record_ids]
        records = [r for r in records if r is not None]

        # 按时间倒序
        records.sort(key=lambda r: r.timestamp, reverse=True)
        return records[:limit]

    def query_by_time_range(
        self,
        start_time: datetime,
        end_time: datetime,
        entity_type: EntityType | None = None,
        limit: int = 100,
    ) -> list[AuditRecord]:
        """按时间范围查询审计记录"""
        records = [r for r in self._records if start_time <= r.timestamp <= end_time]

        if entity_type:
            records = [r for r in records if r.entity_type == entity_type]

        # 按时间倒序
        records.sort(key=lambda r: r.timestamp, reverse=True)
        return records[:limit]

    def get_entity_history(
        self,
        entity_id: str,
    ) -> list[AuditRecord]:
        """获取实体的完整历史"""
        return self.query_by_entity(entity_id, limit=QueryLimits.AUDIT_QUERY_LIMIT)

    def get_statistics(
        self,
        entity_type: EntityType | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> dict[str, Any]:
        """
        获取统计信息

        Returns:
            包含统计数据的字典
        """
        records = self._records

        # 过滤
        if entity_type:
            records = [r for r in records if r.entity_type == entity_type]
        if start_time:
            records = [r for r in records if r.timestamp >= start_time]
        if end_time:
            records = [r for r in records if r.timestamp <= end_time]

        # 统计
        total = len(records)
        by_status = {}
        by_action = {}
        by_entity_type = {}
        by_operator = {}

        for record in records:
            # 按状态统计
            by_status[record.status.value] = by_status.get(record.status.value, 0) + 1

            # 按操作统计
            by_action[record.action.value] = by_action.get(record.action.value, 0) + 1

            # 按实体类型统计
            by_entity_type[record.entity_type.value] = (
                by_entity_type.get(record.entity_type.value, 0) + 1
            )

            # 按操作者统计
            if record.operator:
                by_operator[record.operator] = by_operator.get(record.operator, 0) + 1

        # 计算成功率
        success_count = by_status.get(AuditStatus.SUCCESS.value, 0)
        success_rate = success_count / total if total > 0 else 0

        # 计算平均评估分数
        evaluation_records = [r for r in records if r.evaluation_score is not None]
        avg_score = (
            sum(r.evaluation_score for r in evaluation_records)
            / len(evaluation_records)
            if evaluation_records
            else None
        )

        return {
            "total": total,
            "by_status": by_status,
            "by_action": by_action,
            "by_entity_type": by_entity_type,
            "by_operator": by_operator,
            "success_rate": success_rate,
            "average_evaluation_score": avg_score,
        }

    def export_records(
        self,
        entity_id: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> str:
        """导出审计记录为 JSON"""
        if entity_id:
            records = self.query_by_entity(
                entity_id, limit=QueryLimits.AUDIT_QUERY_LIMIT
            )
        elif start_time or end_time:
            start = start_time or datetime.min
            end = end_time or datetime.max
            records = self.query_by_time_range(
                start, end, limit=QueryLimits.AUDIT_QUERY_LIMIT
            )
        else:
            records = self._records

        return json.dumps(
            [r.to_dict() for r in records],
            ensure_ascii=False,
            indent=2,
            default=str,
        )

    def _add_to_index(self, record: AuditRecord) -> None:
        """将记录添加到索引（内部方法）"""
        self._entity_index.setdefault(record.entity_id, []).append(record.id)
        self._action_index.setdefault(record.action, []).append(record.id)
        if record.operator:
            self._operator_index.setdefault(record.operator, []).append(record.id)

    def _add_record(self, record: AuditRecord) -> None:
        """添加记录到索引"""
        self._records.append(record)
        self._add_to_index(record)

    def clear_old_records(self, before_date: datetime) -> int:
        """清理旧记录"""
        initial_count = len(self._records)
        self._records = [r for r in self._records if r.timestamp >= before_date]

        # 重建索引
        self._entity_index.clear()
        self._action_index.clear()
        self._operator_index.clear()
        for record in self._records:
            self._add_to_index(record)

        return initial_count - len(self._records)


# 全局审计日志实例
_audit_logger: AuditLogger | None = None


def get_audit_logger() -> AuditLogger:
    """获取全局审计日志实例"""
    global _audit_logger
    if _audit_logger is None:
        _audit_logger = AuditLogger()
    return _audit_logger
