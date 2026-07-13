"""外部工具适配器基类。

暴露接口：
- ExternalToolAdapter：标准适配器基类，所有外部工具连接器继承此类
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

from pydantic import ValidationError

from tools.external.exceptions import (
    ConnectionError,
    ExecutionError,
    ExternalTimeoutError,
)
from tools.external.interfaces import IExternalToolAdapter, IExternalToolConnection
from tools.external.types import (
    ExternalToolCapability,
    ExternalToolConfig,
    ExternalToolState,
    RetryPolicy,
)
from tools.types import Tool, ToolCategory, ToolSource

logger = logging.getLogger(__name__)


class ExternalToolAdapter(IExternalToolAdapter):
    """外部工具适配器基类。

    所有外部工具连接器必须继承此类，并实现以下抽象方法：
    - define_schemas(): 定义工具能力
    - _do_execute(): 实际执行逻辑

    此基类提供：
    - 输入验证（基于 Pydantic Schema）
    - 重试策略（指数退避）
    - 超时控制
    - 连接管理委托
    - to_tool() 方法：转换为内部 Tool 对象
    """

    def __init__(
        self,
        config: ExternalToolConfig,
        connection: IExternalToolConnection | None = None,
    ) -> None:
        """初始化适配器。

        Args:
            config: 工具配置
            connection: 连接管理器（可选，可后续注入）
        """
        self._config = config
        self._connection = connection
        self._capabilities: list[ExternalToolCapability] | None = None
        self._logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    @property
    def config(self) -> ExternalToolConfig:
        """获取工具配置。"""
        return self._config

    @property
    def connection(self) -> IExternalToolConnection | None:
        """获取连接管理器。"""
        return self._connection

    @connection.setter
    def connection(self, value: IExternalToolConnection) -> None:
        """设置连接管理器。"""
        self._connection = value

    @property
    def name(self) -> str:
        """获取工具名称。"""
        return self._config.name

    @property
    def state(self) -> ExternalToolState:
        """获取当前连接状态。"""
        if self._connection is None:
            return ExternalToolState.DISCONNECTED
        return self._connection.get_state()

    def get_capabilities(self) -> list[ExternalToolCapability]:
        """获取能力列表（带缓存）。"""
        if self._capabilities is None:
            self._capabilities = self.define_schemas()
        return self._capabilities

    def get_capability(self, operation: str) -> ExternalToolCapability | None:
        """按操作名获取能力描述。

        Args:
            operation: 操作名称

        Returns:
            能力描述，不存在返回 None
        """
        for cap in self.get_capabilities():
            if cap.name == operation:
                return cap
        return None

    def define_schemas(self) -> list[ExternalToolCapability]:
        """定义工具能力。子类必须实现。"""
        raise NotImplementedError("子类必须实现 define_schemas()")

    def validate_input(
        self,
        operation: str,
        inputs: dict[str, Any],
    ) -> dict[str, Any]:
        """验证输入参数。

        基于能力描述中的 input_schema 进行验证。

        Args:
            operation: 操作名称
            inputs: 输入参数

        Returns:
            验证后的输入参数

        Raises:
            ExecutionError: 验证失败
        """
        capability = self.get_capability(operation)
        if capability is None:
            raise ExecutionError(
                message=f"不支持的操作: {operation}",
                tool_name=self.name,
                operation=operation,
            )

        schema = capability.input_schema
        if not schema:
            return inputs

        # 使用 Pydantic 进行基本验证
        try:
            from pydantic import TypeAdapter  # noqa: PLC0415

            adapter = TypeAdapter(dict)
            validated = adapter.validate_python(inputs)
            return validated
        except (ValidationError, Exception) as e:
            raise ExecutionError(
                message=f"输入参数验证失败: {e}",
                tool_name=self.name,
                operation=operation,
            ) from e

    async def execute(
        self,
        operation: str,
        inputs: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """执行操作（含重试逻辑）。

        Args:
            operation: 操作名称
            inputs: 输入参数
            context: 执行上下文

        Returns:
            执行结果

        Raises:
            ExecutionError: 执行失败
            ExternalTimeoutError: 超时
        """
        # 1. 验证输入
        validated_inputs = self.validate_input(operation, inputs)

        # 2. 获取操作超时
        capability = self.get_capability(operation)
        timeout = (
            capability.timeout_override if capability and capability.timeout_override else self._config.execute_timeout
        )

        # 3. 带重试执行
        retry_policy = self._config.retry_policy
        last_error: Exception | None = None

        for attempt in range(retry_policy.max_retries + 1):
            try:
                result = await asyncio.wait_for(
                    self._do_execute(operation, validated_inputs, context),
                    timeout=timeout,
                )
                return result

            except asyncio.TimeoutError:
                last_error = ExternalTimeoutError(
                    message=f"操作 '{operation}' 超时 ({timeout}s)",
                    tool_name=self.name,
                    timeout_seconds=timeout,
                    operation=operation,
                )
                self._logger.warning(
                    "操作超时 | tool=%s | op=%s | attempt=%d/%d | timeout=%ss",
                    self.name,
                    operation,
                    attempt + 1,
                    retry_policy.max_retries + 1,
                    timeout,
                )

            except ConnectionError as e:
                last_error = e
                self._logger.warning(
                    "连接错误 | tool=%s | op=%s | attempt=%d/%d | error=%s",
                    self.name,
                    operation,
                    attempt + 1,
                    retry_policy.max_retries + 1,
                    e,
                )

            except ExecutionError as e:
                last_error = e
                self._logger.warning(
                    "执行错误 | tool=%s | op=%s | attempt=%d/%d | error=%s",
                    self.name,
                    operation,
                    attempt + 1,
                    retry_policy.max_retries + 1,
                    e,
                )

            except Exception as e:
                last_error = ExecutionError(
                    message=f"未预期的执行错误: {e}",
                    tool_name=self.name,
                    operation=operation,
                    cause=e,
                )
                self._logger.error(
                    "未预期错误 | tool=%s | op=%s | error=%s",
                    self.name,
                    operation,
                    e,
                    exc_info=True,
                )

            # 重试前等待（指数退避）
            if attempt < retry_policy.max_retries:
                delay = self._calculate_delay(retry_policy, attempt)
                self._logger.debug(
                    "等待重试 | tool=%s | op=%s | delay=%.2fs",
                    self.name,
                    operation,
                    delay,
                )
                await asyncio.sleep(delay)

        # 所有重试失败，返回错误信息
        if last_error is not None:
            return await self.handle_error(operation, last_error)

        return {"error": "未知错误", "success": False}

    async def _do_execute(
        self,
        operation: str,
        inputs: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """实际执行逻辑，子类必须实现。

        Args:
            operation: 操作名称
            inputs: 已验证的输入参数
            context: 执行上下文

        Returns:
            执行结果
        """
        raise NotImplementedError("子类必须实现 _do_execute()")

    async def handle_error(
        self,
        operation: str,
        error: Exception,
    ) -> dict[str, Any]:
        """处理错误，返回标准化错误信息。

        Args:
            operation: 操作名称
            error: 原始异常

        Returns:
            错误信息字典
        """
        self._logger.error(
            "操作失败 | tool=%s | op=%s | error=%s",
            self.name,
            operation,
            error,
        )
        return {
            "success": False,
            "error": str(error),
            "operation": operation,
            "tool_name": self.name,
        }

    def to_tool(self) -> list[Tool]:
        """将外部工具能力转换为系统内部 Tool 对象列表。

        每个能力对应一个内部 Tool 对象，可通过 ToolRegistry 注册。

        Returns:
            Tool 对象列表
        """
        tools: list[Tool] = []
        capabilities = self.get_capabilities()

        for cap in capabilities:
            tool = Tool(
                name=f"{self._config.name}__{cap.name}",
                description=cap.description or f"{self._config.display_name} - {cap.name}",
                input_schema=cap.input_schema
                or {
                    "type": "object",
                    "properties": {},
                },
                output_schema=cap.output_schema,
                source=ToolSource.HTTP,
                category=ToolCategory.EXECUTION,
                metadata={
                    "external_tool": self._config.name,
                    "operation": cap.name,
                    "requires_sandbox": cap.requires_sandbox,
                    "dangerous": cap.dangerous,
                },
                version=self._config.extra.get("version", "1.0.0"),
            )
            tools.append(tool)

        return tools

    def _calculate_delay(self, policy: RetryPolicy, attempt: int) -> float:
        """计算重试延迟（指数退避 + 随机抖动）。

        Args:
            policy: 重试策略
            attempt: 当前重试次数（从0开始）

        Returns:
            延迟秒数
        """
        delay = min(
            policy.base_delay * (policy.exponential_base**attempt),
            policy.max_delay,
        )
        if policy.jitter:
            delay *= random.uniform(0.5, 1.0)
        return delay
