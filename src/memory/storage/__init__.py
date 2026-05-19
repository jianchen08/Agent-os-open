"""记忆存储后端。

提供两种存储实现：
- JsonMemoryStore: JSON 文件存储（MVP 默认）
- PgVectorRetriever: pgvector 纯向量检索器（可选，需安装 sqlalchemy + psycopg2）

已废弃：
- PgVectorStore: pgvector 全文+向量双存（已迁移为 PgVectorRetriever）

使用 JsonMemoryStore 作为默认后端：
    from memory.storage import JsonMemoryStore
    store = JsonMemoryStore(data_dir="data/memory")
"""

from __future__ import annotations

from memory.storage.json_store import JsonMemoryStore

__all__ = ["JsonMemoryStore"]

# pgvector 向量检索器可选导入
try:
    from memory.storage.pgvector_retriever import PgVectorRetriever as PgVectorRetriever
    __all__.append("PgVectorRetriever")
except ImportError:
    pass

# 向后兼容：PgVectorStore 标记为 deprecated
try:
    from memory.storage.pgvector_store import PgVectorStore as PgVectorStore
    __all__.append("PgVectorStore")
except ImportError:
    pass
