"""
任务执行跟踪器

暴露接口：
- is_executing(task_id: str) -> bool：is_executing功能
- mark_executing(task_id: str) -> bool：mark_executing功能
- unmark_executing(task_id: str) -> None：unmark_executing功能
- get_executor() -> concurrent.futures.ThreadPoolExecutor：get_executor功能
- get_executing_tasks() -> set[str]：get_executing_tasks功能
"""

import concurrent.futures
import threading

from config.settings import get_settings

_settings = get_settings()
_background_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=_settings.task_max_workers, thread_name_prefix="task_bg_"
)

_executing_tasks: set[str] = set()
_executing_lock = threading.Lock()


def is_executing(task_id: str) -> bool:
    """检查任务是否正在执行中"""
    with _executing_lock:
        return task_id in _executing_tasks


def mark_executing(task_id: str) -> bool:
    """标记任务为执行中"""
    with _executing_lock:
        if task_id in _executing_tasks:
            return False
        _executing_tasks.add(task_id)
        return True


def unmark_executing(task_id: str) -> None:
    """取消任务的执行中标记"""
    with _executing_lock:
        _executing_tasks.discard(task_id)


def get_executor() -> concurrent.futures.ThreadPoolExecutor:
    """获取全局线程池执行器"""
    return _background_executor


def get_executing_tasks() -> set[str]:
    """获取当前正在执行的任务 ID 集合（副本）"""
    with _executing_lock:
        return _executing_tasks.copy()
