"""
记忆协调器 - 负责记忆相关的服务集成和管理

职责：
- 初始化和管理记忆检索服务
- 初始化和管理嵌入服务
- 初始化和管理分层上下文存储
- 初始化和管理知识注入服务
- 处理记忆相关的依赖注入
"""

import logging
from collections.abc import Callable
from typing import Any

from src.agents.interfaces import IEmbeddingService, IRetriever
from src.agents.types import AgentConfig

logger = logging.getLogger(__name__)


class MemoryCoordinator:
    """
    记忆协调器

    负责记忆相关服务的初始化和管理

    BUG-FIX-fix_20260226_session_leak: 修复会话泄漏问题
    问题根因: 手动调用 __aenter__ 而没有对应的 __aexit__，导致连接泄漏
    修复方案: 追踪上下文管理器，在 cleanup 中正确调用 __aexit__
    影响范围: 数据库连接池
    """

    def __init__(
        self,
        config: AgentConfig,
        user_id: str | None = None,
        session_id: str | None = None,
        db_session: Any | None = None,
        retriever: IRetriever | None = None,
        embedding_service: IEmbeddingService | None = None,
    ):
        self.config = config
        self.user_id = user_id
        self.session_id = session_id
        self._db_session = db_session
        self._session_context: Any | None = None
        self._owns_session: bool = db_session is None

        self._retriever = retriever
        self._embedding_service = embedding_service

        self._layered_context_store: Any | None = None
        self._context_builder: Any | None = None
        self._knowledge_injection: Any | None = None

    async def initialize(self) -> None:
        """初始化记忆相关组件"""
        await self._initialize_db_session()
        await self._initialize_retriever()
        await self._initialize_embedding_service()
        await self._initialize_layered_context_store()
        await self._initialize_context_builder()
        await self._initialize_knowledge_injection()

    async def _initialize_db_session(self) -> None:
        """
        初始化数据库会话（如果未注入）

        BUG-FIX-fix_20260226_session_leak: 使用上下文管理器追踪会话
        """
        if self._db_session is not None:
            return

        try:
            from src.db.session_manager import managed_session

            self._session_context = managed_session()
            self._db_session = await self._session_context.__aenter__()
            logger.debug("[MemoryCoordinator] 数据库会话已创建")
        except Exception as e:
            logger.warning(f"[MemoryCoordinator] 数据库会话初始化失败: {e}")

    async def _initialize_retriever(self) -> None:
        """初始化记忆检索器（如果未注入）"""
        if self._retriever is not None:
            return

        if self._db_session is None:
            return

        try:
            from src.memory.retriever import HybridRetriever

            self._retriever = HybridRetriever(self._db_session)
            logger.debug("[MemoryCoordinator] HybridRetriever 已创建")
        except Exception as e:
            logger.warning(f"[MemoryCoordinator] HybridRetriever 创建失败: {e}")

    async def _initialize_embedding_service(self) -> None:
        """初始化嵌入服务（如果未注入）"""
        if self._embedding_service is not None:
            return

        try:
            from src.core.embeddings import EmbeddingService

            self._embedding_service = EmbeddingService()
            logger.debug("[MemoryCoordinator] EmbeddingService 已创建")
        except Exception as e:
            logger.warning(f"[MemoryCoordinator] EmbeddingService 创建失败: {e}")

    async def _initialize_layered_context_store(self) -> None:
        """
        初始化分层上下文存储（默认创建）

        这是新架构的核心组件，用于统一的上下文管理
        """
        if self._layered_context_store is not None:
            return

        try:
            from src.core.di import get_global_container
            from src.memory.compressor.store import LayeredContextStore

            container = get_global_container()
            llm_factory = container.get("llm_factory")
            llm_client = llm_factory.get_client(self.config.model_name)

            self._layered_context_store = LayeredContextStore(
                llm_client=llm_client,
                embedding_service=self._embedding_service,
                session=self._db_session,
                user_id=self.user_id,
                session_id=self.session_id,
                model_alias=self.config.model_name,
                executor_type="agent",  # 执行者类型
                executor_id=(
                    self.config.name if hasattr(self.config, "name") else "default"
                ),  # Agent ID (使用 name 作为 executor_id)
                executor_name=(
                    self.config.name if hasattr(self.config, "name") else None
                ),  # Agent 名称
            )
            # 设置固定提示（系统提示词）
            self._layered_context_store.set_fixed_prompt(self.config.system_prompt)
            logger.info("[MemoryCoordinator] LayeredContextStore 已创建（默认启用）")
        except Exception as e:
            logger.error(
                f"[MemoryCoordinator] LayeredContextStore 创建失败: {e}", exc_info=True
            )
            raise RuntimeError(f"LayeredContextStore 创建失败: {e}") from e

    async def _initialize_context_builder(self) -> None:
        """初始化上下文构建器（配置驱动）"""
        if self._context_builder is not None:
            return

        try:
            from src.memory import ContextBuilder

            # 创建 ContextBuilder（不需要传递 context_window）
            self._context_builder = ContextBuilder()
            logger.debug("[MemoryCoordinator] ContextBuilder 已创建（配置驱动）")
        except Exception as e:
            logger.warning(f"[MemoryCoordinator] ContextBuilder 创建失败: {e}")

    async def _initialize_knowledge_injection(self) -> None:
        """初始化知识注入服务（如果配置了 knowledge）"""
        if self._knowledge_injection is not None:
            return

        if not self.config.knowledge:
            return

        if not self._embedding_service:
            logger.debug("[MemoryCoordinator] 未提供嵌入服务，跳过知识注入初始化")
            return

        try:
            from src.agents.knowledge_injection import KnowledgeInjectionService
            from src.memory.semantic_memory import SemanticMemory

            semantic_memory = SemanticMemory(self._db_session)
            self._knowledge_injection = KnowledgeInjectionService(
                semantic_memory=semantic_memory,
                config=self.config.knowledge,
            )
            logger.debug("[MemoryCoordinator] KnowledgeInjectionService 已创建")
        except Exception as e:
            logger.warning(
                f"[MemoryCoordinator] KnowledgeInjectionService 创建失败: {e}"
            )

    @property
    def retriever(self) -> IRetriever | None:
        """获取记忆检索器"""
        return self._retriever

    @property
    def embedding_service(self) -> IEmbeddingService | None:
        """获取嵌入服务"""
        return self._embedding_service

    @property
    def layered_context_store(self) -> Any | None:
        """获取分层上下文存储 (LayeredContextStore)"""
        return self._layered_context_store

    @property
    def context_builder(self) -> Any | None:
        """获取上下文构建器 (ContextBuilder)"""
        return self._context_builder

    @property
    def prompt_builder(self) -> Any | None:
        """获取提示构建器 (ContextBuilder 的别名)"""
        return self._context_builder

    @property
    def knowledge_injection(self) -> Any | None:
        """获取知识注入服务 (KnowledgeInjectionService)"""
        return self._knowledge_injection

    async def register_dynamic_tools(
        self,
        tool_registry: Any,
        evaluator_callback: Callable | None = None,
    ) -> dict[str, Any]:
        """
        注册动态工具

        Args:
            tool_registry: 工具注册表
            evaluator_callback: 评估器回调函数

        Returns:
            注册的工具字典
        """
        if not self._db_session:
            return {}

        try:
            from src.tools.builtin import register_all_builtin_tools

            registered_tools = await register_all_builtin_tools(
                registry=tool_registry,
                session=self._db_session,
                evaluator_callback=evaluator_callback,
            )

            if registered_tools:
                logger.info(
                    f"[MemoryCoordinator] 成功注册动态工具: {registered_tools}"
                )

            return {name: name for name in registered_tools}

        except Exception as e:
            logger.warning(f"[MemoryCoordinator] 动态工具注册失败: {e}")
            return {}

    async def cleanup(self) -> None:
        """
        清理记忆协调器资源

        BUG-FIX-fix_20260226_session_leak: 正确释放数据库连接
        """
        if self._session_context:
            try:
                await self._session_context.__aexit__(None, None, None)
            except Exception as e:
                logger.warning(f"[MemoryCoordinator] 关闭数据库会话时出错: {e}")
            finally:
                self._session_context = None

        # 确保 _db_session 被设置为 None
        self._db_session = None

        logger.debug("[MemoryCoordinator] 记忆协调器资源已清理")

    async def get_memory_stats(self) -> dict[str, Any]:
        """
        获取记忆统计信息

        Returns:
            记忆统计信息
        """
        if not self._db_session:
            return {"error": "数据库会话不可用"}

        try:
            from src.core.memory_session_manager import get_session_manager

            session_manager = get_session_manager()
            memory_service = await session_manager.get_memory_service(self.session_id)
            if not memory_service:
                return {"error": "记忆服务不可用"}

            return await memory_service.get_stats(self.user_id)

        except Exception as e:
            logger.error(f"获取记忆统计失败: {e}")
            return {"error": str(e)}

    async def search_memories(
        self,
        query: str,
        memory_types: list[str] | None = None,
        top_k: int = 10,
    ) -> dict[str, Any]:
        """
        搜索记忆

        Args:
            query: 搜索查询
            memory_types: 记忆类型过滤
            top_k: 返回数量

        Returns:
            搜索结果
        """
        if not self._db_session:
            return {
                "items": [],
                "total": 0,
                "query": query,
                "error": "数据库会话不可用",
            }

        try:
            from src.core.memory_session_manager import get_session_manager

            session_manager = get_session_manager()
            memory_service = await session_manager.get_memory_service(self.session_id)
            if not memory_service:
                return {
                    "items": [],
                    "total": 0,
                    "query": query,
                    "error": "记忆服务不可用",
                }

            return await memory_service.search(
                user_id=self.user_id,
                query=query,
                memory_types=memory_types,
                top_k=top_k,
            )

        except Exception as e:
            logger.error(f"搜索记忆失败: {e}")
            return {"items": [], "total": 0, "query": query, "error": str(e)}
