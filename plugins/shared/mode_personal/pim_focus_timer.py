"""pim 番茄计时器——定向 asyncio 定时任务（替代原 5s 轮询循环）。

每个进行中的番茄一个任务：睡到 ``focus_started_at + 25min`` 后自动落账
（1 番茄=1 树），不产生用户通知。启动时从 store 重放进行中番茄；事项状态
变化（停止/完成/撤销）时取消对应任务。没有全局轮询。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from pim_store import PimStore

logger = logging.getLogger(__name__)


class PimFocusTimer:
    """按 todo 的 focus_started_at 定向调度自动落账任务。"""

    def __init__(
        self,
        store: PimStore,
        now: Any | None = None,
        sleep: Any | None = None,
    ) -> None:
        self._store = store
        self._now = now or datetime.now
        self._sleep = sleep or asyncio.sleep
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._closed = False

    def start(self) -> None:
        """启动：从 store 重放所有进行中的番茄（幂等）。"""
        for todo in self._store.list_records("todo"):
            if todo.get("status") == "doing" and todo.get("focus_started_at"):
                self._schedule(str(todo["id"]), todo)

    async def stop(self) -> None:
        """停机：取消全部在途计时任务。"""
        self._closed = True
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def on_change(
        self,
        entity: str,
        key: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
    ) -> None:
        """store 变更观察者：进行中番茄排程，其余状态取消。"""
        if entity != "todo":
            return
        record = self._store.get("todo", key)
        if (
            record is not None
            and record.get("status") == "doing"
            and record.get("focus_started_at")
        ):
            self._schedule(key, record)
        else:
            self._cancel(key)

    def _schedule(self, todo_id: str, record: dict[str, Any]) -> None:
        if self._closed:
            return
        self._cancel(todo_id)
        try:
            started = datetime.fromisoformat(str(record.get("focus_started_at")))
        except (TypeError, ValueError):
            logger.warning("[pim_focus] focus_started_at 不可解析，跳过 | todo=%s", todo_id)
            return
        minutes = int(record.get("focus_duration_minutes") or self._store.focus_default_minutes())
        duration = timedelta(minutes=max(1, minutes))
        remaining = (started + duration - self._now()).total_seconds()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # 同步调用面（测试/CLI）：等下一次 on_change 或显式 start
        self._tasks[todo_id] = loop.create_task(self._run(todo_id, max(remaining, 0.0)))

    async def _run(self, todo_id: str, remaining: float) -> None:
        try:
            await self._sleep(remaining)
            # 先摘任务再落账：record_focus_complete 会触发 on_change，避免自取消
            self._tasks.pop(todo_id, None)
            self._store.record_focus_complete(todo_id)
            logger.info("[pim_focus] 番茄计时到自动落账 | todo=%s", todo_id)
        except asyncio.CancelledError:
            raise
        except (KeyError, ValueError):
            # 计时期间被停止/完成/删除：静默（状态已由用户动作落账）
            pass
        except Exception:  # noqa: BLE001 — 计时任务失败不拖垮其余番茄
            logger.exception("[pim_focus] 番茄自动落账失败 | todo=%s", todo_id)
        finally:
            self._tasks.pop(todo_id, None)

    def _cancel(self, todo_id: str) -> None:
        task = self._tasks.pop(todo_id, None)
        if task is not None:
            task.cancel()
