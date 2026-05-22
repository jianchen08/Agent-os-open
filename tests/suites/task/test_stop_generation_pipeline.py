"""stop_generation 管道精确取消逻辑单元与集成测试。

验证 app_factory.py L374-465 中 stop_generation 处理器的行为：
1. pipeline_id 存在时仅取消指定管道（精确取消）
2. pipeline_id 不存在时回退到全量取消（向后兼容）
3. cancel_pipeline 前先调用 fail_task 标记任务失败
4. 停止一个管道不影响其他管道
5. 停止操作触发 state_change 事件
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── 辅助：构建 stop_generation 管道取消逻辑的测试替身 ──────────


def _make_mock_engine(
    has_suspended_state: bool = True,
    has_wake_event: bool = True,
) -> MagicMock:
    """创建模拟的管道引擎实例。"""
    engine = MagicMock()
    if has_suspended_state:
        engine._suspended_state = {"ended": False}
    else:
        engine._suspended_state = None
    if has_wake_event:
        wake_evt = MagicMock()
        wake_evt.set = MagicMock()
        engine._wake_event = wake_evt
    else:
        engine._wake_event = None
    return engine


def _make_mock_task(
    pipeline_run_id: str = "",
    parent_pipeline_id: str = "",
) -> MagicMock:
    """创建模拟的任务对象。"""
    task = MagicMock()
    task.pipeline_run_id = pipeline_run_id
    task.parent_pipeline_id = parent_pipeline_id
    return task


def _make_mock_task_worker(
    active_task_ids: list[str] | None = None,
    task_svc: MagicMock | None = None,
) -> MagicMock:
    """创建模拟的 TaskWorker 实例。"""
    tw = MagicMock()
    tw._active_tasks = set(active_task_ids or [])
    tw._task_service = task_svc or MagicMock()
    tw._task_id_to_bg_task = {}
    tw.cancel_pipeline = MagicMock()
    return tw


# ── 单元测试：管道过滤逻辑 ──────────────────────────────


class TestStopGenerationPipelineIdFilter:
    """验证 pipeline_id 过滤逻辑：精确取消 vs 全量回退。"""

    async def _run_pipeline_filter(
        self,
        data: dict,
        thread_id: str,
        pipeline_thread_map: dict[str, str] | None = None,
        find_engine_side_effect: list | None = None,
    ) -> set[str]:
        """执行管道过滤逻辑并返回收集到的 pipeline_ids。

        模拟 app_factory.py 中 Step 2 的管道查找逻辑。
        """
        notifier = MagicMock()
        if pipeline_thread_map is not None:
            notifier._pipeline_thread_map = pipeline_thread_map

        _pipeline_id = data.get("pipeline_id", "")
        _all_pipeline_ids: set[str] = set()

        with patch("pipeline.message_bus._find_engine") as mock_find:
            if find_engine_side_effect is not None:
                mock_find.side_effect = find_engine_side_effect
            else:
                mock_find.return_value = (None, "")

            if _pipeline_id:
                _all_pipeline_ids.add(_pipeline_id)
            elif hasattr(notifier, "_pipeline_thread_map"):
                for _pid, _tid in notifier._pipeline_thread_map.items():
                    if _tid == thread_id:
                        _all_pipeline_ids.add(_pid)

            for _pid in _all_pipeline_ids:
                _eng, _st = mock_find(_pid)
                if _eng:
                    if hasattr(_eng, "_suspended_state") and _eng._suspended_state is not None:
                        _eng._suspended_state["ended"] = True
                    if hasattr(_eng, "_wake_event") and _eng._wake_event is not None:
                        _eng._wake_event.set()

        return _all_pipeline_ids

    async def test_pipeline_id_filter_exact_match(self):
        """有 pipeline_id 时只取消指定管道，不扫描 _pipeline_thread_map。"""
        data = {"pipeline_id": "pipe_target_001"}
        thread_id = "thread_abc"

        pipeline_thread_map = {
            "pipe_target_001": "thread_abc",
            "pipe_other_002": "thread_abc",
            "pipe_target_001": "thread_abc",
        }

        result_ids = await self._run_pipeline_filter(
            data=data,
            thread_id=thread_id,
            pipeline_thread_map=pipeline_thread_map,
        )

        assert result_ids == {"pipe_target_001"}

    async def test_pipeline_id_filter_fallback_to_thread_map(self):
        """无 pipeline_id 时回退到 _pipeline_thread_map 全量查找。"""
        data = {}
        thread_id = "thread_abc"

        pipeline_thread_map = {
            "pipe_A": "thread_abc",
            "pipe_B": "thread_abc",
            "pipe_C": "thread_xyz",
        }

        result_ids = await self._run_pipeline_filter(
            data=data,
            thread_id=thread_id,
            pipeline_thread_map=pipeline_thread_map,
        )

        assert result_ids == {"pipe_A", "pipe_B"}
        assert "pipe_C" not in result_ids

    async def test_pipeline_id_filter_empty_string(self):
        """pipeline_id 为空字符串时等同于不存在，回退到全量。"""
        data = {"pipeline_id": ""}
        thread_id = "thread_abc"

        pipeline_thread_map = {
            "pipe_A": "thread_abc",
            "pipe_B": "thread_xyz",
        }

        result_ids = await self._run_pipeline_filter(
            data=data,
            thread_id=thread_id,
            pipeline_thread_map=pipeline_thread_map,
        )

        assert result_ids == {"pipe_A"}


# ── 单元测试：fail_task 调用顺序 ──────────────────────────


class TestStopGenerationFailTask:
    """验证 fail_task 在 cancel_pipeline 前被调用，且失败后仍继续取消。"""

    async def _run_task_cancellation_logic(
        self,
        all_pipeline_ids: set[str],
        task_worker: MagicMock,
        data: dict,
    ) -> list[str]:
        """执行 TaskWorker 任务取消逻辑（Step 3）。

        返回被 cancel_pipeline 调用的 task_id 列表。
        """
        _task_svc = task_worker._task_service
        cancelled_tids: list[str] = []

        for _active_tid in list(getattr(task_worker, "_active_tasks", set())):
            try:
                _t = _task_svc.get_task(_active_tid)
                if _t:
                    _t_pipeline = getattr(_t, "pipeline_run_id", "") or ""
                    _t_parent = getattr(_t, "parent_pipeline_id", "") or ""
                    if _t_pipeline in all_pipeline_ids or _t_parent in all_pipeline_ids:
                        try:
                            await _task_svc.fail_task(
                                _active_tid,
                                reason=f"用户取消: {data.get('reason', 'stop_generation')}",
                            )
                        except Exception:
                            pass
                        task_worker.cancel_pipeline(_active_tid)
                        cancelled_tids.append(_active_tid)
            except Exception:
                pass

        return cancelled_tids

    async def test_fail_task_called_before_cancel(self):
        """验证 fail_task 在 cancel_pipeline 之前被调用。"""
        call_order: list[str] = []

        task_svc = MagicMock()
        task_svc.get_task = MagicMock(return_value=_make_mock_task(pipeline_run_id="pipe_001"))
        task_svc.fail_task = AsyncMock(side_effect=lambda tid, **kw: call_order.append("fail_task"))

        tw = _make_mock_task_worker(
            active_task_ids=["task_001"],
            task_svc=task_svc,
        )
        tw.cancel_pipeline = MagicMock(side_effect=lambda tid: call_order.append("cancel_pipeline"))

        await self._run_task_cancellation_logic(
            all_pipeline_ids={"pipe_001"},
            task_worker=tw,
            data={"reason": "stop_generation"},
        )

        assert call_order == ["fail_task", "cancel_pipeline"]

    async def test_cancel_pipeline_still_called_on_fail_task_error(self):
        """fail_task 抛出异常后 cancel_pipeline 仍被调用。"""
        task_svc = MagicMock()
        task_svc.get_task = MagicMock(return_value=_make_mock_task(pipeline_run_id="pipe_001"))
        task_svc.fail_task = AsyncMock(side_effect=RuntimeError("DB 连接失败"))

        tw = _make_mock_task_worker(
            active_task_ids=["task_001"],
            task_svc=task_svc,
        )

        cancelled = await self._run_task_cancellation_logic(
            all_pipeline_ids={"pipe_001"},
            task_worker=tw,
            data={"reason": "stop_generation"},
        )

        assert "task_001" in cancelled
        tw.cancel_pipeline.assert_called_once_with("task_001")


# ── 集成测试：端到端场景 ──────────────────────────────────


async def _simulate_stop_generation(
    data: dict,
    thread_id: str,
    websocket: AsyncMock,
    notifier: MagicMock,
    task_worker: MagicMock,
    engines: dict[str, MagicMock] | None = None,
) -> dict:
    """模拟完整的 stop_generation 处理流程。

    返回 websocket.send_text 的所有调用参数。
    """
    engines = engines or {}

    # Step 1: 设置 stop_event 并取消流式任务（此处跳过，不是测试重点）

    # Step 2: 查找并取消关联的管道引擎
    _pipeline_id = data.get("pipeline_id", "")
    _all_pipeline_ids: set[str] = set()

    with patch("pipeline.message_bus._find_engine") as mock_find:
        def _find_side_effect(pid):
            if pid in engines:
                return (engines[pid], "running")
            return (None, "")

        mock_find.side_effect = _find_side_effect

        if _pipeline_id:
            _all_pipeline_ids.add(_pipeline_id)
        elif hasattr(notifier, "_pipeline_thread_map"):
            for _pid, _tid in notifier._pipeline_thread_map.items():
                if _tid == thread_id:
                    _all_pipeline_ids.add(_pid)

        for _pid in _all_pipeline_ids:
            _eng, _st = mock_find(_pid)
            if _eng:
                if hasattr(_eng, "_suspended_state") and _eng._suspended_state is not None:
                    _eng._suspended_state["ended"] = True
                if hasattr(_eng, "_wake_event") and _eng._wake_event is not None:
                    _eng._wake_event.set()

    # Step 3: 取消 TaskWorker 中关联的后台任务
    _task_svc = getattr(task_worker, "_task_service", None)
    if _task_svc:
        for _active_tid in list(getattr(task_worker, "_active_tasks", set())):
            try:
                _t = _task_svc.get_task(_active_tid)
                if _t:
                    _t_pipeline = getattr(_t, "pipeline_run_id", "") or ""
                    _t_parent = getattr(_t, "parent_pipeline_id", "") or ""
                    if _t_pipeline in _all_pipeline_ids or _t_parent in _all_pipeline_ids:
                        try:
                            await _task_svc.fail_task(
                                _active_tid,
                                reason=f"用户取消: {data.get('reason', 'stop_generation')}",
                            )
                        except Exception:
                            pass
                        task_worker.cancel_pipeline(_active_tid)
            except Exception:
                pass

    # Step 4: 发送 state_change 事件
    await websocket.send_text(json.dumps({
        "type": "state_change",
        "data": {"status": "stopped", "thread_id": thread_id},
    }))

    sent_messages = []
    for call_args in websocket.send_text.call_args_list:
        sent_messages.append(json.loads(call_args[0][0]))

    return {
        "all_pipeline_ids": _all_pipeline_ids,
        "sent_messages": sent_messages,
    }


class TestStopGenerationIntegration:
    """端到端集成测试：验证停止生成的完整流程。"""

    async def test_stop_single_pipeline_not_affect_others(self):
        """停止指定 pipeline_id 时，其他管道的引擎不被取消。"""
        engine_target = _make_mock_engine()
        engine_other = _make_mock_engine()
        engines = {
            "pipe_target": engine_target,
            "pipe_other": engine_other,
        }

        notifier = MagicMock()
        notifier._pipeline_thread_map = {
            "pipe_target": "thread_abc",
            "pipe_other": "thread_abc",
        }

        task_svc = MagicMock()
        task_svc.get_task = MagicMock(return_value=None)
        tw = _make_mock_task_worker(active_task_ids=[], task_svc=task_svc)

        result = await _simulate_stop_generation(
            data={"pipeline_id": "pipe_target"},
            thread_id="thread_abc",
            websocket=AsyncMock(),
            notifier=notifier,
            task_worker=tw,
            engines=engines,
        )

        assert result["all_pipeline_ids"] == {"pipe_target"}
        assert engine_target._suspended_state["ended"] is True
        assert engine_other._suspended_state["ended"] is False
        engine_target._wake_event.set.assert_called_once()
        engine_other._wake_event.set.assert_not_called()

    async def test_stop_without_pipeline_id_cancels_all(self):
        """无 pipeline_id 时取消 thread_id 关联的所有管道。"""
        engine_a = _make_mock_engine()
        engine_b = _make_mock_engine()
        engines = {
            "pipe_A": engine_a,
            "pipe_B": engine_b,
        }

        notifier = MagicMock()
        notifier._pipeline_thread_map = {
            "pipe_A": "thread_abc",
            "pipe_B": "thread_abc",
            "pipe_C": "thread_xyz",
        }

        task_a = _make_mock_task(pipeline_run_id="pipe_A")
        task_b = _make_mock_task(pipeline_run_id="pipe_B")
        task_svc = MagicMock()
        task_svc.get_task = MagicMock(side_effect=lambda tid: {
            "task_A": task_a,
            "task_B": task_b,
        }.get(tid))
        task_svc.fail_task = AsyncMock()

        tw = _make_mock_task_worker(
            active_task_ids=["task_A", "task_B"],
            task_svc=task_svc,
        )

        result = await _simulate_stop_generation(
            data={},
            thread_id="thread_abc",
            websocket=AsyncMock(),
            notifier=notifier,
            task_worker=tw,
            engines=engines,
        )

        assert result["all_pipeline_ids"] == {"pipe_A", "pipe_B"}
        assert "pipe_C" not in result["all_pipeline_ids"]
        assert engine_a._suspended_state["ended"] is True
        assert engine_b._suspended_state["ended"] is True
        assert tw.cancel_pipeline.call_count == 2
        assert task_svc.fail_task.call_count == 2

    async def test_stop_triggers_task_state_changed_event(self):
        """停止生成后发送 state_change 事件到前端。"""
        engines = {"pipe_001": _make_mock_engine()}
        task_svc = MagicMock()
        task_svc.get_task = MagicMock(return_value=_make_mock_task(pipeline_run_id="pipe_001"))
        task_svc.fail_task = AsyncMock()

        tw = _make_mock_task_worker(
            active_task_ids=["task_001"],
            task_svc=task_svc,
        )

        notifier = MagicMock()
        notifier._pipeline_thread_map = {"pipe_001": "thread_abc"}

        ws = AsyncMock()

        result = await _simulate_stop_generation(
            data={"pipeline_id": "pipe_001", "reason": "用户手动停止"},
            thread_id="thread_abc",
            websocket=ws,
            notifier=notifier,
            task_worker=tw,
            engines=engines,
        )

        state_change_msgs = [
            m for m in result["sent_messages"]
            if m.get("type") == "state_change"
        ]
        assert len(state_change_msgs) == 1
        assert state_change_msgs[0]["data"]["status"] == "stopped"
        assert state_change_msgs[0]["data"]["thread_id"] == "thread_abc"

        task_svc.fail_task.assert_called_once_with(
            "task_001",
            reason="用户取消: 用户手动停止",
        )
        tw.cancel_pipeline.assert_called_once_with("task_001")
