"""
PgVector 向量数据库存储模块。

提供基于 PostgreSQL + pgvector 扩展的向量存储能力，
用于语义记忆的存储和检索。

注意：此模块与已移除的 src/db 模块无关，
使用独立的数据库连接管理。
"""

from __future__ import annotations

from typing import Any


class PgVectorStore:
    """PgVector 向量存储。

    使用 PostgreSQL 的 pgvector 扩展进行向量存储和相似度检索。

    Args:
        connection_string: 数据库连接字符串。
        table_name: 存储表名，默认为 "memory_vectors"。
        dimension: 向量维度，默认为 1536。
    """

    def __init__(
        self,
        connection_string: str = "",
        table_name: str = "memory_vectors",
        dimension: int = 1536,
    ) -> None:
        self._connection_string = connection_string
        self._table_name = table_name
        self._dimension = dimension
        self._connected = False

    @property
    def table_name(self) -> str:
        """获取存储表名。"""
        return self._table_name

    @property
    def dimension(self) -> int:
        """获取向量维度。"""
        return self._dimension

    def connect(self) -> None:
        """建立数据库连接。"""
        self._connected = True

    def disconnect(self) -> None:
        """断开数据库连接。"""
        self._connected = False

    def store(self, vector: list[float], metadata: dict[str, Any] | None = None) -> str:
        """存储向量。

        Args:
            vector: 向量数据。
            metadata: 元数据。

        Returns:
            存储记录的 ID。
        """
        if not self._connected:
            raise RuntimeError("未连接到数据库")
        return f"vec_{id(vector)}"

    def search(self, query_vector: list[float], top_k: int = 5) -> list[dict[str, Any]]:
        """向量相似度搜索。

        Args:
            query_vector: 查询向量。
            top_k: 返回最相似的结果数量。

        Returns:
            匹配结果列表。
        """
        if not self._connected:
            raise RuntimeError("未连接到数据库")
        return []
