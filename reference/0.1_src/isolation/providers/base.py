"""
隔离提供者抽象基类

暴露接口：
- get_level(self) -> IsolationLevel：get_level功能
- IsolationProvider：IsolationProvider类
"""

from abc import ABC, abstractmethod
from typing import Any

from isolation.types import (
    EnvironmentStatus,
    ExecutionResult,
    IsolationContext,
    IsolationEnvironment,
    IsolationLevel,
)


class IsolationProvider(ABC):
    """隔离提供者抽象基类

    所有隔离提供者必须实现此接口，确保提供者之间的可替换性
    """

    @abstractmethod
    def get_level(self) -> IsolationLevel:
        """获取支持的隔离级别"""

    @abstractmethod
    async def is_available(self) -> tuple[bool, str | None]:
        """检查提供者是否可用"""

    @abstractmethod
    async def create_environment(self, context: IsolationContext) -> IsolationEnvironment:
        """创建隔离环境"""

    @abstractmethod
    async def destroy_environment(self, env_id: str, success: bool = True) -> bool:
        """销毁隔离环境。

        返回是否真正从底层（如 docker）删除成功。runc 卡死等故障下删除可能
        失败，调用方据此决定是否走重建兜底（避免内存记录与底层状态脱节）。
        """

    @abstractmethod
    async def execute_in_environment(self, env_id: str, operation: dict[str, Any]) -> ExecutionResult:
        """在隔离环境中执行操作"""

    @abstractmethod
    async def get_environment_status(self, env_id: str) -> EnvironmentStatus:
        """获取环境状态"""

    async def health_check(self) -> tuple[bool, str | None]:
        """健康检查"""
        return await self.is_available()
