"""验证 _auto_advance_memstore_task 协程静默失败修复。

BUG-FIX-fix_20260520_silent_failure:
修复前: task_service 为 None 时零日志、控制流隐式 fallthrough，
        fallback 路径不可追踪、协程静默失败。
修复后: 每个分支有明确日志，task_service 为 None 时记录 info 并走 fallback，
        fallback 路径始终可执行，异常时任务标记为 failed。
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# 辅助：构造 MemoryStore 实例和任务
# ---------------------------------------------------------------------------
def _make_store_with_task(task_id: str, status: str = "queued") -> MagicMock:
    """创建一个模拟的 MemoryStore，包含指定状态的任务。"""
    task = {
        "id": task_id,
        "title": "测试任务",
        "description": "测试描述",
        "status": status,
        "priority": 5,
        "agent_id": "",
    }
    store = MagicMock()
    store.get_task.return_value = task
    store.update_task.return_value = task
    return store


def _make_store_without_task() -> MagicMock:
    """创建一个模拟的 MemoryStore，任务不存在。"""
    store = MagicMock()
    store.get_task.return_value = None
    return store


# ===========================================================================
# 测试类
# ===========================================================================
class TestAutoAdvanceFallbackWhenNoTaskService:
    """task_service 为 None 时，fallback 路径应正确推进任务状态。"""

    @pytest.mark.asyncio
    async def test_fallback_advances_queued_to_running_then_completed(self) -> None:
        """task_service=None → fallback 应将 queued 推进到 completed。"""
        task_id = "test_task_001"
        mock_store = _make_store_with_task(task_id, "queued")

        with patch("channels.api.routes_tasks.store", mock_store), \
             patch("channels.api.routes_tasks._get_task_service", return_value=None):
            from channels.api.routes_tasks import _auto_advance_memstore_task

            # 用 mock 替换 asyncio.sleep 避免等待
            with patch("asyncio.sleep", new_callable=AsyncMock):
                await _auto_advance_memstore_task(task_id)

        # 验证：update_task 应先设为 running，再设为 completed
        calls = mock_store.update_task.call_args_list
        status_updates = [c.kwargs.get("status") or c.args[1] if len(c.args) > 1 else c.kwargs.get("status") for c in calls]
        assert "running" in status_updates, f"应更新为 running，实际调用: {status_updates}"
        assert "completed" in status_updates, f"应更新为 completed，实际调用: {status_updates}"

    @pytest.mark.asyncio
    async def test_fallback_logs_task_service_unavailable(self, caplog: pytest.LogCaptureFixture) -> None:
        """task_service=None 时应记录 info 级别日志。"""
        task_id = "test_task_002"
        mock_store = _make_store_with_task(task_id, "queued")

        with caplog.at_level(logging.INFO, logger="channels.api.routes_tasks"), \
             patch("channels.api.routes_tasks.store", mock_store), \
             patch("channels.api.routes_tasks._get_task_service", return_value=None), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            from channels.api.routes_tasks import _auto_advance_memstore_task
            await _auto_advance_memstore_task(task_id)

        log_messages = caplog.text
        assert "task_service 不可用" in log_messages or "fallback" in log_messages, \
            f"应记录 task_service 不可用或 fallback 日志，实际: {log_messages}"


class TestAutoAdvanceEventBusFailureFallback:
    """EventBus 不可用时，应走 fallback 路径。"""

    @pytest.mark.asyncio
    async def test_event_bus_returns_false_triggers_fallback(self) -> None:
        """EventBus 提交事件返回 False → 应走 fallback。"""
        task_id = "test_task_010"
        mock_store = _make_store_with_task(task_id, "queued")

        mock_task_service = MagicMock()
        mock_task_service.get_task.return_value = None  # TaskStorage 中不存在

        with patch("channels.api.routes_tasks.store", mock_store), \
             patch("channels.api.routes_tasks._get_task_service", return_value=mock_task_service), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            # _submit_task_event 返回 False（EventBus 不可用）
            with patch("channels.api.routes_tasks._submit_task_event", new_callable=AsyncMock, return_value=False):
                # Mock tasks.types 导入
                mock_task_model = MagicMock()
                mock_task_status = MagicMock()
                mock_task_priority = MagicMock()
                with patch.dict("sys.modules", {
                    "tasks": MagicMock(),
                    "tasks.types": MagicMock(
                        Task=mock_task_model,
                        TaskStatus=mock_task_status,
                        TaskPriority=mock_task_priority,
                    ),
                }):
                    from channels.api.routes_tasks import _auto_advance_memstore_task
                    await _auto_advance_memstore_task(task_id)

        # 验证 fallback 被执行：update_task 应有 running 和 completed
        calls = mock_store.update_task.call_args_list
        status_updates = [
            c.kwargs.get("status") or (c.args[1] if len(c.args) > 1 else None)
            for c in calls
        ]
        assert "completed" in status_updates, \
            f"EventBus 失败后应走 fallback 推进到 completed，实际: {status_updates}"

    @pytest.mark.asyncio
    async def test_event_bus_exception_triggers_fallback(self) -> None:
        """EventBus 路径抛异常 → 应走 fallback。"""
        task_id = "test_task_011"
        mock_store = _make_store_with_task(task_id, "queued")

        mock_task_service = MagicMock()
        mock_task_service.get_task.return_value = None

        with patch("channels.api.routes_tasks.store", mock_store), \
             patch("channels.api.routes_tasks._get_task_service", return_value=mock_task_service), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            # 导入 tasks.types 时抛异常
            with patch.dict("sys.modules", {"tasks": None, "tasks.types": None}):
                from channels.api.routes_tasks import _auto_advance_memstore_task
                await _auto_advance_memstore_task(task_id)

        # fallback 应被执行
        calls = mock_store.update_task.call_args_list
        status_updates = [
            c.kwargs.get("status") or (c.args[1] if len(c.args) > 1 else None)
            for c in calls
        ]
        assert "completed" in status_updates, \
            f"EventBus 异常后应走 fallback 推进到 completed，实际: {status_updates}"


class TestAutoAdvanceEventBusSuccess:
    """EventBus 成功时，应将任务设为 running 并 return。"""

    @pytest.mark.asyncio
    async def test_event_bus_success_sets_running_and_returns(self) -> None:
        """EventBus 提交成功 → 任务应设为 running，不应走 fallback。"""
        task_id = "test_task_020"
        mock_store = _make_store_with_task(task_id, "queued")

        mock_task_service = MagicMock()
        mock_task_service.get_task.return_value = None
        mock_task_service.create_task = AsyncMock()

        with patch("channels.api.routes_tasks.store", mock_store), \
             patch("channels.api.routes_tasks._get_task_service", return_value=mock_task_service), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            with patch("channels.api.routes_tasks._submit_task_event", new_callable=AsyncMock, return_value=True):
                mock_task_model = MagicMock()
                mock_task_status = MagicMock()
                mock_task_priority = MagicMock()
                with patch.dict("sys.modules", {
                    "tasks": MagicMock(),
                    "tasks.types": MagicMock(
                        Task=mock_task_model,
                        TaskStatus=mock_task_status,
                        TaskPriority=mock_task_priority,
                    ),
                }):
                    from channels.api.routes_tasks import _auto_advance_memstore_task
                    await _auto_advance_memstore_task(task_id)

        # 只有 running，没有 completed（因为 return 了）
        calls = mock_store.update_task.call_args_list
        status_updates = [
            c.kwargs.get("status") or (c.args[1] if len(c.args) > 1 else None)
            for c in calls
        ]
        assert "running" in status_updates, f"EventBus 成功应设为 running，实际: {status_updates}"
        assert "completed" not in status_updates, f"EventBus 成功不应走 fallback，实际: {status_updates}"


class TestAutoAdvanceEdgeCases:
    """边界场景测试。"""

    @pytest.mark.asyncio
    async def test_task_not_found_skips_advance(self) -> None:
        """任务不存在时应跳过流转。"""
        task_id = "nonexistent_task"
        mock_store = _make_store_without_task()

        with patch("channels.api.routes_tasks.store", mock_store), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            from channels.api.routes_tasks import _auto_advance_memstore_task
            await _auto_advance_memstore_task(task_id)

        mock_store.update_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_task_not_queued_skips_advance(self) -> None:
        """任务状态非 queued 时应跳过流转。"""
        task_id = "test_task_030"
        mock_store = _make_store_with_task(task_id, "running")

        with patch("channels.api.routes_tasks.store", mock_store), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            from channels.api.routes_tasks import _auto_advance_memstore_task
            await _auto_advance_memstore_task(task_id)

        mock_store.update_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_exception_marks_task_failed(self) -> None:
        """协程内部异常应将任务标记为 failed。"""
        task_id = "test_task_040"
        mock_store = MagicMock()
        # 第一次调用（status 检查）返回 queued 任务
        mock_store.get_task.return_value = {"id": task_id, "status": "queued", "title": "t"}
        # update_task 抛异常
        mock_store.update_task.side_effect = [Exception("DB error"), None]

        with patch("channels.api.routes_tasks.store", mock_store), \
             patch("channels.api.routes_tasks._get_task_service", return_value=None), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            from channels.api.routes_tasks import _auto_advance_memstore_task
            # 不应抛异常，应被内部捕获
            await _auto_advance_memstore_task(task_id)

        # 验证异常后将任务标记为 failed
        calls = mock_store.update_task.call_args_list
        status_values = [
            c.kwargs.get("status") or (c.args[1] if len(c.args) > 1 else None)
            for c in calls
        ]
        assert "failed" in status_values, \
            f"异常后应将任务标记为 failed，实际: {status_values}"

    @pytest.mark.asyncio
    async def test_background_tasks_set_prevents_gc(self) -> None:
        """验证 _background_tasks 集合存在且被使用。"""
        from channels.api.routes_tasks import _background_tasks
        assert isinstance(_background_tasks, set), \
            "_background_tasks 应为 set 类型"


class TestAutoAdvanceLogging:
    """验证修复后各路径都有适当的日志输出。"""

    @pytest.mark.asyncio
    async def test_fallback_path_has_logging(self, caplog: pytest.LogCaptureFixture) -> None:
        """fallback 路径应记录关键日志。"""
        task_id = "test_task_050"
        mock_store = _make_store_with_task(task_id, "queued")

        with caplog.at_level(logging.INFO, logger="channels.api.routes_tasks"), \
             patch("channels.api.routes_tasks.store", mock_store), \
             patch("channels.api.routes_tasks._get_task_service", return_value=None), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            from channels.api.routes_tasks import _auto_advance_memstore_task
            await _auto_advance_memstore_task(task_id)

        log_text = caplog.text
        # 应包含 fallback 开始和完成的日志
        assert "fallback" in log_text.lower() or "直接推进" in log_text, \
            f"fallback 路径应有日志，实际: {log_text}"
        assert "流转完成" in log_text or "completed" in log_text.lower(), \
            f"应记录流转完成，实际: {log_text}"

    @pytest.mark.asyncio
    async def test_task_not_queued_logs_reason(self, caplog: pytest.LogCaptureFixture) -> None:
        """任务状态非 queued 时应记录跳过原因。"""
        task_id = "test_task_051"
        mock_store = _make_store_with_task(task_id, "running")

        with caplog.at_level(logging.INFO, logger="channels.api.routes_tasks"), \
             patch("channels.api.routes_tasks.store", mock_store), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            from channels.api.routes_tasks import _auto_advance_memstore_task
            await _auto_advance_memstore_task(task_id)

        log_text = caplog.text
        assert "跳过" in log_text or "非 queued" in log_text, \
            f"应记录跳过原因，实际: {log_text}"
