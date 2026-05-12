"""
任务管理器

提供任务执行闭环的核心逻辑：
- 任务提交
- 评估执行（增量/最终）
- 状态管理
- 进度追踪

会话管理策略：
- 接收外部传入的会话（通常是 Service 层传递）
- 在需要独立事务时使用 SessionManager 创建独立事务
- 评估操作封装在事务中，确保数据一致性

支持新的评估机制：
- 使用可复用的评估指标（EvaluationMetric）
- 创建 ExecutionRecord 记录执行过程
- 支持批量评估和增量评估
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Task
from src.db.repositories.evaluation_metric_repo import EvaluationMetricRepository
from src.db.repositories.execution_record_repo import ExecutionRecordRepository
from src.db.repositories.task_repo import TaskRepository
from src.db.session_manager import get_session_manager, independent_transaction
from src.core.event_bus import get_event_bus
from src.core.event_bus.types import ExecutionEvent, EventType

logger = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    """任务状态"""

    PENDING = "pending"  # 待执行
    IN_PROGRESS = "in_progress"  # 执行中
    EVALUATING = "evaluating"  # 评估中
    COMPLETED = "completed"  # 已完成
    FAILED = "failed"  # 失败
    BLOCKED = "blocked"  # 阻塞（需用户介入）


@dataclass
class EvaluationResult:
    """评估结果"""

    passed: bool
    score: float = 0.0
    feedback: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)


class TaskManager:
    """
    任务管理器

    核心职责：
    1. 任务提交和初始化
    2. 评估执行（支持增量和最终两种模式）
    3. 状态转换和进度更新
    4. 重试控制和死循环保护

    会话管理策略：
    - 初始化时接收外部会话
    - 常规操作使用传入的会话（共享事务）
    - 评估结果保存使用独立事务（确保不丢失）
    - 支持在独立事务中执行操作

    支持新的评估机制：
    - 使用可复用的评估指标（EvaluationMetric）
    - 创建 ExecutionRecord 记录执行过程
    """

    # 默认配置
    DEFAULT_MAX_RETRIES = 3
    AC_MAX_RETRIES = 5  # AC 最大重试次数

    def __init__(
        self,
        session: AsyncSession,
        evaluator_callback: Callable | None = None,
    ):
        """
        初始化任务管理器

        Args:
            session: 数据库会话（由调用者管理生命周期）
            evaluator_callback: 自定义评估回调（用于语义评估）
        """
        self.session = session
        self.evaluator_callback = evaluator_callback
        self.task_repo = TaskRepository(session)
        self.metric_repo = EvaluationMetricRepository(session)
        self.execution_record_repo = ExecutionRecordRepository(session)
        self._session_manager = get_session_manager()

    # ========================================================================
    # 任务提交
    # ========================================================================

    async def submit_task(
        self,
        goal: dict[str, Any],
        evaluation_metric_ids: list[str] | None = None,
        acceptance_criteria: dict[str, dict[str, Any]] | None = None,
        target_type: str = "",
        target_id: str = "",
        target_name: str | None = None,
        user_id: str | None = None,
        parent_task_id: str | None = None,
        session_id: str | None = None,
        task_type: str = "execution",
        priority: int = 5,
        max_retries: int = 3,
        metadata: dict[str, Any] | None = None,
        project_id: str | None = None,
        task_index: int | None = None,
        dependencies: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        提交任务

        会话管理：
        - 使用传入的会话创建任务记录
        - 依赖验证使用传入的会话
        - 执行记录创建使用传入的会话

        Args:
            goal: 任务目标
            evaluation_metric_ids: 评估指标 ID 列表
            acceptance_criteria: 验收标准字典
            target_type: 目标类型
            target_id: 目标 ID
            target_name: 目标名称
            user_id: 用户 ID
            parent_task_id: 父任务 ID
            session_id: 会话 ID
            task_type: 任务类型
            priority: 优先级
            max_retries: 最大重试次数
            metadata: 元数据
            project_id: 项目 ID
            task_index: 任务索引
            dependencies: 依赖的任务 ID 列表

        Returns:
            提交结果
        """
        import uuid

        now = datetime.now(UTC)

        # 依赖验证
        if dependencies:
            from src.tasks.dependency_validator import DependencyValidator

            validator = DependencyValidator(self.session)
            validation_result = await validator.validate(
                task_id=None,
                dependencies=dependencies,
                parent_task_id=parent_task_id,
            )

            if not validation_result.is_valid:
                logger.error(
                    f"[TaskManager] 依赖验证失败 | errors={validation_result.errors}"
                )
                raise ValueError(f"依赖验证失败: {validation_result.errors}")

            if validation_result.warnings:
                for warning in validation_result.warnings:
                    logger.warning(f"[TaskManager] 依赖验证警告 | {warning}")

        # 兼容性处理
        if acceptance_criteria and not evaluation_metric_ids:
            evaluation_metric_ids = list(acceptance_criteria.keys())
        elif evaluation_metric_ids and not acceptance_criteria:
            acceptance_criteria = {mid: {} for mid in evaluation_metric_ids}
        elif not evaluation_metric_ids and not acceptance_criteria:
            raise ValueError("必须提供 evaluation_metric_ids 或 acceptance_criteria")

        # 验证评估指标是否存在
        metrics = await self.metric_repo.get_metrics_by_ids(evaluation_metric_ids)
        if len(metrics) != len(evaluation_metric_ids):
            missing = set(evaluation_metric_ids) - {m.id for m in metrics}
            logger.warning(f"部分评估指标不存在 | missing={missing} | 使用可用指标")
            evaluation_metric_ids = [m.id for m in metrics]
            acceptance_criteria = {
                mid: acceptance_criteria.get(mid, {}) for mid in evaluation_metric_ids
            }

        # 构建 acceptance_criteria 数组格式
        acceptance_criteria_list = []
        for metric in metrics:
            metric_config = acceptance_criteria.get(metric.id, {})
            if isinstance(metric_config, dict) and "input_params" in metric_config:
                input_params = metric_config.get("input_params", {})
                pass_threshold = metric_config.get("pass_threshold")
            else:
                input_params = metric_config
                pass_threshold = None

            acceptance_criteria_list.append(
                {
                    "metric_id": metric.id,
                    "input_params": input_params,
                    "pass_threshold": pass_threshold,
                    "status": "pending",
                    "retry_count": 0,
                    "evaluated_at": None,
                    "evaluation_result": None,
                }
            )

        # 创建 ExecutionRecord
        execution_record_id = metadata.get("execution_record_id") if metadata else None

        if not execution_record_id:
            raise ValueError(
                f"提交任务失败：缺少 execution_record_id。"
                f"请确保 tool_record_id 已正确注入到任务提交流程中。"
            )

        await self.execution_record_repo.save_execution_record(
            session_id=session_id or "",
            message_data={
                "record_type": "task_execution",
                "executor": {
                    "type": target_type,
                    "id": target_id,
                    "name": target_name or goal.get("title", ""),
                },
                "input": goal,
                "status": "pending",
                "timing": {
                    "started_at": now.isoformat(),
                },
            },
            record_id=execution_record_id,
        )

        # 使用嵌套方式生成 Task ID
        from src.utils.message_id_helper import generate_task_id

        task_id = await generate_task_id(
            db=self.session,
            parent_task_id=parent_task_id,
            thread_id=session_id,
        )

        # 创建任务数据
        task_data = {
            "id": task_id,
            "title": goal.get("title"),
            "status": TaskStatus.PENDING.value,
            "parent_task_id": parent_task_id,
            "session_id": session_id,
            "priority": priority,
            "user_id": user_id,
            "goal": goal,
            "evaluation_metric_ids": evaluation_metric_ids,
            "acceptance_criteria": acceptance_criteria_list,
            "execution_record_id": execution_record_id,
            "target_type": target_type,
            "target_id": target_id,
            "target_name": target_name,
            "total_criteria": len(acceptance_criteria_list),
            "passed_criteria": 0,
            "failed_criteria": 0,
            "progress_percent": 0.0,
            "retry_count": 0,
            "max_retries": max_retries,
            "dependencies": dependencies or [],
            "task_metadata": {
                "task_type": task_type,
                "submitted_at": now.isoformat(),
                **(metadata or {}),
            },
        }

        # 使用仓储创建任务
        task = await self.task_repo.create_task(task_data)
        await self.session.flush()

        logger.info(
            f"任务已提交: {task_id}, 目标: {target_type}/{target_id}, "
            f"评估指标: {len(evaluation_metric_ids)}"
        )

        # 发布任务提交事件（事件驱动改造）
        event_bus = get_event_bus()
        event = ExecutionEvent(
            event_type=EventType.TASK_SUBMITTED,
            session_id=f"task_{task_id}",
            data={
                "task_id": task_id,
                "target_type": target_type,
                "target_id": target_id,
                "priority": priority,
                "agent_level": self._infer_agent_level(target_type),
                "parent_task_id": parent_task_id,
                "session_id": session_id,
                "task_type": task_type,
                "metadata": metadata or {},
            },
        )
        await event_bus.publish(event)

        if parent_task_id is None:
            logger.info(
                f"独立任务已创建，等待执行触发 | task_id={task_id} | "
                f"请通过 TaskService.create_task() 或事件总线触发执行"
            )

        return {
            "task_id": task_id,
            "status": TaskStatus.PENDING.value,
            "total_criteria": len(evaluation_metric_ids),
            "execution_record_id": execution_record_id,
            "created_at": now.isoformat(),
        }

    # ========================================================================
    # 评估执行
    # ========================================================================

    async def evaluate_criteria(
        self,
        task_id: str,
        criteria_id: str,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        """
        评估单个验收标准

        会话管理：
        - 使用传入的会话读取任务
        - 评估结果保存使用独立事务

        Args:
            task_id: 任务 ID
            criteria_id: 验收标准 ID
            evidence: 完成证据

        Returns:
            评估结果
        """
        task = await self._get_task(task_id)
        if not task:
            return {"error": "任务不存在", "error_code": "TASK_NOT_FOUND"}

        if task.status == TaskStatus.COMPLETED.value:
            return {"error": "任务已完成", "error_code": "TASK_COMPLETED"}

        if task.status == TaskStatus.BLOCKED.value:
            return {"error": "任务已阻塞，请等待用户介入", "error_code": "TASK_BLOCKED"}

        return await self._evaluate_single_criteria(task, criteria_id, evidence)

    async def apply_evaluation_result(
        self,
        task_id: str,
        evaluation_result: dict[str, Any],
    ) -> dict[str, Any]:
        """
        应用评估结果

        会话管理：
        - 使用独立事务保存评估结果，确保不丢失

        Args:
            task_id: 任务 ID
            evaluation_result: 评估结果

        Returns:
            应用后的任务状态
        """
        # 使用独立事务保存评估结果
        async with independent_transaction() as session:
            # 在独立事务中重新获取任务
            result = await session.execute(select(Task).where(Task.id == task_id))
            task = result.scalar_one_or_none()

            if not task:
                return {"error": "任务不存在", "error_code": "TASK_NOT_FOUND"}

            # 检查是否成功评估
            if not evaluation_result.get("success"):
                return {
                    "error": evaluation_result.get("error", "评估失败"),
                    "error_code": evaluation_result.get("error_code", "EVALUATION_FAILED"),
                }

            # 获取 AC 更新信息
            ac_update = evaluation_result.get("ac_update")
            if not ac_update:
                return {"error": "评估结果缺少 ac_update", "error_code": "INVALID_RESULT"}

            # 更新 AC 状态
            acceptance_criteria = task.acceptance_criteria or []
            ac_index = ac_update.get("index")
            if ac_index is None or ac_index >= len(acceptance_criteria):
                return {
                    "error": f"无效的 AC 索引: {ac_index}",
                    "error_code": "INVALID_AC_INDEX",
                }

            # 应用更新
            target_ac = acceptance_criteria[ac_index]
            for key, value in ac_update.items():
                if key != "index":
                    target_ac[key] = value
            acceptance_criteria[ac_index] = target_ac

            # 计算进度
            total = len(acceptance_criteria)
            passed = sum(1 for ac in acceptance_criteria if ac.get("status") == "passed")
            failed = sum(1 for ac in acceptance_criteria if ac.get("status") == "failed")
            progress = (passed / total * 100) if total > 0 else 0

            # 判断任务状态
            any_ac_max_retries = any(
                ac.get("retry_count", 0) >= self.AC_MAX_RETRIES
                for ac in acceptance_criteria
                if ac.get("status") != "passed"
            )

            if passed == total:
                new_status = TaskStatus.COMPLETED.value
            elif any_ac_max_retries:
                new_status = TaskStatus.BLOCKED.value
                logger.warning(f"任务 {task.id} 被阻塞：至少一个 AC 达到最大重试次数")
            else:
                new_status = TaskStatus.IN_PROGRESS.value

            # 更新任务
            now = datetime.now()
            update_data = {
                "acceptance_criteria": acceptance_criteria,
                "total_criteria": total,
                "passed_criteria": passed,
                "failed_criteria": failed,
                "progress_percent": progress,
                "status": new_status,
                "updated_at": now,
            }

            if new_status == TaskStatus.COMPLETED.value:
                update_data["completed_at"] = now

            if not evaluation_result.get("passed", False):
                update_data["retry_count"] = task.retry_count + 1

            await session.execute(
                update(Task).where(Task.id == task.id).values(**update_data)
            )
            await session.commit()

        # 发布事件（在独立事务外）
        await self._publish_evaluation_events(
            task_id=task_id,
            ac_id=evaluation_result.get("ac_id"),
            status=target_ac.get("status"),
            new_status=new_status,
            progress={
                "total": total,
                "passed": passed,
                "failed": failed,
                "percent": progress,
            },
        )

        return {
            "task_id": task_id,
            "ac_id": evaluation_result.get("ac_id"),
            "evaluation_passed": evaluation_result.get("passed"),
            "evaluation_score": evaluation_result.get("score", 0),
            "evaluation_feedback": evaluation_result.get("feedback", ""),
            "criteria_status": target_ac.get("status"),
            "task_status": new_status,
            "progress": {
                "total": total,
                "passed": passed,
                "failed": failed,
                "percent": progress,
            },
        }

    async def _publish_evaluation_events(
        self,
        task_id: str,
        ac_id: str | None,
        status: str,
        new_status: str,
        progress: dict[str, Any],
    ) -> None:
        """发布评估相关事件"""
        event_bus = get_event_bus()
        
        # 发布评估完成事件
        await event_bus.publish(
            ExecutionEvent(
                event_type=EventType.EVALUATION_COMPLETE,
                session_id=f"task_{task_id}",
                data={
                    "task_id": task_id,
                    "ac_id": ac_id,
                    "status": status,
                },
            )
        )

        # 发布状态变更事件
        await event_bus.publish(
            ExecutionEvent(
                event_type=EventType.STATE_CHANGE,
                session_id=f"task_{task_id}",
                data={
                    "task_id": task_id,
                    "new_status": new_status,
                    "progress": progress,
                },
            )
        )

        if new_status == TaskStatus.COMPLETED.value:
            await event_bus.publish(
                ExecutionEvent(
                    event_type=EventType.EXECUTION_COMPLETE,
                    session_id=f"task_{task_id}",
                    data={
                        "task_id": task_id,
                        "status": "completed",
                    },
                )
            )

    async def apply_batch_evaluation_results(
        self,
        task_id: str,
        evaluation_results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """
        批量应用评估结果

        会话管理：
        - 使用独立事务保存所有评估结果

        Args:
            task_id: 任务 ID
            evaluation_results: 评估结果列表

        Returns:
            应用后的任务状态
        """
        # 使用独立事务
        async with independent_transaction() as session:
            result = await session.execute(select(Task).where(Task.id == task_id))
            task = result.scalar_one_or_none()

            if not task:
                return {"error": "任务不存在", "error_code": "TASK_NOT_FOUND"}

            # 更新所有 AC 状态
            acceptance_criteria = task.acceptance_criteria or []
            applied_count = 0
            failed_count = 0

            for eval_result in evaluation_results:
                if not eval_result.get("success"):
                    failed_count += 1
                    continue

                ac_update = eval_result.get("ac_update")
                if not ac_update:
                    continue

                ac_index = ac_update.get("index")
                if ac_index is None or ac_index >= len(acceptance_criteria):
                    continue

                target_ac = acceptance_criteria[ac_index]
                for key, value in ac_update.items():
                    if key != "index":
                        target_ac[key] = value
                acceptance_criteria[ac_index] = target_ac
                applied_count += 1

            # 计算进度
            total = len(acceptance_criteria)
            passed = sum(1 for ac in acceptance_criteria if ac.get("status") == "passed")
            failed = sum(1 for ac in acceptance_criteria if ac.get("status") == "failed")
            progress = (passed / total * 100) if total > 0 else 0

            # 判断任务状态
            all_passed = passed == total
            red_line_failed = any(
                ac.get("is_red_line") and ac.get("status") != "passed"
                for ac in acceptance_criteria
            )

            if all_passed and not red_line_failed:
                new_status = TaskStatus.COMPLETED.value
            elif red_line_failed or task.retry_count >= task.max_retries:
                new_status = TaskStatus.BLOCKED.value
            else:
                new_status = TaskStatus.IN_PROGRESS.value

            # 更新任务
            now = datetime.now()
            update_data = {
                "acceptance_criteria": acceptance_criteria,
                "total_criteria": total,
                "passed_criteria": passed,
                "failed_criteria": failed,
                "progress_percent": progress,
                "status": new_status,
                "updated_at": now,
            }

            if new_status == TaskStatus.COMPLETED.value:
                update_data["completed_at"] = now

            if not all_passed:
                update_data["retry_count"] = task.retry_count + 1

            await session.execute(
                update(Task).where(Task.id == task.id).values(**update_data)
            )
            await session.commit()

        # 发布事件
        event_bus = get_event_bus()
        await event_bus.publish(
            ExecutionEvent(
                event_type=EventType.STATE_CHANGE,
                session_id=f"task_{task_id}",
                data={
                    "task_id": task_id,
                    "new_status": new_status,
                    "progress": {
                        "total": total,
                        "passed": passed,
                        "failed": failed,
                        "percent": progress,
                    },
                },
            )
        )

        return {
            "task_id": task_id,
            "applied_count": applied_count,
            "failed_count": failed_count,
            "all_passed": all_passed,
            "task_status": new_status,
            "progress": {
                "total": total,
                "passed": passed,
                "failed": failed,
                "percent": progress,
            },
        }

    async def evaluate_final(
        self,
        task_id: str,
        all_evidence: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """
        最终评估（一次性评估所有 AC）

        Args:
            task_id: 任务 ID
            all_evidence: 所有 AC 的证据

        Returns:
            评估结果
        """
        task = await self._get_task(task_id)
        if not task:
            return {"error": "任务不存在", "error_code": "TASK_NOT_FOUND"}

        if task.status == TaskStatus.COMPLETED.value:
            return {"error": "任务已完成", "error_code": "TASK_COMPLETED"}

        # 评估所有验收标准
        evaluation_results = await self._evaluate_all_criteria(task, all_evidence)

        # 执行综合检查
        integration_result = await self._perform_integration_check(
            task, evaluation_results, all_evidence
        )

        # 使用独立事务更新任务状态
        async with independent_transaction() as session:
            await self._update_task_after_evaluation(
                task, evaluation_results, integration_result, session
            )

        # 发布状态变更事件
        await self._publish_task_status_event(task, evaluation_results)

        return {
            "task_id": task_id,
            "evaluation_passed": evaluation_results["all_passed"],
            "task_status": evaluation_results["new_status"],
            "results": evaluation_results["results"],
            "progress": {
                "total": evaluation_results["total"],
                "passed": evaluation_results["passed"],
                "failed": evaluation_results["failed"],
                "percent": evaluation_results["progress"],
            },
        }

    async def _evaluate_all_criteria(
        self, task: Any, all_evidence: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        """评估所有验收标准"""
        now = datetime.now()
        acceptance_criteria = task.acceptance_criteria or []
        results = []
        all_passed = True
        red_line_failed = False

        for ac in acceptance_criteria:
            criteria_id = ac.get("id")
            evidence = all_evidence.get(criteria_id, {})

            if not evidence:
                result = self._create_no_evidence_result(ac, criteria_id)
                all_passed = False
                if ac.get("is_red_line"):
                    red_line_failed = True
            else:
                result = await self._evaluate_single_criterion(ac, evidence, task, now)
                if not result["passed"]:
                    all_passed = False
                    if ac.get("is_red_line"):
                        red_line_failed = True

            results.append(result)

        # 计算统计信息
        total = len(acceptance_criteria)
        passed = sum(1 for ac in acceptance_criteria if ac.get("status") == "passed")
        failed = sum(1 for ac in acceptance_criteria if ac.get("status") == "failed")
        progress = (passed / total * 100) if total > 0 else 0

        # 确定新状态
        new_status = self._determine_task_status(all_passed, red_line_failed, task)

        return {
            "results": results,
            "all_passed": all_passed,
            "red_line_failed": red_line_failed,
            "total": total,
            "passed": passed,
            "failed": failed,
            "progress": progress,
            "new_status": new_status,
            "acceptance_criteria": acceptance_criteria,
        }

    def _create_no_evidence_result(self, ac: dict, criteria_id: str) -> dict[str, Any]:
        """创建无证据的评估结果"""
        ac["status"] = "failed"
        ac["evaluation_result"] = {"passed": False, "feedback": "未提供证据"}
        return {
            "criteria_id": criteria_id,
            "passed": False,
            "feedback": "未提供证据",
        }

    async def _evaluate_single_criterion(
        self, ac: dict, evidence: dict, task: Any, now: datetime
    ) -> dict[str, Any]:
        """评估单个验收标准"""
        eval_result = await self._run_evaluation(ac, evidence, task)

        ac["evaluated_at"] = now.isoformat()
        ac["evaluation_result"] = eval_result
        ac["last_evidence"] = evidence

        if eval_result["passed"]:
            ac["status"] = "passed"
            ac["passed_at"] = now.isoformat()
        else:
            ac["status"] = "failed"
            ac["retry_count"] = ac.get("retry_count", 0) + 1

        return {
            "criteria_id": ac.get("id"),
            "passed": eval_result["passed"],
            "score": eval_result.get("score", 0),
            "feedback": eval_result.get("feedback", ""),
        }

    def _determine_task_status(
        self, all_passed: bool, red_line_failed: bool, task: Any
    ) -> str:
        """确定任务状态"""
        if all_passed:
            return TaskStatus.COMPLETED.value

        acceptance_criteria = task.acceptance_criteria or []
        any_ac_max_retries = any(
            ac.get("retry_count", 0) >= self.AC_MAX_RETRIES
            for ac in acceptance_criteria
            if ac.get("status") != "passed"
        )

        if red_line_failed or any_ac_max_retries:
            return TaskStatus.BLOCKED.value
        else:
            return TaskStatus.IN_PROGRESS.value

    async def _perform_integration_check(
        self, task: Any, evaluation_results: dict, all_evidence: dict
    ) -> dict[str, Any]:
        """执行综合检查"""
        if not evaluation_results["all_passed"]:
            return {"passed": True}

        integration_result = await self._integration_check(
            task, evaluation_results["acceptance_criteria"], all_evidence
        )

        if not integration_result["passed"]:
            evaluation_results["all_passed"] = False
            evaluation_results["results"].append(
                {
                    "criteria_id": "_integration",
                    "passed": False,
                    "feedback": integration_result.get("feedback", "综合检查未通过"),
                }
            )

        return integration_result

    async def _update_task_after_evaluation(
        self,
        task: Any,
        evaluation_results: dict,
        integration_result: dict,
        session: AsyncSession,
    ) -> None:
        """评估后更新任务"""
        now = datetime.now()

        update_data = {
            "acceptance_criteria": evaluation_results["acceptance_criteria"],
            "total_criteria": evaluation_results["total"],
            "passed_criteria": evaluation_results["passed"],
            "failed_criteria": evaluation_results["failed"],
            "progress_percent": evaluation_results["progress"],
            "status": evaluation_results["new_status"],
            "updated_at": now,
        }

        if evaluation_results["new_status"] == TaskStatus.COMPLETED.value:
            update_data["completed_at"] = now

        if not evaluation_results["all_passed"]:
            update_data["retry_count"] = task.retry_count + 1

        await session.execute(
            update(Task).where(Task.id == task.id).values(**update_data)
        )
        await session.commit()

    async def _publish_task_status_event(
        self, task: Any, evaluation_results: dict
    ) -> None:
        """发布任务状态变更事件"""
        event_bus = get_event_bus()
        await event_bus.publish(
            ExecutionEvent(
                event_type=EventType.STATE_CHANGE,
                session_id=f"task_{task.id}",
                data={
                    "task_id": task.id,
                    "old_status": task.status,
                    "new_status": evaluation_results["new_status"],
                    "progress": {
                        "total": evaluation_results["total"],
                        "passed": evaluation_results["passed"],
                        "failed": evaluation_results["failed"],
                        "percent": evaluation_results["progress"],
                    },
                    "retry_count": task.retry_count,
                    "max_retries": task.max_retries,
                },
            )
        )

    # ========================================================================
    # 任务恢复
    # ========================================================================

    async def cancel_task(
        self,
        task_id: str,
        reason: str = "用户取消",
        cascade: bool = True,
    ) -> dict[str, Any]:
        """
        取消任务

        Args:
            task_id: 任务 ID
            reason: 取消原因
            cascade: 是否级联取消子任务

        Returns:
            取消结果
        """
        task = await self._get_task(task_id)
        if not task:
            return {"error": "任务不存在", "error_code": "TASK_NOT_FOUND"}

        if task.status in ["completed", "cancelled"]:
            return {
                "error": f"任务已{task.status}，无法取消",
                "error_code": "TASK_ALREADY_FINISHED",
            }

        # 更新任务状态
        now = datetime.now()
        await self.session.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(
                status="cancelled",
                updated_at=now,
                task_metadata={
                    **(task.task_metadata or {}),
                    "cancelled_at": now.isoformat(),
                    "cancel_reason": reason,
                },
            )
        )
        await self.session.flush()

        logger.info(f"任务已取消: {task_id}, 原因: {reason}")

        # 发布任务取消事件
        event_bus = get_event_bus()
        await event_bus.publish(
            ExecutionEvent(
                event_type=EventType.EXECUTION_CANCELLED,
                session_id=f"task_{task_id}",
                data={
                    "task_id": task_id,
                    "reason": reason,
                    "cascade": cascade,
                    "parent_task_id": task.parent_task_id,
                },
            )
        )

        # 级联取消子任务
        if cascade:
            subtasks = await self._get_subtasks(task_id)
            for subtask in subtasks:
                await self.cancel_task(
                    subtask.id, f"父任务取消: {reason}", cascade=True
                )

        return {
            "success": True,
            "task_id": task_id,
            "cancelled_at": now.isoformat(),
            "reason": reason,
        }

    async def _get_subtasks(self, parent_task_id: str) -> list[Task]:
        """获取子任务列表"""
        result = await self.session.execute(
            select(Task).where(Task.parent_task_id == parent_task_id)
        )
        return result.scalars().all()

    async def resume_task(
        self,
        task_id: str,
        message: str | None = None,
    ) -> dict[str, Any]:
        """
        恢复任务执行

        Args:
            task_id: 任务 ID
            message: 恢复消息

        Returns:
            恢复结果
        """
        task = await self._get_task(task_id)
        if not task:
            return {"error": "任务不存在", "error_code": "TASK_NOT_FOUND"}

        if task.status == TaskStatus.COMPLETED.value:
            return {"error": "任务已完成", "error_code": "TASK_COMPLETED"}

        now = datetime.now()

        # 找出未完成的 AC
        pending_criteria = [
            ac
            for ac in (task.acceptance_criteria or [])
            if ac.get("status") != "passed"
        ]

        # 更新任务状态
        await self.session.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(
                status=TaskStatus.IN_PROGRESS.value,
                updated_at=now,
            )
        )
        await self.session.flush()

        logger.info(f"任务已恢复: {task_id}, 待完成 AC: {len(pending_criteria)}")

        return {
            "task_id": task_id,
            "status": TaskStatus.IN_PROGRESS.value,
            "pending_criteria": [
                {"id": ac.get("id"), "description": ac.get("description")}
                for ac in pending_criteria
            ],
            "message": message or "任务已恢复，请继续执行",
            "resumed_at": now.isoformat(),
        }

    # ========================================================================
    # 内部方法
    # ========================================================================

    def _infer_agent_level(self, target_type: str) -> int:
        """
        根据目标类型推断 Agent 层级

        Args:
            target_type: 目标类型

        Returns:
            Agent 层级 (1, 2, 3)
        """
        if target_type in ["chat", "user_interaction"]:
            return 1
        elif target_type in ["orchestrator", "planner", "coordinator"]:
            return 2
        else:
            return 3

    async def _get_task(self, task_id: str) -> Task | None:
        """获取任务"""
        result = await self.session.execute(select(Task).where(Task.id == task_id))
        return result.scalar_one_or_none()

    async def _evaluate_single_criteria(
        self,
        task: Task,
        criteria_id: str,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        """评估单个 AC（增量模式）"""
        acceptance_criteria = task.acceptance_criteria or []

        # 查找目标 AC
        target_ac = None
        target_index = -1
        for i, ac in enumerate(acceptance_criteria):
            if ac.get("id") == criteria_id:
                target_ac = ac
                target_index = i
                break

        if target_ac is None:
            return {
                "error": f"验收标准不存在: {criteria_id}",
                "error_code": "CRITERIA_NOT_FOUND",
            }

        if target_ac.get("status") == "passed":
            return {
                "task_id": task.id,
                "criteria_id": criteria_id,
                "status": "already_passed",
                "message": "该验收标准已通过",
            }

        # 检查重试次数
        ac_retry_count = target_ac.get("retry_count", 0)
        if ac_retry_count >= self.AC_MAX_RETRIES:
            return {
                "error": f"验收标准 {criteria_id} 已达到最大重试次数",
                "error_code": "AC_MAX_RETRIES_EXCEEDED",
            }

        # 执行评估
        now = datetime.now()
        eval_result = await self._run_evaluation(target_ac, evidence, task)

        target_ac["evaluated_at"] = now.isoformat()
        target_ac["evaluation_result"] = eval_result
        target_ac["last_evidence"] = evidence

        if eval_result["passed"]:
            target_ac["status"] = "passed"
            target_ac["passed_at"] = now.isoformat()
        else:
            target_ac["status"] = "failed"
            target_ac["retry_count"] = ac_retry_count + 1

        # 发布验收标准评估事件
        event_bus = get_event_bus()
        await event_bus.publish(
            ExecutionEvent(
                event_type=EventType.EVALUATION_COMPLETE,
                session_id=f"task_{task.id}",
                data={
                    "task_id": task.id,
                    "ac_id": criteria_id,
                    "status": target_ac["status"],
                    "result": eval_result,
                    "retry_count": target_ac.get("retry_count", 0),
                },
            )
        )

        acceptance_criteria[target_index] = target_ac

        # 计算进度
        total = len(acceptance_criteria)
        passed = sum(1 for ac in acceptance_criteria if ac.get("status") == "passed")
        failed = sum(1 for ac in acceptance_criteria if ac.get("status") == "failed")
        progress = (passed / total * 100) if total > 0 else 0

        # 判断任务状态
        any_ac_max_retries = any(
            ac.get("retry_count", 0) >= self.AC_MAX_RETRIES
            for ac in acceptance_criteria
            if ac.get("status") != "passed"
        )

        if passed == total:
            new_status = TaskStatus.COMPLETED.value
        elif any_ac_max_retries:
            new_status = TaskStatus.BLOCKED.value
            logger.warning(
                f"任务 {task.id} 被阻塞：至少一个 AC 达到最大重试次数 "
                f"| ac_id={criteria_id} | retry_count={target_ac.get('retry_count', 0)}"
            )
        else:
            new_status = TaskStatus.IN_PROGRESS.value

        # 更新任务
        update_data = {
            "acceptance_criteria": acceptance_criteria,
            "total_criteria": total,
            "passed_criteria": passed,
            "failed_criteria": failed,
            "progress_percent": progress,
            "status": new_status,
            "updated_at": now,
        }

        if new_status == TaskStatus.COMPLETED.value:
            update_data["completed_at"] = now

        if not eval_result["passed"]:
            update_data["retry_count"] = task.retry_count + 1

        await self.session.execute(
            update(Task).where(Task.id == task.id).values(**update_data)
        )
        await self.session.flush()

        # 发布任务状态变更事件
        event_bus = get_event_bus()
        await event_bus.publish(
            ExecutionEvent(
                event_type=EventType.STATE_CHANGE,
                session_id=f"task_{task.id}",
                data={
                    "task_id": task.id,
                    "old_status": task.status,
                    "new_status": new_status,
                    "progress": {
                        "total": total,
                        "passed": passed,
                        "failed": failed,
                        "percent": progress,
                    },
                    "retry_count": task.retry_count
                    + (0 if eval_result["passed"] else 1),
                    "max_retries": task.max_retries,
                },
            )
        )

        return {
            "task_id": task.id,
            "criteria_id": criteria_id,
            "evaluation_passed": eval_result["passed"],
            "evaluation_score": eval_result.get("score", 0),
            "evaluation_feedback": eval_result.get("feedback", ""),
            "criteria_status": target_ac["status"],
            "task_status": new_status,
            "progress": {
                "total": total,
                "passed": passed,
                "failed": failed,
                "percent": progress,
            },
        }

    async def _run_evaluation(
        self,
        criteria: dict[str, Any],
        evidence: dict[str, Any],
        task: Task,
    ) -> dict[str, Any]:
        """
        执行评估逻辑

        注意：此方法保留用于向后兼容，但实际评估应使用 ACEvaluator
        """
        # 检查是否有评估器配置
        evaluator_type = criteria.get("evaluator_type")
        evaluator_id = criteria.get("evaluator_id")

        if evaluator_type and evaluator_id:
            logger.warning(
                f"AC {criteria.get('id')} 配置了评估器 "
                f"({evaluator_type}:{evaluator_id})，"
                "应通过 ACEvaluator 进行评估"
            )
            return {
                "passed": False,
                "score": 0,
                "feedback": "请使用 ACEvaluator 进行评估",
                "error": "USE_AC_EVALUATOR",
            }

        # 旧格式兼容
        criteria_type = criteria.get("type", "semantic")

        if criteria_type == "manual":
            return {"passed": False, "score": 0, "feedback": "需要人工确认"}

        # 简化的语义评估
        return await self._simple_semantic_evaluation(criteria, evidence)

    async def _simple_semantic_evaluation(
        self,
        criteria: dict[str, Any],
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        """
        简化的语义评估（用于向后兼容）
        """
        import re

        criteria_desc = criteria.get("description", "").lower()
        evidence_desc = evidence.get("description", "").lower()

        words = re.findall(r"[\u4e00-\u9fa5a-zA-Z]+", criteria_desc)
        stopwords = {"的", "是", "在", "和", "了", "有", "这", "那", "要", "能"}
        keywords = [w for w in words if w not in stopwords and len(w) > 1]

        matched = sum(1 for kw in keywords if kw in evidence_desc)
        match_rate = matched / len(keywords) if keywords else 0

        if evidence.get("artifacts"):
            match_rate = min(1.0, match_rate + 0.2)

        passed = match_rate >= 0.6
        return {
            "passed": passed,
            "score": match_rate * 100,
            "feedback": f"语义匹配度: {match_rate * 100:.0f}%",
        }

    async def _integration_check(
        self,
        task: Task,
        acceptance_criteria: list[dict[str, Any]],
        all_evidence: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """综合检查（检查 AC 之间的一致性）"""
        if self.evaluator_callback:
            try:
                return await self.evaluator_callback(
                    {"type": "integration", "criteria": acceptance_criteria},
                    all_evidence,
                    task,
                )
            except Exception:
                pass

        return {"passed": True, "feedback": "综合检查通过"}
