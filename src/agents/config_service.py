"""
Agent 配置服务

从数据库读取 Agent 配置，提供统一的配置访问接口。
YAML 文件仅用于编辑和版本控制，运行时所有读取都从数据库获取。
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.types import AgentConfig as AgentConfigType
from src.agents.types import AgentLevel, AgentType
from src.db.models import AgentConfig as AgentConfigModel


class AgentConfigService:
    """Agent 配置服务 - 从数据库读取配置"""

    @staticmethod
    async def get_by_config_id(
        session: AsyncSession,
        config_id: str,
    ) -> AgentConfigType | None:
        """
        通过 config_id 获取 Agent 配置

        Args:
            session: 数据库会话
            config_id: Agent 配置 ID（如 'lingxi'）

        Returns:
            AgentConfigType 对象，不存在返回 None
        """
        result = await session.execute(
            select(AgentConfigModel).where(AgentConfigModel.config_id == config_id).limit(1)
        )
        db_config = result.scalar_one_or_none()

        if not db_config:
            return None

        return AgentConfigService._to_config_type(db_config)

    @staticmethod
    async def get_by_name(
        session: AsyncSession,
        name: str,
    ) -> AgentConfigType | None:
        """
        通过名称获取 Agent 配置

        Args:
            session: 数据库会话
            name: Agent 名称（如 '灵汐'）

        Returns:
            AgentConfigType 对象，不存在返回 None
        """
        result = await session.execute(
            select(AgentConfigModel).where(AgentConfigModel.name == name).limit(1)
        )
        db_config = result.scalar_one_or_none()

        if not db_config:
            return None

        return AgentConfigService._to_config_type(db_config)

    @staticmethod
    async def get_all(
        session: AsyncSession,
        is_active: bool | None = True,
    ) -> list[AgentConfigType]:
        """
        获取所有 Agent 配置

        Args:
            session: 数据库会话
            is_active: 是否只获取激活的配置

        Returns:
            AgentConfigType 列表
        """
        query = select(AgentConfigModel)
        if is_active is not None:
            query = query.where(AgentConfigModel.is_active == is_active)

        result = await session.execute(query)
        db_configs = result.scalars().all()

        return [AgentConfigService._to_config_type(c) for c in db_configs]

    @staticmethod
    async def get_by_type(
        session: AsyncSession,
        agent_type: str,
    ) -> list[AgentConfigType]:
        """
        按类型获取 Agent 配置

        Args:
            session: 数据库会话
            agent_type: Agent 类型（如 'main', 'atomic'）

        Returns:
            AgentConfigType 列表
        """
        result = await session.execute(
            select(AgentConfigModel).where(
                AgentConfigModel.agent_type == agent_type,
                AgentConfigModel.is_active == True,  # noqa: E712
            )
        )
        db_configs = result.scalars().all()

        return [AgentConfigService._to_config_type(c) for c in db_configs]

    @staticmethod
    def _to_config_type(db_config: AgentConfigModel) -> AgentConfigType:
        """
        将数据库模型转换为 AgentConfigType

        Args:
            db_config: 数据库模型对象

        Returns:
            AgentConfigType 对象
        """
        # 处理 agent_type 转换
        agent_type_str = db_config.agent_type or "atomic"
        try:
            agent_type = AgentType(agent_type_str.lower())
        except ValueError:
            # 处理特殊值
            type_map = {
                "main": AgentType.MAIN,
                "subagent": AgentType.SUBAGENT,
                "specialized": AgentType.SPECIALIZED,
            }
            agent_type = type_map.get(agent_type_str.lower(), AgentType.ATOMIC)

        # 处理 level 转换
        level_enum = AgentLevel.USER
        if db_config.level:
            level_map = {1: AgentLevel.L1, 2: AgentLevel.L2, 3: AgentLevel.L3}
            level_enum = level_map.get(db_config.level, AgentLevel.USER)

        return AgentConfigType(
            name=db_config.name,
            description=db_config.description or "",
            agent_type=agent_type,
            model_name=db_config.model_name,
            model_settings=None,  # 数据库暂不存储完整模型配置
            model_params=db_config.model_params or {},
            system_prompt=db_config.system_prompt or "",
            tool_ids=db_config.tool_ids or [],
            timeout_seconds=db_config.timeout_seconds,
            category=None,  # 数据库暂不存储分类
            level=level_enum,
            context_variables=None,
            prompt_structure=None,
            knowledge=None,
            memory_injection=None,
            rule_reinforcement=None,
            output_schema=db_config.output_schema,  # 添加 output_schema
            metadata={
                **(db_config.agent_metadata or {}),
                "config_id": db_config.config_id,  # Store config_id in metadata
                "hard_constraints": db_config.hard_constraints or [],
                "soft_constraints": db_config.soft_constraints or [],
            },
        )


# 便捷函数
async def get_agent_config(
    session: AsyncSession,
    identifier: str,
) -> AgentConfigType | None:
    """
    获取 Agent 配置（自动识别 config_id 或 name）

    Args:
        session: 数据库会话
        identifier: config_id 或 name

    Returns:
        AgentConfigType 对象
    """
    # 先尝试 config_id
    config = await AgentConfigService.get_by_config_id(session, identifier)
    if config:
        return config

    # 再尝试 name
    return await AgentConfigService.get_by_name(session, identifier)
