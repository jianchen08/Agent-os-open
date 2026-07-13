"""JSON 文件存储实现。

MVP 默认的存储后端，使用 JSON 文件持久化记忆数据。
支持 Episode 和 Knowledge 两种记忆类型。

按需读取模式：启动时只扫描文件名构建 ID 索引，不加载内容到内存。
读写操作直接操作磁盘文件，避免内存占用随数据量增长。

暴露接口：
- JsonMemoryStore: JSON 文件记忆存储
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from memory.ports import IEpisodeStorage, IMemoryStore, IRetriever, ISemanticStorage
from memory.types import Episode, Knowledge, MemoryType, SearchResult

logger = logging.getLogger(__name__)


class JsonMemoryStore(IMemoryStore, IEpisodeStorage, ISemanticStorage, IRetriever):
    """JSON 文件记忆存储。

    实现三个存储接口：IMemoryStore、IEpisodeStorage、ISemanticStorage。
    数据以 JSON 文件形式持久化到磁盘，按需读取，不预加载到内存。

    由于 IEpisodeStorage 和 ISemanticStorage 存在同名方法（save/get/find_by_user/delete），
    统一方法通过 ID 索引自动判断条目类型，实现双接口兼容。

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
        _episode_ids: 情景记忆 ID 索引集合（只存 ID，不存内容）
        _knowledge_ids: 知识 ID 索引集合（只存 ID，不存内容）
    """

    def __init__(self, data_dir: str = "data/memory") -> None:
        """初始化 JSON 文件存储。

        Args:
            data_dir: 数据存储目录
        """
        self._data_dir = Path(data_dir)
        self._episodes_dir = self._data_dir / "episodes"
        self._knowledge_dir = self._data_dir / "knowledge"

        self._episodes_dir.mkdir(parents=True, exist_ok=True)
        self._knowledge_dir.mkdir(parents=True, exist_ok=True)

        self._episode_ids: set[str] = set()
        self._knowledge_ids: set[str] = set()
        self._scan_existing_files()

    def _scan_existing_files(self) -> None:
        """扫描已有文件，构建 ID 索引（不加载内容）。"""
        for f in self._episodes_dir.glob("*.json"):
            self._episode_ids.add(f.stem)
        for f in self._knowledge_dir.glob("*.json"):
            self._knowledge_ids.add(f.stem)
        logger.info(
            "[JsonMemoryStore] 索引扫描完成 | episodes=%d | knowledge=%d",
            len(self._episode_ids),
            len(self._knowledge_ids),
        )

    def _is_episode_id(self, entry_id: str) -> bool:
        """判断 ID 是否属于情景记忆。"""
        return entry_id in self._episode_ids

    def _is_knowledge_id(self, entry_id: str) -> bool:
        """判断 ID 是否属于知识。"""
        return entry_id in self._knowledge_ids

    def _read_episode_from_disk(self, episode_id: str) -> Episode | None:
        """按需从磁盘读取单个情景记忆。

        Args:
            episode_id: 情景记忆 ID

        Returns:
            情景记忆实例，不存在则返回 None
        """
        file_path = self._episodes_dir / f"{episode_id}.json"
        if not file_path.exists():
            return None
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
            return self._dict_to_episode(data)
        except Exception as e:
            logger.warning("[JsonMemoryStore] 读取情景记忆失败 | id=%s | error=%s", episode_id, e)
            return None

    def _read_knowledge_from_disk(self, knowledge_id: str) -> Knowledge | None:
        """按需从磁盘读取单个知识。

        Args:
            knowledge_id: 知识 ID

        Returns:
            知识实例，不存在则返回 None
        """
        file_path = self._knowledge_dir / f"{knowledge_id}.json"
        if not file_path.exists():
            return None
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
            return self._dict_to_knowledge(data)
        except Exception as e:
            logger.warning("[JsonMemoryStore] 读取知识失败 | id=%s | error=%s", knowledge_id, e)
            return None

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

    def _iter_all_episodes(self) -> list[Episode]:
        """遍历所有情景记忆文件并读取。

        Returns:
            情景记忆列表
        """
        episodes = []
        for f in self._episodes_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                episodes.append(self._dict_to_episode(data))
            except Exception as e:
                logger.warning("[JsonMemoryStore] 读取情景记忆失败 | file=%s | error=%s", f, e)
        return episodes

    def _iter_all_knowledge(self) -> list[Knowledge]:
        """遍历所有知识文件并读取。

        Returns:
            知识列表
        """
        knowledge_list = []
        for f in self._knowledge_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                knowledge_list.append(self._dict_to_knowledge(data))
            except Exception as e:
                logger.warning("[JsonMemoryStore] 读取知识失败 | file=%s | error=%s", f, e)
        return knowledge_list

    @staticmethod
    def _dict_to_episode(data: dict[str, Any]) -> Episode:
        """从字典创建 Episode 实例。

        Args:
            data: 字典数据

        Returns:
            Episode 实例
        """
        from datetime import UTC, datetime  # noqa: PLC0415

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
        from datetime import UTC, datetime  # noqa: PLC0415

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
        """保存记忆条目到磁盘并更新索引。

        通过 isinstance 自动推断存储路径，无需依赖 memory_type 参数。

        Args:
            entry: 记忆条目
            memory_type: 记忆类型（保留参数兼容，实际由 entry 类型决定）

        Returns:
            条目 ID
        """
        if isinstance(entry, Episode):
            self._save_episode_to_disk(entry)
            self._episode_ids.add(entry.id)
            return entry.id
        if isinstance(entry, Knowledge):
            self._save_knowledge_to_disk(entry)
            self._knowledge_ids.add(entry.id)
            return entry.id
        raise ValueError(f"不支持的类型: {type(entry)}")

    async def load(
        self,
        entry_id: str,
        memory_type: str = "episode",
    ) -> Episode | Knowledge | None:
        """按需从磁盘加载记忆条目。

        Args:
            entry_id: 条目 ID
            memory_type: 记忆类型

        Returns:
            记忆条目
        """
        if memory_type == "episode":
            return self._read_episode_from_disk(entry_id)
        if memory_type == "semantic":
            return self._read_knowledge_from_disk(entry_id)
        return None

    async def delete(self, entry_id: str, memory_type: str = "episode") -> bool:
        """删除记忆条目（磁盘文件 + 索引）。

        当 memory_type 为默认值 "episode" 但 ID 实际属于 knowledge 时，
        自动回退到 knowledge 删除，确保 ISemanticStorage 接口调用正确。

        Args:
            entry_id: 条目 ID
            memory_type: 记忆类型

        Returns:
            是否删除成功
        """
        if memory_type == "episode" and entry_id in self._episode_ids:
            file_path = self._episodes_dir / f"{entry_id}.json"
            if file_path.exists():
                file_path.unlink()
            self._episode_ids.discard(entry_id)
            return True
        if memory_type == "semantic" and entry_id in self._knowledge_ids or entry_id in self._knowledge_ids:
            file_path = self._knowledge_dir / f"{entry_id}.json"
            if file_path.exists():
                file_path.unlink()
            self._knowledge_ids.discard(entry_id)
            return True
        if entry_id in self._episode_ids:
            file_path = self._episodes_dir / f"{entry_id}.json"
            if file_path.exists():
                file_path.unlink()
            self._episode_ids.discard(entry_id)
            return True
        return False

    async def search(
        self,
        query: str,
        user_id: str | None = None,
        limit: int = 10,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        """搜索记忆（基于关键词匹配，按需读取磁盘文件）。

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
        tags_filter = filters.get("tags")
        results: list[SearchResult] = []

        query_lower = query.lower()

        if memory_type in ("all", "episode"):
            for ep in self._iter_all_episodes():
                if user_id and ep.user_id != user_id:
                    continue
                if tags_filter and not any(t in ep.tags for t in tags_filter):
                    continue
                score = self._compute_keyword_score(
                    query_lower,
                    [ep.intent_text, ep.execution_summary or ""] + ep.tags,
                )
                if score > 0:
                    results.append(
                        SearchResult(
                            id=ep.id,
                            content=ep.execution_summary or ep.intent_text,
                            score=score,
                            memory_type=MemoryType.EPISODE,
                            metadata={"tags": ep.tags},
                        )
                    )

        if memory_type in ("all", "semantic"):
            for kn in self._iter_all_knowledge():
                if user_id and kn.user_id != user_id:
                    continue
                if tags_filter:
                    kn_tags = kn.extra_data.get("tags", []) if kn.extra_data else []
                    if not any(t in kn_tags for t in tags_filter):
                        continue
                score = self._compute_keyword_score(
                    query_lower,
                    [kn.content],
                )
                if score > 0:
                    results.append(
                        SearchResult(
                            id=kn.id,
                            content=kn.content,
                            score=score,
                            memory_type=MemoryType.SEMANTIC,
                            metadata=kn.extra_data,
                        )
                    )

        results.sort(key=lambda x: x.score, reverse=True)
        return results[:limit]

    # ============================================
    # IRetriever 接口实现
    # ============================================

    async def retrieve(
        self,
        query: str,
        user_id: str | None = None,
        top_k: int = 5,
        memory_type: str = "semantic",
        filters: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        """检索相关记忆（IRetriever 接口实现）。

        将 IRetriever 接口参数映射到已有的 search() 方法：
        - top_k → limit
        - memory_type 注入到 filters 中
        - 委托给 self.search()

        Args:
            query: 查询文本
            user_id: 用户 ID
            top_k: 返回数量
            memory_type: 记忆类型
            filters: 额外过滤条件

        Returns:
            搜索结果列表
        """
        merged_filters = dict(filters) if filters else {}
        if "memory_type" not in merged_filters:
            merged_filters["memory_type"] = memory_type
        return await self.search(
            query=query,
            user_id=user_id,
            limit=top_k,
            filters=merged_filters,
        )

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
    # IEpisodeStorage + ISemanticStorage 共享方法
    # ============================================
    # 由于两个接口存在同名方法（save/get/find_by_user/delete），
    # Python 只能有一个实现。通过 ID 索引自动判断类型。

    async def get(self, entry_id: str) -> Episode | Knowledge | None:
        """获取记忆条目，自动根据 ID 索引判断类型。

        同时满足 IEpisodeStorage.get() 和 ISemanticStorage.get()。

        Args:
            entry_id: 条目 ID

        Returns:
            记忆条目实例
        """
        if entry_id in self._episode_ids:
            return self._read_episode_from_disk(entry_id)
        if entry_id in self._knowledge_ids:
            return self._read_knowledge_from_disk(entry_id)
        return self._read_episode_from_disk(entry_id) or self._read_knowledge_from_disk(entry_id)

    async def find_by_user(
        self,
        user_id: str,
        limit: int = 20,
        offset: int = 0,
    ) -> list[Episode]:
        """按用户查找情景记忆（按需遍历磁盘文件）。

        同时满足 IEpisodeStorage.find_by_user() 和 ISemanticStorage.find_by_user()。
        KnowledgeService 通过 find_knowledge_by_user() 专属方法调用，不会走到这里。

        Args:
            user_id: 用户 ID
            limit: 返回数量上限
            offset: 偏移量

        Returns:
            情景记忆列表
        """
        episodes = [ep for ep in self._iter_all_episodes() if ep.user_id == user_id]
        episodes.sort(key=lambda x: x.created_at, reverse=True)
        return episodes[offset : offset + limit]

    # ============================================
    # IEpisodeStorage 专用方法
    # ============================================

    async def save_episode(self, episode: Episode) -> str:
        """保存情景记忆。

        Args:
            episode: 情景记忆实例

        Returns:
            条目 ID
        """
        return await self.save(episode, "episode")

    async def update(self, episode_id: str, **kwargs: Any) -> bool:
        """更新情景记忆（读取→修改→写回）。

        Args:
            episode_id: 情景记忆 ID
            **kwargs: 要更新的字段

        Returns:
            是否更新成功
        """
        episode = self._read_episode_from_disk(episode_id)
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
        """统计用户的情景记忆数量（按需遍历磁盘文件）。

        Args:
            user_id: 用户 ID

        Returns:
            记忆数量
        """
        return sum(1 for ep in self._iter_all_episodes() if ep.user_id == user_id)

    # ============================================
    # ISemanticStorage 专用方法
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
        """获取知识（按需从磁盘读取）。

        Args:
            knowledge_id: 知识 ID

        Returns:
            知识实例
        """
        return self._read_knowledge_from_disk(knowledge_id)

    async def find_knowledge_by_user(
        self,
        user_id: str,
        limit: int = 20,
    ) -> list[Knowledge]:
        """按用户查找知识（按需遍历磁盘文件）。

        Args:
            user_id: 用户 ID
            limit: 返回数量上限

        Returns:
            知识列表
        """
        knowledge = [kn for kn in self._iter_all_knowledge() if kn.user_id == user_id]
        knowledge.sort(key=lambda x: x.created_at, reverse=True)
        return knowledge[:limit]

    async def update_embedding(
        self,
        knowledge_id: str,
        embedding: list[float],
    ) -> bool:
        """更新知识的向量嵌入（读取→修改→写回）。

        Args:
            knowledge_id: 知识 ID
            embedding: 向量嵌入

        Returns:
            是否更新成功
        """
        knowledge = self._read_knowledge_from_disk(knowledge_id)
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
