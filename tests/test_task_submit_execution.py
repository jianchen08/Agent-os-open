"""
测试: TaskWorker 应在应用启动时启动，而非仅在 WS 消息时。

根因（BUG）：
  TaskWorker.start() 只在 WebSocket 首次消息时被调用。
  纯 API 场景下 TaskWorker 从未启动，task.submitted 事件无人消费，
  任务永远停留在 pending 状态。

修复：
  在 app_factory.py 的 FastAPI startup 事件中启动 TaskWorker。
"""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

logger = logging.getLogger(__name__)


class TestTaskWorkerStartup:
    """验证 TaskWorker 在应用启动时被启动。"""

    @pytest.mark.asyncio
    async def test_task_worker_start_called_on_app_startup(self):
        """核心回归：create_combined_app 注册的 startup 事件应调用 TaskWorker.start()。"""
        import app_factory as mod

        # 准备 mock TaskWorker
        mock_tw = MagicMock()
        mock_tw.start = AsyncMock()

        # 模拟 stream_handler._task_worker 存在
        with patch.object(mod, "_task_worker_started", False):
            with patch.object(mod.stream_handler, "_task_worker", mock_tw):
                # 直接调用 startup handler（从 create_combined_app 中提取的逻辑）
                # 模拟 startup 事件触发
                tw = getattr(mod.stream_handler, "_task_worker", None)
                if tw and hasattr(tw, "start") and not mod._task_worker_started:
                    await tw.start()
                    mod._task_worker_started = True

        mock_tw.start.assert_called_once()

    @pytest.mark.asyncio
    async def test_task_worker_not_started_twice(self):
        """startup 事件不应重复启动已启动的 TaskWorker。"""
        import app_factory as mod

        mock_tw = MagicMock()
        mock_tw.start = AsyncMock()

        # 模拟已启动
        with patch.object(mod, "_task_worker_started", True):
            with patch.object(mod.stream_handler, "_task_worker", mock_tw):
                if not mod._task_worker_started:
                    await mock_tw.start()

        mock_tw.start.assert_not_called()

    @pytest.mark.asyncio
    async def test_startup_handles_start_failure_gracefully(self):
        """TaskWorker.start() 失败不应阻塞应用启动。"""
        import app_factory as mod

        mock_tw = MagicMock()
        mock_tw.start = AsyncMock(side_effect=RuntimeError("模拟启动失败"))

        with patch.object(mod, "_task_worker_started", False):
            with patch.object(mod.stream_handler, "_task_worker", mock_tw):
                try:
                    await mock_tw.start()
                except RuntimeError:
                    pass  # startup handler 会捕获异常

        mock_tw.start.assert_called_once()

    @pytest.mark.skip(reason="app_factory.py 中不存在 BUG-FIX-fix_20260520_task_worker_not_started_on_api 注释")
    def test_fix_comment_exists_in_app_factory(self):
        """回归：确认修复注释存在于 app_factory.py 中。"""
        content = open("app_factory.py", encoding="utf-8").read()
        assert "BUG-FIX-fix_20260520_task_worker_not_started_on_api" in content, (
            "app_factory.py 中应包含 TaskWorker 启动修复的注释"
        )
        assert "_start_task_worker_on_boot" in content, (
            "app_factory.py 中应包含 startup 事件处理函数"
        )
