"""Tag 服务。

管理 Tag 的创建、向量化和共现关系。
Tag 通过 embedding_fn 向量化后写入 PG tags 表，
同时以 JSON 文件形式持久化。

暴露接口：
- TagService: Tag 服务
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from memory.types import TagInfo

logger = logging.getLogger(__name__)


class TagService:
    """Tag 服务。

    负责 Tag 的 CRUD、向量化和共现关系管理。

    Attributes:
        _content_store: JSON 内容存储
        _vector_retriever: PG 向量检索器（可选）
        _embedding_fn: 异步嵌入函数
        _tags_dir: Tag JSON 文件目录
        _cache: 内存缓存（name -> TagInfo）
    """

    def __init__(
        self,
        content_store: Any = None,
        vector_retriever: Any = None,
        embedding_fn: Callable[[str], Coroutine[Any, Any, list[float]]] | None = None,
        data_dir: str = "data/memory",
    ) -> None:
        """初始化 Tag 服务。

        Args:
            content_store: JSON 内容存储实例
            vector_retriever: PG 向量检索器实例（可选）
            embedding_fn: 异步嵌入函数（可选）
            data_dir: 数据存储根目录
        """
        self._content_store = content_store
        self._vector_retriever = vector_retriever
        self._embedding_fn = embedding_fn
        self._tags_dir = Path(data_dir) / "tags"
        self._tags_dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, TagInfo] = {}
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        """从磁盘加载已有 Tag。"""
        for f in self._tags_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                tag = TagInfo(
                    id=data.get("id", 0),
                    name=data.get("name", ""),
                    vector=data.get("vector"),
                    frequency=data.get("frequency", 0),
                )
                if tag.name:
                    self._cache[tag.name] = tag
            except Exception as e:
                logger.warning("[TagService] 加载 Tag 失败 | file=%s | error=%s", f, e)
        logger.info("[TagService] 加载完成 | tags=%d", len(self._cache))

    async def get_or_create(self, name: str) -> TagInfo:
        """查找或创建 Tag。

        先查内存缓存，找到则 frequency += 1 并更新；
        未找到则通过 embedding_fn 向量化后写入 PG tags 表 + 写 JSON。

        Args:
            name: Tag 名称

        Returns:
            Tag 信息
        """
        # 查缓存
        if name in self._cache:
            tag = self._cache[name]
            tag.frequency += 1

            # 更新 PG
            if self._vector_retriever and hasattr(self._vector_retriever, "save_tag"):
                try:
                    tag.id = await self._vector_retriever.save_tag(
                        name=name,
                        vector=tag.vector or [],
                        frequency=tag.frequency,
                    )
                except Exception as e:
                    logger.warning("[TagService] 更新 Tag PG 失败 | name=%s | error=%s", name, e)

            # 更新 JSON
            self._save_to_disk(tag)
            return tag

        # 创建新 Tag
        vector: list[float] = []
        if self._embedding_fn:
            try:
                vector = await self._embedding_fn(name)
            except Exception as e:
                logger.warning("[TagService] 生成 Tag 向量失败 | name=%s | error=%s", name, e)

        tag_id = 0
        if self._vector_retriever and hasattr(self._vector_retriever, "save_tag"):
            try:
                tag_id = await self._vector_retriever.save_tag(
                    name=name,
                    vector=vector,
                    frequency=1,
                )
            except Exception as e:
                logger.warning("[TagService] 写入 Tag PG 失败 | name=%s | error=%s", name, e)

        tag = TagInfo(id=tag_id, name=name, vector=vector, frequency=1)
        self._cache[name] = tag
        self._save_to_disk(tag)

        return tag

    async def link_to_memory(
        self,
        memory_id: str,
        memory_type: str,
        keywords: list[str],
    ) -> None:
        """关联 Tag 到记忆。

        对每个 keyword 调用 get_or_create，更新共现关系。

        Args:
            memory_id: 记忆 ID
            memory_type: 记忆类型（episode/chunk 等）
            keywords: 关键词列表
        """
        tag_ids: list[int] = []

        for keyword in keywords:
            try:
                tag = await self.get_or_create(keyword)
                if tag.id:
                    tag_ids.append(tag.id)
            except Exception as e:
                logger.warning("[TagService] 创建 Tag 失败 | keyword=%s | error=%s", keyword, e)

        # 更新共现关系
        if len(tag_ids) >= 2:
            await self._update_cooccurrences(tag_ids)

    async def _update_cooccurrences(self, tag_ids: list[int]) -> None:
        """增量更新共现关系。

        对 tag_ids 中所有两两组合更新共现计数。

        Args:
            tag_ids: Tag ID 列表
        """
        if not self._vector_retriever or not hasattr(self._vector_retriever, "update_cooccurrence"):
            return

        for i in range(len(tag_ids)):
            for j in range(i + 1, len(tag_ids)):
                try:
                    await self._vector_retriever.update_cooccurrence(tag_ids[i], tag_ids[j])
                except Exception as e:
                    logger.warning(
                        "[TagService] 更新共现关系失败 | tag1=%d | tag2=%d | error=%s",
                        tag_ids[i],
                        tag_ids[j],
                        e,
                    )

    async def get_tag(self, name: str) -> TagInfo | None:
        """获取 Tag。

        Args:
            name: Tag 名称

        Returns:
            Tag 信息，不存在则返回 None
        """
        return self._cache.get(name)

    async def list_tags(self, limit: int = 100) -> list[TagInfo]:
        """列出所有 Tag。

        Args:
            limit: 返回数量上限

        Returns:
            Tag 列表（按频率降序）
        """
        tags = sorted(self._cache.values(), key=lambda t: t.frequency, reverse=True)
        return tags[:limit]

    def _save_to_disk(self, tag: TagInfo) -> None:
        """将 Tag 保存到磁盘。

        Args:
            tag: Tag 信息
        """
        safe_name = tag.name.replace("/", "_").replace("\\", "_").replace(":", "_")
        file_path = self._tags_dir / f"{safe_name}.json"
        try:
            data = {
                "id": tag.id,
                "name": tag.name,
                "frequency": tag.frequency,
            }
            file_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error("[TagService] 保存 Tag 失败 | name=%s | error=%s", tag.name, e)
