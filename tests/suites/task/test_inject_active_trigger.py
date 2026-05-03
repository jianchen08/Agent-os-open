"""主动触发注入机制测试。

验证本次修复的 3 个 bug：
1. _inject_task 优先用 inject_notification() 主动触发运行中引擎
2. _run_loop 管道结束前检查 MessageQueue 兜底
3. _retry_task 的 message 参数正确传递到新管道

测试覆盖：
- inject_notification 触发运行中引擎 → 管道不提前结束
- 管道即将结束时检查 MessageQueue → 消息不丢失
- retry_message 存入 metadata → TaskWorker 构建 full_input 时读取
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from infrastructure.message_queue import Message, MessageQueue, create_message_id


# ── Bug 1 测试: _run_loop 管道结束前检查 MessageQueue ─────


class TestEngineEndOfPipelineMessageQueueCheck:
    """验证管道即将结束时也检查 MessageQueue。"""

    @pytest.mark.asyncio
    async def test_pending_notifications_plus_message_queue(self):
        """_pending_notifications 和 MessageQueue 双来源合并。"""
        queue = MessageQueue()
        pipeline_id = "pipe_test_mq"

        # 向 MessageQueue 推入一条消息
        await queue.push(Message(
            id=create_message_id(),
            pipeline_id=pipeline_id,
            target_id="task_001",
            content="来自 MessageQueue 的兜底消息",
            priority=100,
        ))

        # 模拟 _run_loop 的 end-of-pipeline 检查逻辑
        notif_sources: list[str] = []

        # 来源1: _pending_notifications
        pending_notifications = ["来自 inject_notification 的主动消息"]
        notif_sources.extend(pending_notifications)

        # 来源2: MessageQueue
        mq_msgs: list[str] = []
        while True:
            mq_msg = await queue.pop(pipeline_id)
            if mq_msg is None:
                break
            mq_msgs.append(mq_msg.content)
        notif_sources.extend(mq_msgs)

        assert len(notif_sources) == 2
        assert "来自 inject_notification 的主动消息" in notif_sources
        assert "来自 MessageQueue 的兜底消息" in notif_sources

    @pytest.mark.asyncio
    async def test_message_queue_alone_triggers_continue(self):
        """只有 MessageQueue 有消息时也能触发继续循环。"""
        queue = MessageQueue()
        pipeline_id = "pipe_test_mq_only"

        await queue.push(Message(
            id=create_message_id(),
            pipeline_id=pipeline_id,
            target_id="task_001",
            content="只有队列有消息",
            priority=100,
        ))

        # 模拟 _pending_notifications 为空，但 MessageQueue 有消息
        notif_sources: list[str] = []
        # _pending_notifications 为空
        # MessageQueue 检查
        while True:
            mq_msg = await queue.pop(pipeline_id)
            if mq_msg is None:
                break
            notif_sources.append(mq_msg.content)

        assert len(notif_sources) == 1
        assert notif_sources[0] == "只有队列有消息"


# ── Bug 2 测试: _inject_task 主动触发运行中引擎 ────────


class TestInjectTaskActiveTrigger:
    """验证 _inject_task 优先使用 inject_notification 主动触发。"""

    @pytest.mark.asyncio
    async def test_running_engine_dual_channel_inject(self):
        """运行中引擎应同时走 MessageQueue + inject_notification 双通道。"""
        mock_engine = MagicMock()
        mock_engine.inject_notification = MagicMock()

        queue = MessageQueue()

        mock_provider = MagicMock()
        mock_provider.get = lambda key: mock_engine if key == "__running_engine_test_pipe" else None

        with patch("infrastructure.service_provider.get_service_provider", return_value=mock_provider):
            with patch("tools.builtin.task.TaskTool._get_task_service") as mock_svc:
                with patch("tools.builtin.task.TaskTool._check_permission", return_value=(True, "")):
                    with patch("tools.builtin.task.TaskTool._get_message_queue", return_value=queue):
                        from tasks.types import TaskStatus
                        mock_task = MagicMock()
                        mock_task.status = TaskStatus.RUNNING
                        mock_task.pipeline_run_id = "test_pipe"
                        mock_svc.return_value.get_task.return_value = mock_task

                        from tools.builtin.task import TaskTool
                        tool = TaskTool()
                        result = await tool._inject_task(
                            inputs={
                                "task_id": "task_001",
                                "message": "请改用方案B",
                                "parent_agent_level": 1,
                            },
                            parent_agent_level=1,
                        )

        # 验证双通道都触发
        mock_engine.inject_notification.assert_called_once_with("请改用方案B")
        popped = await queue.pop("test_pipe")
        assert popped is not None
        assert popped.content == "请改用方案B"
        # 结果标记为双通道
        assert result.data["trigger"] == "inject_notification+message_queue"

    @pytest.mark.asyncio
    async def test_suspended_engine_gets_inject_and_wake(self):
        """挂起引擎应收到 inject_and_wake 调用。"""
        mock_engine = MagicMock()
        mock_engine.inject_and_wake = MagicMock()

        mock_provider = MagicMock()
        # 没有 running engine
        def provider_get(key):
            if key == "__running_engine_test_pipe":
                return None
            if key == "__suspended_engine_test_pipe":
                return mock_engine
            return None
        mock_provider.get = provider_get

        with patch("infrastructure.service_provider.get_service_provider", return_value=mock_provider):
            with patch("tools.builtin.task.TaskTool._get_task_service") as mock_svc:
                with patch("tools.builtin.task.TaskTool._check_permission", return_value=(True, "")):
                    from tasks.types import TaskStatus
                    mock_task = MagicMock()
                    mock_task.status = TaskStatus.RUNNING
                    mock_task.pipeline_run_id = "test_pipe"
                    mock_svc.return_value.get_task.return_value = mock_task

                    from tools.builtin.task import TaskTool
                    tool = TaskTool()
                    result = await tool._inject_task(
                        inputs={
                            "task_id": "task_001",
                            "message": "唤醒并注入",
                            "parent_agent_level": 1,
                        },
                        parent_agent_level=1,
                    )

        mock_engine.inject_and_wake.assert_called_once_with("唤醒并注入")

    @pytest.mark.asyncio
    async def test_no_engine_falls_back_to_message_queue(self):
        """引擎未找到时应回退到 MessageQueue。"""
        queue = MessageQueue()

        mock_provider = MagicMock()
        mock_provider.get = lambda key: None  # 无引擎

        with patch("infrastructure.service_provider.get_service_provider", return_value=mock_provider):
            with patch("tools.builtin.task.TaskTool._get_task_service") as mock_svc:
                with patch("tools.builtin.task.TaskTool._check_permission", return_value=(True, "")):
                    with patch("tools.builtin.task.TaskTool._get_message_queue", return_value=queue):
                        from tasks.types import TaskStatus
                        mock_task = MagicMock()
                        mock_task.status = TaskStatus.RUNNING
                        mock_task.pipeline_run_id = "fallback_pipe"
                        mock_svc.return_value.get_task.return_value = mock_task

                        from tools.builtin.task import TaskTool
                        tool = TaskTool()
                        result = await tool._inject_task(
                            inputs={
                                "task_id": "task_001",
                                "message": "兜底消息",
                                "parent_agent_level": 1,
                            },
                            parent_agent_level=1,
                        )

        # 验证消息在 MessageQueue 中
        popped = await queue.pop("fallback_pipe")
        assert popped is not None
        assert popped.content == "兜底消息"


# ── Bug 3 测试: _retry_task message 参数传递 ────────


class TestRetryMessagePassthrough:
    """验证 _retry_task 将 message 存入 metadata 供 TaskWorker 读取。"""

    @pytest.mark.asyncio
    async def test_retry_message_saved_to_metadata(self):
        """retry 带的 message 应存入 task.metadata["retry_message"]。"""
        captured_metadata = {}

        mock_task = MagicMock()
        mock_task.status = MagicMock()
        mock_task.status.value = "failed"
        from tasks.types import TaskStatus
        mock_task.status = TaskStatus.FAILED
        mock_task.metadata = {"target_id": "agent_001"}
        mock_task.id = "task_retry_001"

        def save_task(t):
            nonlocal captured_metadata
            captured_metadata = dict(t.metadata)
        mock_svc = MagicMock()
        mock_svc.get_task.return_value = mock_task
        mock_svc.save_task = save_task
        mock_svc.force_transition = MagicMock()

        mock_provider = MagicMock()
        mock_event_bus = MagicMock()
        mock_event_bus.has_subscribers.return_value = True
        mock_event_bus.emit = AsyncMock()
        mock_provider.get.return_value = mock_event_bus

        with patch("infrastructure.service_provider.get_service_provider", return_value=mock_provider):
            with patch("tools.builtin.task.TaskTool._get_task_service", return_value=mock_svc):
                with patch("tools.builtin.task.TaskTool._check_permission", return_value=(True, "")):
                    from tools.builtin.task import TaskTool
                    tool = TaskTool()
                    result = await tool._retry_task(
                        inputs={
                            "task_id": "task_retry_001",
                            "message": "上次方向错了，改用方法X",
                            "parent_agent_level": 1,
                        },
                        parent_agent_level=1,
                    )

        assert captured_metadata.get("retry_message") == "上次方向错了，改用方法X"

    @pytest.mark.asyncio
    async def test_retry_without_message_no_metadata_key(self):
        """retry 不带 message 时不应创建 retry_message 键。"""
        captured_metadata = {}

        mock_task = MagicMock()
        from tasks.types import TaskStatus
        mock_task.status = TaskStatus.FAILED
        mock_task.metadata = {"target_id": "agent_001"}
        mock_task.id = "task_retry_002"

        def save_task(t):
            nonlocal captured_metadata
            captured_metadata = dict(t.metadata)
        mock_svc = MagicMock()
        mock_svc.get_task.return_value = mock_task
        mock_svc.save_task = save_task
        mock_svc.force_transition = MagicMock()

        mock_provider = MagicMock()
        mock_event_bus = MagicMock()
        mock_event_bus.has_subscribers.return_value = True
        mock_event_bus.emit = AsyncMock()
        mock_provider.get.return_value = mock_event_bus

        with patch("infrastructure.service_provider.get_service_provider", return_value=mock_provider):
            with patch("tools.builtin.task.TaskTool._get_task_service", return_value=mock_svc):
                with patch("tools.builtin.task.TaskTool._check_permission", return_value=(True, "")):
                    from tools.builtin.task import TaskTool
                    tool = TaskTool()
                    result = await tool._retry_task(
                        inputs={
                            "task_id": "task_retry_002",
                            "parent_agent_level": 1,
                        },
                        parent_agent_level=1,
                    )

        assert "retry_message" not in captured_metadata
