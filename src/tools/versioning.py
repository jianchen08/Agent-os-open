"""
工具版本管理模块

提供工具的版本控制、回滚、历史查询等功能
"""

import copy
from typing import Any

from src.core.audit import AuditAction, AuditLogger, AuditStatus, EntityType
from src.core.versioning import VersionManager, VersionStatus, VersionType
from src.tools.types import Tool


class ToolVersionManager:
    """
    工具版本管理器

    基于统一版本管理，提供工具特定的版本控制接口
    """

    def __init__(
        self,
        version_manager: VersionManager | None = None,
        audit_logger: AuditLogger | None = None,
    ):
        """
        初始化工具版本管理器

        Args:
            version_manager: 统一版本管理器实例
            audit_logger: 审计日志记录器实例
        """
        self.version_manager = version_manager or VersionManager()
        self.audit_logger = audit_logger

    def register_tool(
        self,
        tool_id: str,
        tool_name: str,
    ) -> None:
        """
        注册新工具

        Args:
            tool_id: 工具 ID
            tool_name: 工具名称
        """
        self.version_manager.register_entity(
            entity_id=tool_id,
            entity_name=tool_name,
            version_type=VersionType.TOOL,
        )

    def create_version(
        self,
        tool: Tool,
        created_by: str | None = None,
        description: str | None = None,
        changelog: str | None = None,
        tags: list[str] | None = None,
        metrics: dict[str, Any] | None = None,
        status: VersionStatus = VersionStatus.ACTIVE,
    ) -> str:
        """
        创建工具新版本

        Args:
            tool: 工具定义
            created_by: 创建者
            description: 版本描述
            changelog: 变更日志
            tags: 标签
            metrics: 性能指标
            status: 版本状态

        Returns:
            版本号
        """
        # 注册工具（如果尚未注册）
        if tool.name not in self.version_manager._histories:
            self.register_tool(tool.name, tool.name)

        # 创建版本
        metadata = self.version_manager.create_version(
            entity_id=tool.name,
            data=copy.deepcopy(tool),
            version_number=tool.version,
            created_by=created_by,
            description=description,
            changelog=changelog,
            tags=tags or [],
            metrics=metrics or {},
            status=status,
        )

        # 记录审计日志
        if self.audit_logger:
            self.audit_logger.log_generation(
                entity_type=EntityType.TOOL,
                entity_id=tool.name,
                entity_name=tool.name,
                prompt=description or "创建工具版本",
                context={"version": tool.version},
                result={"version": tool.version, "metadata": metadata.dict()},
                operator=created_by,
            )

        return tool.version

    def get_version(self, tool_name: str, version: str) -> Tool | None:
        """
        获取指定版本的工具

        Args:
            tool_name: 工具名称
            version: 版本号

        Returns:
            工具定义，如果不存在则返回 None
        """
        return self.version_manager.get_version(tool_name, version)

    def get_current_version(self, tool_name: str) -> Tool | None:
        """
        获取工具的当前版本

        Args:
            tool_name: 工具名称

        Returns:
            当前版本的工具定义
        """
        return self.version_manager.get_current_version(tool_name)

    def list_versions(self, tool_name: str) -> list[dict[str, Any]]:
        """
        列出工具的所有版本

        Args:
            tool_name: 工具名称

        Returns:
            版本信息列表
        """
        versions = self.version_manager.list_versions(tool_name)
        return [v.dict() for v in versions]

    def rollback(
        self,
        tool_name: str,
        target_version: str,
        rollback_by: str | None = None,
        reason: str | None = None,
    ) -> str:
        """
        回滚工具到指定版本

        Args:
            tool_name: 工具名称
            target_version: 目标版本号
            rollback_by: 回滚操作者
            reason: 回滚原因

        Returns:
            新版本号
        """
        # 记录审计日志
        if self.audit_logger:
            self.audit_logger.log_rollback(
                entity_type=EntityType.TOOL,
                entity_id=tool_name,
                entity_name=tool_name,
                from_version=self.version_manager._histories[tool_name].current_version,
                to_version=target_version,
                reason=reason,
                operator=rollback_by,
            )

        # 执行回滚
        metadata = self.version_manager.rollback(
            entity_id=tool_name,
            target_version=target_version,
            rollback_by=rollback_by,
        )

        return metadata.version_number

    def deprecate_version(
        self,
        tool_name: str,
        version: str,
        operator: str | None = None,
    ) -> None:
        """
        弃用工具的指定版本

        Args:
            tool_name: 工具名称
            version: 版本号
            operator: 操作者
        """
        self.version_manager.deprecate_version(tool_name, version)

        # 记录审计日志
        if self.audit_logger:
            self.audit_logger.log(
                action=AuditAction.DEPRECATE,
                entity_type=EntityType.TOOL,
                entity_id=tool_name,
                entity_name=tool_name,
                status=AuditStatus.SUCCESS,
                operator=operator,
                version=version,
            )

    def activate_version(
        self,
        tool_name: str,
        version: str,
        operator: str | None = None,
    ) -> None:
        """
        激活工具的指定版本

        Args:
            tool_name: 工具名称
            version: 版本号
            operator: 操作者
        """
        self.version_manager.activate_version(tool_name, version)

        # 记录审计日志
        if self.audit_logger:
            self.audit_logger.log(
                action=AuditAction.ACTIVATE,
                entity_type=EntityType.TOOL,
                entity_id=tool_name,
                entity_name=tool_name,
                status=AuditStatus.SUCCESS,
                operator=operator,
                version=version,
            )

    def update_metrics(
        self,
        tool_name: str,
        version: str,
        metrics: dict[str, Any],
    ) -> None:
        """
        更新工具版本的性能指标

        Args:
            tool_name: 工具名称
            version: 版本号
            metrics: 性能指标，如 {"success_rate": 0.95, "avg_latency_ms": 100}
        """
        self.version_manager.update_metrics(tool_name, version, metrics)

    def get_history(self, tool_name: str) -> dict[str, Any]:
        """
        获取工具的完整版本历史

        Args:
            tool_name: 工具名称

        Returns:
            版本历史
        """
        history = self.version_manager.get_history(tool_name)
        if not history:
            return {}

        return {
            "tool_name": history.entity_name,
            "current_version": history.current_version,
            "versions": [v.dict() for v in history.versions],
        }

    def compare_versions(
        self,
        tool_name: str,
        version1: str,
        version2: str,
    ) -> dict[str, Any]:
        """
        比较工具的两个版本

        Args:
            tool_name: 工具名称
            version1: 版本号 1
            version2: 版本号 2

        Returns:
            差异信息
        """
        return self.version_manager.compare_versions(tool_name, version1, version2)

    def export_history(self, tool_name: str) -> str:
        """
        导出工具版本历史为 JSON

        Args:
            tool_name: 工具名称

        Returns:
            JSON 字符串
        """
        return self.version_manager.export_history(tool_name)

    def import_history(
        self,
        tool_name: str,
        json_data: str,
    ) -> None:
        """
        从 JSON 导入工具版本历史

        Args:
            tool_name: 工具名称
            json_data: JSON 数据
        """
        self.version_manager.import_history(tool_name, json_data)

    def cleanup_old_versions(
        self,
        tool_name: str,
        keep_count: int = 5,
        keep_active: bool = True,
        operator: str | None = None,
    ) -> list[str]:
        """
        清理工具的旧版本

        Args:
            tool_name: 工具名称
            keep_count: 保留版本数量
            keep_active: 是否保留活跃版本
            operator: 操作者

        Returns:
            被删除的版本号列表
        """
        deleted = self.version_manager.cleanup_old_versions(
            tool_name,
            keep_count=keep_count,
            keep_active=keep_active,
        )

        # 记录审计日志
        if self.audit_logger and deleted:
            self.audit_logger.log(
                action=AuditAction.DELETE,
                entity_type=EntityType.TOOL,
                entity_id=tool_name,
                entity_name=tool_name,
                status=AuditStatus.SUCCESS,
                operator=operator,
                description=f"清理 {len(deleted)} 个旧版本: {', '.join(deleted)}",
            )

        return deleted

    def get_tool_statistics(
        self,
        tool_name: str,
    ) -> dict[str, Any]:
        """
        获取工具的统计信息

        Args:
            tool_name: 工具名称

        Returns:
            统计信息
        """
        history = self.version_manager.get_history(tool_name)
        if not history:
            return {}

        versions = history.versions

        # 统计版本状态
        status_count = {}
        for v in versions:
            status_count[v.status.value] = status_count.get(v.status.value, 0) + 1

        # 获取性能指标
        metrics_history = [v.metrics for v in versions if v.metrics]
        avg_success_rate = None
        if metrics_history:
            success_rates = [
                m.get("success_rate", 0) for m in metrics_history if "success_rate" in m
            ]
            if success_rates:
                avg_success_rate = sum(success_rates) / len(success_rates)

        return {
            "total_versions": len(versions),
            "current_version": history.current_version,
            "status_distribution": status_count,
            "average_success_rate": avg_success_rate,
            "latest_version": versions[0].version_number if versions else None,
        }

    def evaluate_version(
        self,
        tool_name: str,
        version: str,
        evaluation_result: dict[str, Any],
        operator: str | None = None,
    ) -> None:
        """
        评估工具版本

        Args:
            tool_name: 工具名称
            version: 版本号
            evaluation_result: 评估结果
            operator: 评估者
        """
        # 更新指标
        metrics = evaluation_result.get("metrics", {})
        if metrics:
            self.update_metrics(tool_name, version, metrics)

        # 记录审计日志
        if self.audit_logger:
            self.audit_logger.log_evaluation(
                entity_type=EntityType.TOOL,
                entity_id=tool_name,
                entity_name=tool_name,
                score=evaluation_result.get("score", 0),
                details=evaluation_result.get("details", {}),
                criteria=evaluation_result.get("criteria", []),
                passed=evaluation_result.get("passed", False),
                operator=operator,
            )
