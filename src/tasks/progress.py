"""
任务进度追踪模块

暴露接口：
- percent(self) -> float：percent功能
- update(self, value: int) -> None：update功能
- to_display_string(self, width: int) -> str：to_display_string功能
- start(self) -> None：start功能
- complete(self) -> None：complete功能
- fail(self, error: str) -> None：fail功能
- update_metric(self, metric_name: str, value: Any) -> None：update_metric功能
- calculate_metrics_progress(self) -> float：calculate_metrics_progress功能
- get_status_icon(self) -> str：get_status_icon功能
- is_completed(self) -> bool：is_completed功能
- completed_count(self) -> int：completed_count功能
- failed_count(self) -> int：failed_count功能
- start_subtask(self, task_id: str) -> None：start_subtask功能
- complete_subtask(self, task_id: str) -> None：complete_subtask功能
- fail_subtask(self, task_id: str, error: str) -> None：fail_subtask功能
- update_subtask_progress(self, task_id: str, progress: int) -> None：update_subtask_progress功能
- get_snapshot(self) -> dict[str, Any]：get_snapshot功能
- to_display_string(self) -> str：to_display_string功能
- start(self) -> None：start功能
- update_progress(self, progress: int, message: str | None) -> None：update_progress功能
- complete(self, result: dict[str, Any] | None) -> None：complete功能
- fail(self, error: str) -> None：fail功能
- to_dict(self) -> dict[str, Any]：to_dict功能
- subtask_count(self) -> int：subtask_count功能
- completed_count(self) -> int：completed_count功能
- failed_count(self) -> int：failed_count功能
- get_subtask(self, subtask_id: str) -> L3Subtask | None：get_subtask功能
- get_all_subtasks(self) -> list[L3Subtask]：get_all_subtasks功能
- get_evidence(self) -> dict[str, Any]：get_evidence功能
- get_snapshot(self) -> dict[str, Any]：get_snapshot功能
- ProgressBar：ProgressBar类
- SubTaskProgress：SubTaskProgress类
- TaskProgressTracker：TaskProgressTracker类
- L3SubtaskType：L3SubtaskType类
- L3Subtask：L3Subtask类
- L3ProgressManager：L3ProgressManager类
"""

from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from core.states import ExecutionStatus

if TYPE_CHECKING:
    from tasks.scheduler import TaskDefinition


class ProgressBar(BaseModel):
    """
    进度条

    支持百分比显示和可视化输出
    """

    total: int = Field(100, description="总量")
    current: int = Field(0, description="当前值")
    status: ExecutionStatus = Field(ExecutionStatus.PENDING, description="状态")

    @property
    def percent(self) -> float:
        """计算百分比"""
        if self.total == 0:
            return 0.0
        return (self.current / self.total) * 100

    def update(self, value: int) -> None:
        """更新进度值"""
        self.current = min(value, self.total)
        if self.current >= self.total:
            self.status = ExecutionStatus.COMPLETED

    def to_display_string(self, width: int = 20) -> str:
        """生成可视化进度条字符串"""
        filled = int(width * self.percent / 100)
        empty = width - filled
        bar = "█" * filled + "░" * empty
        return f"[{bar}] {self.percent:.0f}%"


class SubTaskProgress(BaseModel):
    """
    子任务进度

    跟踪单个子任务的执行状态和进度
    """

    task_id: str = Field(..., description="任务 ID")
    title: str = Field(..., description="任务标题")
    status: ExecutionStatus = Field(ExecutionStatus.PENDING, description="状态")
    progress_bar: ProgressBar = Field(default_factory=ProgressBar)

    # 时间信息
    start_time: datetime | None = Field(None, description="开始时间")
    end_time: datetime | None = Field(None, description="结束时间")

    # 评估指标（可选）
    evaluation_metrics: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="评估指标: {指标名: {target: 目标值, current: 当前值}}",
    )

    # 错误信息
    error_message: str | None = Field(None, description="错误信息")
    retry_count: int = Field(0, description="重试次数")

    def start(self) -> None:
        """开始执行"""
        self.status = ExecutionStatus.RUNNING
        self.start_time = datetime.now(UTC)
        self.progress_bar.status = ExecutionStatus.RUNNING

    def complete(self) -> None:
        """完成任务"""
        self.status = ExecutionStatus.COMPLETED
        self.end_time = datetime.now(UTC)
        self.progress_bar.update(self.progress_bar.total)
        self.progress_bar.status = ExecutionStatus.COMPLETED

    def fail(self, error: str) -> None:
        """标记失败"""
        self.status = ExecutionStatus.FAILED
        self.end_time = datetime.now(UTC)
        self.error_message = error
        self.progress_bar.status = ExecutionStatus.FAILED

    def update_metric(self, metric_name: str, value: Any) -> None:
        """更新评估指标"""
        if metric_name in self.evaluation_metrics:
            self.evaluation_metrics[metric_name]["current"] = value

    def calculate_metrics_progress(self) -> float:
        """根据评估指标计算综合进度"""
        if not self.evaluation_metrics:
            return self.progress_bar.percent

        total_progress = 0.0
        for metric in self.evaluation_metrics.values():
            target = metric.get("target", 1)
            current = metric.get("current", 0)
            if target > 0:
                progress = min(current / target, 1.0) * 100
                total_progress += progress

        return total_progress / len(self.evaluation_metrics)

    def get_status_icon(self) -> str:
        """获取状态图标"""
        icons = {
            ExecutionStatus.PENDING: "[等待]",
            ExecutionStatus.RUNNING: "[进行中]",
            ExecutionStatus.COMPLETED: "[完成]",
            ExecutionStatus.FAILED: "[失败]",
            ExecutionStatus.CANCELLED: "[取消]",
            ExecutionStatus.SUSPENDED: "[暂停]",
        }
        return icons.get(self.status, "[未知]")


