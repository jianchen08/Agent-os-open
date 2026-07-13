"""
文件存储抽象

暴露接口：
- IFileStorage：IFileStorage类
- DiskFileStorage：DiskFileStorage类
- LocalFileStorage：LocalFileStorage类
- StorageError：StorageError类
"""

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from pydantic import BaseModel


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


def _serialize(data: Any) -> dict[str, Any]:
    """将 data 序列化为可 JSON 化的 dict。

    支持 AttachmentInfo（Pydantic 模型）和普通 dict。
    其他类型抛出 StorageError。

    Args:
        data: 要序列化的数据

    Returns:
        可 JSON 序列化的字典

    Raises:
        StorageError: data 类型不支持
    """
    if isinstance(data, BaseModel):
        return data.model_dump(mode="json")
    if isinstance(data, dict):
        return data
    raise StorageError(
        f"不支持的数据类型: {type(data).__name__}，仅支持 BaseModel 或 dict",
    )


def _deserialize(raw: dict[str, Any]) -> Any:
    """从 JSON dict 反序列化。

    若 dict 含 AttachmentInfo 的字段特征，还原为 AttachmentInfo 对象；
    否则直接返回 dict。

    Args:
        raw: 从磁盘读取的 JSON 字典

    Returns:
        AttachmentInfo 或原始 dict
    """
    required_fields = {"file_id", "filename", "mime_type", "size", "media_type"}
    if required_fields.issubset(raw.keys()):
        from multimodal.types import AttachmentInfo  # noqa: PLC0415

        return AttachmentInfo.model_validate(raw)
    return raw


class DiskFileStorage(IFileStorage):
    """磁盘文件存储实现。

    将文件元数据以 JSON 格式持久化到磁盘目录，适用于单机部署场景。
    每个文件存储为 ``{file_id}.json``。

    特点:
        - 持久化存储（重启后数据不丢失）
        - 路径可配置（通过 base_dir 参数）
        - 支持 AttachmentInfo 和 dict 数据

    Attributes:
        _base_dir: 存储根目录路径

    Example:
        >>> storage = DiskFileStorage(base_dir="./data/uploads")
        >>> await storage.save("file-123", attachment_info)
        >>> attachment = await storage.load("file-123")
    """

    def __init__(self, base_dir: str | None = None) -> None:
        """初始化磁盘文件存储。

        Args:
            base_dir: 存储根目录。默认为环境变量 ``MULTIMODAL_STORAGE_DIR``
                      或 ``./data/multimodal``。
        """
        if base_dir is None:
            import os  # noqa: PLC0415

            base_dir = os.environ.get("MULTIMODAL_STORAGE_DIR", "./data/multimodal")
        self._base_dir = Path(base_dir)
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def _meta_path(self, file_id: str) -> Path:
        """获取文件元数据路径。"""
        return self._base_dir / f"{file_id}.json"

    async def save(self, file_id: str, data: Any) -> None:
        """保存文件元数据到磁盘。

        Args:
            file_id: 文件唯一标识
            data: AttachmentInfo 或 dict

        Raises:
            StorageError: data 类型不支持
        """
        serialized = _serialize(data)
        path = self._meta_path(file_id)
        path.write_text(
            json.dumps(serialized, ensure_ascii=False),
            encoding="utf-8",
        )

    async def load(self, file_id: str) -> Any | None:
        """从磁盘加载文件元数据。

        Args:
            file_id: 文件唯一标识

        Returns:
            AttachmentInfo 或 dict，文件不存在时返回 None
        """
        path = self._meta_path(file_id)
        if not path.exists():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        return _deserialize(raw)

    async def delete(self, file_id: str) -> bool:
        """从磁盘删除文件。

        Args:
            file_id: 文件唯一标识

        Returns:
            删除成功返回 True，文件不存在返回 False
        """
        path = self._meta_path(file_id)
        if path.exists():
            path.unlink()
            return True
        return False

    async def exists(self, file_id: str) -> bool:
        """检查文件是否存在于磁盘上。

        Args:
            file_id: 文件唯一标识

        Returns:
            文件存在返回 True
        """
        return self._meta_path(file_id).exists()


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
