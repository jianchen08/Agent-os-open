# @feature: FP-MIGR 0.1→0.2迁移清理 | @ci: none-local
"""delete_thread 级联删除测试（0.2 语义改写版，批次 2 §4.1）。

原 0.1 用例断言 metadata.session_id 级联 + mock _get_execution_record_storage；
后者已随三表删除退役（execution_records 全链删除，2026-08-19）。本文件按 0.2
现语义改写（B 类）；2026-08-20 用户裁定（"按照需要的来都要统一"）后补齐
metadata.session_id 级联，读写两口径已统一。

0.2 级联语义（plugins/shared/system/channel_api/routes_threads.py delete_thread）：
- 关联判定 _task_linked_to_thread（读写统一口径，对齐读侧 get_task_tree）：
  task.parent_pipeline_id ∈ 会话管道集 / == thread_id，或
  task.metadata.session_id == thread_id（读侧策略 1，2026-08-20 补齐写侧级联）；
- all_pipeline_ids 自 session.pipeline_ids 起步迭代扩展（关联命中 → 纳入
  task.id / pipeline_run_id / 子任务 pipeline_run_id，直到不动点）；
- 命中任务 hard_delete_sync；内核侧 delete_session（session_routes.rs）只删
  会话记录与消息，任务级联全部在插件侧。

读写已统一（批次 2 留档的分叉已闭环，特征用例见
test_delete_thread_metadata_only_link_cascaded）：任务仅凭
metadata.session_id == thread_id 关联时，读路径 routes_missing.get_task_tree
策略 1 按此匹配展示，写路径 delete_thread 同样按此关联级联删除。统一方向：
以读侧展示口径为准（任务凭 metadata.session_id 关联会话即属该会话）。

覆盖场景：
1. happy path：parent_pipeline_id == thread_id 或 ∈ session.pipeline_ids → 硬删除
2. 迭代展开：任务 A 命中 → A.pipeline_run_id 进入 all_pipeline_ids
   → 任务 B（parent_pipeline_id 指向该管道）联动删除
3. 无关联任务（metadata=None / 指向其它会话管道）：不崩溃、不误删
4. 其它会话管道上的任务：不删除
5. 仅 metadata.session_id 关联：级联删除（特征用例，读写统一后语义）
6. metadata 关联任务的管道链：其 pipeline_run_id 进入管道集 → 挂其下的子任务联动删除
7. 幂等：双关联任务只删一次；重复删除会话 → 404 且不再级联
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

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

    def test_delete_thread_metadata_only_link_cascaded(self) -> None:
        """特征用例（读写统一，2026-08-20）：任务仅凭 metadata.session_id == thread_id
        关联（parent_pipeline_id=None、pipeline_run_id=None）时，delete_thread 级联删除
        该任务——与读侧 routes_missing.get_task_tree 策略 1 的展示口径一致
        （统一方向：以读侧展示口径为准，原批次 2 留档"不级联"分叉已闭环）。
        """
        thread_id = "thread-cascade-005"
        task_meta_only = _make_task("task-meta-only", session_id=thread_id)

        _, task_service = self._call_delete_thread(thread_id, [task_meta_only])

        deleted_ids = {c.args[0] for c in task_service.hard_delete_sync.call_args_list}
        assert deleted_ids == {"task-meta-only"}

    def test_delete_thread_metadata_link_expands_pipeline_chain(self) -> None:
        """metadata 关联任务与管道关联任务走同款级联：meta 任务 M 命中 →
        M.pipeline_run_id 进入 all_pipeline_ids → 子任务 C（parent_pipeline_id
        指向 M.pipeline_run_id）联动删除（与场景 2 的迭代展开同路径）。
        """
        thread_id = "thread-cascade-006"
        task_meta = _make_task(
            "task-meta-chain", session_id=thread_id, pipeline_run_id="pipe-meta-006",
        )
        task_child = _make_task("task-meta-child", parent_pipeline_id="pipe-meta-006")

        _, task_service = self._call_delete_thread(thread_id, [task_meta, task_child])

        deleted_ids = {c.args[0] for c in task_service.hard_delete_sync.call_args_list}
        assert deleted_ids == {"task-meta-chain", "task-meta-child"}

    def test_delete_thread_dual_linked_task_deleted_once(self) -> None:
        """幂等分支：任务同时命中 metadata.session_id 与管道关联（双关联）→
        hard_delete_sync 对该任务恰好调用一次，不重复删除。
        """
        thread_id = "thread-cascade-007"
        task_dual = _make_task(
            "task-dual", session_id=thread_id, parent_pipeline_id="pipe-root-007",
        )

        _, task_service = self._call_delete_thread(
            thread_id, [task_dual], pipeline_ids=["pipe-root-007"],
        )

        dual_calls = [
            c for c in task_service.hard_delete_sync.call_args_list if c.args[0] == "task-dual"
        ]
        assert len(dual_calls) == 1

    def test_delete_thread_repeat_delete_returns_404_without_recascade(self) -> None:
        """幂等分支：重复删除同一会话——第二次 delete_thread 时线程已不存在 →
        404（APIError），且不再触发任务级联（hard_delete_sync 调用数不变）。
        """
        from deps import APIError

        thread_id = "thread-cascade-008"
        task_meta = _make_task("task-idem", session_id=thread_id)

        session = MagicMock()
        session.pipeline_ids = []

        task_service = MagicMock()
        task_service.get_all_tasks.return_value = [task_meta]
        task_service.list_subtasks.return_value = []

        mock_store = MagicMock()
        mock_store.get_session.return_value = session
        mock_store.get_thread.return_value = None
        mock_store.delete_thread.side_effect = [True, False]  # 第二次删除：线程已不存在

        with (
            patch.object(routes_threads, "store", mock_store),
            patch.object(routes_threads, "_safe_get_service", return_value=task_service),
            patch.object(routes_threads, "_notify_session_update"),
            patch.object(routes_threads, "_destroy_session_container"),
        ):
            first = routes_threads.delete_thread(thread_id, {"sub": "user-1"})
            assert first == {"message": "线程已删除"}

            calls_after_first = task_service.hard_delete_sync.call_count
            assert calls_after_first == 1  # 首次删除已按 metadata 关联级联

            with pytest.raises(APIError) as exc_info:
                routes_threads.delete_thread(thread_id, {"sub": "user-1"})

        assert exc_info.value.status_code == 404
        assert task_service.hard_delete_sync.call_count == calls_after_first  # 无二次级联