class TaskProgressTracker:
    """
    任务进度追踪器

    管理主任务和所有子任务的进度，提供 UI 展示数据

    两层进度结构：
    - 主任务进度：根据子任务完成数量计算
    - 子任务进度：每个子任务独立的进度条
    """

    def __init__(
        self,
        main_task_title: str,
        subtasks: list["TaskDefinition"],
        main_task_id: str | None = None,
    ):
        """初始化进度追踪器"""
        self.main_task_id = main_task_id or f"main_{id(self)}"
        self.main_task_title = main_task_title

        # 主任务进度条
        self.main_progress = ProgressBar(
            total=len(subtasks),
            current=0,
            status=ExecutionStatus.PENDING,
        )

        # 子任务进度映射
        self.subtask_progress: dict[str, SubTaskProgress] = {}
        for task in subtasks:
            self.subtask_progress[task.id] = SubTaskProgress(
                task_id=task.id,
                title=task.title,
                status=ExecutionStatus.PENDING,
                start_time=None,
                end_time=None,
                error_message=None,
                retry_count=0,
            )

        # 创建时间
        self.created_at = datetime.now(UTC)
        self.updated_at = datetime.now(UTC)

    @property
    def is_completed(self) -> bool:
        """是否全部完成"""
        return self.main_progress.percent >= 100.0

    @property
    def completed_count(self) -> int:
        """已完成子任务数量"""
        return sum(
            1
            for p in self.subtask_progress.values()
            if p.status == ExecutionStatus.COMPLETED
        )

    @property
    def failed_count(self) -> int:
        """失败子任务数量"""
        return sum(
            1
            for p in self.subtask_progress.values()
            if p.status == ExecutionStatus.FAILED
        )

    def start_subtask(self, task_id: str) -> None:
        """开始子任务"""
        if task_id in self.subtask_progress:
            self.subtask_progress[task_id].start()
            self._update_main_progress()

    def complete_subtask(self, task_id: str) -> None:
        """完成子任务"""
        if task_id in self.subtask_progress:
            self.subtask_progress[task_id].complete()
            self._update_main_progress()

    def fail_subtask(self, task_id: str, error: str) -> None:
        """标记子任务失败"""
        if task_id in self.subtask_progress:
            self.subtask_progress[task_id].fail(error)
            self._update_main_progress()

    def update_subtask_progress(
        self,
        task_id: str,
        progress: int,
    ) -> None:
        """更新子任务进度值"""
        if task_id in self.subtask_progress:
            self.subtask_progress[task_id].progress_bar.update(progress)
            self._update_main_progress()

    def _update_main_progress(self) -> None:
        """更新主任务进度"""
        self.main_progress.current = self.completed_count
        self.updated_at = datetime.now(UTC)

        # 更新主任务状态
        if self.is_completed:
            self.main_progress.status = ExecutionStatus.COMPLETED
        elif any(
            p.status == ExecutionStatus.RUNNING
            for p in self.subtask_progress.values()
        ):
            self.main_progress.status = ExecutionStatus.RUNNING

    def get_snapshot(self) -> dict[str, Any]:
        """获取进度快照（用于 UI 展示）"""
        return {
            "main_task": {
                "id": self.main_task_id,
                "title": self.main_task_title,
                "percent": self.main_progress.percent,
                "status": self.main_progress.status.value,
                "completed": self.completed_count,
                "total": len(self.subtask_progress),
                "failed": self.failed_count,
            },
            "subtasks": {
                task_id: {
                    "title": progress.title,
                    "status": progress.status.value,
                    "percent": progress.progress_bar.percent,
                    "error": progress.error_message,
                    "retry_count": progress.retry_count,
                }
                for task_id, progress in self.subtask_progress.items()
            },
            "updated_at": self.updated_at.isoformat(),
        }

    def to_display_string(self) -> str:
        """生成可视化显示字符串"""
        lines = []

        # 主任务进度
        main_bar = self.main_progress.to_display_string()
        lines.append(f"[任务] {self.main_task_title} {main_bar}")
        lines.append(
            f"   ({self.completed_count}/{len(self.subtask_progress)} 任务完成)"
        )
        lines.append("")

        # 子任务列表
        for _task_id, progress in self.subtask_progress.items():
            icon = progress.get_status_icon()
            percent = f"({progress.progress_bar.percent:.0f}%)"
            lines.append(f"   {icon} {progress.title} {percent}")

        return "\n".join(lines)


