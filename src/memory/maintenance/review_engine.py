"""ReviewEngine - 复盘引擎，负责对 pipeline 执行结果进行复盘和经验提取。"""
from __future__ import annotations

<<<<<<< C:\Users\jc\AppData\Local\Temp\tmpea0iy_or\current
=======
import logging
>>>>>>> D:\myproject\container_08f57__wt_b30e823c\src\memory\maintenance\review_engine.py
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
<<<<<<< C:\Users\jc\AppData\Local\Temp\tmpea0iy_or\current
<<<<<<< C:\Users\jc\AppData\Local\Temp\tmpm3wy5hdt\current
=======
from pathlib import Path
>>>>>>> D:\myproject\container_08f57__wt_7f34aa1e\src\memory\maintenance\review_engine.py
from typing import Any

=======
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "ReviewStatus",
    "ErrorRecord",
    "Experience",
    "Pipeline",
    "ChunkData",
    "ExecutionRecord",
    "PipelineRunSummary",
    "ReviewEngine",
]

>>>>>>> D:\myproject\container_08f57__wt_b30e823c\src\memory\maintenance\review_engine.py

class ReviewStatus(str, Enum):
    """复盘状态枚举。"""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class ErrorRecord:
    """错误记录。"""
    error_id: str
    error_type: str
    message: str
    timestamp: str


@dataclass
class Experience:
    """从错误中提取的经验。"""
    experience_id: str
    source_error_id: str
    lesson: str
    category: str
    created_at: str


@dataclass
class Pipeline:
    """Pipeline 执行记录。"""
    pipeline_id: str
    status: ReviewStatus = ReviewStatus.PENDING
    errors: list[ErrorRecord] = field(default_factory=list)
    experiences: list[Experience] = field(default_factory=list)
    reviewed_at: str | None = None


<<<<<<< C:\Users\jc\AppData\Local\Temp\tmpea0iy_or\current
=======
@dataclass
class ChunkData:
    """管道数据块。"""
    chunk_id: str
    pipeline_id: str
    layer: str
    content: str
    extra_data: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionRecord:
    """执行记录（复盘引擎视角的精简版）。"""
    iteration: int
    type: str
    name: str
    error: str
    thinking_content: str | None = None
    tool_calls_json: str | None = None
    content: str = ""
    sequence: int = 0


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


>>>>>>> D:\myproject\container_08f57__wt_b30e823c\src\memory\maintenance\review_engine.py
class ReviewEngine:
    """复盘引擎：对 pipeline 执行结果进行复盘，提取经验教训。

    核心职责：
<<<<<<< C:\Users\jc\AppData\Local\Temp\tmpea0iy_or\current
    1. 接收 pending 状态的 pipeline 列表
    2. 对每个 pipeline 进行复盘（分析错误、提取经验）
    3. 更新 pipeline 状态为 completed
    """

    def __init__(self) -> None:
        self._pipelines: dict[str, Pipeline] = {}
