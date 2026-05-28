"""ReviewEngine - 复盘引擎，负责对 pipeline 执行结果进行复盘和经验提取。"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


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


class ReviewEngine:
    """复盘引擎：对 pipeline 执行结果进行复盘，提取经验教训。

    核心职责：
    1. 接收 pending 状态的 pipeline 列表
    2. 对每个 pipeline 进行复盘（分析错误、提取经验）
    3. 更新 pipeline 状态为 completed
    """

    def __init__(self) -> None:
        self._pipelines: dict[str, Pipeline] = {}

    def register_pipeline(self, pipeline: Pipeline) -> None:
        """注册待复盘的 pipeline。"""
        self._pipelines[pipeline.pipeline_id] = pipeline

    def register_pipelines(self, pipelines: list[Pipeline]) -> None:
        """批量注册待复盘的 pipeline。"""
        for p in pipelines:
            self.register_pipeline(p)

    def get_pending_pipelines(self) -> list[Pipeline]:
        """获取所有待复盘的 pipeline。"""
        return [p for p in self._pipelines.values() if p.status == ReviewStatus.PENDING]

    def run_review(self) -> dict[str, Any]:
        """执行复盘流程：对所有 pending pipeline 进行复盘。

        Returns:
            复盘结果摘要，包含处理数量、经验提取数量等。
        """
        pending = self.get_pending_pipelines()
        result: dict[str, Any] = {
            "total_pending": len(pending),
            "processed": 0,
            "experiences_extracted": 0,
            "pipeline_results": [],
        }

        for pipeline in pending:
            pipeline.status = ReviewStatus.IN_PROGRESS

            try:
                # 从错误中提取经验
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
