"""
隔离工具执行包装器

包装 ToolExecutor，在工具执行前后插入隔离逻辑。
这是隔离系统与工具执行层的集成点。
"""

import logging
from typing import Any

from src.agents.coordinators.isolation_coordinator import IsolationCoordinator
from src.core.results import ToolExecutionResult
from src.tools.executor import ExecutionContext, ToolExecutor

logger = logging.getLogger(__name__)


class IsolationToolWrapper:
    """工具执行包装器

    包装原始的 ToolExecutor，在工具执行前后插入隔离逻辑。
    这是实现低耦合隔离的关键组件。

    执行流程：
    1. pre_execute() - 决策隔离级别，获取/创建环境
    2. execute() - 在隔离环境或宿主机中执行
    3. post_execute() - 更新环境状态

    注意：此类不继承 ToolExecutor，而是包装它。
    """

    def __init__(
        self,
        original_executor: ToolExecutor,
        isolation_coordinator: IsolationCoordinator,
    ):
        """初始化包装器

        Args:
            original_executor: 原始工具执行器
            isolation_coordinator: 隔离协调器
        """
        self._original = original_executor
        self._coordinator = isolation_coordinator

        # 统计信息
        self._execution_count = 0
        self._isolated_count = 0
        self._fallback_count = 0

    @property
    def original_executor(self) -> ToolExecutor:
        """获取原始执行器"""
        return self._original

    @property
    def coordinator(self) -> IsolationCoordinator:
        """获取隔离协调器"""
        return self._coordinator

    async def execute(
        self,
        tool_name: str,
        inputs: dict[str, Any],
        context: ExecutionContext,
        **kwargs,
    ) -> ToolExecutionResult:
        """执行工具（带隔离）

        在工具执行前后插入隔离逻辑：
        1. 执行前：决策隔离级别，获取/创建环境
        2. 执行：在隔离环境或宿主机中执行
        3. 执行后：更新环境状态

        Args:
            tool_name: 工具名称
            inputs: 输入参数
            context: 执行上下文
            **kwargs: 额外参数

        Returns:
            工具执行结果
        """
        self._execution_count += 1

        # 1. 执行前处理
        isolation_ctx = await self._coordinator.pre_execute(
            tool_name=tool_name,
            inputs=inputs,
            context=context,
        )

        # 记录是否使用隔离
        if isolation_ctx.parent_env_id is not None:
            self._isolated_count += 1
            logger.debug(
                f"[IsolationToolWrapper] 使用隔离执行 | "
                f"tool={tool_name}, env_id={isolation_ctx.parent_env_id}"
            )

        try:
            # 2. 执行工具
            result = await self._coordinator.execute(
                tool_name=tool_name,
                inputs=inputs,
                context=context,
                isolation_ctx=isolation_ctx,
                original_executor=self._original.execute,
            )

            # 3. 执行后处理
            await self._coordinator.post_execute(
                tool_name=tool_name,
                context=context,
                result=result,
            )

            return result

        except Exception as e:
            # 记录错误
            logger.error(
                f"[IsolationToolWrapper] 工具执行失败 | tool={tool_name}, error={e}"
            )

            # 返回失败结果
            return ToolExecutionResult.create_failed(
                error=f"工具执行失败: {str(e)}"
            )

    async def __call__(
        self,
        tool_name: str,
        inputs: dict[str, Any],
        context: ExecutionContext,
        **kwargs,
    ) -> ToolExecutionResult:
        """支持直接调用

        使包装器可以直接替代 ToolExecutor 使用
        """
        return await self.execute(tool_name, inputs, context, **kwargs)

    # ==================== 统计方法 ====================

    def get_stats(self) -> dict[str, int]:
        """获取执行统计

        Returns:
            统计信息字典
        """
        return {
            "total_executions": self._execution_count,
            "isolated_executions": self._isolated_count,
            "fallback_executions": self._fallback_count,
            "isolation_rate": (
                self._isolated_count / self._execution_count
                if self._execution_count > 0
                else 0
            ),
        }

    def reset_stats(self) -> None:
        """重置统计信息"""
        self._execution_count = 0
        self._isolated_count = 0
        self._fallback_count = 0

    # ==================== 生命周期管理 ====================

    async def initialize(self) -> None:
        """初始化包装器

        初始化隔离协调器（如果尚未初始化）
        """
        if not self._coordinator._running:
            await self._coordinator.initialize()

    async def cleanup(self) -> None:
        """清理包装器

        清理隔离协调器
        """
        await self._coordinator.cleanup()


# ==================== 工厂函数 ====================


def wrap_executor_with_isolation(
    executor: ToolExecutor,
    coordinator: IsolationCoordinator,
) -> IsolationToolWrapper:
    """包装工具执行器以支持隔离

    Args:
        executor: 原始工具执行器
        coordinator: 隔离协调器

    Returns:
        包装后的工具执行器
    """
    return IsolationToolWrapper(
        original_executor=executor,
        isolation_coordinator=coordinator,
    )
