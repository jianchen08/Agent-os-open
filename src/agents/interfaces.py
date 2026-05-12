"""
Agent 模块接口定义

定义 AgentLoop 依赖的抽象接口，支持依赖注入
"""

from abc import ABC, abstractmethod
from typing import Any


class IRetriever(ABC):
    """记忆检索器接口"""

    @abstractmethod
    async def retrieve(
        self,
        query: str,
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """
        检索相关记忆

        Args:
            query: 查询文本
            top_k: 返回数量
            filters: 过滤条件

        Returns:
            检索结果列表
        """
        ...


class IEmbeddingService(ABC):
    """嵌入服务接口"""

    @abstractmethod
    async def embed_text(self, text: str) -> list[float]:
        """
        生成文本嵌入向量

        Args:
            text: 输入文本

        Returns:
            嵌入向量
        """
        ...

    @abstractmethod
    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """
        批量生成文本嵌入向量

        Args:
            texts: 输入文本列表

        Returns:
            嵌入向量列表
        """
        ...


class IUsageMonitor(ABC):
    """用量监控接口"""

    @abstractmethod
    def record_usage(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        model: str,
    ) -> None:
        """
        记录 Token 使用量

        Args:
            prompt_tokens: 提示词 Token 数
            completion_tokens: 生成 Token 数
            model: 模型名称
        """
        ...

    @abstractmethod
    def check_quota(self) -> bool:
        """
        检查配额是否充足

        Returns:
            是否可以继续使用
        """
        ...

    @abstractmethod
    def get_statistics(self) -> dict[str, Any]:
        """
        获取使用统计

        Returns:
            统计信息字典
        """
        ...


class ITaskProgressManager(ABC):
    """任务进度管理器接口"""

    @abstractmethod
    async def update_progress(
        self,
        task_id: str,
        progress: float,
        status: str,
        message: str | None = None,
    ) -> None:
        """
        更新任务进度

        Args:
            task_id: 任务 ID
            progress: 进度百分比 (0-100)
            status: 状态
            message: 进度消息
        """
        ...

    @abstractmethod
    async def cleanup(self) -> None:
        """清理资源"""
        ...


class ICheckpointer(ABC):
    """检查点管理器接口"""

    @abstractmethod
    async def save(self, state: dict[str, Any], thread_id: str) -> str:
        """
        保存检查点

        Args:
            state: 状态数据
            thread_id: 线程 ID

        Returns:
            检查点 ID
        """
        ...

    @abstractmethod
    async def load(self, checkpoint_id: str) -> dict[str, Any] | None:
        """
        加载检查点

        Args:
            checkpoint_id: 检查点 ID

        Returns:
            状态数据，不存在返回 None
        """
        ...
