"""外部工具标准接口规范。

暴露接口：
- IExternalToolConnection：连接管理接口
- IExternalToolAdapter：工具适配接口
- IExternalToolSandbox：沙箱接口
- ISecretManager：密钥管理接口
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from tools.external.types import (
    ExternalToolCapability,
    ExternalToolState,
    SandboxResourceLimits,
)


class IExternalToolConnection(ABC):
    """外部工具连接管理接口。

    职责：管理外部工具的网络连接生命周期。
    实现：WebSocket 和 HTTP 双协议支持、自动重连、心跳保活。
    """

    @abstractmethod
    async def connect(self) -> None:
        """建立连接。

        Raises:
            ConnectionError: 连接失败时抛出
        """

    @abstractmethod
    async def disconnect(self) -> None:
        """断开连接。

        优雅关闭，等待进行中的操作完成。
        """

    @abstractmethod
    async def health_check(self) -> bool:
        """健康检查。

        Returns:
            连接是否健康
        """

    @abstractmethod
    def get_state(self) -> ExternalToolState:
        """获取当前连接状态。

        Returns:
            当前状态
        """

    @abstractmethod
    async def send_request(
        self,
        operation: str,
        payload: dict[str, Any],
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """发送请求到外部工具。

        Args:
            operation: 操作名称
            payload: 请求参数
            timeout: 超时时间（秒），None 使用默认值

        Returns:
            响应数据

        Raises:
            ExternalTimeoutError: 超时
            ExecutionError: 执行失败
            ConnectionError: 连接异常
        """


class IExternalToolAdapter(ABC):
    """外部工具适配接口。

    职责：定义外部工具的输入/输出 Schema、参数验证、执行逻辑和错误处理。
    每个外部工具连接器必须实现此接口。
    """

    @abstractmethod
    def define_schemas(self) -> list[ExternalToolCapability]:
        """定义工具支持的所有操作及其输入/输出 Schema。

        Returns:
            能力描述列表
        """

    @abstractmethod
    def validate_input(
        self,
        operation: str,
        inputs: dict[str, Any],
    ) -> dict[str, Any]:
        """验证输入参数。

        Args:
            operation: 操作名称
            inputs: 输入参数

        Returns:
            验证后的输入参数

        Raises:
            ExecutionError: 验证失败
        """

    @abstractmethod
    async def execute(
        self,
        operation: str,
        inputs: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """执行操作。

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

    @abstractmethod
    async def handle_error(
        self,
        operation: str,
        error: Exception,
    ) -> dict[str, Any]:
        """处理执行错误。

        Args:
            operation: 操作名称
            error: 原始异常

        Returns:
            错误信息字典（包含 error 字段）
        """


class IExternalToolSandbox(ABC):
    """沙箱执行接口。

    职责：管理隔离执行环境，复用 isolation 模块。
    """

    @abstractmethod
    async def create_sandbox(
        self,
        tool_name: str,
        resource_limits: SandboxResourceLimits | None = None,
    ) -> str:
        """创建沙箱环境。

        Args:
            tool_name: 工具名称
            resource_limits: 资源限制

        Returns:
            沙箱 ID

        Raises:
            SandboxError: 创建失败
        """

    @abstractmethod
    async def execute_in_sandbox(
        self,
        sandbox_id: str,
        command: str,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """在沙箱中执行命令。

        Args:
            sandbox_id: 沙箱 ID
            command: 要执行的命令
            timeout: 超时时间

        Returns:
            执行结果

        Raises:
            SandboxError: 执行失败
            ExternalTimeoutError: 超时
        """

    @abstractmethod
    async def destroy_sandbox(self, sandbox_id: str) -> None:
        """销毁沙箱环境。

        Args:
            sandbox_id: 沙箱 ID
        """


class ISecretManager(ABC):
    """密钥管理接口。

    职责：安全存储和读取密钥，支持加密和轮换。
    """

    @abstractmethod
    async def store_secret(
        self,
        key: str,
        value: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """存储密钥。

        Args:
            key: 密钥标识
            value: 密钥值
            metadata: 元数据（如过期时间）

        Raises:
            SecretError: 存储失败
        """

    @abstractmethod
    async def get_secret(self, key: str) -> str:
        """获取密钥。

        Args:
            key: 密钥标识

        Returns:
            密钥值

        Raises:
            SecretError: 密钥不存在或解密失败
        """

    @abstractmethod
    async def rotate_secret(self, key: str, new_value: str) -> None:
        """轮换密钥。

        Args:
            key: 密钥标识
            new_value: 新密钥值

        Raises:
            SecretError: 轮换失败
        """

    @abstractmethod
    async def delete_secret(self, key: str) -> None:
        """删除密钥。

        Args:
            key: 密钥标识
        """
