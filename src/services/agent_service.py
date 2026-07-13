"""
Agent 服务层

提供 Agent 配置相关的业务逻辑
基于新的 AgentConfig 模型
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.constants import Timeout
from src.core.exceptions import NotFoundException, ValidationException
from src.db.models import AgentConfig


def _get_default_chat_model() -> str:
    """获取默认对话模型别名（从 llm.yaml 读取）。"""
    from src.config.llm_config import get_llm_config  # noqa: PLC0415

    return get_llm_config().get_default_alias("chat")


class AgentService:
    """Agent 服务类"""

    def __init__(self, session: AsyncSession):
        """
        初始化 Agent 服务

        Args:
            session: 数据库会话
        """
        self.session = session

    async def list_agents(
        self,
        user_id: str,
        page: int = 1,
        page_size: int = 20,
        status: str | None = None,
        agent_type: str | None = None,
        search: str | None = None,
    ) -> dict[str, Any]:
        """
        获取 Agent 配置列表

        Args:
            user_id: 用户 ID
            page: 页码
            page_size: 每页数量
            status: 状态过滤（active/inactive）
            agent_type: 类型过滤
            search: 搜索关键词

        Returns:
            包含 items, total, page, page_size 的字典
        """
        # 构建查询
        query = select(AgentConfig)

        # 添加过滤条件
        if status:
            is_active = status == "active"
            query = query.where(AgentConfig.is_active == is_active)

        if agent_type:
            query = query.where(AgentConfig.agent_type == agent_type)

        if search:
            query = query.where(AgentConfig.name.ilike(f"%{search}%"))

        # 计算总数
        count_query = select(func.count()).select_from(query.subquery())
        total_result = await self.session.execute(count_query)
        total = total_result.scalar()

        # 分页
        query = query.offset((page - 1) * page_size).limit(page_size)
        query = query.order_by(AgentConfig.created_at.desc())

        # 执行查询
        result = await self.session.execute(query)
        agents = result.scalars().all()

        # 转换为字典列表
        items = [self._agent_to_response_dict(agent) for agent in agents]

        return {"items": items, "total": total, "page": page, "page_size": page_size}

    async def get_agent(self, agent_id: str) -> dict[str, Any]:
        """
        获取 Agent 配置详情

        Args:
            agent_id: Agent ID

        Returns:
            Agent 详情字典

        Raises:
            NotFoundException: Agent 不存在
        """
        query = select(AgentConfig).where(AgentConfig.id == agent_id)
        result = await self.session.execute(query)
        agent = result.scalar_one_or_none()

        if not agent:
            raise NotFoundException(
                message=f"Agent 不存在: {agent_id}",
                resource_type="Agent",
                resource_id=agent_id,
                code="AGENT_001",
            )

        return self._agent_to_response_dict(agent)

    async def get_agent_by_config_id(self, config_id: str) -> dict[str, Any] | None:
        """
        通过 config_id 获取 Agent 配置

        Args:
            config_id: 配置 ID

        Returns:
            Agent 详情字典，不存在返回 None
        """
        query = select(AgentConfig).where(AgentConfig.config_id == config_id)
        result = await self.session.execute(query)
        agent = result.scalar_one_or_none()

        if not agent:
            return None

        return self._agent_to_response_dict(agent)

    def _agent_to_response_dict(self, agent: AgentConfig) -> dict[str, Any]:
        """
        将 AgentConfig 模型转换为响应字典

        Args:
            agent: AgentConfig 模型实例

        Returns:
            符合 AgentResponse schema 的字典
        """
        return {
            "id": agent.id,  # 保持 UUID 类型
            "config_id": agent.config_id,
            "name": agent.name,
            "description": agent.description,
            "model": agent.model_name or "glm-5.2",
            "system_prompt": agent.system_prompt or "",
            "tool_names": agent.tool_ids or [],
            "max_iterations": agent.max_iterations or 10,
            "timeout": agent.timeout_seconds or Timeout.DEFAULT_AGENT_TIMEOUT,
            "agent_type": agent.agent_type or "atomic",
            "status": agent.status or ("active" if agent.is_active else "inactive"),
            "tags": agent.tags or [],
            "metadata": agent.agent_metadata or {},
            "created_at": agent.created_at,  # 保持 datetime 类型
            "updated_at": agent.updated_at,  # 保持 datetime 类型
        }

    async def create_agent(
        self,
        user_id: str,
        name: str,
        description: str | None = None,
        agent_type: str = "atomic",
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        创建 Agent 配置

        Args:
            user_id: 用户 ID
            name: Agent 名称
            description: Agent 描述
            agent_type: Agent 类型
            config: Agent 配置

        Returns:
            创建的 Agent 详情
        """
        config = config or {}

        # 生成 config_id
        import uuid  # noqa: PLC0415

        config_id = f"agent-{uuid.uuid4().hex[:8]}"

        agent = AgentConfig(
            config_id=config_id,
            name=name,
            description=description,
            agent_type=agent_type,
            model_name=config.get("model") or _get_default_chat_model(),
            system_prompt=config.get("system_prompt", "你是一个有用的 AI 助手。"),
            tool_ids=config.get("tool_names", []),
            max_iterations=config.get("max_iterations", 10),
            timeout_seconds=config.get("timeout", Timeout.DEFAULT_AGENT_TIMEOUT),
            tags=config.get("tags", []),
            agent_metadata=config.get("metadata", {}),
            status="active",
            is_active=True,
        )

        self.session.add(agent)
        await self.session.flush()
        await self.session.refresh(agent)

        return self._agent_to_response_dict(agent)

    async def update_agent(  # noqa: PLR0912
        self,
        agent_id: str,
        name: str | None = None,
        description: str | None = None,
        config: dict[str, Any] | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        """
        更新 Agent 配置

        Args:
            agent_id: Agent ID
            name: 新名称
            description: 新描述
            config: 新配置
            status: 新状态（active/inactive）

        Returns:
            更新后的 Agent 详情

        Raises:
            NotFoundException: Agent 不存在
            ValidationException: 状态值无效
        """
        query = select(AgentConfig).where(AgentConfig.id == agent_id)
        result = await self.session.execute(query)
        agent = result.scalar_one_or_none()

        if not agent:
            raise NotFoundException(
                message=f"Agent 不存在: {agent_id}",
                resource_type="Agent",
                resource_id=agent_id,
                code="AGENT_001",
            )

        if status is not None and status not in ["active", "inactive"]:
            raise ValidationException(
                message=f"无效的状态值: {status}",
                field="status",
                code="AGENT_005",
            )

        if name is not None:
            agent.name = name
        if description is not None:
            agent.description = description
        if status is not None:
            agent.status = status
            agent.is_active = status == "active"

        if config is not None:
            if "model" in config:
                agent.model_name = config["model"]
            if "system_prompt" in config:
                agent.system_prompt = config["system_prompt"]
            if "tool_names" in config:
                agent.tool_ids = config["tool_names"]
            if "max_iterations" in config:
                agent.max_iterations = config["max_iterations"]
            if "timeout" in config:
                agent.timeout_seconds = config["timeout"]
            if "tags" in config:
                agent.tags = config["tags"]
            if "metadata" in config:
                agent.agent_metadata = config["metadata"]

        await self.session.flush()
        await self.session.refresh(agent)

        return self._agent_to_response_dict(agent)

    async def delete_agent(self, agent_id: str) -> None:
        """
        删除 Agent 配置

        Args:
            agent_id: Agent ID

        Raises:
            NotFoundException: Agent 不存在
        """
        query = select(AgentConfig).where(AgentConfig.id == agent_id)
        result = await self.session.execute(query)
        agent = result.scalar_one_or_none()

        if not agent:
            raise NotFoundException(
                message=f"Agent 不存在: {agent_id}",
                resource_type="Agent",
                resource_id=agent_id,
                code="AGENT_001",
            )

        await self.session.delete(agent)

    async def check_agents_health(self) -> dict[str, Any]:
        """
        检查所有 Agent 的健康状态

        Returns:
            包含整体状态和各 Agent 状态的字典
        """
        # 查询所有活跃 Agent 配置
        query = select(AgentConfig).where(AgentConfig.is_active)
        result = await self.session.execute(query)
        agents = result.scalars().all()

        # 构建健康状态列表
        agent_statuses = []
        healthy_count = 0
        unhealthy_count = 0

        for agent in agents:
            # 检查 Agent 配置是否完整
            has_model = bool(agent.model_name)
            has_prompt = bool(agent.system_prompt)

            if has_model and has_prompt and agent.is_active:
                status_str = "healthy"
                healthy_count += 1
                error = None
            else:
                status_str = "unhealthy"
                unhealthy_count += 1
                error = "配置不完整" if not (has_model and has_prompt) else "状态异常"

            agent_statuses.append(
                {
                    "agent_id": str(agent.id),
                    "agent_name": agent.name,
                    "status": status_str,
                    "last_active": agent.created_at.isoformat() if agent.created_at else None,
                    "error": error,
                }
            )

        # 确定整体状态
        total = len(agents)
        if total == 0 or unhealthy_count == 0:
            overall_status = "healthy"
        elif healthy_count == 0:
            overall_status = "unhealthy"
        else:
            overall_status = "degraded"

        return {
            "overall_status": overall_status,
            "total_agents": total,
            "healthy_count": healthy_count,
            "unhealthy_count": unhealthy_count,
            "agents": agent_statuses,
            "checked_at": datetime.now().isoformat(),
        }

    async def get_default_agent(self, user_default_agent_id: str | None = None) -> dict[str, Any]:
        """
        获取默认 Agent

        Args:
            user_default_agent_id: 用户设置的默认 Agent ID（来自用户偏好）

        Returns:
            Agent 详情字典

        Raises:
            NotFoundException: 默认 Agent 不存在
        """
        # 首先尝试获取用户设置的默认 Agent
        if user_default_agent_id:
            try:
                agent_uuid = UUID(user_default_agent_id)
                result = await self.session.execute(
                    select(AgentConfig).where(AgentConfig.id == agent_uuid, AgentConfig.is_active)
                )
                agent = result.scalar_one_or_none()
                if agent:
                    return self._agent_to_response_dict(agent)
            except ValueError:
                pass  # 继续尝试其他方式

        # 返回系统默认 Agent（优先查找 config_id="lingxi" 的主 Agent）
        result = await self.session.execute(
            select(AgentConfig).where(AgentConfig.config_id == "lingxi", AgentConfig.is_active)
        )
        agent = result.scalar_one_or_none()

        # 如果没有灵汐，尝试查找 agent_type="main" 的 Agent
        if not agent:
            result = await self.session.execute(
                select(AgentConfig)
                .where(AgentConfig.agent_type == "main", AgentConfig.is_active)
                .order_by(AgentConfig.created_at.desc())
            )
            agent = result.scalar_one_or_none()

        # 如果还没有，查找任意活跃的 Agent
        if not agent:
            result = await self.session.execute(
                select(AgentConfig).where(AgentConfig.is_active).order_by(AgentConfig.created_at.desc())
            )
            agent = result.scalar_one_or_none()

        if not agent:
            raise NotFoundException(
                message="默认 Agent 不存在，请先运行数据库迁移",
                resource_type="Agent",
                resource_id="default",
                code="AGENT_002",
            )

        return self._agent_to_response_dict(agent)