# ============================================================================
# L3 子任务追踪（内存模式）
# ============================================================================


class L3SubtaskType(str, Enum):
    """L3 子任务类型"""

    TOOL = "tool"  # 工具调用
    WORKFLOW = "workflow"  # 工作流执行
    AGENT = "agent"  # Agent 调用


class L3Subtask(BaseModel):
    """
    L3 子任务

    跟踪单个 L3 原子任务的执行状态
    """

    subtask_id: str = Field(..., description="子任务 ID")
    subtask_type: L3SubtaskType = Field(..., description="子任务类型")
    name: str = Field(..., description="子任务名称")
    description: str | None = Field(None, description="子任务描述")
    status: ExecutionStatus = Field(ExecutionStatus.PENDING, description="状态")
    progress: int = Field(0, description="进度百分比 0-100")

    # 时间信息
    start_time: datetime | None = Field(None, description="开始时间")
    end_time: datetime | None = Field(None, description="结束时间")

    # 结果信息
    result: dict[str, Any] | None = Field(None, description="执行结果")
    error: str | None = Field(None, description="错误信息")

    def start(self) -> None:
        """开始执行"""
        self.status = ExecutionStatus.RUNNING
        self.start_time = datetime.now(UTC)

    def update_progress(self, progress: int, message: str | None = None) -> None:
        """更新进度"""
        self.progress = min(max(progress, 0), 100)
        if message:
            self.description = message

    def complete(self, result: dict[str, Any] | None = None) -> None:
        """完成任务"""
        self.status = ExecutionStatus.COMPLETED
        self.end_time = datetime.now(UTC)
        self.progress = 100
        self.result = result

    def fail(self, error: str) -> None:
        """标记失败"""
        self.status = ExecutionStatus.FAILED
        self.end_time = datetime.now(UTC)
        self.error = error

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "subtask_id": self.subtask_id,
            "subtask_type": self.subtask_type.value,
            "name": self.name,
            "description": self.description,
            "status": self.status.value,
            "progress": self.progress,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "result": self.result,
            "error": self.error,
        }


