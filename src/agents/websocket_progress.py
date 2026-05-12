"""
WebSocket 任务进度推送系统

实现任务执行进度的实时推送，提供更好的用户体验
"""

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class ProgressEventType(Enum):
    """进度事件类型"""

    TASK_STARTED = "task_started"
    TASK_PROGRESS = "task_progress"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    STEP_STARTED = "step_started"
    STEP_COMPLETED = "step_completed"
    TOOL_CALLED = "tool_called"
    TOOL_RESULT = "tool_result"


@dataclass
class ProgressEvent:
    """进度事件"""

    event_type: ProgressEventType
    task_id: str
    timestamp: datetime
    data: dict[str, Any]
    step_index: int | None = None
    total_steps: int | None = None
    progress_percentage: float | None = None
    message: str | None = None


class TaskProgressTracker:
    """任务进度追踪器"""

    def __init__(self, task_id: str, websocket_manager: "WebSocketProgressManager"):
        self.task_id = task_id
        self.websocket_manager = websocket_manager
        self.start_time = datetime.now()
        self.current_step = 0
        self.total_steps = 0
        self.is_completed = False

    async def start_task(self, total_steps: int = 0, message: str = "任务开始执行"):
        """开始任务"""
        self.total_steps = total_steps
        event = ProgressEvent(
            event_type=ProgressEventType.TASK_STARTED,
            task_id=self.task_id,
            timestamp=datetime.now(),
            data={"total_steps": total_steps},
            message=message,
        )
        await self.websocket_manager.broadcast_progress(event)

    async def update_progress(
        self, step_index: int, message: str, data: dict[str, Any] = None
    ):
        """更新进度"""
        self.current_step = step_index
        progress = (step_index / self.total_steps * 100) if self.total_steps > 0 else 0

        event = ProgressEvent(
            event_type=ProgressEventType.TASK_PROGRESS,
            task_id=self.task_id,
            timestamp=datetime.now(),
            data=data or {},
            step_index=step_index,
            total_steps=self.total_steps,
            progress_percentage=progress,
            message=message,
        )
        await self.websocket_manager.broadcast_progress(event)

    async def step_started(self, step_name: str, step_index: int):
        """步骤开始"""
        event = ProgressEvent(
            event_type=ProgressEventType.STEP_STARTED,
            task_id=self.task_id,
            timestamp=datetime.now(),
            data={"step_name": step_name},
            step_index=step_index,
            message=f"开始执行步骤: {step_name}",
        )
        await self.websocket_manager.broadcast_progress(event)

    async def step_completed(self, step_name: str, step_index: int, result: Any = None):
        """步骤完成"""
        event = ProgressEvent(
            event_type=ProgressEventType.STEP_COMPLETED,
            task_id=self.task_id,
            timestamp=datetime.now(),
            data={"step_name": step_name, "result": result},
            step_index=step_index,
            message=f"完成步骤: {step_name}",
        )
        await self.websocket_manager.broadcast_progress(event)

    async def tool_called(self, tool_name: str, tool_args: dict[str, Any]):
        """工具调用"""
        event = ProgressEvent(
            event_type=ProgressEventType.TOOL_CALLED,
            task_id=self.task_id,
            timestamp=datetime.now(),
            data={"tool_name": tool_name, "args": tool_args},
            message=f"调用工具: {tool_name}",
        )
        await self.websocket_manager.broadcast_progress(event)

    async def tool_result(
        self, tool_name: str, success: bool, result: Any = None, error: str = None
    ):
        """工具结果"""
        event = ProgressEvent(
            event_type=ProgressEventType.TOOL_RESULT,
            task_id=self.task_id,
            timestamp=datetime.now(),
            data={
                "tool_name": tool_name,
                "success": success,
                "result": result,
                "error": error,
            },
            message=f"工具 {tool_name} {'成功' if success else '失败'}",
        )
        await self.websocket_manager.broadcast_progress(event)

    async def complete_task(self, success: bool, result: Any = None, error: str = None):
        """完成任务"""
        self.is_completed = True
        duration = (datetime.now() - self.start_time).total_seconds()

        event = ProgressEvent(
            event_type=(
                ProgressEventType.TASK_COMPLETED
                if success
                else ProgressEventType.TASK_FAILED
            ),
            task_id=self.task_id,
            timestamp=datetime.now(),
            data={
                "success": success,
                "result": result,
                "error": error,
                "duration_seconds": duration,
            },
            progress_percentage=100.0 if success else None,
            message=f"任务{'成功完成' if success else '执行失败'}",
        )
        await self.websocket_manager.broadcast_progress(event)


