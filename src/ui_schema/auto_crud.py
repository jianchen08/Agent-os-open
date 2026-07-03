"""数据声明即接口 -- CRUD 自动生成器。

根据模块 YAML 中声明的 data 段，自动生成并注册 FastAPI CRUD 路由。
支持内存存储（默认）和自定义存储后端。

使用示例::

    from ui_schema.auto_crud import AutoCRUDGenerator

    generator = AutoCRUDGenerator()
    router = generator.register(
        module_id="my_module",
        collection="inventory",
        definition={
            "fields": {
                "id": {"type": "uuid", "primary": True, "auto": True},
                "name": {"type": "string", "required": True},
                "quantity": {"type": "integer", "default": 1, "min": 0},
            },
            "access": "crud",
            "filters": ["name"],
            "sort": ["name"],
            "pagination": True,
        },
    )
    # 将 router 注册到 FastAPI 应用
    app.include_router(router)
"""

from __future__ import annotations

import logging
import uuid
from copy import deepcopy
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from ui_schema.auth_types import AutoCRUDError

logger = logging.getLogger(__name__)

# ---- 类型映射：YAML 类型名 -> Python 类型 ----
_TYPE_MAP: dict[str, type] = {
    "string": str,
    "str": str,
    "integer": int,
    "int": int,
    "float": float,
    "number": float,
    "boolean": bool,
    "bool": bool,
    "uuid": str,
}

# ---- 全局内存存储：key=(module_id, collection)，value=记录列表 ----
_global_store: dict[tuple[str, str], list[dict[str, Any]]] = {}


def _get_store(module_id: str, collection: str) -> list[dict[str, Any]]:
    """获取指定模块和集合的内存存储。

    Args:
        module_id: 模块 ID。
        collection: 集合名称。

    Returns:
        记录列表引用。
    """
    key = (module_id, collection)
    if key not in _global_store:
        _global_store[key] = []
    return _global_store[key]


def _clear_store(module_id: str | None = None, collection: str | None = None) -> None:
    """清空内存存储，主要用于测试。

    Args:
        module_id: 模块 ID，为 None 时清空全部。
        collection: 集合名称，为 None 时清空该模块全部集合。
    """
    if module_id is None:
        _global_store.clear()
        return
    if collection is None:
        keys_to_remove = [k for k in _global_store if k[0] == module_id]
        for k in keys_to_remove:
            del _global_store[k]
        return
    _global_store.pop((module_id, collection), None)


def _coerce_value(value: Any, field_type: str) -> Any:
    """将输入值强制转换为字段声明类型。

    Args:
        value: 待转换的值。
        field_type: YAML 中声明的字段类型。

    Returns:
        转换后的值。
    """
    if value is None:
        return None
    python_type = _TYPE_MAP.get(field_type, str)
    if field_type == "uuid":
        return str(value)
    try:
        if python_type is bool:
            # 布尔值需要特殊处理：字符串 "true"/"false" 等
            if isinstance(value, str):
                return value.lower() in ("true", "1", "yes")
            return bool(value)
        return python_type(value)
    except (ValueError, TypeError):
        return value


def _validate_field_value(  # noqa: PLR0911
    field_name: str,
    value: Any,
    field_def: dict[str, Any],
) -> str | None:
    """校验单个字段值是否符合声明约束。

    Args:
        field_name: 字段名。
        value: 字段值。
        field_def: 字段定义（来自 YAML）。

    Returns:
        错误消息字符串，校验通过返回 None。
    """
    # 必填校验
    if field_def.get("required") and (value is None or value == ""):
        return f"字段 '{field_name}' 为必填项"

    if value is None:
        return None

    # 枚举校验
    enum_values = field_def.get("values")
    if enum_values is not None and value not in enum_values:
        return f"字段 '{field_name}' 的值 '{value}' 不在允许范围 {enum_values} 内"

    # 数值范围校验
    field_type = field_def.get("type", "string")
    if field_type in ("integer", "int", "float", "number"):
        min_val = field_def.get("min")
        max_val = field_def.get("max")
        if min_val is not None and value < min_val:
            return f"字段 '{field_name}' 的值 {value} 小于最小值 {min_val}"
        if max_val is not None and value > max_val:
            return f"字段 '{field_name}' 的值 {value} 大于最大值 {max_val}"

    # 字符串长度校验
    if field_type in ("string", "str") and isinstance(value, str):
        min_len = field_def.get("minLength") or field_def.get("min_length")
        max_len = field_def.get("maxLength") or field_def.get("max_length")
        if min_len is not None and len(value) < min_len:
            return f"字段 '{field_name}' 长度不足，最少 {min_len} 个字符"
        if max_len is not None and len(value) > max_len:
            return f"字段 '{field_name}' 长度超限，最多 {max_len} 个字符"

    return None


