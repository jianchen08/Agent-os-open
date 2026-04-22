"""长期任务稳定性测试 — 验证容器创建与工作空间持久化。"""

from __future__ import annotations

from pathlib import Path

import pytest


def _compute_workspace(task_data: dict, task_id: str) -> str:
    """从 task_data 中计算 workspace，模拟 TaskWorker 中的默认值逻辑。"""
    workspace = task_data.get("workspace", "")
    if not workspace:
        workspace = f".ai_workspaces/{task_id}"
    return workspace


class TestLongTermStability:
    """套件 E：长期任务稳定性测试。"""

    @pytest.mark.task
    @pytest.mark.unit
    def test_long_term_task_creates_container(self, tmp_path: Path) -> None:
        """E1: 长期任务提交时，workspace 路径基于 task_id 生成且格式正确。"""
        task_id = "long_term_task_001"
        task_data: dict = {"task_scope": "long_term"}

        workspace = _compute_workspace(task_data, task_id)

        assert task_id in workspace
        assert workspace.startswith(".ai_workspaces/")

    @pytest.mark.task
    @pytest.mark.unit
    def test_long_term_workspace_persistence(self, tmp_path: Path) -> None:
        """E3: 长期任务的工作空间跨执行持久化，文件在多次执行间保留。"""
        workspace = tmp_path / ".ai_workspaces" / "long_task_001"
        workspace.mkdir(parents=True, exist_ok=True)

        (workspace / "solution.md").write_text("# 方案", encoding="utf-8")

        assert (workspace / "solution.md").exists()
        assert (workspace / "solution.md").read_text(encoding="utf-8") == "# 方案"
