"""
资源搜索工具

暴露接口：
- get_tool_definition() -> Tool：get_tool_definition功能
- ResourceSearchTool：ResourceSearchTool类
- WorkflowRegistry：WorkflowRegistry类
- WorkflowWrapper：WorkflowWrapper类
"""

import logging
from typing import Any

from core.constants import ToolLimits
from core.results import ToolExecutionResult
from tools.types import (
    Tool,
    ToolCategory,
    ToolLevel,
    ToolSource,
    create_success_result,
)

logger = logging.getLogger(__name__)


class ResourceSearchTool:
    """
    资源搜索工具

    提供：
    - 搜索 Agent
    - 搜索工具
    - 搜索工作流
    - 搜索 Skill

    支持两种搜索模式：
    1. 向量检索模式（推荐）：使用语义理解和混合检索
    2. 传统遍历模式（回退）：全量遍历 + 关键词匹配
    """

    def __init__(
        self,
        agent_registry=None,
        tool_registry=None,
        workflow_registry=None,
        skill_registry=None,
        search_engine=None,
        dynamic_tool_injector=None,
    ):
        """
        初始化资源搜索工具

        Args:
            agent_registry: Agent 注册表
            tool_registry: 工具注册表
            workflow_registry: 工作流注册表
            skill_registry: Skill 注册表
            search_engine: 搜索引擎（MemoryService 实例或创建函数）
            dynamic_tool_injector: 动态工具注入回调函数，签名为 async (tool_name: str) -> bool
        """
        self.agent_registry = agent_registry
        self.tool_registry = tool_registry
        self.workflow_registry = workflow_registry
        self.skill_registry = skill_registry
        self._search_engine = search_engine
        self._dynamic_tool_injector = dynamic_tool_injector

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义"""
        return Tool(
            name="resource_search",
            description="搜索系统中的 Agent、工具和 Skill 资源。支持模糊搜索和精确匹配。建议：先用模糊搜索找到资源名称，再用精确匹配获取完整信息。",
            input_schema={
                "type": "object",
                "properties": {
                    "resource_type": {
                        "type": "string",
                        "enum": ["agent", "tool", "skill", "all"],
                        "description": "资源类型。agent=Agent，tool=工具，skill=Skill（包含脚本），all=全部类型",
                    },
                    "query": {
                        "type": "string",
                        "description": "搜索关键词。模糊模式时匹配名称、描述、标签；精确模式时作为精确名称或ID进行匹配",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["simple", "detailed"],
                        "default": "simple",
                        "description": '返回模式。simple=模糊搜索，返回名称和描述列表；detailed=精确匹配，query作为精确名称或ID，返回单个资源的完整信息。不同资源类型的详细返回：Agent无detailed模式（产出物和评估指标已拼入description，simple即可获取完整信息）；Tool触发动态工具注入（支持逗号分隔批量加载，如query="rollback_task,state_update"，最多5个）；Skill返回SKILL.md完整文件内容。',
                    },
                    "filters": {
                        "type": "object",
                        "properties": {
                            "category": {
                                "type": "string",
                                "description": "按分类过滤",
                            },
                            "level": {
                                "type": "string",
                                "enum": ["system", "user", "all"],
                                "description": "按级别过滤。system=系统级，user=用户级，all=全部",
                            },
                            "language": {
                                "type": "string",
                                "description": "按语言过滤 Skill 中的脚本。可选：python/nodejs/bash/powershell",
                            },
                        },
                        "description": "可选过滤条件，用于缩小搜索范围",
                    },
                    "limit": {
                        "type": "integer",
                        "default": ToolLimits.RESOURCE_SEARCH_DEFAULT,
                        "maximum": ToolLimits.RESOURCE_SEARCH_DEFAULT,
                        "description": "返回数量，默认20条，最大20条",
                    },
                },
                "required": ["resource_type"],
            },
            source=ToolSource.CODE,
            category=ToolCategory.SEARCH,
            level=ToolLevel.SYSTEM,
            injected_params=["session_id", "parent_record_id"],
            tags=["search", "resource", "system"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """执行搜索"""
        resource_type = inputs.get("resource_type", "all")
        query = inputs.get("query", "")
        mode = inputs.get("mode", "simple")
        filters = inputs.get("filters", {})
        limit = min(
            inputs.get("limit", ToolLimits.RESOURCE_SEARCH_DEFAULT),
            ToolLimits.RESOURCE_SEARCH_DEFAULT,
        )

        detailed = mode == "detailed"
        exact = detailed

        # 获取 session_id 用于会话隔离
        session_id = inputs.get("session_id", "")
        parent_record_id = inputs.get("parent_record_id", "")

        logger.debug(
            f"[resource_search] execute: query={query}, mode={mode}, detailed={detailed}, session_id={session_id}"
        )
        logger.info(
            f"[resource_search] execute: query={query}, mode={mode}, detailed={detailed}, resource_type={resource_type}, session_id={session_id}"
        )
        category = filters.get("category")
        language = filters.get("language")
        level = filters.get("level", "all")

        search_engine = self._get_search_engine()
        if search_engine and query:
            try:
                if callable(search_engine):
                    search_engine_instance = await search_engine()
                else:
                    search_engine_instance = search_engine

                if search_engine_instance is None:
                    logger.info("[resource_search] 搜索引擎实例为空，跳过向量检索")
                else:
                    results = await self._search_with_engine(
                        search_engine=search_engine_instance,
                        resource_type=resource_type,
                        query=query,
                        limit=limit,
                        detailed=detailed,
                        category=category,
                        language=language,
                        level=level,
                        exact=exact,
                    )
                    if results:
                        return create_success_result(
                            data={
                                "query": query,
                                **results,
                                "total": sum(
                                    v for k, v in results.items() if k.endswith("_c")
                                ),
                                "mode": "vector",
                            },
                            metadata={"action": "resource_search"},
                        )
            except Exception as e:
                logger.warning("Vector search failed, fallback to traversal: %s", e)

        results = {}

        if resource_type in ["agent", "all"]:
            # BUG-FIX-fix_20260422_file_check_path: 使用 detailed=True 获取
            # recommended_metrics，使 LLM 能看到正确的 file_check 路径
            agent_names, agent_descriptions, agent_ids, agent_details = await self._search_agents(
                query, category, level, limit, detailed=True, exact=False
            )
            if agent_names:
                results["agent_h"] = ["config_id", "agent_name", "agent_description"]
                results["agent_d"] = []
                for i in range(len(agent_names)):
                    row = [agent_ids[i], agent_names[i], agent_descriptions[i]]
                    detail = agent_details[i] if i < len(agent_details) else {}
                    metrics = detail.get("recommended_metrics", [])
                    if metrics:
                        metrics_str = "; ".join(
                            f"{m.get('metric_id', '')}({', '.join(f'{k}={v}' for k, v in m.get('default_params', {}).items())})"
                            for m in metrics
                        )
                        row.append(f"推荐评估: {metrics_str}")
                        if len(results["agent_h"]) == 3:
                            results["agent_h"].append("recommended_metrics")
                    results["agent_d"].append(row)
                results["agent_c"] = len(agent_names)

        if resource_type in ["tool", "all"]:
            tool_names, tool_descriptions, tool_schemas = await self._search_tools(
                query, category, level, limit, detailed, exact
            )
            logger.debug(
                f"[resource_search] _search_tools 返回：tool_names={tool_names}, detailed={detailed}"
            )
            if tool_names:
                # detailed 模式：触发动态工具加载和注入，返回简化消息
                if detailed and tool_names:
                    logger.debug(f"[resource_search] 准备注入动态工具：{tool_names}")
                    await self._inject_dynamic_tools(
                        tool_names, session_id, parent_record_id
                    )
                    results["tool_h"] = ["tool_name", "tool_description"]
                    results["tool_d"] = [
                        [tool_names[i], tool_descriptions[i]]
                        for i in range(len(tool_names))
                    ]
                    results["tool_c"] = len(tool_names)
                    if len(tool_names) == 1:
                        results["message"] = (
                            f"工具 '{tool_names[0]}' 已找到并加载，现在可以直接调用该工具"
                        )
                    else:
                        results["message"] = (
                            f"工具 {tool_names} 已找到并加载，现在可以直接调用这些工具"
                        )
                else:
                    # simple 模式：返回 schema（如果有）
                    if tool_schemas and any(tool_schemas):
                        results["tool_h"] = [
                            "tool_name",
                            "tool_description",
                            "tool_schema",
                        ]
                        results["tool_d"] = [
                            [tool_names[i], tool_descriptions[i], str(tool_schemas[i])]
                            for i in range(len(tool_names))
                        ]
                    else:
                        results["tool_h"] = ["tool_name", "tool_description"]
                        results["tool_d"] = [
                            [tool_names[i], tool_descriptions[i]]
                            for i in range(len(tool_names))
                        ]
                    results["tool_c"] = len(tool_names)

        if resource_type in ["skill", "all"]:
            skill_names, skill_descriptions, skill_details = await self._search_skills(
                query, language, limit, detailed, exact
            )
            if skill_names:
                if detailed and skill_details and any(skill_details):
                    results["skill_h"] = [
                        "skill_name",
                        "skill_description",
                        "skill_content",
                    ]
                    results["skill_d"] = [
                        [
                            skill_names[i],
                            skill_descriptions[i],
                            skill_details[i].get("skill_content", ""),
                        ]
                        for i in range(len(skill_names))
                    ]
                else:
                    results["skill_h"] = ["skill_name", "skill_description"]
                    results["skill_d"] = [
                        [skill_names[i], skill_descriptions[i]]
                        for i in range(len(skill_names))
                    ]
                results["skill_c"] = len(skill_names)

        return create_success_result(
            data={
                "query": query,
                **results,
                "mode": "traversal",
            },
            metadata={"action": "resource_search"},
        )

    async def _search_with_engine(
        self,
        search_engine,
        resource_type: str,
        query: str,
        limit: int,
        detailed: bool,
        category: str | None,
        language: str | None,
        level: str | None,
        exact: bool = False,
    ) -> dict[str, Any]:
        """
        使用 MemoryService 进行向量检索

        Args:
            search_engine: MemoryService 实例
            resource_type: 资源类型（agent/tool/skill/all）
            query: 搜索查询
            limit: 返回数量限制
            detailed: 是否返回详细信息
            category: 分类过滤
            language: 语言过滤（仅脚本）
            level: 级别过滤
            exact: 是否精确匹配

        Returns:
            搜索结果字典，格式与 execute() 方法的 results 相同
        """
        try:
            # 构建检索配置
            from memory.types import RetrievalMethod

            # 执行向量检索（使用 MemoryService.retrieve() 的正确参数）
            results = await search_engine.retrieve(
                user_id="system",  # 系统级资源搜索
                query=query,
                retrieval_method=RetrievalMethod.VECTOR.value,
                top_k=limit * 2,  # 多取一些，用于过滤
            )

            if not results:
                logger.info(f"[resource_search] 向量检索无结果: query={query}")
                return {}

            # 按资源类型分组
            grouped_results = {}

            for result in results:
                # 从 metadata 中提取资源信息
                metadata = result.metadata or {}
                res_type = metadata.get("resource_type", "unknown")
                res_name = metadata.get("name", "")
                res_description = metadata.get("description", "")

                # 过滤资源类型
                if resource_type != "all" and res_type != resource_type:
                    continue

                # 过滤分类
                if category and metadata.get("category") != category:
                    continue

                # 过滤级别
                if level and level != "all" and metadata.get("level") != level:
                    continue

                # 过滤语言（仅 Skill 中的脚本）
                if (
                    language
                    and res_type == "skill"
                    and metadata.get("language") != language
                ):
                    continue

                # 精确匹配过滤
                if exact and query.lower() != res_name.lower():
                    continue

                # 按类型分组
                if res_type not in grouped_results:
                    grouped_results[res_type] = []

                grouped_results[res_type].append(
                    {
                        "name": res_name,
                        "description": res_description,
                        "metadata": metadata,
                        "score": result.score if hasattr(result, "score") else 1.0,
                    }
                )

            # 构建返回结果（与 traversal 模式格式一致）
            output = {}

            # Agent 结果
            if "agent" in grouped_results:
                agents = grouped_results["agent"][:limit]
                output["agent_h"] = ["agent_name", "agent_description"]
                output["agent_d"] = [[a["name"], a["description"]] for a in agents]
                output["agent_c"] = len(agents)

            # Tool 结果
            if "tool" in grouped_results:
                tools = grouped_results["tool"][:limit]
                if detailed:
                    output["tool_h"] = ["tool_name", "tool_description"]
                    output["tool_d"] = [[t["name"], t["description"]] for t in tools]
                else:
                    output["tool_h"] = ["tool_name", "tool_description", "tool_schema"]
                    output["tool_d"] = [
                        [
                            t["name"],
                            t["description"],
                            str(t["metadata"].get("input_schema", {})),
                        ]
                        for t in tools
                    ]
                output["tool_c"] = len(tools)

            # Skill 结果
            if "skill" in grouped_results:
                skills = grouped_results["skill"][:limit]
                if detailed:
                    output["skill_h"] = [
                        "skill_name",
                        "skill_description",
                        "skill_content",
                    ]
                    output["skill_d"] = [
                        [
                            s["name"],
                            s["description"],
                            s["metadata"].get("skill_content", ""),
                        ]
                        for s in skills
                    ]
                else:
                    output["skill_h"] = ["skill_name", "skill_description"]
                    output["skill_d"] = [[s["name"], s["description"]] for s in skills]
                output["skill_c"] = len(skills)

            logger.info(
                f"[resource_search] 向量检索成功: query={query}, "
                f"results={sum(len(v) for v in grouped_results.values())}"
            )

            return output

        except Exception as e:
            logger.warning(
                f"[resource_search] 向量检索失败，回退到遍历模式: {e}", exc_info=True
            )
            return {}

    def _get_search_engine(self):
        """
        获取搜索引擎（延迟加载）

        直接复用 MemoryService 的搜索接口。
        首次调用时创建工厂函数，后续调用复用已创建的实例。
        """
        if self._search_engine is None:
            try:
                from infrastructure.db import get_async_session
                from memory.service import MemoryService

                _instance_cache = {"instance": None}

                async def create_or_get_engine():
                    """创建或复用 MemoryService 实例"""
                    if _instance_cache["instance"] is not None:
                        return _instance_cache["instance"]
                    # BUG-FIX-fix_20260416_001: 修复 async for 误用为 coroutine 的问题
                    # 问题根因: get_async_session() 是 async def 函数，返回 AsyncSession|None，
                    #           不是异步生成器，不能用 async for 遍历
                    # 修复方案: 改用 await 获取 session
                    session = await get_async_session()
                    if session is None:
                        return None
                    _instance_cache["instance"] = MemoryService(session=session)
                    return _instance_cache["instance"]

                self._search_engine = create_or_get_engine
            except Exception as e:
                logger.warning("Failed to initialize MemoryService: %s", e)
                self._search_engine = None

        return self._search_engine

    def _get_agent_registry(self):
        """获取 Agent 注册表（延迟加载，从 config/agents/ YAML 目录加载）"""
        if self.agent_registry is None:
            try:
                from agents.registry import AgentRegistry
                from pathlib import Path

                registry = AgentRegistry()
                config_dir = Path(__file__).resolve().parent.parent.parent.parent / "config" / "agents"
                if config_dir.exists():
                    registry.load_directory(config_dir)
                self.agent_registry = registry
            except Exception as e:
                logger.warning("Failed to load agent registry from config: %s", e)
                self.agent_registry = None
        return self.agent_registry

    def _get_tool_registry(self):
        """获取 Tool 注册表（延迟加载）"""
        if self.tool_registry is None:
            from tools.global_registry import get_global_tool_registry_sync

            self.tool_registry = get_global_tool_registry_sync()
        return self.tool_registry

    def _get_workflow_registry(self):
        """获取 Workflow 注册表（延迟加载）"""
        if self.workflow_registry is None:
            from sqlalchemy import select

            from db.models import Workflow
            from infrastructure.db import get_async_session

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
                                self.metadata = type(
                                    "Metadata",
                                    (),
                                    {
                                        "name": db_workflow.name,
                                        "description": db_workflow.description or "",
                                        "tags": db_workflow.tags or [],
                                        "category": None,
                                        "level": "user",
                                    },
                                )()
                                self.nodes = db_workflow.definition.get("nodes", [])
                                # BUG-FIX-fix_20260316: 添加 inputs 属性
                                # 问题根因: WorkflowWrapper 缺少 inputs 属性，导致详细模式无法返回工作流输入定义
                                # 修复方案: 从 workflow.definition 中提取 inputs
                                self.inputs = db_workflow.definition.get("inputs", {})

                        return [WorkflowWrapper(wf) for wf in workflows]

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
        exact: bool = False,
    ) -> tuple[list[str], list[str], list[str], list[dict]]:
        """搜索 Agent，返回名称、描述、config_id、详情。"""
        agent_registry = self._get_agent_registry()
        if not agent_registry:
            return [], [], [], []

        names = []
        descriptions = []
        config_ids = []
        details_list = []
        query_lower = query.lower()

        if detailed and exact:
            limit = 1

        for agent_config in agent_registry.list_all():
            if level != "all":
                agent_level = getattr(agent_config, "level", "user")
                if agent_level != level:
                    continue

            if category:
                agent_category = getattr(agent_config, "category", None)
                if agent_category != category:
                    continue

            config_id = getattr(agent_config, "config_id", "")
            matched = self._match_query(
                query_lower,
                agent_config.name,
                agent_config.description,
                agent_config.tags,
                exact,
            )
            if not matched and config_id and query_lower in config_id.lower():
                matched = True
            if matched:
                names.append(agent_config.name)
                descriptions.append(agent_config.description)
                config_ids.append(config_id)

                if detailed:
                    details_list.append(
                        {
                            "deliverables": getattr(agent_config, "deliverables", None)
                            or [],
                            "recommended_metrics": getattr(
                                agent_config, "recommended_metrics", None
                            )
                            or [],
                        }
                    )
                else:
                    details_list.append({})

                if len(names) >= limit:
                    break

        return names, descriptions, config_ids, details_list

    async def _search_tools(
        self,
        query: str,
        category: str | None,
        level: str,
        limit: int,
        detailed: bool = False,
        exact: bool = False,
    ) -> tuple[list[str], list[str], list[dict]]:
        """搜索工具，detailed 模式支持逗号分隔批量匹配"""
        tool_registry = self._get_tool_registry()
        if not tool_registry:
            return [], [], []

        names = []
        descriptions = []
        schemas_list = []

        if detailed:
            query_parts = [q.strip() for q in query.split(",") if q.strip()]
            limit = min(len(query_parts), 5)
            for query_part in query_parts:
                if len(names) >= limit:
                    break
                query_lower = query_part.lower()
                found = self._match_tool_single(
                    tool_registry, query_lower, category, level, exact=True
                )
                if found and found.name not in names:
                    names.append(found.name)
                    descriptions.append(found.description)
                    schemas_list.append({})

            if not names:
                names, descriptions, schemas_list = await self._search_tools_from_db(
                    query, category, level, limit, detailed, exact
                )
            return names, descriptions, schemas_list

        query_lower = query.lower()

        for tool in tool_registry.list_all():
            if level != "all":
                tool_level = getattr(tool, "level", "user")
                if tool_level != level:
                    continue

            if category:
                tool_category = getattr(tool, "category", None)
                if tool_category != category:
                    continue

            if self._match_query(
                query_lower,
                tool.name,
                tool.description,
                tool.tags,
                exact,
            ):
                names.append(tool.name)
                descriptions.append(tool.description)
                schemas_list.append({})

                if len(names) >= limit:
                    break

        if not names:
            names, descriptions, schemas_list = await self._search_tools_from_db(
                query, category, level, limit, detailed, exact
            )

        return names, descriptions, schemas_list

    def _match_tool_single(
        self,
        tool_registry,
        query_lower: str,
        category: str | None,
        level: str,
        exact: bool = True,
    ):
        """在工具注册表中精确匹配单个工具"""
        for tool in tool_registry.list_all():
            if level != "all":
                tool_level = getattr(tool, "level", "user")
                if tool_level != level:
                    continue

            if category:
                tool_category = getattr(tool, "category", None)
                if tool_category != category:
                    continue

            if query_lower == tool.name.lower():
                return tool
        return None

    async def _search_tools_from_db(
        self,
        query: str,
        category: str | None,
        level: str,
        limit: int,
        detailed: bool = False,
        exact: bool = False,
    ) -> tuple[list[str], list[str], list[dict]]:
        """从数据库 tool_library 表搜索工具（内存注册表无结果时的回退）"""
        try:
            from sqlalchemy import select

            from db.models import ToolLibrary
            from infrastructure.db import get_async_session

            names = []
            descriptions = []
            schemas_list = []
            query_lower = query.lower()

            session = await get_async_session()
            if session is None:
                return [], [], []

            async with session:
                stmt = select(ToolLibrary).where(ToolLibrary.status == "active")

                if level and level != "all":
                    stmt = stmt.where(ToolLibrary.level == level)
                if category:
                    stmt = stmt.where(ToolLibrary.category == category)

                result = await session.execute(stmt)
                db_tools = result.scalars().all()

                for db_tool in db_tools:
                    if self._match_query(
                        query_lower,
                        db_tool.name,
                        db_tool.description or "",
                        db_tool.tags or [],
                        exact,
                    ):
                        names.append(db_tool.name)
                        descriptions.append(db_tool.description or "")

                        if detailed:
                            schemas_list.append(
                                {
                                    "input_schema": db_tool.input_schema or {},
                                    "output_schema": db_tool.output_schema or {},
                                    "when_to_use": db_tool.when_to_use or [],
                                }
                            )
                        else:
                            schemas_list.append({})

                        if len(names) >= limit:
                            break

            if names:
                logger.info(
                    f"[resource_search] 从数据库搜索到 {len(names)} 个工具（内存注册表为空）"
                )

            return names, descriptions, schemas_list

        except Exception as e:
            logger.warning(f"[resource_search] 数据库搜索工具失败: {e}")
            return [], [], []

    async def _search_workflows(
        self,
        query: str,
        category: str | None,
        level: str,
        limit: int,
        detailed: bool = False,
        exact: bool = False,
    ) -> tuple[list[str], list[str], list[str], list[dict]]:
        """搜索工作流"""
        workflow_registry = self._get_workflow_registry()
        if not workflow_registry:
            return [], [], [], []

        ids = []
        names = []
        descriptions = []
        inputs_list = []
        query_lower = query.lower()

        if detailed and exact:
            limit = 1

        for workflow in await workflow_registry.list_all():
            if level != "all":
                wf_level = getattr(workflow.metadata, "level", "user")
                if wf_level != level:
                    continue

            if category:
                wf_category = getattr(workflow.metadata, "category", None)
                if wf_category != category:
                    continue

            if self._match_query(
                query_lower,
                workflow.metadata.name,
                workflow.metadata.description,
                workflow.metadata.tags,
                exact,
                resource_id=query if exact else None,
                workflow_id=workflow.id,
            ):
                ids.append(workflow.id)
                names.append(workflow.metadata.name)
                descriptions.append(workflow.metadata.description)

                if detailed:
                    inputs_list.append(workflow.inputs or {})
                else:
                    inputs_list.append({})

                if len(names) >= limit:
                    break

        return ids, names, descriptions, inputs_list

    def _match_query(
        self,
        query_lower: str,
        name: str,
        description: str,
        tags: list[str],
        exact: bool = False,
        resource_id: str | None = None,
        workflow_id: str | None = None,
    ) -> bool:
        """匹配查询关键词"""
        if exact:
            if resource_id and workflow_id:
                return resource_id == workflow_id
            if query_lower:
                return query_lower == name.lower()
            return True

        if not query_lower:
            return True

        if query_lower in name.lower():
            return True

        if description and query_lower in description.lower():
            return True

        for tag in tags:
            if query_lower in tag.lower():
                return True

        return False

    def _get_skill_registry(self):
        """获取 Skill 注册表（延迟加载）"""
        if self.skill_registry is None:
            try:
                from skills.registry import get_global_skill_registry
                self.skill_registry = get_global_skill_registry()
            except ImportError:
                self.skill_registry = None
        return self.skill_registry

    async def _search_skills(
        self,
        query: str,
        language: str | None,
        limit: int,
        detailed: bool = False,
        exact: bool = False,
    ) -> tuple[list[str], list[str], list[dict]]:
        """
        搜索 Skill

        simple 模式返回名称和描述，detailed 模式返回 SKILL.md 完整文件内容。
        """
        skill_registry = self._get_skill_registry()
        if not skill_registry or not skill_registry.is_initialized():
            return [], [], []

        names = []
        descriptions = []
        details_list = []
        query_lower = query.lower()

        if detailed and exact:
            limit = 1

        skills = skill_registry.search_skills(
            query=query if not exact else "",
            limit=limit * 2,
        )

        for skill in skills:
            if language:
                has_matching_lang = any(s.language == language for s in skill.scripts)
                if not has_matching_lang:
                    continue

            if exact:
                if query_lower != skill.skill_name.lower():
                    continue

            names.append(skill.skill_name)
            descriptions.append(skill.description)

            if detailed:
                skill_content = self._read_skill_markdown(skill.skill_path)
                details_list.append({"skill_content": skill_content})
            else:
                details_list.append({})

            if len(names) >= limit:
                break

        return names, descriptions, details_list

    def _read_skill_markdown(self, skill_path: str) -> str:
        """读取 Skill 的 SKILL.md 文件内容"""
        try:
            from pathlib import Path

            skill_dir = Path(skill_path)
            if not skill_dir.exists():
                return ""

            candidates = [
                skill_dir / "SKILL.md",
                skill_dir / "skill.md",
            ]
            for candidate in candidates:
                if candidate.exists():
                    with open(candidate, encoding="utf-8") as f:
                        return f.read()

            return ""
        except Exception as e:
            logger.warning("读取 Skill 文件失败: %s, 错误: %s", skill_path, e)
            return ""

    async def _inject_dynamic_tools(
        self, tool_names: list[str], session_id: str = "", parent_record_id: str = ""
    ) -> None:
        """
        通过 auto_loader 将搜索到的工具动态加载到全局 ToolRegistry。

        加载后的工具会被 registry 标记为动态工具，
        后续 ToolSchemaPlugin 会自动将其 schema 合并到 LLM 可见的工具列表中。

        Args:
            tool_names: 要注入的工具名称列表
            session_id: 会话 ID（保留参数，暂未使用）
            parent_record_id: 父记录 ID（保留参数，暂未使用）
        """
        logger.info(
            "[resource_search] 开始注入动态工具: tool_names=%s", tool_names
        )

        from tools.auto_loader import get_tool_auto_loader

        auto_loader = get_tool_auto_loader()
        if not auto_loader:
            logger.warning("[resource_search] ToolAutoLoader 未初始化，无法注入动态工具")
            return

        for tool_name in tool_names:
            try:
                tool = await auto_loader.auto_load_tool(tool_name)
                if not tool:
                    logger.warning("[resource_search] 工具加载失败: %s", tool_name)
                    continue
                logger.info("[resource_search] 动态工具加载成功: %s", tool_name)
            except Exception as e:
                logger.error(
                    "[resource_search] 动态工具加载失败: %s, 错误: %s",
                    tool_name, e,
                )
