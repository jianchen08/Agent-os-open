"""记忆检索器端口接口。

从 src/memory/ports.py 精简搬迁：仅保留 sidecar 所需的 IRetriever 接口与存储错误，
import 改为本地相对引用。

暴露接口：
- IRetriever: 统一检索接口
- StorageError: 存储错误基类
- EmbeddingUnavailableError: embedding 不可用错误（用于上层降级 keyword）
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from models import SearchResult


class IRetriever(ABC):
    """统一检索接口。

    定义记忆检索的标准接口，由 memory.search 工具的检索分派逻辑调用。
    """

    @abstractmethod
    async def retrieve(
        self,
        query: str,
        user_id: str | None = None,
        top_k: int = 5,
        memory_type: str = "semantic",
        filters: dict | None = None,
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


class StorageError(Exception):
    """存储错误基类。"""


class EmbeddingUnavailableError(StorageError):
    """embedding 不可用错误。

    当 embedding API key 未配置或调用失败时抛出，
    上层 memory.search 捕获后降级为 keyword 检索。
    """
