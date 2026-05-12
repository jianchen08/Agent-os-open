"""
工作流路由

提供工作流管理相关的 API 端点
"""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.routes.auth import get_current_user
from src.db.connection import get_async_session
from src.services.workflow_service import WorkflowService

router = APIRouter()


async def get_workflow_service(
    session: AsyncSession = Depends(get_async_session),
) -> WorkflowService:
    """
    获取工作流服务实例

    Args:
        session: 数据库会话

    Returns:
        WorkflowService: 工作流服务实例
    """
    return WorkflowService(session)


# ============================================================================
# 数据模型
# ============================================================================


class WorkflowCreateRequest(BaseModel):
    """工作流创建请求"""

    name: str = Field(..., min_length=1, description="工作流名称")
    description: str | None = Field(None, description="工作流描述")
    definition: dict[str, Any] = Field(..., description="工作流定义（UWF 格式）")
    tags: list[str] = Field(default_factory=list, description="标签")


class WorkflowUpdateRequest(BaseModel):
    """工作流更新请求"""

    name: str | None = Field(None, min_length=1, description="工作流名称")
    description: str | None = Field(None, description="工作流描述")
    definition: dict[str, Any] | None = Field(None, description="工作流定义")
    tags: list[str] | None = Field(None, description="标签")
    status: str | None = Field(None, description="状态")


class WorkflowExecuteRequest(BaseModel):
    """工作流执行请求"""

    inputs: dict[str, Any] = Field(default_factory=dict, description="输入参数")
    timeout: float | None = Field(None, description="超时时间（秒）")


class WorkflowResponse(BaseModel):
    """工作流响应"""

    id: UUID = Field(..., description="工作流 ID")
    name: str = Field(..., description="工作流名称")
    description: str | None = Field(None, description="工作流描述")
    definition: dict[str, Any] = Field(..., description="工作流定义")
    status: str = Field(..., description="状态")
    tags: list[str] = Field(default_factory=list, description="标签")
    created_at: str = Field(..., description="创建时间")
    updated_at: str | None = Field(None, description="更新时间")


class WorkflowListResponse(BaseModel):
    """工作流列表响应"""

    items: list[WorkflowResponse] = Field(..., description="工作流列表")
    total: int = Field(..., ge=0, description="总数量")
    page: int = Field(..., ge=1, description="当前页码")
    page_size: int = Field(..., ge=1, description="每页数量")


class WorkflowExecuteResponse(BaseModel):
    """工作流执行响应"""

    execution_id: str = Field(..., description="执行 ID")
    workflow_id: str = Field(..., description="工作流 ID")
    status: str = Field(..., description="执行状态")
    message: str = Field(..., description="执行消息")


# ============================================================================
# 路由
# ============================================================================


@router.get(
    "",
    response_model=WorkflowListResponse,
    summary="获取工作流列表",
    description="获取当前用户可访问的工作流列表",
)
async def list_workflows(
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    status: str | None = Query(None, description="状态过滤"),
    search: str | None = Query(None, description="搜索关键词"),
    current_user=Depends(get_current_user),
    workflow_service: WorkflowService = Depends(get_workflow_service),
) -> WorkflowListResponse:
    """获取工作流列表"""
    result = await workflow_service.list_workflows(
        user_id=current_user.id,
        page=page,
        page_size=page_size,
        status=status,
        search=search,
    )

    return WorkflowListResponse(**result)


@router.post(
    "",
    response_model=WorkflowResponse,
    status_code=status.HTTP_201_CREATED,
    summary="创建工作流",
    description="创建新的工作流",
)
async def create_workflow(
    request: WorkflowCreateRequest,
    current_user=Depends(get_current_user),
    workflow_service: WorkflowService = Depends(get_workflow_service),
) -> WorkflowResponse:
    """创建工作流"""
    result = await workflow_service.create_workflow(
        user_id=current_user.id, **request.model_dump()
    )

    return WorkflowResponse(**result)


@router.get(
    "/{workflow_id}",
    response_model=WorkflowResponse,
    summary="获取工作流详情",
    description="获取指定工作流的详细信息",
)
async def get_workflow(
    workflow_id: UUID,
    current_user=Depends(get_current_user),
    workflow_service: WorkflowService = Depends(get_workflow_service),
) -> WorkflowResponse:
    """获取工作流详情"""
    # NotFoundException 会被全局异常处理器自动处理
    result = await workflow_service.get_workflow(
        workflow_id=workflow_id, user_id=current_user.id
    )
    return WorkflowResponse(**result)


@router.put(
    "/{workflow_id}",
    response_model=WorkflowResponse,
    summary="更新工作流",
    description="更新指定工作流的配置",
)
async def update_workflow(
    workflow_id: UUID,
    request: WorkflowUpdateRequest,
    current_user=Depends(get_current_user),
    workflow_service: WorkflowService = Depends(get_workflow_service),
) -> WorkflowResponse:
    """更新工作流"""
    # NotFoundException 和 ValidationException 会被全局异常处理器自动处理
    result = await workflow_service.update_workflow(
        workflow_id=workflow_id,
        user_id=current_user.id,
        **request.model_dump(exclude_unset=True),
    )

    return WorkflowResponse(**result)


@router.delete(
    "/{workflow_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除工作流",
    description="删除指定的工作流",
)
async def delete_workflow(
    workflow_id: UUID,
    current_user=Depends(get_current_user),
    workflow_service: WorkflowService = Depends(get_workflow_service),
) -> None:
    """删除工作流"""
    # NotFoundException 会被全局异常处理器自动处理
    await workflow_service.delete_workflow(
        workflow_id=workflow_id, user_id=current_user.id
    )
