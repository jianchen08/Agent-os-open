"""ReviewEngine - 复盘查询层。

复盘的执行（LLM 深度分析、报告产出）由 MemoryMaintenanceService.trigger_llm_review
编排。本引擎只保留复盘系统对存储层的查询能力：

- get_pending_pipelines(): 列出待复盘管道（status=已结束 且 review_status=pending）
- get_summary(): 单个管道复盘摘要
- mark_reviewed(): 复盘产出报告后，把管道标记为已复盘
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "ReviewStatus",
    "PipelineRunSummary",
    "ReviewEngine",
]


class ReviewStatus(str, Enum):
    """复盘状态枚举（仅用于存储层 review_status 字段值对齐）。"""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class PipelineRunSummary:
    """管道运行摘要。"""

    run_id: str
    total_records: int = 0
    total_iterations: int = 0
    created_at: str = ""
    status: str = ""
    error: str = ""
    review_status: str = "pending"


class ReviewEngine:
    """复盘查询层：列出待复盘管道、标记已复盘。

    复盘执行由 MemoryMaintenanceService.trigger_llm_review 编排，本类只负责
    对存储层的查询与状态标记。

    Args:
        storage: 执行记录存储，提供 list_all_summaries/get_summary/update_summary
        chunk_db: 数据块存储，提供 find_by_pipeline/save_chunk（标记复盘用）
    """

    # 已结束状态：track 插件写入 success/failed，旧版/单元测试用 completed。
    # get_pending_pipelines 共用此集合作为"哪些 pipeline 可复盘"的单一真相源。
    _TERMINAL_STATUSES = frozenset({"completed", "success", "failed"})

    def __init__(
        self,
        storage: Any = None,
        chunk_db: Any = None,
        knowledge_service: Any = None,
        task_lookup: Any | None = None,
    ) -> None:
        """初始化复盘查询层。

        Args:
            storage: 执行记录存储
            chunk_db: 数据块存储（标记复盘用）
            knowledge_service: 未使用，保留参数位以兼容构造签名。
            task_lookup: 未使用，保留参数位以兼容构造签名。
        """
        self._storage = storage
        self._chunk_db = chunk_db
        # knowledge_service / task_lookup 仅保留参数位以兼容构造签名
        self._knowledge_service = knowledge_service
        self._task_lookup = task_lookup

    # ============================================
    # 查询：待复盘管道
    # ============================================

    def get_pending_pipelines(self) -> list[PipelineRunSummary]:
        """获取所有待复盘的管道。

        过滤条件：review_status='pending' 且 status 为"已结束"状态。
        兼容 track 插件实际写入的 status 值（success/failed/completed）。

        Returns:
            待复盘的管道摘要列表。storage 为 None 时返回空列表。
        """
        if self._storage is None:
            return []
        summaries = self._storage.list_all_summaries()
        return [s for s in summaries if s.status in self._TERMINAL_STATUSES and s.review_status == "pending"]

    def get_summary(self, run_id: str) -> PipelineRunSummary | None:
        """获取单个管道的复盘摘要。

        Args:
            run_id: 管道运行 ID

        Returns:
            管道摘要，不存在时返回 None
        """
        if self._storage is not None:
            return self._storage.get_summary(run_id)
        return None

    # ============================================
    # 标记：复盘产出报告后调用
    # ============================================

    async def mark_reviewed(self, run_id: str, *, failed: bool = False) -> None:
        """把管道标记为已复盘（复盘产出报告后调用）。

        Args:
            run_id: 管道运行 ID
            failed: True 时标记为 failed（LLM 复盘未产出报告），
                默认 completed。
        """
        review_status = "failed" if failed else "completed"

        # chunk_db 为 None 是合法配置（纯 API 触发场景没有压缩块），
        # 静默跳过 chunk 标记，只更新 summary。真实异常才记 warning。
        if self._chunk_db is not None:
            try:
                chunks = await self._chunk_db.find_by_pipeline(run_id)
                for chunk in chunks:
                    chunk.extra_data["review_status"] = review_status
                    self._chunk_db.save_chunk(chunk)
            except Exception:
                logger.warning("Failed to update chunk reviewed flags for %s", run_id)

        self._storage.update_summary(run_id, {"review_status": review_status})
