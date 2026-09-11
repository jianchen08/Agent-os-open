"""BUG-4 行为回归：关键路径异常必须留痕，不得静默吞。

可观察行为契约（workspace_lifecycle）：
- 依赖（task_tree）抛错时，restore / 父链查找路径记录 warning 且不向上传播
  恢复类异常（降级继续）；
- 降级后无法满足契约时显式报错（拒绝静默回退）——错误是值，不是沉默。

以 raising 的假 task_tree 驱动真实留痕/降级逻辑，断言日志记录与异常行为，
不检查源码文本。

[来源: plugins/shared/system/isolation/workspace_lifecycle.py]
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

import tests._isolation_path  # noqa: F401  # isort: skip —— 须在 workspace_lifecycle import 前注入 sys.path
from workspace_lifecycle import WorkspaceLifecycleManager


class _RaisingTree:
    """get_task 恒抛错的假任务树（外部依赖边界：任务仓储）。"""

    def get_task(self, task_id: str):
        raise RuntimeError(f"task store down: {task_id}")


class _FakeTree:
    """返回固定任务的假任务树。"""

    def __init__(self, tasks: dict) -> None:
        self._tasks = tasks

    def get_task(self, task_id: str):
        return self._tasks.get(task_id)


def _make_manager(tmp_path: Path, task_tree: object, meta_store: dict | None = None) -> WorkspaceLifecycleManager:
    return WorkspaceLifecycleManager(
        resource_merge=None,
        config={"workspace": {"default_mode": "worktree", "root": str(tmp_path)}},
        task_tree=task_tree,
        ws_meta_store=meta_store if meta_store is not None else {},
        base_path=str(tmp_path),
    )


def test_restore_ws_meta_logs_and_degrades_when_store_down(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """任务仓储不可用：restore 留痕降级——不抛、store 不被污染。"""
    mgr = _make_manager(tmp_path, _RaisingTree())
    with caplog.at_level(logging.WARNING, logger="workspace_lifecycle"):
        mgr.restore_ws_meta("t1")  # 不抛异常
    assert any("restore_ws_meta 失败" in r.message for r in caplog.records)
    assert mgr._ws_meta_store == {}


def test_restore_ws_meta_populates_store_from_task_metadata(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """正常路径（正对照）：从 task.metadata.ws_meta 恢复坐标，无警告。"""
    task = type("T", (), {"id": "t2", "parent_task_id": None, "metadata": {"ws_meta": {"mode": "worktree", "path": "/ws/t2"}}})()
    mgr = _make_manager(tmp_path, _FakeTree({"t2": task}))
    with caplog.at_level(logging.WARNING, logger="workspace_lifecycle"):
        mgr.restore_ws_meta("t2")
    assert mgr._ws_meta_store["t2"] == {"mode": "worktree", "path": "/ws/t2"}
    assert not any("restore_ws_meta" in r.message for r in caplog.records)


def test_subtask_without_parent_contract_fails_loudly_after_logging(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """子任务父链查找失败：先留痕，再显式报错（拒绝静默回退独立目录）。"""
    mgr = _make_manager(tmp_path, _RaisingTree())
    with caplog.at_level(logging.WARNING, logger="workspace_lifecycle"), pytest.raises(RuntimeError, match="父链工作空间解析失败"):
        mgr.on_task_start("t3", str(tmp_path / "ws"), {"is_root": False})
    assert any("_start_subtask 查找父任务失败" in r.message for r in caplog.records)
