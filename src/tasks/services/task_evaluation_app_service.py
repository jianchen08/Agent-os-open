"""
任务评估应用服务 - 统一入口

提供任务评估的统一入口，整合 API 路由、工具和工作流执行器的评估逻辑。

核心功能：
1. 评估单个 AC
2. 评估所有 AC
3. 获取评估状态
4. 权限验证

设计原则：
- 统一入口：所有评估请求都通过此服务处理
- 职责分离：评估执行委托给 UnifiedEvaluationEngine，结果应用委托给 EvaluationService
- 接口不变：对外接口签名和返回值保持一致
"""

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas.tasks import (
    AcceptanceCriterionStatus,
    ACEvaluationResult,
    TaskACListResponse,
)
from src.db.models import EvaluationMetric, Task
from src.evaluation import ContextBuilder
from src.evaluation.unified_engine import UnifiedEvaluationEngine
from src.tasks.services.evaluation_service import EvaluationService
from src.tasks.services.state_service import TaskStateService
from src.tools.executor import ToolExecutor
from src.tools.global_registry import get_global_tool_registry_sync

logger = logging.getLogger(__name__)


class TaskEvaluationAppService:
    """
    任务评估应用服务 - 统一入口

    整合所有评估入口的逻辑，提供统一的评估接口。

    核心职责：
    1. 权限验证
    2. 评估执行（委托给 UnifiedEvaluationEngine）
    3. 结果应用（委托给 EvaluationService）
    4. 状态查询

    Attributes:
        session: 数据库会话
        evaluation_service: 评估服务
        state_service: 任务状态服务

    Example:
        >>> service = TaskEvaluationAppService(session, evaluation_service, state_service)
        >>> result = await service.evaluate_single_ac(
        ...     task_id="task-001",
        ...     ac_id="ac-001",
        ...     evidence={"description": "完成说明"},
        ... )
    """

    def __init__(
        self,
        session: AsyncSession,
        evaluation_service: EvaluationService,
        state_service: TaskStateService,
    ):
        """
        初始化任务评估应用服务

        Args:
            session: 数据库会话
            evaluation_service: 评估服务实例
            state_service: 任务状态服务实例
        """
        self.session = session
        self.evaluation_service = evaluation_service
        self.state_service = state_service

        # 延迟初始化
        self._evaluation_engine: UnifiedEvaluationEngine | None = None
        self._tool_executor: ToolExecutor | None = None

    def _get_evaluation_engine(self) -> UnifiedEvaluationEngine:
        """
        获取统一评估引擎实例

        Returns:
            UnifiedEvaluationEngine 实例
        """
        if self._evaluation_engine is None:
            registry = get_global_tool_registry_sync()
            self._tool_executor = ToolExecutor(registry)
            self._evaluation_engine = UnifiedEvaluationEngine(
                session=self.session,
                tool_registry=self._tool_executor._registry,
            )
        return self._evaluation_engine

    async def verify_task_access(
        self,
        task_id: str,
        user_id: str | None = None,
    ) -> tuple[bool, Task | None]:
        """
        验证用户是否有权访问任务

        Args:
            task_id: 任务 ID
            user_id: 用户 ID（可选，为空时跳过权限检查）

        Returns:
            (是否有权访问, 任务对象)
        """
        result = await self.session.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()

        if task is None:
            return False, None

        # 如果未提供 user_id，跳过权限检查
        if user_id is None:
            return True, task

        # 检查任务所属用户
        return task.user_id == user_id, task

    async def evaluate_single_ac(
        self,
        task_id: str,
        ac_id: str,
        evidence: dict[str, Any] | None = None,
        user_id: str | None = None,
    ) -> ACEvaluationResult:
        """
        评估单个 AC

        统一的 AC 评估入口，整合权限验证、评估执行和结果应用。

        Args:
            task_id: 任务 ID
            ac_id: AC ID
            evidence: 评估证据（可选）
            user_id: 用户 ID（可选，用于权限验证）

        Returns:
            ACEvaluationResult: 评估结果

        Raises:
            ValueError: 任务不存在、AC 不存在或评估配置错误
        """
        # 1. 验证权限并获取任务
        has_access, task = await self.verify_task_access(task_id, user_id)
        if not has_access or task is None:
            raise ValueError(f"任务不存在: {task_id}")

        # 2. 查找目标 AC
        acceptance_criteria = task.acceptance_criteria or []
        target_ac = None

        for _i, ac in enumerate(acceptance_criteria):
            if ac.get("id") == ac_id:
                target_ac = ac
                break

        if target_ac is None:
            raise ValueError(f"验收标准不存在: {ac_id}")

        # 3. 检查是否已通过
        if target_ac.get("status") == "passed":
            raise ValueError("该验收标准已通过")

        # 4. 如果提供了证据，先更新到 AC 中
        if evidence:
            target_ac["last_evidence"] = evidence
            from sqlalchemy import update
            await self.session.execute(
                update(Task)
                .where(Task.id == task_id)
                .values(acceptance_criteria=acceptance_criteria)
            )
            await self.session.commit()

        # 5. 准备评估配置
        evaluator_type = target_ac.get("evaluator_type", "tool")
        evaluator_id = target_ac.get("evaluator_id", "")

        if not evaluator_id:
            raise ValueError("验收标准未配置评估器")

        # 6. 构建评估上下文
        context_builder = ContextBuilder()
        eval_context = context_builder.build(
            task_goal=task.goal or {},
            criteria=target_ac,
            evidence=target_ac.get("last_evidence", {}),
            metadata={
                "task_id": task_id,
                "task_title": task.title,
                "task_description": task.description,
            },
        )

        # 7. 执行评估
        metric_config = {
            "id": ac_id,
            "name": target_ac.get("name", ac_id),
            "evaluator_type": evaluator_type,
            "evaluator_id": evaluator_id,
            "timeout_seconds": target_ac.get("timeout", 300),
        }

        engine = self._get_evaluation_engine()
        engine_result = await engine.evaluate(
            metric_config=metric_config,
            context=eval_context,
            timeout=metric_config["timeout_seconds"],
        )

        # 8. 应用评估结果
        evaluated_at = datetime.now(UTC).isoformat()
        apply_result = await self.evaluation_service.apply_evaluation_results(
            task_id=task_id,
            evaluation_results=[
                {
                    "metric_id": ac_id,
                    "passed": engine_result.passed,
                    "score": engine_result.score,
                    "feedback": engine_result.message,
                    "details": engine_result.details,
                }
            ],
        )

        if "error" in apply_result:
            raise ValueError(apply_result.get("error", "应用评估结果失败"))

        # 9. 返回结果
        return ACEvaluationResult(
            task_id=task_id,
            ac_id=ac_id,
            passed=engine_result.passed,
            score=engine_result.score,
            feedback=engine_result.message,
            details=engine_result.details,
            execution_time=(engine_result.execution_time_ms or 0) / 1000,
            evaluated_at=evaluated_at,
        )

    async def evaluate_all_acs(
        self,
        task_id: str,
        parallel: bool = True,
        user_id: str | None = None,
    ) -> TaskACListResponse:
        """
        评估所有 AC

        统一的批量评估入口，支持并行或串行执行。

        Args:
            task_id: 任务 ID
            parallel: 是否并行评估，默认 True
            user_id: 用户 ID（可选，用于权限验证）

        Returns:
            TaskACListResponse: 评估结果列表

        Raises:
            ValueError: 任务不存在或评估配置错误
        """
        # 1. 验证权限并获取任务
        has_access, task = await self.verify_task_access(task_id, user_id)
        if not has_access or task is None:
            raise ValueError(f"任务不存在: {task_id}")

        acceptance_criteria = task.acceptance_criteria or []

        if not acceptance_criteria:
            return TaskACListResponse(
                task_id=task_id,
                total=0,
                passed=0,
                failed=0,
                pending=0,
                acceptance_criteria=[],
            )

        # 2. 筛选待评估的 AC
        pending_acs = [
            ac
            for ac in acceptance_criteria
            if ac.get("status") not in ("passed", "evaluating")
        ]

        if not pending_acs:
            # 所有 AC 已评估，直接返回当前状态
            return self._build_ac_list_response(task_id, acceptance_criteria)

        # 3. 准备评估指标配置
        metrics = []
        for ac in pending_acs:
            evaluator_type = ac.get("evaluator_type", "tool")
            evaluator_id = ac.get("evaluator_id", "")
            if evaluator_id:
                metrics.append({
                    "id": ac.get("id"),
                    "name": ac.get("name", ac.get("id")),
                    "evaluator_type": evaluator_type,
                    "evaluator_id": evaluator_id,
                    "timeout_seconds": ac.get("timeout", 300),
                })

        if not metrics:
            raise ValueError("没有可评估的验收标准（缺少评估器配置）")

        # 4. 构建评估上下文
        context_builder = ContextBuilder()
        eval_context = context_builder.build(
            task_goal=task.goal or {},
            criteria={},
            evidence={},
            metadata={
                "task_id": task_id,
                "task_title": task.title,
                "task_description": task.description,
            },
        )

        # 5. 执行批量评估
        engine = self._get_evaluation_engine()
        summary = await engine.evaluate_batch(
            metrics=metrics,
            context=eval_context,
            parallel=parallel,
            timeout=300,
        )

        # 6. 转换结果为应用格式
        evaluated_at = datetime.now(UTC).isoformat()
        eval_results = []

        for result in summary.results:
            ac_id = result.metric_id
            ac_index = -1
            for idx, ac in enumerate(acceptance_criteria):
                if ac.get("id") == ac_id:
                    ac_index = idx
                    break

            if ac_index >= 0:
                eval_results.append({
                    "metric_id": ac_id,
                    "passed": result.passed,
                    "score": result.score,
                    "feedback": result.message or "",
                    "details": result.details or {},
                })

        # 7. 批量应用评估结果
        apply_result = await self.evaluation_service.apply_evaluation_results(
            task_id=task_id,
            evaluation_results=eval_results,
        )

        if "error" in apply_result:
            raise ValueError(apply_result.get("error", "应用评估结果失败"))

        # 8. 重新获取任务以获取最新状态
        result = await self.session.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()

        if task is None:
            raise ValueError(f"任务不存在: {task_id}")

        # 9. 返回结果
        return self._build_ac_list_response(task_id, task.acceptance_criteria or [])

    async def get_evaluation_status(
        self,
        task_id: str,
        user_id: str | None = None,
    ) -> TaskACListResponse:
        """
        获取评估状态

        查询任务的所有 AC 及其评估状态。

        Args:
            task_id: 任务 ID
            user_id: 用户 ID（可选，用于权限验证）

        Returns:
            TaskACListResponse: AC 状态列表

        Raises:
            ValueError: 任务不存在
        """
        # 1. 验证权限并获取任务
        has_access, task = await self.verify_task_access(task_id, user_id)
        if not has_access or task is None:
            raise ValueError(f"任务不存在: {task_id}")

        # 2. 返回状态
        return self._build_ac_list_response(task_id, task.acceptance_criteria or [])

    def _build_ac_list_response(
        self,
        task_id: str,
        acceptance_criteria: list[dict[str, Any]],
    ) -> TaskACListResponse:
        """
        构建 AC 列表响应

        Args:
            task_id: 任务 ID
            acceptance_criteria: 验收标准列表

        Returns:
            TaskACListResponse: AC 列表响应
        """
        ac_list = []
        total = len(acceptance_criteria)
        passed = 0
        failed = 0
        pending = 0

        for ac in acceptance_criteria:
            ac_status = ac.get("status", "pending")

            if ac_status == "passed":
                passed += 1
            elif ac_status == "failed":
                failed += 1
            else:
                pending += 1

            ac_list.append(
                AcceptanceCriterionStatus(
                    id=ac.get("id", ""),
                    description=ac.get("description", ""),
                    type=ac.get("type"),
                    is_red_line=ac.get("is_red_line", False),
                    weight=ac.get("weight", 1.0),
                    status=ac_status,
                    evaluator_type=ac.get("evaluator_type"),
                    evaluator_id=ac.get("evaluator_id"),
                    evaluated_at=ac.get("evaluated_at"),
                    retry_count=ac.get("retry_count", 0),
                    evaluation_result=ac.get("evaluation_result"),
                )
            )

        return TaskACListResponse(
            task_id=task_id,
            total=total,
            passed=passed,
            failed=failed,
            pending=pending,
            acceptance_criteria=ac_list,
        )

    async def evaluate_by_metric_ids(
        self,
        task_id: str,
        metric_ids: list[str] | None = None,
        evidence: dict[str, Any] | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """
        根据指标 ID 列表评估任务

        用于 TaskEvaluateTool 和 WorkflowExecutor 的评估入口。
        支持 EvaluationMetric 表的评估指标。

        Args:
            task_id: 任务 ID
            metric_ids: 要评估的指标 ID 列表，None 表示评估全部
            evidence: 评估证据
            user_id: 用户 ID（可选）

        Returns:
            评估结果，包含：
            - task_id: 任务 ID
            - task_status: 任务状态
            - progress: 进度信息
            - evaluation_summary: 评估汇总
            - message: 结果消息

        Raises:
            ValueError: 任务不存在或评估配置错误
        """
        # 1. 获取任务
        task = await self.session.get(Task, task_id)
        if not task:
            raise ValueError(f"任务不存在: {task_id}")

        # 2. 设置评估中状态
        task.status = "evaluating"
        await self.session.commit()

        # 3. 确定要评估的指标
        all_metric_ids = task.evaluation_metric_ids or []
        if not all_metric_ids:
            # 没有评估指标，返回错误
            # 任务必须通过评估流程完成，不能无指标直接完成
            return {
                "task_id": task_id,
                "task_status": "blocked",
                "error": "任务没有配置评估指标，无法完成评估",
                "message": "请先为任务配置评估指标",
            }

        # 如果未指定 metric_ids，则评估全部
        target_metric_ids = metric_ids if metric_ids else all_metric_ids

        # 4. 获取指标定义
        result = await self.session.execute(
            select(EvaluationMetric).where(EvaluationMetric.id.in_(target_metric_ids))
        )
        metrics = list(result.scalars().all())

        if not metrics:
            raise ValueError("未找到有效的评估指标")

        # 5. 准备评估上下文
        context = {
            "criteria": {
                "description": task.description or "",
            },
            "evidence": evidence or {},
            "task_goal": {
                "id": task.id,
                "title": task.title,
                "description": task.description,
            },
            "metadata": {
                "task_id": task.id,
            },
        }

        # 6. 使用 UnifiedEvaluationEngine 批量评估
        engine = self._get_evaluation_engine()
        metric_configs = [
            {
                "id": m.id,
                "name": m.name,
                "evaluator_type": m.evaluator_type,
                "evaluator_id": m.evaluator_id,
                "input_mapping": m.default_config.get("input_mapping") if m.default_config else None,
                "output_mapping": m.default_config.get("output_mapping") if m.default_config else None,
                "default_config": m.default_config,
                "timeout_seconds": m.default_config.get("timeout_seconds", 300) if m.default_config else 300,
            }
            for m in metrics
        ]

        batch_result = await engine.evaluate_batch(
            metrics=metric_configs,
            context=context,
            parallel=True,
        )

        # 7. 转换评估结果
        evaluation_results = []
        for i, metric in enumerate(metrics):
            eval_result = batch_result.results[i] if i < len(batch_result.results) else None
            if eval_result:
                result_item = {
                    "metric_id": metric.id,
                    "metric_name": metric.name,
                    "passed": eval_result.passed,
                    "score": eval_result.score,
                    "feedback": eval_result.message or eval_result.feedback or "",
                    "details": eval_result.details or {},
                }
            else:
                result_item = {
                    "metric_id": metric.id,
                    "metric_name": metric.name,
                    "passed": False,
                    "score": 0.0,
                    "feedback": "评估结果缺失",
                }
            evaluation_results.append(result_item)

        # 8. 使用 EvaluationService.apply_evaluation_results 更新任务状态
        update_result = await self.evaluation_service.apply_evaluation_results(
            task_id=task_id,
            evaluation_results=evaluation_results,
        )

        # 9. 构建返回结果
        if update_result.get("error"):
            raise ValueError(update_result.get("error", "应用评估结果失败"))

        task_status = update_result.get("task_status")
        progress = update_result.get("progress", {})

        if task_status == "completed":
            return {
                "task_id": task_id,
                "task_status": "completed",
                "message": f"任务完成！所有评估指标通过 ({progress.get('passed')}/{progress.get('total')})",
                "evaluation_summary": {
                    "total": progress.get("total", 0),
                    "passed": progress.get("passed", 0),
                    "failed": progress.get("failed", 0),
                    "results": evaluation_results,
                },
            }
        elif task_status == "blocked":
            return {
                "task_id": task_id,
                "task_status": "blocked",
                "message": f"任务阻塞：有指标达到最大重试次数 ({progress.get('passed')}/{progress.get('total')})",
                "evaluation_summary": {
                    "total": progress.get("total", 0),
                    "passed": progress.get("passed", 0),
                    "failed": progress.get("failed", 0),
                    "results": evaluation_results,
                },
            }
        else:
            # 收集失败信息
            failed_metrics = [
                {
                    "metric_id": r.get("metric_id"),
                    "metric_name": r.get("metric_name"),
                    "feedback": r.get("feedback", ""),
                }
                for r in evaluation_results
                if not r.get("passed", False)
            ]

            return {
                "task_id": task_id,
                "task_status": "running",
                "message": f"任务继续：{len(failed_metrics)} 个评估指标失败 ({progress.get('passed')}/{progress.get('total')})",
                "failed_metrics": failed_metrics,
                "evaluation_summary": {
                    "total": progress.get("total", 0),
                    "passed": progress.get("passed", 0),
                    "failed": progress.get("failed", 0),
                    "results": evaluation_results,
                },
                "next_step": "修复失败的指标后重新评估",
            }
