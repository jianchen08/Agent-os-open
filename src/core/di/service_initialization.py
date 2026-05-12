"""
Service Initialization

Initializes and registers all application services in the DI container.
This module is responsible for setting up the dependency injection graph
during application startup.
"""

import logging

from src.core.di import Container

logger = logging.getLogger(__name__)


async def register_all_services(container: Container) -> None:
    """
    Register all application services in the DI container

    This should be called during application startup to set up the
    complete dependency injection graph.

    Args:
        container: The DI container instance
    """
    logger.info("Registering services in DI container...")

    # Register configuration services first (no dependencies)
    await _register_configuration_services(container)

    # Register infrastructure services (depend on config)
    await _register_infrastructure_services(container)

    # Register core services (depend on infrastructure)
    await _register_core_services(container)

    # Register application services (depend on core services)
    await _register_application_services(container)

    logger.info(f"All services registered: {container.list_services()}")


async def _register_configuration_services(container: Container) -> None:
    """
    Register configuration services

    These services have no dependencies and should be registered first.
    """
    logger.info("Registering configuration services...")

    # LLM Configuration
    from src.config.llm_config import LLMConfigManager

    container.register_singleton(
        "llm_config", LLMConfigManager, factory=lambda c: LLMConfigManager()
    )

    # System Configuration
    from src.config.system_config import SystemConfigManager

    container.register_singleton(
        "system_config", SystemConfigManager, factory=lambda c: SystemConfigManager()
    )

    # API Configuration
    from src.config.api_config import APIConfigManager

    container.register_singleton(
        "api_config", APIConfigManager, factory=lambda c: APIConfigManager()
    )

    logger.info("Configuration services registered")


async def _register_infrastructure_services(container: Container) -> None:
    """
    Register infrastructure services

    These services provide core infrastructure like database, cache, etc.
    """
    logger.info("Registering infrastructure services...")

    # Database Manager
    from src.db.connection import get_db_manager

    try:
        db_manager = get_db_manager()
        container.register_instance("db_manager", db_manager)
        logger.info("Database manager registered")
    except Exception as e:
        logger.warning(f"Failed to register database manager: {e}")

    # Redis Manager (optional)
    try:
        from src.cache.redis_manager import get_redis_manager

        redis_manager = get_redis_manager()
        container.register_instance("redis_manager", redis_manager)
        logger.info("Redis manager registered")
    except Exception as e:
        logger.debug(f"Redis manager not available: {e}")

    # Multi-level Cache
    try:
        from src.cache.multi_level_cache import MultiLevelCache

        container.register_singleton(
            "multi_level_cache",
            MultiLevelCache,
            factory=lambda c: MultiLevelCache(
                redis_manager=c.get("redis_manager") if c.has("redis_manager") else None
            ),
        )
        logger.info("Multi-level cache registered")
    except Exception as e:
        logger.debug(f"Multi-level cache not available: {e}")

    logger.info("Infrastructure services registered")


async def _register_core_services(container: Container) -> None:
    """
    Register core services

    These are fundamental services used throughout the application.
    """
    logger.info("Registering core services...")

    # Tool Registry
    from src.tools.global_registry import create_tool_registry
    from src.tools.registry import ToolRegistry

    container.register_singleton(
        "tool_registry",
        ToolRegistry,
        factory=lambda c: create_tool_registry(
            sync_service=None,  # Will be injected later if needed
            lazy_load=True,
            load_builtin_tools=True,
        ),
    )

    # LLM Factory
    from src.llm.factory import LLMFactory

    container.register_singleton(
        "llm_factory",
        LLMFactory,
        factory=lambda c: LLMFactory(config_manager=c.get("llm_config")),
    )

    # Event Bus
    try:
        from src.core.event_bus.factory import get_event_bus

        event_bus = get_event_bus()
        container.register_instance("event_bus", event_bus)
        logger.info("Event bus registered")
    except Exception as e:
        logger.warning(f"Failed to register event bus: {e}")

    # Token Counter
    from src.core.tokenizer import TokenCounter

    container.register_singleton(
        "token_counter", TokenCounter, factory=lambda c: TokenCounter()
    )

    # Retry Handler
    from src.llm.retry import RetryHandler

    container.register_singleton(
        "retry_handler", RetryHandler, factory=lambda c: RetryHandler()
    )

    # Scheduler
    from src.orchestration.scheduler import Scheduler

    container.register_singleton(
        "scheduler",
        Scheduler,
        factory=lambda c: Scheduler(),
    )

    # Session Manager
    from src.core.memory_session_manager import SessionManager

    container.register_singleton(
        "session_manager",
        SessionManager,
        factory=lambda c: SessionManager(),
    )

    # Kernel
    try:
        from src.core.kernel import Kernel

        container.register_singleton(
            "kernel",
            Kernel,
            factory=lambda c: Kernel(
                llm_factory=c.get("llm_factory"),
                tool_registry=c.get("tool_registry"),
            ),
        )
        logger.info("Kernel registered")
    except Exception as e:
        logger.warning(f"Failed to register kernel: {e}")

    logger.info("Core services registered")


