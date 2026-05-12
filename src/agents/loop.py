"""
Agent Loop - Agent 主循环

基于 LangGraph StateGraph 的实现

增强功能：
- LangGraph 状态图执行
- 检查点和恢复
- 人工审批中断
- 工具/工作流自动搜索匹配
- 经验沉淀
- 用量监控与告警

重构说明：
- 使用协调器模式解耦职责
- LLM 客户端管理委托给 LLMCoordinator
- 工具管理委托给 ToolCoordinator
- 记忆管理委托给 MemoryCoordinator
- 监控管理委托给 MonitoringCoordinator
- 生命周期管理委托给 LifecycleManager
- 状态管理委托给 StateManager
"""

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from src.agents.context import AgentContext
from src.agents.coordinators import (
    LLMCoordinator,
    MemoryCoordinator,
    MonitoringCoordinator,
    ToolCoordinator,
)
from src.agents.graph import AgentGraphBuilder
from src.agents.interfaces import (
    ICheckpointer,
    IEmbeddingService,
    IRetriever,
    ITaskProgressManager,
    IUsageMonitor,
)
from src.agents.lifecycle import LifecycleManager, StateManager
from src.agents.state import create_initial_state
from src.agents.types import AgentConfig, ToolCallRecord, create_agent_result
from src.agents.types import AgentLifecycleState as AgentLifecycleStateEnum
from src.core.results import AgentExecutionResult
from src.tools.executor import ToolExecutor
from src.tools.interfaces import IToolExecutor, IToolRegistry
from src.tools.reasoning.middleware import ReasoningMiddleware
from src.tools.registry import ToolRegistry

# 延迟导入以避免循环导入
if False:  # type: ignore
    pass

logger = logging.getLogger(__name__)

# 可选导入（用于增强功能）
try:
    # 新架构组件
    ENHANCED_FEATURES_AVAILABLE = True
except ImportError:
    ENHANCED_FEATURES_AVAILABLE = False


