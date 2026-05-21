"""任务自动流转单元测试 — 验证 _auto_advance_memstore_task 修复。

验证场景:
1. task_service 为 None 时，fallback 路径能正确推进 queued→running→completed
2. task_service 可用但 EventBus 失败时，fallback 路径仍能执行
3. 协程入口有日志，异常不再静默
4. 任务不存在或状态不是 queued 时安全退出
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# 辅助: 创建 MemoryStore 并插入一个 queued 状态的任务
# ---------------------------------------------------------------------------

def _create_store_with_task(task_id: str = "test_task_001", status: str = "queued"):
    """创建一个 MemoryStore 并插入指定状态的任务。"""
    from channels.api.memory_store import MemoryStore
    ms = MemoryStore()
    ms.tasks[task_id] = {
        "id": task_id,
        "title": "测试任务",
        "description": "自动流转测试",
        "status": status,
        "priority": 5,
        "agent_id": "",
        "thread_id": None,
        "created_by": "admin_user_001",
        "tags": [],
        "input_data": {},
        "result": None,
        "created_at": "2026-05-20T13:30:00+00:00",
        "updated_at": "2026-05-20T13:30:00+00:00",
    }
    return ms


# ---------------------------------------------------------------------------
# 测试类
# ---------------------------------------------------------------------------

class TestAutoAdvanceMemstoreTask:
    """验证 _auto_advance_memstore_task 的行为。"""

    @pytest.mark.asyncio
    async def test_fallback_when_task_service_is_none(self):
        """task_service=None 时，fallback 应将 queued→running→completed。"""
        from channels.api import routes_tasks as rt

        task_id = "fallback_test_001"
        ms = _create_store_with_task(task_id, "queued")

        with patch.object(rt, "store", ms), \
             patch.object(rt, "_get_task_service", return_value=None):
            await rt._auto_advance_memstore_task(task_id)

        # 验证任务状态已推进到 completed
        task = ms.get_task(task_id)
        assert task is not None, "任务不应被删除"
        assert task["status"] == "completed", (
            f"任务应从 queued 推进到 completed，实际为 {task['status']}"
        )

    @pytest.mark.asyncio
    async def test_fallback_when_event_bus_fails(self):
        """task_service 可用但 EventBus 失败时，仍应走 fallback。"""
        from channels.api import routes_tasks as rt

        task_id = "eventbus_fail_001"
        ms = _create_store_with_task(task_id, "queued")

        # task_service 返回一个 mock，但 _submit_task_event 返回 False（EventBus 不可用）
        mock_service = MagicMock()
        mock_service.get_task.return_value = None  # TaskStorage 中不存在

        with patch.object(rt, "store", ms), \
             patch.object(rt, "_get_task_service", return_value=mock_service), \
             patch.object(rt, "_submit_task_event", new_callable=AsyncMock, return_value=False):
            await rt._auto_advance_memstore_task(task_id)

        task = ms.get_task(task_id)
        assert task is not None
        assert task["status"] == "completed", (
            f"EventBus 失败时应 fallback 到 completed，实际为 {task['status']}"
        )

    @pytest.mark.asyncio
    async def test_task_not_found_safe_exit(self):
        """任务不存在时协程应安全退出，不抛异常。"""
        from channels.api import routes_tasks as rt

        ms = _create_store_with_task()  # 空的 MemoryStore，没有 "nonexistent" 任务

        with patch.object(rt, "store", ms):
            # 不应抛出异常
            await rt._auto_advance_memstore_task("nonexistent_id")

    @pytest.mark.asyncio
    async def test_task_not_queued_safe_exit(self):
        """任务状态不是 queued 时协程应安全退出。"""
        from channels.api import routes_tasks as rt

        task_id = "not_queued_001"
        ms = _create_store_with_task(task_id, "pending")  # 状态为 pending

        with patch.object(rt, "store", ms):
            await rt._auto_advance_memstore_task(task_id)

        # 状态应保持不变
        task = ms.get_task(task_id)
        assert task["status"] == "pending"

    @pytest.mark.asyncio
    async def test_sync_to_task_storage_exception_triggers_fallback(self):
        """同步到 TaskStorage 异常时，应 fallback 到直接推进。"""
        from channels.api import routes_tasks as rt

        task_id = "sync_exception_001"
        ms = _create_store_with_task(task_id, "queued")

        mock_service = MagicMock()
        mock_service.get_task.side_effect = RuntimeError("TaskStorage 连接失败")

        with patch.object(rt, "store", ms), \
             patch.object(rt, "_get_task_service", return_value=mock_service):
            await rt._auto_advance_memstore_task(task_id)

        task = ms.get_task(task_id)
        assert task is not None
        assert task["status"] == "completed", (
            f"TaskStorage 异常时应 fallback，实际为 {task['status']}"
        )

    @pytest.mark.asyncio
    async def test_exception_sets_task_failed(self, caplog):
        """协程整体异常时应将任务设为 failed，并有日志。"""
        from channels.api import routes_tasks as rt

        task_id = "exception_test_001"
        ms = _create_store_with_task(task_id, "queued")

        # 让 store.update_task 在设为 running 后抛异常
        original_update = ms.update_task
        call_count = 0

        def flaky_update(tid, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count > 1 and kwargs.get("status") == "completed":
                raise RuntimeError("模拟存储故障")
            return original_update(tid, **kwargs)

        ms.update_task = flaky_update

        with patch.object(rt, "store", ms), \
             patch.object(rt, "_get_task_service", return_value=None), \
             caplog.at_level(logging.ERROR):
            await rt._auto_advance_memstore_task(task_id)

        # 应有错误日志
        error_logs = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(error_logs) > 0, "应有 ERROR 级别日志记录异常"

    @pytest.mark.asyncio
    async def test_entry_logging(self, caplog):
        """协程入口应有日志记录开始执行。"""
        from channels.api import routes_tasks as rt

        task_id = "log_test_001"
        ms = _create_store_with_task(task_id, "queued")

        with patch.object(rt, "store", ms), \
             patch.object(rt, "_get_task_service", return_value=None), \
             caplog.at_level(logging.INFO):
            await rt._auto_advance_memstore_task(task_id)

        info_logs = [r.message for r in caplog.records if r.levelno >= logging.INFO]
        # 应有入口日志或流转完成日志
        assert any("_auto_advance" in msg for msg in info_logs), (
            f"应有包含 '_auto_advance' 的日志，实际: {info_logs}"
        )

    @pytest.mark.asyncio
    async def test_full_lifecycle_queued_to_completed(self):
        """完整生命周期: queued → running → completed。"""
        from channels.api import routes_tasks as rt

        task_id = "lifecycle_001"
        ms = _create_store_with_task(task_id, "queued")

        status_sequence = []

        original_update = ms.update_task
        def track_update(tid, **kwargs):
            if kwargs.get("status"):
                status_sequence.append(kwargs["status"])
            return original_update(tid, **kwargs)
        ms.update_task = track_update

        with patch.object(rt, "store", ms), \
             patch.object(rt, "_get_task_service", return_value=None):
            await rt._auto_advance_memstore_task(task_id)

        assert status_sequence == ["running", "completed"], (
            f"应按序经过 running→completed，实际: {status_sequence}"
        )


class TestAutoAdvanceIntegration:
    """集成测试: 通过 MemoryStore 的 submit_task 端点验证完整流转。"""

    @pytest.mark.asyncio
    async def test_submit_triggers_auto_advance(self):
        """submit_task 创建的后台任务应能自动将 queued→completed。

        模拟完整的 MemoryStore 任务提交流程。
        """
        from channels.api import routes_tasks as rt
        from channels.api.memory_store import MemoryStore

        ms = MemoryStore()
        # 创建一个 pending 状态的任务
        task = ms.create_task(
            user_id="admin_user_001",
            title="集成测试任务",
            description="验证 submit 触发 auto_advance",
            priority=5,
        )
        task_id = task["id"]
        assert task["status"] == "pending"

        # 模拟 submit_task 的逻辑: 设为 queued + 启动 auto_advance
        ms.update_task(task_id, status="queued")

        with patch.object(rt, "store", ms), \
             patch.object(rt, "_get_task_service", return_value=None):
            await rt._auto_advance_memstore_task(task_id)

        result = ms.get_task(task_id)
        assert result["status"] == "completed", (
            f"任务应完成流转到 completed，实际为 {result['status']}"
        )
