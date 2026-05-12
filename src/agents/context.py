"""
Agent 上下文 - 依赖注入上下文对象

职责：
- 封装 AgentLoop 所需的所有依赖
- 提供统一的依赖访问接口
- 支持依赖的懒加载和可选注入
- 简化 AgentLoop 的构造函数

设计模式：
- Context Object Pattern: 将多个依赖封装为单个上下文对象
- Facade Pattern: 为复杂的依赖关系提供简化的接口
"""

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.coordinators import (
    IsolationConfig,
    IsolationCoordinator,
    LLMCoordinator,
    MemoryCoordinator,
    MonitoringCoordinator,
    ToolCoordinator,
)
from src.agents.interfaces import (
    ICheckpointer,
    IEmbeddingService,
    IRetriever,
    ITaskProgressManager,
    IUsageMonitor,
)
from src.agents.lifecycle import LifecycleManager, StateManager
from src.agents.types import AgentConfig
from src.tools.executor import ToolExecutor
from src.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class AgentContext:
    """
    Agent 上下文对象

    封装 AgentLoop 所需的所有依赖，提供统一的访问接口。
    使用 Context Object Pattern 减少构造函数参数数量。
    """

    def __init__(
        self,
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
        enable_isolation: bool = False,  # 隔离功能默认关闭
        quota_config: Any | None = None,
        # 额外上下文
        extra_context: dict[str, Any] | None = None,
    ):
        """
        初始化 Agent 上下文

        Args:
            config: Agent 配置
            tool_registry: 工具注册表
            tool_executor: 工具执行器
            user_id: 用户 ID
            session_id: 会话 ID
            db_session: 数据库会话（可选，注入）
            retriever: 记忆检索器（可选，注入）
            embedding_service: 嵌入服务（可选，注入）
            usage_monitor: 用量监控器（可选，注入）
            task_progress_manager: 任务进度管理器（可选，注入）
            checkpointer: 检查点管理器（可选，注入）
            enable_learning: 是否启用学习功能
            enable_monitoring: 是否启用监控
            enable_checkpointing: 是否启用检查点
            enable_approval: 是否启用人工审批
            enable_isolation: 是否启用工具隔离
            quota_config: 配额配置
            extra_context: 额外上下文（如 task_id）
        """
        # 基础配置
        self.config = config
        self.tool_registry = tool_registry
        self.tool_executor = tool_executor
        self.user_id = user_id
        self.session_id = session_id
        self.extra_context = extra_context or {}

        # 功能开关
        self.enable_learning = enable_learning
        self.enable_monitoring = enable_monitoring
        self.enable_checkpointing = enable_checkpointing
        self.enable_approval = enable_approval
        self.enable_isolation = enable_isolation

        # 可选依赖（注入的或自动创建的）
        self._db_session = db_session
        self._retriever = retriever
        self._embedding_service = embedding_service
        self._usage_monitor = usage_monitor
        self._task_progress_manager = task_progress_manager
        self._checkpointer = checkpointer
        self._quota_config = quota_config

        # 协调器（懒加载）
        self._llm_coordinator: LLMCoordinator | None = None
        self._tool_coordinator: ToolCoordinator | None = None
        self._memory_coordinator: MemoryCoordinator | None = None
        self._monitoring_coordinator: MonitoringCoordinator | None = None
        self._isolation_coordinator: IsolationCoordinator | None = None
        self._lifecycle_manager: LifecycleManager | None = None
        self._state_manager: StateManager | None = None

        # 新架构组件（通过协调器访问）
        self._layered_context_store: Any | None = None
        self._prompt_builder: Any | None = None
        self._knowledge_injection: Any | None = None

    # ===== 协调器访问器 =====

    @property
    def llm_coordinator(self) -> LLMCoordinator:
        """获取 LLM 协调器（懒加载）"""
        if self._llm_coordinator is None:
            self._llm_coordinator = LLMCoordinator(self.config)
        return self._llm_coordinator

    @property
    def tool_coordinator(self) -> ToolCoordinator:
        """获取工具协调器（懒加载）"""
        if self._tool_coordinator is None:
            self._tool_coordinator = ToolCoordinator(
                tool_ids=self.config.tool_ids or [],
                tool_registry=self.tool_registry,
                tool_executor=self.tool_executor,
                session_id=self.session_id,
                user_id=self.user_id,
            )
        return self._tool_coordinator

    @property
    def memory_coordinator(self) -> MemoryCoordinator:
        """获取记忆协调器（懒加载）"""
        if self._memory_coordinator is None:
            self._memory_coordinator = MemoryCoordinator(
                config=self.config,
                user_id=self.user_id,
                session_id=self.session_id,
                db_session=self._db_session,
                retriever=self._retriever,
                embedding_service=self._embedding_service,
            )
        return self._memory_coordinator

    @property
    def monitoring_coordinator(self) -> MonitoringCoordinator:
        """获取监控协调器（懒加载）"""
        if self._monitoring_coordinator is None:
            self._monitoring_coordinator = MonitoringCoordinator(
                session_id=self.session_id,
                user_id=self.user_id,
                quota_config=self._quota_config,
                usage_monitor=self._usage_monitor,
                task_progress_manager=self._task_progress_manager,
                alert_callback=None,  # 由 AgentLoop 设置
            )
        return self._monitoring_coordinator

    @property
    def lifecycle_manager(self) -> LifecycleManager:
        """获取生命周期管理器（懒加载）"""
        if self._lifecycle_manager is None:
            self._lifecycle_manager = LifecycleManager(
                user_id=self.user_id,
                session_id=self.session_id,
                db_session=self._db_session,
                embedding_service=self._embedding_service,
                enable_learning=self.enable_learning,
            )
        return self._lifecycle_manager

    @property
    def state_manager(self) -> StateManager:
        """获取状态管理器（懒加载）"""
        if self._state_manager is None:
            self._state_manager = StateManager()
        return self._state_manager

    @property
    def isolation_coordinator(self) -> IsolationCoordinator:
        """获取隔离协调器（懒加载）"""
        if self._isolation_coordinator is None:
            # 加载隔离配置
            config = IsolationConfig(enabled=self.enable_isolation)
            self._isolation_coordinator = IsolationCoordinator(config=config)
        return self._isolation_coordinator

    # ===== 依赖访问器 =====

    @property
    def db_session(self) -> AsyncSession | None:
        """获取数据库会话"""
        return self._db_session

    @property
    def retriever(self) -> IRetriever | None:
        """获取记忆检索器"""
        return self._retriever

    @property
    def embedding_service(self) -> IEmbeddingService | None:
        """获取嵌入服务"""
        return self._embedding_service

    @property
    def usage_monitor(self) -> IUsageMonitor | None:
        """获取用量监控器"""
        return self._usage_monitor

    @property
    def task_progress_manager(self) -> ITaskProgressManager | None:
        """获取任务进度管理器"""
        return self._task_progress_manager

    @property
    def checkpointer(self) -> ICheckpointer | None:
        """获取检查点管理器"""
        return self._checkpointer

    # ===== 新架构组件访问器 =====

    @property
    def layered_context_store(self) -> Any | None:
        """获取分层上下文存储"""
        return self._layered_context_store

    @layered_context_store.setter
    def layered_context_store(self, value: Any) -> None:
        """设置分层上下文存储"""
        self._layered_context_store = value

    @property
    def prompt_builder(self) -> Any | None:
        """获取提示构建器"""
        return self._prompt_builder

    @prompt_builder.setter
    def prompt_builder(self, value: Any) -> None:
        """设置提示构建器"""
        self._prompt_builder = value

    @property
    def knowledge_injection(self) -> Any | None:
        """获取知识注入服务"""
        return self._knowledge_injection

    @knowledge_injection.setter
    def knowledge_injection(self, value: Any) -> None:
        """设置知识注入服务"""
        self._knowledge_injection = value

    # ===== 更新方法 =====

    def update_from_coordinators(self) -> None:
        """
        从协调器更新依赖引用

        在协调器初始化后调用，以获取其创建的组件
        """
        # 从记忆协调器更新
        if self._memory_coordinator is not None:
            if self._memory_coordinator.retriever:
                self._retriever = self._memory_coordinator.retriever
            if self._memory_coordinator.embedding_service:
                self._embedding_service = self._memory_coordinator.embedding_service
            # 更新新架构组件
            self._layered_context_store = self._memory_coordinator.layered_context_store
            self._prompt_builder = self._memory_coordinator.prompt_builder
            self._knowledge_injection = self._memory_coordinator.knowledge_injection

        # 从监控协调器更新
        if self._monitoring_coordinator is not None:
            if self._monitoring_coordinator.usage_monitor:
                self._usage_monitor = self._monitoring_coordinator.usage_monitor
            if self._monitoring_coordinator.task_progress_manager:
                self._task_progress_manager = (
                    self._monitoring_coordinator.task_progress_manager
                )

    # ===== 验证方法 =====

    def validate(self) -> list[str]:
        """
        验证上下文配置

        Returns:
            警告消息列表（空列表表示无警告）
        """
        warnings = []

        # 检查必需的依赖
        if not self.tool_registry:
            warnings.append("tool_registry 未设置")

        if not self.tool_executor:
            warnings.append("tool_executor 未设置")

        # 检查功能开关与依赖的一致性
        if self.enable_learning and not self._db_session:
            warnings.append("enable_learning=True 但未提供 db_session")

        if self.enable_monitoring and not self._usage_monitor:
            warnings.append("enable_monitoring=True 但未提供 usage_monitor")

        if self.enable_checkpointing and not self._checkpointer:
            warnings.append("enable_checkpointing=True 但未提供 checkpointer")

        return warnings

    # ===== 工具方法 =====

    def has_feature(self, feature: str) -> bool:
        """
        检查是否具有某个功能

        Args:
            feature: 功能名称（learning, monitoring, checkpointing, approval, isolation）

        Returns:
            是否启用该功能
        """
        feature_map = {
            "learning": self.enable_learning,
            "monitoring": self.enable_monitoring,
            "checkpointing": self.enable_checkpointing,
            "approval": self.enable_approval,
            "isolation": self.enable_isolation,
        }
        return feature_map.get(feature, False)

    def get_dependency_summary(self) -> dict[str, Any]:
        """
        获取依赖摘要

        Returns:
            依赖摘要字典
        """
        return {
            "has_db_session": self._db_session is not None,
            "has_retriever": self._retriever is not None,
            "has_embedding_service": self._embedding_service is not None,
            "has_usage_monitor": self._usage_monitor is not None,
            "has_task_progress_manager": self._task_progress_manager is not None,
            "has_checkpointer": self._checkpointer is not None,
            "features": {
                "learning": self.enable_learning,
                "monitoring": self.enable_monitoring,
                "checkpointing": self.enable_checkpointing,
                "approval": self.enable_approval,
                "isolation": self.enable_isolation,
            },
            "coordinators": {
                "llm": self._llm_coordinator is not None,
                "tool": self._tool_coordinator is not None,
                "memory": self._memory_coordinator is not None,
                "monitoring": self._monitoring_coordinator is not None,
                "isolation": self._isolation_coordinator is not None,
                "lifecycle": self._lifecycle_manager is not None,
                "state": self._state_manager is not None,
            },
        }
