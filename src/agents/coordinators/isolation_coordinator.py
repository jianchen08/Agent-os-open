"""
隔离协调器 - 负责在工具执行层面集成隔离系统

职责：
1. 决策工具是否需要隔离
2. 管理隔离环境生命周期
3. 在隔离/宿主机间路由执行
4. 处理降级和错误恢复
"""

import logging
from collections.abc import Callable
from datetime import UTC
from typing import Any

from src.agents.coordinators.isolation_config import IsolationConfig, load_config
from src.core.results import ToolExecutionResult
from src.isolation.manager import (
    EnvironmentStatus,
    IsolationContext,
    IsolationLevel,
    IsolationManager,
    OperationType,
    TaskType,
)
from src.tools.executor import ExecutionContext
from src.tools.types import Tool

logger = logging.getLogger(__name__)


class IsolationCoordinator:
    """隔离协调器

    作为隔离系统的统一入口，集成 IsolationManager 和 IsolationDecider。
    在工具执行层面提供隔离能力，实现低耦合的隔离。

    核心功能：
    - should_isolate(): 决策工具是否需要隔离
    - pre_execute(): 执行前处理，获取/创建隔离环境
    - execute(): 在隔离环境或宿主机中执行工具
    - post_execute(): 执行后处理，更新环境状态
    """

    def __init__(
        self,
        config: IsolationConfig | None = None,
        isolation_manager: IsolationManager | None = None,
    ):
        """初始化协调器

        Args:
            config: 隔离配置（可选，未提供时从文件加载）
            isolation_manager: 隔离管理器（可选，未提供时创建默认）
        """
        # 加载配置
        self._config = config or load_config()

        # 创建或使用注入的管理器
        self._manager = isolation_manager or IsolationManager()

        # 工具执行环境缓存: session_id -> env_id
        self._environment_cache: dict[str, str] = {}

        # 工具定义缓存
        self._tool_definitions: dict[str, Tool] = {}

        # 状态
        self._running = False

    @property
    def config(self) -> IsolationConfig:
        """获取配置"""
        return self._config

    @property
    def manager(self) -> IsolationManager:
        """获取隔离管理器"""
        return self._manager

    async def initialize(self) -> None:
        """初始化协调器

        启动隔离管理器，加载配置
        """
        if self._running:
            logger.warning("[IsolationCoordinator] 协调器已经初始化")
            return

        await self._manager.start()
        self._running = True

        logger.info(
            f"[IsolationCoordinator] 协调器已初始化 | "
            f"enabled={self._config.enabled}, "
            f"fallback={self._config.enable_fallback}"
        )

    async def cleanup(self) -> None:
        """清理协调器

        停止隔离管理器，清理缓存
        """
        if not self._running:
            return

        await self._manager.stop()
        self._environment_cache.clear()
        self._tool_definitions.clear()
        self._running = False

        logger.info("[IsolationCoordinator] 协调器已清理")

    # ==================== 决策方法 ====================

    async def should_isolate(
        self,
        tool_name: str,
        context: ExecutionContext,
    ) -> bool:
        """判断工具是否需要隔离

        决策流程：
        1. 检查全局开关
        2. 检查工具白名单
        3. 检查工具黑名单
        4. 检查工具特定策略
        5. 检查工具分类

        Args:
            tool_name: 工具名称
            context: 执行上下文

        Returns:
            是否需要隔离
        """
        # 1. 检查全局开关
        if not self._config.enabled:
            return False

        # 2. 检查工具白名单（不隔离）
        if self._config.is_tool_whitelisted(tool_name):
            return False

        # 3. 检查工具黑名单（强制隔离）
        if self._config.is_tool_blacklisted(tool_name):
            return True

        # 4. 检查工具特定策略
        policy_level = self._config.get_tool_policy(tool_name)
        if policy_level is not None:
            return policy_level != IsolationLevel.HOST

        # 5. 检查工具分类
        tool_def = await self._get_tool_definition(tool_name)
        if tool_def and tool_def.category:
            return self._config.should_isolate_category(tool_def.category)

        # 默认不隔离
        return False

    # ==================== 执行方法 ====================

    async def pre_execute(
        self,
        tool_name: str,
        inputs: dict[str, Any],
        context: ExecutionContext,
    ) -> IsolationContext:
        """执行前处理

        决策隔离级别，获取或创建隔离环境

        Args:
            tool_name: 工具名称
            inputs: 输入参数
            context: 执行上下文

        Returns:
            隔离上下文（包含环境信息）
        """
        # 检查是否需要隔离
        needs_isolation = await self.should_isolate(tool_name, context)

        if not needs_isolation:
            # 不需要隔离，返回宿主机上下文
            return IsolationContext(
                task_id=context.session_id,
                task_type=TaskType.ATOMIC,
                operation_type=None,
                parent_env_id=None,  # 宿主机执行
            )

        # 需要隔离，决策隔离级别并创建环境
        operation_type = self._map_tool_to_operation(tool_name)
        task_type = self._infer_task_type(context)

        # 检查可用的提供者
        available = await self._manager._check_providers_availability()

        # 决策隔离级别
        level = await self._manager._decider.decide(
            task_type=task_type,
            operation_type=operation_type,
            available_providers=available,
        )

        # 获取或创建环境
        parent_env_id = self._environment_cache.get(context.session_id)

        env = await self._manager.get_or_create_environment(
            task_id=f"{context.session_id}:{tool_name}",
            task_type=task_type,
            operation_type=operation_type,
            parent_env_id=parent_env_id,
            metadata={
                "tool_name": tool_name,
                "session_id": context.session_id,
                "user_id": context.user_id,
            },
        )

        # 缓存环境以供后续复用
        if self._config.reuse_environment:
            self._environment_cache[context.session_id] = env.env_id

        logger.debug(
            f"[IsolationCoordinator] pre_execute | "
            f"tool={tool_name}, level={level}, env_id={env.env_id}"
        )

        return env.context

    async def execute(
        self,
        tool_name: str,
        inputs: dict[str, Any],
        context: ExecutionContext,
        isolation_ctx: IsolationContext,
        original_executor: Callable,
    ) -> ToolExecutionResult:
        """执行工具（在隔离环境或宿主机）

        Args:
            tool_name: 工具名称
            inputs: 输入参数
            context: 执行上下文
            isolation_ctx: 隔离上下文
            original_executor: 原始执行器函数

        Returns:
            工具执行结果
        """
        # 判断是否需要隔离
        needs_isolation = isolation_ctx.parent_env_id is not None

        if not needs_isolation:
            # 宿主机直接执行
            return await original_executor(tool_name, inputs, context)

        # 在隔离环境中执行
        try:
            result = await self._manager.execute_in_isolation(
                task_id=isolation_ctx.task_id,
                task_type=isolation_ctx.task_type,
                operation_type=isolation_ctx.operation_type,
                operation={
                    "type": "tool_execution",
                    "tool_name": tool_name,
                    "inputs": inputs,
                },
                parent_env_id=isolation_ctx.parent_env_id,
            )

            return ToolExecutionResult.create_completed(
                output=result.output,
                metadata=result.metadata,
            ) if result.success else ToolExecutionResult.create_failed(
                error=result.error,
                metadata=result.metadata,
            )

        except Exception as e:
            # 隔离执行失败，尝试降级
            if self._config.enable_fallback:
                logger.warning(
                    f"[IsolationCoordinator] 隔离执行失败，降级到宿主机: "
                    f"tool={tool_name}, error={e}"
                )
                return await original_executor(tool_name, inputs, context)
            else:
                # 不启用降级，抛出异常
                raise

    async def post_execute(
        self,
        tool_name: str,
        context: ExecutionContext,
        result: ToolExecutionResult,
    ) -> None:
        """执行后处理

        更新环境使用时间，处理清理

        Args:
            tool_name: 工具名称
            context: 执行上下文
            result: 执行结果
        """
        # 更新环境使用时间
        env_id = self._environment_cache.get(context.session_id)
        if env_id:
            env = await self._manager.get_environment(env_id)
            if env and env.status == EnvironmentStatus.READY:
                from datetime import datetime

                env.last_used_at = datetime.now(UTC).isoformat()

    # ==================== 辅助方法 ====================

    async def _get_tool_definition(self, tool_name: str) -> Tool | None:
        """获取工具定义

        Args:
            tool_name: 工具名称

        Returns:
            工具定义，如果未找到返回 None
        """
        # 先查缓存
        if tool_name in self._tool_definitions:
            return self._tool_definitions[tool_name]

        # 从工具注册表获取
        try:
            from src.tools.registry import get_tool_registry

            registry = get_tool_registry()
            tool = registry.get_tool(tool_name)

            if tool:
                self._tool_definitions[tool_name] = tool

            return tool

        except Exception as e:
            logger.warning(f"[IsolationCoordinator] 获取工具定义失败: {tool_name}, {e}")
            return None

    def _map_tool_to_operation(self, tool_name: str) -> OperationType:
        """将工具映射到操作类型

        Args:
            tool_name: 工具名称

        Returns:
            操作类型
        """
        # 常见工具到操作类型的映射
        mapping = {
            "bash": OperationType.CODE_EXECUTION,
            "shell_execute": OperationType.CODE_EXECUTION,
            "python_execute": OperationType.CODE_EXECUTION,
            "desktop_control": OperationType.DESKTOP_CONTROL,
            "isolation_execute": OperationType.CODE_EXECUTION,
        }

        return mapping.get(tool_name, OperationType.FILE_OPERATION)

    def _infer_task_type(self, context: ExecutionContext) -> TaskType:
        """从上下文推断任务类型

        Args:
            context: 执行上下文

        Returns:
            任务类型
        """
        # 默认使用原子任务类型
        # 可以从上下文中的任务信息推断更准确的类型
        return TaskType.ATOMIC


# ==================== 工厂函数 ====================


def create_isolation_coordinator(
    config: IsolationConfig | None = None,
    config_path: str | None = None,
) -> IsolationCoordinator:
    """创建隔离协调器

    Args:
        config: 配置实例（优先级高于 config_path）
        config_path: 配置文件路径

    Returns:
        隔离协调器实例
    """
    # 优先使用提供的配置
    if config is None and config_path:
        config = load_config(config_path)

    return IsolationCoordinator(config=config)


async def get_isolation_coordinator(
    config_path: str | None = None,
) -> IsolationCoordinator:
    """获取或创建隔离协调器（单例模式）

    Args:
        config_path: 配置文件路径

    Returns:
        隔离协调器实例
    """
    coordinator = create_isolation_coordinator(config_path)
    await coordinator.initialize()
    return coordinator