class L3ProgressManager:
    """
    L3 子任务进度管理器

    管理 L2 任务执行过程中的 L3 原子任务追踪。
    支持两种模式：
    - memory: 纯内存模式，不持久化（默认）
    - db: 数据库持久化模式

    核心功能：
    1. 创建/更新/完成 L3 子任务
    2. 通过 WebSocket 实时推送状态
    3. 收集 L3 结果作为 AC 证据
    """

    def __init__(
        self,
        parent_task_id: str,
        user_id: str,
        mode: str = "memory",
    ):
        """初始化 L3 进度管理器"""
        self.parent_task_id = parent_task_id
        self.user_id = user_id
        self.mode = mode

        # L3 子任务映射
        self._subtasks: dict[str, L3Subtask] = {}

        # 创建时间
        self.created_at = datetime.now(UTC)

    @property
    def subtask_count(self) -> int:
        """子任务数量"""
        return len(self._subtasks)

    @property
    def completed_count(self) -> int:
        """已完成子任务数量"""
        return sum(
            1
            for s in self._subtasks.values()
            if s.status == ExecutionStatus.COMPLETED
        )

    @property
    def failed_count(self) -> int:
        """失败子任务数量"""
        return sum(
            1 for s in self._subtasks.values() if s.status == ExecutionStatus.FAILED
        )

    async def create_subtask(
        self,
        subtask_id: str,
        subtask_type: str,
        name: str,
        description: str | None = None,
    ) -> L3Subtask:
        """创建 L3 子任务"""
        import logging

        logger = logging.getLogger(__name__)

        # 转换类型
        try:
            st_type = L3SubtaskType(subtask_type)
        except ValueError:
            st_type = L3SubtaskType.TOOL

        subtask = L3Subtask(
            subtask_id=subtask_id,
            subtask_type=st_type,
            name=name,
            description=description,
        )
        subtask.start()

        self._subtasks[subtask_id] = subtask

        # 推送 WebSocket 事件
        await self._send_started_event(subtask)

        logger.info(
            f"L3 子任务已创建 | parent={self.parent_task_id} | subtask={subtask_id} | type={subtask_type}"
        )

        return subtask

    async def update_subtask(
        self,
        subtask_id: str,
        progress: int,
        message: str | None = None,
    ) -> L3Subtask | None:
        """更新 L3 子任务进度"""
        subtask = self._subtasks.get(subtask_id)
        if not subtask:
            return None

        subtask.update_progress(progress, message)

        # 推送 WebSocket 事件
        await self._send_progress_event(subtask)

        return subtask

    async def complete_subtask(
        self,
        subtask_id: str,
        result: dict[str, Any] | None = None,
    ) -> L3Subtask | None:
        """完成 L3 子任务"""
        import logging

        logger = logging.getLogger(__name__)

        subtask = self._subtasks.get(subtask_id)
        if not subtask:
            return None

        subtask.complete(result)

        # 推送 WebSocket 事件
        await self._send_completed_event(subtask, success=True)

        logger.info(
            f"L3 子任务已完成 | parent={self.parent_task_id} | subtask={subtask_id}"
        )

        return subtask

    async def fail_subtask(
        self,
        subtask_id: str,
        error: str,
    ) -> L3Subtask | None:
        """标记 L3 子任务失败"""
        import logging

        logger = logging.getLogger(__name__)

        subtask = self._subtasks.get(subtask_id)
        if not subtask:
            return None

        subtask.fail(error)

        # 推送 WebSocket 事件
        await self._send_completed_event(subtask, success=False, error=error)

        logger.warning(
            f"L3 子任务失败 | parent={self.parent_task_id} | subtask={subtask_id} | error={error}"
        )

        return subtask

    def get_subtask(self, subtask_id: str) -> L3Subtask | None:
        """获取子任务"""
        return self._subtasks.get(subtask_id)

    def get_all_subtasks(self) -> list[L3Subtask]:
        """获取所有子任务"""
        return list(self._subtasks.values())

    def get_evidence(self) -> dict[str, Any]:
        """收集所有 L3 子任务结果作为 AC 证据"""
        return {
            "parent_task_id": self.parent_task_id,
            "total_subtasks": self.subtask_count,
            "completed_subtasks": self.completed_count,
            "failed_subtasks": self.failed_count,
            "subtasks": [s.to_dict() for s in self._subtasks.values()],
            "collected_at": datetime.now(UTC).isoformat(),
        }

    def get_snapshot(self) -> dict[str, Any]:
        """获取进度快照（用于 UI 展示）"""
        return {
            "parent_task_id": self.parent_task_id,
            "mode": self.mode,
            "total": self.subtask_count,
            "completed": self.completed_count,
            "failed": self.failed_count,
            "subtasks": {
                s.subtask_id: {
                    "type": s.subtask_type.value,
                    "name": s.name,
                    "status": s.status.value,
                    "progress": s.progress,
                    "error": s.error,
                }
                for s in self._subtasks.values()
            },
            "created_at": self.created_at.isoformat(),
        }

    # ========================================================================
    # WebSocket 事件推送
    # ========================================================================

    async def _send_started_event(self, subtask: L3Subtask) -> None:
        """推送子任务开始事件"""
        event_service = self._get_event_service()
        if event_service:
            await event_service.send_l3_subtask_started(
                user_id=self.user_id,
                taskId=self.parent_task_id,
                subtaskId=subtask.subtask_id,
                subtaskType=subtask.subtask_type.value,
                name=subtask.name,
                description=subtask.description,
            )

    async def _send_progress_event(self, subtask: L3Subtask) -> None:
        """推送子任务进度事件"""
        event_service = self._get_event_service()
        if event_service:
            await event_service.send_l3_subtask_progress(
                user_id=self.user_id,
                taskId=self.parent_task_id,
                subtaskId=subtask.subtask_id,
                progress=subtask.progress,
                message=subtask.description,
            )

    async def _send_completed_event(
        self,
        subtask: L3Subtask,
        success: bool,
        error: str | None = None,
    ) -> None:
        """推送子任务完成事件"""
        event_service = self._get_event_service()
        if event_service:
            await event_service.send_l3_subtask_completed(
                user_id=self.user_id,
                taskId=self.parent_task_id,
                subtaskId=subtask.subtask_id,
                success=success,
                result=subtask.result,
                error=error or subtask.error,
            )

    def _get_event_service(self):
        """获取事件推送服务"""
        try:
            from api.websocket.service import get_event_service

            return get_event_service()
        except Exception:
            return None

