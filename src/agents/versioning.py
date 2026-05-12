"""
Agent 版本管理模块

提供 Agent 配置的版本控制、回滚、历史查询等功能
"""

import copy
from pathlib import Path
from typing import Any

import yaml

from src.agents.types import AgentConfig
from src.core.audit import AuditAction, AuditLogger, AuditStatus, EntityType
from src.core.versioning import VersionManager, VersionStatus, VersionType


class AgentVersionManager:
    """
    Agent 版本管理器

    基于 AgentConfig 的版本控制，支持从 YAML 文件加载和保存
    """

    def __init__(
        self,
        version_manager: VersionManager | None = None,
        audit_logger: AuditLogger | None = None,
        config_dir: str | None = None,
    ):
        """
        初始化 Agent 版本管理器

        Args:
            version_manager: 统一版本管理器实例
            audit_logger: 审计日志记录器实例
            config_dir: Agent 配置文件目录
        """
        self.version_manager = version_manager or VersionManager()
        self.audit_logger = audit_logger
        self.config_dir = Path(config_dir) if config_dir else None

    def register_agent(
        self,
        agent_id: str,
        agent_name: str,
    ) -> None:
        """
        注册新 Agent

        Args:
            agent_id: Agent ID
            agent_name: Agent 名称
        """
        self.version_manager.register_entity(
            entity_id=agent_id,
            entity_name=agent_name,
            version_type=VersionType.AGENT,
        )

    def create_version(
        self,
        config: AgentConfig,
        created_by: str | None = None,
        description: str | None = None,
        changelog: str | None = None,
        tags: list[str] | None = None,
        metrics: dict[str, Any] | None = None,
        status: VersionStatus = VersionStatus.ACTIVE,
    ) -> str:
        """
        创建 Agent 新版本

        Args:
            config: Agent 配置
            created_by: 创建者
            description: 版本描述
            changelog: 变更日志
            tags: 标签
            metrics: 性能指标
            status: 版本状态

        Returns:
            版本号（使用 metadata 中的 version，如果没有则生成默认版本号）
        """
        # 注册 Agent（如果尚未注册）
        if config.name not in self.version_manager._histories:
            self.register_agent(config.name, config.name)

        # 从 metadata 中获取版本号，或生成默认版本号
        version = config.metadata.get("version", "1.0.0")

        # 创建版本
        metadata = self.version_manager.create_version(
            entity_id=config.name,
            data=copy.deepcopy(config),
            version_number=version,
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
                entity_type=EntityType.AGENT,
                entity_id=config.name,
                entity_name=config.name,
                prompt=description or "创建 Agent 版本",
                context={"version": version, "model": config.model},
                result={"version": version, "metadata": metadata.dict()},
                operator=created_by,
            )

        return version

    def create_version_from_yaml(
        self,
        yaml_path: str,
        created_by: str | None = None,
        description: str | None = None,
    ) -> str:
        """
        从 YAML 文件创建 Agent 版本

        Args:
            yaml_path: YAML 文件路径
            created_by: 创建者
            description: 版本描述

        Returns:
            版本号
        """
        # 读取 YAML 文件
        with open(yaml_path, encoding="utf-8") as f:
            yaml_data = yaml.safe_load(f)

        # 创建 AgentConfig
        config = AgentConfig(**yaml_data)

        # 如果 YAML 中有版本号，添加到 metadata
        if "version" in yaml_data:
            config.metadata["version"] = yaml_data["version"]

        return self.create_version(
            config=config,
            created_by=created_by,
            description=description or f"从 {yaml_path} 创建",
        )

    def get_version(self, agent_name: str, version: str) -> AgentConfig | None:
        """
        获取指定版本的 Agent 配置

        Args:
            agent_name: Agent 名称
            version: 版本号

        Returns:
            Agent 配置，如果不存在则返回 None
        """
        return self.version_manager.get_version(agent_name, version)

    def get_current_version(self, agent_name: str) -> AgentConfig | None:
        """
        获取 Agent 的当前版本

        Args:
            agent_name: Agent 名称

        Returns:
            当前版本的 Agent 配置
        """
        return self.version_manager.get_current_version(agent_name)

    def list_versions(self, agent_name: str) -> list[dict[str, Any]]:
        """
        列出 Agent 的所有版本

        Args:
            agent_name: Agent 名称

        Returns:
            版本信息列表
        """
        versions = self.version_manager.list_versions(agent_name)
        return [v.dict() for v in versions]

    def rollback(
        self,
        agent_name: str,
        target_version: str,
        rollback_by: str | None = None,
        reason: str | None = None,
    ) -> str:
        """
        回滚 Agent 到指定版本

        Args:
            agent_name: Agent 名称
            target_version: 目标版本号
            rollback_by: 回滚操作者
            reason: 回滚原因

        Returns:
            新版本号
        """
        # 记录审计日志
        if self.audit_logger:
            self.audit_logger.log_rollback(
                entity_type=EntityType.AGENT,
                entity_id=agent_name,
                entity_name=agent_name,
                from_version=self.version_manager._histories[
                    agent_name
                ].current_version,
                to_version=target_version,
                reason=reason,
                operator=rollback_by,
            )

        # 执行回滚
        metadata = self.version_manager.rollback(
            entity_id=agent_name,
            target_version=target_version,
            rollback_by=rollback_by,
        )

        return metadata.version_number

    def deprecate_version(
        self,
        agent_name: str,
        version: str,
        operator: str | None = None,
    ) -> None:
        """
        弃用 Agent 的指定版本

        Args:
            agent_name: Agent 名称
            version: 版本号
            operator: 操作者
        """
        self.version_manager.deprecate_version(agent_name, version)

        # 记录审计日志
        if self.audit_logger:
            self.audit_logger.log(
                action=AuditAction.DEPRECATE,
                entity_type=EntityType.AGENT,
                entity_id=agent_name,
                entity_name=agent_name,
                status=AuditStatus.SUCCESS,
                operator=operator,
                version=version,
            )

    def activate_version(
        self,
        agent_name: str,
        version: str,
        operator: str | None = None,
    ) -> None:
        """
        激活 Agent 的指定版本

        Args:
            agent_name: Agent 名称
            version: 版本号
            operator: 操作者
        """
        self.version_manager.activate_version(agent_name, version)

        # 记录审计日志
        if self.audit_logger:
            self.audit_logger.log(
                action=AuditAction.ACTIVATE,
                entity_type=EntityType.AGENT,
                entity_id=agent_name,
                entity_name=agent_name,
                status=AuditStatus.SUCCESS,
                operator=operator,
                version=version,
            )

    def update_metrics(
        self,
        agent_name: str,
        version: str,
        metrics: dict[str, Any],
    ) -> None:
        """
        更新 Agent 版本的性能指标

        Args:
            agent_name: Agent 名称
            version: 版本号
            metrics: 性能指标，如 {"success_rate": 0.95, "avg_iterations": 10}
        """
        self.version_manager.update_metrics(agent_name, version, metrics)

    def get_history(self, agent_name: str) -> dict[str, Any]:
        """
        获取 Agent 的完整版本历史

        Args:
            agent_name: Agent 名称

        Returns:
            版本历史
        """
        history = self.version_manager.get_history(agent_name)
        if not history:
            return {}

        return {
            "agent_name": history.entity_name,
            "current_version": history.current_version,
            "versions": [v.dict() for v in history.versions],
        }

    def compare_versions(
        self,
        agent_name: str,
        version1: str,
        version2: str,
    ) -> dict[str, Any]:
        """
        比较 Agent 的两个版本

        Args:
            agent_name: Agent 名称
            version1: 版本号 1
            version2: 版本号 2

        Returns:
            差异信息
        """
        return self.version_manager.compare_versions(agent_name, version1, version2)

    def export_version_to_yaml(
        self,
        agent_name: str,
        version: str,
        output_path: str | None = None,
    ) -> str:
        """
        导出 Agent 版本到 YAML 文件

        Args:
            agent_name: Agent 名称
            version: 版本号
            output_path: 输出文件路径（如果不指定则使用 config_dir）

        Returns:
            文件路径
        """
        config = self.get_version(agent_name, version)
        if not config:
            raise ValueError(f"版本 {version} 不存在")

        # 确定输出路径
        if output_path:
            path = Path(output_path)
        elif self.config_dir:
            path = self.config_dir / f"{agent_name}_{version}.yaml"
        else:
            path = Path(f"{agent_name}_{version}.yaml")

        # 添加版本号到 metadata
        config_dict = config.dict()
        if "version" not in config_dict["metadata"]:
            config_dict["metadata"]["version"] = version

        # 写入 YAML
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(config_dict, f, allow_unicode=True, default_flow_style=False)

        return str(path)

    def export_history(self, agent_name: str) -> str:
        """
        导出 Agent 版本历史为 JSON

        Args:
            agent_name: Agent 名称

        Returns:
            JSON 字符串
        """
        return self.version_manager.export_history(agent_name)

    def import_history(
        self,
        agent_name: str,
        json_data: str,
    ) -> None:
        """
        从 JSON 导入 Agent 版本历史

        Args:
            agent_name: Agent 名称
            json_data: JSON 数据
        """
        self.version_manager.import_history(agent_name, json_data)

    def cleanup_old_versions(
        self,
        agent_name: str,
        keep_count: int = 5,
        keep_active: bool = True,
        operator: str | None = None,
    ) -> list[str]:
        """
        清理 Agent 的旧版本

        Args:
            agent_name: Agent 名称
            keep_count: 保留版本数量
            keep_active: 是否保留活跃版本
            operator: 操作者

        Returns:
            被删除的版本号列表
        """
        deleted = self.version_manager.cleanup_old_versions(
            agent_name,
            keep_count=keep_count,
            keep_active=keep_active,
        )

        # 记录审计日志
        if self.audit_logger and deleted:
            self.audit_logger.log(
                action=AuditAction.DELETE,
                entity_type=EntityType.AGENT,
                entity_id=agent_name,
                entity_name=agent_name,
                status=AuditStatus.SUCCESS,
                operator=operator,
                description=f"清理 {len(deleted)} 个旧版本: {', '.join(deleted)}",
            )

        return deleted

    def get_agent_statistics(
        self,
        agent_name: str,
    ) -> dict[str, Any]:
        """
        获取 Agent 的统计信息

        Args:
            agent_name: Agent 名称

        Returns:
            统计信息
        """
        history = self.version_manager.get_history(agent_name)
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
        agent_name: str,
        version: str,
        evaluation_result: dict[str, Any],
        operator: str | None = None,
    ) -> None:
        """
        评估 Agent 版本

        Args:
            agent_name: Agent 名称
            version: 版本号
            evaluation_result: 评估结果
            operator: 评估者
        """
        # 更新指标
        metrics = evaluation_result.get("metrics", {})
        if metrics:
            self.update_metrics(agent_name, version, metrics)

        # 记录审计日志
        if self.audit_logger:
            self.audit_logger.log_evaluation(
                entity_type=EntityType.AGENT,
                entity_id=agent_name,
                entity_name=agent_name,
                score=evaluation_result.get("score", 0),
                details=evaluation_result.get("details", {}),
                criteria=evaluation_result.get("criteria", []),
                passed=evaluation_result.get("passed", False),
                operator=operator,
            )

    def load_current_to_yaml(
        self,
        agent_name: str,
        output_path: str | None = None,
    ) -> str | None:
        """
        加载当前版本的 Agent 并保存到 YAML 文件

        Args:
            agent_name: Agent 名称
            output_path: 输出文件路径（如果不指定则覆盖原配置文件）

        Returns:
            文件路径
        """
        config = self.get_current_version(agent_name)
        if not config:
            return None

        # 确定输出路径
        if output_path:
            path = Path(output_path)
        elif self.config_dir:
            path = self.config_dir / f"{agent_name}.yaml"
        else:
            path = Path(f"{agent_name}.yaml")

        # 写入 YAML
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(config.dict(), f, allow_unicode=True, default_flow_style=False)

        return str(path)
