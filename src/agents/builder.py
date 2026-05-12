"""
AgentLoop 构建器 - 使用 Builder 模式构建 AgentLoop 实例

职责：
- 提供流畅的接口来配置 AgentLoop
- 处理复杂的依赖组装逻辑
- 支持默认值和可选配置
- 简化 AgentLoop 的创建过程

设计模式：
- Builder Pattern: 分步骤构建复杂对象
- Fluent Interface: 提供链式调用 API
"""

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.context import AgentContext
from src.agents.interfaces import (
    ICheckpointer,
    IEmbeddingService,
    IRetriever,
    ITaskProgressManager,
    IUsageMonitor,
)
from src.agents.loop import AgentLoop
from src.agents.types import AgentConfig
from src.tools.executor import ToolExecutor
from src.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class AgentLoopBuilder:
    """
    AgentLoop 构建器

    提供流畅的接口来配置和构建 AgentLoop 实例。
    使用 Builder 模式简化复杂对象的创建。
    """

    def __init__(
        self,
        config: AgentConfig,
        tool_registry: ToolRegistry,
        tool_executor: ToolExecutor,
    ):
        """
        初始化构建器

        Args:
            config: Agent 配置
            tool_registry: 工具注册表
            tool_executor: 工具执行器
        """
        self._config = config
        self._tool_registry = tool_registry
        self._tool_executor = tool_executor

        # 默认值
        self._user_id: str | None = None
        self._session_id: str | None = None

        # 可选依赖
        self._db_session: AsyncSession | None = None
        self._retriever: IRetriever | None = None
        self._embedding_service: IEmbeddingService | None = None
        self._usage_monitor: IUsageMonitor | None = None
        self._task_progress_manager: ITaskProgressManager | None = None
        self._checkpointer: ICheckpointer | None = None

        # 功能开关
        self._enable_learning = True
        self._enable_monitoring = True
        self._enable_checkpointing = True
        self._enable_approval = False
        self._quota_config: Any | None = None

        # 额外上下文
        self._extra_context: dict[str, Any] = {}

    # ===== 基础配置方法 =====

    def with_user(self, user_id: str) -> "AgentLoopBuilder":
        """
        设置用户 ID

        Args:
            user_id: 用户 ID

        Returns:
            构建器实例（支持链式调用）
        """
        self._user_id = user_id
        return self

    def with_session(self, session_id: str) -> "AgentLoopBuilder":
        """
        设置会话 ID

        Args:
            session_id: 会话 ID（必须使用 thread-{user_id_short}-{session_seq} 格式）

        Returns:
            构建器实例（支持链式调用）

        Raises:
            ValueError: 如果未提供 session_id
        """
        if not session_id:
            raise ValueError("必须提供 session_id，请使用 thread-{user_id_short}-{session_seq} 格式")
        self._session_id = session_id
        return self

    def with_extra_context(self, **kwargs) -> "AgentLoopBuilder":
        """
        设置额外上下文

        Args:
            **kwargs: 额外的上下文键值对

        Returns:
            构建器实例（支持链式调用）
        """
        self._extra_context.update(kwargs)
        return self

    # ===== 依赖注入方法 =====

    def with_database(self, db_session: AsyncSession) -> "AgentLoopBuilder":
        """
        注入数据库会话

        Args:
            db_session: 数据库会话

        Returns:
            构建器实例（支持链式调用）
        """
        self._db_session = db_session
        return self

    def with_retriever(self, retriever: IRetriever) -> "AgentLoopBuilder":
        """
        注入记忆检索器

        Args:
            retriever: 记忆检索器

        Returns:
            构建器实例（支持链式调用）
        """
        self._retriever = retriever
        return self

    def with_embedding_service(
        self, embedding_service: IEmbeddingService
    ) -> "AgentLoopBuilder":
        """
        注入嵌入服务

        Args:
            embedding_service: 嵌入服务

        Returns:
            构建器实例（支持链式调用）
        """
        self._embedding_service = embedding_service
        return self

    def with_usage_monitor(self, usage_monitor: IUsageMonitor) -> "AgentLoopBuilder":
        """
        注入用量监控器

        Args:
            usage_monitor: 用量监控器

        Returns:
            构建器实例（支持链式调用）
        """
        self._usage_monitor = usage_monitor
        return self

    def with_task_progress_manager(
        self, task_progress_manager: ITaskProgressManager
    ) -> "AgentLoopBuilder":
        """
        注入任务进度管理器

        Args:
            task_progress_manager: 任务进度管理器

        Returns:
            构建器实例（支持链式调用）
        """
        self._task_progress_manager = task_progress_manager
        return self

    def with_checkpointer(self, checkpointer: ICheckpointer) -> "AgentLoopBuilder":
        """
        注入检查点管理器

        Args:
            checkpointer: 检查点管理器

        Returns:
            构建器实例（支持链式调用）
        """
        self._checkpointer = checkpointer
        return self

    def with_quota_config(self, quota_config: Any) -> "AgentLoopBuilder":
        """
        设置配额配置

        Args:
            quota_config: 配额配置

        Returns:
            构建器实例（支持链式调用）
        """
        self._quota_config = quota_config
        return self

    # ===== 功能开关方法 =====

    def enable_learning(self, enable: bool = True) -> "AgentLoopBuilder":
        """
        启用/禁用学习功能

        Args:
            enable: 是否启用

        Returns:
            构建器实例（支持链式调用）
        """
        self._enable_learning = enable
        return self

    def enable_monitoring(self, enable: bool = True) -> "AgentLoopBuilder":
        """
        启用/禁用监控功能

        Args:
            enable: 是否启用

        Returns:
            构建器实例（支持链式调用）
        """
        self._enable_monitoring = enable
        return self

    def enable_checkpointing(self, enable: bool = True) -> "AgentLoopBuilder":
        """
        启用/禁用检查点功能

        Args:
            enable: 是否启用

        Returns:
            构建器实例（支持链式调用）
        """
        self._enable_checkpointing = enable
        return self

    def enable_approval(self, enable: bool = True) -> "AgentLoopBuilder":
        """
        启用/禁用人工审批功能

        Args:
            enable: 是否启用

        Returns:
            构建器实例（支持链式调用）
        """
        self._enable_approval = enable
        return self

    # ===== 预设配置方法 =====

    def minimal(self) -> "AgentLoopBuilder":
        """
        最小化配置（用于测试）

        禁用所有增强功能，只保留核心执行能力。

        Returns:
            构建器实例（支持链式调用）
        """
        self._enable_learning = False
        self._enable_monitoring = False
        self._enable_checkpointing = False
        self._enable_approval = False
        return self

    def full_featured(self) -> "AgentLoopBuilder":
        """
        完整功能配置

        启用所有增强功能。

        Returns:
            构建器实例（支持链式调用）
        """
        self._enable_learning = True
        self._enable_monitoring = True
        self._enable_checkpointing = True
        self._enable_approval = False
        return self

    def production(self) -> "AgentLoopBuilder":
        """
        生产环境配置

        启用监控和检查点，但禁用人工审批。

        Returns:
            构建器实例（支持链式调用）
        """
        self._enable_learning = True
        self._enable_monitoring = True
        self._enable_checkpointing = True
        self._enable_approval = False
        return self

    def development(self) -> "AgentLoopBuilder":
        """
        开发环境配置

        启用所有功能，包括人工审批。

        Returns:
            构建器实例（支持链式调用）
        """
        self._enable_learning = True
        self._enable_monitoring = True
        self._enable_checkpointing = True
        self._enable_approval = True
        return self

    # ===== 构建方法 =====

    def build(self) -> AgentLoop:
        """
        构建 AgentLoop 实例

        Returns:
            配置好的 AgentLoop 实例
        """
        # 验证会话 ID 已设置
        if self._session_id is None:
            raise ValueError("必须提供 session_id，请使用 with_session() 方法设置")

        # 创建上下文对象
        context = AgentContext(
            config=self._config,
            tool_registry=self._tool_registry,
            tool_executor=self._tool_executor,
            user_id=self._user_id,
            session_id=self._session_id,
            db_session=self._db_session,
            retriever=self._retriever,
            embedding_service=self._embedding_service,
            usage_monitor=self._usage_monitor,
            task_progress_manager=self._task_progress_manager,
            checkpointer=self._checkpointer,
            enable_learning=self._enable_learning,
            enable_monitoring=self._enable_monitoring,
            enable_checkpointing=self._enable_checkpointing,
            enable_approval=self._enable_approval,
            quota_config=self._quota_config,
            extra_context=self._extra_context,
        )

        # 验证配置
        warnings = context.validate()
        if warnings:
            logger.warning(f"[AgentLoopBuilder] 构建警告: {', '.join(warnings)}")

        # 创建 AgentLoop
        agent_loop = AgentLoop(context=context)

        logger.debug(
            f"[AgentLoopBuilder] 构建完成 | "
            f"session_id={self._session_id} | "
            f"features={context.get_dependency_summary()['features']}"
        )

        return agent_loop

    def build_async(self) -> AgentLoop:
        """
        异步构建 AgentLoop 实例（与 build 相同，保留以保持 API 一致性）

        Returns:
            配置好的 AgentLoop 实例
        """
        return self.build()


