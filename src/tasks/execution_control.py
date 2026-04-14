"""
任务执行控制模块

暴露接口：
- is_task_execution_enabled() -> bool：is_task_execution_enabled功能
- get_execution_status() -> ExecutionControlStatus：get_execution_status功能
- set_task_execution_enabled(enabled: bool) -> bool：set_task_execution_enabled功能
- toggle_task_execution() -> bool：toggle_task_execution功能
- register_state_change_callback(callback: Callable[[bool], None]) -> None：register_state_change_callback功能
- unregister_state_change_callback(callback: Callable[[bool], None]) -> None：unregister_state_change_callback功能
- check_task_execution_allowed() -> None：check_task_execution_allowed功能
- record_paused_task(task_id: str) -> None：record_paused_task功能
- get_paused_tasks() -> set[str]：get_paused_tasks功能
- clear_paused_tasks() -> None：clear_paused_tasks功能
- ExecutionControlStatus：ExecutionControlStatus类
- TaskExecutionDisabledError：TaskExecutionDisabledError类
"""

import logging
from collections.abc import Callable
from enum import Enum, auto

logger = logging.getLogger(__name__)


class ExecutionControlStatus(Enum):
    """任务执行控制状态"""
    ENABLED = auto()    # 允许执行
    DISABLED = auto()   # 禁止执行


# 全局任务执行开关（默认开启）
_execution_status: ExecutionControlStatus = ExecutionControlStatus.ENABLED

# 状态变更回调列表
_state_change_callbacks: list[Callable[[bool], None]] = []

# 被暂停的任务 ID 集合（记录哪些任务因开关关闭而被暂停）
_paused_task_ids: set[str] = set()


def is_task_execution_enabled() -> bool:
    """检查任务执行是否启用"""
    return _execution_status == ExecutionControlStatus.ENABLED


def get_execution_status() -> ExecutionControlStatus:
    """获取任务执行控制状态"""
    return _execution_status


def set_task_execution_enabled(enabled: bool) -> bool:
    """设置任务执行开关状态"""
    global _execution_status

    old_status = _execution_status
    new_status = ExecutionControlStatus.ENABLED if enabled else ExecutionControlStatus.DISABLED

    if old_status != new_status:
        _execution_status = new_status

        if enabled:
            logger.info(
                "[任务执行控制] 任务执行已启用 | "
                "新任务可以启动，运行中的任务继续执行"
            )
            # 清空暂停记录（任务会自动继续，不需要手动恢复）
            _paused_task_ids.clear()
        else:
            logger.info(
                "[任务执行控制] 任务执行已禁用 | "
                "新任务无法启动，运行中的任务将在下一个检查点暂停"
            )

        # 触发状态变更回调
        for callback in _state_change_callbacks:
            try:
                callback(enabled)
            except Exception as e:
                logger.error("[任务执行控制] 状态变更回调执行失败 | error=%s", e)

    return is_task_execution_enabled()


def toggle_task_execution() -> bool:
    """切换任务执行开关状态"""
    return set_task_execution_enabled(not is_task_execution_enabled())


def register_state_change_callback(callback: Callable[[bool], None]) -> None:
    """注册状态变更回调函数"""
    if callback not in _state_change_callbacks:
        _state_change_callbacks.append(callback)


def unregister_state_change_callback(callback: Callable[[bool], None]) -> None:
    """注销状态变更回调函数"""
    if callback in _state_change_callbacks:
        _state_change_callbacks.remove(callback)


def check_task_execution_allowed() -> None:
    """检查是否允许执行任务"""
    if not is_task_execution_enabled():
        raise TaskExecutionDisabledError("任务执行已禁用，请联系管理员开启")


def record_paused_task(task_id: str) -> None:
    """记录因开关关闭而被暂停的任务"""
    _paused_task_ids.add(task_id)
    logger.info("[任务执行控制] 记录暂停任务 | task_id=%s", task_id)


def get_paused_tasks() -> set[str]:
    """获取所有被暂停的任务 ID"""
    return _paused_task_ids.copy()


def clear_paused_tasks() -> None:
    """清空暂停任务记录"""
    _paused_task_ids.clear()


class TaskExecutionDisabledError(Exception):
    """任务执行已禁用异常"""
    pass
