"""
资源搜索工具

暴露接口：
- get_tool_definition() -> Tool：get_tool_definition功能
- ResourceSearchTool：ResourceSearchTool类
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
        skill_registry=None,
        search_engine=None,
        dynamic_tool_injector=None,
        external_search=None,
    ):
        """
        初始化资源搜索工具

        Args:
            agent_registry: Agent 注册表
            tool_registry: 工具注册表
            skill_registry: Skill 注册表
            search_engine: 搜索引擎（MemoryService 实例或创建函数）
            dynamic_tool_injector: 动态工具注入回调函数，签名为 async (tool_name: str) -> bool
            external_search: ExternalResourceSearch 实例（可选，None 则不启用外部搜索）
        """
        self.agent_registry = agent_registry
        self.tool_registry = tool_registry
        self.skill_registry = skill_registry
        self._search_engine = search_engine
        self._dynamic_tool_injector = dynamic_tool_injector
        self._external_search = external_search
        self._desc_cache: dict[str, str] = {}

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义"""
        return Tool(
            name="resource_search",
            description="搜索系统内 Agent、工具、Skill 资源。已有明确资源映射时直接使用，无需搜索。空结果是正常的，不要重复搜索。",
            when_to_use=[
                "不确定有哪些可用资源时",
                "需要加载未在当前工具列表中的工具时（用 detailed 模式）",
            ],
            when_not_to_use=[
                "已知资源名称或映射 → 直接使用，无需搜索",
                "搜索文件内容 → 用 enhanced_search",
                "搜索互联网信息 → 用 web_search",
            ],
            caveats=[
                "搜索无结果是正常的，不要重复调用",
                "每次只调用一次",
            ],
            input_schema={
                "type": "object",
                "properties": {
                    "resource_type": {
                        "type": "string",
                        "enum": ["agent", "tool", "skill", "all"],
                        "description": "资源类型：agent/tool/skill/all",
                    },
                    "query": {
                        "type": "string",
                        "description": "搜索关键词",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["simple", "detailed"],
                        "default": "simple",
                        "description": 'simple=列出匹配资源；detailed=按精确名称加载资源（tool类型会动态注入到当前会话，支持逗号分隔批量，如 query="file_read,bash_execute"）',
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
                                "description": "按级别过滤",
                            },
                            "language": {
                                "type": "string",
                                "description": "按语言过滤 Skill 脚本：python/nodejs/bash/powershell",
                            },
                        },
                        "description": "可选过滤条件",
                    },
                    "limit": {
                        "type": "integer",
                        "default": ToolLimits.RESOURCE_SEARCH_DEFAULT,
                        "maximum": ToolLimits.RESOURCE_SEARCH_DEFAULT,
                        "description": "返回数量，默认20",
                    },
                },
                "required": ["resource_type"],
            },
            source=ToolSource.CODE,
            category=ToolCategory.SEARCH,
            level=ToolLevel.SYSTEM,
            injected_params=["session_id", "parent_record_id", "_retriever"],
            tags=["search", "resource", "system"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:  # noqa: PLR0912,PLR0915
        """执行搜索"""
        # 缓存注入的向量检索器（由 ToolCore 通过 _SERVICE_INJECT_MAP 注入）
        injected_retriever = inputs.get("_retriever")
        if injected_retriever and not self._search_engine:
            self._search_engine = injected_retriever

        resource_type = inputs.get("resource_type", "all")
        query = inputs.get("query", "")
        mode = inputs.get("mode", "simple")
        filters = inputs.get("filters", {})
        raw_limit = inputs.get("limit", ToolLimits.RESOURCE_SEARCH_DEFAULT)
        limit = min(int(raw_limit), ToolLimits.RESOURCE_SEARCH_DEFAULT)

        detailed = mode == "detailed"
        exact = detailed

        # 获取 session_id 用于会话隔离
        session_id = inputs.get("session_id", "")
        parent_record_id = inputs.get("parent_record_id", "")

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
                            data=self._slim_results(results, detailed=detailed),
                            metadata={},
                        )
            except Exception as e:
                logger.warning("Vector search failed, fallback to traversal: %s", e)

        results = {}

        if resource_type in ["agent", "all"]:
            agent_names, agent_descriptions, agent_ids, agent_details = await self._search_agents(
                query, category, level, limit, detailed=detailed, exact=False
            )
            if agent_names:
                results["agent_h"] = ["config_id", "agent_name", "agent_description"]
                results["agent_d"] = []
                for i in range(len(agent_names)):
                    row = [agent_ids[i], agent_names[i], agent_descriptions[i]]
                    detail = agent_details[i] if i < len(agent_details) else {}
                    metrics = detail.get("recommended_metrics", [])
                    if metrics:

                        def _metric_str(m):
                            mid = getattr(m, "metric_id", None) or (
                                m.get("metric_id", "") if isinstance(m, dict) else ""
                            )
                            params = getattr(m, "default_params", None) or (
                                m.get("default_params", {}) if isinstance(m, dict) else {}
                            )
                            if isinstance(params, dict) and params:
                                return f"{mid}({', '.join(f'{k}={v}' for k, v in params.items())})"
                            return str(mid)

                        metrics_str = "; ".join(_metric_str(m) for m in metrics)
                        row.append(f"推荐评估: {metrics_str}")
                        if len(results["agent_h"]) == 3:
                            results["agent_h"].append("recommended_metrics")
                    results["agent_d"].append(row)
                results["agent_c"] = len(agent_names)

        if resource_type in ["tool", "all"]:
            tool_names, tool_descriptions, tool_schemas = await self._search_tools(
                query, category, level, limit, detailed, exact
            )
            logger.debug(f"[resource_search] _search_tools 返回：tool_names={tool_names}, detailed={detailed}")
            if tool_names:
                # detailed 模式：触发动态工具加载和注入，返回简化消息
                if detailed and tool_names:
                    logger.debug(f"[resource_search] 准备注入动态工具：{tool_names}")
                    await self._inject_dynamic_tools(tool_names, session_id, parent_record_id)
                    results["tool_h"] = ["tool_name", "tool_description"]
                    results["tool_d"] = [[tool_names[i], tool_descriptions[i]] for i in range(len(tool_names))]
                    results["tool_c"] = len(tool_names)
                    if len(tool_names) == 1:
                        results["message"] = f"工具 '{tool_names[0]}' 已找到并加载，现在可以直接调用该工具"
                    else:
                        results["message"] = f"工具 {tool_names} 已找到并加载，现在可以直接调用这些工具"
                else:
                    # simple 模式：返回 schema（如果有）
                    if tool_schemas and any(tool_schemas):
                        results["tool_h"] = [
                            "tool_name",
                            "tool_description",
                            "tool_schema",
                        ]
                        results["tool_d"] = [
                            [tool_names[i], tool_descriptions[i], str(tool_schemas[i])] for i in range(len(tool_names))
                        ]
                    else:
                        results["tool_h"] = ["tool_name", "tool_description"]
                        results["tool_d"] = [[tool_names[i], tool_descriptions[i]] for i in range(len(tool_names))]
                    results["tool_c"] = len(tool_names)

        if resource_type in ["skill", "all"]:
            skill_names, skill_descriptions, skill_details = await self._search_skills(
                query,
                language,
                limit,
                detailed,
                exact,
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
                    results["skill_d"] = [[skill_names[i], skill_descriptions[i]] for i in range(len(skill_names))]
                results["skill_c"] = len(skill_names)

        return create_success_result(
            data=self._slim_results(results, detailed=detailed),
            metadata={},
        )

    async def _search_with_engine(  # noqa: PLR0912
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
            # 直接调用 IRetriever.retrieve()（资源搜索不经过 MemoryService）
            results = await search_engine.retrieve(
                query=query,
                user_id=None,  # 资源搜索不按用户隔离
                top_k=limit * 2,  # 多取一些，用于过滤
                memory_type="resource",  # 标记为资源类型检索
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
                if resource_type not in ("all", res_type):
                    continue

                # 过滤分类
                if category and metadata.get("category") != category:
                    continue

                # 过滤级别
                if level and level != "all" and metadata.get("level") != level:
                    continue

                # 过滤语言（仅 Skill 中的脚本）
                if language and res_type == "skill" and metadata.get("language") != language:
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
            logger.warning(f"[resource_search] 向量检索失败，回退到遍历模式: {e}", exc_info=True)
            return {}

    @staticmethod
    def _slim_results(results: dict[str, Any], detailed: bool) -> dict[str, Any]:  # noqa: ARG004
        """精简搜索结果，移除对 LLM 无用的字段

        Args:
            results: 原始搜索结果（包含 _h/_d/_c 等字段）
            detailed: 保留参数，目前所有 _h/_d/_c/message 都保留

        Returns:
            精简后的结果字典
        """
        slim = {}
        for key, value in results.items():
            if key.endswith("_d") or key.endswith("_h") or key.endswith("_c") or key == "message":
                slim[key] = value
        return slim

    def _get_search_engine(self):
        """
        获取向量检索器（优先使用注入的共享实例）

        优先级：
        1. 已缓存的检索器实例（可能来自构造函数参数或 execute() 中的注入缓存）
        2. 返回 None（走遍历搜索模式）
        """
        if self._search_engine is not None:
            return self._search_engine

        logger.debug("[resource_search] 向量检索器未注入，使用遍历搜索模式")
        return None

    def _get_agent_registry(self):
        """获取 Agent 注册表（使用全局单例，统一从 config/agents/ 加载）"""
        if self.agent_registry is None:
            try:
                from agents.global_registry import get_global_agent_registry_sync  # noqa: PLC0415

                self.agent_registry = get_global_agent_registry_sync()
            except Exception as e:
                logger.warning("Failed to load global agent registry: %s", e)
                self.agent_registry = None
        return self.agent_registry

    def _get_tool_registry(self):
        """获取 Tool 注册表（延迟加载）"""
        if self.tool_registry is None:
            from tools.global_registry import get_global_tool_registry_sync  # noqa: PLC0415

            self.tool_registry = get_global_tool_registry_sync()
        return self.tool_registry

    async def _search_agents(  # noqa: PLR0912
        self,
        query: str,
        category: str | None,
        level: str,
        limit: int,
        detailed: bool = False,
        exact: bool = False,
    ) -> tuple[list[str], list[str], list[str], list[dict]]:
        """搜索 Agent，返回名称、描述、config_id、详情。

        当 query 为空或通配符（"*"、"all"、"所有"等）时，直接返回所有 agent（受 limit 限制）。
        否则调用 _match_query 进行子串/分词匹配。
        """

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

        # 判断是否为通配符/空查询，如果是则直接匹配所有 agent
        wildcard_patterns = {"*", "all", "所有", "全部", "any"}
        is_wildcard = (not query_lower) or (query_lower.strip() in wildcard_patterns)

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

            # 通配符/空查询直接匹配，否则走 _match_query
            if is_wildcard:
                matched = True
            else:
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
                            "deliverables": getattr(agent_config, "deliverables", None) or [],
                            "recommended_metrics": getattr(agent_config, "recommended_metrics", None) or [],
                        }
                    )
                else:
                    details_list.append({})

                if len(names) >= limit:
                    break

        return names, descriptions, config_ids, details_list

    async def _search_tools(  # noqa: PLR0912,PLR0915
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
                found = self._match_tool_single(tool_registry, query_lower, category, level, exact=True)
                if found and found.name not in names:
                    names.append(found.name)
                    descriptions.append(found.description)
                    schemas_list.append({})

            if not names:
                names, descriptions, schemas_list = await self._search_tools_from_db(
                    query, category, level, limit, detailed, exact
                )

            # 兜底：DynamicToolLoader 已发现但未注册的工具
            if not names:
                for query_part in query_parts:
                    query_lower = query_part.lower()
                    result = self._search_tools_from_dynamic_loader(query_lower, 1, exact=True)
                    if result[0]:
                        names.extend(result[0])
                        descriptions.extend(result[1])
                        schemas_list.extend(result[2])

            # 兜底：从外部平台搜索（detailed 模式）
            if not names and query:
                ext_names, ext_descs, ext_schemas = await self._search_external(query, "tool", limit)
                if ext_names:
                    names = ext_names
                    descriptions = ext_descs
                    schemas_list = ext_schemas

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

        # 兜底：从 builtin_tools_config.yaml 搜索（覆盖未加载到内存的内置工具）
        if not names:
            names, descriptions, schemas_list = self._search_tools_from_yaml(query_lower, category, level, limit, exact)

        # 兜底：从 DynamicToolLoader 已发现的工具中搜索（覆盖已扫描但未注册的工具）
        if not names:
            names, descriptions, schemas_list = self._search_tools_from_dynamic_loader(query_lower, limit, exact)

        # 兜底：从外部平台搜索（仅在内部搜索结果不足时触发）
        if not names and query:
            ext_names, ext_descs, ext_schemas = await self._search_external(query, "tool", limit)
            if ext_names:
                names = ext_names
                descriptions = ext_descs
                schemas_list = ext_schemas

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
            from db.models import ToolLibrary  # noqa: PLC0415
        except ImportError:
            ToolLibrary = None  # noqa: N806

        if ToolLibrary is None:
            logger.debug("[resource_search] db.models.ToolLibrary 不可用，跳过数据库工具搜索")
            return [], [], []

        try:
            from sqlalchemy import select  # noqa: PLC0415

            from infrastructure.db import get_async_session  # noqa: PLC0415

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
                logger.info(f"[resource_search] 从数据库搜索到 {len(names)} 个工具（内存注册表为空）")

            return names, descriptions, schemas_list

        except Exception as e:
            logger.warning(f"[resource_search] 数据库搜索工具失败: {e}")
            return [], [], []

    def _search_tools_from_dynamic_loader(
        self,
        query_lower: str,
        limit: int,
        exact: bool,
    ) -> tuple[list[str], list[str], list[dict]]:
        """从 DynamicToolLoader 已发现的工具中搜索（覆盖已扫描但未注册到 Registry 的工具）"""
        try:
            from tools.loader import get_dynamic_tool_loader, init_dynamic_tool_loader  # noqa: PLC0415

            loader = get_dynamic_tool_loader()
            if not loader:
                tool_registry = self._get_tool_registry()
                if not tool_registry:
                    return [], [], []
                loader = init_dynamic_tool_loader(tool_registry)
                logger.info("[resource_search] 已自动初始化 DynamicToolLoader")

            discovered = loader.get_discovered_tools()
            if not discovered:
                return [], [], []

            names = []
            descriptions = []
            schemas_list = []

            for tool_name in discovered:
                if not query_lower:
                    desc = self._get_tool_description_from_code(tool_name, discovered[tool_name])
                    names.append(tool_name)
                    descriptions.append(desc)
                    schemas_list.append({})
                    if len(names) >= limit:
                        break
                    continue

                if exact:
                    if query_lower == tool_name.lower():
                        desc = self._get_tool_description_from_code(tool_name, discovered[tool_name])
                        names.append(tool_name)
                        descriptions.append(desc)
                        schemas_list.append({})
                        break
                elif query_lower in tool_name.lower():
                    desc = self._get_tool_description_from_code(tool_name, discovered[tool_name])
                    names.append(tool_name)
                    descriptions.append(desc)
                    schemas_list.append({})
                    if len(names) >= limit:
                        break

            if names:
                logger.info(f"[resource_search] 从 DynamicToolLoader 搜索到 {len(names)} 个工具")

            return names, descriptions, schemas_list

        except Exception as e:
            logger.debug(f"[resource_search] DynamicToolLoader 搜索失败: {e}")
            return [], [], []

    def _get_tool_description_from_code(
        self,
        tool_name: str,
        tool_info: tuple[str, str],
    ) -> str:
        """从工具代码中提取描述信息（缓存后复用）"""
        if tool_name in self._desc_cache:
            return self._desc_cache[tool_name]

        try:
            import importlib  # noqa: PLC0415

            module_path, class_name = tool_info
            module = importlib.import_module(module_path)
            tool_class = getattr(module, class_name, None)
            if tool_class and hasattr(tool_class, "get_tool_definition"):
                try:
                    tool_def = tool_class.get_tool_definition()
                    desc = tool_def.description or ""
                except Exception:
                    desc = f"内置工具 {tool_name}"
            else:
                desc = f"内置工具 {tool_name}"
        except Exception:
            desc = f"内置工具 {tool_name}"

        self._desc_cache[tool_name] = desc
        return desc

    def _search_tools_from_yaml(
        self,
        query_lower: str,
        category: str | None,
        level: str,
        limit: int,
        exact: bool,
    ) -> tuple[list[str], list[str], list[dict]]:
        """从 builtin_tools_config.yaml 搜索工具（兜底：覆盖未加载的内置工具）"""
        try:
            from pathlib import Path  # noqa: F401,PLC0415

            import yaml  # noqa: F401,PLC0415

            from config.config_center import get_config_center  # noqa: PLC0415

            config = get_config_center().get("tools/builtin_tools_config.yaml")
            if not config:
                return [], [], []

            if not config or "tools" not in config:
                return [], [], []

            names = []
            descriptions = []
            schemas_list = []

            for tool_info in config["tools"]:
                name = tool_info.get("name", "")
                desc = tool_info.get("description", "")
                tags = tool_info.get("tags", [])
                tool_category = tool_info.get("category")
                tool_level = tool_info.get("level", "user")

                if level not in ("all", tool_level):
                    continue

                if category and tool_category != category:
                    continue

                if self._match_query(query_lower, name, desc, tags, exact):
                    names.append(name)
                    descriptions.append(desc)
                    schemas_list.append({})

                    if len(names) >= limit:
                        break

            if names:
                logger.info(f"[resource_search] 从 YAML 配置搜索到 {len(names)} 个工具")

            return names, descriptions, schemas_list

        except Exception as e:
            logger.debug(f"[resource_search] YAML 配置搜索工具失败: {e}")
            return [], [], []

    def _match_query(  # noqa: PLR0911,PLR0912
        self,
        query_lower: str,
        name: str,
        description: str,
        tags: list[str],
        exact: bool = False,
    ) -> bool:
        """匹配查询关键词

        支持三种匹配模式：
        1. exact 模式：精确匹配 name（完全相等）
        2. 通配符模式：query 为 "*"、"all"、"所有" 等通配符时直接返回 True
        3. 分词匹配模式：将 query 按空格/逗号分词，任一关键词命中即算匹配

        匹配字段包括 name、description、tags，任一字段命中即返回 True。
        """

        # exact 模式保持原有行为不变
        if exact:
            if query_lower:
                return query_lower == name.lower()
            return True

        # 空查询视为匹配所有
        if not query_lower:
            return True

        # 通配符支持："*"、"all"、"所有"、"全部" 等直接返回 True
        wildcard_patterns = {"*", "all", "所有", "全部", "any"}
        if query_lower.strip() in wildcard_patterns:
            return True

        name_lower = name.lower()
        desc_lower = (description or "").lower()
        tags_lower = [tag.lower() for tag in tags]

        # 先尝试完整子串匹配（保持原有行为）
        if query_lower in name_lower:
            return True
        if query_lower in desc_lower:
            return True
        for tag in tags_lower:
            if query_lower in tag:
                return True

        # 分词匹配：将 query 按空格和逗号分词，任一关键词命中即算匹配
        keywords = [kw.strip() for kw in query_lower.replace(",", " ").split() if kw.strip()]
        for keyword in keywords:
            # 通配符关键词也直接匹配
            if keyword in wildcard_patterns:
                return True
            if keyword in name_lower:
                return True
            if keyword in desc_lower:
                return True
            for tag in tags_lower:
                if keyword in tag:
                    return True

        return False

    def _get_skill_registry(self):
        """获取 Skill 注册表（延迟加载，对接 skills.registry 模块）。"""
        if self.skill_registry is None:
            try:
                from skills.registry import get_global_skill_registry  # noqa: PLC0415

                self.skill_registry = get_global_skill_registry()
            except Exception as exc:
                logger.debug("[resource_search] SkillRegistry 加载失败: %s", exc)
                self.skill_registry = None
        return self.skill_registry

    def _get_external_search(self):
        """
        获取外部资源搜索器（延迟加载）

        优先使用构造函数注入的实例，否则尝试从配置创建。
        从 YAML 配置中读取平台列表，动态实例化对应的适配器。
        若配置未启用外部搜索则返回 None。
        """
        if self._external_search is not None:
            return self._external_search

        try:
            from pathlib import Path  # noqa: F401,PLC0415

            import yaml  # noqa: F401,PLC0415

            from config.config_center import get_config_center  # noqa: PLC0415

            config = get_config_center().get("tools/search/resource_search.yaml")
            if not config:
                return None

            ext_config = config.get("external_search", {})
            if not ext_config.get("enabled", False):
                return None

            from tools.builtin.external_resource_search import ExternalResourceSearch  # noqa: PLC0415
            from tools.builtin.platform_adapters import PLATFORM_ADAPTER_MAP  # noqa: PLC0415

            # 根据配置动态实例化平台适配器
            platform_adapters = self._build_platform_adapters(
                ext_config.get("platforms", []),
                PLATFORM_ADAPTER_MAP,
            )

            self._external_search = ExternalResourceSearch(
                cache_path=ext_config.get("cache_path", "data/external_tool_cache.json"),
                review_model=ext_config.get("review_model", "fast"),
                max_results=ext_config.get("max_results", 5),
                platforms=platform_adapters,
            )
            logger.info(
                "[resource_search] 外部资源搜索器已初始化，加载 %d 个平台适配器",
                len(platform_adapters),
            )
            return self._external_search

        except Exception as e:
            logger.debug("[resource_search] 外部资源搜索器初始化失败: %s", e)
            return None

    @staticmethod
    def _build_platform_adapters(
        platform_configs: list[dict[str, Any]],
        adapter_map: dict[str, type],
    ) -> list:
        """
        根据配置列表构建平台适配器实例

        Args:
            platform_configs: YAML 中 platforms 节的配置列表
            adapter_map: 平台名称到适配器类的映射字典

        Returns:
            已实例化的平台适配器列表
        """
        adapters = []
        for plat_conf in platform_configs:
            name = plat_conf.get("name", "")
            if not plat_conf.get("enabled", False):
                logger.debug("[resource_search] 跳过已禁用的平台: %s", name)
                continue

            adapter_cls = adapter_map.get(name)
            if adapter_cls is None:
                logger.warning(
                    "[resource_search] 未知的平台适配器: %s，可用列表: %s",
                    name,
                    list(adapter_map.keys()),
                )
                continue

            # 提取适配器特定参数（排除 name 和 enabled）
            kwargs = {k: v for k, v in plat_conf.items() if k not in ("name", "enabled") and v}
            try:
                adapter = adapter_cls(**kwargs)
                adapters.append(adapter)
                logger.debug("[resource_search] 平台适配器已加载: %s", name)
            except Exception as e:
                logger.warning(
                    "[resource_search] 平台适配器 %s 实例化失败: %s",
                    name,
                    e,
                )

        return adapters

    async def _search_external(
        self,
        query: str,
        resource_type: str,
        limit: int,
    ) -> tuple[list[str], list[str], list[dict]]:
        """
        搜索外部平台资源（仅在内部搜索结果不足时触发）

        Args:
            query: 搜索关键词
            resource_type: 资源类型（tool / skill）
            limit: 最大返回数量

        Returns:
            (名称列表, 描述列表, schema列表)
        """
        ext_search = self._get_external_search()
        if not ext_search or not query:
            return [], [], []

        try:
            results = await ext_search.search(query, resource_type, limit)
            if not results:
                return [], [], []

            names = []
            descriptions = []
            schemas_list = []

            for item in results:
                names.append(item.get("name", ""))
                descriptions.append(item.get("description", ""))
                schemas_list.append(
                    {
                        "external_schema": item.get("schema", {}),
                        "source": item.get("source", ""),
                        "trust_score": item.get("trust_score", 0.5),
                        "review_status": item.get("review_status", "unreviewed"),
                        "from_cache": item.get("from_cache", False),
                    }
                )

            logger.info(
                "[resource_search] 外部搜索返回 %d 个 %s 资源",
                len(names),
                resource_type,
            )
            return names, descriptions, schemas_list

        except Exception as e:
            logger.warning("[resource_search] 外部搜索失败，跳过: %s", e)
            return [], [], []

    async def _search_skills(  # noqa: PLR0912
        self,
        query: str,
        language: str | None,
        limit: int,
        detailed: bool = False,
        exact: bool = False,
    ) -> tuple[list[str], list[str], list[dict]]:
        """搜索本地 Skill。

        simple 模式：只返回名称和描述。
        detailed 模式：返回 SKILL.md 完整内容。
        （技能文件由 WorkspaceLifecycleManager 在任务启动时统一复制到工作空间。）
        """
        skill_registry = self._get_skill_registry()
        if not skill_registry or not skill_registry.is_initialized():
            return [], [], []

        names: list[str] = []
        descriptions: list[str] = []
        details_list: list[dict] = []
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

            if exact and query_lower != skill.skill_name.lower():
                continue

            names.append(skill.skill_name)
            descriptions.append(skill.description)

            if detailed:
                # 用 Skill 对象的懒加载属性读取完整内容
                details_list.append({"skill_content": skill.skill_content})
            else:
                details_list.append({})

            if len(names) >= limit:
                break

        return names, descriptions, details_list

    def _read_skill_markdown(self, skill_path: str) -> str:
        """读取 Skill 的 SKILL.md 文件内容"""
        try:
            from pathlib import Path  # noqa: PLC0415

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
        logger.info("[resource_search] 开始注入动态工具: tool_names=%s", tool_names)

        from tools.auto_loader import get_tool_auto_loader  # noqa: PLC0415

        auto_loader = get_tool_auto_loader()
        if not auto_loader:
            logger.warning("[resource_search] ToolAutoLoader 未初始化，无法注入动态工具")
            return

        # 获取与 tool_schema 同一个 registry 实例，确保 mark_dynamic 生效
        tool_registry = self._get_tool_registry()
        logger.info(
            "[resource_search] registry_id=%s auto_loader_registry_id=%s tool_names=%s",
            id(tool_registry) if tool_registry else None,
            id(auto_loader._registry) if auto_loader else None,
            tool_names,
        )

        for tool_name in tool_names:
            try:
                tool = await auto_loader.auto_load_tool(tool_name)
                if not tool:
                    logger.warning("[resource_search] 工具加载失败: %s", tool_name)
                    continue

                # 直接在 registry 上标记为动态工具，确保 tool_schema 下一轮可见
                if tool_registry and tool_name not in tool_registry.get_dynamic_tool_names():
                    tool_registry.mark_dynamic(tool_name)
                    logger.info(
                        "[resource_search] 已标记动态工具: %s (dynamic_tools=%s)",
                        tool_name,
                        tool_registry.get_dynamic_tool_names(),
                    )

                logger.info("[resource_search] 动态工具加载成功: %s", tool_name)
            except Exception as e:
                logger.error(
                    "[resource_search] 动态工具加载失败: %s, 错误: %s",
                    tool_name,
                    e,
                )
