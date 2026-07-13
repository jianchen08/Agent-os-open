"""TaskTool.get（列表模式）过滤回归测试。

回归 BUG-FIX(list_empty)：原实现在 `_get_task_list` 中先按 limit=50 截断、
再做权限/状态/scope 过滤，且底层 `list_all` 未启用 reverse 排序，导致：

1. `service.list_all(limit=50)` 拿到的是「按 created_at 升序」的最早 50 条
   任务（多 session 共享同一存储时，这 50 条很可能不属于当前 session）。
2. 后续 session_id / status / task_scope / show_all 过滤把它们全过滤掉。
3. 返回空列表，但 `get(task_id=...)` 仍能命中（因走单查接口）。

修复后必须满足：
- 列表查询能跨越较多老任务，定位到当前 session 较新的任务。
- status / task_scope 过滤生效，但不会因截断窗口而漏掉本应匹配的任务。
- show_all=true 时能把 L2 提交的子任务一并列出。
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from tasks.types import TaskPriority, TaskStatus, create_task
from tools.builtin.task.tool import TaskTool


def _make_task(
    *,
    title: str,
    created_at: str,
    session_id: str = "sess-A",
    submitted_by_level: int = 1,
    status: TaskStatus = TaskStatus.RUNNING,
    task_scope: str = "non_container",
) -> Any:
    task = create_task(
        title=title,
        description=title,
        priority=TaskPriority.NORMAL,
        metadata={
            "session_id": session_id,
            "submitted_by_level": submitted_by_level,
            "task_scope": task_scope,
        },
    )
    task.status = status
    task.created_at = created_at
    return task


def _build_tool(tasks: list[Any]) -> TaskTool:
    tool = TaskTool()

    async def _list_all(limit: int = 1000, session_id: str | None = None, reverse: bool = False):
        items = list(tasks)
        if session_id:
            items = [t for t in items if t.metadata.get("session_id") == session_id]
        items.sort(key=lambda t: t.created_at or "", reverse=reverse)
        return items[:limit]

    fake_service = MagicMock()
    fake_service.list_all = _list_all
    tool._task_service = fake_service
    # 屏蔽 ExecutionRecordStorage 访问，单元测试无需 provider
    tool._get_latest_activity = lambda _t: None  # type: ignore[assignment]
    tool._calc_elapsed_seconds = staticmethod(lambda _t: None)  # type: ignore[assignment]
    return tool


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture()
def many_tasks() -> list[Any]:
    """构造 80 条「老任务」+ 5 条「当前 session 新任务」。

    这是触发原 bug 的最小数据形态：limit=50 + 升序排序会把所有新任务挤掉。
    """
    older = [
        _make_task(
            title=f"old-{i}",
            created_at=f"2024-01-01T00:00:{i:02d}",
            session_id="sess-OLD",
        )
        for i in range(80)
    ]
    newer = [
        _make_task(
            title=f"new-{i}",
            created_at=f"2026-06-24T10:00:{i:02d}",
            session_id="sess-A",
            status=TaskStatus.RUNNING if i % 2 == 0 else TaskStatus.COMPLETED,
        )
        for i in range(5)
    ]
    return older + newer


class TestGetTaskListFilter:
    """`_get_task_list` 过滤行为回归。"""

    def test_session_scope_returns_current_session_tasks(self, many_tasks):
        tool = _build_tool(many_tasks)
        result = _run(
            tool._get_task_list(
                inputs={"action": "get", "session_id": "sess-A", "limit": 50},
                parent_agent_level=1,
            )
        )

        assert result.success, result.error
        rows = result.output["d"]
        titles = {r[1] for r in rows}
        # 5 条新任务全部应命中，旧 session 的不应出现
        assert {"new-0", "new-1", "new-2", "new-3", "new-4"} <= titles
        assert not any(t.startswith("old-") for t in titles)

    def test_status_filter_after_limit_does_not_empty_result(self, many_tasks):
        """status=running 必须能命中当前 session 中 running 的任务，
        即使存储里有大量更早的任务，截断也不应把它们挤掉。"""
        tool = _build_tool(many_tasks)
        result = _run(
            tool._get_task_list(
                inputs={
                    "action": "get",
                    "session_id": "sess-A",
                    "status": "running",
                    "limit": 50,
                },
                parent_agent_level=1,
            )
        )

        assert result.success, result.error
        rows = result.output["d"]
        assert rows, "status=running 过滤后不应为空（回归 BUG-FIX(list_empty)）"
        for row in rows:
            assert row[2] == "running"

    def test_task_scope_filter_after_limit_does_not_empty_result(self, many_tasks):
        """task_scope=non_container 过滤同样不应被截断窗口吞掉。"""
        tool = _build_tool(many_tasks)
        result = _run(
            tool._get_task_list(
                inputs={
                    "action": "get",
                    "session_id": "sess-A",
                    "task_scope": "non_container",
                    "limit": 50,
                },
                parent_agent_level=1,
            )
        )

        assert result.success, result.error
        assert result.output["d"], "task_scope 过滤后不应为空"

    def test_show_all_includes_l2_submitted_tasks(self):
        """show_all=true 时应把 L2 提交的子任务一并展示。"""
        tasks = [
            _make_task(
                title="l1-task",
                created_at="2026-06-24T10:00:00",
                submitted_by_level=1,
            ),
            _make_task(
                title="l2-task",
                created_at="2026-06-24T10:00:01",
                submitted_by_level=2,
            ),
        ]
        tool = _build_tool(tasks)

        no_show_all = _run(
            tool._get_task_list(
                inputs={"action": "get", "session_id": "sess-A"},
                parent_agent_level=1,
            )
        )
        with_show_all = _run(
            tool._get_task_list(
                inputs={"action": "get", "session_id": "sess-A", "show_all": True},
                parent_agent_level=1,
            )
        )

        titles_default = {row[1] for row in no_show_all.output["d"]}
        titles_show_all = {row[1] for row in with_show_all.output["d"]}
        assert titles_default == {"l1-task"}
        assert titles_show_all == {"l1-task", "l2-task"}
