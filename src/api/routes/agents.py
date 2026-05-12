"""
Agent 路由

提供 Agent 管理相关的 API 端点
"""

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.routes.auth import get_current_user
from src.api.schemas.agents import (
    AgentCreateRequest,
    AgentListResponse,
    AgentResponse,
    AgentUpdateRequest,
)
from src.db.connection import get_async_session
from src.services.agent_service import AgentService

router = APIRouter()


# ============================================================================
# 健康检查响应模型
# ============================================================================


class AgentHealthStatus(BaseModel):
    """单个 Agent 健康状态"""

    agent_id: str = Field(..., description="Agent ID")
    agent_name: str = Field(..., description="Agent 名称")
    status: str = Field(..., description="状态: healthy/unhealthy/unknown")
    last_active: str | None = Field(None, description="最后活跃时间")
    error: str | None = Field(None, description="错误信息")


class AgentHealthResponse(BaseModel):
    """Agent 健康检查响应"""

    overall_status: str = Field(..., description="整体状态: healthy/degraded/unhealthy")
    total_agents: int = Field(..., description="Agent 总数")
    healthy_count: int = Field(..., description="健康数量")
    unhealthy_count: int = Field(..., description="不健康数量")
    agents: list[AgentHealthStatus] = Field(..., description="各 Agent 状态")
    checked_at: str = Field(..., description="检查时间")


async def get_agent_service(
    session: AsyncSession = Depends(get_async_session),
) -> AgentService:
    """
    获取 Agent 服务实例

    Args:
        session: 数据库会话

    Returns:
        AgentService: Agent 服务实例
    """
    return AgentService(session)


@router.get(
    "/health",
    response_model=AgentHealthResponse,
    summary="Agent 健康检查",
    description="检查所有 Agent 的健康状态",
)
async def check_agents_health(
    current_user=Depends(get_current_user),
    agent_service: AgentService = Depends(get_agent_service),
) -> AgentHealthResponse:
    """
    检查 Agent 健康状态

    返回所有活跃 Agent 的健康状态信息
    """
    result = await agent_service.check_agents_health()
    return AgentHealthResponse(**result)


@router.get(
    "",
    response_model=AgentListResponse,
    summary="获取 Agent 列表",
    description="获取当前用户可访问的 Agent 列表，支持分页和过滤",
)
async def list_agents(
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    status: str | None = Query(None, description="状态过滤"),
    agent_type: str | None = Query(None, description="类型过滤"),
    search: str | None = Query(None, description="搜索关键词"),
    current_user=Depends(get_current_user),
    agent_service: AgentService = Depends(get_agent_service),
) -> AgentListResponse:
    """获取 Agent 列表"""
    result = await agent_service.list_agents(
        user_id=current_user.id,
        page=page,
        page_size=page_size,
        status=status,
        agent_type=agent_type,
        search=search,
    )

    return AgentListResponse(**result)


@router.post(
    "",
    response_model=AgentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="创建 Agent",
    description="创建新的 Agent 配置",
)
async def create_agent(
    request: AgentCreateRequest,
    current_user=Depends(get_current_user),
    agent_service: AgentService = Depends(get_agent_service),
) -> AgentResponse:
    """创建 Agent"""
    # 构建配置字典
    config = {
        "model": request.model,
        "system_prompt": request.system_prompt,
        "tool_names": request.tool_names,
        "max_iterations": request.max_iterations,
        "timeout": request.timeout,
        "tags": request.tags,
        "metadata": request.metadata,
    }

    result = await agent_service.create_agent(
        user_id=current_user.id,
        name=request.name,
        description=request.description,
        agent_type=request.agent_type or "user_defined",
        config=config,
    )

    return AgentResponse(**result)


@router.get(
    "/default",
    response_model=AgentResponse,
    summary="获取默认 Agent",
    description="获取用户的默认 Agent（用于新建会话）",
)
async def get_default_agent(
    current_user=Depends(get_current_user),
    agent_service: AgentService = Depends(get_agent_service),
) -> AgentResponse:
    """获取默认 Agent"""
    # 获取用户设置的默认 Agent ID
    default_agent_id = (
        current_user.preferences.get("default_agent_id")
        if current_user.preferences
        else None
    )

    # 调用服务层方法
    result = await agent_service.get_default_agent(
        user_default_agent_id=default_agent_id
    )
    return AgentResponse(**result)


@router.get(
    "/{agent_id}",
    response_model=AgentResponse,
    summary="获取 Agent 详情",
    description="获取指定 Agent 的详细信息",
)
async def get_agent(
    agent_id: str,
    current_user=Depends(get_current_user),
    agent_service: AgentService = Depends(get_agent_service),
) -> AgentResponse:
    """获取 Agent 详情"""
    # NotFoundException 会被全局异常处理器自动处理
    result = await agent_service.get_agent(agent_id=agent_id)
    return AgentResponse(**result)


@router.put(
    "/{agent_id}",
    response_model=AgentResponse,
    summary="更新 Agent",
    description="更新指定 Agent 的配置",
)
async def update_agent(
    agent_id: str,
    request: AgentUpdateRequest,
    current_user=Depends(get_current_user),
    agent_service: AgentService = Depends(get_agent_service),
) -> AgentResponse:
    """更新 Agent"""
    # 构建配置字典（仅包含非 None 的字段）
    config = {}
    if request.model is not None:
        config["model"] = request.model
    if request.system_prompt is not None:
        config["system_prompt"] = request.system_prompt
    if request.tool_names is not None:
        config["tool_names"] = request.tool_names
    if request.max_iterations is not None:
        config["max_iterations"] = request.max_iterations
    if request.timeout is not None:
        config["timeout"] = request.timeout
    if request.tags is not None:
        config["tags"] = request.tags
    if request.metadata is not None:
        config["metadata"] = request.metadata

    # NotFoundException 和 ValidationException 会被全局异常处理器自动处理
    result = await agent_service.update_agent(
        agent_id=agent_id,
        name=request.name,
        description=request.description,
        config=config if config else None,
        status=request.status,
    )

    return AgentResponse(**result)


@router.delete(
    "/{agent_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除 Agent",
    description="删除指定的 Agent",
)
async def delete_agent(
    agent_id: str,
    current_user=Depends(get_current_user),
    agent_service: AgentService = Depends(get_agent_service),
) -> None:
    """删除 Agent"""
    # NotFoundException 会被全局异常处理器自动处理
    await agent_service.delete_agent(agent_id=agent_id)
