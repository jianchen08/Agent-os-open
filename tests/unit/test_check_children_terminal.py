"""engine._check_children_terminal 终态判定回归测试。

BUG-FIX-fix_20260702_stopped_not_terminal:
terminal_statuses 原为 {"completed","failed","cancelled"}，但 TaskStatus 枚举
仅有 STOPPED/COMPLETED/FAILED（无 cancelled）。cancel_task 产生的子任务状态为
stopped，不在集合内 → 父管道 _check_children_terminal 永远返回 False →
child_task_guard 反复"挂起→超时唤醒→查子任务非终态→再挂起"死循环。

修复：terminal_statuses 改为 {"completed","failed","stopped"}。
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from pipeline.engine import PipelineEngine
from pipeline.types import StateKeys


def _build_engine() -> PipelineEngine:
    return PipelineEngine(
        input_route_table=MagicMock(),
        output_route_table=MagicMock(),
        plugin_registry=MagicMock(),
        services={"__test__": True},
    )


def _make_task(status_value: str) -> MagicMock:
    """构造一个 status.value == status_value 的 mock 任务对象。"""
    task = MagicMock()
    task.status.value = status_value
    return task


def _patch_provider(task_service: Any):
    """patch get_service_provider 返回含 task_service 的 provider。"""
    provider = MagicMock()
    provider.get.return_value = task_service
    return patch(
        "infrastructure.service_provider.get_service_provider",
        return_value=provider,
    )


class TestCheckChildrenTerminal:
    """_check_children_terminal 对各终态值的判定。"""

    def test_stopped_child_counts_as_terminal(self) -> None:
        """修复 A 核心：stopped 子任务应判定为终态（原 bug 漏判）。"""
        engine = _build_engine()
        task_service = MagicMock()
        task_service.get_task.return_value = _make_task("stopped")
        state = {
            StateKeys.PIPELINE_ID: "pipe-test",
            "submitted_task_ids": ["child-stopped"],
        }

        with _patch_provider(task_service):
            result = engine._check_children_terminal(state)

        assert result is True, "stopped 子任务应判定为终态"
        # 终态判定成功后应清空 submitted_task_ids
        assert state["submitted_task_ids"] == []

    def test_completed_child_is_terminal(self) -> None:
        engine = _build_engine()
        task_service = MagicMock()
        task_service.get_task.return_value = _make_task("completed")
        state = {StateKeys.PIPELINE_ID: "p", "submitted_task_ids": ["c"]}

        with _patch_provider(task_service):
            assert engine._check_children_terminal(state) is True

    def test_failed_child_is_terminal(self) -> None:
        engine = _build_engine()
        task_service = MagicMock()
        task_service.get_task.return_value = _make_task("failed")
        state = {StateKeys.PIPELINE_ID: "p", "submitted_task_ids": ["c"]}

        with _patch_provider(task_service):
            assert engine._check_children_terminal(state) is True

    def test_running_child_not_terminal(self) -> None:
        """running 子任务应判定为非终态（继续等待）。"""
        engine = _build_engine()
        task_service = MagicMock()
        task_service.get_task.return_value = _make_task("running")
        state = {StateKeys.PIPELINE_ID: "p", "submitted_task_ids": ["c"]}

        with _patch_provider(task_service):
            assert engine._check_children_terminal(state) is False

    def test_pending_child_not_terminal(self) -> None:
        engine = _build_engine()
        task_service = MagicMock()
        task_service.get_task.return_value = _make_task("pending")
        state = {StateKeys.PIPELINE_ID: "p", "submitted_task_ids": ["c"]}

        with _patch_provider(task_service):
            assert engine._check_children_terminal(state) is False

    def test_mixed_terminal_and_running_not_all_terminal(self) -> None:
        """一个终态 + 一个非终态 → 整体非终态。"""
        engine = _build_engine()
        task_service = MagicMock()
        task_service.get_task.side_effect = [
            _make_task("stopped"),   # 第一个已终态
            _make_task("running"),   # 第二个还在跑
        ]
        state = {StateKeys.PIPELINE_ID: "p", "submitted_task_ids": ["a", "b"]}

        with _patch_provider(task_service):
            assert engine._check_children_terminal(state) is False

    def test_all_terminal_clears_submitted_task_ids(self) -> None:
        """全部终态时 submitted_task_ids 必须被清空（防重复检查）。"""
        engine = _build_engine()
        task_service = MagicMock()
        task_service.get_task.side_effect = [
            _make_task("stopped"),
            _make_task("completed"),
        ]
        state = {StateKeys.PIPELINE_ID: "p", "submitted_task_ids": ["a", "b"]}

        with _patch_provider(task_service):
            result = engine._check_children_terminal(state)

        assert result is True
        assert state["submitted_task_ids"] == []

    def test_empty_submitted_task_ids_returns_false(self) -> None:
        """无 submitted_task_ids 时返回 False（不触发终态唤醒）。"""
        engine = _build_engine()
        state = {StateKeys.PIPELINE_ID: "p", "submitted_task_ids": []}
        assert engine._check_children_terminal(state) is False

    def test_cancelled_value_not_in_enum_still_handled(self) -> None:
        """历史 bug 回归保护：即便上游误传 cancelled，也不应崩。
        cancelled 不在新集合内，会被判非终态（保守等待），不会误唤醒。
        """
        engine = _build_engine()
        task_service = MagicMock()
        task_service.get_task.return_value = _make_task("cancelled")
        state = {StateKeys.PIPELINE_ID: "p", "submitted_task_ids": ["c"]}

        with _patch_provider(task_service):
            # cancelled 不在 {completed,failed,stopped} 内 → 非终态
            assert engine._check_children_terminal(state) is False
