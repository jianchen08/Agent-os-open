"""
工具业务服务

提供工具注册、执行、权限管理和使用统计功能。

本模块已重构，核心功能已拆分到以下子模块：
- tool_permission_service.py: ToolPermissionManager 权限管理
- tool_usage_service.py: ToolUsageTracker 使用统计
- src/tools/types.py: Tool, ToolResult, ToolUsageStats 数据类
"""

import logging
from dataclasses import asdict
from datetime import datetime
from enum import Enum
from typing import Any

from src.config.settings import get_settings
from src.core.event_bus.base import EventBusBase
from src.services.tool_permission_service import ToolPermissionManager
from src.services.tool_usage_service import ToolUsageTracker
from src.tools.types import Tool, ToolUsageStats

logger = logging.getLogger(__name__)


class ToolService:
    """工具业务服务"""

    def __init__(self, event_bus: EventBusBase = None, tool_registry=None):
        """
        初始化工具服务

        Args:
            event_bus: 事件总线（可选）
            tool_registry: 工具注册表（可选，推荐通过 DI 注入）
        """
        self.tools: dict[str, Tool] = {}
        self.permission_manager = ToolPermissionManager()
        self.usage_tracker = ToolUsageTracker()
        self.event_bus = event_bus
        self.settings = get_settings()
        self.tool_registry = tool_registry

        # 同步加载默认工具（避免事件循环问题）
        self._init_default_tools()

    def _init_default_tools(self):
        """同步初始化默认工具"""
        try:
            from datetime import datetime  # noqa: PLC0415

            # 从工具注册表加载内置工具
            try:
                # 优先使用注入的 registry，否则使用全局单例
                if self.tool_registry is not None:
                    registry = self.tool_registry
                else:
                    from src.tools.global_registry import get_global_tool_registry_sync  # noqa: PLC0415

                    registry = get_global_tool_registry_sync()

                all_tools = registry.list_all()

                for tool_def in all_tools:
                    tool = Tool(
                        name=tool_def.name,
                        description=tool_def.description,
                        input_schema=tool_def.input_schema,
                        source=tool_def.source,
                        category=tool_def.category,
                        permissions=[],
                        parameters=tool_def.input_schema.get("properties", {}) if tool_def.input_schema else {},
                        enabled=True,
                        version="1.0.0",
                        author="system",
                        created_at=datetime.now(),
                    )
                    self.tools[tool.name] = tool

                logger.info(f"已从注册表加载 {len(all_tools)} 个内置工具")
            except Exception as e:
                logger.warning(f"从注册表加载工具失败: {e}，使用默认工具")
                # 回退到基础工具列表
                default_tools = [
                    Tool(
                        name="file_read",
                        description="读取文件内容",
                        input_schema={
                            "type": "object",
                            "properties": {
                                "path": {
                                    "type": "string",
                                    "description": "文件路径",
                                }
                            },
                            "required": ["path"],
                        },
                        source="code",
                        category="file",
                        permissions=["file:read"],
                        parameters={
                            "path": {
                                "type": "string",
                                "required": True,
                                "description": "文件路径",
                            }
                        },
                        enabled=True,
                        version="1.0.0",
                        author="system",
                        created_at=datetime.now(),
                    ),
                    Tool(
                        name="bash",
                        description="执行命令行命令",
                        input_schema={
                            "type": "object",
                            "properties": {
                                "command": {
                                    "type": "string",
                                    "description": "要执行的命令",
                                }
                            },
                            "required": ["command"],
                        },
                        source="code",
                        category="system",
                        permissions=["system:execute"],
                        parameters={
                            "command": {
                                "type": "string",
                                "required": True,
                                "description": "要执行的命令",
                            }
                        },
                        enabled=True,
                        version="1.0.0",
                        author="system",
                        created_at=datetime.now(),
                    ),
                ]

                for tool in default_tools:
                    self.tools[tool.name] = tool

                logger.info(f"已加载 {len(default_tools)} 个默认工具")
        except Exception as e:
            logger.error(f"加载默认工具失败: {e}")

    async def register_tool(self, tool: Tool) -> bool:
        """注册工具"""
        try:
            if not tool.created_at:
                tool.created_at = datetime.now()

            self.tools[tool.name] = tool

            # 设置工具权限
            self.permission_manager.set_tool_permissions(tool.name, tool.permissions)

            # 发送事件
            await self.event_bus.emit(
                "tool_registered",
                {
                    "tool_name": tool.name,
                    "category": tool.category.value if tool.category else None,
                    "permissions": tool.permissions,
                },
            )

            logger.info(f"工具已注册: {tool.name}")
            return True

        except Exception as e:
            logger.error(f"注册工具失败 {tool.name}: {e}")
            return False

    async def unregister_tool(self, tool_name: str) -> bool:
        """注销工具"""
        try:
            if tool_name in self.tools:
                del self.tools[tool_name]

                # 发送事件
                await self.event_bus.emit("tool_unregistered", {"tool_name": tool_name})

                logger.info(f"工具已注销: {tool_name}")
                return True
            return False

        except Exception as e:
            logger.error(f"注销工具失败 {tool_name}: {e}")
            return False

    async def get_tool_suggestions(self, context: str, user_id: str = None) -> list[Tool]:
        """获取工具建议"""
        try:
            suggestions = []
            context_lower = context.lower()

            for tool in self.tools.values():
                if not tool.enabled:
                    continue

                # 检查权限
                if user_id and not self.permission_manager.check_permission(user_id, tool.name):
                    continue

                # 简单的关键词匹配
                category_str = tool.category.value if tool.category else ""
                if (
                    context_lower in tool.name.lower()
                    or context_lower in tool.description.lower()
                    or context_lower in category_str.lower()
                ):
                    suggestions.append(tool)

            # 按使用频率排序
            tool_stats = self.usage_tracker.get_stats()
            suggestions.sort(
                key=lambda t: tool_stats.get(t.name, ToolUsageStats(t.name)).total_calls,
                reverse=True,
            )

            return suggestions[:10]  # 返回前10个建议

        except Exception as e:
            logger.error(f"获取工具建议失败: {e}")
            return []

    def get_available_tools(self, user_id: str = None, user_roles: list[str] = None) -> list[Tool]:
        """获取可用工具列表"""
        available_tools = []

        for tool in self.tools.values():
            if not tool.enabled:
                continue

            # 检查权限
            if user_id and not self.permission_manager.check_permission(user_id, tool.name, user_roles):
                continue

            available_tools.append(tool)

        return available_tools

    def get_tool_stats(self, tool_name: str = None) -> dict[str, Any]:
        """获取工具使用统计"""
        stats = self.usage_tracker.get_stats(tool_name)

        return {
            "stats": {name: asdict(stat) for name, stat in stats.items()},
            "top_tools": [asdict(stat) for stat in self.usage_tracker.get_top_tools()],
            "total_tools": len(self.tools),
            "enabled_tools": len([t for t in self.tools.values() if t.enabled]),
        }

    def get_tool_info(self, tool_name: str) -> dict[str, Any] | None:
        """获取工具详细信息"""
        if tool_name not in self.tools:
            return None

        tool = self.tools[tool_name]
        stats = self.usage_tracker.get_stats(tool_name).get(tool_name)

        return {
            "tool": tool.model_dump(),
            "stats": asdict(stats) if stats else None,
            "permissions": list(self.permission_manager.tool_permissions.get(tool_name, set())),
        }

    async def set_tool_enabled(self, tool_name: str, enabled: bool) -> bool:
        """启用/禁用工具"""
        if tool_name not in self.tools:
            return False

        self.tools[tool_name].enabled = enabled

        # 发送事件
        await self.event_bus.emit("tool_status_changed", {"tool_name": tool_name, "enabled": enabled})

        return True

    def add_user_permission(self, user_id: str, permission: str):
        """添加用户权限"""
        self.permission_manager.add_user_permission(user_id, permission)

    def add_role_permission(self, role: str, permission: str):
        """添加角色权限"""
        self.permission_manager.add_role_permission(role, permission)

    async def list_tools(  # noqa: PLR0912
        self,
        page: int = 1,
        page_size: int = 20,
        category: str | None = None,
        source: str | None = None,
        status: str | None = None,
        search: str | None = None,
    ) -> dict[str, Any]:
        """
        获取工具列表

        Args:
            page: 页码
            page_size: 每页数量
            category: 分类过滤
            source: 来源过滤
            status: 状态过滤
            search: 搜索关键词

        Returns:
            包含工具列表和分页信息的字典
        """
        try:
            # 获取所有工具 - 首先从 self.tools，如果为空则从注册表获取
            all_tools = list(self.tools.values())

            # 如果 self.tools 为空，从注册表动态加载
            if not all_tools:
                logger.info("[list_tools] self.tools is empty, loading from registry")
                try:
                    # 优先使用注入的 registry
                    if self.tool_registry is not None:
                        registry = self.tool_registry
                    else:
                        from src.tools.global_registry import (  # noqa: PLC0415
                            get_global_tool_registry_sync,
                        )

                        registry = get_global_tool_registry_sync()

                    registry_tools = registry.list_all()
                    logger.info(f"[list_tools] Loaded {len(registry_tools)} tools from registry")

                    # 将注册表工具转换为内部 Tool 格式
                    for tool_def in registry_tools:
                        if tool_def.name not in self.tools:
                            tool = Tool(
                                name=tool_def.name,
                                description=tool_def.description,
                                input_schema=tool_def.input_schema or {"type": "object", "properties": {}},
                                source=tool_def.source,
                                category=tool_def.category,
                                permissions=[],
                                parameters=tool_def.input_schema.get("properties", {}) if tool_def.input_schema else {},
                                enabled=True,
                                version="1.0.0",
                                author="system",
                                created_at=datetime.now(),
                            )
                            self.tools[tool.name] = tool
                            all_tools.append(tool)
                except Exception as e:
                    logger.error(f"[list_tools] Failed to load from registry: {e}")

            # 应用过滤条件
            filtered_tools = []
            for tool in all_tools:
                # 分类过滤
                category_str = tool.category.value if tool.category else ""
                if category and category_str != category:
                    continue

                # 来源过滤（默认所有工具都是 builtin）
                tool_source = getattr(tool, "source", "builtin")
                if isinstance(tool_source, Enum):
                    tool_source = tool_source.value
                if source and tool_source != source:
                    continue

                # 状态过滤
                tool_status = "active" if tool.enabled else "inactive"
                if status and tool_status != status:
                    continue

                # 搜索过滤
                if search:
                    search_lower = search.lower()
                    if (
                        search_lower not in tool.name.lower()
                        and search_lower not in tool.description.lower()
                        and search_lower not in category_str.lower()
                    ):
                        continue

                filtered_tools.append(tool)

            # 计算分页
            total = len(filtered_tools)
            start_idx = (page - 1) * page_size
            end_idx = start_idx + page_size
            page_tools = filtered_tools[start_idx:end_idx]

            # 转换为响应格式（符合ToolResponse schema）
            items = []
            for tool in page_tools:
                # 使用统一的 schema 构建方法
                input_schema = self._build_input_schema(tool.parameters)

                items.append(
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "input_schema": input_schema,
                        "output_schema": None,  # 暂时不定义输出schema
                        "source": getattr(tool, "source", "builtin"),
                        "category": tool.category.value if tool.category else "general",
                        "requires_approval": False,
                        "version": getattr(tool, "version", "1.0.0"),
                        "tags": [],
                        "status": "active" if tool.enabled else "inactive",
                    }
                )

            return {
                "items": items,
                "total": total,
                "page": page,
                "page_size": page_size,
            }

        except Exception as e:
            logger.error(f"获取工具列表失败: {e}")
            return {
                "items": [],
                "total": 0,
                "page": page,
                "page_size": page_size,
            }

    async def get_tool(self, tool_name: str) -> dict[str, Any] | None:
        """
        获取单个工具详情

        Args:
            tool_name: 工具名称

        Returns:
            工具详情字典，如果不存在则返回 None
        """
        try:
            logger.info(f"[get_tool] Looking for tool '{tool_name}'")

            # 从注册表获取工具
            # 优先使用注入的 registry
            if self.tool_registry is not None:
                registry = self.tool_registry
            else:
                from src.tools.global_registry import get_global_tool_registry_sync  # noqa: PLC0415

                registry = get_global_tool_registry_sync()

            tool_def = registry.get_optional(tool_name)

            if not tool_def:
                logger.warning(f"[get_tool] Tool '{tool_name}' not found in registry")
                return None

            logger.info(f"[get_tool] Found tool in registry: {tool_def.name}")

            # 直接从 tool_def 构建响应
            input_schema = tool_def.input_schema if tool_def.input_schema else {"type": "object", "properties": {}}
            return {
                "name": tool_def.name,
                "description": tool_def.description,
                "category": tool_def.category.value if tool_def.category else "general",
                "source": tool_def.source.value if tool_def.source else "builtin",
                "status": "active",
                "input_schema": input_schema,
                "output_schema": None,
                "requires_approval": tool_def.requires_approval if hasattr(tool_def, "requires_approval") else False,
                "parameters": input_schema.get("properties", {}),
                "created_at": None,
                "version": "1.0.0",
                "tags": list(tool_def.tags) if hasattr(tool_def, "tags") else [],
            }

        except Exception as e:
            logger.error(f"获取工具详情失败 {tool_name}: {e}", exc_info=True)
            return None

    async def generate_tool(
        self,
        name: str,
        description: str,
        category: str | None = None,
        parameters: dict[str, Any] | None = None,
        code: str | None = None,
        created_by: str | None = None,
    ) -> dict[str, Any]:
        """
        生成/创建新工具

        Args:
            name: 工具名称
            description: 工具描述
            category: 工具分类
            parameters: 参数定义
            code: 代码实现
            created_by: 创建者

        Returns:
            创建的工具信息

        Raises:
            ToolAlreadyExistsError: 工具已存在
        """
        try:
            if name in self.tools:
                from src.core.exceptions import ToolAlreadyExistsError  # noqa: PLC0415

                raise ToolAlreadyExistsError(f"工具 '{name}' 已存在")

            # 创建新工具
            from src.tools.types import ToolCategory  # noqa: PLC0415

            tool = Tool(
                name=name,
                description=description,
                input_schema=self._build_input_schema(parameters or {}),
                source="code",
                category=ToolCategory(category) if category else ToolCategory.SYSTEM,
                permissions=[],  # 自定义工具默认无权限要求
                parameters=parameters or {},
                created_at=datetime.now(),
                author=created_by or "system",
            )

            # 注册工具
            await self.register_tool(tool)

            # 构建输入 schema
            input_schema = self._build_input_schema(tool.parameters)

            return {
                "name": tool.name,
                "description": tool.description,
                "category": tool.category.value if tool.category else "custom",
                "source": "custom",
                "status": "active" if tool.enabled else "inactive",
                "input_schema": input_schema,
                "parameters": tool.parameters,
                "created_at": tool.created_at.isoformat() if tool.created_at else None,
                "version": getattr(tool, "version", "1.0.0"),
            }

        except Exception as e:
            logger.error(f"生成工具失败 {name}: {e}")
            raise

    async def update_tool(
        self,
        tool_name: str,
        status: str | None = None,
        description: str | None = None,
        category: str | None = None,
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """
        更新工具

        Args:
            tool_name: 工具名称
            status: 工具状态
            description: 工具描述
            category: 工具分类
            parameters: 参数定义

        Returns:
            更新后的工具信息，如果不存在则返回 None
        """
        try:
            if tool_name not in self.tools:
                return None

            tool = self.tools[tool_name]

            # 更新字段
            if status is not None:
                tool.enabled = status == "active"
            if description is not None:
                tool.description = description
            if category is not None:
                from src.tools.types import ToolCategory  # noqa: PLC0415

                tool.category = ToolCategory(category)
            if parameters is not None:
                tool.parameters = parameters

            # 构建输入 schema
            input_schema = self._build_input_schema(tool.parameters)

            return {
                "name": tool.name,
                "description": tool.description,
                "category": tool.category.value if tool.category else "general",
                "source": getattr(tool, "source", "builtin"),
                "status": "active" if tool.enabled else "inactive",
                "input_schema": input_schema,
                "parameters": tool.parameters,
                "created_at": tool.created_at.isoformat() if tool.created_at else None,
                "version": getattr(tool, "version", "1.0.0"),
            }

        except Exception as e:
            logger.error(f"更新工具失败 {tool_name}: {e}")
            return None

    async def delete_tool(self, tool_name: str) -> bool:
        """
        删除工具

        Args:
            tool_name: 工具名称

        Returns:
            是否删除成功
        """
        try:
            if tool_name not in self.tools:
                return False

            # 删除工具
            del self.tools[tool_name]

            # 发送事件
            await self.event_bus.emit("tool_deleted", {"tool_name": tool_name})

            logger.info(f"工具已删除: {tool_name}")
            return True

        except Exception as e:
            logger.error(f"删除工具失败 {tool_name}: {e}")
            return False

    async def rollback_tool(
        self,
        tool_name: str,
        version: int | None = None,
        rollback_by: str | None = None,
    ) -> dict[str, Any] | None:
        """
        回滚工具版本

        Args:
            tool_name: 工具名称
            version: 目标版本号
            rollback_by: 回滚操作者

        Returns:
            回滚后的工具信息，如果不存在则返回 None
        """
        try:
            if tool_name not in self.tools:
                return None

            tool = self.tools[tool_name]

            # 简单实现：重置为默认状态
            # 实际项目中应该有版本历史管理
            logger.info(f"工具回滚: {tool_name} 由 {rollback_by} 操作")

            # 构建输入 schema
            input_schema = self._build_input_schema(tool.parameters)

            return {
                "name": tool.name,
                "description": tool.description,
                "category": tool.category.value if tool.category else "general",
                "source": getattr(tool, "source", "builtin"),
                "status": "active" if tool.enabled else "inactive",
                "input_schema": input_schema,
                "parameters": tool.parameters,
                "created_at": tool.created_at.isoformat() if tool.created_at else None,
                "version": getattr(tool, "version", "1.0.0"),
            }

        except Exception as e:
            logger.error(f"回滚工具失败 {tool_name}: {e}")
            return None

    def _build_input_schema(self, parameters: dict[str, Any]) -> dict[str, Any]:
        """
        构建输入参数 Schema

        Args:
            parameters: 参数定义字典

        Returns:
            JSON Schema 格式的输入定义
        """
        if not parameters:
            return {"type": "object", "properties": {}}

        # 如果已经是正确的 schema 格式，直接返回
        if "type" in parameters and "properties" in parameters:
            return parameters

        # 否则，从参数定义构建 schema
        properties = {}
        required = []

        for param_name, param_def in parameters.items():
            if isinstance(param_def, dict):
                prop_def = {}

                # 类型映射
                param_type = param_def.get("type", "string")
                prop_def["type"] = param_type

                # 描述
                if "description" in param_def:
                    prop_def["description"] = param_def["description"]

                # 枚举值
                if "enum" in param_def:
                    prop_def["enum"] = param_def["enum"]

                # 默认值
                if "default" in param_def:
                    prop_def["default"] = param_def["default"]

                # 是否必需
                if param_def.get("required", False):
                    required.append(param_name)

                properties[param_name] = prop_def

        return {"type": "object", "properties": properties, "required": required}


# 全局工具服务实例
_tool_service = None


def get_tool_service() -> ToolService:
    """获取工具服务实例"""
    global _tool_service  # noqa: PLW0603
    if _tool_service is None:
        _tool_service = ToolService()
    return _tool_service
