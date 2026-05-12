"""
应用生命周期管理

负责应用启动和关闭时的初始化和清理工作
"""

import logging
import time

from sqlalchemy.ext.asyncio import AsyncSession

from src.db.connection import DatabaseManager

logger = logging.getLogger(__name__)


class StartupManager:
    """
    应用启动管理器

    负责处理应用生命周期中的各种初始化任务，包括：
    - LangChain 模块预加载
    - 数据库初始化和资源同步
    - 工具系统初始化
    - 调度器启动
    """

    def __init__(self):
        """初始化启动管理器"""
        self.db_manager: DatabaseManager | None = None
        self.watchdog_manager = None
        self.hot_reloader = None
        self.task_executor = None  # 任务执行器（事件驱动）
        self.task_scheduler = None  # 任务调度服务
        self.task_orchestrator = None  # 任务编排器（事件驱动改造）

    async def startup(self):
        """
        执行应用启动逻辑

        按顺序执行以下任务：
        1. 预加载 LangChain 模块
        2. 初始化 DI 容器并注册所有服务
        3. 初始化数据库管理器
        4. 初始化认证依赖
        5. 启动 Watchdog 服务
        6. 同步资源到数据库
        7. 初始化工具系统
        8. 启动全局调度器
        9. 初始化序列号管理器
        10. 启动配置热更新服务
        """
        logger.info("正在启动应用...")

        # 1. 预加载 LangChain 模块
        await self._preload_langchain()

        # 2. 初始化 DI 容器并注册所有服务
        await self._initialize_di_container()

        # 3. 初始化数据库管理器
        self.db_manager = await self._initialize_database()

        # 4. 初始化认证依赖
        await self._initialize_auth()

        # 5. 启动 Watchdog 服务
        self.watchdog_manager = await self._start_watchdog()

        # 6. 同步资源到数据库
        if self.db_manager:
            await self._sync_resources()

        # 7. 初始化工具系统
        await self._initialize_tools()

        # 8. 启动全局调度器
        await self._start_scheduler()

        # 9. 初始化序列号管理器
        await self._initialize_sequence_manager()

        # 10. 启动配置热更新服务
        await self._start_hot_reloader()

        logger.info("应用启动完成")

    async def shutdown(self):
        """
        执行应用关闭逻辑

        清理资源，停止服务
        """
        logger.info("正在关闭应用...")

        from src.orchestration.scheduler import stop_global_scheduler

        try:
            await stop_global_scheduler()
            logger.info("调度器已停止")
        except Exception as e:
            logger.warning("停止调度器失败: %s", e)

        if self.task_scheduler:
            try:
                await self.task_scheduler.stop()
                logger.info("任务调度服务已停止")
            except Exception as e:
                logger.warning("停止任务调度服务失败: %s", e)

        if self.task_executor:
            try:
                await self.task_executor.stop()
                logger.info("任务执行器已停止")
            except Exception as e:
                logger.warning("停止任务执行器失败: %s", e)

        await self._stop_watchdog()

        await self._stop_hot_reloader()

        logger.info("应用关闭完成")

    async def _preload_langchain(self):
        """
        预加载 LangChain 模块

        减少首次调用时的延迟
        """
        start = time.time()
        try:
            from langchain_anthropic import ChatAnthropic  # noqa: F401
            from langchain_community.chat_models import ChatOllama  # noqa: F401
            from langchain_openai import ChatOpenAI  # noqa: F401

            logger.info("LangChain 预加载完成 | 耗时: %.2fs", time.time() - start)
        except Exception as e:
            logger.warning("LangChain 预加载失败: %s", e)

    async def _initialize_di_container(self):
        """
        初始化 DI 容器并注册所有服务

        这是依赖注入的核心入口点，确保所有服务都通过 DI 容器管理
        """
        try:
            from src.core.di import get_global_container
            from src.core.di.service_initialization import register_all_services

            # 获取全局容器
            container = get_global_container()

            # 注册所有服务
            await register_all_services(container)

            logger.info(
                f"DI 容器初始化完成 | 已注册服务: {list(container.list_services().keys())}"
            )
        except Exception as e:
            logger.error("初始化 DI 容器失败: %s", e, exc_info=True)
            # 不抛出异常，允许应用继续运行（使用旧的全局单例作为后备）

    async def _initialize_auth(self):
        """
        初始化认证依赖

        设置全局的认证依赖，包括 TokenManager、UserRepository 和 RBACManager
        """
        try:
            from src.api.dependencies import token_manager
            from src.auth import init_auth_dependencies
            from src.auth.rbac import RBACManager
            from src.db.repositories import UserRepository

            # 创建数据库会话工厂
            if self.db_manager:
                # 初始化认证依赖
                def user_repo_factory():
                    """用户仓库工厂"""
                    return UserRepository(self.db_manager.session_factory())

                # 初始化 RBAC 管理器
                rbac_manager = RBACManager()

                # 初始化全局认证依赖
                init_auth_dependencies(
                    token_manager=token_manager,
                    user_repository=user_repo_factory(),
                    rbac_manager=rbac_manager,
                )

                logger.info("认证依赖初始化成功")
            else:
                logger.warning("数据库管理器未初始化，跳过认证依赖初始化")

        except Exception as e:
            logger.error("初始化认证依赖失败: %s", e, exc_info=True)

    async def _initialize_database(self) -> DatabaseManager | None:
        """
        初始化数据库管理器

        Returns:
            DatabaseManager 实例，失败时返回 None
        """
        try:
            from src.db.connection import get_db_manager

            db_manager = get_db_manager()

            # 确保数据库表已创建
            await db_manager.create_all()
            logger.info("数据库表初始化完成")

            return db_manager
        except Exception as e:
            logger.error("获取数据库管理器失败: %s", e, exc_info=True)
            return None

    async def _start_watchdog(self):
        """
        启动 Watchdog 服务

        Returns:
            WatchdogServiceManager 实例，失败时返回 None
        """
        try:
            from src.core.di import get_global_container
            from src.services.watchdog_service import WatchdogServiceManager

            container = get_global_container()

            if not container.has("execution_service"):
                logger.warning(
                    "execution_service 未在 DI 容器中注册，跳过 Watchdog 服务启动"
                )
                return None

            # 如果 watchdog_manager 已在 DI 容器中注册，获取它并启动
            if container.has("watchdog_manager"):
                watchdog_manager = container.get("watchdog_manager")
                if hasattr(watchdog_manager, "start") and callable(
                    watchdog_manager.start
                ):
                    await watchdog_manager.start()
                    logger.info("Watchdog 服务已启动（从 DI 容器获取）")
                    return watchdog_manager
                return watchdog_manager

            # 兼容旧版本：如果容器中没有，则创建并注册
            watchdog_manager = WatchdogServiceManager(self.db_manager.session_factory)
            await watchdog_manager.start()
            logger.info("Watchdog 服务已启动")
            return watchdog_manager
        except Exception as e:
            logger.error("启动 Watchdog 服务失败: %s", e, exc_info=True)
            return None

    async def _sync_resources(self):
        """
        全量同步资源到数据库

        包括工具、Agent 和工作流的同步
        """
        try:
            logger.info("开始全量同步资源到数据库...")
            sync_start = time.time()

            async with self.db_manager.session_factory() as session:
                # 1. 同步工具
                await self._sync_tools(session)

                # 2. 同步 Agent
                await self._sync_agents(session)

                # 3. 同步工作流
                await self._sync_workflows(session)

                await session.commit()

            logger.info("资源全量同步完成 | 耗时: %.2fs", time.time() - sync_start)
        except Exception as e:
            logger.error("资源同步失败: %s", e, exc_info=True)

    async def _sync_tools(self, session: AsyncSession):
        """
        同步工具到数据库

        Args:
            session: 数据库会话
        """
        from src.services.tool_sync_service import ToolSyncService

        tool_sync = ToolSyncService(session)
        tool_result = await tool_sync.sync_all_builtin_tools()
        logger.info(
            "工具同步完成 | 新增: %d, 更新: %d, 废弃: %d, 未变更: %d",
            len(tool_result.added),
            len(tool_result.updated),
            len(tool_result.deprecated),
            len(tool_result.unchanged),
        )

    async def _sync_agents(self, session: AsyncSession):
        """
        同步 Agent 到数据库

        Args:
            session: 数据库会话
        """
        from src.services.agent_sync_service import AgentSyncService

        agent_sync = AgentSyncService()
        agent_stats = await agent_sync.sync_all(session)
        logger.info(
            "Agent同步完成 | 创建: %d, 更新: %d, 跳过: %d, 失败: %d",
            agent_stats["created"],
            agent_stats["updated"],
            agent_stats["skipped"],
            agent_stats["failed"],
        )

    async def _sync_workflows(self, session: AsyncSession):
        """
        同步工作流到数据库

        Args:
            session: 数据库会话
        """
        from src.services.workflow_sync_service import WorkflowSyncService

        workflow_sync = WorkflowSyncService()
        workflow_stats = await workflow_sync.sync_all(session)
        logger.info(
            "工作流同步完成 | 创建: %d, 更新: %d, 跳过: %d, 失败: %d",
            workflow_stats["created"],
            workflow_stats["updated"],
            workflow_stats["skipped"],
            workflow_stats["failed"],
        )

    async def _initialize_tools(self):
        """
        初始化全局工具注册表

        预热工具注册，避免首次连接时的延迟
        """
        try:
            logger.info("开始初始化全局工具注册表...")
            start = time.time()

            # 预热全局工具注册表
            from src.tools.global_registry import get_global_tool_registry

            await get_global_tool_registry()

            elapsed = time.time() - start
            logger.info(
                "全局工具注册表初始化完成 | 耗时: %.2fs",
                elapsed,
            )
        except Exception as e:
            logger.error("初始化全局工具注册表失败: %s", e, exc_info=True)

    async def _start_scheduler(self):
        """
        启动调度相关服务

        统一调度入口设计：
        - scheduler.schedule(task_id) 是唯一的任务执行入口
        - TaskRunner 执行实际的任务
        - TaskRecoveryOrchestrator 恢复未完成任务
        - TaskSchedulerService 处理任务提交事件
        """
        restored_timers = 0
        try:
            from src.tasks.timer_manager import TimerManager

            timer_manager = TimerManager.get_instance()

            async def on_timeout(task_id: str) -> None:
                """计时器超时回调"""
                from src.core.event_bus import EventType, ExecutionEvent
                from src.core.event_bus.factory import get_event_bus

                event_bus = get_event_bus()
                await event_bus.publish(
                    ExecutionEvent(
                        event_type=EventType.TASK_TIMEOUT,
                        session_id=task_id,
                        data={"task_id": task_id, "reason": "timer_expired"},
                    )
                )
                logger.warning(f"任务超时事件已发布: task_id={task_id}")

            restored_timers = await timer_manager.restore_from_db(on_timeout)
            logger.info(f"计时器恢复完成: restored={restored_timers}")
        except Exception as e:
            logger.error("恢复计时器失败: %s", e, exc_info=True)

        try:
            from src.orchestration.scheduler import start_global_scheduler

            await start_global_scheduler()
            logger.info("全局调度器已启动")
        except Exception as e:
            logger.error("启动全局调度器失败: %s", e, exc_info=True)

        try:
            from src.orchestration.recovery import restore_tasks_on_startup

            recovery_result = await restore_tasks_on_startup(
                session_factory=self.db_manager.session_factory,
            )
            logger.info(
                f"任务恢复完成 | "
                f"pending={recovery_result.pending_restored} | "
                f"running={recovery_result.running_restored} | "
                f"total={recovery_result.total_restored()} | "
                f"duration={recovery_result.duration_ms}ms"
            )
        except Exception as e:
            logger.error("恢复任务失败: %s", e, exc_info=True)

        try:
            from src.core.event_bus import get_event_bus
            from src.tasks.scheduler_service import TaskSchedulerService

            event_bus = get_event_bus()
            self.task_scheduler = TaskSchedulerService(event_bus)
            await self.task_scheduler.start()
            logger.info("任务调度服务已启动")
        except Exception as e:
            logger.error("启动任务调度服务失败: %s", e, exc_info=True)

        try:
            from src.agents.task_runner import TaskRunner

            self.task_executor = TaskRunner()
            await self.task_executor.start()
            logger.info("任务执行器已启动")
        except Exception as e:
            logger.error("启动任务执行器失败: %s", e, exc_info=True)

    async def _initialize_sequence_manager(self):
        """
        初始化序列号管理器

        从数据库恢复现有的序列号，确保ID生成的连续性，避免重复。
        """
        try:
            if self.db_manager:
                from src.utils.sequence_manager import get_sequence_manager

                sequence_manager = get_sequence_manager()
                async with self.db_manager.session_factory() as session:
                    await sequence_manager.initialize_from_db(session)
                logger.info("序列号管理器初始化完成")
            else:
                logger.warning("数据库管理器未初始化，跳过序列号管理器初始化")
        except Exception as e:
            logger.error("初始化序列号管理器失败: %s", e, exc_info=True)
            # 不抛出异常，允许应用继续运行

    async def _start_hot_reloader(self):
        """
        启动配置热更新服务

        监听配置文件变化，自动同步到数据库
        """
        try:
            from src.config.hot_reload import init_hot_reloader

            if self.db_manager:
                self.hot_reloader = init_hot_reloader(
                    config_dir="config",
                    session_factory=self.db_manager.session_factory,
                )
                self.hot_reloader.start()
                logger.info("配置热更新服务已启动")
            else:
                logger.warning("数据库管理器未初始化，跳过配置热更新服务")
        except Exception as e:
            logger.error("启动配置热更新服务失败: %s", e, exc_info=True)

    async def _stop_watchdog(self):
        """
        停止 Watchdog 服务
        """
        try:
            from src.services.watchdog_service import get_watchdog_manager

            watchdog_manager = get_watchdog_manager()
            if watchdog_manager and watchdog_manager.is_started():
                await watchdog_manager.stop()
                logger.info("Watchdog 服务已停止")
        except Exception as e:
            logger.error("停止 Watchdog 服务失败: %s", e, exc_info=True)

    async def _stop_hot_reloader(self):
        """
        停止配置热更新服务
        """
        try:
            if self.hot_reloader and self.hot_reloader.is_running():
                self.hot_reloader.stop()
                logger.info("配置热更新服务已停止")
        except Exception as e:
            logger.error("停止配置热更新服务失败: %s", e, exc_info=True)