=======
    1. 获取 pending 状态的 pipeline 列表
    2. 对每个 pipeline 进行复盘（分析错误、提取经验）
    3. 更新 pipeline 状态为 completed

    Args:
        storage: 执行记录存储，提供 get_summary/list_by_pipeline/list_all_summaries/update_summary
        chunk_db: 数据块存储，提供 find_by_pipeline/save_chunk
        knowledge_service: 知识服务，提供 list_semantic_memory/create_knowledge
        pipeline_engine: 可选管道引擎，提供 run 方法用于深度分析
    """

    def __init__(
        self,
        storage: Any = None,
        chunk_db: Any = None,
        knowledge_service: Any = None,
        pipeline_engine: Any | None = None,
    ) -> None:
        # 向后兼容：无参数时使用内存存储
        self._pipelines: dict[str, Pipeline] = {}
        self._storage = storage
        self._chunk_db = chunk_db
        self._knowledge_service = knowledge_service
        self._pipeline_engine = pipeline_engine

    # ------------------------------------------------------------------
    # 向后兼容接口（简化版 ReviewEngine）
    # ------------------------------------------------------------------
>>>>>>> D:\myproject\container_08f57__wt_b30e823c\src\memory\maintenance\review_engine.py

    def register_pipeline(self, pipeline: Pipeline) -> None:
        """注册待复盘的 pipeline。"""
        self._pipelines[pipeline.pipeline_id] = pipeline

    def register_pipelines(self, pipelines: list[Pipeline]) -> None:
        """批量注册待复盘的 pipeline。"""
        for p in pipelines:
            self.register_pipeline(p)

<<<<<<< C:\Users\jc\AppData\Local\Temp\tmpea0iy_or\current
    def get_pending_pipelines(self) -> list[Pipeline]:
        """获取所有待复盘的 pipeline。"""
        return [p for p in self._pipelines.values() if p.status == ReviewStatus.PENDING]

    def run_review(self) -> dict[str, Any]:
        """执行复盘流程：对所有 pending pipeline 进行复盘。

        Returns:
            复盘结果摘要，包含处理数量、经验提取数量等。
        """
        pending = self.get_pending_pipelines()
=======
    def _extract_experiences(self, pipeline: Pipeline) -> list[Experience]:
        """从 pipeline 的错误记录中提取经验。"""
        experiences: list[Experience] = []
        for error in pipeline.errors:
            experience = Experience(
                experience_id=f"exp-{uuid.uuid4().hex[:8]}",
                source_error_id=error.error_id,
                lesson=self._generate_lesson(error),
                category=self._categorize_error(error),
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            experiences.append(experience)
        return experiences

    def _generate_lesson(self, error: ErrorRecord) -> str:
        """根据错误类型生成经验教训。"""
        lessons = {
            "timeout": f"操作超时({error.message})：建议增加超时时间或添加重试机制",
            "connection": f"连接失败({error.message})：建议检查网络配置和服务可用性",
            "validation": f"数据验证失败({error.message})：建议加强输入校验",
            "permission": f"权限不足({error.message})：建议检查访问控制配置",
        }
        return lessons.get(error.error_type, f"未知错误({error.message})：建议排查具体原因")

    def _categorize_error(self, error: ErrorRecord) -> str:
        """对错误进行分类。"""
        categories = {
            "timeout": "performance",
            "connection": "infrastructure",
            "validation": "data_quality",
            "permission": "security",
        }
        return categories.get(error.error_type, "unknown")

    # ------------------------------------------------------------------
    # 完整版接口（基于 storage/chunk_db/knowledge_service）
    # ------------------------------------------------------------------

    def get_pending_pipelines(self) -> list[PipelineRunSummary]:
        """获取所有待复盘的 pipeline（status='completed' 且 review_status='pending'）。"""
        if self._storage is None:
            # 向后兼容：从内存 pipelines 中筛选
            return [
                PipelineRunSummary(
                    run_id=p.pipeline_id,
                    status=p.status.value,
                    review_status="pending",
                )
                for p in self._pipelines.values()
                if p.status == ReviewStatus.PENDING
            ]
        summaries = self._storage.list_all_summaries()
        return [
            s for s in summaries
            if s.status == "completed" and s.review_status == "pending"
        ]

    def run_review(self, run_id: str = "") -> Any:
        """执行复盘流程。

        Args:
            run_id: 管道运行 ID。为空时走简化版（内存 pipelines），否则走完整版。

        Returns:
            简化版直接返回 dict；完整版返回 coroutine（需 await）。
        """
        if not run_id:
            return self._run_review_simple()

        return self._run_review_full(run_id)

    def _run_review_simple(self) -> dict[str, Any]:
        """简化版复盘：对内存中注册的 pipelines 进行复盘。"""
        pending = [
            p for p in self._pipelines.values()
            if p.status == ReviewStatus.PENDING
        ]
>>>>>>> D:\myproject\container_08f57__wt_b30e823c\src\memory\maintenance\review_engine.py
        result: dict[str, Any] = {
            "total_pending": len(pending),
            "processed": 0,
            "experiences_extracted": 0,
            "pipeline_results": [],
        }

        for pipeline in pending:
            pipeline.status = ReviewStatus.IN_PROGRESS
<<<<<<< C:\Users\jc\AppData\Local\Temp\tmpea0iy_or\current

            try:
                # 从错误中提取经验
=======
            try:
>>>>>>> D:\myproject\container_08f57__wt_b30e823c\src\memory\maintenance\review_engine.py
                experiences = self._extract_experiences(pipeline)
                pipeline.experiences = experiences
                pipeline.status = ReviewStatus.COMPLETED
                pipeline.reviewed_at = datetime.now(timezone.utc).isoformat()

                result["processed"] += 1
                result["experiences_extracted"] += len(experiences)
                result["pipeline_results"].append({
                    "pipeline_id": pipeline.pipeline_id,
                    "status": "completed",
                    "error_count": len(pipeline.errors),
                    "experience_count": len(experiences),
                })
            except Exception as e:
                pipeline.status = ReviewStatus.FAILED
                result["pipeline_results"].append({
                    "pipeline_id": pipeline.pipeline_id,
                    "status": "failed",
                    "error": str(e),
                })

        return result

<<<<<<< C:\Users\jc\AppData\Local\Temp\tmpea0iy_or\current
    def _extract_experiences(self, pipeline: Pipeline) -> list[Experience]:
        """从 pipeline 的错误记录中提取经验。

        每条错误记录生成一条对应的经验。
        """
        experiences: list[Experience] = []
        for error in pipeline.errors:
            experience = Experience(
                experience_id=f"exp-{uuid.uuid4().hex[:8]}",
                source_error_id=error.error_id,
                lesson=self._generate_lesson(error),
                category=self._categorize_error(error),
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            experiences.append(experience)
        return experiences

    def _generate_lesson(self, error: ErrorRecord) -> str:
        """根据错误类型生成经验教训。"""
        lessons = {
            "timeout": f"操作超时({error.message})：建议增加超时时间或添加重试机制",
            "connection": f"连接失败({error.message})：建议检查网络配置和服务可用性",
            "validation": f"数据验证失败({error.message})：建议加强输入校验",
            "permission": f"权限不足({error.message})：建议检查访问控制配置",
        }
        return lessons.get(error.error_type, f"未知错误({error.message})：建议排查具体原因")

    def _categorize_error(self, error: ErrorRecord) -> str:
        """对错误进行分类。"""
        categories = {
            "timeout": "performance",
            "connection": "infrastructure",
            "validation": "data_quality",
            "permission": "security",
        }
        return categories.get(error.error_type, "unknown")
<<<<<<< C:\Users\jc\AppData\Local\Temp\tmpm3wy5hdt\current
=======

    # -- 任务1：service.py 期望的接口方法 --

    def run_batch_review(self) -> dict[str, Any]:
        """批量复盘，委托给 run_review()。

        Returns:
            复盘结果摘要。
        """
        return self.run_review()

    def get_summary(self) -> dict[str, Any]:
        """返回引擎内部统计摘要。

        Returns:
            包含已注册 pipeline 数、pending 数、已完成数等统计信息。
        """
        total = len(self._pipelines)
        pending = sum(
            1 for p in self._pipelines.values()
            if p.status == ReviewStatus.PENDING
        )
        completed = sum(
            1 for p in self._pipelines.values()
            if p.status == ReviewStatus.COMPLETED
        )
        failed = sum(
            1 for p in self._pipelines.values()
            if p.status == ReviewStatus.FAILED
        )
        return {
            "total_registered": total,
            "pending": pending,
            "completed": completed,
            "failed": failed,
        }

    def reset(self) -> None:
        """清空所有已注册的 pipeline，重置引擎状态。"""
        self._pipelines.clear()

    # -- 日志解析（委托给 log_parser 模块） --

    @classmethod
    def parse_pipeline_logs(cls, log_dir: str | Path) -> list[Pipeline]:
        """扫描 log_dir 下的 pipeline_*.log 文件，解析为 Pipeline 列表。

        委托给 PipelineLogParser 实现，保持向后兼容。

        Args:
            log_dir: 日志目录路径。

        Returns:
            解析出的 Pipeline 列表，可直接传给 register_pipelines()。
        """
        from src.memory.maintenance.log_parser import PipelineLogParser

        return PipelineLogParser.parse_pipeline_logs(log_dir)
>>>>>>> D:\myproject\container_08f57__wt_7f34aa1e\src\memory\maintenance\review_engine.py
=======
    async def _run_review_full(self, run_id: str) -> dict[str, Any]:
        """完整版复盘：基于 storage/chunk_db/knowledge_service。"""
        summary = self._storage.get_summary(run_id)
        if summary is None:
            return {"status": "error", "run_id": run_id, "message": "Pipeline not found"}

        if summary.status != "completed":
            return {"status": "error", "run_id": run_id, "message": "Pipeline not completed"}

        # 标记为复盘中
        self._storage.update_summary(run_id, {"review_status": "reviewing"})

        try:
            # 加载执行记录
            records: list[ExecutionRecord] = self._storage.list_by_pipeline(run_id)

            # 加载已有经验（去重用）
            existing_experiences = await self._load_existing_experiences()

            # 提取新经验
            saved_counts: dict[str, int] = {"experiences": 0}
            for record in records:
                if not record.error:
                    continue
                content = f"Pipeline {run_id} - {record.name}: {record.error}"
                if content in existing_experiences:
                    continue
                await self._knowledge_service.create_knowledge(
                    user_id="system",
                    source_type="review_experience",
                    content=content,
                )
                saved_counts["experiences"] += 1

            # 深度分析（可选）
            if self._pipeline_engine is not None:
                chunks = await self._chunk_db.find_by_pipeline(run_id)
                for chunk in chunks:
                    await self._pipeline_engine.run(
                        user_input=chunk.content,
                        allow_default_fallback=True,
                    )

            # Bug1 修复：使用 saved_counts.get("experiences", 0)
            experience_count = saved_counts.get("experiences", 0)

            # 标记为已复盘
            await self._mark_pipeline_reviewed(run_id)

            return {
                "status": "success",
                "run_id": run_id,
                "experience_count": experience_count,
                "records_analyzed": len(records),
            }
        except Exception as e:
            self._storage.update_summary(run_id, {"review_status": "failed"})
            return {
                "status": "error",
                "run_id": run_id,
                "message": str(e),
            }

    async def _load_existing_experiences(self) -> set[str]:
        """加载已有经验，按 source_type='review_experience' 过滤。

        Bug2 修复：使用 list_semantic_memory(user_id='system') + 按 source_type 过滤。
        """
        try:
            result = await self._knowledge_service.list_semantic_memory(user_id="system")
            items = result.get("items", [])
            return {
                item["content"]
                for item in items
                if item.get("source_type") == "review_experience"
            }
        except Exception:
            return set()

    async def _mark_pipeline_reviewed(self, run_id: str) -> None:
        """标记 pipeline 为已复盘。

        Bug3 修复：从同步改为 async，内部使用 await。
        """
        try:
            chunks = await self._chunk_db.find_by_pipeline(run_id)
            for chunk in chunks:
                chunk.extra_data["reviewed"] = True
                self._chunk_db.save_chunk(chunk)
        except Exception:
            logger.warning("Failed to update chunk reviewed flags for %s", run_id)

        self._storage.update_summary(run_id, {"review_status": "completed"})

    # ------------------------------------------------------------------
    # service.py 期望的方法适配器
    # ------------------------------------------------------------------

    def run_batch_review(self, run_ids: list[str] | None = None) -> dict[str, Any]:
        """批量复盘适配器（同步版，供 MemoryMaintenanceService 调用）。

        Args:
            run_ids: 要复盘的 pipeline ID 列表。为空时复盘所有 pending。

        Returns:
            批量复盘结果。
        """
        if run_ids is None:
            # 简化版：复盘内存中的所有 pending pipelines
            return self._run_review_simple()

        import asyncio
        results: list[dict[str, Any]] = []
        for rid in run_ids:
            try:
                asyncio.get_running_loop()  # 检测是否已有事件循环
                # 已有事件循环中无法用 run_until_complete，用 create_task
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    result = pool.submit(
                        asyncio.run, self._run_review_full(rid)
                    ).result()
                results.append(result)
            except RuntimeError:
                # 没有事件循环，直接 asyncio.run
                results.append(asyncio.run(self._run_review_full(rid)))

        return {
            "total": len(results),
            "results": results,
        }

    def get_summary(self, run_id: str) -> PipelineRunSummary | None:
        """获取单个 pipeline 的复盘摘要。"""
        if self._storage is not None:
            return self._storage.get_summary(run_id)
        p = self._pipelines.get(run_id)
        if p is None:
            return None
        return PipelineRunSummary(
            run_id=p.pipeline_id,
            status=p.status.value,
            review_status="completed" if p.status == ReviewStatus.COMPLETED else "pending",
        )

    def reset(self) -> None:
        """重置引擎状态。"""
        self._pipelines.clear()
>>>>>>> D:\myproject\container_08f57__wt_b30e823c\src\memory\maintenance\review_engine.py