def _validate_record(
    data: dict[str, Any],
    fields: dict[str, dict[str, Any]],
    is_update: bool = False,
) -> list[str]:
    """校验整条记录的所有字段。

    Args:
        data: 待校验的数据字典。
        fields: 字段定义字典。
        is_update: 是否为更新操作（更新时跳过未提供字段的必填校验）。

    Returns:
        错误消息列表，为空表示校验通过。
    """
    errors: list[str] = []
    for field_name, field_def in fields.items():
        # 自动生成字段跳过
        if field_def.get("auto"):
            continue
        # 更新操作中未提供的字段跳过必填校验
        if is_update and field_name not in data:
            continue
        value = data.get(field_name)
        # 类型转换
        field_type = field_def.get("type", "string")
        if value is not None:
            value = _coerce_value(value, field_type)
        error = _validate_field_value(field_name, value, field_def)
        if error:
            errors.append(error)
    return errors


def _normalize_access(access: str) -> str:
    """将 access 模式标准化为下划线格式。

    支持连字符和下划线两种写法，统一输出下划线格式。
    例如: "read-only" → "read_only", "read_only" → "read_only"

    Args:
        access: 原始 access 字符串。

    Returns:
        标准化后的 access 字符串。
    """
    return access.replace("-", "_").lower()


def _find_primary_key(fields: dict[str, dict[str, Any]]) -> str | None:
    """查找主键字段名。

    Args:
        fields: 字段定义字典。

    Returns:
        主键字段名，未找到返回 None。
    """
    for name, field_def in fields.items():
        if field_def.get("primary"):
            return name
    return None


def _generate_default(field_def: dict[str, Any]) -> Any:
    """根据字段定义生成默认值。

    Args:
        field_def: 字段定义。

    Returns:
        生成的默认值。
    """
    if field_def.get("auto"):
        field_type = field_def.get("type", "string")
        if field_type == "uuid":
            return str(uuid.uuid4())
        if field_type in ("integer", "int"):
            return 0
        return None
    return field_def.get("default")


