"""ReviewEngine - 复盘引擎，负责对 pipeline 执行结果进行复盘和经验提取。"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
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


class ReviewEngine:
    """复盘引擎：对 pipeline 执行结果进行复盘，提取经验教训。

    核心职责：
    1. 获取 pending 状态的 pipeline 列表
    2. 对每个 pipeline 进行复盘（分析错误、提取经验）
    3. 更新 pipeline 状态为 completed

    Args:
        storage: 执行记录存储，提供 get_summary/list_by_pipeline (返回 (records, has_more) 元组)/list_all_summaries/update_summary
        chunk_db: 数据块存储，提供 find_by_pipeline/save_chunk
        knowledge_service: 知识服务，提供 list_semantic_memory/create_knowledge
        pipeline_engine: 可选管道引擎，提供 run 方法用于深度分析
    """

    # 已结束状态：track 插件写入 success/failed，旧版/单元测试用 completed。
    # get_pending_pipelines 和 _run_review_full 共用此集合，确保"哪些 pipeline 可复盘"
    # 与"哪些 pipeline 会被复盘"判断一致（单一真相源）。
    _TERMINAL_STATUSES = frozenset({"completed", "success", "failed"})

    def __init__(
        self,
        storage: Any = None,
        chunk_db: Any = None,
        knowledge_service: Any = None,
        pipeline_engine: Any | None = None,
        task_lookup: Any | None = None,
    ) -> None:
        """初始化复盘引擎。

        Args:
            storage: 执行记录存储
            chunk_db: 数据块存储
            knowledge_service: 知识服务
            pipeline_engine: 可选管道引擎，用于深度分析
            task_lookup: 可选的任务反查回调，签名 (pipeline_run_id) -> dict | None。
                用于把 pipeline_run_id 反查到目标 agent 和任务标题。返回字典格式：
                {"agent": "solution_planning_agent", "title": "任务标题"}，查不到返回 None。
                不提供时经验产出不含 agent 身份（仅用任务描述兜底）。
        """
        self._pipelines: dict[str, Pipeline] = {}
        self._storage = storage
        self._chunk_db = chunk_db
        self._knowledge_service = knowledge_service
        self._pipeline_engine = pipeline_engine
        self._task_lookup = task_lookup

    def register_pipeline(self, pipeline: Pipeline) -> None:
        """注册待复盘的 pipeline。"""
        self._pipelines[pipeline.pipeline_id] = pipeline

    def register_pipelines(self, pipelines: list[Pipeline]) -> None:
        """批量注册待复盘的 pipeline。"""
        for p in pipelines:
            self.register_pipeline(p)

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

    def get_pending_pipelines(self) -> list[PipelineRunSummary]:
        """获取所有待复盘的 pipeline。

        过滤条件：review_status='pending' 且 status 为"已结束"状态。
        兼容 track 插件实际写入的 status 值（success/failed/completed）。
        """
        # 已结束状态：track 插件写入 success/failed，旧版/单元测试用 completed
        # （定义见类常量 _TERMINAL_STATUSES，此处仅为保留方法内引用注释）
        if self._storage is None:
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
            if s.status in self._TERMINAL_STATUSES and s.review_status == "pending"
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
        result: dict[str, Any] = {
            "total_pending": len(pending),
            "processed": 0,
            "experiences_extracted": 0,
            "pipeline_results": [],
        }

        for pipeline in pending:
            pipeline.status = ReviewStatus.IN_PROGRESS
            try:
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

    async def _run_review_full(self, run_id: str) -> dict[str, Any]:
        """完整版复盘：基于 storage/chunk_db/knowledge_service。"""
        summary = self._storage.get_summary(run_id)
        if summary is None:
            return {"status": "error", "run_id": run_id, "message": "Pipeline not found"}

        if summary.status not in self._TERMINAL_STATUSES:
            return {"status": "error", "run_id": run_id, "message": "Pipeline not completed"}

        self._storage.update_summary(run_id, {"review_status": "reviewing"})

        try:
            records: list[ExecutionRecord] = self._storage.list_by_pipeline(run_id)[0]

            existing_experiences = await self._load_existing_experiences()
            if existing_experiences is None:
                # fail-closed：加载失败时跳过经验存储，避免去重被绕过导致经验库重复膨胀
                logger.warning(
                    "[ReviewEngine] 已有经验加载失败，本次复盘跳过经验存储以避免重复"
                )

            # 提取任务描述（首条 user 消息）和执行规模，让经验产出可读
            task_desc = self._extract_task_description(records)
            total_iters = getattr(summary, "total_iterations", 0) or 0
            total_secs = getattr(summary, "total_seconds", 0.0) or 0.0
            created_at = getattr(summary, "created_at", "") or ""

            # 反查目标 agent（通过 task_lookup 回调）。
            # 真实数据里约 58% 的管道由任务系统创建，能查到 target_id；
            # 其余是纯对话/手动触发的管道，没有对应 task，此时 agent_info 为 None，
            # 经验产出用 task_desc 兜底（三档完整度，都不崩）。
            agent_info: dict[str, Any] | None = None
            if self._task_lookup is not None:
                try:
                    agent_info = self._task_lookup(run_id)
                except Exception:
                    agent_info = None
            agent_id = (agent_info or {}).get("agent", "") or ""
            # task_lookup 返回的 title 比 user 消息更规范，优先用
            if (agent_info or {}).get("title"):
                task_desc = (agent_info or {})["title"][:80]

            saved_counts: dict[str, int] = {"experiences": 0}
            for record in records:
                if not record.error:
                    continue
                content = self._build_experience_content(
                    run_id, summary.status, record.error,
                    task=task_desc,
                    iterations=total_iters,
                    duration=total_secs,
                    created_at=created_at,
                    source_name=getattr(record, "name", "") or "",
                    agent=agent_id,
                )
                # 加载失败时跳过存储（fail-closed）；成功时去重
                if existing_experiences is None or content in existing_experiences:
                    continue
                await self._knowledge_service.create_knowledge(
                    user_id="system",
                    content=content,
                    source_type="review_experience",
                )
                saved_counts["experiences"] += 1

            if saved_counts["experiences"] == 0 and summary.error:
                content = self._build_experience_content(
                    run_id, summary.status, summary.error,
                    task=task_desc,
                    iterations=total_iters,
                    duration=total_secs,
                    created_at=created_at,
                    agent=agent_id,
                )
                # 加载失败时不存储（fail-closed）；成功时去重
                if existing_experiences is not None and content not in existing_experiences:
                    await self._knowledge_service.create_knowledge(
                        user_id="system",
                        content=content,
                        source_type="review_experience",
                    )
                    saved_counts["experiences"] += 1

            if self._pipeline_engine is not None:
                chunks = await self._chunk_db.find_by_pipeline(run_id)
                for chunk in chunks:
                    await self._pipeline_engine.run(
                        user_input=chunk.content,
                    )

            experience_count = saved_counts.get("experiences", 0)

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

    async def _load_existing_experiences(self) -> set[str] | None:
        """加载已有经验，按 source_type='review_experience' 过滤。

        Returns:
            已有经验内容集合；加载失败时返回 None（fail-closed），
            调用方需检查 None 以避免去重被绕过导致经验库重复膨胀。
        """
        try:
            result = await self._knowledge_service.list_semantic_memory(user_id="system")
            items = result.get("items", [])
            return {
                item["content"]
                for item in items
                if item.get("source_type") == "review_experience"
            }
        except Exception as e:
            # fail-closed：返回 None 让调用方跳过经验存储，避免空集合导致去重失效
            logger.warning("[ReviewEngine] 加载已有经验失败: %s", e)
            return None

    @staticmethod
    def _extract_task_description(records: list[Any]) -> str:
        """从执行记录中提取任务描述（首条 user 消息）。

        真实数据里 summary 没有专门的任务名字段，但管道执行的首条 user 消息
        就是用户下达的任务指令（如"测试评估指标""设计一个容器"）。
        把它带进经验产出，让人一眼看出"这条经验是关于什么任务的"。

        Args:
            records: 该管道的执行记录列表

        Returns:
            任务描述文本（截断到 80 字符），无 user 消息时返回空串
        """
        for record in records:
            # 兼容 storage 的 ExecutionRecordData（role/type/content）
            # 和引擎内部的 ExecutionRecord（type/content）
            role = getattr(record, "role", "")
            rtype = getattr(record, "type", "")
            content = getattr(record, "content", "") or ""
            is_user = role == "user" or rtype == "user"
            if is_user and content.strip():
                # 去掉控制字符和多余空白
                desc = " ".join(content.split())
                return desc[:80] + ("..." if len(desc) > 80 else "")
        return ""

    @staticmethod
    def _build_experience_content(
        run_id: str,
        status: str,
        error: str,
        *,
        task: str = "",
        iterations: int = 0,
        duration: float = 0.0,
        created_at: str = "",
        source_name: str = "",
        agent: str = "",
    ) -> str:
        """构造可读的经验内容。

        格式：[{agent} | {status} | {N}轮 | {秒}s | {时间}] 任务: "{task}" {source_name} -> {error} (pipeline={run_id})

        三档信息完整度（都不崩、都产出可读经验）：
        - 有 task_lookup 且查到：带 agent 身份（如 solution_planning_agent）
        - 有 user 消息但无 agent：带任务描述兜底
        - 两者都没有（纯对话管道）：只有状态+错误+pipeline 追溯

        Args:
            run_id: 管道运行 ID（用于追溯）
            status: 管道状态（success/failed/completed）
            error: 错误内容
            task: 任务描述（首条 user 消息），可选
            iterations: 总迭代轮数，可选
            duration: 总耗时秒数，可选
            created_at: 创建时间，可选
            source_name: 错误来源标识（如步骤名 "step_a"），可选
            agent: 目标 agent（从 task 反查的 target_id），可选

        Returns:
            可读的经验内容字符串
        """
        # 头部：[agent | 状态 | 规模 | 时间]
        head_parts: list[str] = []
        if agent:
            head_parts.append(agent)
        head_parts.append(status)
        if iterations:
            head_parts.append(f"{iterations}轮")
        if duration:
            # 超过 60 秒显示分钟，否则秒
            head_parts.append(f"{duration:.0f}s" if duration < 60 else f"{duration/60:.1f}min")
        if created_at:
            # 只取日期+时分部分
            head_parts.append(created_at[:16])
        head = " | ".join(head_parts)

        # 中间：任务描述（如果有）
        task_part = f' 任务: "{task}"' if task else ""

        # 来源标识（record 级错误用步骤名，summary 级用空）
        source_part = f" [{source_name}]" if source_name else ""

        return f"[{head}]{task_part}{source_part} -> {error} (pipeline={run_id})"

    async def _mark_pipeline_reviewed(self, run_id: str) -> None:
        """标记 pipeline 为已复盘。"""
        # chunk_db 为 None 是合法配置（纯 API 触发场景没有压缩块），
        # 静默跳过 chunk 标记，只更新 summary。真实异常才记 warning。
        if self._chunk_db is not None:
            try:
                chunks = await self._chunk_db.find_by_pipeline(run_id)
                for chunk in chunks:
                    chunk.extra_data["reviewed"] = True
                    self._chunk_db.save_chunk(chunk)
            except Exception:
                logger.warning("Failed to update chunk reviewed flags for %s", run_id)

        self._storage.update_summary(run_id, {"review_status": "completed"})

    def run_batch_review(self, run_ids: list[str] | None = None) -> dict[str, Any]:
        """批量复盘适配器（同步版，供 MemoryMaintenanceService 调用）。

        Args:
            run_ids: 要复盘的 pipeline ID 列表。为空时复盘所有 pending。

        Returns:
            批量复盘结果。
        """
        if run_ids is None:
            return self._run_review_simple()

        import asyncio
        results: list[dict[str, Any]] = []
        for rid in run_ids:
            try:
                asyncio.get_running_loop()
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    result = pool.submit(
                        asyncio.run, self._run_review_full(rid)
                    ).result()
                results.append(result)
            except RuntimeError:
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
