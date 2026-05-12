"""
聊天服务

集成记忆增强功能，提供记忆增强的用户输入处理
"""

import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.builtin import load_agent
from src.agents.loop import AgentLoop
from src.core.constants import QueryLimits
from src.core.memory_session_manager import get_session_manager
from src.memory.types import ContextRequest, ContextType
from src.tools.executor import ToolExecutor
from src.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class ChatService:
    """
    聊天服务

    职责：
    - 集成记忆增强功能
    - 处理用户输入的记忆上下文注入
    - 提供上下文摘要功能
    """

    def __init__(self):
        """初始化聊天服务"""
        self.session_manager = get_session_manager()
        logger.info("ChatService 初始化完成")

    async def enhance_user_input_with_memory(
        self,
        user_input: str,
        user_id: str,
        session_id: str,
        max_context_tokens: int = 4000,
        required_memories: list[str] | None = None,
        optional_memories: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        使用记忆增强用户输入

        Args:
            user_input: 用户输入内容
            user_id: 用户ID
            session_id: 会话ID
            max_context_tokens: 最大上下文Token数
            required_memories: 必需的记忆类型
            optional_memories: 可选的记忆类型

        Returns:
            增强结果字典，包含：
            - enhanced: 是否进行了增强
            - context: 增强的上下文
            - token_usage: 使用的Token数量
            - relevant_memories: 相关记忆列表
        """
        start_time = time.time()

        try:
            # 获取记忆服务
            memory_service = await self.session_manager.get_memory_service(session_id)
            if not memory_service:
                logger.warning(f"无法获取记忆服务 - session_id: {session_id}")
                return {
                    "enhanced": False,
                    "context": None,
                    "token_usage": 0,
                    "relevant_memories": [],
                    "error": "记忆服务不可用",
                }

            # 构建上下文请求
            context_request = ContextRequest(
                required_memories=required_memories
                or [ContextType.USER_INTENT, ContextType.EXECUTION_HISTORY],
                optional_memories=optional_memories
                or [ContextType.DOMAIN_KNOWLEDGE, ContextType.TOOL_DESCRIPTIONS],
                max_context_tokens=max_context_tokens,
            )

            # 获取增强上下文
            context = await memory_service.get_context(
                user_id=user_id,
                request=context_request,
                user_intent=user_input,
            )

            # 检索相关记忆
            relevant_memories = []

            # 检索情景记忆
            episode_results = await memory_service.retrieve_episodes(
                user_id=user_id,
                query=user_input,
                limit=QueryLimits.CONTEXT_SAMPLE_SMALL,
            )
            relevant_memories.extend(
                [
                    {
                        "type": "episode",
                        "content": result.content,
                        "score": result.score,
                        "id": result.id,
                    }
                    for result in episode_results
                ]
            )

            # 检索知识记忆
            knowledge_results = await memory_service.retrieve_knowledge(
                user_id=user_id,
                query=user_input,
                limit=QueryLimits.CONTEXT_SAMPLE_MINIMAL,
            )
            relevant_memories.extend(
                [
                    {
                        "type": "knowledge",
                        "content": result.content,
                        "score": result.score,
                        "id": result.id,
                    }
                    for result in knowledge_results
                ]
            )

            retrieval_time = int((time.time() - start_time) * 1000)

            logger.info(
                f"记忆增强完成 - session_id: {session_id}, "
                f"token_usage: {context.total_tokens}, "
                f"memories_count: {len(relevant_memories)}, "
                f"time: {retrieval_time}ms"
            )

            return {
                "enhanced": True,
                "context": context,
                "token_usage": context.total_tokens,
                "relevant_memories": relevant_memories,
                "retrieval_time_ms": retrieval_time,
            }

        except Exception as e:
            logger.error(f"记忆增强失败 - session_id: {session_id}, error: {e}")
            return {
                "enhanced": False,
                "context": None,
                "token_usage": 0,
                "relevant_memories": [],
                "error": str(e),
            }

    def summarize_context(self, context: Any | None) -> str:
        """
        生成上下文摘要

        Args:
            context: 上下文对象

        Returns:
            上下文摘要字符串
        """
        if not context:
            return "无上下文信息"

        try:
            summary_parts = []

            # 用户意图
            if hasattr(context, "user_intent") and context.user_intent:
                summary_parts.append(f"意图: {context.user_intent[:100]}...")

            # 执行历史
            if hasattr(context, "execution_history") and context.execution_history:
                history_count = len(context.execution_history)
                summary_parts.append(f"历史: {history_count}条记录")

            # 领域知识
            if hasattr(context, "domain_knowledge") and context.domain_knowledge:
                knowledge_count = len(context.domain_knowledge)
                summary_parts.append(f"知识: {knowledge_count}条")

            # 工具描述
            if hasattr(context, "tool_descriptions") and context.tool_descriptions:
                tools_count = len(context.tool_descriptions)
                summary_parts.append(f"工具: {tools_count}个")

            # Token使用量
            if hasattr(context, "total_tokens"):
                summary_parts.append(f"Token: {context.total_tokens}")

            return " | ".join(summary_parts) if summary_parts else "空上下文"

        except Exception as e:
            logger.error(f"生成上下文摘要失败: {e}")
            return "摘要生成失败"

    async def search_memories(
        self,
        user_id: str,
        session_id: str,
        query: str,
        memory_types: list[str] | None = None,
        top_k: int = 10,
        min_score: float = 0.5,
    ) -> dict[str, Any]:
        """
        搜索记忆

        Args:
            user_id: 用户ID
            session_id: 会话ID
            query: 搜索查询
            memory_types: 记忆类型过滤
            top_k: 返回数量
            min_score: 最小相关性得分

        Returns:
            搜索结果
        """
        start_time = time.time()

        try:
            # 获取记忆服务
            memory_service = await self.session_manager.get_memory_service(session_id)
            if not memory_service:
                return {
                    "items": [],
                    "total": 0,
                    "query": query,
                    "error": "记忆服务不可用",
                }

            # 执行搜索
            results = await memory_service.search(
                user_id=user_id,
                query=query,
                memory_types=memory_types,
                top_k=top_k,
                min_score=min_score,
            )

            search_time = int((time.time() - start_time) * 1000)
            results["search_time_ms"] = search_time

            logger.info(
                f"记忆搜索完成 - user_id: {user_id}, "
                f"query: {query[:50]}..., "
                f"results: {results['total']}, "
                f"time: {search_time}ms"
            )

            return results

        except Exception as e:
            logger.error(f"记忆搜索失败 - user_id: {user_id}, error: {e}")
            return {"items": [], "total": 0, "query": query, "error": str(e)}

    async def create_episode_memory(
        self,
        user_id: str,
        session_id: str,
        intent_text: str,
        execution_summary: str | None = None,
        final_score: float | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        创建情景记忆

        Args:
            user_id: 用户ID
            session_id: 会话ID
            intent_text: 意图文本
            execution_summary: 执行摘要
            final_score: 最终得分
            tags: 标签列表

        Returns:
            创建结果
        """
        try:
            # 获取记忆服务
            memory_service = await self.session_manager.get_memory_service(session_id)
            if not memory_service:
                return {"success": False, "error": "记忆服务不可用"}

            # 创建情景记忆
            episode = await memory_service.create_episode(
                user_id=user_id,
                intent_text=intent_text,
                execution_summary=execution_summary,
                final_score=final_score,
                tags=tags or [],
            )

            logger.info(f"创建情景记忆成功 - episode_id: {episode['id']}")

            return {"success": True, "episode": episode}

        except Exception as e:
            logger.error(f"创建情景记忆失败 - user_id: {user_id}, error: {e}")
            return {"success": False, "error": str(e)}

    async def get_memory_stats(
        self,
        user_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        """
        获取记忆统计信息

        Args:
            user_id: 用户ID
            session_id: 会话ID

        Returns:
            统计信息
        """
        try:
            # 获取记忆服务
            memory_service = await self.session_manager.get_memory_service(session_id)
            if not memory_service:
                return {"error": "记忆服务不可用"}

            # 获取统计信息
            stats = await memory_service.get_stats(user_id=user_id)

            return stats

        except Exception as e:
            logger.error(f"获取记忆统计失败 - user_id: {user_id}, error: {e}")
            return {"error": str(e)}

    async def stream_response(
        self,
        db: AsyncSession,
        thread_id: str,
        user_input: str,
        user_id: str | None = None,
    ) -> AsyncIterator[str]:
        """
        流式生成 AI 响应

        Args:
            db: 数据库会话
            thread_id: 会话 ID
            user_input: 用户输入
            user_id: 用户 ID（可选）

        Yields:
            响应内容片段
        """
        try:
            # ========== 测试日志：确认新代码是否被执行 ==========
            logger.info(
                f"[stream_response] 方法被调用 | thread_id={thread_id} | user_input={user_input[:50]}"
            )
            # =====================================================

            # 1. 获取会话的 Agent 配置
            # 从数据库 session 表获取该会话使用的 agent_id
            from sqlalchemy import select

            from src.db.models import AgentConfig, Session

            # 查询会话绑定的 agent_id
            # 注意：Session.id 不是唯一字段，必须同时使用 user_id 过滤
            session_result = await db.execute(
                select(Session).where(
                    Session.id == thread_id,
                    Session.user_id == user_id,
                )
            )
            session = session_result.scalar_one_or_none()

            if session and session.agent_id:
                # 会话绑定了特定的 agent_id，从数据库加载
                agent_result = await db.execute(
                    select(AgentConfig).where(AgentConfig.id == session.agent_id)
                )
                db_agent = agent_result.scalar_one_or_none()

                if db_agent:
                    # 从数据库 AgentConfig 创建 Agent 配置对象
                    from src.agents.types import AgentConfig as AgentConfigType
                    from src.agents.types import AgentType

                    # 处理 agent_type（从字符串转为枚举）
                    agent_type_enum = AgentType.ATOMIC
                    if db_agent.agent_type:
                        agent_type_str = db_agent.agent_type.lower()
                        if agent_type_str == "main":
                            agent_type_enum = AgentType.MAIN
                        elif agent_type_str == "subagent":
                            agent_type_enum = AgentType.SUBAGENT
                        elif agent_type_str == "specialized":
                            agent_type_enum = AgentType.SPECIALIZED
                        elif agent_type_str == "atomic":
                            agent_type_enum = AgentType.ATOMIC
                        else:
                            try:
                                agent_type_enum = AgentType(agent_type_str)
                            except ValueError:
                                agent_type_enum = AgentType.ATOMIC

                    agent_config = AgentConfigType(
                        name=db_agent.name,
                        description=db_agent.description or "",
                        agent_type=agent_type_enum,
                        model_name=db_agent.model_name,
                        model_params=db_agent.model_params or {},
                        system_prompt=db_agent.system_prompt,
                        tool_ids=db_agent.tool_ids or [],
                        hard_constraints=db_agent.hard_constraints or [],
                        soft_constraints=db_agent.soft_constraints or [],
                        context_variables=db_agent.context_variables or {},
                        static_vars=db_agent.static_vars or {},
                        dynamic_vars=db_agent.dynamic_vars or {},
                        input_schema=db_agent.input_schema or {},
                        output_schema=db_agent.output_schema or {},
                    )
                    logger.info(
                        f"[stream_response] 使用数据库 Agent | "
                        f"agent_id={session.agent_id} | "
                        f"agent_name={db_agent.name} | "
                        f"model={db_agent.model_name}"
                    )
                else:
                    logger.warning(
                        f"[stream_response] 会话绑定的 Agent 不存在 | "
                        f"agent_id={session.agent_id}，使用默认 Agent"
                    )
                    # 回退到默认 Agent
                    from src.core.constants import DEFAULT_AGENT_NAME

                    agent_config = load_agent(DEFAULT_AGENT_NAME)
            else:
                # 会话未绑定 agent，使用默认 Agent
                from src.core.constants import DEFAULT_AGENT_NAME

                agent_config = load_agent(DEFAULT_AGENT_NAME)
                logger.info(
                    f"[stream_response] 会话未绑定 Agent，使用默认 Agent | "
                    f"agent_name={DEFAULT_AGENT_NAME}"
                )

            if not agent_config:
                logger.error("无法加载 Agent 配置")
                yield "[错误] 无法加载 Agent 配置"
                return

            logger.info(
                f"[stream_response] Agent 加载成功 | "
                f"agent_name={agent_config.name} | "
                f"model={agent_config.model_name}"
            )

            # 2. 创建工具注册表并注册工具
            from src.tools.builtin import register_all_builtin_tools

            tool_registry = ToolRegistry()

            # 注册所有内置工具
            registered_tools = register_all_builtin_tools(
                registry=tool_registry,
                session=db,  # 传递数据库会话
            )
            logger.info(
                f"[stream_response] 注册工具成功 | "
                f"count={len(registered_tools)} | "
                f"tools={registered_tools}"
            )

            tool_executor = ToolExecutor(registry=tool_registry)

            # 3. 创建 Agent Loop（集成记忆系统）
            agent_loop = AgentLoop(
                config=agent_config,
                tool_registry=tool_registry,
                tool_executor=tool_executor,
                user_id=user_id or "anonymous",
                session_id=thread_id,
                enable_learning=False,
                enable_monitoring=False,
                enable_checkpointing=False,
            )

            logger.info(
                f"[stream_response] AgentLoop 初始化完成 | thread_id={thread_id}"
            )

            # 4. 执行 Agent 并流式输出结果
            # 使用 messages 模式获取逐 token 输出
            try:
                token_count = 0
                async for event in agent_loop.stream(
                    user_input, stream_mode="messages"
                ):
                    # messages 模式返回 (Message, metadata) 元组
                    if isinstance(event, tuple) and len(event) >= 1:
                        message = event[0]  # 提取消息对象
                        event[1] if len(event) > 1 else {}

                        # 只处理 AIMessageChunk（流式片段），跳过最后的完整 AIMessage
                        if message.__class__.__name__ == "AIMessageChunk":
                            if hasattr(message, "content"):
                                content = message.content
                                if isinstance(content, str) and content:  # 确保内容非空
                                    # 逐 token yield
                                    token_count += 1
                                    logger.debug(
                                        f"[stream_response] Token #{token_count} | "
                                        f"content={repr(content[:20])}"
                                    )
                                    yield content
                                elif isinstance(content, list):
                                    # 处理多模态内容（图片等）
                                    for item in content:
                                        if hasattr(item, "text"):
                                            yield item.text
                    else:
                        # 处理其他类型的事件（不应该发生，但记录日志）
                        logger.debug(f"[stream_response] 非元组事件: {type(event)}")

                logger.info(
                    f"[stream_response] 流式输出完成 | "
                    f"thread_id={thread_id} | "
                    f"total_tokens={token_count}"
                )

            except Exception as stream_error:
                logger.exception(
                    f"[stream_response] 流式执行失败 | "
                    f"thread_id={thread_id} | "
                    f"error={stream_error}"
                )
                # 回退到非流式模式
                logger.warning("[stream_response] 回退到非流式模式")
                result = await agent_loop.run(user_input)
                if result.success:
                    yield result.output or ""
                else:
                    yield f"[错误] {result.error or '执行失败'}"

        except Exception as e:
            logger.exception(
                f"[stream_response] 流式响应失败 | thread_id={thread_id} | error={e}"
            )
            yield f"[错误] 处理失败: {str(e)}"


# 全局单例实例
_chat_service: ChatService | None = None


def get_chat_service() -> ChatService:
    """
    获取全局聊天服务实例

    Returns:
        ChatService实例
    """
    global _chat_service

    if _chat_service is None:
        _chat_service = ChatService()

    return _chat_service
