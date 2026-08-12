"""delete_thread 级联删除测试。

覆盖 P1 API 对齐：删除会话时应级联删除数据库数据（execution_record）+ 对应任务。

关键修复点：原 delete_thread 只按 parent_pipeline_id 关联删除任务，
遗漏了 task.metadata.session_id == thread_id 关联的任务
（routes_missing.get_task_tree 正是按 metadata.session_id 过滤会话任务）。

覆盖场景（对应 code_reviewer Should Fix #2）：
1. happy path：metadata.session_id 命中 → 任务被硬删除
2. 迭代展开：任务 A.metadata.session_id 命中 → A.pipeline_run_id 进入 all_pipeline_ids
   → 触发按管道关联的任务 B 联动删除
3. metadata 为 None 的边界：不崩溃、不误删
4. thread_id 不匹配：metadata.session_id 指向其它会话 → 不删除
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import tests.channels.conftest as _ch

_ch.use_channel("api")
from routes_threads import delete_thread  # noqa: E402


def _make_task(
    task_id: str,
    session_id: str | None = None,
    parent_pipeline_id: str | None = None,
    pipeline_run_id: str | None = None,
) -> MagicMock:
    """构造任务对象，支持 metadata.session_id / parent_pipeline_id / pipeline_run_id 三种关联。"""
    task = MagicMock()
    task.id = task_id
    task.parent_pipeline_id = parent_pipeline_id
    task.pipeline_run_id = pipeline_run_id
    task.metadata = {"session_id": session_id} if session_id else None
    return task


class TestDeleteThreadCascadeByMetadataSession:
    """delete_thread 按 metadata.session_id 级联删除关联任务。"""

    def _call_delete_thread(self, thread_id: str, tasks: list[MagicMock]) -> dict:
        """构造 mock 环境调用 delete_thread，返回结果。"""
        session = MagicMock()
        session.pipeline_ids = []

        task_service = MagicMock()
        task_service.get_all_tasks.return_value = tasks
        task_service.list_subtasks.return_value = []

        exec_storage = MagicMock()
        exec_storage._pipeline_root_map = {}

        with (
            patch("channels.api.routes_threads.store.get_session", return_value=session),
            patch("channels.api.routes_threads.store.delete_thread", return_value=True),
            patch("channels.api.routes_threads._get_execution_record_storage", return_value=exec_storage),
            patch("channels.api.routes_threads._safe_get_service", return_value=task_service),
            patch("channels.api.routes_threads._notify_session_update"),
        ):
            return delete_thread(thread_id, {"sub": "user-1"}), task_service

    def test_delete_thread_cascades_tasks_linked_by_metadata_session_id(self) -> None:
        """happy path：任务通过 metadata.session_id == thread_id 关联时，删除会话应级联删除该任务。"""
        thread_id = "thread-cascade-001"
        task = _make_task("task-session-linked", session_id=thread_id)

        _, task_service = self._call_delete_thread(thread_id, [task])

        task_service.hard_delete_sync.assert_called_once_with("task-session-linked")

    def test_delete_thread_iterative_expansion_via_pipeline_run_id(self) -> None:
        """迭代展开：任务 A 经 metadata.session_id 命中 → A.pipeline_run_id 进 all_pipeline_ids
        → 任务 B（经 parent_pipeline_id 关联 A.pipeline_run_id）被联动删除。"""
        thread_id = "thread-cascade-002"
        task_a = _make_task(
            "task-a", session_id=thread_id, pipeline_run_id="pipe-a-001",
        )
        task_b = _make_task(
            "task-b", parent_pipeline_id="pipe-a-001", pipeline_run_id="pipe-b-001",
        )

        _, task_service = self._call_delete_thread(thread_id, [task_a, task_b])

        # A 经 metadata 命中；B 经 A.pipeline_run_id 的管道链命中（迭代展开）
        deleted_ids = {c.args[0] for c in task_service.hard_delete_sync.call_args_list}
        assert "task-a" in deleted_ids
        assert "task-b" in deleted_ids

    def test_delete_thread_metadata_none_does_not_crash(self) -> None:
        """metadata 为 None 的边界：不崩溃、不误删无关联任务。"""
        thread_id = "thread-cascade-003"
        task_no_meta = _make_task("task-no-meta", session_id=None)  # metadata=None
        task_other_session = _make_task("task-other", session_id="another-thread")

        _, task_service = self._call_delete_thread(thread_id, [task_no_meta, task_other_session])

        # 无 metadata / 其它会话的任务都不应被删除
        task_service.hard_delete_sync.assert_not_called()

    def test_delete_thread_does_not_delete_task_of_other_session(self) -> None:
        """thread_id 不匹配：metadata.session_id 指向其它会话 → 不删除。"""
        thread_id = "thread-cascade-004"
        task_other = _make_task("task-other-session", session_id="thread-other-999")

        _, task_service = self._call_delete_thread(thread_id, [task_other])

        task_service.hard_delete_sync.assert_not_called()
