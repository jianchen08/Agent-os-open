"""复盘引擎模块 - 负责对 Pipeline 执行结果进行自动复盘和经验提取。"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class PipelineRunSummary:
    """Pipeline 运行摘要。"""
    run_id: str = ""
    total_records: int = 0
    total_iterations: int = 0
    created_at: str = ""
    status: str = ""
    error: str = ""
    review_status: str = "pending"  # pending / reviewing / completed


@dataclass
class ExecutionRecord:
    """执行记录。"""
    iteration: int = 0
    type: str = ""
    name: str = ""
    error: str = ""
    thinking_content: str = ""
    tool_calls_json: str = ""
    content: str = ""
    sequence: int = 0


@dataclass
class ChunkData:
    """数据块。"""
    chunk_id: str = ""
    pipeline_id: str = ""
    layer: str = ""
    content: str = ""
    extra_data: dict[str, Any] = field(default_factory=dict)


class ReviewEngine:
    """复盘引擎 - 对已完成的 Pipeline 进行自动复盘分析。

    Bug 修复记录:
    - Bug1 (原第161行): saved_count 未定义 → 改为 saved_counts.get("experiences", 0)
    - Bug2 (原第784行): _load_existing_experiences 调用签名错误 → 改用 list_semantic_memory + 按 source_type 过滤
    - Bug3 (原第806行): _mark_pipeline_reviewed 从同步改为 async，内部 run_until_complete 改为 await
    """

    EXPERIENCE_SOURCE_TYPE = "review_experience"

    def __init__(
        self,
        storage: Any,
        chunk_db: Any,
        knowledge_service: Any,
        pipeline_engine: Any | None = None,
    ) -> None:
        self.storage = storage
        self.chunk_db = chunk_db
        self.knowledge_service = knowledge_service
        self.pipeline_engine = pipeline_engine

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def get_pending_pipelines(self) -> list[PipelineRunSummary]:
        """获取待复盘的 Pipeline 列表。"""
        all_summaries = self.storage.list_all_summaries()
        pending = [
            s for s in all_summaries
            if s.status == "completed" and s.review_status == "pending"
        ]
        return pending

    async def run_review(self, run_id: str) -> dict[str, Any]:
        """对单个 Pipeline 执行完整的复盘流程。

        流程：获取摘要 → 加载执行记录 → 分析 → 保存经验 → 标记复盘完成。
        """
        summary = self.storage.get_summary(run_id)
        if summary is None:
            return {"status": "error", "message": f"Pipeline {run_id} not found"}

        if summary.status != "completed":
            return {"status": "error", "message": f"Pipeline {run_id} not completed"}

        # 标记为复盘中
        self.storage.update_summary(run_id, {"review_status": "reviewing"})

        # 加载执行记录
        records = self.storage.list_by_pipeline(run_id)

        # 分析执行记录，提取经验
        saved_counts: dict[str, int] = {}
        await self._analyze_and_save_experiences(run_id, records, saved_counts)

        # 加载已有的 chunk 数据进行补充分析
        chunks = await self.chunk_db.find_by_pipeline(run_id, layer="summary")

        # 用 pipeline_engine 做深度分析（如果可用）
        if self.pipeline_engine is not None and chunks:
            try:
                result = await self.pipeline_engine.run(
                    user_input=json.dumps([c.content for c in chunks], ensure_ascii=False),
                    agent_config={"mode": "review"},
                    allow_default_fallback=True,
                )
                logger.info("Deep review analysis completed for %s", run_id)
            except Exception as exc:
                logger.warning("Deep review analysis failed: %s", exc)

        # [Bug1 修复] 使用 saved_counts.get 而不是未定义的 saved_count
        experience_count = saved_counts.get("experiences", 0)

        # 标记复盘完成 - [Bug3 修复] 使用 await 而非 run_until_complete
        await self._mark_pipeline_reviewed(run_id)

        return {
            "status": "success",
            "run_id": run_id,
            "experience_count": experience_count,
            "records_analyzed": len(records),
        }

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    async def _analyze_and_save_experiences(
        self,
        run_id: str,
        records: list[ExecutionRecord],
        saved_counts: dict[str, int],
    ) -> None:
        """分析执行记录并保存经验到知识库。"""
        saved_counts["experiences"] = 0

        # 筛选有错误的记录作为经验来源
        error_records = [r for r in records if r.error]

        # 加载已有经验，避免重复 - [Bug2 修复] 使用 list_semantic_memory + 按 source_type 过滤
        existing_experiences = await self._load_existing_experiences()

        for record in error_records:
            content = f"Pipeline {run_id} - {record.name}: {record.error}"
            # 去重检查
            if content in existing_experiences:
                continue

            try:
                await self.knowledge_service.create_knowledge(
                    user_id="system",
                    content=content,
                    source_type=self.EXPERIENCE_SOURCE_TYPE,
                    extra_data={"run_id": run_id, "iteration": record.iteration},
                )
                saved_counts["experiences"] += 1
            except Exception as exc:
                logger.warning("Failed to save experience: %s", exc)

    async def _load_existing_experiences(self) -> set[str]:
        """加载已有的复盘经验，用于去重。

        [Bug2 修复] 原实现调用 _load_existing_experiences(run_id) 签名错误，
        现改为使用 list_semantic_memory(user_id="system") + 按 source_type 过滤。
        """
        try:
            result = await self.knowledge_service.list_semantic_memory(user_id="system")
            items = result.get("items", [])
            # 按 source_type 过滤，只保留复盘经验
            existing: set[str] = set()
            for item in items:
                if item.get("source_type") == self.EXPERIENCE_SOURCE_TYPE:
                    existing.add(item.get("content", ""))
            return existing
        except Exception as exc:
            logger.warning("Failed to load existing experiences: %s", exc)
            return set()

    async def _mark_pipeline_reviewed(self, run_id: str) -> None:
        """标记 Pipeline 已完成复盘。

        [Bug3 修复] 原实现是同步方法，内部使用 asyncio.get_event_loop().run_until_complete()
        在已处于异步上下文时会导致 "This event loop is already running" 错误。
        改为 async 方法，直接 await。
        """
        self.storage.update_summary(run_id, {"review_status": "completed"})

        # 异步保存复盘相关的 chunk 标记
        try:
            chunks = await self.chunk_db.find_by_pipeline(run_id, layer="summary")
            for chunk in chunks:
                chunk.extra_data["reviewed"] = True
                self.chunk_db._save_to_disk(chunk)
        except Exception as exc:
            logger.warning("Failed to update chunk review flags: %s", exc)
