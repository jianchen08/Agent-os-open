# @feature: FP-MIGR 0.1→0.2迁移清理 | @ci: none-local
"""delete_thread 级联删除测试（0.2 语义改写版，批次 2 §4.1）。

原 0.1 用例断言 metadata.session_id 级联 + mock _get_execution_record_storage；
后者已随三表删除退役（execution_records 全链删除，2026-08-19），前者在 0.2
实现中改为按管道关联级联。本文件按 0.2 现语义改写（B 类）：

0.2 级联语义（plugins/shared/system/channel_api/routes_threads.py delete_thread）：
- all_pipeline_ids 自 session.pipeline_ids 起步迭代扩展（parent_pipeline_id 命中
  → 纳入 task.id / pipeline_run_id / 子任务 pipeline_run_id，直到不动点）；
- 命中条件：task.parent_pipeline_id ∈ all_pipeline_ids 或 == thread_id；
- 命中任务 hard_delete_sync；内核侧 delete_session（session_routes.rs）只删
  会话记录与消息，任务级联全部在插件侧。

已知读写分叉（留档，见 test_delete_thread_metadata_only_link_not_cascaded）：
任务仅凭 metadata.session_id == thread_id 关联时，读路径
routes_missing.get_task_tree 策略 1 仍按此匹配展示，但当前 0.2 delete_thread
不级联删除此类任务。若未来补齐 metadata 级联，请同步更新该特征用例。

覆盖场景：
1. happy path：parent_pipeline_id == thread_id 或 ∈ session.pipeline_ids → 硬删除
2. 迭代展开：任务 A 命中 → A.pipeline_run_id 进入 all_pipeline_ids
   → 任务 B（parent_pipeline_id 指向该管道）联动删除
3. 无关联任务（metadata=None / 指向其它会话管道）：不崩溃、不误删
4. 其它会话管道上的任务：不删除
5. 仅 metadata.session_id 关联：当前 0.2 不级联（特征用例，留档分叉）
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import tests.channels.conftest as _ch

_ch.use_channel("api")
import routes_threads  # noqa: E402


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


class TestDeleteThreadCascadeByPipelineLinkage:
    """delete_thread 按 0.2 管道关联（parent_pipeline_id）级联删除关联任务。"""

    def _call_delete_thread(
        self,
        thread_id: str,
        tasks: list[MagicMock],
        pipeline_ids: list[str] | None = None,
    ) -> tuple[dict, MagicMock]:
        """构造 mock 环境调用 delete_thread，返回 (结果, task_service)。"""
        session = MagicMock()
        session.pipeline_ids = list(pipeline_ids or [])

        task_service = MagicMock()
        task_service.get_all_tasks.return_value = tasks
        task_service.list_subtasks.return_value = []

        mock_store = MagicMock()
        mock_store.get_session.return_value = session
        mock_store.get_thread.return_value = None
        mock_store.delete_thread.return_value = True

        notify = MagicMock()

        with (
            patch.object(routes_threads, "store", mock_store),
            patch.object(routes_threads, "_safe_get_service", return_value=task_service),
            patch.object(routes_threads, "_notify_session_update", notify),
            patch.object(routes_threads, "_destroy_session_container"),
        ):
            result = routes_threads.delete_thread(thread_id, {"sub": "user-1"})

        notify.assert_called_once_with(thread_id, "deleted")
        return result, task_service

    def test_delete_thread_cascades_tasks_linked_by_thread_or_session_pipeline(self) -> None:
        """happy path：任务经 parent_pipeline_id == thread_id 或 ∈ session.pipeline_ids
        关联时，删除会话应级联删除该任务（0.2 关联形态）。"""
        thread_id = "thread-cascade-001"
        task_thread_linked = _make_task("task-thread-linked", parent_pipeline_id=thread_id)
        task_pipeline_linked = _make_task("task-pipeline-linked", parent_pipeline_id="pipe-root-001")

        result, task_service = self._call_delete_thread(
            thread_id, [task_thread_linked, task_pipeline_linked], pipeline_ids=["pipe-root-001"],
        )

        deleted_ids = {c.args[0] for c in task_service.hard_delete_sync.call_args_list}
        assert deleted_ids == {"task-thread-linked", "task-pipeline-linked"}
        assert result == {"message": "线程已删除"}

    def test_delete_thread_iterative_expansion_via_pipeline_run_id(self) -> None:
        """迭代展开：会话管道 pipe-root 命中任务 A → A.pipeline_run_id 进 all_pipeline_ids
        → 任务 B（经 parent_pipeline_id 关联 A.pipeline_run_id）被联动删除。

        注意：all_pipeline_ids 以 session.pipeline_ids 为种子，空种子下扩展循环
        不启动（while len > prev_size）——迭代展开需会话持有至少一条管道。
        """
        thread_id = "thread-cascade-002"
        task_a = _make_task(
            "task-a", parent_pipeline_id="pipe-root-000", pipeline_run_id="pipe-a-001",
        )
        task_b = _make_task(
            "task-b", parent_pipeline_id="pipe-a-001", pipeline_run_id="pipe-b-001",
        )

        _, task_service = self._call_delete_thread(
            thread_id, [task_a, task_b], pipeline_ids=["pipe-root-000"],
        )

        # A 经会话管道直接命中；B 经 A.pipeline_run_id 的管道链命中（迭代展开）
        deleted_ids = {c.args[0] for c in task_service.hard_delete_sync.call_args_list}
        assert "task-a" in deleted_ids
        assert "task-b" in deleted_ids

    def test_delete_thread_unlinked_tasks_not_deleted(self) -> None:
        """无关联任务的边界：metadata 为 None / 指向其它会话 → 不崩溃、不误删。"""
        thread_id = "thread-cascade-003"
        task_no_meta = _make_task("task-no-meta", session_id=None)  # metadata=None
        task_other_session = _make_task("task-other", session_id="another-thread")

        result, task_service = self._call_delete_thread(thread_id, [task_no_meta, task_other_session])

        task_service.hard_delete_sync.assert_not_called()
        assert result == {"message": "线程已删除"}

    def test_delete_thread_does_not_delete_task_of_other_session(self) -> None:
        """thread_id 不匹配：任务挂在其它会话的管道上（parent 指向其它会话管道）→ 不删除。"""
        thread_id = "thread-cascade-004"
        task_other = _make_task(
            "task-other-session", session_id="thread-other-999", parent_pipeline_id="pipe-other-999",
        )

        _, task_service = self._call_delete_thread(thread_id, [task_other])

        task_service.hard_delete_sync.assert_not_called()

    def test_delete_thread_metadata_only_link_not_cascaded(self) -> None:
        """特征用例（0.2 现语义留档）：任务仅凭 metadata.session_id == thread_id 关联
        （parent_pipeline_id=None、pipeline_run_id=None）时，当前 delete_thread 不级联删除。

        读路径 routes_missing.get_task_tree 策略 1 仍按 metadata.session_id 匹配展示
        此类任务——读写路径分叉为已知遗留（原 0.1 修复语义为级联删除）。
        若未来补齐 metadata 级联，请把本用例改为断言 hard_delete_sync 被调用。
        """
        thread_id = "thread-cascade-005"
        task_meta_only = _make_task("task-meta-only", session_id=thread_id)

        _, task_service = self._call_delete_thread(thread_id, [task_meta_only])

        task_service.hard_delete_sync.assert_not_called()
