"""语义知识存储服务。

从旧代码 src/memory/knowledge_service.py 搬迁。
移除 SQLAlchemy 硬依赖，通过 ISemanticStorage 接口操作存储。
没有 storage 时降级到内存字典。

暴露接口：
- KnowledgeService: 语义知识存储服务
"""

from __future__ import annotations

import logging
from typing import Any

from memory.ports import ISemanticStorage
from memory.types import Knowledge

logger = logging.getLogger(__name__)


class KnowledgeService:
    """语义知识存储服务。

    职责（仅存储操作）：
    - 创建和存储语义知识
    - 更新语义知识
    - 删除语义知识
    - 列出语义知识

    检索操作请使用 MemoryService.retrieve(memory_type="semantic", ...)。

    Attributes:
        _storage: 语义记忆存储接口
        _in_memory: 内存降级存储
    """

    def __init__(
        self,
        semantic_storage: ISemanticStorage | None = None,
    ) -> None:
        """初始化语义知识存储服务。

        Args:
            semantic_storage: 语义记忆存储接口，None 时降级到内存
        """
        self._storage = semantic_storage
        self._in_memory: dict[str, Knowledge] = {}

    async def store_knowledge(self, knowledge: Knowledge) -> str:
        """存储知识。

        Args:
            knowledge: 知识实例

        Returns:
            存储的条目 ID
        """
        if self._storage:
            return await self._storage.save(knowledge)

        # 内存降级
        self._in_memory[knowledge.id] = knowledge
        logger.debug("[KnowledgeService] 内存存储 | id=%s", knowledge.id)
        return knowledge.id

    async def create_knowledge(
        self,
        user_id: str,
        content: str,
        source_type: str,
        extra_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """创建知识。

        Args:
            user_id: 用户 ID
            content: 知识内容
            source_type: 来源类型
            extra_data: 额外数据

        Returns:
            创建的知识字典
        """
        knowledge = Knowledge(
            user_id=user_id,
            content=content,
            source_type=source_type,
            extra_data=extra_data or {},
        )

        await self.store_knowledge(knowledge)

        return knowledge.to_dict()

    async def list_semantic_memory(
        self,
        user_id: str,
    ) -> dict[str, Any]:
        """获取语义记忆列表。

        Args:
            user_id: 用户 ID

        Returns:
            语义记忆列表字典
        """
        if self._storage:
            memories = await self._storage.find_by_user(user_id)
        else:
            memories = [
                kn for kn in self._in_memory.values()
                if kn.user_id == user_id
            ]
            memories.sort(key=lambda x: x.created_at, reverse=True)

        items = [kn.to_dict() for kn in memories]

        return {"items": items, "total": len(items)}

    async def delete_knowledge(
        self,
        knowledge_id: str,
        user_id: str,
    ) -> bool:
        """删除知识。

        Args:
            knowledge_id: 知识 ID
            user_id: 用户 ID（用于权限校验）

        Returns:
            是否删除成功
        """
        if self._storage:
            knowledge = await self._storage.get(knowledge_id)
            if not knowledge or knowledge.user_id != user_id:
                return False
            return await self._storage.delete(knowledge_id)

        # 内存降级
        knowledge = self._in_memory.get(knowledge_id)
        if not knowledge or knowledge.user_id != user_id:
            return False

        del self._in_memory[knowledge_id]
        return True

    async def get_knowledge_count(self, user_id: str) -> int:
        """获取知识数量。

        Args:
            user_id: 用户 ID

        Returns:
            该用户的知识数量
        """
        if self._storage:
            memories = await self._storage.find_by_user(user_id)
            return len(memories)

        return sum(1 for kn in self._in_memory.values() if kn.user_id == user_id)
