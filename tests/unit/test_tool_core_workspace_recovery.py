"""tool_core 容器隔离 workspace 丢失恢复测试。

Bug 真实根因（来自 logs/pipeline/pipeline_e96578575097.log 的现场）：
任务运行中途 workspace 从 state 丢失（观察发生在 human_interaction 交互 /
外部消息注入后），并非仅发生在 revive/idle/cancel 启动路径。bash_execute 走
容器隔离时读 state["workspace"]，丢失即被守卫拒绝。

治标：_execute_in_isolated_container 在 state 无 workspace 时，用 state 中仍存
的 task_id 反查 task.metadata.ws_meta.path 恢复，覆盖所有丢失路径。

本测试验证恢复辅助函数 _recover_workspace_from_task 的逻辑。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest

from plugins.core.tool_core.plugin import _recover_workspace_from_task


@dataclass
class FakeTask:
    id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class FakeTaskService:
    def __init__(self, task: FakeTask | None = None):
        self._task = task

    def get_task(self, task_id: str) -> FakeTask | None:
        if self._task is None:
            return None
        return self._task if self._task.id == task_id else None


class FakeProvider:
    def __init__(self, task_service: FakeTaskService | None):
        self._ts = task_service

    def get(self, key: str, default=None):
        return self._ts if key == "task_service" else default


def _task_with_workspace(task_id: str, ws_path: str) -> FakeTask:
    return FakeTask(id=task_id, metadata={"ws_meta": {"path": ws_path}})


# ---------------------------------------------------------------------------
# 恢复成功路径
# ---------------------------------------------------------------------------


def test_recover_workspace_when_state_lost():
    """state 无 workspace 但 task_id 在 → 从 ws_meta.path 恢复成功。

    ws_meta.path 用绝对路径（带盘符），避免 resolve_task_workspace 的
    相对→绝对转换逻辑干扰断言。
    """
    ws_path = "D:/workspaces/task-1"
    task = _task_with_workspace("task-1", ws_path)
    provider = FakeProvider(FakeTaskService(task))

    # _recover_workspace_from_task 内部 import infrastructure.service_provider.get_service_provider
    with patch(
        "infrastructure.service_provider.get_service_provider",
        return_value=provider,
    ):
        ws = _recover_workspace_from_task({"task_id": "task-1"}, "task-1")

    # resolve_task_workspace 返回 str(Path(...))，可能规范化分隔符
    from pathlib import Path  # noqa: PLC0415

    assert ws == str(Path(ws_path))


def test_recover_returns_none_for_unknown_task_id():
    """task_id 为 unknown 占位值 → 不反查，返回 None。"""
    with patch(
        "infrastructure.service_provider.get_service_provider",
        return_value=FakeProvider(FakeTaskService(None)),
    ):
        assert _recover_workspace_from_task({}, "unknown") is None


def test_recover_returns_none_for_empty_task_id():
    """task_id 为空 → 返回 None。"""
    with patch(
        "infrastructure.service_provider.get_service_provider",
        return_value=FakeProvider(FakeTaskService(None)),
    ):
        assert _recover_workspace_from_task({}, "") is None


def test_recover_returns_none_when_task_not_found():
    """任务系统中找不到该任务 → 返回 None。"""
    with patch(
        "infrastructure.service_provider.get_service_provider",
        return_value=FakeProvider(FakeTaskService(None)),
    ):
        assert _recover_workspace_from_task({"task_id": "ghost"}, "ghost") is None


def test_recover_returns_none_when_ws_meta_missing():
    """任务存在但 ws_meta 缺失 → 返回 None（不编造路径）。"""
    task = FakeTask(id="task-1", metadata={})
    with patch(
        "infrastructure.service_provider.get_service_provider",
        return_value=FakeProvider(FakeTaskService(task)),
    ):
        assert _recover_workspace_from_task({"task_id": "task-1"}, "task-1") is None


def test_recover_returns_none_when_provider_unavailable():
    """ServiceProvider 不可用 → 返回 None（不抛异常）。"""
    with patch(
        "infrastructure.service_provider.get_service_provider",
        return_value=None,
    ):
        assert _recover_workspace_from_task({"task_id": "task-1"}, "task-1") is None


def test_recover_returns_none_when_task_service_missing():
    """task_service 未注册 → 返回 None。"""
    with patch(
        "infrastructure.service_provider.get_service_provider",
        return_value=FakeProvider(None),
    ):
        assert _recover_workspace_from_task({"task_id": "task-1"}, "task-1") is None


def test_recover_swallows_exceptions():
    """任何异常都返回 None（绝不在容器执行点抛异常）。"""

    class BrokenTaskService:
        def get_task(self, task_id):
            raise RuntimeError("db down")

    with patch(
        "infrastructure.service_provider.get_service_provider",
        return_value=FakeProvider(BrokenTaskService()),
    ):
        assert _recover_workspace_from_task({"task_id": "task-1"}, "task-1") is None
