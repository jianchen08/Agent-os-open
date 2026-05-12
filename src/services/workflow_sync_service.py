"""
工作流同步服务

实现 YAML 配置文件到数据库的同步逻辑。
核心原则：YAML 文件是配置的唯一来源，数据库用于运行时读取。
"""

import logging
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import AgentConfig, ToolLibrary, Workflow
from src.services.sync.base import YamlConfigSyncService

logger = logging.getLogger(__name__)


def _get_default_config_dir() -> Path:
    """获取默认的工作流配置目录（基于项目根目录）"""
    current_file = Path(__file__)
    project_root = current_file.parent.parent.parent
    return project_root / "config" / "workflows"


class WorkflowSyncService(YamlConfigSyncService):
    """工作流同步服务"""

    def _get_default_config_dir(self) -> Path:
        """获取默认的工作流配置目录"""
        return _get_default_config_dir()

    def _get_config_id_field(self) -> str:
        """获取配置 ID 字段名"""
        return "id"

    def _get_entity_class(self) -> type:
        """获取数据库实体类"""
        return Workflow

    def _get_entity_id_field(self) -> str:
        """获取实体 ID 字段名"""
        return "source_id"

    def _get_checksum_from_entity(self, entity: Any) -> str | None:
        """从实体中获取校验和"""
        definition: dict[str, Any] = entity.definition
        return definition.get("metadata", {}).get("checksum")

    def _get_log_prefix(self) -> str:
        """获取日志前缀"""
        return "工作流"

    def _prepare_entity_data(self, data: dict, checksum: str) -> dict:
        """
        准备工作流数据（转换为数据库格式）

        Args:
            data: YAML 数据
            checksum: 校验和

        Returns:
            数据库字段字典
        """
        # 在 definition 的 metadata 中添加 checksum
        definition = data.copy()
        if "metadata" not in definition:
            definition["metadata"] = {}
        definition["metadata"]["checksum"] = checksum

        # 提取元数据
        metadata = data.get("metadata", {})

        return {
            "name": metadata.get("name", data["id"]),
            "description": metadata.get("description", ""),
            "type": metadata.get("type", "user_defined"),
            "source": "native",
            "source_id": data["id"],
            "definition": definition,
            "inputs_schema": data.get("inputs", {}),
            "outputs_schema": data.get("outputs", {}),
            "status": data.get("status", "active"),
            "tags": metadata.get("tags", []),
        }

    async def validate_workflows(
        self,
        session: AsyncSession,
    ) -> dict[str, list[str]]:
        """
        验证工作流配置完整性

        Args:
            session: 数据库会话

        Returns:
            验证结果 {errors, warnings}
        """
        errors = []
        warnings = []

        # 查询所有工作流
        result = await session.execute(select(Workflow))
        workflows = result.scalars().all()

        # 查询可用的工具和 Agent
        tool_result = await session.execute(select(ToolLibrary.name))
        available_tools = {name for (name,) in tool_result.all()}

        agent_result = await session.execute(select(AgentConfig.config_id))
        available_agents = {config_id for (config_id,) in agent_result.all()}

        for workflow in workflows:
            # 检查必需字段
            if not workflow.source_id:
                errors.append(f"工作流缺少 source_id: ID={workflow.id}")

            # 解析 definition
            definition: dict[str, Any] = workflow.definition

            # 验证节点引用
            nodes = definition.get("nodes", [])
            for node in nodes:
                node_config = node.get("config", {})

                # 检查工具引用
                tool_name = node_config.get("tool_name")
                if tool_name and tool_name not in available_tools:
                    warnings.append(
                        f"工作流 {workflow.name} 引用了不存在的工具: {tool_name}"
                    )

                # 检查 Agent 引用
                agent_id = node_config.get("agent_id")
                if agent_id and agent_id not in available_agents:
                    warnings.append(
                        f"工作流 {workflow.name} 引用了不存在的 Agent: {agent_id}"
                    )

        return {"errors": errors, "warnings": warnings}
