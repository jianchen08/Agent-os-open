"""JSON 文件存储实现。

MVP 默认的存储后端，使用 JSON 文件持久化记忆数据。
支持 Episode 和 Knowledge 两种记忆类型。

暴露接口：
- JsonMemoryStore: JSON 文件记忆存储
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from memory.ports import IEpisodeStorage, IMemoryStore, ISemanticStorage
from memory.types import Episode, Knowledge, MemoryType, SearchResult

logger = logging.getLogger(__name__)


class JsonMemoryStore(IMemoryStore, IEpisodeStorage, ISemanticStorage):
    """JSON 文件记忆存储。

    实现三个存储接口：IMemoryStore、IEpisodeStorage、ISemanticStorage。
    数据以 JSON 文件形式持久化到磁盘。

    目录结构：
        data_dir/
        ├── episodes/
        │   ├── {episode_id}.json
        │   └── ...
        └── knowledge/
            ├── {knowledge_id}.json
            └── ...

    Attributes:
        _data_dir: 数据目录路径
        _episodes: 内存中的情景记忆缓存
        _knowledge: 内存中的知识缓存
    """

    def __init__(self, data_dir: str = "data/memory") -> None:
        """初始化 JSON 文件存储。

        Args:
            data_dir: 数据存储目录
        """
        self._data_dir = Path(data_dir)
        self._episodes_dir = self._data_dir / "episodes"
        self._knowledge_dir = self._data_dir / "knowledge"

        # 内存缓存
        self._episodes: dict[str, Episode] = {}
        self._knowledge: dict[str, Knowledge] = {}

        # 从磁盘加载数据
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        """从磁盘加载已有数据。"""
        self._episodes_dir.mkdir(parents=True, exist_ok=True)
        self._knowledge_dir.mkdir(parents=True, exist_ok=True)

        # 加载情景记忆
        for f in self._episodes_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                episode = self._dict_to_episode(data)
                self._episodes[episode.id] = episode
            except Exception as e:
                logger.warning("[JsonMemoryStore] 加载情景记忆失败 | file=%s | error=%s", f, e)

        # 加载知识
        for f in self._knowledge_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                knowledge = self._dict_to_knowledge(data)
                self._knowledge[knowledge.id] = knowledge
            except Exception as e:
                logger.warning("[JsonMemoryStore] 加载知识失败 | file=%s | error=%s", f, e)

        logger.info(
            "[JsonMemoryStore] 加载完成 | episodes=%d | knowledge=%d",
            len(self._episodes), len(self._knowledge),
        )

    def _save_episode_to_disk(self, episode: Episode) -> None:
        """将情景记忆保存到磁盘。

        Args:
            episode: 情景记忆实例
        """
        file_path = self._episodes_dir / f"{episode.id}.json"
        try:
            self._episodes_dir.mkdir(parents=True, exist_ok=True)
            file_path.write_text(
                json.dumps(episode.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error("[JsonMemoryStore] 保存情景记忆失败 | id=%s | error=%s", episode.id, e)

    def _save_knowledge_to_disk(self, knowledge: Knowledge) -> None:
        """将知识保存到磁盘。

        Args:
            knowledge: 知识实例
        """
        file_path = self._knowledge_dir / f"{knowledge.id}.json"
        try:
            self._knowledge_dir.mkdir(parents=True, exist_ok=True)
            file_path.write_text(
                json.dumps(knowledge.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error("[JsonMemoryStore] 保存知识失败 | id=%s | error=%s", knowledge.id, e)

    @staticmethod
    def _dict_to_episode(data: dict[str, Any]) -> Episode:
        """从字典创建 Episode 实例。

        Args:
            data: 字典数据

        Returns:
            Episode 实例
        """
        from datetime import datetime, UTC

        created_at = data.get("created_at")
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        elif not isinstance(created_at, datetime):
            created_at = datetime.now(UTC)

        return Episode(
            id=data.get("id", ""),
            user_id=data.get("user_id", ""),
            session_id=data.get("session_id"),
            intent_text=data.get("intent_text", ""),
            intent_vector=data.get("intent_vector"),
            plan_dag=data.get("plan_dag"),
            execution_summary=data.get("execution_summary"),
            evaluation_report=data.get("evaluation_report"),
            final_score=data.get("final_score"),
            tags=data.get("tags", []),
            created_at=created_at,
        )

    @staticmethod
    def _dict_to_knowledge(data: dict[str, Any]) -> Knowledge:
        """从字典创建 Knowledge 实例。

        Args:
            data: 字典数据

        Returns:
            Knowledge 实例
        """
        from datetime import datetime, UTC

        created_at = data.get("created_at")
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        elif not isinstance(created_at, datetime):
            created_at = datetime.now(UTC)

        updated_at = data.get("updated_at")
        if isinstance(updated_at, str):
            updated_at = datetime.fromisoformat(updated_at)

        return Knowledge(
            id=data.get("id", ""),
            user_id=data.get("user_id", ""),
            source_type=data.get("source_type", ""),
            source_id=data.get("source_id"),
            content=data.get("content", ""),
            embedding=data.get("embedding"),
            extra_data=data.get("extra_data"),
            created_at=created_at,
            updated_at=updated_at,
        )

    # ============================================
    # IMemoryStore 接口实现
    # ============================================

    async def save(self, entry: Episode | Knowledge, memory_type: str = "episode") -> str:
        """保存记忆条目。

        Args:
            entry: 记忆条目
            memory_type: 记忆类型

        Returns:
            条目 ID
        """
        if memory_type == "episode" and isinstance(entry, Episode):
            self._episodes[entry.id] = entry
            self._save_episode_to_disk(entry)
            return entry.id
        elif memory_type == "semantic" and isinstance(entry, Knowledge):
            self._knowledge[entry.id] = entry
            self._save_knowledge_to_disk(entry)
            return entry.id
        else:
            raise ValueError(f"不支持的类型组合: memory_type={memory_type}, entry={type(entry)}")

    async def load(
        self, entry_id: str, memory_type: str = "episode",
    ) -> Episode | Knowledge | None:
        """加载记忆条目。

        Args:
            entry_id: 条目 ID
            memory_type: 记忆类型

        Returns:
            记忆条目
        """
        if memory_type == "episode":
            return self._episodes.get(entry_id)
        elif memory_type == "semantic":
            return self._knowledge.get(entry_id)
        return None

    async def delete(self, entry_id: str, memory_type: str = "episode") -> bool:
        """删除记忆条目。

        Args:
            entry_id: 条目 ID
            memory_type: 记忆类型

        Returns:
            是否删除成功
        """
        if memory_type == "episode" and entry_id in self._episodes:
            del self._episodes[entry_id]
            file_path = self._episodes_dir / f"{entry_id}.json"
            if file_path.exists():
                file_path.unlink()
            return True
        elif memory_type == "semantic" and entry_id in self._knowledge:
            del self._knowledge[entry_id]
            file_path = self._knowledge_dir / f"{entry_id}.json"
            if file_path.exists():
                file_path.unlink()
            return True
        return False

    async def search(
        self,
        query: str,
        user_id: str | None = None,
        limit: int = 10,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        """搜索记忆（基于关键词匹配）。

        Args:
            query: 搜索查询
            user_id: 用户 ID
            limit: 返回数量上限
            filters: 过滤条件

        Returns:
            搜索结果列表
        """
        filters = filters or {}
        memory_type = filters.get("memory_type", "all")
        results: list[SearchResult] = []

        query_lower = query.lower()

        if memory_type in ("all", "episode"):
            for ep in self._episodes.values():
                if user_id and ep.user_id != user_id:
                    continue
                # 简单关键词匹配
                score = self._compute_keyword_score(
                    query_lower, [ep.intent_text, ep.execution_summary or ""] + ep.tags,
                )
                if score > 0:
                    results.append(SearchResult(
                        id=ep.id,
                        content=ep.execution_summary or ep.intent_text,
                        score=score,
                        memory_type=MemoryType.EPISODE,
                        metadata={"tags": ep.tags},
                    ))

        if memory_type in ("all", "semantic"):
            for kn in self._knowledge.values():
                if user_id and kn.user_id != user_id:
                    continue
                score = self._compute_keyword_score(
                    query_lower, [kn.content],
                )
                if score > 0:
                    results.append(SearchResult(
                        id=kn.id,
                        content=kn.content,
                        score=score,
                        memory_type=MemoryType.SEMANTIC,
                        metadata=kn.extra_data,
                    ))

        results.sort(key=lambda x: x.score, reverse=True)
        return results[:limit]

    @staticmethod
    def _compute_keyword_score(query: str, texts: list[str]) -> float:
        """计算关键词匹配得分。

        Args:
            query: 查询文本（小写）
            texts: 待匹配文本列表

        Returns:
            匹配得分 (0-1)
        """
        if not query:
            return 0.0

        query_words = query.split()
        if not query_words:
            return 0.0

        combined = " ".join(texts).lower()
        matched = sum(1 for w in query_words if w in combined)
        return matched / len(query_words) if query_words else 0.0

    # ============================================
    # IEpisodeStorage 接口实现
    # ============================================

    async def save_episode(self, episode: Episode) -> str:
        """保存情景记忆。

        Args:
            episode: 情景记忆实例

        Returns:
            条目 ID
        """
        return await self.save(episode, "episode")

    async def get(self, episode_id: str) -> Episode | None:
        """获取情景记忆。

        Args:
            episode_id: 情景记忆 ID

        Returns:
            情景记忆实例
        """
        return self._episodes.get(episode_id)

    async def find_by_user(
        self, user_id: str, limit: int = 20, offset: int = 0,
    ) -> list[Episode]:
        """按用户查找情景记忆。

        Args:
            user_id: 用户 ID
            limit: 返回数量上限
            offset: 偏移量

        Returns:
            情景记忆列表
        """
        episodes = [
            ep for ep in self._episodes.values()
            if ep.user_id == user_id
        ]
        episodes.sort(key=lambda x: x.created_at, reverse=True)
        return episodes[offset:offset + limit]

    async def update(self, episode_id: str, **kwargs: Any) -> bool:
        """更新情景记忆。

        Args:
            episode_id: 情景记忆 ID
            **kwargs: 要更新的字段

        Returns:
            是否更新成功
        """
        episode = self._episodes.get(episode_id)
        if not episode:
            return False

        for key, value in kwargs.items():
            if hasattr(episode, key):
                setattr(episode, key, value)

        self._save_episode_to_disk(episode)
        return True

    async def delete_episode_by_id(self, episode_id: str) -> bool:
        """删除情景记忆。

        Args:
            episode_id: 情景记忆 ID

        Returns:
            是否删除成功
        """
        return await self.delete(episode_id, "episode")

    async def count_by_user(self, user_id: str) -> int:
        """统计用户的情景记忆数量。

        Args:
            user_id: 用户 ID

        Returns:
            记忆数量
        """
        return sum(1 for ep in self._episodes.values() if ep.user_id == user_id)

    # ============================================
    # ISemanticStorage 接口实现
    # ============================================

    async def save_knowledge(self, knowledge: Knowledge) -> str:
        """保存知识。

        Args:
            knowledge: 知识实例

        Returns:
            条目 ID
        """
        return await self.save(knowledge, "semantic")

    async def get_knowledge(self, knowledge_id: str) -> Knowledge | None:
        """获取知识。

        Args:
            knowledge_id: 知识 ID

        Returns:
            知识实例
        """
        return self._knowledge.get(knowledge_id)

    async def find_knowledge_by_user(
        self, user_id: str, limit: int = 20,
    ) -> list[Knowledge]:
        """按用户查找知识。

        Args:
            user_id: 用户 ID
            limit: 返回数量上限

        Returns:
            知识列表
        """
        knowledge = [
            kn for kn in self._knowledge.values()
            if kn.user_id == user_id
        ]
        knowledge.sort(key=lambda x: x.created_at, reverse=True)
        return knowledge[:limit]

    async def update_embedding(
        self, knowledge_id: str, embedding: list[float],
    ) -> bool:
        """更新知识的向量嵌入。

        Args:
            knowledge_id: 知识 ID
            embedding: 向量嵌入

        Returns:
            是否更新成功
        """
        knowledge = self._knowledge.get(knowledge_id)
        if not knowledge:
            return False

        knowledge.embedding = embedding
        self._save_knowledge_to_disk(knowledge)
        return True

    async def delete_knowledge_by_id(self, knowledge_id: str) -> bool:
        """删除知识。

        Args:
            knowledge_id: 知识 ID

        Returns:
            是否删除成功
        """
        return await self.delete(knowledge_id, "semantic")

    # IEpisodeStorage.delete 兼容
    async def delete(self, entry_id: str, memory_type: str = "episode") -> bool:
        """删除记忆条目。

        Args:
            entry_id: 条目 ID
            memory_type: 记忆类型

        Returns:
            是否删除成功
        """
        if memory_type == "episode" and entry_id in self._episodes:
            del self._episodes[entry_id]
            file_path = self._episodes_dir / f"{entry_id}.json"
            if file_path.exists():
                file_path.unlink()
            return True
        elif memory_type == "semantic" and entry_id in self._knowledge:
            del self._knowledge[entry_id]
            file_path = self._knowledge_dir / f"{entry_id}.json"
            if file_path.exists():
                file_path.unlink()
            return True
        return False
