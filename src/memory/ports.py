"""记忆模块存储抽象接口（端口）。

从旧代码 src/memory/ports.py 搬迁，保持接口签名不变，
将 UUID 类型改为 str 以简化使用。

暴露接口：
- IMemoryStore: 统一记忆存储接口
- IRetriever: 统一检索接口
- IEpisodeStorage: 情景记忆存储接口
- ISemanticStorage: 语义记忆存储接口
- StorageError: 存储错误基类
- EpisodeNotFoundError: 情景记忆不存在错误
- KnowledgeNotFoundError: 知识不存在错误
- StorageConnectionError: 存储连接错误
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from memory.types import Episode, Knowledge, SearchResult


class IMemoryStore(ABC):
    """统一记忆存储接口。

    定义所有记忆类型的通用存储操作，支持多种存储后端：
    - JSON 文件（MVP 默认）
    - pgvector（可选）
    - 内存（测试用）
    """

    @abstractmethod
    async def save(self, entry: Episode | Knowledge, memory_type: str = "episode") -> str:
        """保存记忆条目。

        Args:
            entry: 记忆条目（Episode 或 Knowledge）
            memory_type: 记忆类型

        Returns:
            保存的条目 ID
        """

    @abstractmethod
    async def load(self, entry_id: str, memory_type: str = "episode") -> Episode | Knowledge | None:
        """加载记忆条目。

        Args:
            entry_id: 条目 ID
            memory_type: 记忆类型

        Returns:
            记忆条目，不存在则返回 None
        """

    @abstractmethod
    async def delete(self, entry_id: str, memory_type: str = "episode") -> bool:
        """删除记忆条目。

        Args:
            entry_id: 条目 ID
            memory_type: 记忆类型

        Returns:
            是否删除成功
        """

    @abstractmethod
    async def search(
        self,
        query: str,
        user_id: str | None = None,
        limit: int = 10,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        """搜索记忆。

        Args:
            query: 搜索查询
            user_id: 用户 ID
            limit: 返回数量上限
            filters: 过滤条件

        Returns:
            搜索结果列表
        """


class IRetriever(ABC):
    """统一检索接口。

    定义记忆检索的标准接口，由 MemoryService 的检索逻辑调用。
    """

    @abstractmethod
    async def retrieve(
        self,
        query: str,
        user_id: str | None = None,
        top_k: int = 5,
        memory_type: str = "semantic",
        filters: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        """检索相关记忆。

        Args:
            query: 查询文本
            user_id: 用户 ID
            top_k: 返回数量
            memory_type: 记忆类型
            filters: 额外过滤条件

        Returns:
            搜索结果列表
        """


class IEpisodeStorage(ABC):
    """情景记忆存储接口。

    定义情景记忆的存储操作，支持多种存储后端。
    """

    @abstractmethod
    async def save(self, episode: Episode) -> str:
        """保存情景记忆。

        Args:
            episode: 情景记忆实例

        Returns:
            保存的条目 ID
        """

    @abstractmethod
    async def get(self, episode_id: str) -> Episode | None:
        """获取情景记忆。

        Args:
            episode_id: 情景记忆 ID

        Returns:
            情景记忆实例，不存在则返回 None
        """

    @abstractmethod
    async def find_by_user(
        self,
        user_id: str,
        limit: int = 20,
        offset: int = 0,
    ) -> list[Episode]:
        """按用户查找情景记忆。

        Args:
            user_id: 用户 ID
            limit: 返回数量上限
            offset: 偏移量

        Returns:
            情景记忆列表
        """

    @abstractmethod
    async def update(
        self,
        episode_id: str,
        **kwargs: Any,
    ) -> bool:
        """更新情景记忆。

        Args:
            episode_id: 情景记忆 ID
            **kwargs: 要更新的字段

        Returns:
            是否更新成功
        """

    @abstractmethod
    async def delete(self, episode_id: str) -> bool:
        """删除情景记忆。

        Args:
            episode_id: 情景记忆 ID

        Returns:
            是否删除成功
        """

    @abstractmethod
    async def count_by_user(self, user_id: str) -> int:
        """统计用户的情景记忆数量。

        Args:
            user_id: 用户 ID

        Returns:
            记忆数量
        """


class ISemanticStorage(ABC):
    """语义记忆存储接口。

    定义语义记忆（知识）的存储操作。
    """

    @abstractmethod
    async def save(self, knowledge: Knowledge) -> str:
        """保存知识。

        Args:
            knowledge: 知识实例

        Returns:
            保存的条目 ID
        """

    @abstractmethod
    async def get(self, knowledge_id: str) -> Knowledge | None:
        """获取知识。

        Args:
            knowledge_id: 知识 ID

        Returns:
            知识实例，不存在则返回 None
        """

    @abstractmethod
    async def find_by_user(
        self,
        user_id: str,
        limit: int = 20,
    ) -> list[Knowledge]:
        """按用户查找知识。

        Args:
            user_id: 用户 ID
            limit: 返回数量上限

        Returns:
            知识列表
        """

    @abstractmethod
    async def update_embedding(
        self,
        knowledge_id: str,
        embedding: list[float],
    ) -> bool:
        """更新知识的向量嵌入。

        Args:
            knowledge_id: 知识 ID
            embedding: 向量嵌入

        Returns:
            是否更新成功
        """

    @abstractmethod
    async def delete(self, knowledge_id: str) -> bool:
        """删除知识。

        Args:
            knowledge_id: 知识 ID

        Returns:
            是否删除成功
        """


class StorageError(Exception):
    """存储错误基类。

    所有存储操作抛出的异常都应继承此类。

    Attributes:
        message: 错误消息
        details: 错误详情
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        """初始化存储错误。

        Args:
            message: 错误消息
            details: 错误详情
        """
        self.message = message
        self.details = details or {}
        super().__init__(self.message)


class EpisodeNotFoundError(StorageError):
    """情景记忆不存在错误。"""


class KnowledgeNotFoundError(StorageError):
    """知识不存在错误。"""


class StorageConnectionError(StorageError):
    """存储连接错误。"""
