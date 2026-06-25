"""
Agent 同步服务

实现 YAML 配置文件到数据库的同步逻辑。
核心原则：YAML 文件是配置的唯一来源，数据库用于运行时读取。
"""

import logging
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import AgentConfig, ToolLibrary
from src.services.sync.base import YamlConfigSyncService

logger = logging.getLogger(__name__)


def _get_default_config_dir() -> Path:
    """获取默认的 Agent 配置目录（基于项目根目录）"""
    # 从当前文件位置推断项目根目录
    # 当前文件: src/services/agent_sync_service.py
    # 项目根目录: 当前文件的父目录的父目录的父目录
    current_file = Path(__file__)
    project_root = current_file.parent.parent.parent
    return project_root / "config" / "agents"


class AgentSyncService(YamlConfigSyncService):
    """Agent 同步服务"""

    def _get_default_config_dir(self) -> Path:
        """获取默认的 Agent 配置目录"""
        return _get_default_config_dir()

    def _get_config_id_field(self) -> str:
        """获取配置 ID 字段名"""
        return "config_id"

    def _get_entity_class(self) -> type:
        """获取数据库实体类"""
        return AgentConfig

    def _get_entity_id_field(self) -> str:
        """获取实体 ID 字段名"""
        return "config_id"

    def _get_checksum_from_entity(self, entity: Any) -> str | None:
        """从实体中获取校验和"""
        if entity.agent_metadata:
            return entity.agent_metadata.get("checksum")
        return None

    def _get_log_prefix(self) -> str:
        """获取日志前缀"""
        return "Agent"

    def _prepare_entity_data(self, data: dict, checksum: str) -> dict:
        """
        准备 Agent 数据（转换为数据库格式）

        Args:
            data: YAML 数据
            checksum: 校验和

        Returns:
            数据库字段字典
        """
        # 准备 metadata（包含 checksum）
        metadata = data.get("metadata", {})
        metadata["checksum"] = checksum

        # 处理 level 字段：YAML 中是字符串 "L1"/"L2"/"L3"，数据库中是 int 1/2/3
        level_value = 1  # 默认 L1
        level_str = data.get("level", "")
        if level_str:
            level_map = {"L1": 1, "L2": 2, "L3": 3}
            level_value = level_map.get(level_str, 1)

        return {
            "config_id": data["config_id"],
            "name": data["name"],
            "description": data.get("description", ""),
            "agent_type": data.get("agent_type", "atomic"),
            "model_name": data["model_name"],
            "model_params": data.get("model_params", {}),
            "system_prompt": data.get("system_prompt", ""),
            "tool_ids": data.get("tool_ids", []),
            "hard_constraints": data.get("hard_constraints", []),
            "soft_constraints": data.get("soft_constraints", []),
            "context_variables": data.get("context_variables", {}),
            "dynamic_vars": {"enabled": True, "vars": data.get("dynamic_variables", [])},
            "input_schema": data.get("input_schema", {}),
            "output_schema": data.get("output_schema", {}),
            "version": data.get("version", "1.0.0"),
            "is_active": data.get("is_active", True),
            "max_iterations": data.get("max_iterations", 10),
            "timeout_seconds": data.get("timeout_seconds", 300),
            "tags": data.get("tags", []),
            "agent_metadata": metadata,
            "status": data.get("status", "active"),
            "level": level_value,
        }

    async def validate_agents(
        self,
        session: AsyncSession,
    ) -> dict[str, list[str]]:
        """
        验证 Agent 配置完整性

        Args:
            session: 数据库会话

        Returns:
            验证结果 {errors, warnings}
        """
        errors = []
        warnings = []

        # 查询所有 Agent
        result = await session.execute(select(AgentConfig))
        agents = result.scalars().all()

        # 查询所有工具（用于验证 tool_ids）
        tool_result = await session.execute(select(ToolLibrary.name))
        available_tools = {name for (name,) in tool_result.all()}

        for agent in agents:
            # 检查必需字段
            if not agent.config_id:
                errors.append(f"Agent 缺少 config_id: ID={agent.id}")
            if not agent.model_name:
                errors.append(f"Agent 缺少 model_name: {agent.config_id}")

            # 验证 tool_ids
            if agent.tool_ids:
                for tool_id in agent.tool_ids:
                    if tool_id not in available_tools:
                        warnings.append(
                            f"Agent {agent.config_id} 引用了不存在的工具: {tool_id}"
                        )

        return {"errors": errors, "warnings": warnings}