async def _register_application_services(container: Container) -> None:
    """
    Register application services

    These are high-level services that provide business logic.
    """
    logger.info("Registering application services...")

    # Chat Service
    try:
        from src.api.services.chat_service import ChatService

        container.register_singleton(
            "chat_service",
            ChatService,
            factory=lambda c: ChatService(
                llm_factory=c.get("llm_factory"),
                tool_registry=c.get("tool_registry"),
            ),
        )
        logger.info("Chat service registered")
    except Exception as e:
        logger.warning(f"Failed to register chat service: {e}")

    # Tool Service
    try:
        from src.services.tool_service import ToolService

        container.register_singleton(
            "tool_service",
            ToolService,
            factory=lambda c: ToolService(
                tool_registry=c.get("tool_registry"),
            ),
        )
        logger.info("Tool service registered")
    except Exception as e:
        logger.warning(f"Failed to register tool service: {e}")

    # Agent Executor
    try:
        from src.orchestration.agent_executor import AgentExecutor

        container.register_singleton(
            "agent_executor",
            AgentExecutor,
            factory=lambda c: AgentExecutor(
                llm_factory=c.get("llm_factory"),
                tool_registry=c.get("tool_registry"),
            ),
        )
        logger.info("Agent executor registered")
    except Exception as e:
        logger.warning(f"Failed to register agent executor: {e}")

    # Memory Factory
    try:
        from src.memory.factory import StorageFactory

        container.register_singleton(
            "memory_factory",
            StorageFactory,
            factory=lambda c: StorageFactory(),
        )
        logger.info("Memory factory registered")
    except Exception as e:
        logger.warning(f"Failed to register memory factory: {e}")

    # Watchdog Service
    try:
        from src.services.watchdog_service import WatchdogServiceManager

        container.register_singleton(
            "watchdog_manager",
            WatchdogServiceManager,
            factory=lambda c: WatchdogServiceManager(
                session_factory=c.get("db_manager").session_factory
            ),
        )
        logger.info("Watchdog manager registered")
    except Exception as e:
        logger.warning(f"Failed to register watchdog manager: {e}")

    # Execution Service - 统一的任务执行入口
    try:
        from src.services.execution_service import ExecutionService

        container.register_singleton(
            "execution_service",
            ExecutionService,
            factory=lambda c: ExecutionService(),
        )
        logger.info("Execution service registered")
    except Exception as e:
        logger.warning(f"Failed to register execution service: {e}")

    # Performance Monitor
    try:
        from src.monitoring.performance_monitor import PerformanceMonitor

        container.register_singleton(
            "performance_monitor",
            PerformanceMonitor,
            factory=lambda c: PerformanceMonitor(),
        )
        logger.info("Performance monitor registered")
    except Exception as e:
        logger.warning(f"Failed to register performance monitor: {e}")

    logger.info("Application services registered")


def create_fastapi_dependency(service_name: str):
    """
    Create a FastAPI dependency function for a service

    Usage:
        @router.get("/tools")
        async def list_tools(
            tool_registry: ToolRegistry = Depends(create_fastapi_dependency("tool_registry"))
        ):
            return tool_registry.list_tools()

    Args:
        service_name: Name of the service in the container

    Returns:
        FastAPI dependency function
    """
    from fastapi import Depends

    def _get_service():
        from src.core.di import get_global_container

        container = get_global_container()
        return container.get(service_name)

    return Depends(_get_service)
