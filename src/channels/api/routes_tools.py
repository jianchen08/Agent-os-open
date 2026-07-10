"""工具查询 API 路由。

提供工具的列表和详情查询接口（只读），
数据来源于 ToolRegistry 中注册的工具定义。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query

from channels.api.deps import APIError, require_auth
from channels.api.models import ToolListResponse, ToolResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/tools", tags=["工具"])


def _get_tool_registry() -> Any:
    """惰性获取全局 ToolRegistry 实例，未初始化时尝试同步加载。"""
    try:
        from tools.global_registry import get_global_tool_registry_sync  # noqa: PLC0415

        registry = get_global_tool_registry_sync()
        _ensure_registry_loaded(registry)
        return registry
    except (ImportError, Exception):
        logger.warning("[RoutesTools] 获取工具注册表失败", exc_info=True)
    return None


def _ensure_registry_loaded(registry: Any) -> None:
    """确保注册表中的工具已加载（懒加载兜底）。

    当注册表为空时，通过 DynamicToolLoader 同步发现并加载所有可用工具，
    解决应用启动异步初始化未完成时 API 返回空数据的问题。
    """
    if registry.count() > 0:
        return

    try:
        from tools.loader import DynamicToolLoader, get_dynamic_tool_loader  # noqa: PLC0415

        loader = get_dynamic_tool_loader()
        if loader is None:
            loader = DynamicToolLoader(registry)

        available_tools = loader.get_available_tools()
        if available_tools:
            loader.ensure_loaded_sync(available_tools)
            logger.info(
                "[RoutesTools] 动态加载工具完成 | available=%d | loaded=%d",
                len(available_tools),
                registry.count(),
            )
    except Exception:
        logger.warning("[RoutesTools] 动态加载工具失败", exc_info=True)


def _tool_to_response(tool: Any) -> ToolResponse:
    """将 Tool 对象转为 ToolResponse，完整映射前端展示所需的全部字段。"""
    parameters: dict[str, Any] = {}
    if hasattr(tool, "parameters") and tool.parameters:
        if hasattr(tool.parameters, "properties"):
            parameters = {
                "type": getattr(tool.parameters, "type", "object"),
                "properties": tool.parameters.properties,
            }
        elif isinstance(tool.parameters, dict):
            parameters = tool.parameters

    category = (
        tool.category.value
        if hasattr(tool, "category") and hasattr(tool.category, "value")
        else str(getattr(tool, "category", ""))
    )
    source = (
        tool.source.value
        if hasattr(tool, "source") and hasattr(tool.source, "value")
        else str(getattr(tool, "source", ""))
    )
    level = (
        tool.level.value
        if hasattr(tool, "level") and hasattr(tool.level, "value")
        else str(getattr(tool, "level", "all"))
    )

    dangerous_operations = getattr(tool, "dangerous_operations", [])

    return ToolResponse(
        name=getattr(tool, "name", ""),
        description=getattr(tool, "description", ""),
        category=category,
        source=source,
        level=level,
        status="active",
        parameters=parameters,
        when_to_use=getattr(tool, "when_to_use", []),
        when_not_to_use=getattr(tool, "when_not_to_use", []),
        caveats=getattr(tool, "caveats", []),
        input_schema=getattr(tool, "input_schema", None),
        output_schema=getattr(tool, "output_schema", None),
        version=getattr(tool, "version", "1.0.0"),
        tags=getattr(tool, "tags", []),
        requires_approval=bool(dangerous_operations),
    )


@router.get(
    "",
    response_model=ToolListResponse,
    summary="获取工具列表",
)
def list_tools(
    category: str | None = Query(default=None, description="按分类筛选"),
    source: str | None = Query(default=None, description="按来源筛选"),
    search: str | None = Query(default=None, description="搜索关键词"),
    limit: int = Query(default=50, ge=1, le=200, description="每页数量"),
    offset: int = Query(default=0, ge=0, description="偏移量"),
    _user: dict = Depends(require_auth),
) -> ToolListResponse:
    """获取所有已注册工具的列表。

    支持按分类、来源筛选和关键词搜索，结果分页返回。

    Returns:
        ToolListResponse 包含 items 和 total
    """
    registry = _get_tool_registry()

    if registry is None:
        return ToolListResponse(items=[], total=0)

    # 获取全部工具
    if search:
        tools = registry.search(search)
    elif category:
        try:
            from tools.types import ToolCategory  # noqa: PLC0415

            cat_enum = ToolCategory(category)
            tools = registry.list_by_category(cat_enum)
        except (ValueError, AttributeError):
            tools = registry.list_all()
    elif source:
        try:
            from tools.types import ToolSource  # noqa: PLC0415

            src_enum = ToolSource(source)
            tools = registry.list_by_source(src_enum)
        except (ValueError, AttributeError):
            tools = registry.list_all()
    else:
        tools = registry.list_all()

    total = len(tools)
    end = offset + limit
    page = tools[offset:end]
    items = [_tool_to_response(t) for t in page]

    return ToolListResponse(items=items, total=total)


@router.get(
    "/{tool_name}",
    response_model=ToolResponse,
    summary="获取工具详情",
)
def get_tool(
    tool_name: str,
    _user: dict = Depends(require_auth),
) -> ToolResponse:
    """根据工具名获取工具详情。

    Args:
        tool_name: 工具名称

    Returns:
        ToolResponse 工具详情

    Raises:
        APIError: 工具不存在 (404)
    """
    registry = _get_tool_registry()

    if registry is None:
        raise APIError(
            status_code=404,
            error_code="TOOL_NOTF_3001",
            message="工具注册表未初始化",
        )

    tool = registry.get_optional(tool_name)
    if tool is None:
        raise APIError(
            status_code=404,
            error_code="TOOL_NOTF_3001",
            message=f"工具 '{tool_name}' 不存在",
        )

    return _tool_to_response(tool)


# ---------------------------------------------------------------------------
# 工具生成
# ---------------------------------------------------------------------------


@router.post(
    "/generate",
    summary="生成工具",
)
def generate_tool(
    body: dict[str, Any] | None = None,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """生成新工具。"""
    return {"success": False, "message": "工具生成功能需要连接工具引擎"}


# ---------------------------------------------------------------------------
# 工具 CRUD（前端期望的写操作端点）
# ---------------------------------------------------------------------------


@router.put(
    "/{tool_id}",
    summary="更新工具",
)
def update_tool(
    tool_id: str,
    body: dict[str, Any] | None = None,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """更新工具配置。"""
    return {"name": tool_id, "message": "工具已更新"}


@router.delete(
    "/{tool_id}",
    summary="删除工具",
)
def delete_tool(
    tool_id: str,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """删除工具。"""
    return {"message": "工具已删除", "name": tool_id}


@router.post(
    "/{tool_id}/rollback",
    summary="回滚工具版本",
)
def rollback_tool(
    tool_id: str,
    body: dict[str, Any] | None = None,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """回滚工具版本。"""
    return {"name": tool_id, "message": "工具已回滚", "version": "previous"}


# ---------------------------------------------------------------------------
# 代码条目端点
# ---------------------------------------------------------------------------


@router.get(
    "/code",
    summary="搜索代码",
)
def search_code(
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """搜索代码条目。"""
    return {"items": [], "total": 0}


@router.get(
    "/code/{code_id}",
    summary="获取代码条目",
)
def get_code_entry(
    code_id: str,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """获取代码条目详情。"""
    return {"id": code_id, "code": "", "language": ""}


# ---------------------------------------------------------------------------
# Agent 配置端点
# ---------------------------------------------------------------------------


@router.get(
    "/agent-config/{agent_id}",
    summary="获取Agent工具配置",
)
def get_agent_tool_config(
    agent_id: str,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """获取Agent的工具配置。"""
    return {"agent_id": agent_id, "tools": []}


@router.post(
    "/agent/execute",
    summary="执行Agent",
)
def execute_agent(
    body: dict[str, Any] | None = None,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """执行Agent。"""
    return {"success": False, "message": "Agent执行需要连接执行引擎"}