class WebSocketProgressManager:
    """WebSocket 进度管理器"""

    def __init__(self):
        # 存储活跃的 WebSocket 连接
        self.active_connections: dict[
            str, list
        ] = {}  # user_id -> [websocket_connections]
        self.task_trackers: dict[str, TaskProgressTracker] = {}  # task_id -> tracker

    async def connect_user(self, user_id: str, websocket):
        """用户连接 WebSocket"""
        if user_id not in self.active_connections:
            self.active_connections[user_id] = []
        self.active_connections[user_id].append(websocket)
        logger.info(
            f"[WebSocket] 用户 {user_id} 连接，当前连接数: {len(self.active_connections[user_id])}"
        )

    async def disconnect_user(self, user_id: str, websocket):
        """用户断开 WebSocket"""
        if user_id in self.active_connections:
            try:
                self.active_connections[user_id].remove(websocket)
                if not self.active_connections[user_id]:
                    del self.active_connections[user_id]
                logger.info(f"[WebSocket] 用户 {user_id} 断开连接")
            except ValueError:
                pass

    def create_tracker(self, task_id: str) -> TaskProgressTracker:
        """创建任务追踪器"""
        tracker = TaskProgressTracker(task_id, self)
        self.task_trackers[task_id] = tracker
        return tracker

    def get_tracker(self, task_id: str) -> TaskProgressTracker | None:
        """获取任务追踪器"""
        return self.task_trackers.get(task_id)

    async def broadcast_progress(self, event: ProgressEvent):
        """广播进度事件"""
        message = {
            "type": "task_progress",
            "event": asdict(event),
            "timestamp": event.timestamp.isoformat(),
        }

        # 发送给所有连接的用户（可以根据需要过滤特定用户）
        disconnected_users = []
        for user_id, connections in self.active_connections.items():
            disconnected_connections = []
            for websocket in connections:
                try:
                    await websocket.send_text(json.dumps(message, ensure_ascii=False))
                except Exception as e:
                    logger.warning(
                        f"[WebSocket] 发送消息失败 | user={user_id} | error={e}"
                    )
                    disconnected_connections.append(websocket)

            # 清理断开的连接
            for ws in disconnected_connections:
                try:
                    connections.remove(ws)
                except ValueError:
                    pass

            if not connections:
                disconnected_users.append(user_id)

        # 清理断开的用户
        for user_id in disconnected_users:
            del self.active_connections[user_id]

    async def send_to_user(self, user_id: str, event: ProgressEvent):
        """发送进度事件给特定用户"""
        if user_id not in self.active_connections:
            return

        message = {
            "type": "task_progress",
            "event": asdict(event),
            "timestamp": event.timestamp.isoformat(),
        }

        connections = self.active_connections[user_id]
        disconnected_connections = []

        for websocket in connections:
            try:
                await websocket.send_text(json.dumps(message, ensure_ascii=False))
            except Exception as e:
                logger.warning(f"[WebSocket] 发送消息失败 | user={user_id} | error={e}")
                disconnected_connections.append(websocket)

        # 清理断开的连接
        for ws in disconnected_connections:
            try:
                connections.remove(ws)
            except ValueError:
                pass


# 全局 WebSocket 进度管理器
websocket_progress_manager = WebSocketProgressManager()


# 装饰器：自动创建进度追踪
def track_progress(task_id_key: str = "task_id"):
    """
    装饰器：为函数自动创建进度追踪

    Args:
        task_id_key: 从函数参数中获取 task_id 的键名
    """

    def decorator(func):
        async def wrapper(*args, **kwargs):
            # 获取 task_id
            task_id = kwargs.get(task_id_key)
            if not task_id and args:
                # 尝试从位置参数获取
                task_id = getattr(args[0], task_id_key, None)

            if task_id:
                tracker = websocket_progress_manager.create_tracker(task_id)
                kwargs["progress_tracker"] = tracker

                try:
                    await tracker.start_task(message=f"开始执行函数: {func.__name__}")
                    result = await func(*args, **kwargs)
                    await tracker.complete_task(True, result)
                    return result
                except Exception as e:
                    await tracker.complete_task(False, error=str(e))
                    raise
            else:
                return await func(*args, **kwargs)

        return wrapper

    return decorator
