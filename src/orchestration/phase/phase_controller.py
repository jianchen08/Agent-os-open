"""
任务阶段控制器

提供任务三阶段模型的核心控制逻辑：
- 准备阶段 (prepare): 系统强制触发，收集上下文、生成计划
- 执行阶段 (execute): Agent 自主执行
- 评估阶段 (evaluate): 系统强制触发，验证 AC

核心原则：
- 准备和评估阶段由系统强制触发
- 执行阶段由 Agent 自主控制
- 评估失败可回到执行阶段重试

迁移说明：
- 原位置: src/tasks/phase_controller.py
- 新位置: src/orchestration/phase/phase_controller.py
- 迁移时间: 2026-02-27
- 迁移原因: PhaseController 编排跨模块阶段控制，应归属 orchestration 模块

状态: 暂时禁用
- 该控制器暂时不使用
- 任务状态由 should_continue 机制和 EvaluationService 管理
- 阶段状态仅作为记录，不参与状态转换
"""

import logging
from datetime import datetime
from enum import Enum
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import NotFoundException
from src.db.models import Task

logger = logging.getLogger(__name__)


class TaskPhase(str, Enum):
    """任务阶段"""

    PREPARE = "prepare"
    EXECUTE = "execute"
    EVALUATE = "evaluate"


class PhaseStatus(str, Enum):
    """阶段状态"""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class TaskPhaseController:
    """
    任务阶段控制器

    状态: 暂时禁用

    核心原则：
    1. 只管理阶段状态（current_phase, phase_status）
    2. 不处理任务状态（status）
    3. 任务状态由 should_continue 机制和 EvaluationService 管理
    """

    def __init__(self, session: AsyncSession):
        """
        初始化阶段控制器

        Args:
            session: 数据库会话
        """
        self.session = session

    async def get_phase_status(self, task_id: str) -> dict[str, Any]:
        """
        获取任务阶段状态

        Args:
            task_id: 任务 ID

        Returns:
            阶段状态
        """
        task = await self._get_task(task_id)
        if not task:
            return {"error": "任务不存在", "error_code": "TASK_NOT_FOUND"}

        return {
            "task_id": task_id,
            "current_phase": task.current_phase,
            "task_status": task.status,
            "phases": task.phase_status or {},
        }

    async def get_phase_output(
        self,
        task_id: str,
        phase: str,
    ) -> dict[str, Any]:
        """
        获取阶段产物

        Args:
            task_id: 任务 ID
            phase: 阶段名称

        Returns:
            阶段产物
        """
        task = await self._get_task(task_id)
        if not task:
            return {"error": "任务不存在", "error_code": "TASK_NOT_FOUND"}

        phase_status = task.phase_status or {}
        phase_data = phase_status.get(phase, {})

        if phase_data.get("status") != PhaseStatus.COMPLETED.value:
            return {
                "error": f"阶段 {phase} 尚未完成",
                "error_code": "PHASE_NOT_COMPLETED",
            }

        return {
            "task_id": task_id,
            "phase": phase,
            "status": phase_data.get("status"),
            "output": phase_data.get("output", {}),
            "start_time": phase_data.get("start_time"),
            "end_time": phase_data.get("end_time"),
        }

    async def _get_task(self, task_id: str) -> Task | None:
        """获取任务"""
        result = await self.session.execute(select(Task).where(Task.id == task_id))
        return result.scalar_one_or_none()
