"""
任务执行跟踪器

暴露接口：
- is_executing(task_id: str) -> bool：is_executing功能
- mark_executing(task_id: str) -> bool：mark_executing功能
- unmark_executing(task_id: str) -> None：unmark_executing功能
- get_executor() -> concurrent.futures.ThreadPoolExecutor：get_executor功能
- get_executing_tasks() -> set[str]：get_executing_tasks功能

优化说明：
- 去掉 threading.Lock — CPython set 的 add/discard/in 操作是原子的
- mark_executing 使用 set.add 的原子性保证幂等（不检查再添加）
"""

import concurrent.futures
import os

# BUG-FIX: config.settings 模块不存在，改为从环境变量读取线程池大小
_TASK_MAX_WORKERS = int(os.environ.get("TASK_MAX_WORKERS", "4"))
_background_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=_TASK_MAX_WORKERS, thread_name_prefix="task_bg_"
)

_executing_tasks: set[str] = set()


def is_executing(task_id: str) -> bool:
    """检查任务是否正在执行中（无锁 — set.__contains__ 是原子操作）"""
    return task_id in _executing_tasks


def mark_executing(task_id: str) -> bool:
    """标记任务为执行中（无锁 — 利用 set 返回值判断是否为新添加）"""
    prev_len = len(_executing_tasks)
    _executing_tasks.add(task_id)
    return len(_executing_tasks) > prev_len


def unmark_executing(task_id: str) -> None:
    """取消任务的执行中标记（无锁 — set.discard 是原子操作）"""
    _executing_tasks.discard(task_id)


def get_executor() -> concurrent.futures.ThreadPoolExecutor:
    """获取全局线程池执行器"""
    return _background_executor


def get_executing_tasks() -> set[str]:
    """获取当前正在执行的任务 ID 集合（副本）"""
    return _executing_tasks.copy()
