"""
任务提交服务

负责任务的创建和提交，处理依赖验证、评估指标解析等逻辑。

核心功能：
1. 任务提交和初始化
2. 依赖验证
3. 评估指标解析
4. ExecutionRecord 创建
5. 事件发布
"""

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.event_bus import EventType, ExecutionEvent, get_event_bus
from src.core.states import ExecutionStatus
from src.db.repositories.execution_record_repo import ExecutionRecordRepository
from src.db.repositories.task_repo import TaskRepository
from src.evaluation.metric_loader import get_metric_loader

logger = logging.getLogger(__name__)


class TaskSubmissionService:
    """
    任务提交服务

    负责任务的创建和提交，处理依赖验证、评估指标解析等逻辑。

    核心职责：
    1. 任务提交和初始化
    2. 依赖验证
    3. 评估指标解析
    4. ExecutionRecord 创建
    5. 事件发布

    Example:
        >>> service = TaskSubmissionService(session)
        >>> result = await service.submit(
        ...     goal={"title": "实现登录功能"},
        ...     acceptance_criteria={"file_check": {"input_params": {...}}},
        ... )
    """

    def __init__(self, session: AsyncSession):
        """
        初始化任务提交服务

        Args:
            session: 数据库会话
        """
        self.session = session
        self.task_repo = TaskRepository(session)
        self.metric_loader = get_metric_loader()
        self.execution_record_repo = ExecutionRecordRepository(session)

    async def submit(
        self,
        goal: dict[str, Any],
        evaluation_metric_ids: list[str] | None = None,
        acceptance_criteria: dict[str, dict[str, Any]] | list[dict[str, Any]] | None = None,
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

        Args:
            goal: 任务目标，必须包含 title 字段
            evaluation_metric_ids: 评估指标 ID 列表
            acceptance_criteria: 验收标准字典
            target_type: 目标类型（agent/workflow）
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
            提交结果，包含：
            - task_id: 任务 ID
            - status: 任务状态
            - total_criteria: 总标准数
            - execution_record_id: 执行记录 ID
            - created_at: 创建时间
            - error: 错误信息（如果失败）
        """
        now = datetime.now(UTC)

        # 1. 依赖验证
        if dependencies:
            validation_result = await self._validate_dependencies(
                dependencies=dependencies,
                parent_task_id=parent_task_id,
            )
            if not validation_result["is_valid"]:
                return {
                    "error": f"依赖验证失败: {validation_result['errors']}",
                    "error_code": "DEPENDENCY_VALIDATION_FAILED",
                }

        # 2. 处理评估指标
        metric_result = await self._resolve_metrics(
            evaluation_metric_ids=evaluation_metric_ids,
            acceptance_criteria=acceptance_criteria,
        )

        if metric_result.get("error"):
            return metric_result

        evaluation_metric_ids = metric_result["evaluation_metric_ids"]
        acceptance_criteria_list = metric_result["acceptance_criteria_list"]

        # 3. 创建 ExecutionRecord
        execution_record_id = await self._create_execution_record(
            session_id=session_id or "",
            goal=goal,
            target_type=target_type,
            target_id=target_id,
            target_name=target_name or goal.get("title", ""),
            metadata=metadata,
            now=now,
        )

        # 4. 生成 Task ID（使用嵌套方式）
        task_id = await self._generate_task_id(
            parent_task_id=parent_task_id,
            session_id=session_id,
        )

        # 5. 创建任务数据
        task_data = {
            "id": task_id,
            "title": goal.get("title"),
            "status": ExecutionStatus.PENDING.value,
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

        # 6. 使用仓储创建任务
        await self.task_repo.create_task(task_data)
        await self.session.flush()

        logger.info(
            f"[TaskSubmissionService] 任务已提交 | "
            f"task_id={task_id} | target={target_type}/{target_id} | "
            f"metrics={len(evaluation_metric_ids)}"
        )

        # 7. 发布任务提交事件
        await self._publish_submitted_event(
            task_id=task_id,
            target_type=target_type,
            target_id=target_id,
            priority=priority,
            parent_task_id=parent_task_id,
            session_id=session_id,
            task_type=task_type,
            metadata=metadata,
        )

        return {
            "task_id": task_id,
            "status": ExecutionStatus.PENDING.value,
            "total_criteria": len(evaluation_metric_ids),
            "execution_record_id": execution_record_id,
            "created_at": now.isoformat(),
        }

    async def _validate_dependencies(
        self,
        dependencies: list[str],
        parent_task_id: str | None,
    ) -> dict[str, Any]:
        """
        验证依赖关系

        Args:
            dependencies: 依赖的任务 ID 列表
            parent_task_id: 父任务 ID

        Returns:
            验证结果
        """
        from src.tasks.dependency_validator import DependencyValidator

        validator = DependencyValidator(self.session)
        validation_result = await validator.validate(
            task_id=None,
            dependencies=dependencies,
            parent_task_id=parent_task_id,
        )

        if not validation_result.is_valid:
            logger.error(
                f"[TaskSubmissionService] 依赖验证失败 | errors={validation_result.errors}"
            )
            return {
                "is_valid": False,
                "errors": validation_result.errors,
            }

        if validation_result.warnings:
            for warning in validation_result.warnings:
                logger.warning(f"[TaskSubmissionService] 依赖验证警告 | {warning}")

        return {"is_valid": True, "warnings": validation_result.warnings}

    async def _resolve_metrics(
        self,
        evaluation_metric_ids: list[str] | None,
        acceptance_criteria: dict[str, dict[str, Any]] | list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        """
        解析评估指标

        Args:
            evaluation_metric_ids: 评估指标 ID 列表
            acceptance_criteria: 验收标准字典或列表（支持从 task_submit 工具传入的列表格式）

        Returns:
            解析结果，包含 evaluation_metric_ids 和 acceptance_criteria_list
        """
        # 如果 acceptance_criteria 已经是列表格式，直接返回
        if isinstance(acceptance_criteria, list):
            # 从列表中提取 evaluation_metric_ids（非自定义指标）
            if not evaluation_metric_ids:
                evaluation_metric_ids = [
                    ac["metric_id"] for ac in acceptance_criteria if not ac.get("is_custom")
                ]
            return {
                "evaluation_metric_ids": evaluation_metric_ids,
                "acceptance_criteria_list": acceptance_criteria,
            }

        # 兼容性处理（字典格式）
        if acceptance_criteria and not evaluation_metric_ids:
            evaluation_metric_ids = list(acceptance_criteria.keys())
        elif evaluation_metric_ids and not acceptance_criteria:
            acceptance_criteria = {mid: {} for mid in evaluation_metric_ids}
        elif not evaluation_metric_ids and not acceptance_criteria:
            return {
                "error": "必须提供 evaluation_metric_ids 或 acceptance_criteria",
                "error_code": "MISSING_METRICS",
            }

        # 验证评估指标是否存在
        metrics = await self.metric_loader.get_metrics_by_ids(evaluation_metric_ids)
        if len(metrics) != len(evaluation_metric_ids):
            found_ids = {m.get("id") for m in metrics}
            missing = set(evaluation_metric_ids) - found_ids
            missing_list = sorted(list(missing))

            # 获取可用的评估指标列表，用于提示用户
            available_metrics = await self.metric_loader.list_metrics(limit=10)
            available_metrics_str = ", ".join([
                f"{m.get('id')}({m.get('name')})" for m in available_metrics[:5]
            ])

            error_msg = (
                f"任务评估指标不存在: {missing_list}\n"
                f"\n建议操作:\n"
                f"1. 创建这些评估指标后再提交任务\n"
                f"2. 使用系统中已存在的评估指标，例如: {available_metrics_str}\n"
                f"3. 查看完整的评估指标列表，选择合适的指标"
            )

            logger.error(
                f"[TaskSubmissionService] 任务提交失败: 评估指标不存在 | missing={missing_list}"
            )

            return {
                "error": error_msg,
                "error_code": "EVALUATION_METRICS_NOT_FOUND",
                "missing_metrics": missing_list,
                "available_metrics": [
                    {"id": m.get("id"), "name": m.get("name")} for m in available_metrics[:10]
                ],
            }

        # 构建 acceptance_criteria 数组格式
        acceptance_criteria_list = []
        for metric in metrics:
            metric_id = metric.get("id")
            metric_config = acceptance_criteria.get(metric_id, {})
            if isinstance(metric_config, dict) and "input_params" in metric_config:
                input_params = metric_config.get("input_params", {})
                pass_threshold = metric_config.get("pass_threshold")
            else:
                input_params = metric_config
                pass_threshold = None

            acceptance_criteria_list.append(
                {
                    "metric_id": metric_id,
                    "input_params": input_params,
                    "pass_threshold": pass_threshold,
                    "status": "pending",
                    "retry_count": 0,
                    "evaluated_at": None,
                    "evaluation_result": None,
                }
            )

        return {
            "evaluation_metric_ids": evaluation_metric_ids,
            "acceptance_criteria_list": acceptance_criteria_list,
        }

    async def _create_execution_record(
        self,
        session_id: str,
        goal: dict[str, Any],
        target_type: str,
        target_id: str,
        target_name: str,
        metadata: dict[str, Any] | None,
        now: datetime,
    ) -> str:
        """
        获取或创建执行记录

        如果 metadata 中已有 execution_record_id（由 task_submit 工具创建），
        直接返回它，不创建新记录。

        Args:
            session_id: 会话 ID
            goal: 任务目标
            target_type: 目标类型
            target_id: 目标 ID
            target_name: 目标名称
            metadata: 元数据
            now: 当前时间

        Returns:
            执行记录 ID
        """
        execution_record_id = metadata.get("execution_record_id") if metadata else None

        if execution_record_id:
            # 已有执行记录ID（由 task_submit 工具创建），直接返回
            logger.info(
                f"[TaskSubmissionService] 使用已有执行记录ID | id={execution_record_id}"
            )
            return execution_record_id

        # 没有执行记录ID，创建新的（API 测试场景）
        from src.utils.message_id_helper import generate_execution_record_id

        execution_record_id = await generate_execution_record_id(
            self.session, session_id or "unknown"
        )
        logger.info(
            f"[TaskSubmissionService] 生成执行记录ID | id={execution_record_id}"
        )

        await self.execution_record_repo.save_execution_record(
            session_id=session_id or "",
            message_data={
                "record_type": "task_execution",
                "executor": {
                    "type": target_type,
                    "id": target_id,
                    "name": target_name,
                },
                "input": goal,
                "status": "pending",
                "timing": {
                    "started_at": now.isoformat(),
                },
            },
            record_id=execution_record_id,
        )

        return execution_record_id

    async def _generate_task_id(
        self,
        parent_task_id: str | None = None,
        session_id: str | None = None,
    ) -> str:
        """
        生成任务ID（使用嵌套方式）

        任务ID生成规则：
        - 根任务（无 parent_task_id）：基于 session_id 生成
        - 子任务（有 parent_task_id）：基于 parent_task_id 嵌套生成

        Args:
            parent_task_id: 父任务ID（可选）
            session_id: 会话ID（根任务需要）

        Returns:
            任务ID
        """
        from src.utils.message_id_helper import generate_task_id

        return await generate_task_id(
            db=self.session,
            parent_task_id=parent_task_id,
            thread_id=session_id,
        )

    async def _publish_submitted_event(
        self,
        task_id: str,
        target_type: str,
        target_id: str,
        priority: int,
        parent_task_id: str | None,
        session_id: str | None,
        task_type: str,
        metadata: dict[str, Any] | None,
    ) -> None:
        """
        发布任务提交事件

        Args:
            task_id: 任务 ID
            target_type: 目标类型
            target_id: 目标 ID
            priority: 优先级
            parent_task_id: 父任务 ID
            session_id: 会话 ID
            task_type: 任务类型
            metadata: 元数据
        """
        event_bus = get_event_bus()
        await event_bus.publish(
            ExecutionEvent(
                event_type=EventType.TASK_SUBMITTED,
                session_id=session_id,
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
        )

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
