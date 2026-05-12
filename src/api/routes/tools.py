"""
工具路由

提供工具管理相关的 API 端点
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from src.api.dependencies import generate_trace_id
from src.api.errors import create_error_response
from src.api.routes.auth import get_current_user
from src.api.schemas.tools import ToolListResponse, ToolResponse
from src.core.exceptions import ToolAlreadyExistsError
from src.services.tool_service import ToolService, get_tool_service

router = APIRouter()


# ============================================================================
# 请求模型
# ============================================================================


class ToolGenerateRequest(BaseModel):
    """工具生成请求"""

    name: str = Field(..., min_length=1, description="工具名称")
    description: str = Field(..., min_length=1, description="工具描述")
    category: str | None = Field(None, description="工具分类")
    parameters: dict | None = Field(None, description="参数定义")
    code: str | None = Field(None, description="代码实现")


class ToolRollbackRequest(BaseModel):
    """工具回滚请求"""

    version: int | None = Field(None, description="目标版本号")


class ToolUpdateRequest(BaseModel):
    """工具更新请求"""

    status: str | None = Field(None, description="工具状态: active/inactive")
    description: str | None = Field(None, description="工具描述")
    category: str | None = Field(None, description="工具分类")
    parameters: dict | None = Field(None, description="参数定义")


# ============================================================================
# 依赖注入
# ============================================================================


def get_tool_service_dep() -> ToolService:
    """获取工具服务依赖"""
    return get_tool_service()


# ============================================================================
# 路由
# ============================================================================


@router.get(
    "",
    response_model=ToolListResponse,
    summary="获取工具列表",
    description="获取可用的工具列表，支持分页和过滤",
)
async def list_tools(
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    category: str | None = Query(None, description="分类过滤"),
    source: str | None = Query(None, description="来源过滤"),
    status: str | None = Query(None, description="状态过滤"),
    search: str | None = Query(None, description="搜索关键词"),
    current_user=Depends(get_current_user),
    tool_service: ToolService = Depends(get_tool_service_dep),
) -> ToolListResponse:
    """
    获取工具列表

    Args:
        page: 页码
        page_size: 每页数量
        category: 分类过滤
        source: 来源过滤
        status: 状态过滤
        search: 搜索关键词
        current_user: 当前用户
        tool_service: 工具服务

    Returns:
        ToolListResponse: 工具列表
    """
    result = await tool_service.list_tools(
        page=page,
        page_size=page_size,
        category=category,
        source=source,
        status=status,
        search=search,
    )

    return ToolListResponse(**result)


@router.get(
    "/{tool_name}",
    response_model=ToolResponse,
    summary="获取工具详情",
    description="获取指定工具的详细信息",
)
async def get_tool(
    tool_name: str,
    current_user=Depends(get_current_user),
    tool_service: ToolService = Depends(get_tool_service_dep),
) -> ToolResponse:
    """
    获取工具详情

    Args:
        tool_name: 工具名称
        current_user: 当前用户
        tool_service: 工具服务

    Returns:
        ToolResponse: 工具详情

    Raises:
        HTTPException: 工具不存在
    """
    result = await tool_service.get_tool(tool_name=tool_name)

    if result is None:
        trace_id = generate_trace_id()
        error = create_error_response(
            error_code="TOOL_001", trace_id=trace_id, path=f"/api/v1/tools/{tool_name}"
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=error.model_dump(mode="json")
        )

    # 调试：打印返回结果
    import logging

    logger = logging.getLogger(__name__)
    logger.info(f"[get_tool API] result keys: {list(result.keys())}")
    logger.info(f"[get_tool API] has input_schema: {'input_schema' in result}")
    if "input_schema" in result:
        logger.info(
            f"[get_tool API] input_schema type: {type(result.get('input_schema'))}"
        )
        logger.info(f"[get_tool API] input_schema value: {result.get('input_schema')}")

    try:
        return ToolResponse(**result)
    except Exception as e:
        logger.error(f"[get_tool API] ToolResponse validation failed: {e}")
        logger.error(f"[get_tool API] result: {result}")
        raise


@router.post(
    "/generate",
    response_model=ToolResponse,
    status_code=status.HTTP_201_CREATED,
    summary="生成工具",
    description="生成/创建新的工具",
)
async def generate_tool(
    request: ToolGenerateRequest,
    current_user=Depends(get_current_user),
    tool_service: ToolService = Depends(get_tool_service_dep),
) -> ToolResponse:
    """
    生成工具

    Args:
        request: 生成请求
        current_user: 当前用户
        tool_service: 工具服务

    Returns:
        ToolResponse: 生成的工具

    Raises:
        HTTPException: 工具已存在
    """
    try:
        result = await tool_service.generate_tool(
            name=request.name,
            description=request.description,
            category=request.category,
            parameters=request.parameters,
            code=request.code,
            created_by=str(current_user.id),
        )
        return ToolResponse(**result)
    except ToolAlreadyExistsError:
        trace_id = generate_trace_id()
        error = create_error_response(
            error_code="TOOL_002",
            trace_id=trace_id,
            message=f"工具 '{request.name}' 已存在",
            path="/api/v1/tools/generate",
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=error.model_dump(mode="json")
        )


@router.patch(
    "/{tool_name}",
    response_model=ToolResponse,
    summary="更新工具",
    description="更新工具的状态、描述等信息",
)
async def update_tool(
    tool_name: str,
    request: ToolUpdateRequest,
    current_user=Depends(get_current_user),
    tool_service: ToolService = Depends(get_tool_service_dep),
) -> ToolResponse:
    """
    更新工具

    Args:
        tool_name: 工具名称
        request: 更新请求
        current_user: 当前用户
        tool_service: 工具服务

    Returns:
        ToolResponse: 更新后的工具

    Raises:
        HTTPException: 工具不存在
    """
    result = await tool_service.update_tool(
        tool_name=tool_name,
        status=request.status,
        description=request.description,
        category=request.category,
        parameters=request.parameters,
    )

    if result is None:
        trace_id = generate_trace_id()
        error = create_error_response(
            error_code="TOOL_001", trace_id=trace_id, path=f"/api/v1/tools/{tool_name}"
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=error.model_dump(mode="json")
        )

    return ToolResponse(**result)


@router.delete(
    "/{tool_name}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除工具",
    description="删除指定的工具",
)
async def delete_tool(
    tool_name: str,
    current_user=Depends(get_current_user),
    tool_service: ToolService = Depends(get_tool_service_dep),
) -> None:
    """
    删除工具

    Args:
        tool_name: 工具名称
        current_user: 当前用户
        tool_service: 工具服务

    Raises:
        HTTPException: 工具不存在
    """
    success = await tool_service.delete_tool(tool_name=tool_name)

    if not success:
        trace_id = generate_trace_id()
        error = create_error_response(
            error_code="TOOL_001", trace_id=trace_id, path=f"/api/v1/tools/{tool_name}"
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=error.model_dump(mode="json")
        )


@router.post(
    "/{tool_name}/rollback",
    response_model=ToolResponse,
    summary="回滚工具版本",
    description="回滚工具到指定版本或上一版本",
)
async def rollback_tool(
    tool_name: str,
    request: ToolRollbackRequest = None,
    current_user=Depends(get_current_user),
    tool_service: ToolService = Depends(get_tool_service_dep),
) -> ToolResponse:
    """
    回滚工具版本

    Args:
        tool_name: 工具名称
        request: 回滚请求
        current_user: 当前用户
        tool_service: 工具服务

    Returns:
        ToolResponse: 回滚后的工具

    Raises:
        HTTPException: 工具不存在或无法回滚
    """
    version = request.version if request else None

    result = await tool_service.rollback_tool(
        tool_name=tool_name,
        version=version,
        rollback_by=str(current_user.id),
    )

    if result is None:
        trace_id = generate_trace_id()
        error = create_error_response(
            error_code="TOOL_003",
            trace_id=trace_id,
            message=f"工具 '{tool_name}' 不存在或无法回滚",
            path=f"/api/v1/tools/{tool_name}/rollback",
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=error.model_dump(mode="json")
        )

    return ToolResponse(**result)
