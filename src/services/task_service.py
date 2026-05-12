"""
任务服务

提供任务的 CRUD 操作和业务逻辑

会话管理策略：
- 接收外部传入的会话（通常是 API 层的 FastAPI 依赖注入）
- 在需要独立事务时使用 SessionManager 创建独立事务
- 保持与调用者的事务边界清晰

支持新的数据库模型：
- 使用 evaluation_metric_ids 替代 acceptance_criteria
- 关联 ExecutionRecord 记录执行过程
- 支持可复用的评估指标

执行入口统一设计：
- TaskService.create_task() 是唯一的任务执行入口
- 通过线程池执行后台任务，避免与主事件循环冲突
- 使用 _executing_tasks 集合防止重复执行
"""

import asyncio
import concurrent.futures
import logging
import threading
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.constants import TaskPriority
from src.db.models import Task
from src.db.repositories.execution_record_repo import ExecutionRecordRepository
from src.db.repositories.task_repo import TaskRepository
from src.db.session_manager import independent_transaction
from src.evaluation.metric_loader import get_metric_loader

logger = logging.getLogger(__name__)

# 全局线程池执行器，用于在独立线程中运行后台任务
_background_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="task_bg_"
)

# 全局集合，用于跟踪正在执行的任务（防止重复执行）
_executing_tasks: set[str] = set()
_executing_lock = threading.Lock()


def _run_task_in_thread(task_id: str):
    """
    在独立线程中运行任务，创建新的事件循环

    这个函数在独立线程中执行，确保：
    1. 拥有独立的事件循环
    2. 不受 API 请求生命周期影响
    3. 可以可靠地执行长时间运行的任务
    4. 使用全局集合防止重复执行

    注意：TaskExecutor 内部使用 SessionManager 管理会话，
    因此这里不需要传递会话参数。

    Args:
        task_id: 任务 ID
    """
    thread_id = threading.current_thread().ident
    thread_name = threading.current_thread().name

    # 检查任务是否已在执行中
    with _executing_lock:
        if task_id in _executing_tasks:
            logger.warning(
                "[后台任务] 任务已在执行中，跳过重复执行 | task_id=%s | thread=%s",
                task_id,
                thread_name,
            )
            return
        _executing_tasks.add(task_id)

    logger.info(
        "[后台任务] 线程启动 | task_id=%s | thread_id=%s | thread_name=%s",
        task_id,
        thread_id,
        thread_name,
    )

    # 在新线程中创建新的事件循环
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        # 在新的事件循环中运行异步任务
        loop.run_until_complete(_execute_task_async(task_id))
    except Exception as e:
        logger.error(
            "[后台任务] 线程执行异常 | task_id=%s | error=%s",
            task_id,
            str(e),
            exc_info=True,
        )
    finally:
        # 清理执行状态
        with _executing_lock:
            _executing_tasks.discard(task_id)

        # 清理事件循环
        try:
            loop.close()
        except Exception as e:
            logger.warning(
                "[后台任务] 关闭事件循环失败 | task_id=%s | error=%s",
                task_id,
                str(e),
            )
        logger.info(
            "[后台任务] 线程结束 | task_id=%s | thread_id=%s",
            task_id,
            thread_id,
        )


async def _execute_task_async(task_id: str):
    """
    实际的异步执行函数

    使用 TaskExecutor 执行任务，TaskExecutor 内部使用 SessionManager
    管理会话，因此这里不需要处理会话。

    Args:
        task_id: 任务 ID
    """
    try:
        from src.agents.task_runner import TaskRunner

        logger.info("[后台任务] 准备执行任务 | task_id=%s", task_id)
        # TaskExecutor 内部使用 SessionManager 管理会话
        executor = TaskRunner()
        result = await executor.execute_task_with_result(task_id)
        if result.get("success"):
            logger.info("[后台任务] 执行成功 | task_id=%s", task_id)
        else:
            logger.error(
                "[后台任务] 执行失败 | task_id=%s | error=%s",
                task_id,
                result.get("error"),
            )
    except Exception as e:
        logger.error(
            "[后台任务] 执行异常 | task_id=%s | error=%s",
            task_id,
            str(e),
            exc_info=True,
        )


