"""
文件存储抽象

暴露接口：
- IFileStorage：IFileStorage类
- LocalFileStorage：LocalFileStorage类
- StorageError：StorageError类
"""

from abc import ABC, abstractmethod
from typing import Any


class IFileStorage(ABC):
    """
    文件存储接口

    定义文件存储的通用接口，支持不同存储后端的实现（本地、云存储等）。

    子类需要实现:
        - save(): 保存文件元数据和内容
        - load(): 加载文件元数据和内容
        - delete(): 删除文件
        - exists(): 检查文件是否存在
    """

    @abstractmethod
    async def save(self, file_id: str, data: Any) -> None:
        """保存文件"""
        pass

    @abstractmethod
    async def load(self, file_id: str) -> Any | None:
        """加载文件"""
        pass

    @abstractmethod
    async def delete(self, file_id: str) -> bool:
        """删除文件"""
        pass

    @abstractmethod
    async def exists(self, file_id: str) -> bool:
        """检查文件是否存在"""
        pass


class LocalFileStorage(IFileStorage):
    """
    本地文件存储实现

    使用内存缓存存储文件元数据和内容，适用于单机部署场景。

    特点:
        - 快速访问（内存存储）
        - 无持久化（重启后数据丢失）
        - 适合小规模使用

    Attributes:
        _cache: 内存缓存字典，存储文件ID到数据的映射

    Example:
        >>> storage = LocalFileStorage()
        >>> await storage.save("file-123", attachment_info)
        >>> attachment = await storage.load("file-123")
    """

    def __init__(self) -> None:
        """初始化本地文件存储"""
        self._cache: dict[str, Any] = {}

    async def save(self, file_id: str, data: Any) -> None:
        """保存文件到内存缓存"""
        self._cache[file_id] = data

    async def load(self, file_id: str) -> Any | None:
        """从内存缓存加载文件"""
        return self._cache.get(file_id)

    async def delete(self, file_id: str) -> bool:
        """从内存缓存删除文件"""
        if file_id in self._cache:
            del self._cache[file_id]
            return True
        return False

    async def exists(self, file_id: str) -> bool:
        """检查文件是否存在于内存缓存中"""
        return file_id in self._cache

    async def clear(self) -> None:
        """
        清空所有缓存

        删除所有存储的文件数据。

        Example:
            >>> await storage.clear()
        """
        self._cache.clear()

    async def list_files(self) -> list[str]:
        """列出所有文件ID"""
        return list(self._cache.keys())

    async def count(self) -> int:
        """统计文件数量"""
        return len(self._cache)


class StorageError(Exception):
    """
    存储错误异常

    当文件存储操作失败时抛出。

    Attributes:
        message: 错误消息
        file_id: 相关的文件ID（可选）

    Example:
        >>> raise StorageError("保存文件失败", file_id="file-123")
    """

    def __init__(self, message: str, file_id: str | None = None) -> None:
        """初始化存储错误"""
        self.message = message
        self.file_id = file_id
        super().__init__(self.message)

    def __str__(self) -> str:
        """返回错误字符串表示"""
        if self.file_id:
            return f"StorageError: {self.message} (file_id={self.file_id})"
        return f"StorageError: {self.message}"