# ===== 工厂函数（向后兼容）=====


async def create_agent_loop(
    config: AgentConfig,
    tool_registry: ToolRegistry,
    tool_executor: ToolExecutor,
    user_id: str | None = None,
    session_id: str | None = None,
    # 可选依赖注入
    db_session: AsyncSession | None = None,
    retriever: IRetriever | None = None,
    embedding_service: IEmbeddingService | None = None,
    usage_monitor: IUsageMonitor | None = None,
    task_progress_manager: ITaskProgressManager | None = None,
    checkpointer: ICheckpointer | None = None,
    # 功能开关
    enable_learning: bool = True,
    enable_monitoring: bool = True,
    enable_checkpointing: bool = True,
    enable_approval: bool = False,
    quota_config: Any | None = None,
    # 额外上下文
    extra_context: dict[str, Any] | None = None,
) -> AgentLoop:
    """
    创建 AgentLoop 实例（工厂函数，向后兼容）

    Args:
        config: Agent 配置
        tool_registry: 工具注册表
        tool_executor: 工具执行器
        user_id: 用户 ID
        session_id: 会话 ID
        db_session: 数据库会话（可选，注入）
        retriever: 记忆检索器（可选，注入）
        embedding_service: 嵌入服务（可选，注入）
        usage_monitor: 用量监控（可选，注入）
        task_progress_manager: 任务进度管理器（可选，注入）
        checkpointer: 检查点管理器（可选，注入）
        enable_learning: 是否启用学习功能
        enable_monitoring: 是否启用监控
        enable_checkpointing: 是否启用检查点
        enable_approval: 是否启用人工审批
        quota_config: 配额配置
        extra_context: 额外上下文

    Returns:
        配置好的 AgentLoop 实例
    """
    builder = AgentLoopBuilder(config, tool_registry, tool_executor)

    # 应用配置
    if user_id:
        builder.with_user(user_id)
    if session_id:
        builder.with_session(session_id)

    # 应用依赖注入
    if db_session:
        builder.with_database(db_session)
    if retriever:
        builder.with_retriever(retriever)
    if embedding_service:
        builder.with_embedding_service(embedding_service)
    if usage_monitor:
        builder.with_usage_monitor(usage_monitor)
    if task_progress_manager:
        builder.with_task_progress_manager(task_progress_manager)
    if checkpointer:
        builder.with_checkpointer(checkpointer)
    if quota_config:
        builder.with_quota_config(quota_config)

    # 应用功能开关
    if not enable_learning:
        builder.enable_learning(False)
    if not enable_monitoring:
        builder.enable_monitoring(False)
    if not enable_checkpointing:
        builder.enable_checkpointing(False)
    if enable_approval:
        builder.enable_approval(True)

    # 应用额外上下文
    if extra_context:
        builder.with_extra_context(**extra_context)

    return builder.build()


