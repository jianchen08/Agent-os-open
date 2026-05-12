"""
资源搜索工具

提供搜索 Agent、工具、工作流的功能
"""

from typing import Any

from src.core.results import ToolExecutionResult
from src.tools.types import (
    Tool,
    ToolCategory,
    ToolLevel,
    ToolSource,
    create_success_result,
)


class ResourceSearchTool:
    """
    资源搜索工具

    提供：
    - 搜索 Agent
    - 搜索工具
    - 搜索工作流
    """

    def __init__(
        self,
        agent_registry=None,
        tool_registry=None,
        workflow_registry=None,
    ):
        """
        初始化资源搜索工具

        Args:
            agent_registry: Agent 注册表
            tool_registry: 工具注册表
            workflow_registry: 工作流注册表
        """
        self.agent_registry = agent_registry
        self.tool_registry = tool_registry
        self.workflow_registry = workflow_registry

    @staticmethod
    def get_tool_definition() -> Tool:
        """
        获取工具定义

        Returns:
            工具定义
        """
        return Tool(
            name="resource_search",
            description="搜索系统中的 Agent、工具和工作流资源。支持两种搜索模式：简单模式（默认）只返回名称和描述，适合快速浏览；详细模式返回完整信息包括类型、分类、标签等。使用建议：先用简单模式找到资源名称，再用 detailed=True + exact_match=True + exact_name 获取完整信息。",
            input_schema={
                "type": "object",
                "properties": {
                    "resource_type": {
                        "type": "string",
                        "enum": ["agent", "tool", "workflow", "all"],
                        "description": "要搜索的资源类型。可选值：'agent'（搜索 Agent）、'tool'（搜索工具）、'workflow'（搜索工作流）、'all'（搜索所有类型）",
                    },
                    "query": {
                        "type": "string",
                        "description": "搜索关键词，用于模糊匹配资源的名称、描述和标签。在 exact_match=False 时生效",
                    },
                    "category": {
                        "type": "string",
                        "description": "按功能分类过滤资源，例如 'development'、'search'、'file' 等",
                    },
                    "level": {
                        "type": "string",
                        "enum": ["system", "user", "all"],
                        "description": "按资源级别过滤。'system' 表示系统级资源，'user' 表示用户级资源，'all' 表示所有级别",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "返回结果的最大数量，默认 20 条",
                        "default": 20,
                    },
                    "detailed": {
                        "type": "boolean",
                        "description": "是否返回详细信息。False（默认）只返回 name 和 description；True 返回完整信息包括 type、category、level、tags 等",
                        "default": False,
                    },
                    "exact_match": {
                        "type": "boolean",
                        "description": "是否启用精确匹配模式。False（默认）为模糊匹配，名称包含关键词即可；True 为精确匹配，名称必须完全相等，需要配合 exact_name 使用",
                        "default": False,
                    },
                    "exact_name": {
                        "type": "string",
                        "description": "精确匹配的资源名称，仅在 exact_match=True 时生效。用于获取特定资源的详细信息",
                    },
                    "resource_id": {
                        "type": "string",
                        "description": "精确匹配的资源 ID，主要用于工作流的精确查找，配合 exact_match=True 使用",
                    },
                },
                "required": ["resource_type"],
            },
            source=ToolSource.CODE,
            category=ToolCategory.SEARCH,
            level=ToolLevel.SYSTEM,
            requires_approval=False,
            tags=["search", "resource", "system"],
            isolation_required=False,
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """
        执行搜索

        Args:
            inputs: 输入参数

        Returns:
            搜索结果
        """
        resource_type = inputs.get("resource_type", "all")
        query = inputs.get("query", "")
        category = inputs.get("category")
        level = inputs.get("level", "all")
        limit = inputs.get("limit", 20)
        detailed = inputs.get("detailed", False)
        exact_match = inputs.get("exact_match", False)
        exact_name = inputs.get("exact_name")
        resource_id = inputs.get("resource_id")

        results = {}

        # 搜索 Agent
        if resource_type in ["agent", "all"]:
            results["agents"] = await self._search_agents(
                query, category, level, limit, detailed, exact_match, exact_name
            )

        # 搜索工具
        if resource_type in ["tool", "all"]:
            results["tools"] = await self._search_tools(
                query, category, level, limit, detailed, exact_match, exact_name
            )

        # 搜索工作流
        if resource_type in ["workflow", "all"]:
            results["workflows"] = await self._search_workflows(
                query,
                category,
                level,
                limit,
                detailed,
                exact_match,
                exact_name,
                resource_id,
            )

        return create_success_result(
            data={
                "query": query,
                "results": results,
                "total": sum(len(v) for v in results.values()),
            },
            metadata={"action": "resource_search"},
        )

    def _get_agent_registry(self):
        """获取 Agent 注册表（延迟加载）"""
        if self.agent_registry is None:
            from src.agents.builtin.loader import load_all_agents
            self.agent_registry = load_all_agents()
        return self.agent_registry

    def _get_tool_registry(self):
        """获取 Tool 注册表（延迟加载）"""
        if self.tool_registry is None:
            from src.tools.global_registry import get_global_tool_registry_sync
            self.tool_registry = get_global_tool_registry_sync()
        return self.tool_registry

    def _get_workflow_registry(self):
        """获取 Workflow 注册表（延迟加载）"""
        if self.workflow_registry is None:
            from sqlalchemy import select

            from src.db.connection import get_async_session
            from src.db.models import Workflow

            class WorkflowRegistry:
                """工作流注册表包装器"""

                async def list_all(self):
                    """获取所有工作流"""
                    async for session in get_async_session():
                        stmt = select(Workflow).where(Workflow.status == "active")
                        result = await session.execute(stmt)
                        workflows = result.scalars().all()

                        class WorkflowWrapper:
                            """工作流包装器"""
                            def __init__(self, db_workflow):
                                self.id = db_workflow.id
                                self.metadata = type('Metadata', (), {
                                    'name': db_workflow.name,
                                    'description': db_workflow.description or '',
                                    'tags': db_workflow.tags or [],
                                    'category': None,
                                    'level': 'user'
                                })()
                                self.nodes = db_workflow.definition.get('nodes', [])

                        return [WorkflowWrapper(wf) for wf in workflows]
                        break

                    return []

            self.workflow_registry = WorkflowRegistry()
        return self.workflow_registry

    async def _search_agents(
        self,
        query: str,
        category: str | None,
        level: str,
        limit: int,
        detailed: bool = False,
        exact_match: bool = False,
        exact_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """搜索 Agent"""
        agent_registry = self._get_agent_registry()
        if not agent_registry:
            return []

        agents = []
        query_lower = query.lower()

        # 精确搜索时限制返回 1 条
        if detailed and exact_match:
            limit = 1

        # 遍历所有 Agent
        for agent_config in agent_registry.values():
            # 级别过滤
            if level != "all":
                agent_level = getattr(agent_config, "level", "user")
                if agent_level != level:
                    continue

            # 分类过滤
            if category:
                agent_category = getattr(agent_config, "category", None)
                if agent_category != category:
                    continue

            # 关键词匹配
            if self._match_query(
                query_lower,
                agent_config.name,
                agent_config.description,
                agent_config.tags,
                exact_match,
                exact_name,
            ):
                # 根据详细模式返回不同字段
                if detailed:
                    agents.append(
                        {
                            "name": agent_config.name,
                            "type": agent_config.agent_type.value,
                            "category": getattr(agent_config, "category", None),
                            "level": getattr(agent_config, "level", "user"),
                            "description": agent_config.description,
                            "tools": agent_config.tool_ids,
                            "tags": agent_config.tags,
                        }
                    )
                else:
                    agents.append(
                        {
                            "name": agent_config.name,
                            "description": agent_config.description,
                        }
                    )

                if len(agents) >= limit:
                    break

        return agents

    async def _search_tools(
        self,
        query: str,
        category: str | None,
        level: str,
        limit: int,
        detailed: bool = False,
        exact_match: bool = False,
        exact_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """搜索工具"""
        tool_registry = self._get_tool_registry()
        if not tool_registry:
            return []

        tools = []
        query_lower = query.lower()

        # 精确搜索时限制返回 1 条
        if detailed and exact_match:
            limit = 1

        # 遍历所有工具
        for tool in tool_registry.list_all():
            # 级别过滤
            if level != "all":
                tool_level = getattr(tool, "level", "user")
                if tool_level != level:
                    continue

            # 分类过滤
            if category:
                tool_category = getattr(tool, "category", None)
                if tool_category != category:
                    continue

            # 关键词匹配
            if self._match_query(
                query_lower,
                tool.name,
                tool.description,
                tool.tags,
                exact_match,
                exact_name,
            ):
                # 根据详细模式返回不同字段
                if detailed:
                    tools.append(
                        {
                            "name": tool.name,
                            "category": getattr(tool, "category", None),
                            "level": getattr(tool, "level", "user"),
                            "description": tool.description,
                            "requires_approval": tool.requires_approval,
                            "dangerous_operations": getattr(
                                tool, "dangerous_operations", []
                            ),
                            "tags": tool.tags,
                        }
                    )
                else:
                    tools.append(
                        {
                            "name": tool.name,
                            "description": tool.description,
                        }
                    )

                if len(tools) >= limit:
                    break

        return tools

    async def _search_workflows(
        self,
        query: str,
        category: str | None,
        level: str,
        limit: int,
        detailed: bool = False,
        exact_match: bool = False,
        exact_name: str | None = None,
        resource_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """搜索工作流"""
        workflow_registry = self._get_workflow_registry()
        if not workflow_registry:
            return []

        workflows = []
        query_lower = query.lower()

        # 精确搜索时限制返回 1 条
        if detailed and exact_match:
            limit = 1

        # 遍历所有工作流
        for workflow in await workflow_registry.list_all():
            # 级别过滤
            if level != "all":
                wf_level = getattr(workflow.metadata, "level", "user")
                if wf_level != level:
                    continue

            # 分类过滤
            if category:
                wf_category = getattr(workflow.metadata, "category", None)
                if wf_category != category:
                    continue

            # 关键词匹配
            if self._match_query(
                query_lower,
                workflow.metadata.name,
                workflow.metadata.description,
                workflow.metadata.tags,
                exact_match,
                exact_name,
                resource_id=resource_id,
                workflow_id=workflow.id,
            ):
                # 根据详细模式返回不同字段
                if detailed:
                    workflows.append(
                        {
                            "id": workflow.id,
                            "name": workflow.metadata.name,
                            "category": getattr(workflow.metadata, "category", None),
                            "level": getattr(workflow.metadata, "level", "user"),
                            "description": workflow.metadata.description,
                            "node_count": len(workflow.nodes),
                            "tags": workflow.metadata.tags,
                        }
                    )
                else:
                    workflows.append(
                        {
                            "id": workflow.id,
                            "name": workflow.metadata.name,
                            "description": workflow.metadata.description,
                        }
                    )

                if len(workflows) >= limit:
                    break

        return workflows

    def _match_query(
        self,
        query_lower: str,
        name: str,
        description: str,
        tags: list[str],
        exact_match: bool = False,
        exact_name: str | None = None,
        resource_id: str | None = None,
        workflow_id: str | None = None,
    ) -> bool:
        """
        匹配查询关键词

        Args:
            query_lower: 小写查询词
            name: 名称
            description: 描述
            tags: 标签列表
            exact_match: 是否精确匹配
            exact_name: 精确搜索的名称
            resource_id: 精确搜索的资源 ID
            workflow_id: 工作流 ID（用于 workflow 精确匹配）

        Returns:
            是否匹配
        """
        # 精确匹配模式
        if exact_match:
            # 优先使用 resource_id 进行匹配（主要用于 workflow）
            if resource_id and workflow_id:
                return resource_id == workflow_id

            # 使用 exact_name 进行精确匹配
            if exact_name:
                return exact_name.lower() == name.lower()

            # 如果没有提供 exact_name 或 resource_id，则用 query 进行精确匹配
            if query_lower:
                return query_lower == name.lower()

            return True

        # 模糊匹配模式（默认）
        if not query_lower:
            return True

        # 名称匹配
        if query_lower in name.lower():
            return True

        # 描述匹配
        if description and query_lower in description.lower():
            return True

        # 标签匹配
        for tag in tags:
            if query_lower in tag.lower():
                return True

        return False
