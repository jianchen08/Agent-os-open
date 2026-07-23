"""SQLite 向量检索器。

参考 src/memory/storage/pgvector_retriever.py 的 PgVectorRetriever.retrieve() 逻辑，
把 PG pgvector 存储替换为 SQLite + numpy：
- sqlite3 建表存记忆内容和向量（embedding 以 JSON BLOB 存）
- retrieve 时：query 经 embedding_fn 转向量 → numpy 余弦相似度 → top_k 排序
- 同时承载 Tag 索引表（tags / tag_cooccurrences），供 TagNetworkRetriever 读取

embedding_fn 通过 LLMClient.embed_texts 实现，key 缺失时上层降级 keyword。

实现 IRetriever 接口。

暴露接口：
- SqliteVectorStore: SQLite 存储（记忆 + 向量 + Tag 索引）
- VectorRetriever: 向量检索器
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from typing import Any

from models import MemoryType, SearchResult
from ports import IRetriever

logger = logging.getLogger(__name__)


class SqliteVectorStore:
    """SQLite 向量存储。

    持有 sqlite3 连接（check_same_thread=False + Lock 保证并发安全），
    提供记忆 CRUD、向量检索、Tag 索引读写。

    表结构：
    - memories: id, memory_type, user_id, content, metadata, embedding(BLOB JSON), created_at
    - tags: id, name(UNIQUE), vector(BLOB JSON), frequency
    - tag_cooccurrences: tag1_id, tag2_id, cooccurrence_count (PK)

    Attributes:
        _conn: sqlite3 连接
        _lock: 读写锁
    """

    def __init__(self, db_path: str) -> None:
        """初始化存储并建表。

        Args:
            db_path: SQLite 数据库文件路径
        """
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_tables()

    def _init_tables(self) -> None:
        """创建表和索引（幂等）。"""
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    memory_type TEXT NOT NULL DEFAULT 'semantic',
                    user_id TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL DEFAULT '',
                    metadata TEXT NOT NULL DEFAULT '{}',
                    embedding BLOB,
                    created_at REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_memories_type_user
                    ON memories(memory_type, user_id);

                CREATE TABLE IF NOT EXISTS tags (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    vector BLOB,
                    frequency INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS tag_cooccurrences (
                    tag1_id INTEGER NOT NULL,
                    tag2_id INTEGER NOT NULL,
                    cooccurrence_count INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY (tag1_id, tag2_id)
                );
                """
            )
            self._conn.commit()

    def close(self) -> None:
        """关闭连接。"""
        with self._lock:
            self._conn.close()

    # ── 记忆 CRUD ─────────────────────────────────────────

    def save_memory(
        self,
        entry_id: str,
        memory_type: str,
        content: str,
        user_id: str = "",
        metadata: dict[str, Any] | None = None,
        embedding: list[float] | None = None,
        created_at: float = 0.0,
    ) -> None:
        """写入或更新记忆（含向量）。

        Args:
            entry_id: 条目 ID
            memory_type: 记忆类型
            content: 内容文本
            user_id: 用户 ID
            metadata: 元数据
            embedding: 向量嵌入
            created_at: 创建时间戳
        """
        emb_blob = json.dumps(embedding) if embedding else None
        meta_str = json.dumps(metadata or {}, ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO memories (id, memory_type, user_id, content, metadata, embedding, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    memory_type=excluded.memory_type,
                    user_id=excluded.user_id,
                    content=excluded.content,
                    metadata=excluded.metadata,
                    embedding=excluded.embedding,
                    created_at=excluded.created_at
                """,
                (entry_id, memory_type, user_id, content, meta_str, emb_blob, created_at),
            )
            self._conn.commit()

    def list_memories(
        self,
        memory_type: str | None = None,
        user_id: str | None = None,
        limit: int = 100,
        order_desc: bool = True,
    ) -> list[dict[str, Any]]:
        """列出记忆（用于 summarize）。

        Args:
            memory_type: 记忆类型过滤（None 表示全部）
            user_id: 用户过滤
            limit: 返回数量上限
            order_desc: 是否按 created_at 降序（取最近）

        Returns:
            记忆字典列表
        """
        clauses: list[str] = []
        params: list[Any] = []
        if memory_type:
            clauses.append("memory_type = ?")
            params.append(memory_type)
        if user_id:
            clauses.append("user_id = ?")
            params.append(user_id)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order = "DESC" if order_desc else "ASC"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT id, memory_type, user_id, content, metadata, created_at "
                f"FROM memories {where} ORDER BY created_at {order} LIMIT ?",
                params,
            ).fetchall()
        return [
            {
                "id": r["id"],
                "memory_type": r["memory_type"],
                "user_id": r["user_id"],
                "content": r["content"],
                "metadata": json.loads(r["metadata"]) if r["metadata"] else {},
                "timestamp": r["created_at"],
            }
            for r in rows
        ]

    def _fetch_with_vectors(
        self,
        memory_type: str,
        user_id: str | None,
    ) -> list[tuple[dict[str, Any], list[float]]]:
        """读取带向量的记忆（向量检索内部用）。

        Args:
            memory_type: 记忆类型
            user_id: 用户过滤

        Returns:
            [(记忆字典, 向量), ...]，仅含向量非空的条目
        """
        clauses = ["memory_type = ?", "embedding IS NOT NULL"]
        params: list[Any] = [memory_type]
        if user_id:
            clauses.append("user_id = ?")
            params.append(user_id)
        where = " AND ".join(clauses)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT id, content, metadata, embedding FROM memories WHERE {where}",
                params,
            ).fetchall()

        results: list[tuple[dict[str, Any], list[float]]] = []
        for r in rows:
            try:
                vec = json.loads(r["embedding"])
            except (TypeError, ValueError):
                continue
            entry = {
                "id": r["id"],
                "content": r["content"],
                "metadata": json.loads(r["metadata"]) if r["metadata"] else {},
            }
            results.append((entry, vec))
        return results

    def count_memories(
        self,
        memory_type: str | None = None,
        user_id: str | None = None,
    ) -> int:
        """统计记忆数量（COUNT 查询，比 list_memories 高效）。

        Args:
            memory_type: 记忆类型过滤（None 表示全部）
            user_id: 用户过滤

        Returns:
            记忆数量
        """
        clauses: list[str] = []
        params: list[Any] = []
        if memory_type:
            clauses.append("memory_type = ?")
            params.append(memory_type)
        if user_id:
            clauses.append("user_id = ?")
            params.append(user_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            row = self._conn.execute(
                f"SELECT COUNT(*) AS n FROM memories {where}", params
            ).fetchone()
        return int(row["n"]) if row else 0

    # ── Tag 索引（供 TagNetworkRetriever 使用）──────────────

    def save_tag(self, name: str, vector: list[float], frequency: int = 1) -> int:
        """写入或更新 Tag。

        Args:
            name: Tag 名称
            vector: Tag 向量
            frequency: 频率

        Returns:
            Tag ID
        """
        vec_blob = json.dumps(vector)
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO tags (name, vector, frequency)
                VALUES (?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET vector=excluded.vector, frequency=excluded.frequency
                RETURNING id
                """,
                (name, vec_blob, frequency),
            )
            row = cur.fetchone()
            self._conn.commit()
            return int(row["id"]) if row else 0

    def update_cooccurrence(self, tag1_id: int, tag2_id: int) -> None:
        """更新 Tag 共现计数（自增）。

        Args:
            tag1_id: Tag 1 ID
            tag2_id: Tag 2 ID
        """
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO tag_cooccurrences (tag1_id, tag2_id, cooccurrence_count)
                VALUES (?, ?, 1)
                ON CONFLICT(tag1_id, tag2_id) DO UPDATE SET
                    cooccurrence_count = tag_cooccurrences.cooccurrence_count + 1
                """,
                (tag1_id, tag2_id),
            )
            self._conn.commit()

    def load_all_tags(self) -> list[dict[str, Any]]:
        """加载所有 Tag。

        Returns:
            Tag 字典列表 [{"id","name","vector","frequency"}, ...]
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, name, vector, frequency FROM tags"
            ).fetchall()

        tags: list[dict[str, Any]] = []
        for r in rows:
            try:
                vec = json.loads(r["vector"]) if r["vector"] else None
            except (TypeError, ValueError):
                vec = None
            tags.append(
                {"id": int(r["id"]), "name": r["name"], "vector": vec, "frequency": int(r["frequency"])}
            )
        return tags

    def load_cooccurrences(self) -> list[tuple[int, int, int]]:
        """加载所有共现关系。

        Returns:
            [(tag1_id, tag2_id, count), ...]
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT tag1_id, tag2_id, cooccurrence_count FROM tag_cooccurrences"
            ).fetchall()
        return [(int(r["tag1_id"]), int(r["tag2_id"]), int(r["cooccurrence_count"])) for r in rows]


def cosine_similarity(query_vec: list[float], matrix: list[list[float]]) -> list[float]:
    """计算 query 与矩阵每行的余弦相似度（numpy 加速）。

    Args:
        query_vec: 查询向量
        matrix: 候选向量矩阵（每行一条记忆）

    Returns:
        相似度列表，顺序与 matrix 行一致
    """
    try:
        import numpy as np

        q = np.asarray(query_vec, dtype=np.float64)
        m = np.asarray(matrix, dtype=np.float64)
        if m.size == 0:
            return []
        q_norm = np.linalg.norm(q)
        if q_norm < 1e-9:
            return [0.0] * len(matrix)
        norms = np.linalg.norm(m, axis=1)
        safe = np.where(norms < 1e-9, 1.0, norms)
        sims = (m @ q) / (safe * q_norm)
        return [float(max(0.0, min(1.0, s))) for s in sims]
    except ImportError:
        # 纯 Python 降级（numpy 不可用时）
        return _cosine_similarity_pure(query_vec, matrix)


def _cosine_similarity_pure(query_vec: list[float], matrix: list[list[float]]) -> list[float]:
    """纯 Python 余弦相似度（numpy 降级方案）。"""
    import math

    q_norm = math.sqrt(sum(v * v for v in query_vec))
    if q_norm < 1e-9:
        return [0.0] * len(matrix)

    sims: list[float] = []
    for row in matrix:
        r_norm = math.sqrt(sum(v * v for v in row))
        if r_norm < 1e-9:
            sims.append(0.0)
            continue
        dot = sum(q * r for q, r in zip(query_vec, row, strict=False))
        sims.append(float(max(0.0, min(1.0, dot / (r_norm * q_norm)))))
    return sims


class VectorRetriever(IRetriever):
    """SQLite 向量检索器。

    通过 embedding_fn 把 query 转向量，在 SQLite 存储中做 numpy 余弦相似度检索。

    Attributes:
        _store: SQLite 向量存储
        _embed_fn: 同步嵌入函数 texts→vectors（由上层注入 LLMClient.embed_texts）
    """

    def __init__(self, store: SqliteVectorStore, embed_fn: Any) -> None:
        """初始化向量检索器。

        Args:
            store: SQLite 向量存储
            embed_fn: 同步嵌入函数（list[str] -> list[list[float]]）
        """
        self._store = store
        self._embed_fn = embed_fn

    # 暴露给 TagNetworkRetriever 的存储访问
    def load_all_tags(self) -> list[dict[str, Any]]:
        """委托存储读取所有 Tag。"""
        return self._store.load_all_tags()

    def load_cooccurrences(self) -> list[tuple[int, int, int]]:
        """委托存储读取所有共现关系。"""
        return self._store.load_cooccurrences()

    async def retrieve(
        self,
        query: str,
        user_id: str | None = None,
        top_k: int = 5,
        memory_type: str = "semantic",
        filters: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        """向量检索相关记忆。

        Args:
            query: 查询文本
            user_id: 用户 ID
            top_k: 返回数量
            memory_type: 记忆类型
            filters: 额外过滤条件（当前未使用）

        Returns:
            搜索结果列表
        """
        if not query:
            return []

        try:
            query_vectors = self._embed_fn([query])
            if not query_vectors:
                return []
            query_vector = query_vectors[0]
        except Exception as e:
            logger.warning("[VectorRetriever] 生成查询向量失败: %s", e)
            return []

        entries = self._store._fetch_with_vectors(memory_type, user_id)
        if not entries:
            return []

        entry_dicts = [e for e, _ in entries]
        matrix = [vec for _, vec in entries]
        scores = cosine_similarity(query_vector, matrix)

        ranked = sorted(zip(entry_dicts, scores), key=lambda x: x[1], reverse=True)[:top_k]

        results: list[SearchResult] = []
        mem_type_enum = MemoryType.EPISODE if memory_type == "episode" else MemoryType.SEMANTIC
        for entry, score in ranked:
            if score < 1e-6:
                continue
            results.append(
                SearchResult(
                    id=entry["id"],
                    content=entry["content"],
                    score=round(score, 4),
                    memory_type=mem_type_enum,
                    metadata=entry["metadata"],
                )
            )
        return results

    def retrieve_by_vector(
        self,
        query_vector: list[float],
        user_id: str | None = None,
        top_k: int = 5,
        memory_type: str = "semantic",
    ) -> list[SearchResult]:
        """用预计算的查询向量做检索（供 TagNetworkRetriever 二次检索用）。

        Args:
            query_vector: 查询向量（已增强）
            user_id: 用户过滤
            top_k: 返回数量
            memory_type: 记忆类型

        Returns:
            搜索结果列表
        """
        entries = self._store._fetch_with_vectors(memory_type, user_id)
        if not entries:
            return []

        entry_dicts = [e for e, _ in entries]
        matrix = [vec for _, vec in entries]
        scores = cosine_similarity(query_vector, matrix)
        ranked = sorted(zip(entry_dicts, scores), key=lambda x: x[1], reverse=True)[:top_k]

        mem_type_enum = MemoryType.EPISODE if memory_type == "episode" else MemoryType.SEMANTIC
        results: list[SearchResult] = []
        for entry, score in ranked:
            if score < 1e-6:
                continue
            results.append(
                SearchResult(
                    id=entry["id"],
                    content=entry["content"],
                    score=round(score, 4),
                    memory_type=mem_type_enum,
                    metadata=entry["metadata"],
                )
            )
        return results
