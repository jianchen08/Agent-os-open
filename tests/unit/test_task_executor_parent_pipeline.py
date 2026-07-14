"""TaskExecutorMixin 父管道 id 解析回归测试。

BUG-FIX-20260713_task_executor_parent_pipeline:
_execute_background_task 此前用 task_data.get("pipeline_id", "") 取父管道 id
（line 74），但 task_submit 构造的 task_data 字典根本没有 "pipeline_id" 这个
key（见 tools/builtin/task_submit/tool.py:1005-1021），永远取空串。

后果：
- 子管道 registry tag "parent_pipeline" 恒空（task_executor.py:320）
- 用 parent_pipeline_id 反查 registry 拿 thread_id 失败（line 78-84）
- 子管道 sink 在部分路径下解析为 targeted:no-thread，流式消息推不回主管道

正确取法（与 task_notifier.py:702 一致）：从 task 对象的 parent_pipeline_id
属性取。本测试验证提取出的 _resolve_parent_pipeline_id 小方法的正确性。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _make_mixin():
    """构造一个仅含 _resolve_parent_pipeline_id 所需依赖的 TaskExecutorMixin 实例。

    TaskExecutorMixin 是 Mixin，不强制构造参数；直接实例化后注入 task_service。
    """
    from infrastructure.task_executor import TaskExecutorMixin

    mixin = TaskExecutorMixin()
    return mixin


class TestResolveParentPipelineId:
    """_resolve_parent_pipeline_id 应从 task.parent_pipeline_id 取，而非 task_data。"""

    def test_reads_from_task_parent_pipeline_id(self) -> None:
        """task 对象的 parent_pipeline_id 是权威来源（与 task_notifier.py:702 一致）。"""
        mixin = _make_mixin()
        task = MagicMock()
        task.parent_pipeline_id = "pipe_abc123"
        task_service = MagicMock()
        task_service.get_task.return_value = task

        result = mixin._resolve_parent_pipeline_id("task-1", task_service)

        assert result == "pipe_abc123"
        task_service.get_task.assert_called_once_with("task-1")

    def test_task_data_pipeline_id_ignored(self) -> None:
        """task_data 里的 pipeline_id key 必须被忽略（它本就不存在，是 bug 来源）。

        即便 task_data 真的塞了一个 pipeline_id，也不应取它 —— 权威来源是 task 对象。
        """
        mixin = _make_mixin()
        task = MagicMock()
        task.parent_pipeline_id = "pipe_real"
        task_service = MagicMock()
        task_service.get_task.return_value = task

        # task_data 里塞一个干扰值，确保不被取用
        result = mixin._resolve_parent_pipeline_id(
            "task-1", task_service, task_data={"pipeline_id": "SHOULD_BE_IGNORED"},
        )

        assert result == "pipe_real"

    def test_returns_empty_when_task_not_found(self) -> None:
        """task 不存在（get_task 返回 None）时安全回退空串，不抛异常。"""
        mixin = _make_mixin()
        task_service = MagicMock()
        task_service.get_task.return_value = None

        result = mixin._resolve_parent_pipeline_id("ghost-task", task_service)

        assert result == ""

    def test_returns_empty_when_task_service_none(self) -> None:
        """task_service 为 None（极端降级）时安全回退空串。"""
        mixin = _make_mixin()
        result = mixin._resolve_parent_pipeline_id("task-1", None)
        assert result == ""

    def test_returns_empty_when_parent_pipeline_id_none(self) -> None:
        """task.parent_pipeline_id 为 None（顶层任务无父管道）时回退空串。"""
        mixin = _make_mixin()
        task = MagicMock()
        task.parent_pipeline_id = None
        task_service = MagicMock()
        task_service.get_task.return_value = task

        result = mixin._resolve_parent_pipeline_id("task-1", task_service)

        assert result == ""

    def test_returns_empty_when_parent_pipeline_id_missing(self) -> None:
        """task 对象无 parent_pipeline_id 属性时安全回退（getattr 兜底）。"""
        mixin = _make_mixin()
        task = MagicMock()
        # 删除属性模拟 dataclass 未赋值
        del task.parent_pipeline_id
        task_service = MagicMock()
        task_service.get_task.return_value = task

        result = mixin._resolve_parent_pipeline_id("task-1", task_service)

        assert result == ""