def create_agent_loop_minimal(
    config: AgentConfig,
    tool_registry: ToolRegistry,
    tool_executor: ToolExecutor,
    user_id: str | None = None,
    session_id: str | None = None,
) -> AgentLoop:
    """
    创建最小化 AgentLoop（用于测试）

    禁用所有增强功能，只保留核心执行能力。

    Args:
        config: Agent 配置
        tool_registry: 工具注册表
        tool_executor: 工具执行器
        user_id: 用户 ID
        session_id: 会话 ID

    Returns:
        最小化配置的 AgentLoop 实例
    """
    return (
        AgentLoopBuilder(config, tool_registry, tool_executor)
        .with_user(user_id or "")
        .with_session(session_id)
        .minimal()
        .build()
    )


async def create_agent_loop_with_defaults(
    config: AgentConfig,
    tool_registry: ToolRegistry,
    tool_executor: ToolExecutor,
    user_id: str | None = None,
    session_id: str | None = None,
) -> AgentLoop:
    """
    使用默认配置创建 AgentLoop

    简化版工厂函数，自动创建所有默认依赖。

    Args:
        config: Agent 配置
        tool_registry: 工具注册表
        tool_executor: 工具执行器
        user_id: 用户 ID
        session_id: 会话 ID

    Returns:
        配置好的 AgentLoop 实例
    """
    return (
        AgentLoopBuilder(config, tool_registry, tool_executor)
        .with_user(user_id or "")
        .with_session(session_id)
        .full_featured()
        .build()
    )


def _get_injected_deps(agent_loop: AgentLoop) -> list:
    """获取已注入的依赖列表"""
    deps = []
    if agent_loop._db_session is not None:
        deps.append("db_session")
    if agent_loop._retriever is not None:
        deps.append("retriever")
    if agent_loop._embedding_service is not None:
        deps.append("embedding_service")
    if agent_loop._usage_monitor is not None:
        deps.append("usage_monitor")
    if agent_loop._task_progress_manager is not None:
        deps.append("task_progress_manager")
    return deps
