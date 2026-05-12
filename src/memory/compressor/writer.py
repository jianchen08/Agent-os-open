"""
上下文写入器（存储端）

只负责写入、压缩、更新元数据
彻底的存取分离设计
"""

import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.tokenizer import get_token_counter
from src.llm.base import LLMClient
from src.memory.context_repository import ContextRepository

from .config import CompressionConfig
from .core import ContextCompressor
from .db import MemoryChunkDB
from .metadata_store import ChunkMetadataStore
from .models import (
    ChunkMetadata,
    ChunkStatus,
    CompressionReport,
    CompressionResult,
    ContentRef,
)

logger = logging.getLogger(__name__)


class ContextWriter:
    """
    上下文写入器

    只负责：
    1. 写入消息到 L0
    2. 根据预算执行压缩（L0→L1→L2→L3）
    3. 更新元数据存储

    不负责：
    - 读取上下文
    - 组装上下文
    """

    def __init__(
        self,
        session_id: str,
        user_id: str,
        config: CompressionConfig,
        llm_client: LLMClient,
        metadata_store: ChunkMetadataStore,
        db_session: AsyncSession,
        context_repository: ContextRepository,
        executor_type: str | None = None,
        executor_id: str | None = None,
        executor_name: str | None = None,
    ):
        self.session_id = session_id
        self.user_id = user_id
        self.config = config
        self.llm_client = llm_client
        self.metadata_store = metadata_store
        self.db_session = db_session
        self.context_repository = context_repository
        self.executor_type = executor_type
        self.executor_id = executor_id
        self.executor_name = executor_name

        self.compressor = ContextCompressor(llm_client, config)
        self.chunk_db = MemoryChunkDB()
        self.token_counter = get_token_counter()

        # 预算配置
        self.budgets = config.get_budgets()
        self.trigger_ratio = getattr(config, 'compress_trigger_ratio', 0.5)

    async def compress_if_needed(self) -> CompressionReport:
        """
        检查预算，按需执行压缩链

        压缩流程：
        1. 检查总上下文是否超出触发阈值
        2. 如果超出，执行递进压缩循环
        3. 每层从旧到新选择块进行压缩
        """
        report = CompressionReport()

        # 计算总 token 数
        total_tokens = await self._get_total_context_tokens()
        trigger_threshold = self.config.context_window * self.trigger_ratio

        if total_tokens <= trigger_threshold:
            logger.debug(f"[ContextWriter] 总上下文 {total_tokens} <= 触发阈值 {trigger_threshold}，无需压缩")
            return report

        logger.info(f"[ContextWriter] 总上下文 {total_tokens} > 触发阈值 {trigger_threshold}，开始压缩")

        # 执行递进压缩循环
        max_iterations = 10
        iteration = 0

        while iteration < max_iterations:
            iteration += 1
            compressed_in_iteration = False

            # 检查 L0 预算
            if await self._is_layer_over_budget("recent"):
                logger.info(f"[ContextWriter] 迭代 {iteration}: L0 超出预算，执行 L0→L1 压缩")
                result = await self._compress_l0_to_l1()
                if result.new_chunk:
                    report.compressed_chunks.extend([(cid, "L0→L1") for cid in result.source_chunk_ids])
                    report.new_chunks.append(result.new_chunk)
                    report.tokens_saved += result.tokens_saved
                    compressed_in_iteration = True

            # 检查 L1 预算
            if await self._is_layer_over_budget("L1"):
                logger.info(f"[ContextWriter] 迭代 {iteration}: L1 超出预算，执行 L1→L2 压缩")
                result = await self._compress_l1_to_l2()
                if result.new_chunk:
                    report.compressed_chunks.extend([(cid, "L1→L2") for cid in result.source_chunk_ids])
                    report.new_chunks.append(result.new_chunk)
                    report.tokens_saved += result.tokens_saved
                    compressed_in_iteration = True

            # 检查 L2 预算
            if await self._is_layer_over_budget("L2"):
                logger.info(f"[ContextWriter] 迭代 {iteration}: L2 超出预算，执行 L2→L3 压缩")
                result = await self._compress_l2_to_l3()
                if result.new_chunk:
                    report.compressed_chunks.extend([(cid, "L2→L3") for cid in result.source_chunk_ids])
                    report.new_chunks.append(result.new_chunk)
                    report.tokens_saved += result.tokens_saved
                    compressed_in_iteration = True

            # 检查 L3 预算
            if await self._is_layer_over_budget("L3"):
                logger.info(f"[ContextWriter] 迭代 {iteration}: L3 超出预算，执行遗忘")
                await self._evict_l3_overflow()
                compressed_in_iteration = True

            if not compressed_in_iteration:
                logger.info(f"[ContextWriter] 迭代 {iteration}: 所有层都符合预算，压缩完成")
                break

            # 检查总上下文是否已符合预算
            total_tokens = await self._get_total_context_tokens()
            if total_tokens <= trigger_threshold:
                logger.info(f"[ContextWriter] 迭代 {iteration}: 总上下文已符合预算")
                break

        report.iterations = iteration
        logger.info(f"[ContextWriter] 压缩完成，共执行 {iteration} 轮迭代，节省 {report.tokens_saved} tokens")
        return report

    async def _compress_l0_to_l1(self) -> CompressionResult:
        """
        将超出的 L0 消息压缩成 L1 块

        逻辑：
        1. 获取未压缩消息（按时间排序）
        2. 从旧到新累加，找到超出预算的部分
        3. 将超出的消息压缩成八段摘要
        4. 保存到数据库，注册元数据
        """
        # 获取未压缩消息
        messages = await self.context_repository.get_uncompressed_messages(
            session_id=self.session_id,
            executor_type=self.executor_type,
            executor_id=self.executor_id
        )

        if not messages:
            return CompressionResult("L0", "L1", [])

        # 按轮次分组
        turns = self._group_messages_into_turns(messages)
        if not turns:
            return CompressionResult("L0", "L1", [])

        # 计算每个轮次的 token 数
        turn_tokens = []
        for turn in turns:
            tokens = sum(self.token_counter.count_message(msg) for msg in turn)
            turn_tokens.append(tokens)

        total_tokens = sum(turn_tokens)
        budget = self.budgets.get("recent", 0)

        if total_tokens <= budget:
            return CompressionResult("L0", "L1", [])

        # 从后往前（从新到旧）累加，找到需要保留的轮次
        keep_turns = 0
        keep_tokens = 0
        for i in range(len(turns) - 1, -1, -1):
            if keep_tokens + turn_tokens[i] <= budget:
                keep_tokens += turn_tokens[i]
                keep_turns += 1
            else:
                break

        # 需要压缩的轮次（前面的，旧的）
        compress_turns = len(turns) - keep_turns
        if compress_turns <= 0:
            return CompressionResult("L0", "L1", [])

        turns_to_compress = turns[:compress_turns]
        messages_to_compress = []
        for turn in turns_to_compress:
            messages_to_compress.extend(turn)

        # 压缩成八段摘要
        summary = await self.compressor.compress_to_l1(messages_to_compress)

        # 保存到数据库
        chunk_id = await self.chunk_db.save_chunk(
            session=self.db_session,
            user_id=self.user_id,
            session_id=self.session_id,
            layer="L1",
            content=summary,
            message_count=len(messages_to_compress),
            executor_type=self.executor_type,
            executor_id=self.executor_id,
            executor_name=self.executor_name,
        )

        # 计算 token 数
        token_count = self.token_counter.count_tokens(summary)

        # 注册元数据
        metadata = ChunkMetadata(
            chunk_id=chunk_id,
            session_id=self.session_id,
            layer="L1",
            token_count=token_count,
            message_count=len(messages_to_compress),
            created_at=datetime.now(),
            content_ref=ContentRef("memory_chunks", chunk_id),
            status=ChunkStatus.ACTIVE,
            executor_id=self.executor_id,
            executor_type=self.executor_type,
        )
        self.metadata_store.register(metadata)

        # 标记被压缩的消息（这里需要 ContextRepository 支持标记功能）
        # 暂时跳过，后续实现

        tokens_saved = sum(turn_tokens[:compress_turns]) - token_count

        logger.info(
            f"[_compress_l0_to_l1] 压缩完成 | "
            f"保留 {keep_turns} 个轮次 ({keep_tokens} tokens) | "
            f"压缩 {compress_turns} 个轮次到 L1 ({token_count} tokens) | "
            f"节省 {tokens_saved} tokens"
        )

        return CompressionResult("L0", "L1", [], metadata, tokens_saved)

    async def _compress_l1_to_l2(self) -> CompressionResult:
        """
        将超出的 L1 块压缩成 L2 块

        逻辑：
        1. 获取 L1 块（按时间排序，旧的在前）
        2. 从旧到新累加，找到超出预算的部分
        3. 将超出的块压缩成三元组
        4. 保存到数据库，更新元数据
        """
        # 获取 L1 块
        l1_chunks = self.metadata_store.get_layer_chunks(self.session_id, "L1", ChunkStatus.ACTIVE)
        if not l1_chunks:
            return CompressionResult("L1", "L2", [])

        # 计算总 token 数
        total_tokens = sum(c.token_count for c in l1_chunks)
        budget = self.budgets.get("L1", 0)

        if total_tokens <= budget:
            return CompressionResult("L1", "L2", [])

        # 从后往前（从新到旧）累加，找到需要保留的块
        keep_chunks = []
        overflow_chunks = []
        keep_tokens = 0

        for chunk in reversed(l1_chunks):
            if keep_tokens + chunk.token_count <= budget:
                keep_chunks.append(chunk)
                keep_tokens += chunk.token_count
            else:
                overflow_chunks.append(chunk)

        if not overflow_chunks:
            return CompressionResult("L1", "L2", [])

        # 加载需要压缩的块的内容
        contents = []
        for chunk in overflow_chunks:
            # 从数据库加载内容
            chunks_data = await self.chunk_db.load_chunks_by_session(
                session=self.db_session,
                session_id=self.session_id,
                executor_id=self.executor_id
            )
            if chunk.chunk_id in [c.get("id") for c in chunks_data.get("L1", [])]:
                # 找到内容
                content = await self._load_chunk_content(chunk.chunk_id)
                if content:
                    contents.append(content)

        if not contents:
            return CompressionResult("L1", "L2", [])

        # 压缩成三元组
        content_to_compress = "\n\n---\n\n".join(contents)
        summary = await self.compressor.compress_to_l2(content_to_compress)

        # 保存到数据库
        chunk_id = await self.chunk_db.save_chunk(
            session=self.db_session,
            user_id=self.user_id,
            session_id=self.session_id,
            layer="L2",
            content=summary,
            executor_type=self.executor_type,
            executor_id=self.executor_id,
            executor_name=self.executor_name,
        )

        # 计算 token 数
        token_count = self.token_counter.count_tokens(summary)

        # 注册元数据
        metadata = ChunkMetadata(
            chunk_id=chunk_id,
            session_id=self.session_id,
            layer="L2",
            token_count=token_count,
            message_count=len(overflow_chunks),
            created_at=datetime.now(),
            content_ref=ContentRef("memory_chunks", chunk_id),
            status=ChunkStatus.ACTIVE,
            executor_id=self.executor_id,
            executor_type=self.executor_type,
        )
        self.metadata_store.register(metadata)

        # 更新被压缩的 L1 块状态
        source_chunk_ids = []
        for chunk in overflow_chunks:
            self.metadata_store.update_status(chunk.chunk_id, ChunkStatus.COMPRESSED)
            source_chunk_ids.append(chunk.chunk_id)

        # 从数据库删除被压缩的 L1 块
        await self.chunk_db.delete_chunks_by_ids(self.db_session, source_chunk_ids)

        tokens_saved = sum(c.token_count for c in overflow_chunks) - token_count

        logger.info(
            f"[_compress_l1_to_l2] 压缩完成 | "
            f"保留 {len(keep_chunks)} 个 L1 块 ({keep_tokens} tokens) | "
            f"压缩 {len(overflow_chunks)} 个 L1 块到 L2 ({token_count} tokens) | "
            f"节省 {tokens_saved} tokens"
        )

        return CompressionResult("L1", "L2", source_chunk_ids, metadata, tokens_saved)

    async def _compress_l2_to_l3(self) -> CompressionResult:
        """
        将超出的 L2 块压缩成 L3 块

        逻辑同 _compress_l1_to_l2
        """
        # 获取 L2 块
        l2_chunks = self.metadata_store.get_layer_chunks(self.session_id, "L2", ChunkStatus.ACTIVE)
        if not l2_chunks:
            return CompressionResult("L2", "L3", [])

        total_tokens = sum(c.token_count for c in l2_chunks)
        budget = self.budgets.get("L2", 0)

        if total_tokens <= budget:
            return CompressionResult("L2", "L3", [])

        # 从后往前累加
        keep_chunks = []
        overflow_chunks = []
        keep_tokens = 0

        for chunk in reversed(l2_chunks):
            if keep_tokens + chunk.token_count <= budget:
                keep_chunks.append(chunk)
                keep_tokens += chunk.token_count
            else:
                overflow_chunks.append(chunk)

        if not overflow_chunks:
            return CompressionResult("L2", "L3", [])

        # 加载内容并压缩
        contents = []
        for chunk in overflow_chunks:
            content = await self._load_chunk_content(chunk.chunk_id)
            if content:
                contents.append(content)

        if not contents:
            return CompressionResult("L2", "L3", [])

        content_to_compress = "\n\n---\n\n".join(contents)
        keywords = await self.compressor.compress_to_l3(content_to_compress)

        # 保存到数据库
        chunk_id = await self.chunk_db.save_chunk(
            session=self.db_session,
            user_id=self.user_id,
            session_id=self.session_id,
            layer="L3",
            content=keywords,
            executor_type=self.executor_type,
            executor_id=self.executor_id,
            executor_name=self.executor_name,
        )

        token_count = self.token_counter.count_tokens(keywords)

        # 注册元数据
        metadata = ChunkMetadata(
            chunk_id=chunk_id,
            session_id=self.session_id,
            layer="L3",
            token_count=token_count,
            message_count=len(overflow_chunks),
            created_at=datetime.now(),
            content_ref=ContentRef("memory_chunks", chunk_id),
            status=ChunkStatus.ACTIVE,
            executor_id=self.executor_id,
            executor_type=self.executor_type,
        )
        self.metadata_store.register(metadata)

        # 更新被压缩的 L2 块状态
        source_chunk_ids = []
        for chunk in overflow_chunks:
            self.metadata_store.update_status(chunk.chunk_id, ChunkStatus.COMPRESSED)
            source_chunk_ids.append(chunk.chunk_id)

        # 从数据库删除被压缩的 L2 块
        await self.chunk_db.delete_chunks_by_ids(self.db_session, source_chunk_ids)

        tokens_saved = sum(c.token_count for c in overflow_chunks) - token_count

        logger.info(
            f"[_compress_l2_to_l3] 压缩完成 | "
            f"保留 {len(keep_chunks)} 个 L2 块 ({keep_tokens} tokens) | "
            f"压缩 {len(overflow_chunks)} 个 L2 块到 L3 ({token_count} tokens) | "
            f"节省 {tokens_saved} tokens"
        )

        return CompressionResult("L2", "L3", source_chunk_ids, metadata, tokens_saved)

    async def _evict_l3_overflow(self) -> None:
        """
        丢弃超出的 L3 块（遗忘）
        """
        l3_chunks = self.metadata_store.get_layer_chunks(self.session_id, "L3", ChunkStatus.ACTIVE)
        if not l3_chunks:
            return

        total_tokens = sum(c.token_count for c in l3_chunks)
        budget = self.budgets.get("L3", 0)

        if total_tokens <= budget:
            return

        # 从后往前累加
        keep_chunks = []
        overflow_chunks = []
        keep_tokens = 0

        for chunk in reversed(l3_chunks):
            if keep_tokens + chunk.token_count <= budget:
                keep_chunks.append(chunk)
                keep_tokens += chunk.token_count
            else:
                overflow_chunks.append(chunk)

        if not overflow_chunks:
            return

        # 标记为丢弃并删除
        chunk_ids_to_delete = []
        for chunk in overflow_chunks:
            self.metadata_store.update_status(chunk.chunk_id, ChunkStatus.DISCARDED)
            chunk_ids_to_delete.append(chunk.chunk_id)

        # 从数据库删除
        await self.chunk_db.delete_chunks_by_ids(self.db_session, chunk_ids_to_delete)

        logger.info(
            f"[_evict_l3_overflow] 遗忘完成 | "
            f"保留 {len(keep_chunks)} 个 L3 块 ({keep_tokens} tokens) | "
            f"遗忘 {len(overflow_chunks)} 个 L3 块"
        )

    async def _is_layer_over_budget(self, layer: str) -> bool:
        """检查指定层是否超出预算"""
        if layer == "recent":
            # L0 层需要特殊处理
            messages = await self.context_repository.get_uncompressed_messages(
                session_id=self.session_id,
                executor_type=self.executor_type,
                executor_id=self.executor_id
            )
            model = getattr(self.llm_client, 'model_name', '')
            tokens = self.token_counter.count_messages(messages, model)
            budget = self.budgets.get("recent", 0)
        else:
            tokens = self.metadata_store.get_layer_tokens(self.session_id, layer)
            budget = self.budgets.get(layer, 0)

        return tokens > budget

    async def _get_total_context_tokens(self) -> int:
        """获取总上下文的 token 数"""
        # L0
        messages = await self.context_repository.get_uncompressed_messages(
            session_id=self.session_id,
            executor_type=self.executor_type,
            executor_id=self.executor_id
        )
        model = getattr(self.llm_client, 'model_name', '')
        l0_tokens = self.token_counter.count_messages(messages, model)

        # L1, L2, L3
        l1_tokens = self.metadata_store.get_layer_tokens(self.session_id, "L1")
        l2_tokens = self.metadata_store.get_layer_tokens(self.session_id, "L2")
        l3_tokens = self.metadata_store.get_layer_tokens(self.session_id, "L3")

        return l0_tokens + l1_tokens + l2_tokens + l3_tokens

    def _group_messages_into_turns(self, messages: list[dict]) -> list[list[dict]]:
        """将消息按对话轮次分组"""
        if not messages:
            return []

        turns = []
        current_turn = []

        for msg in messages:
            role = msg.get("role", "")
            if role == "user" and current_turn:
                turns.append(current_turn)
                current_turn = []
            current_turn.append(msg)

        if current_turn:
            turns.append(current_turn)

        return turns

    async def _load_chunk_content(self, chunk_id: str) -> str:
        """从数据库加载块内容"""
        # 这里简化实现，实际应该从数据库查询
        # 暂时返回空字符串，后续完善
        return ""
