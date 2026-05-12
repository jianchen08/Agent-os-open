"""
记忆检索工具

提供记忆的存储和检索功能，支持 Tag 网络增强检索
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.results import ToolExecutionResult
from src.memory.service import MemoryService
from src.memory.types import ContextRequest, Episode, Knowledge
from src.tools.types import (
    Tool,
    ToolCategory,
    ToolLevel,
    ToolSource,
    create_failure_result,
    create_success_result,
)


class MemoryTool:
    """
    记忆检索工具

    提供：
    - 存储情景记忆
    - 存储知识
    - 检索记忆（支持 Tag 网络增强）
    - 获取上下文
    - 搜索相似 Tag
    - 获取 Tag 关联信息
    """

    def __init__(
        self,
        session: AsyncSession,
        user_id: str | None = None,
        tag_network: Any | None = None,
    ):
        """
        初始化记忆工具

        Args:
            session: 数据库会话
            user_id: 用户 ID
            tag_network: Tag 网络检索器（可选）
        """
        self.memory_service = MemoryService(session=session)
        self.user_id = user_id
        self.tag_network = tag_network
        self.session = session

    def set_tag_network(self, tag_network: Any):
        """设置 Tag 网络检索器"""
        self.tag_network = tag_network

    @staticmethod
    def get_tool_definition() -> Tool:
        """
        获取工具定义

        Returns:
            工具定义
        """
        return Tool(
            name="memory_retrieve",
            description="记忆检索工具：存储和检索记忆、知识，支持 Tag 网络增强检索。支持的操作：store_episode（存储任务执行记录）、store_knowledge（存储知识信息）、retrieve（检索相关记忆）、get_context（获取会话上下文）、search_tags（搜索相似 Tag）、get_related_tags（获取 Tag 关联信息）。限制：Tag 网络增强需要预先初始化 tag_network；检索结果数量受 limit 参数限制，默认10条；存储记忆时会自动生成向量嵌入；Tag 网络增强可能增加检索延迟。",
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "store_episode",
                            "store_knowledge",
                            "retrieve",
                            "get_context",
                            "search_tags",
                            "get_related_tags",
                        ],
                        "description": "操作类型：store_episode（存储任务执行记录）、store_knowledge（存储知识信息）、retrieve（检索相关记忆）、get_context（获取会话上下文）、search_tags（搜索相似 Tag）、get_related_tags（获取 Tag 关联信息）",
                    },
                    "intent": {
                        "type": "string",
                        "description": "意图/查询文本（可选），用于 retrieve、get_context、search_tags 操作，描述检索意图或查询内容",
                    },
                    "content": {
                        "type": "string",
                        "description": "内容（可选），用于 store_knowledge 操作，要存储的知识内容",
                    },
                    "memory_type": {
                        "type": "string",
                        "enum": ["episode", "semantic", "procedural"],
                        "description": "记忆类型（可选，默认为 episode），episode（情景记忆）、semantic（语义记忆）、procedural（程序性记忆）",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "检索结果数量限制（可选，默认 10），控制返回的最大结果数",
                        "default": 10,
                    },
                    "session_id": {
                        "type": "string",
                        "description": "会话 ID（可选），用于会话级记忆隔离和上下文获取",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "任务 ID（可选），用于任务级记忆隔离",
                    },
                    "metadata": {
                        "type": "object",
                        "description": "元数据（可选），存储时附加的额外信息，如 tags、source 等",
                    },
                    "use_tag_boost": {
                        "type": "boolean",
                        "description": "是否使用 Tag 网络增强检索（可选，默认 true），启用后可提高检索相关性",
                        "default": True,
                    },
                    "tag_boost": {
                        "type": "number",
                        "description": "Tag 增强因子（可选，默认 0.3，范围 0-1），越大越依赖 Tag 网络进行结果排序",
                        "default": 0.3,
                    },
                    "tag_name": {
                        "type": "string",
                        "description": "Tag 名称（可选，用于 get_related_tags 操作），指定要查询关联的 Tag 名称",
                    },
                },
                "required": ["action"],
            },
            source=ToolSource.CODE,
            category=ToolCategory.MEMORY,
            level=ToolLevel.SYSTEM,
            requires_approval=False,
            tags=["memory", "retrieval", "knowledge", "tag_network"],
            # 注入参数：这些参数由系统在运行时注入，不暴露给 LLM 决策
            injected_params=["session_id", "user_id", "task_id"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """
        执行工具

        Args:
            inputs: 输入参数

        Returns:
            执行结果
        """
        action = inputs.get("action")

        if action == "store_episode":
            return await self._store_episode(inputs)
        elif action == "store_knowledge":
            return await self._store_knowledge(inputs)
        elif action == "retrieve":
            return await self._retrieve(inputs)
        elif action == "get_context":
            return await self._get_context(inputs)
        elif action == "search_tags":
            return await self._search_tags(inputs)
        elif action == "get_related_tags":
            return await self._get_related_tags(inputs)
        else:
            return create_failure_result(
                error=f"不支持的操作: {action}",
                error_code="INVALID_ACTION",
            )

    async def _store_episode(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """
        存储情景记忆

        Args:
            inputs: 输入参数

        Returns:
            存储结果
        """
        try:
            intent = inputs.get("intent") or inputs.get("content")
            if not intent:
                return create_failure_result(
                    error="意图不能为空",
                    error_code="MISSING_INTENT",
                )

            # 创建情景记忆
            import uuid

            episode = Episode(
                id=uuid.uuid4(),
                user_id=uuid.UUID(self.user_id) if self.user_id else uuid.uuid4(),
                session_id=(
                    uuid.UUID(inputs["session_id"])
                    if inputs.get("session_id")
                    else None
                ),
                intent_text=intent,
                plan_dag=inputs.get("plan_dag"),
                execution_summary=inputs.get("summary"),
                evaluation_report=inputs.get("evaluation"),
                tags=inputs.get("tags", []),
            )

            # 存储到数据库
            memory_id = await self.memory_service.store_episode(episode)

            return create_success_result(
                data={
                    "memory_id": memory_id,
                    "type": "episode",
                },
                metadata={"action": "store_episode"},
            )

        except Exception as e:
            return create_failure_result(
                error=f"存储情景记忆失败: {str(e)}",
                error_code="STORE_FAILED",
            )

    async def _store_knowledge(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """
        存储知识

        Args:
            inputs: 输入参数

        Returns:
            存储结果
        """
        try:
            content = inputs.get("content")
            if not content:
                return create_failure_result(
                    error="知识内容不能为空",
                    error_code="MISSING_CONTENT",
                )

            # 创建知识
            import uuid

            knowledge = Knowledge(
                id=uuid.uuid4(),
                user_id=uuid.UUID(self.user_id) if self.user_id else uuid.uuid4(),
                source_type=inputs.get("source_type", "manual"),
                source_id=(
                    uuid.UUID(inputs["source_id"]) if inputs.get("source_id") else None
                ),
                content=content,
                extra_data=inputs.get("metadata", {}),
            )

            # 存储到数据库
            memory_id = await self.memory_service.store_knowledge(knowledge)

            return create_success_result(
                data={
                    "memory_id": memory_id,
                    "type": "knowledge",
                },
                metadata={"action": "store_knowledge"},
            )

        except Exception as e:
            return create_failure_result(
                error=f"存储知识失败: {str(e)}",
                error_code="STORE_FAILED",
            )

    async def _retrieve(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """
        检索记忆（支持 Tag 网络增强）

        Args:
            inputs: 输入参数

        Returns:
            检索结果
        """
        try:
            query = inputs.get("intent") or inputs.get("content")
            if not query:
                return create_failure_result(
                    error="查询内容不能为空",
                    error_code="MISSING_QUERY",
                )

            memory_type = inputs.get("memory_type", "episode")
            limit = inputs.get("limit", 10)
            session_id = inputs.get("session_id")
            task_id = inputs.get("task_id")
            use_tag_boost = inputs.get("use_tag_boost", True)
            tag_boost = inputs.get("tag_boost", 0.3)

            # Tag 网络增强信息
            tag_info = None

            # 如果启用 Tag 网络增强且有 tag_network
            if use_tag_boost and self.tag_network and tag_boost > 0:
                try:
                    # 获取查询向量
                    query_vector = await self.memory_service.get_embedding(query)
                    if query_vector:
                        import numpy as np

                        query_np = np.array(query_vector, dtype=np.float32)

                        # 应用 Tag 增强
                        boost_result = await self.tag_network.apply_tag_boost(
                            query_np, tag_boost
                        )

                        tag_info = {
                            "matched_tags": boost_result.matched_tags,
                            "spike_count": boost_result.spike_count,
                            "total_spike_score": boost_result.total_spike_score,
                        }

                        # 使用增强后的向量检索
                        results = await self.memory_service.retrieve_with_vector(
                            vector=(
                                boost_result.vector.tolist()
                                if hasattr(boost_result.vector, "tolist")
                                else list(boost_result.vector)
                            ),
                            memory_type=memory_type,
                            limit=limit,
                            user_id=self.user_id,
                            session_id=session_id,
                            task_id=task_id,
                        )
                except Exception as e:
                    # Tag 增强失败，回退到普通检索
                    tag_info = {"error": str(e)}
                    results = await self.memory_service.retrieve(
                        query=query,
                        memory_type=memory_type,
                        limit=limit,
                        user_id=self.user_id,
                        session_id=session_id,
                        task_id=task_id,
                    )
            else:
                # 普通检索
                results = await self.memory_service.retrieve(
                    query=query,
                    memory_type=memory_type,
                    limit=limit,
                    user_id=self.user_id,
                    session_id=session_id,
                    task_id=task_id,
                )

            # 转换为字典列表
            items = []
            for result in results:
                item = {
                    "id": str(result.id) if hasattr(result, "id") else None,
                    "score": getattr(result, "score", 0),
                    "memory_type": memory_type,
                }

                if memory_type == "episode":
                    item.update(
                        {
                            "intent": getattr(result, "intent_text", ""),
                            "summary": getattr(result, "execution_summary", ""),
                        }
                    )
                elif memory_type == "semantic":
                    item.update(
                        {
                            "content": getattr(result, "content", ""),
                        }
                    )

                items.append(item)

            result_data = {
                "query": query,
                "memory_type": memory_type,
                "results": items,
                "count": len(items),
                "tag_boost_enabled": use_tag_boost and self.tag_network is not None,
            }

            if tag_info:
                result_data["tag_info"] = tag_info

            return create_success_result(
                data=result_data,
                metadata={"action": "retrieve"},
            )

        except Exception as e:
            return create_failure_result(
                error=f"检索记忆失败: {str(e)}",
                error_code="RETRIEVE_FAILED",
            )

    async def _search_tags(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """
        搜索相似 Tag

        Args:
            inputs: 输入参数

        Returns:
            相似 Tag 列表
        """
        try:
            query = inputs.get("intent") or inputs.get("content")
            if not query:
                return create_failure_result(
                    error="查询内容不能为空",
                    error_code="MISSING_QUERY",
                )

            limit = inputs.get("limit", 10)

            if not self.tag_network:
                return create_failure_result(
                    error="Tag 网络未初始化",
                    error_code="TAG_NETWORK_NOT_INITIALIZED",
                )

            # 获取查询向量
            query_vector = await self.memory_service.get_embedding(query)
            if not query_vector:
                return create_failure_result(
                    error="无法生成查询向量",
                    error_code="EMBEDDING_FAILED",
                )

            import numpy as np

            query_np = np.array(query_vector, dtype=np.float32)

            # 搜索相似 Tag
            similar_tags = self.tag_network._search_similar_tags(query_np, k=limit)

            # 获取 Tag 名称
            tag_results = []
            for tag_id, score in similar_tags:
                tag_name = self.tag_network._tag_names.get(tag_id, f"tag_{tag_id}")
                tag_results.append(
                    {
                        "tag_id": tag_id,
                        "tag_name": tag_name,
                        "similarity": score,
                    }
                )

            return create_success_result(
                data={
                    "query": query,
                    "tags": tag_results,
                    "count": len(tag_results),
                },
                metadata={"action": "search_tags"},
            )

        except Exception as e:
            return create_failure_result(
                error=f"搜索 Tag 失败: {str(e)}",
                error_code="SEARCH_TAGS_FAILED",
            )

    async def _get_related_tags(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """
        获取 Tag 的关联 Tag（基于共现矩阵）

        Args:
            inputs: 输入参数

        Returns:
            关联 Tag 列表
        """
        try:
            tag_name = inputs.get("tag_name")
            if not tag_name:
                return create_failure_result(
                    error="Tag 名称不能为空",
                    error_code="MISSING_TAG_NAME",
                )

            limit = inputs.get("limit", 20)

            if not self.tag_network:
                return create_failure_result(
                    error="Tag 网络未初始化",
                    error_code="TAG_NETWORK_NOT_INITIALIZED",
                )

            # 查找 Tag ID
            tag_id = None
            for tid, name in self.tag_network._tag_names.items():
                if name == tag_name:
                    tag_id = tid
                    break

            if tag_id is None:
                return create_failure_result(
                    error=f"未找到 Tag: {tag_name}",
                    error_code="TAG_NOT_FOUND",
                )

            # 获取关联 Tag
            related = self.tag_network._cooccurrence.get_related_tags(tag_id)[:limit]

            # 转换为结果
            related_tags = []
            for related_id, weight in related:
                related_name = self.tag_network._tag_names.get(
                    related_id, f"tag_{related_id}"
                )
                freq = self.tag_network._cooccurrence.get_tag_frequency(related_id)
                related_tags.append(
                    {
                        "tag_id": related_id,
                        "tag_name": related_name,
                        "cooccurrence_weight": weight,
                        "global_frequency": freq,
                    }
                )

            return create_success_result(
                data={
                    "tag_name": tag_name,
                    "tag_id": tag_id,
                    "related_tags": related_tags,
                    "count": len(related_tags),
                },
                metadata={"action": "get_related_tags"},
            )

        except Exception as e:
            return create_failure_result(
                error=f"获取关联 Tag 失败: {str(e)}",
                error_code="GET_RELATED_TAGS_FAILED",
            )

    async def _get_context(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """
        获取上下文

        Args:
            inputs: 输入参数

        Returns:
            上下文信息
        """
        try:
            session_id = inputs.get("session_id")
            query = inputs.get("intent") or inputs.get("content", "")
            limit = inputs.get("limit", 10)

            # 构建上下文请求
            context_request = ContextRequest(
                session_id=session_id,
                user_id=self.user_id,
                query=query,
                max_results=limit,
            )

            # 获取上下文
            context = await self.memory_service.get_context(context_request)

            # 转换为字典
            context_dict = {
                "session_id": context.session_id,
                "user_id": context.user_id,
                "query": context.query,
                "episodes": [],
                "knowledge": [],
                "tools": [],
            }

            for episode in context.episodes:
                context_dict["episodes"].append(
                    {
                        "id": episode.id,
                        "intent": episode.intent_text,
                        "summary": episode.execution_summary,
                        "score": episode.score,
                    }
                )

            for knowledge in context.knowledge:
                context_dict["knowledge"].append(
                    {
                        "id": knowledge.id,
                        "content": knowledge.content,
                        "type": knowledge.knowledge_type,
                        "score": knowledge.score,
                    }
                )

            for tool_info in context.tools:
                context_dict["tools"].append(
                    {
                        "tool_name": tool_info.tool_name,
                        "usage_count": tool_info.usage_count,
                        "success_rate": tool_info.success_rate,
                    }
                )

            return create_success_result(
                data=context_dict,
                metadata={"action": "get_context"},
            )

        except Exception as e:
            return create_failure_result(
                error=f"获取上下文失败: {str(e)}",
                error_code="GET_CONTEXT_FAILED",
            )
