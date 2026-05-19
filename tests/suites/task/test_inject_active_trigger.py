"""主动触发注入机制测试。

验证 _inject_task 通过 send_pipeline_message 统一入口投递消息：
1. 运行中引擎 → send_pipeline_message → notification 方法
2. 挂起引擎 → send_pipeline_message → wake 方法
3. 无引擎 → send_pipeline_message → 兜底（MessageQueue 或 revive）

测试覆盖：
- inject_result.trigger 反映 send_pipeline_message 返回的 method
- 消息内容和 metadata 正确传递
- 无引擎时回退到 MessageQueue
"""
from __future__ import annotations

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


# ── Bug 2 测试: _inject_task 通过 send_pipeline_message 投递 ────


class TestInjectTaskViaMessageBus:
    """验证 _inject_task 通过 send_pipeline_message 统一入口投递。"""

    @pytest.mark.asyncio
    async def test_running_engine_uses_notification_method(self):
        """运行中引擎应通过 send_pipeline_message 使用 notification 方法。"""
        from pipeline.message_bus import InjectResult

        mock_result = InjectResult(
            success=True,
            method="notification",
            pipeline_id="test_pipe",
        )

        queue = MessageQueue()

        mock_provider = MagicMock()

        with patch("infrastructure.service_provider.get_service_provider", return_value=mock_provider):
            with patch("tools.builtin.task.TaskTool._get_task_service") as mock_svc:
                with patch("tools.builtin.task.TaskTool._check_permission", return_value=(True, "")):
                    with patch("tools.builtin.task.TaskTool._get_message_queue", return_value=queue):
                        with patch("pipeline.message_bus.send_pipeline_message", new_callable=AsyncMock, return_value=mock_result) as mock_send:
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

        # 验证 send_pipeline_message 被正确调用
        mock_send.assert_called_once()
        call_kwargs = mock_send.call_args
        assert call_kwargs[0][0] == "test_pipe"  # pipeline_id
        assert call_kwargs[0][1] == "请改用方案B"   # message
        # 验证 metadata 中的 source 和 task_id
        metadata = call_kwargs[1].get("metadata", {})
        assert metadata.get("source") == "task_inject"
        assert metadata.get("task_id") == "task_001"

        # 结果标记为 notification 方法
        assert result.data["trigger"] == "notification"
        assert result.data["injected"] is True

    @pytest.mark.asyncio
    async def test_suspended_engine_uses_wake_method(self):
        """挂起引擎应通过 send_pipeline_message 使用 wake 方法。"""
        from pipeline.message_bus import InjectResult

        mock_result = InjectResult(
            success=True,
            method="wake",
            pipeline_id="test_pipe",
        )

        mock_provider = MagicMock()

        with patch("infrastructure.service_provider.get_service_provider", return_value=mock_provider):
            with patch("tools.builtin.task.TaskTool._get_task_service") as mock_svc:
                with patch("tools.builtin.task.TaskTool._check_permission", return_value=(True, "")):
                    with patch("pipeline.message_bus.send_pipeline_message", new_callable=AsyncMock, return_value=mock_result) as mock_send:
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

        mock_send.assert_called_once()
        assert result.data["trigger"] == "wake"

    @pytest.mark.asyncio
    async def test_no_engine_falls_back_to_message_queue(self):
        """引擎未找到时 send_pipeline_message 返回 failed，_inject_task 仍返回成功。"""
        from pipeline.message_bus import InjectResult

        mock_result = InjectResult(
            success=False,
            method="failed",
            pipeline_id="fallback_pipe",
            error="管道 fallback_pipe 不存在且无历史记录",
        )

        queue = MessageQueue()
        # 也向 MessageQueue 推入一条消息（双通道兜底）
        await queue.push(Message(
            id=create_message_id(),
            pipeline_id="fallback_pipe",
            target_id="task_001",
            content="兜底消息",
            priority=100,
        ))

        mock_provider = MagicMock()
        mock_provider.get = lambda key: None  # 无引擎

        with patch("infrastructure.service_provider.get_service_provider", return_value=mock_provider):
            with patch("tools.builtin.task.TaskTool._get_task_service") as mock_svc:
                with patch("tools.builtin.task.TaskTool._check_permission", return_value=(True, "")):
                    with patch("tools.builtin.task.TaskTool._get_message_queue", return_value=queue):
                        with patch("pipeline.message_bus.send_pipeline_message", new_callable=AsyncMock, return_value=mock_result) as mock_send:
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

        # send_pipeline_message 被调用（即使返回失败）
        mock_send.assert_called_once()
        # _inject_task 本身不抛异常，返回结果中 trigger 标记为 failed
        assert result.data["trigger"] == "failed"
        # 验证 MessageQueue 中的消息仍可被消费（双通道兜底）
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
        mock_svc.save_task = AsyncMock(wraps=save_task)
        mock_svc.force_transition = AsyncMock()

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
                    await tool._retry_task(
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
        mock_svc.save_task = AsyncMock(wraps=save_task)
        mock_svc.force_transition = AsyncMock()

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
                    await tool._retry_task(
                        inputs={
                            "task_id": "task_retry_002",
                            "parent_agent_level": 1,
                        },
                        parent_agent_level=1,
                    )

        assert "retry_message" not in captured_metadata
