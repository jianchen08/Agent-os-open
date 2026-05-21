"""任务启动器。

提供统一任务启动入口，验证任务状态后通过 EventBus 发射 task.submitted 事件，
由 TaskWorker 消费并创建后台协程执行 PipelineEngine。

原 orchestration/scheduler.py 中 schedule() 的轻量替代，
移除了未使用的调度队列、资源分配、公平性策略等逻辑。
"""

import logging
from typing import Any

from src.core.event_bus import get_event_bus
from src.core.states import ExecutionStatus
from src.db.session_manager import managed_session

logger = logging.getLogger(__name__)

_processing_task_ids: set[str] = set()


async def launch_task(task_id: str) -> dict[str, Any]:
    """统一任务启动入口。

    验证任务状态后通过 EventBus 发射 task.submitted 事件。
    TaskWorker 订阅该事件后会创建后台协程执行 PipelineEngine。

    Args:
        task_id: 任务 ID

    Returns:
        执行结果字典
    """
    if task_id in _processing_task_ids:
        logger.warning("任务已在执行中，跳过重复提交 | task_id=%s", task_id)
        return {"success": False, "error": "任务已在执行中", "task_id": task_id}

    _processing_task_ids.add(task_id)

    try:
        async with managed_session() as session:
            from sqlalchemy import select

            from src.db.models import Task

            result = await session.execute(select(Task).where(Task.id == task_id))
            task = result.scalar_one_or_none()

            if not task:
                logger.error("任务不存在 | task_id=%s", task_id)
                return {"success": False, "error": "任务不存在", "task_id": task_id}

            if task.status not in (
                ExecutionStatus.PENDING.value,
                ExecutionStatus.RUNNING.value,
            ):
                logger.warning(
                    "任务状态不可执行 | task_id=%s | status=%s",
                    task_id,
                    task.status,
                )
                return {
                    "success": False,
                    "error": f"任务状态不可执行: {task.status}",
                    "task_id": task_id,
                }

        event_bus = get_event_bus()
        await event_bus.emit(
            "task.submitted",
            {
                "task_id": task_id,
                "target_id": task.target_id or "",
                "title": task.title or "",
                "description": task.description or "",
                "goal": task.goal,
                "source": "launcher",
            },
        )

        logger.info("已发射 task.submitted 事件 | task_id=%s", task_id)
        return {"success": True, "task_id": task_id, "status": "submitted"}

    except Exception as e:
        logger.exception("任务启动异常 | task_id=%s | error=%s", task_id, e)
        return {"success": False, "error": str(e), "task_id": task_id}
    finally:
        _processing_task_ids.discard(task_id)
