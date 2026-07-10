"""UI Schema API 路由。

提供模块 UI Schema 的查询接口：
- ``GET /api/v1/modules/ui`` - 返回所有启用模块的 UI Schema 列表
- ``GET /api/v1/modules/ui/{module_id}`` - 返回指定模块的 UI Schema

同时根据模块 YAML 中的 ``data:`` 声明自动注册 CRUD 路由：
- ``GET    /api/v1/modules/{module_id}/data/{collection}`` - 列表查询
- ``GET    /api/v1/modules/{module_id}/data/{collection}/{record_id}`` - 单条查询
- ``POST   /api/v1/modules/{module_id}/data/{collection}`` - 创建记录
- ``PUT    /api/v1/modules/{module_id}/data/{collection}/{record_id}`` - 更新记录
- ``DELETE /api/v1/modules/{module_id}/data/{collection}/{record_id}`` - 删除记录

支持按客户端能力过滤（query param ``client_type``）。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query

from channels.api.deps import APIError, require_auth
from ui_schema.types import ModuleUISchema

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/modules/ui", tags=["UI Schema"])

_schema_parser: Any | None = None
_last_scan_time: float = 0
_SCAN_INTERVAL: float = 5.0

_CONFIG_DIRS = [Path("config/modules")]


def _get_schema_parser() -> Any:
    """获取 SchemaParser，支持 TTL 热重载。

    每隔 _SCAN_INTERVAL 秒重新扫描配置目录，自动感知文件的增删改。
    删除 YAML 文件后，对应的模块会从 API 响应中移除（热插拔）。

    Returns:
        SchemaParser 实例
    """
    global _schema_parser, _last_scan_time  # noqa: PLW0603

    from ui_schema.parser import SchemaParser  # noqa: PLC0415

    if _schema_parser is None:
        _schema_parser = SchemaParser()

    now = time.monotonic()
    if now - _last_scan_time < _SCAN_INTERVAL:
        return _schema_parser

    _last_scan_time = now

    _schema_parser._schemas.clear()
    _schema_parser._data_decls.clear()
    _schema_parser._file_mtimes.clear()
    _schema_parser._file_hashes.clear()

    for config_dir in _CONFIG_DIRS:
        if config_dir.exists():
            try:
                count = len(_schema_parser.load_directory(config_dir))
                logger.debug("热重载: 从 %s 加载了 %d 个 UI Schema", config_dir, count)
            except Exception as exc:
                logger.warning("热重载扫描失败 %s: %s", config_dir, exc)

    return _schema_parser


def _schema_to_dict(schema: ModuleUISchema) -> dict[str, Any]:
    """将 ModuleUISchema 序列化为前端兼容的字典。

    Args:
        schema: ModuleUISchema 对象

    Returns:
        前端 ModuleUISchema 接口对应的字典
    """
    result: dict[str, Any] = schema.model_dump(by_alias=True, exclude_none=True)
    return result


def _filter_by_client_type(schemas: list[ModuleUISchema], client_type: str | None) -> list[ModuleUISchema]:
    """按客户端类型过滤 Schema。

    根据客户端类型过滤掉不兼容的渲染空间和组件。

    Args:
        schemas: Schema 列表。
        client_type: 客户端类型（web/mobile/desktop/ide）。

    Returns:
        过滤后的 Schema 列表。
    """
    if client_type is None:
        return schemas

    # 客户端类型 -> 支持的渲染空间映射
    client_spaces: dict[str, set[str]] = {
        "web": {"chat", "workspace", "floating", "dock", "fullscreen"},
        "desktop": {"chat", "workspace", "floating", "dock", "fullscreen"},
        "mobile": {"chat", "workspace", "floating", "fullscreen"},
        "ide": {"chat", "workspace"},
    }

    supported = client_spaces.get(client_type)
    if supported is None:
        return schemas

    result: list[ModuleUISchema] = []
    for schema in schemas:
        # 过滤渲染空间：只保留客户端支持的空间
        filtered_spaces = [s for s in schema.rendering.spaces if s.space in supported]

        # 如果模块要求的空间客户端不支持，则跳过该模块
        required_spaces = set(schema.clients.required_spaces)
        if required_spaces and not required_spaces.issubset(supported):
            continue

        # 创建过滤后的 Schema 副本
        filtered_schema = schema.model_copy(deep=True)
        filtered_schema.rendering.spaces = filtered_spaces

        # Dock 在移动端和 IDE 不可用
        if client_type in ("mobile", "ide") and filtered_schema.rendering.dock is not None:
            filtered_schema.rendering.dock = None

        result.append(filtered_schema)

    return result


@router.get(
    "",
    summary="获取所有模块 UI Schema",
)
def list_ui_schemas(
    client_type: str | None = Query(
        default=None,
        description="客户端类型过滤 (web/mobile/desktop/ide)",
    ),
    _user: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    """获取所有启用模块的 UI Schema 列表。

    支持按客户端能力过滤，通过 ``client_type`` 参数指定。

    Returns:
        包含 items 和 total 的字典
    """
    parser = _get_schema_parser()
    schemas = parser.list_schemas()

    if client_type:
        schemas = _filter_by_client_type(schemas, client_type)

    items = [_schema_to_dict(s) for s in schemas]
    return {"items": items, "total": len(items)}


@router.get(
    "/{module_id}",
    summary="获取指定模块 UI Schema",
)
def get_ui_schema(
    module_id: str,
    client_type: str | None = Query(
        default=None,
        description="客户端类型过滤 (web/mobile/desktop/ide)",
    ),
    _user: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    """根据 module_id 获取单个模块的 UI Schema。

    Args:
        module_id: 模块唯一标识

    Returns:
        ModuleUISchema 对应的字典

    Raises:
        APIError: 模块不存在 (404)
    """
    parser = _get_schema_parser()
    schema = parser.get_schema(module_id)

    if schema is None:
        raise APIError(
            status_code=404,
            error_code="UI_SCHEMA_4004",
            message=f"模块 UI Schema '{module_id}' 不存在",
        )

    if client_type:
        filtered = _filter_by_client_type([schema], client_type)
        if not filtered:
            raise APIError(
                status_code=404,
                error_code="UI_SCHEMA_4005",
                message=f"模块 '{module_id}' 不支持客户端类型 '{client_type}'",
            )
        schema = filtered[0]

    return _schema_to_dict(schema)


# ---- 模块数据路由（手动注册） ----

_module_data_router: APIRouter | None = None


def get_module_data_router() -> APIRouter:
    """获取模块数据路由器，包含手动注册的模块数据端点。

    与 AutoCRUD 自动生成的路由互补，用于需要自定义逻辑的数据端点。
    使用惰性单例模式，首次调用时创建并缓存路由器。

    Returns:
        包含模块数据路由的 APIRouter
    """
    global _module_data_router  # noqa: PLW0603
    if _module_data_router is not None:
        return _module_data_router

    _module_data_router = APIRouter(prefix="/api/v1/modules", tags=["模块数据"])

    @_module_data_router.get(
        "/task-manager/data/tree",
        summary="获取任务树（通用数据协议）",
    )
    async def get_task_tree_via_module_api(
        session_id: str | None = Query(default=None, description="按会话 ID 过滤"),
        _user: dict[str, Any] = Depends(require_auth),
    ) -> dict[str, Any]:
        """通过通用数据协议获取任务树。

        与 /api/v1/projects/tree 功能完全一致，
        但使用 /api/v1/modules/{module_id}/data/{collection} 路径格式，
        符合通用数据协议规范，供前端 FileTreeWidget 通过
        task-manager://tree 协议访问。

        Args:
            session_id: 可选，按会话 ID 过滤任务

        Returns:
            包含 children（树形结构）、items（扁平列表）、total 的字典
        """
        from channels.api.routes_missing import get_task_tree  # noqa: PLC0415

        return await get_task_tree(session_id=session_id, _user=_user)

    return _module_data_router


# ---- Data CRUD 路由自动注册 ----

# 全局 CRUD 生成器缓存
_crud_generator: Any | None = None
_crud_routers_loaded: bool = False


def register_data_crud_routes() -> list[Any]:
    """扫描所有模块 YAML 中的 data 声明，自动注册 CRUD 路由。

    在应用启动时调用，惰性初始化。会先确保 SchemaParser 已加载
    （以触发 data 段的解析），然后遍历所有 data 声明注册路由。

    Returns:
        生成的 APIRouter 列表，需要通过 app.include_router 注册。
    """
    global _crud_generator, _crud_routers_loaded  # noqa: PLW0603
    if _crud_routers_loaded:
        if _crud_generator is None:
            return []
        # 返回已生成的路由器
        return list(_crud_generator._routers.values())

    _crud_routers_loaded = True

    try:
        # 确保 parser 已加载（会同时解析 data 段）
        parser = _get_schema_parser()
        all_data = parser.list_all_data_decls()

        if not all_data:
            logger.info("未发现任何 data 声明，跳过 CRUD 路由注册")
            return []

        from ui_schema.auto_crud import AutoCRUDGenerator  # noqa: PLC0415

        _crud_generator = AutoCRUDGenerator()

        all_routers: list[Any] = []
        for module_id, data_decls in all_data.items():
            if not isinstance(data_decls, dict):
                continue
            routers = _crud_generator.register_all(module_id, data_decls)
            all_routers.extend(routers)

        logger.info(
            "自动注册了 %d 个 CRUD 路由器（来自 %d 个模块）",
            len(all_routers),
            len(all_data),
        )
        return all_routers

    except Exception as exc:
        logger.warning("Data CRUD 路由注册失败: %s", exc)
        return []