def task_to_dict(
    task: Task,
    metrics_details: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    将任务对象转换为字典（模块级函数，可被其他模块导入使用）

    Args:
        task: 任务对象
        metrics_details: 评估指标详情列表（由调用者提供）

    Returns:
        任务字典
    """
    priority_str = TaskPriority.to_str(task.priority)

    result = {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "agent_id": task.target_id,
        "priority": priority_str,
        "status": task.status,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
        "parent_task_id": task.parent_task_id,
        "session_id": task.session_id,
        "user_id": task.user_id,
        "goal": task.goal,
        "evaluation_metric_ids": task.evaluation_metric_ids,
        "evaluation_metrics": metrics_details or [],
        "execution_record_id": task.execution_record_id,
        "tags": task.tags or [],
        "subtasks": [],
    }

    if task.task_metadata:
        result["task_type"] = task.task_metadata.get("task_type")
        result["agent_level"] = task.task_metadata.get("agent_level")
        result["total_criteria"] = task.task_metadata.get("total_criteria", 0)
        result["passed_criteria"] = task.task_metadata.get("passed_criteria", 0)
        result["failed_criteria"] = task.task_metadata.get("failed_criteria", 0)
        result["progress_percent"] = task.task_metadata.get("progress_percent", 0.0)

    return result


class TaskService:
    """任务服务类

    提供任务的 CRUD 操作和业务逻辑，支持新的评估机制。

    会话管理策略：
    1. 初始化时接收外部会话（通常是 FastAPI 依赖注入）
    2. 常规操作使用传入的会话（共享事务）
    3. 需要独立事务的操作使用 SessionManager
    4. 后台任务使用 TaskExecutor，其内部管理会话
    """

    def __init__(self, session: AsyncSession):
        """
        初始化任务服务

        Args:
            session: 数据库会话（由调用者管理生命周期）
        """
        self.session = session
        self.task_repo = TaskRepository(session)
        self.metric_loader = get_metric_loader()
        self.execution_record_repo = ExecutionRecordRepository(session)

    async def create_task(
        self,
        task_data: dict[str, Any],
        user_id: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """
        创建任务

        独立任务会自动在后台执行。

        会话管理：
        - 使用传入的会话创建任务记录
        - 后台执行使用 TaskExecutor 的独立会话

        Args:
            task_data: 任务数据字典
            user_id: 用户 ID
            session_id: 会话 ID（可选）

        Returns:
            创建的任务信息字典

        Note:
            这是唯一的任务执行统一入口。使用 _executing_lock 和 _executing_tasks
            防止任务重复执行。如果任务已在执行中，会跳过重复提交。
        """
        try:
            # 将 priority 字符串转换为整数
            priority_int = TaskPriority.to_int(task_data.get("priority", "medium"))

            # 验证评估指标 ID
            evaluation_metric_ids = task_data.get("evaluation_metric_ids", [])
            if evaluation_metric_ids:
                metrics = await self.metric_loader.get_metrics_by_ids(
                    evaluation_metric_ids
                )
                if len(metrics) != len(evaluation_metric_ids):
                    found_ids = {m.get("id") for m in metrics}
                    missing = set(evaluation_metric_ids) - found_ids
                    logger.warning(
                        f"部分评估指标不存在 | missing={missing} | 使用可用指标"
                    )
                    evaluation_metric_ids = list(found_ids)

            # 创建执行记录（使用传入的会话）
            from src.utils.message_id_helper import generate_execution_record_id

            # 确保 session_id 不为空
            if not session_id:
                import uuid

                session_id = f"task-{uuid.uuid4().hex[:8]}"
                logger.debug(f"为任务生成 session_id: {session_id}")

            execution_record_id = await generate_execution_record_id(
                self.session, session_id
            )
            execution_record_id = (
                await self.execution_record_repo.save_execution_record(
                    session_id=session_id or "",
                    message_data={
                        "type": "task_execution",
                        "name": task_data.get("title", ""),
                        "input": task_data,
                        "status": "pending",
                    },
                    record_id=execution_record_id,
                )
            )

            # 构建任务数据
            task_create_data = {
                "title": task_data.get("title", ""),
                "target_id": task_data.get("agent_id"),
                "target_type": "agent",
                "target_name": task_data.get("title", ""),
                "priority": priority_int,
                "user_id": str(user_id),
                "session_id": session_id,
                "goal": task_data.get("goal"),
                "evaluation_metric_ids": evaluation_metric_ids,
                "execution_record_id": execution_record_id,
                "parent_task_id": task_data.get("parent_task_id"),
                "task_metadata": {
                    "total_criteria": len(evaluation_metric_ids),
                    "passed_criteria": 0,
                    "failed_criteria": 0,
                    "progress_percent": 0.0,
                    # 存储 acceptance_criteria（评估器的输入和断言）
                    "acceptance_criteria": self._build_acceptance_criteria(
                        task_data.get("acceptance_criteria"),
                        evaluation_metric_ids,
                    ),
                },
                "tags": task_data.get("tags", []),
                "status": "pending",
                "retry_count": 0,
                "max_retries": 3,
            }

            # 使用仓储创建任务（使用传入的会话）
            task = await self.task_repo.create_task(task_create_data)

            logger.info(
                f"任务创建成功 | task_id={task.id} | user_id={user_id} | "
                f"evaluation_metrics_count={len(evaluation_metric_ids)} | "
                f"execution_record_id={execution_record_id}"
            )

            # 发布任务提交事件（事件驱动改造）
            # 统一使用事件驱动机制触发任务执行
            if task.parent_task_id is None:
                await self._publish_task_submitted_event(task.id)

            # 重新获取任务状态（可能已被后台任务修改）
            await self.session.refresh(task)

            # 返回任务信息
            result = await self._build_task_response(task, task_data)
            return result

        except Exception as e:
            await self.session.rollback()
            logger.error(
                f"创建任务失败 | user_id={user_id} | task_data={task_data} | error={str(e)}",
                exc_info=True,
            )
            raise

    async def _publish_task_submitted_event(self, task_id: str) -> None:
        """
        发布任务提交事件（事件驱动改造）

        统一使用事件驱动机制触发任务执行，替代直接提交到线程池。

        Args:
            task_id: 任务 ID
        """
        from src.core.event_bus import get_event_bus
        from src.core.event_bus.types import EventType, ExecutionEvent

        event_bus = get_event_bus()
        event = ExecutionEvent(
            event_type=EventType.TASK_SUBMITTED,
            session_id=f"task_{task_id}",
            data={
                "task_id": task_id,
                "source": "TaskService",
            },
        )
        await event_bus.publish(event)
        logger.info(f"[任务服务] 任务提交事件已发布 | task_id={task_id}")

    def _build_acceptance_criteria(
        self,
        acceptance_criteria: dict[str, Any] | None,
        evaluation_metric_ids: list[str],
    ) -> list[dict[str, Any]]:
        """
        构建验收标准列表

        将 API 传入的 acceptance_criteria 字典转换为存储格式。

        Args:
            acceptance_criteria: API 传入的验收标准字典
                格式: {"metric_id": {"input_params": {...}, "pass_threshold": 85}}
            evaluation_metric_ids: 评估指标 ID 列表

        Returns:
            验收标准列表，格式:
            [{"metric_id": "...", "input_params": {...}, "status": "pending", ...}]
        """
        if not acceptance_criteria:
            # 如果没有传入 acceptance_criteria，为每个指标创建默认项
            return [
                {
                    "metric_id": metric_id,
                    "input_params": {},
                    "status": "pending",
                    "retry_count": 0,
                    "evaluated_at": None,
                    "evaluation_result": None,
                }
                for metric_id in evaluation_metric_ids
            ]

        result = []
        for metric_ref, config in acceptance_criteria.items():
            # 兼容新旧格式
            if isinstance(config, dict) and "input_params" in config:
                input_params = config.get("input_params", {})
                pass_threshold = config.get("pass_threshold")
            else:
                input_params = config if isinstance(config, dict) else {}
                pass_threshold = None

            result.append(
                {
                    "metric_id": metric_ref,
                    "input_params": input_params,
                    "pass_threshold": pass_threshold,
                    "status": "pending",
                    "retry_count": 0,
                    "evaluated_at": None,
                    "evaluation_result": None,
                }
            )

        return result

    async def _build_task_response(
        self, task: Task, original_task_data: dict[str, Any]
    ) -> dict[str, Any]:
        """
        构建任务响应数据

        Args:
            task: 任务对象
            original_task_data: 原始任务数据

        Returns:
            任务响应字典
        """
        result = {
            "id": task.id,
            "title": task.title,
            "description": task.description,
            "agent_id": task.target_id,
            "priority": original_task_data.get("priority", "medium"),
            "status": task.status,
            "goal": task.goal,
            "evaluation_metric_ids": task.evaluation_metric_ids,
            "evaluation_metrics": await self._get_metrics_details(
                task.evaluation_metric_ids
            ),
            "execution_record_id": task.execution_record_id,
            "tags": task.tags or [],
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "updated_at": task.updated_at.isoformat() if task.updated_at else None,
            "user_id": task.user_id,
        }

        # 从 task_metadata 中提取进度信息
        if task.task_metadata:
            result["total_criteria"] = task.task_metadata.get("total_criteria", 0)
            result["passed_criteria"] = task.task_metadata.get("passed_criteria", 0)
            result["failed_criteria"] = task.task_metadata.get("failed_criteria", 0)
            result["progress_percent"] = task.task_metadata.get("progress_percent", 0.0)
        else:
            result["total_criteria"] = 0
            result["passed_criteria"] = 0
            result["failed_criteria"] = 0
            result["progress_percent"] = 0.0

        return result

    async def list_tasks(
        self,
        user_id: str,
        skip: int = 0,
        limit: int = 100,
        include_subtasks: bool = True,
        root_only: bool = False,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """
        获取任务列表

        Args:
            user_id: 用户 ID
            skip: 跳过数量
            limit: 限制数量
            include_subtasks: 是否包含子任务
            root_only: 是否只返回根任务
            filters: 过滤条件

        Returns:
            任务字典列表
        """
        session_id = filters.get("session_id") if filters else None
        status = filters.get("status") if filters else None

        # 如果提供了 session_id，使用 get_root_tasks 方法
        if session_id:
            tasks = await self.task_repo.get_root_tasks(
                user_id=user_id,
                session_id=session_id,
                status=status,
                limit=limit,
            )
        else:
            tasks = await self.task_repo.get_tasks_by_user(
                user_id=user_id,
                status=status,
                limit=limit,
            )

        # 过滤根任务
        if root_only:
            tasks = [t for t in tasks if t.parent_task_id is None]

        # 构建任务字典
        task_dict = {task.id: task for task in tasks}

        # 如果需要包含子任务，递归构建树结构
        if include_subtasks:
            return [
                await self._task_to_dict_with_subtasks(task, task_dict)
                for task in tasks
            ]

        return [await self._task_to_dict(task) for task in tasks]

    async def _task_to_dict(self, task: Task) -> dict[str, Any]:
        """将任务对象转换为字典"""
        metrics_details = await self._get_metrics_details(task.evaluation_metric_ids)
        return task_to_dict(task, metrics_details)

    async def _task_to_dict_with_subtasks(
        self, task: Task, task_dict: dict[str, Task]
    ) -> dict[str, Any]:
        """将任务对象转换为字典，包含子任务"""
        result = await self._task_to_dict(task)

        # 查找子任务
        subtasks = []
        for t in task_dict.values():
            if t.parent_task_id == task.id:
                subtasks.append(await self._task_to_dict_with_subtasks(t, task_dict))

        result["subtasks"] = subtasks
        return result

    async def _get_metrics_details(
        self, metric_ids: list[str] | None
    ) -> list[dict[str, Any]]:
        """
        获取评估指标详情（从文件系统加载）

        Args:
            metric_ids: 指标 ID 列表

        Returns:
            指标详情列表
        """
        if not metric_ids:
            return []

        metrics = await self.metric_loader.get_metrics_by_ids(metric_ids)
        return [
            {
                "id": m.get("id"),
                "name": m.get("name"),
                "description": m.get("description", ""),
                "category": m.get("category", ""),
                "evaluator_type": m.get("evaluator_type", ""),
                "evaluator_id": m.get("evaluator_id", ""),
                "is_red_line": m.get("is_red_line", False),
                "default_weight": m.get("default_weight", 1.0),
            }
            for m in metrics
        ]

    def _build_acceptance_criteria(
        self,
        acceptance_criteria: dict[str, Any] | None,
        evaluation_metric_ids: list[str] | None,
    ) -> list[dict[str, Any]]:
        """
        构建验收标准列表

        将 API 传入的 acceptance_criteria 字典转换为标准列表格式。
        格式：
        {
            "file_check": {
                "input_params": {"path": "test.txt", "check": "exists"},
                "pass_threshold": 80
            }
        }

        转换为：
        [
            {
                "metric_id": "file_check",
                "input_params": {"path": "test.txt", "check": "exists"},
                "pass_threshold": 80,
                "status": "pending",
                "retry_count": 0
            }
        ]

        Args:
            acceptance_criteria: 验收标准字典
            evaluation_metric_ids: 评估指标 ID 列表

        Returns:
            验收标准列表
        """
        if not acceptance_criteria:
            # 如果没有传入 acceptance_criteria，使用 evaluation_metric_ids 初始化空的标准
            if evaluation_metric_ids:
                return [
                    {
                        "metric_id": metric_id,
                        "input_params": {},
                        "pass_threshold": None,
                        "status": "pending",
                        "retry_count": 0,
                        "evaluated_at": None,
                        "evaluation_result": None,
                    }
                    for metric_id in evaluation_metric_ids
                ]
            return []

        result = []
        for metric_ref, config in acceptance_criteria.items():
            # 兼容新旧格式
            if isinstance(config, dict):
                if "input_params" in config:
                    input_params = config.get("input_params", {})
                    pass_threshold = config.get("pass_threshold")
                else:
                    # 旧格式：直接是 input_params
                    input_params = config
                    pass_threshold = None
            else:
                input_params = {}
                pass_threshold = None

            result.append(
                {
                    "metric_id": metric_ref,
                    "input_params": input_params,
                    "pass_threshold": pass_threshold,
                    "status": "pending",
                    "retry_count": 0,
                    "evaluated_at": None,
                    "evaluation_result": None,
                }
            )

        return result

    async def get_task(
        self,
        task_id: str,
        user_id: str,
        include_metrics: bool = True,
        session_id: str | None = None,
    ) -> dict[str, Any] | None:
        """
        获取任务详情

        Args:
            task_id: 任务 ID
            user_id: 用户 ID
            include_metrics: 是否包含评估指标详情
            session_id: 会话 ID（可选）

        Returns:
            任务详情字典，不存在或无权访问返回 None
        """
        task = await self.task_repo.get(task_id)

        # 验证用户权限
        if not task or task.user_id != str(user_id):
            return None

        # 验证会话权限
        if session_id is not None and task.session_id != session_id:
            return None

        # 使用仓储方法获取任务及指标
        task_data = await self.task_repo.get_task_with_metrics(task_id)

        if not task_data:
            return None

        # 转换为返回格式
        result = task_data["task"].copy()

        # 添加评估指标详情
        if include_metrics and task_data["task"].get("evaluation_metric_ids"):
            result["evaluation_metrics"] = await self._get_metrics_details(
                task_data["task"]["evaluation_metric_ids"]
            )

        result["task_metrics"] = []

        # 获取子任务
        subtasks = await self.task_repo.get_subtasks(task_id)
        result["subtasks"] = [await self._task_to_dict(st) for st in subtasks]

        return result

    async def update_task(
        self,
        task_id: str,
        task_data: dict[str, Any],
        user_id: str,
        session_id: str | None = None,
    ) -> dict[str, Any] | None:
        """
        更新任务

        Args:
            task_id: 任务 ID
            task_data: 更新数据字典
            user_id: 用户 ID
            session_id: 会话 ID（可选）

        Returns:
            更新后的任务信息，不存在或无权访问返回 None
        """
        task = await self.task_repo.get(task_id)

        # 验证用户权限
        if not task or task.user_id != str(user_id):
            return None

        # 验证会话权限
        if session_id is not None and task.session_id != session_id:
            return None

        # 允许更新的字段
        allowed_fields = {
            "title",
            "description",
            "status",
            "goal",
            "priority",
            "task_metadata",
        }

        update_data = {k: v for k, v in task_data.items() if k in allowed_fields}

        if not update_data:
            return None

        # 处理 priority 转换
        if "priority" in update_data and isinstance(update_data["priority"], str):
            update_data["priority"] = TaskPriority.to_int(update_data["priority"])

        # 更新任务
        success = await self.task_repo.update(task_id, update_data)

        if not success:
            return None

        await self.session.commit()

        # 获取更新后的任务
        updated_task = await self.task_repo.get(task_id)
        return await self._task_to_dict(updated_task)

    async def delete_task(
        self, task_id: str, user_id: str, session_id: str | None = None
    ) -> bool:
        """
        删除任务

        Args:
            task_id: 任务 ID
            user_id: 用户 ID
            session_id: 会话 ID（可选）

        Returns:
            是否删除成功
        """
        task = await self.task_repo.get(task_id)

        # 验证用户权限
        if not task or task.user_id != str(user_id):
            return False

        # 验证会话权限
        if session_id is not None and task.session_id != session_id:
            return False

        return await self.task_repo.delete(task_id)

    async def get_evaluation_status(self, task_id: str, user_id: str) -> dict[str, Any]:
        """
        查询任务评估状态

        Args:
            task_id: 任务 ID
            user_id: 用户 ID

        Returns:
            评估状态字典
        """
        task = await self.get_task(task_id, user_id)
        if not task:
            from src.core.exceptions import NotFoundException

            raise NotFoundException(
                message=f"任务不存在: {task_id}",
                resource_type="Task",
                resource_id=task_id,
                code="TASK_001",
            )

        # 从任务的 task_metadata 中获取评估状态
        task_metadata = task.get("metadata", {}) or {}
        total = task_metadata.get("total_criteria", 0)
        passed = task_metadata.get("passed_criteria", 0)
        failed = task_metadata.get("failed_criteria", 0)
        pending = total - passed - failed
        skipped = 0
        progress = (passed + failed) / total * 100 if total > 0 else 0.0

        # 构建指标状态列表
        metrics = []
        for metric in task.get("evaluation_metrics", []):
            metrics.append(
                {
                    "metric_id": metric["id"],
                    "status": "pending",
                    "score": None,
                    "feedback": None,
                    "evaluated_at": None,
                }
            )

        return {
            "task_id": task_id,
            "total_metrics": total,
            "pending_metrics": pending,
            "passed_metrics": passed,
            "failed_metrics": failed,
            "skipped_metrics": skipped,
            "progress_percent": round(progress, 2),
            "metrics": metrics,
        }

    async def check_timeout_tasks(self) -> dict[str, Any]:
        """
        检查超时任务

        使用独立事务执行，不影响调用者的事务。

        Returns:
            处理结果字典
        """
        from datetime import timedelta

        from src.db.models import Task

        try:
            # 使用独立事务执行
            async with independent_transaction() as session:
                # 查找超时任务（超过24小时仍在进行中的任务）
                timeout_threshold = datetime.now(UTC) - timedelta(hours=24)

                stmt = select(Task).where(
                    Task.status.in_(["pending", "running"]),
                    Task.created_at < timeout_threshold,
                )
                result = await session.execute(stmt)
                timeout_tasks = result.scalars().all()

                # 更新超时任务状态
                updated_count = 0
                for task in timeout_tasks:
                    task.status = "failed"
                    task.error_message = "任务执行超时"
                    task.completed_at = datetime.now(UTC)
                    updated_count += 1

                if updated_count > 0:
                    await session.commit()

                return {
                    "status": "success",
                    "message": f"检查完成，处理了 {updated_count} 个超时任务",
                    "timeout_tasks_count": updated_count,
                    "checked_at": datetime.now(UTC).isoformat(),
                }

        except Exception as e:
            logger.error(f"检查超时任务失败: {str(e)}", exc_info=True)
            return {
                "status": "error",
                "message": f"检查超时任务失败: {str(e)}",
                "checked_at": datetime.now(UTC).isoformat(),
            }