def _prepare_record(
    data: dict[str, Any],
    fields: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """准备一条完整记录：填充默认值、类型转换、生成自动字段。

    Args:
        data: 输入数据。
        fields: 字段定义。

    Returns:
        处理后的完整记录。
    """
    record: dict[str, Any] = {}
    for field_name, field_def in fields.items():
        if field_name in data:
            field_type = field_def.get("type", "string")
            record[field_name] = _coerce_value(data[field_name], field_type)
        elif field_def.get("auto"):
            record[field_name] = _generate_default(field_def)
        elif "default" in field_def:
            record[field_name] = field_def["default"]
    return record


class AutoCRUDGenerator:
    """CRUD 路由自动生成器。

    根据模块 YAML 中 data 段的声明，自动生成 FastAPI CRUD 路由。
    支持筛选、排序、分页，默认使用内存存储。

    Attributes:
        _routers: 已生成的路由器缓存，key=(module_id, collection)。
    """

    def __init__(self) -> None:
        """初始化 CRUD 生成器。"""
        self._routers: dict[tuple[str, str], APIRouter] = {}

    def register(
        self,
        module_id: str,
        collection: str,
        definition: dict[str, Any],
    ) -> APIRouter | None:
        """根据数据声明注册 CRUD 路由。

        Args:
            module_id: 模块 ID。
            collection: 集合名称。
            definition: 集合定义（来自 YAML data 段）。

        Returns:
            生成的 APIRouter，定义无效时返回 None。
        """
        fields = definition.get("fields")
        if not fields or not isinstance(fields, dict):
            logger.warning(
                "跳过无效的 data 定义: module=%s, collection=%s（缺少 fields）",
                module_id,
                collection,
            )
            return None

        access = _normalize_access(definition.get("access", "crud"))
        filters = definition.get("filters", [])
        sort_fields = definition.get("sort", [])
        pagination_enabled = definition.get("pagination", False)

        # 合法的 access 模式（需求 F-UI-29）
        valid_access = {"read_only", "read_write", "read_create", "crud", "write_only"}
        if access not in valid_access:
            logger.warning(
                "未知的 access 模式 '%s'，回退为 crud: module=%s, collection=%s",
                access,
                module_id,
                collection,
            )
            access = "crud"

        primary_key = _find_primary_key(fields)
        if primary_key is None:
            # 如果没有声明主键，自动添加 id 字段
            primary_key = "id"
            fields = {"id": {"type": "uuid", "primary": True, "auto": True}, **fields}

        prefix = f"/api/v1/modules/{module_id}/data/{collection}"
        router = APIRouter(
            prefix=prefix,
            tags=[f"Auto CRUD - {module_id}/{collection}"],
        )

        # 确保存储存在
        _get_store(module_id, collection)

        # ---- 注册 GET（列表）路由 ----
        # read_only / read_write / read_create / crud 均允许读
        if access in ("read_only", "read_write", "read_create", "crud"):
            self._register_list_route(
                router,
                module_id,
                collection,
                fields,
                filters,
                sort_fields,
                pagination_enabled,
                primary_key,
            )

        # ---- 注册 GET（单条）路由 ----
        if access in ("read_only", "read_write", "read_create", "crud"):
            self._register_get_route(
                router,
                module_id,
                collection,
                primary_key,
            )

        # ---- 注册 POST（创建）路由 ----
        # read_create / crud / write_only 均允许创建
        if access in ("read_create", "crud", "write_only"):
            self._register_create_route(
                router,
                module_id,
                collection,
                fields,
                primary_key,
            )

        # ---- 注册 PUT（更新）路由 ----
        # read_write / crud 均允许更新
        if access in ("read_write", "crud"):
            self._register_update_route(
                router,
                module_id,
                collection,
                fields,
                primary_key,
            )

        # ---- 注册 DELETE（删除）路由 ----
        # 仅 crud 允许删除
        if access == "crud":
            self._register_delete_route(
                router,
                module_id,
                collection,
                primary_key,
            )

        self._routers[(module_id, collection)] = router
        logger.info(
            "注册 CRUD 路由: %s (access=%s, primary_key=%s)",
            prefix,
            access,
            primary_key,
        )
        return router

    def register_all(
        self,
        module_id: str,
        data_decls: dict[str, dict[str, Any]],
    ) -> list[APIRouter]:
        """为模块的所有 data 集合批量注册 CRUD 路由。

        Args:
            module_id: 模块 ID。
            data_decls: data 声明字典，key 为集合名，value 为集合定义。

        Returns:
            成功生成的 APIRouter 列表。
        """
        routers: list[APIRouter] = []
        for collection_name, definition in data_decls.items():
            if not isinstance(definition, dict):
                logger.warning(
                    "跳过非字典的集合定义: module=%s, collection=%s",
                    module_id,
                    collection_name,
                )
                continue
            router = self.register(module_id, collection_name, definition)
            if router is not None:
                routers.append(router)
        return routers

    def _register_list_route(
        self,
        router: APIRouter,
        module_id: str,
        collection: str,
        fields: dict[str, dict[str, Any]],
        filters: list[str],
        sort_fields: list[str],
        pagination_enabled: bool,
        primary_key: str,
    ) -> None:
        """注册 GET 列表路由。

        支持筛选、排序和分页。筛选和排序通过 query 参数传递，
        分页使用 _page 和 _page_size 参数。

        Args:
            router: 目标路由器。
            module_id: 模块 ID。
            collection: 集合名称。
            fields: 字段定义。
            filters: 可筛选字段列表。
            sort_fields: 可排序字段列表。
            pagination_enabled: 是否启用分页。
            primary_key: 主键字段名。
        """
        # 闭包捕获变量
        _filters = list(filters)
        _sort_fields = list(sort_fields)
        _pagination = pagination_enabled
        _module_id = module_id
        _collection = collection
        endpoint_summary = f"获取 {module_id}/{collection} 列表"

        from channels.api.deps import require_auth as _require_auth  # noqa: PLC0415

        @router.get("", summary=endpoint_summary)
        def list_records(
            request: Request,
            _page: int = Query(default=1, ge=1, description="页码"),
            _page_size: int = Query(default=20, ge=1, le=100, description="每页数量"),
            _sort: str | None = Query(default=None, description="排序字段"),
            _order: str = Query(default="asc", description="排序方向: asc/desc"),
            _user: dict[str, Any] = Depends(_require_auth),
        ) -> dict[str, Any]:
            """获取集合中的记录列表，支持筛选、排序和分页。"""
            store = _get_store(_module_id, _collection)
            result = list(store)

            # ---- 筛选处理 ----
            query_params = dict(request.query_params)
            for filter_field in _filters:
                if filter_field in query_params:
                    filter_value = query_params[filter_field]
                    result = [r for r in result if str(r.get(filter_field, "")) == filter_value]

            total = len(result)

            # ---- 排序处理 ----
            sort_field = _sort
            if sort_field and sort_field in _sort_fields:
                reverse = _order.lower() == "desc"
                result.sort(
                    key=lambda r: (r.get(sort_field) is None, r.get(sort_field, "")),
                    reverse=reverse,
                )

            # ---- 分页处理 ----
            if _pagination:
                start = (_page - 1) * _page_size
                end = start + _page_size
                page_items = result[start:end]
                return {
                    "items": page_items,
                    "total": total,
                    "page": _page,
                    "page_size": _page_size,
                    "total_pages": max(1, (total + _page_size - 1) // _page_size),
                }

            return {
                "items": result,
                "total": total,
            }

    def _register_get_route(
        self,
        router: APIRouter,
        module_id: str,
        collection: str,
        primary_key: str,
    ) -> None:
        """注册 GET 单条记录路由。

        Args:
            router: 目标路由器。
            module_id: 模块 ID。
            collection: 集合名称。
            primary_key: 主键字段名。
        """
        endpoint_summary = f"获取 {module_id}/{collection} 单条记录"

        from channels.api.deps import require_auth as _require_auth  # noqa: PLC0415

        @router.get(
            "/{record_id}",
            summary=endpoint_summary,
        )
        def get_record(
            record_id: str,
            _user: dict[str, Any] = Depends(_require_auth),
        ) -> dict[str, Any]:
            """根据主键获取单条记录。"""
            store = _get_store(module_id, collection)
            for record in store:
                if str(record.get(primary_key)) == str(record_id):
                    return record
            raise AutoCRUDError(
                status_code=404,
                error_code="CRUD_4004",
                message=f"记录不存在: {primary_key}={record_id}",
            )

    def _register_create_route(
        self,
        router: APIRouter,
        module_id: str,
        collection: str,
        fields: dict[str, dict[str, Any]],
        primary_key: str,
    ) -> None:
        """注册 POST 创建路由。

        Args:
            router: 目标路由器。
            module_id: 模块 ID。
            collection: 集合名称。
            fields: 字段定义。
            primary_key: 主键字段名。
        """
        endpoint_summary = f"创建 {module_id}/{collection} 记录"

        from channels.api.deps import require_auth as _require_auth  # noqa: PLC0415

        @router.post("", summary=endpoint_summary)
        def create_record(
            body: dict[str, Any],
            _user: dict[str, Any] = Depends(_require_auth),
        ) -> dict[str, Any]:
            """创建一条新记录。"""
            # 字段校验
            errors = _validate_record(body, fields, is_update=False)
            if errors:
                raise AutoCRUDError(
                    status_code=400,
                    error_code="CRUD_4001",
                    message="数据校验失败",
                    details={"errors": errors},
                )

            # 准备记录
            record = _prepare_record(body, fields)

            # 如果主键未自动生成且未提供，则生成 UUID
            if primary_key not in record or not record[primary_key]:
                record[primary_key] = str(uuid.uuid4())

            # 检查主键唯一性
            store = _get_store(module_id, collection)
            for existing in store:
                if str(existing.get(primary_key)) == str(record[primary_key]):
                    raise AutoCRUDError(
                        status_code=409,
                        error_code="CRUD_4009",
                        message=f"记录已存在: {primary_key}={record[primary_key]}",
                    )

            # 添加元数据
            record["_created_at"] = datetime.now().isoformat()
            record["_updated_at"] = datetime.now().isoformat()

            store.append(record)
            logger.info(
                "创建记录: %s/%s %s=%s",
                module_id,
                collection,
                primary_key,
                record[primary_key],
            )
            return record

    def _register_update_route(
        self,
        router: APIRouter,
        module_id: str,
        collection: str,
        fields: dict[str, dict[str, Any]],
        primary_key: str,
    ) -> None:
        """注册 PUT 更新路由。

        Args:
            router: 目标路由器。
            module_id: 模块 ID。
            collection: 集合名称。
            fields: 字段定义。
            primary_key: 主键字段名。
        """
        endpoint_summary = f"更新 {module_id}/{collection} 记录"

        from channels.api.deps import require_auth as _require_auth  # noqa: PLC0415

        @router.put(
            "/{record_id}",
            summary=endpoint_summary,
        )
        def update_record(
            record_id: str,
            body: dict[str, Any],
            _user: dict[str, Any] = Depends(_require_auth),
        ) -> dict[str, Any]:
            """根据主键更新记录（部分更新）。"""
            # 字段校验（更新模式：跳过未提供字段的必填校验）
            errors = _validate_record(body, fields, is_update=True)
            if errors:
                raise AutoCRUDError(
                    status_code=400,
                    error_code="CRUD_4001",
                    message="数据校验失败",
                    details={"errors": errors},
                )

            store = _get_store(module_id, collection)
            for i, record in enumerate(store):
                if str(record.get(primary_key)) == str(record_id):
                    # 合并更新：仅更新提供的字段
                    updated = deepcopy(record)
                    for field_name, value in body.items():
                        if field_name in fields:
                            field_type = fields[field_name].get("type", "string")
                            updated[field_name] = _coerce_value(value, field_type)
                    updated["_updated_at"] = datetime.now().isoformat()
                    store[i] = updated
                    logger.info(
                        "更新记录: %s/%s %s=%s",
                        module_id,
                        collection,
                        primary_key,
                        record_id,
                    )
                    return updated

            raise AutoCRUDError(
                status_code=404,
                error_code="CRUD_4004",
                message=f"记录不存在: {primary_key}={record_id}",
            )

    def _register_delete_route(
        self,
        router: APIRouter,
        module_id: str,
        collection: str,
        primary_key: str,
    ) -> None:
        """注册 DELETE 删除路由。

        Args:
            router: 目标路由器。
            module_id: 模块 ID。
            collection: 集合名称。
            primary_key: 主键字段名。
        """
        endpoint_summary = f"删除 {module_id}/{collection} 记录"

        from channels.api.deps import require_auth as _require_auth  # noqa: PLC0415

        @router.delete(
            "/{record_id}",
            summary=endpoint_summary,
        )
        def delete_record(
            record_id: str,
            _user: dict[str, Any] = Depends(_require_auth),
        ) -> dict[str, Any]:
            """根据主键删除记录。"""
            store = _get_store(module_id, collection)
            for i, record in enumerate(store):
                if str(record.get(primary_key)) == str(record_id):
                    deleted = store.pop(i)
                    logger.info(
                        "删除记录: %s/%s %s=%s",
                        module_id,
                        collection,
                        primary_key,
                        record_id,
                    )
                    return {
                        "deleted": True,
                        "record": deleted,
                    }

            raise AutoCRUDError(
                status_code=404,
                error_code="CRUD_4004",
                message=f"记录不存在: {primary_key}={record_id}",
            )
