"""
文件系统存储适配器（预留实现 - 未完成）

状态: 未实现
选择此存储后端将抛出 NotImplementedError

提供基于文件系统的记忆存储，适用于简单部署和开发环境。
如需使用，请实现以下功能：
- 使用 JSON 文件存储
- 支持按用户/会话分目录
- 实现简单的索引机制加速查询
"""

import uuid
from pathlib import Path
from typing import Any

from src.memory.ports import (
    IEpisodeStorage,
    ISemanticStorage,
)
from src.memory.types import Episode, Knowledge, SearchResult


class FileEpisodeStorage(IEpisodeStorage):
    """
    情景记忆文件系统存储实现（预留）

    特性：
    - 无需数据库，易于部署
    - 支持 JSON 序列化
    - 适合开发和测试环境

    TODO: 实现细节
    - 使用 JSON 文件存储
    - 支持按用户/会话分目录
    - 实现简单的索引机制加速查询
    """

    def __init__(self, base_path: str = "./data/memory", compression: bool = False):
        """
        初始化文件存储

        Args:
            base_path: 基础存储路径
            compression: 是否启用压缩（使用 gzip）
        """
        self.base_path = Path(base_path)
        self.compression = compression
        # TODO: 创建目录结构

    async def save(self, episode: Episode) -> str:
        """保存情景记忆"""
        # TODO: 实现 JSON 文件存储
        raise NotImplementedError("文件存储尚未实现")

    async def get(self, episode_id: uuid.UUID) -> Episode | None:
        """获取情景记忆"""
        # TODO: 实现 JSON 文件读取
        raise NotImplementedError("文件存储尚未实现")

    async def find_by_user(
        self,
        user_id: uuid.UUID,
        limit: int = 20,
        offset: int = 0,
    ) -> list[Episode]:
        """按用户查找情景记忆"""
        # TODO: 实现文件扫描
        raise NotImplementedError("文件存储尚未实现")

    async def search(
        self,
        query: str,
        user_id: uuid.UUID,
        limit: int = 10,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        """搜索情景记忆"""
        # TODO: 实现简单的文本搜索
        raise NotImplementedError("文件存储尚未实现")

    async def update(
        self,
        episode_id: uuid.UUID,
        **kwargs,
    ) -> bool:
        """更新情景记忆"""
        # TODO: 实现 JSON 文件更新
        raise NotImplementedError("文件存储尚未实现")

    async def delete(self, episode_id: uuid.UUID) -> bool:
        """删除情景记忆"""
        # TODO: 实现文件删除
        raise NotImplementedError("文件存储尚未实现")

    async def count_by_user(self, user_id: uuid.UUID) -> int:
        """统计用户的情景记忆数量"""
        # TODO: 实现文件计数
        raise NotImplementedError("文件存储尚未实现")


class FileSemanticStorage(ISemanticStorage):
    """
    语义记忆文件系统存储实现（预留）

    TODO: 实现细节
    - 使用 JSON 文件存储
    - 支持向量索引（使用 faiss 或 hnswlib）
    """

    def __init__(self, base_path: str = "./data/memory/knowledge"):
        """
        初始化文件存储

        Args:
            base_path: 基础存储路径
        """
        self.base_path = Path(base_path)

    async def save(self, knowledge: Knowledge) -> str:
        """保存知识"""
        raise NotImplementedError("文件存储尚未实现")

    async def get(self, knowledge_id: uuid.UUID) -> Knowledge | None:
        """获取知识"""
        raise NotImplementedError("文件存储尚未实现")

    async def find_by_user(
        self,
        user_id: uuid.UUID,
        limit: int = 20,
    ) -> list[Knowledge]:
        """按用户查找知识"""
        raise NotImplementedError("文件存储尚未实现")

    async def search(
        self,
        query: str,
        user_id: uuid.UUID,
        limit: int = 10,
        domain: str | None = None,
    ) -> list[SearchResult]:
        """搜索知识"""
        raise NotImplementedError("文件存储尚未实现")

    async def update_embedding(
        self,
        knowledge_id: uuid.UUID,
        embedding: list[float],
    ) -> bool:
        """更新知识的向量嵌入"""
        raise NotImplementedError("文件存储尚未实现")

    async def delete(self, knowledge_id: uuid.UUID) -> bool:
        """删除知识"""
        raise NotImplementedError("文件存储尚未实现")