class AgentLoop:
    """
    Agent 主循环（LangGraph 版本）

    基于 LangGraph StateGraph 实现的 Agent 执行循环

    特性：
    - 使用 StateGraph 管理执行流程
    - 支持检查点和恢复
    - 支持人工审批中断
    - 兼容原有接口
    """

    def __init__(
        self,
        context: AgentContext | None = None,
        # 向后兼容：支持直接传入参数
        config: AgentConfig | None = None,
        tool_registry: IToolRegistry | None = None,
        tool_executor: IToolExecutor | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        # 依赖注入参数（可选）
        db_session: Any | None = None,
        retriever: IRetriever | None = None,
        embedding_service: IEmbeddingService | None = None,
        usage_monitor: IUsageMonitor | None = None,
        task_progress_manager: ITaskProgressManager | None = None,
        checkpointer: ICheckpointer | None = None,
        # 功能开关
        enable_learning: bool = True,
        quota_config: Any | None = None,
        enable_monitoring: bool = True,
        enable_checkpointing: bool = True,
        enable_approval: bool = False,
        # 额外上下文（用于传递 task_id 等）
        extra_context: dict[str, Any] | None = None,
    ):
        """
        初始化 AgentLoop（支持依赖注入）

        新用法（推荐）：
            agent_loop = AgentLoop(context=agent_context)

        旧用法（向后兼容）：
            agent_loop = AgentLoop(
                config=config,
                tool_registry=tool_registry,
                tool_executor=tool_executor,
                ...
            )

        Args:
            context: Agent 上下文对象（推荐）
            config: Agent 配置（向后兼容）
            tool_registry: 工具注册表（向后兼容）
            tool_executor: 工具执行器（向后兼容）
            user_id: 用户 ID（向后兼容）
            session_id: 会话 ID（向后兼容）
            db_session: 数据库会话（可选，注入）
            retriever: 记忆检索器（可选，注入）
            embedding_service: 嵌入服务（可选，注入）
            usage_monitor: 用量监控器（可选，注入）
            task_progress_manager: 任务进度管理器（可选，注入）
            checkpointer: 检查点管理器（可选，注入）
            enable_learning: 是否启用学习功能
            quota_config: 配额配置
            enable_monitoring: 是否启用监控
            enable_checkpointing: 是否启用检查点
            enable_approval: 是否启用人工审批
            extra_context: 额外上下文（如 task_id）
        """
        # 初始化时间戳
        self._init_time = time.time()

        # 初始化标志
        self._components_initialized = False
        self._graph_built = False
        self._dynamic_tools_registered = False
        self._graph_reset_pending = False  # 标记是否需要重置图（消息删除后）
        self._clear_context_store_pending = False  # 标记是否需要清除 LayeredContextStore（消息删除后）

        # 注册到全局注册表
        from src.agents.registry import get_agent_loop_registry

        self._registry = get_agent_loop_registry()

        # 判断使用哪种模式
        if context is not None:
            # 新模式：使用 AgentContext
            self._context = context
            self.config = context.config
            self.tool_registry = context.tool_registry
            self._original_executor = context.tool_executor
            self.user_id = context.user_id
            if not context.session_id:
                raise ValueError("必须提供 session_id，请使用 thread-{user_id_short}-{session_seq} 格式")
            self.session_id = context.session_id
            self.enable_learning = (
                context.enable_learning and ENHANCED_FEATURES_AVAILABLE
            )
            self.enable_monitoring = (
                context.enable_monitoring and ENHANCED_FEATURES_AVAILABLE
            )
            self.enable_checkpointing = context.enable_checkpointing
            self.enable_approval = context.enable_approval
            self._extra_context = context.extra_context

            # 使用上下文的协调器
            self._llm_coordinator = context.llm_coordinator
            self._tool_coordinator = context.tool_coordinator
            self._memory_coordinator = context.memory_coordinator
            self._monitoring_coordinator = context.monitoring_coordinator
            self._lifecycle_manager = context.lifecycle_manager
            self._state_manager = context.state_manager

            # 设置监控回调
            self._monitoring_coordinator._alert_callback = self._handle_usage_alert

            # LangGraph 图
            self._graph: Any | None = None
            self._checkpointer = context.checkpointer

            # 向后兼容的属性（通过上下文访问）
            self._db_session = context.db_session
            self._retriever = context.retriever
            self._embedding_service = context.embedding_service
            self._usage_monitor = context.usage_monitor
            self._task_progress_manager = context.task_progress_manager

            # 新架构组件（通过上下文访问）
            self._layered_context_store = context.layered_context_store
            self._prompt_builder = context.prompt_builder
            self._knowledge_injection = context.knowledge_injection

            # 包装工具执行器
            self.tool_executor = ReasoningMiddleware(self._original_executor)

            # TaskClient 延迟初始化（在 _initialize_components 中）
            self.task_client = None

            # 第二条AI消息ID（工具调用后的回复）- 由 stream_processor 设置
            self.second_ai_message_id: str | None = None

        else:
            # 旧模式：直接传入参数（向后兼容）
            # 发出弃用警告
            import warnings

            warnings.warn(
                "直接传递参数给 AgentLoop 已弃用。"
                "请使用 AgentContext 或工厂函数（create_agent_loop）。"
                "此方式将在 v2.0 中移除。"
                "示例: context = AgentContext(config, tool_registry, tool_executor); loop = AgentLoop(context=context)",
                DeprecationWarning,
                stacklevel=2,
            )

            if config is None:
                raise ValueError(
                    "必须提供 context 参数，或者提供 config 参数"
                )

            # 如果没有提供 tool_registry 或 tool_executor，创建默认实例
            if tool_registry is None:
                tool_registry = ToolRegistry()
            if tool_executor is None:
                tool_executor = ToolExecutor(registry=tool_registry)

            self.config = config
            self.tool_registry = tool_registry
            # 使用推理中间件包装工具执行器
            self.tool_executor = ReasoningMiddleware(tool_executor)
            self._original_executor = tool_executor  # 保留原始执行器引用
            self.user_id = user_id
            if not session_id:
                raise ValueError("必须提供 session_id，请使用 thread-{user_id_short}-{session_seq} 格式")
            self.session_id = session_id
            self.enable_learning = enable_learning and ENHANCED_FEATURES_AVAILABLE
            self.enable_monitoring = enable_monitoring and ENHANCED_FEATURES_AVAILABLE
            self.enable_checkpointing = enable_checkpointing
            self.enable_approval = enable_approval
            self._extra_context = extra_context or {}

            # 创建协调器
            self._llm_coordinator = LLMCoordinator(config)
            self._tool_coordinator = ToolCoordinator(
                tool_ids=config.tool_ids or [],
                tool_registry=tool_registry,
                tool_executor=tool_executor,
                session_id=self.session_id,
                user_id=user_id,
            )
            self._memory_coordinator = MemoryCoordinator(
                config=config,
                user_id=user_id,
                session_id=self.session_id,
                db_session=db_session,
                retriever=retriever,
                embedding_service=embedding_service,
            )
            self._monitoring_coordinator = MonitoringCoordinator(
                session_id=self.session_id,
                user_id=user_id,
                quota_config=quota_config,
                usage_monitor=usage_monitor,
                task_progress_manager=task_progress_manager,
                alert_callback=self._handle_usage_alert,
            )
            self._lifecycle_manager = LifecycleManager(
                user_id=user_id,
                session_id=self.session_id,
                db_session=db_session,
                embedding_service=embedding_service,
                enable_learning=self.enable_learning,
            )
            self._state_manager = StateManager()

            # LangGraph 图
            self._graph: Any | None = None
            self._checkpointer = checkpointer

            # 向后兼容的属性（通过协调器访问）
            self._db_session = db_session
            self._retriever = retriever
            self._embedding_service = embedding_service
            self._usage_monitor = usage_monitor
            self._task_progress_manager = task_progress_manager

            # 新架构组件（通过记忆协调器访问）
            self._prompt_builder: Any | None = None
            self._knowledge_injection: Any | None = None
            self._layered_context_store: Any | None = None

            # TaskClient 延迟初始化（在 _initialize_components 中）
            self.task_client = None

            # 第二条AI消息ID（工具调用后的回复）- 由 stream_processor 设置
            self.second_ai_message_id: str | None = None

        # 注册到全局注册表（两种模式都需要）
        self._registry.register(self.session_id, self)
        logger.debug(f"[Agent Loop] 已注册到全局注册表 | session_id={self.session_id}")

    def _register_if_needed(self) -> None:
        """如果需要，注册到全局注册表"""
        if hasattr(self, '_registry') and hasattr(self, 'session_id') and self.session_id:
            if self._registry.get(self.session_id) != self:
                self._registry.register(self.session_id, self)
                logger.debug(f"[Agent Loop] 已注册到全局注册表 | session_id={self.session_id}")

    @property
    def state(self) -> AgentLifecycleStateEnum:
        """获取当前状态"""
        return self._state_manager.state

    def stop(self) -> None:
        """请求停止执行"""
        self._state_manager.request_stop()

    def pause(self) -> None:
        """请求暂停执行"""
        self._state_manager.request_pause()

    def resume(self) -> None:
        """恢复执行"""
        self._state_manager.resume()

    async def _cleanup_tasks(self) -> None:
        """
        清理已完成的任务

        移除已完成的任务，防止内存泄漏
        """
        await self._lifecycle_manager.cleanup_tasks()

    async def cleanup(self) -> None:
        """
        清理资源

        取消所有待处理的后台任务，释放资源
        """
        # 清理所有协调器
        await self._lifecycle_manager.cleanup()
        await self._monitoring_coordinator.cleanup()
        await self._memory_coordinator.cleanup()
        self._llm_coordinator.cleanup()
        self._state_manager.cleanup()

        # 清理数据库会话（向后兼容）
        if self._db_session:
            await self._db_session.close()
            self._db_session = None

    async def _initialize_components(self) -> None:
        """
        初始化组件

        优先使用注入的依赖，未注入时才自动创建
        使用初始化标志避免重复初始化
        """
        import time

        init_start = time.time()

        # 检查是否已经初始化
        if self._components_initialized:
            logger.debug("[Agent Loop] 组件已初始化，跳过初始化过程")
            return

        # 并行初始化协调器，减少等待时间
        init_tasks = []
        if self._memory_coordinator:
            init_tasks.append(self._memory_coordinator.initialize())
        if self._monitoring_coordinator:
            init_tasks.append(self._monitoring_coordinator.initialize())

        if init_tasks:
            await asyncio.gather(*init_tasks)

        # 更新向后兼容的属性
        if self._memory_coordinator:
            if self._memory_coordinator.retriever:
                self._retriever = self._memory_coordinator.retriever
            if self._memory_coordinator.embedding_service:
                self._embedding_service = self._memory_coordinator.embedding_service
            # 更新新架构组件
            self._layered_context_store = self._memory_coordinator.layered_context_store
            self._prompt_builder = self._memory_coordinator.prompt_builder
            self._knowledge_injection = self._memory_coordinator.knowledge_injection

            # 检查是否需要清除 LayeredContextStore（消息删除后）
            if self._clear_context_store_pending and self._layered_context_store is not None:
                try:
                    self._layered_context_store.clear_messages()
                    self._clear_context_store_pending = False
                    logger.info(f"[Agent Loop] LayeredContextStore 已清除（初始化后）| session_id={self.session_id}")
                except Exception as e:
                    logger.debug(f"[Agent Loop] 清除 LayeredContextStore 时出错: {e}")

        if self._monitoring_coordinator:
            if self._monitoring_coordinator.usage_monitor:
                self._usage_monitor = self._monitoring_coordinator.usage_monitor
            if self._monitoring_coordinator.task_progress_manager:
                self._task_progress_manager = (
                    self._monitoring_coordinator.task_progress_manager
                )

        # 初始化检查点（如果未注入）
        if self.enable_checkpointing and self._checkpointer is None:
            # 使用 checkpoint 管理器获取线程隔离的 MemorySaver
            from src.agents.langgraph_checkpoint import get_checkpoint_manager

            checkpoint_manager = get_checkpoint_manager()
            self._checkpointer = checkpoint_manager.get_checkpointer(self.session_id)

        # 初始化 TaskClient（延迟导入避免循环导入）
        if self.task_client is None:
            from src.orchestration.task_client import TaskClient
            from src.orchestration.types import AgentLevel

            # 从 extra_context 获取 parent_record_id（用于 SubAgent 场景）
            parent_record_id = self._extra_context.get("parent_record_id") if self._extra_context else None

            self.task_client = TaskClient(
                current_agent_level=AgentLevel.L1,  # 默认 L1，可通过配置调整
                session_id=self.session_id,
                parent_record_id=parent_record_id,  # 传递父执行记录 ID
            )

        # 构建 LangGraph 图（延迟构建，仅在需要时）
        # 检查是否需要重置图（消息删除后）
        # 注意：_graph_reset_pending 不在此处重置，而是在 stream 方法中使用新的 thread_id 后才重置
        if self._graph_reset_pending and self._graph is not None:
            logger.info(f"[Agent Loop] 检测到图重置标记，重新构建图 | session_id={self.session_id}")
            self._graph = None
            self._graph_built = False

        if not self._graph_built and self._graph is None:
            builder = AgentGraphBuilder()
            # 只有在启用 checkpointing 时才使用 checkpointer
            if self.enable_checkpointing and self._checkpointer:
                builder.with_checkpointer(self._checkpointer)
            if self.enable_approval:
                builder.with_human_approval(True)
            self._graph = builder.build()
            self._graph_built = True

        # 注册动态工具（需要数据库会话的工具）
        if (
            not self._dynamic_tools_registered
            and self._memory_coordinator
            and self._memory_coordinator._db_session
        ):
            try:
                # 设置工具注册表到记忆协调器
                self.config._tool_registry = self.tool_registry
                await self._memory_coordinator.register_dynamic_tools(
                    tool_registry=self.tool_registry,
                )
                self._dynamic_tools_registered = True
            except Exception as e:
                logger.warning(f"[Agent Loop] 动态工具注册失败: {e}")

        # 标记初始化完成
        self._components_initialized = True

        logger.debug(
            f"[Agent Loop] 组件初始化完成 | duration_ms={int((time.time() - init_start) * 1000)}"
        )
        logger.info(
            f"[Agent Loop] 从创建到初始化完成总耗时 | total_duration_ms={int((time.time() - self._init_time) * 1000)}"
        )

    async def _handle_usage_alert(self, alert: Any) -> None:
        """
        处理用量告警

        Args:
            alert: 告警对象
        """
        logger.warning(
            f"[AgentLoop] 用量告警: {alert.level.value.upper()} - {alert.message}"
        )

        if alert.level == "CRITICAL":
            self._state_manager.request_stop()

    async def _handle_subagent_tool_call(self, tool_call: Any) -> str:
        """
        处理 SubAgent 工具调用

        使用 TaskClient 提交子任务到全局调度器。
        支持 Agent 和 Workflow 两种目标类型。

        Args:
            tool_call: 工具调用对象，包含 arguments 属性

        Returns:
            任务执行结果字符串
        """
        arguments = tool_call.arguments if hasattr(tool_call, "arguments") else tool_call.get("arguments", {})
        target_type = arguments.get("target_type", "agent")

        logger.info(
            f"[AgentLoop] 处理 SubAgent 工具调用 | "
            f"target_type={target_type} | "
            f"session_id={self.session_id}"
        )

        try:
            if target_type == "agent":
                result = await self.task_client.submit_agent_task(
                    description=arguments.get("description", "子 Agent 任务"),
                    prompt=arguments.get("prompt", ""),
                    target_id=arguments.get("agent_id"),
                    agent_config=arguments.get("agent_config"),
                    is_subagent_context=True,
                )
            elif target_type == "workflow":
                result = await self.task_client.submit_workflow_task(
                    description=arguments.get("description", "工作流任务"),
                    workflow=arguments.get("workflow"),
                    inputs=arguments.get("inputs", {}),
                    is_subagent_context=True,
                )
            else:
                result = f"错误：不支持的目标类型 {target_type}"
                logger.warning(f"[AgentLoop] 不支持的目标类型 | target_type={target_type}")

            return result

        except Exception as e:
            logger.error(f"[AgentLoop] SubAgent 工具调用失败 | error={str(e)}")
            return f"子任务执行失败: {str(e)}"

    async def run(self, user_input: str) -> AgentExecutionResult:
        """
        执行 Agent Loop

        Args:
            user_input: 用户输入

        Returns:
            执行结果
        """
        logger.info(
            f"[Agent Loop] 开始执行 | session_id={self.session_id} | user_id={self.user_id} | model={self.config.model_name}"
        )
        logger.debug(
            f"[Agent Loop] 用户输入 | input={user_input[:200] + '...' if len(user_input) > 200 else user_input}"
        )
        logger.debug(
            f"[Agent Loop] 执行配置 | agent_type={self.config.agent_type} | tool_count={len(self.config.tool_ids) if self.config.tool_ids else 0}"
        )

        # 初始化组件
        await self._initialize_components()
        logger.debug("[Agent Loop] 组件初始化完成")

        # 关键：将用户输入添加到 LayeredContextStore
        if self._layered_context_store and user_input:
            try:
                # 添加用户消息到 LayeredContextStore（需要 await）
                await self._layered_context_store.add_message(
                    message={
                        "role": "user",
                        "content": user_input,
                        "metadata": {"type": "user_input", "source": "agent_loop.run"},
                        "executor": {
                            "type": "user",
                            "id": self.user_id or "system",
                            "name": "TaskSubmitter",
                        },
                    },
                    persist_to_db=True,  # 保存到数据库，确保 ContextBuilder 能加载
                )
                logger.info(f"[Agent Loop] 用户输入已添加到 LayeredContextStore | length={len(user_input)}")
            except Exception as e:
                logger.warning(f"[Agent Loop] 添加用户输入到 LayeredContextStore 失败: {e}")

        # 准备执行状态
        self._state_manager.prepare_for_execution()

        execution_start_time = time.time()

        try:
            # 获取可用工具
            tools = self._tool_coordinator.get_tools_for_graph()
            tool_names = [getattr(t, "name", str(t)) for t in tools]
            logger.info(
                f"[Agent Loop] 加载工具 | count={len(tools)} | tools={tool_names}"
            )
            logger.debug(f"[Agent Loop] 工具详情 | tool_ids={self.config.tool_ids}")

            # 记录系统提示
            if self.config.system_prompt:
                prompt_preview = (
                    self.config.system_prompt[:300] + "..."
                    if len(self.config.system_prompt) > 300
                    else self.config.system_prompt
                )
                logger.debug(f"[Agent Loop] 系统提示 | prompt={prompt_preview}")

            # 创建初始状态
            # 合并基础 context 和额外 context（如 task_id）
            context = {
                "session_id": self.session_id,
                "user_id": self.user_id,
                **self._extra_context,
            }
            initial_state = create_initial_state(
                user_input=user_input,  # 保留参数用于兼容性，但不再用于创建消息
                system_prompt=self.config.system_prompt,
                llm_client=self._llm_coordinator.get_langchain_llm(),
                tools=tools,
                tool_executor=self.tool_executor,
                context=context,
                agent_config=self.config,
                layered_context_store=self._layered_context_store,
            )

            # 配置执行
            # 如果图被重置过，使用一个新的 thread_id 来避免从旧的 checkpoint 恢复状态
            logger.info(f"[Agent Loop] 检查 _graph_reset_pending | value={self._graph_reset_pending} | session_id={self.session_id}")
            if self._graph_reset_pending:
                import uuid
                thread_id = f"{self.session_id}-{uuid.uuid4().hex[:8]}"
                logger.info(f"[Agent Loop] 使用新的 thread_id 避免从旧 checkpoint 恢复 | thread_id={thread_id}")
                # 重置标记，下次执行时使用正常的 thread_id
                self._graph_reset_pending = False
            else:
                thread_id = self.session_id

            config = {
                "configurable": {
                    "thread_id": thread_id,
                    "llm_client": self._llm_coordinator.get_langchain_llm(),
                    "tool_executor": self.tool_executor,
                    "layered_context_store": self._layered_context_store,
                    # 硬编码 recursion_limit，支持复杂调用链
                    "recursion_limit": 500,
                }
            }

            logger.info(
                "[Agent Loop] 开始执行图"
            )

            # 执行图
            final_state = await self._graph.ainvoke(initial_state, config)
            execution_duration_ms = int((time.time() - execution_start_time) * 1000)

            # 处理结果
            result = self._process_result(final_state, user_input)

            logger.info(
                f"[Agent Loop] 执行完成 | success={result.success} | "
                f"iterations={result.iterations} | tool_calls={len(result.tool_calls)} | "
                f"duration_ms={execution_duration_ms}"
            )
            if result.output:
                output_preview = (
                    result.output[:300] + "..."
                    if len(result.output) > 300
                    else result.output
                )
                logger.debug(f"[Agent Loop] 最终输出 | output={output_preview}")

            return result

        except Exception as e:
            logger.exception(
                f"[Agent Loop] 执行失败 | session_id={self.session_id} | error={str(e)}"
            )
            self._state_manager.set_state(AgentLifecycleStateEnum.FAILED)
            return create_agent_result(
                success=False,
                error=str(e),
                error_code="EXECUTION_ERROR",
                iterations=0,
                tool_calls=self._state_manager.tool_calls,
            )

    def _process_result(
        self,
        final_state: dict[str, Any],
        user_input: str,
    ) -> AgentExecutionResult:
        """
        处理执行结果

        Args:
            final_state: 最终状态
            user_input: 用户输入

        Returns:
            AgentExecutionResult
        """
        # 提取工具调用记录
        tool_calls_data = final_state.get("tool_calls", [])
        tool_calls = [
            ToolCallRecord(**tc) if isinstance(tc, dict) else tc
            for tc in tool_calls_data
        ]
        self._state_manager.set_tool_calls(tool_calls)

        # 检查错误
        error = final_state.get("error")
        if error:
            self._state_manager.set_state(AgentLifecycleStateEnum.FAILED)
            return create_agent_result(
                success=False,
                error=error,
                error_code="GRAPH_ERROR",
                iterations=final_state.get("iteration", 0),
                tool_calls=tool_calls,
            )

        # 获取最终输出
        final_output = final_state.get("final_output")
        if not final_output:
            # 从消息中提取最后的 AI 回复
            messages = final_state.get("messages", [])
            for msg in reversed(messages):
                if hasattr(msg, "type") and msg.type == "ai":
                    final_output = msg.content
                    break

        self._state_manager.set_state(AgentLifecycleStateEnum.COMPLETED)

        # 存储经验（如果启用）
        if self.enable_learning and self.user_id:
            self._lifecycle_manager.schedule_experience_storage(
                intent=user_input,
                result=final_output or "",
                iterations=final_state.get("iteration", 0),
                tool_calls=tool_calls,
                tags=self.config.tags,
            )

        return create_agent_result(
            success=True,
            output=final_output,
            iterations=final_state.get("iteration", 0),
            tool_calls=tool_calls,
        )

    async def stream(
        self,
        user_input: str,
        stream_mode: str = "updates",
        enable_thinking: bool | None = None,  # 改为 Optional[bool]
        thinking_callback: Callable | None = None,  # 思考内容回调函数
        execution_record_id: str | None = None,  # 执行记录 ID（用于工具调用嵌套）
    ):
        """
        流式执行 Agent Loop

        Args:
            user_input: 用户输入
            stream_mode: 流式模式
                - "updates": 仅状态变化（推荐用于聊天）
                - "values": 完整状态快照
                - "debug": 完整执行追踪
            enable_thinking: 是否启用思考模式（None=使用Agent配置，True/False=用户明确指定）
            thinking_callback: 思考内容回调函数（用于实时发送思考内容到前端）
            execution_record_id: 执行记录 ID（用于工具调用嵌套）

        Yields:
            根据 stream_mode 返回不同类型的数据：
            - updates: Dict[node_name, updates] 字典
            - values: 完整状态字典
            - debug: 调试信息
        """
        await self._initialize_components()

        # 临时存储回调函数（用于在 create_initial_state 中访问）
        self._thinking_callback = thinking_callback

        # 准备执行状态
        self._state_manager.prepare_for_execution()

        tools = self._tool_coordinator.get_tools_for_graph()
        tool_names = [getattr(t, "name", str(t)) for t in tools]

        logger.info(
            f"[AgentLoop.stream] 开始流式执行 | "
            f"stream_mode={stream_mode} | "
            f"session_id={self.session_id} | "
            f"user_id={self.user_id} | "
            f"model={self.config.model_name} | "
            f"tools={tool_names}"
        )
        logger.debug(f"[AgentLoop.stream] 用户输入 | input={user_input[:200]}...")

        initial_state = create_initial_state(
            user_input=user_input,
            system_prompt=self.config.system_prompt,
            llm_client=self._llm_coordinator.get_langchain_llm(),
            tools=tools,
            tool_executor=self.tool_executor,
            context={
                "session_id": self.session_id,
                "user_id": self.user_id,
                "model_name": self.config.model_name,
                "execution_record_id": execution_record_id,  # 用于工具调用嵌套
            },
            enable_thinking=enable_thinking,
            agent_config=self.config,
            layered_context_store=self._layered_context_store,
            thinking_callback=self._thinking_callback,
        )

        # 配置执行
        # 如果图被重置过，使用一个新的 thread_id 来避免从旧的 checkpoint 恢复状态
        logger.info(f"[Agent Loop] 检查 _graph_reset_pending | value={self._graph_reset_pending} | session_id={self.session_id}")
        if self._graph_reset_pending:
            import uuid
            thread_id = f"{self.session_id}-{uuid.uuid4().hex[:8]}"
            logger.info(f"[Agent Loop] 使用新的 thread_id 避免从旧 checkpoint 恢复 | thread_id={thread_id}")
            # 重置标记，下次执行时使用正常的 thread_id
            self._graph_reset_pending = False
        else:
            thread_id = self.session_id

        config = {
            "configurable": {
                "thread_id": thread_id,
                "llm_client": self._llm_coordinator.get_langchain_llm(),
                "tool_executor": self.tool_executor,
                "layered_context_store": self._layered_context_store,
                "thinking_callback": self._thinking_callback,
                "agent_loop": self,  # 传递 agent_loop 实例，用于获取 second_ai_message_id
            }
        }

        logger.debug(
            f"[AgentLoop.stream] 状态创建完成 | enable_thinking={enable_thinking}"
        )

        event_count = 0
        event_types = {}
        stream_start_time = time.time()

        async for event in self._graph.astream(
            initial_state, config, stream_mode=stream_mode
        ):
            event_count += 1
            event_type = type(event).__name__
            event_types[event_type] = event_types.get(event_type, 0) + 1

            # 记录事件详情
            if isinstance(event, tuple) and len(event) >= 1:
                msg = event[0]
                if hasattr(msg, "content"):
                    content_preview = str(msg.content)[:50] if msg.content else ""
                    has_tool_calls = hasattr(msg, "tool_calls") and msg.tool_calls
                    logger.debug(
                        f"[AgentLoop.stream] 事件 #{event_count} | "
                        f"type={event_type} | "
                        f"content='{content_preview}' | "
                        f"has_tool_calls={has_tool_calls}"
                    )
                else:
                    logger.debug(
                        f"[AgentLoop.stream] 事件 #{event_count} | type={event_type}"
                    )
            else:
                logger.debug(
                    f"[AgentLoop.stream] 事件 #{event_count} | type={event_type}"
                )
            yield event

        stream_duration_ms = int((time.time() - stream_start_time) * 1000)

        logger.info(
            f"[AgentLoop.stream] 流式执行完成 | "
            f"总事件数={event_count} | "
            f"事件类型统计={event_types} | "
            f"duration_ms={stream_duration_ms}"
        )

    async def resume_from_interrupt(self, response: dict[str, Any]) -> AgentExecutionResult:
        """
        从人工审批中断处恢复执行

        Args:
            response: 人工审批响应

        Returns:
            执行结果
        """
        if not self._graph or not self._checkpointer:
            return AgentExecutionResult(
                success=False,
                error="无法恢复：图或检查点未初始化",
                error_code="RESUME_ERROR",
            )

        from langgraph.types import Command

        config = {
            "configurable": {
                "thread_id": self.session_id,
                "llm_client": self._llm_coordinator.get_langchain_llm(),
                "tool_executor": self.tool_executor,
                "layered_context_store": self._layered_context_store,
            }
        }

        try:
            final_state = await self._graph.ainvoke(
                Command(resume=response),
                config,
            )
            return self._process_result(final_state, "")

        except Exception as e:
            return create_agent_result(
                success=False,
                error=str(e),
                error_code="RESUME_ERROR",
            )

    def get_usage_statistics(self) -> dict[str, Any] | None:
        """获取用量统计"""
        return self._monitoring_coordinator.get_usage_statistics()

    async def get_memory_stats(self) -> dict[str, Any]:
        """
        获取记忆统计信息

        Returns:
            记忆统计信息
        """
        if not self.user_id:
            return {"error": "用户ID未提供"}

        try:
            stats = await self._memory_coordinator.get_memory_stats()
            return stats
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
        if not self.user_id:
            return {"error": "用户ID未提供"}

        return await self._memory_coordinator.search_memories(
            query=query,
            memory_types=memory_types,
            top_k=top_k,
        )

    def reset_graph(self) -> None:
        """
        重置 LangGraph 图实例

        当用户删除消息时调用此方法，强制下次执行时重新构建图，
        确保不会使用已删除的历史消息。
        """
        # 设置重置标记，下次执行时自动重置
        self._graph_reset_pending = True

        # 立即重置图实例，确保下次执行时重新构建
        if self._graph is not None:
            self._graph = None
            self._graph_built = False
            logger.info(f"[Agent Loop] 图实例已立即重置 | session_id={self.session_id}")

        # 重置 checkpointer（如果启用）
        if self.enable_checkpointing:
            from langgraph.checkpoint.memory import MemorySaver

            from src.agents.langgraph_checkpoint import get_checkpoint_manager

            checkpoint_manager = get_checkpoint_manager()

            # 清除旧的 checkpoint 数据
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.create_task(checkpoint_manager.clear_thread_checkpoints(self.session_id))
                else:
                    loop.run_until_complete(checkpoint_manager.clear_thread_checkpoints(self.session_id))
            except Exception as e:
                logger.debug(f"[Agent Loop] 清除 checkpoint 时出错: {e}")

            # 创建全新的 MemorySaver 实例
            self._checkpointer = MemorySaver()
            logger.info(f"[Agent Loop] Checkpointer 已重置 | session_id={self.session_id}")

        # 清除 LayeredContextStore 缓存（无论是否启用 checkpointing）
        if self._layered_context_store is not None:
            try:
                # LayeredContextStore 使用 clear_messages 方法
                self._layered_context_store.clear_messages()
                logger.info(f"[Agent Loop] LayeredContextStore 已清除 | session_id={self.session_id}")
            except Exception as e:
                logger.debug(f"[Agent Loop] 清除 LayeredContextStore 时出错: {e}")
        else:
            # LayeredContextStore 还未初始化，设置标记，在初始化后清除
            self._clear_context_store_pending = True
            logger.info(f"[Agent Loop] LayeredContextStore 未初始化，设置清除标记 | session_id={self.session_id}")

        logger.info(f"[Agent Loop] 图重置完成 | session_id={self.session_id}")
