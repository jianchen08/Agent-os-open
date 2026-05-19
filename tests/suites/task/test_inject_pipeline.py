"""inject 消息注入链路集成测试。

验证完整的 inject 链路：
  TaskTool.inject → MessageQueue.push → MessageInjectPlugin.pop → messages 注入

测试覆盖：
1. MessageQueue 按 pipeline_id 隔离（定向投递）
2. TaskTool.inject 正确投递消息到目标管道
3. MessageInjectPlugin 正确消费消息并注入到 messages
4. 端到端：inject → queue → plugin → messages 全链路畅通
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from infrastructure.message_queue import Message, MessageQueue, create_message_id
from plugins.input.message_inject import MessageInjectPlugin
from pipeline.plugin import PluginContext, PluginResult


# ── 辅助 ──────────────────────────────────────────────


def _make_plugin_context(
    pipeline_id: str = "pipe_001",
    messages: list[dict[str, str]] | None = None,
    services: dict[str, Any] | None = None,
) -> PluginContext:
    """构造测试用的 PluginContext。"""
    ctx = MagicMock(spec=PluginContext)
    state = {
        "pipeline_id": pipeline_id,
        "messages": messages or [],
    }
    ctx.state = state
    ctx.get_service = lambda key: (services or {})[key]
    return ctx


# ── MessageQueue 测试 ──────────────────────────────────


class TestMessageQueuePipelineIsolation:
    """验证 MessageQueue 按 pipeline_id 隔离消息。"""

    @pytest.mark.asyncio
    async def test_push_and_pop_same_pipeline(self):
        """同一 pipeline_id：push 后能 pop 到。"""
        queue = MessageQueue()
        msg = Message(
            id=create_message_id(),
            pipeline_id="pipe_A",
            target_id="task_001",
            content="调整方向",
            priority=100,
        )
        await queue.push(msg)
        popped = await queue.pop("pipe_A")
        assert popped is not None
        assert popped.content == "调整方向"
        assert popped.pipeline_id == "pipe_A"

    @pytest.mark.asyncio
    async def test_different_pipeline_isolation(self):
        """不同 pipeline_id：push 到 A 后，从 B pop 不到。"""
        queue = MessageQueue()
        msg_a = Message(
            id=create_message_id(),
            pipeline_id="pipe_A",
            target_id="task_001",
            content="给A的消息",
            priority=100,
        )
        await queue.push(msg_a)

        popped_b = await queue.pop("pipe_B")
        assert popped_b is None

        popped_a = await queue.pop("pipe_A")
        assert popped_a is not None
        assert popped_a.content == "给A的消息"

    @pytest.mark.asyncio
    async def test_multiple_pipelines_independent(self):
        """多个管道同时存在消息，互不干扰。"""
        queue = MessageQueue()
        for pid, content in [("pipe_A", "A消息"), ("pipe_B", "B消息"), ("pipe_C", "C消息")]:
            await queue.push(Message(
                id=create_message_id(),
                pipeline_id=pid,
                target_id=f"task_{pid}",
                content=content,
                priority=100,
            ))

        assert (await queue.pop("pipe_A")).content == "A消息"
        assert (await queue.pop("pipe_B")).content == "B消息"
        assert (await queue.pop("pipe_C")).content == "C消息"

        assert await queue.pop("pipe_A") is None
        assert await queue.pop("pipe_B") is None
        assert await queue.pop("pipe_C") is None

    @pytest.mark.asyncio
    async def test_priority_ordering(self):
        """同一 pipeline_id 内，高优先级先出。"""
        queue = MessageQueue()
        for pri, content in [(10, "低优先"), (100, "高优先"), (50, "中优先")]:
            await queue.push(Message(
                id=create_message_id(),
                pipeline_id="pipe_A",
                target_id="task_001",
                content=content,
                priority=pri,
            ))

        first = await queue.pop("pipe_A")
        assert first.content == "高优先"
        second = await queue.pop("pipe_A")
        assert second.content == "中优先"
        third = await queue.pop("pipe_A")
        assert third.content == "低优先"

    @pytest.mark.asyncio
    async def test_pop_consumes_message(self):
        """pop 后消息被消费，再次 pop 返回 None。"""
        queue = MessageQueue()
        await queue.push(Message(
            id=create_message_id(),
            pipeline_id="pipe_A",
            target_id="task_001",
            content="一次性消息",
            priority=100,
        ))
        assert (await queue.pop("pipe_A")) is not None
        assert await queue.pop("pipe_A") is None


# ── MessageInjectPlugin 测试 ────────────────────────────


class TestMessageInjectPlugin:
    """验证 MessageInjectPlugin 消费消息并注入 messages。"""

    @pytest.mark.asyncio
    async def test_inject_message_to_front(self):
        """消息注入到 messages 列表前部。"""
        queue = MessageQueue()
        await queue.push(Message(
            id=create_message_id(),
            pipeline_id="pipe_001",
            target_id="task_001",
            content="请改用方案B",
            priority=100,
        ))

        plugin = MessageInjectPlugin()
        ctx = _make_plugin_context(
            pipeline_id="pipe_001",
            messages=[{"role": "user", "content": "原始消息"}],
            services={"message_queue": queue},
        )

        result = await plugin.execute(ctx)
        assert isinstance(result, PluginResult)

        updated_messages = result.state_updates["messages"]
        assert len(updated_messages) == 2
        assert updated_messages[0] == {"role": "user", "content": "请改用方案B"}
        assert updated_messages[1] == {"role": "user", "content": "原始消息"}

    @pytest.mark.asyncio
    async def test_no_message_in_queue(self):
        """队列为空时不注入任何内容。"""
        queue = MessageQueue()
        plugin = MessageInjectPlugin()
        ctx = _make_plugin_context(
            pipeline_id="pipe_001",
            services={"message_queue": queue},
        )

        result = await plugin.execute(ctx)
        assert result.state_updates == {}

    @pytest.mark.asyncio
    async def test_no_pipeline_id_in_state(self):
        """state 中没有 pipeline_id 时不注入。"""
        queue = MessageQueue()
        plugin = MessageInjectPlugin()
        ctx = _make_plugin_context(
            pipeline_id="",
            services={"message_queue": queue},
        )

        result = await plugin.execute(ctx)
        assert result.state_updates == {}

    @pytest.mark.asyncio
    async def test_no_message_queue_service(self):
        """没有 message_queue 服务时不注入（降级）。"""
        plugin = MessageInjectPlugin()
        ctx = _make_plugin_context(pipeline_id="pipe_001")
        ctx.get_service = lambda key: (_ for _ in ()).throw(KeyError(key))

        result = await plugin.execute(ctx)
        assert result.state_updates == {}

    @pytest.mark.asyncio
    async def test_message_consumed_after_inject(self):
        """注入后消息从队列中移除，不会重复注入。"""
        queue = MessageQueue()
        await queue.push(Message(
            id=create_message_id(),
            pipeline_id="pipe_001",
            target_id="task_001",
            content="一次性指令",
            priority=100,
        ))

        plugin = MessageInjectPlugin()
        ctx1 = _make_plugin_context(
            pipeline_id="pipe_001",
            services={"message_queue": queue},
        )
        result1 = await plugin.execute(ctx1)
        assert "messages" in result1.state_updates

        ctx2 = _make_plugin_context(
            pipeline_id="pipe_001",
            services={"message_queue": queue},
        )
        result2 = await plugin.execute(ctx2)
        assert result2.state_updates == {}


# ── 端到端链路测试 ──────────────────────────────────────


class TestInjectEndToEnd:
    """端到端测试：inject → queue → plugin → messages 全链路。"""

    @pytest.mark.asyncio
    async def test_full_inject_pipeline(self):
        """模拟完整的 inject 链路。

        步骤：
        1. 创建共享的 MessageQueue
        2. 模拟 inject 生产端：创建 Message 并 push 到目标 pipeline_id
        3. 模拟消费端：MessageInjectPlugin 从同一 pipeline_id 取消息
        4. 验证消息正确注入到 messages 前部
        """
        shared_queue = MessageQueue()

        target_pipeline_id = "abc123def456"

        # Step 1: 模拟 TaskTool.inject 生产端
        inject_msg = Message(
            id=create_message_id(),
            pipeline_id=target_pipeline_id,
            target_id="task_001",
            content="注意：需求有变更，请改用方案B",
            priority=100,
            metadata={
                "source": "task_inject",
                "injected_by": "L1",
                "task_id": "task_001",
            },
        )
        success = await shared_queue.push(inject_msg)
        assert success is True

        # 验证其他管道取不到
        other_msg = await shared_queue.pop("wrong_pipeline")
        assert other_msg is None

        # Step 2: 模拟 MessageInjectPlugin 消费端
        plugin = MessageInjectPlugin()
        ctx = _make_plugin_context(
            pipeline_id=target_pipeline_id,
            messages=[{"role": "system", "content": "系统提示"}, {"role": "user", "content": "原始任务"}],
            services={"message_queue": shared_queue},
        )

        result = await plugin.execute(ctx)
        assert isinstance(result, PluginResult)

        updated = result.state_updates["messages"]
        assert len(updated) == 3
        assert updated[0] == {"role": "user", "content": "注意：需求有变更，请改用方案B"}
        assert updated[1] == {"role": "system", "content": "系统提示"}
        assert updated[2] == {"role": "user", "content": "原始任务"}

        # Step 3: 验证消息已被消费
        assert await shared_queue.size(target_pipeline_id) == 0

    @pytest.mark.asyncio
    async def test_multi_pipeline_concurrent_inject(self):
        """多管道并发注入：3 个子任务同时收到不同指令，互不干扰。"""
        shared_queue = MessageQueue()

        # 给 3 个管道各发一条消息
        pipelines = {
            "pipe_A": "请优先处理登录模块",
            "pipe_B": "数据库改用 PostgreSQL",
            "pipe_C": "前端需要适配移动端",
        }
        for pid, content in pipelines.items():
            await shared_queue.push(Message(
                id=create_message_id(),
                pipeline_id=pid,
                target_id=f"task_{pid}",
                content=content,
                priority=100,
            ))

        plugin = MessageInjectPlugin()

        # 各管道各自消费，互不干扰
        for pid, expected_content in pipelines.items():
            ctx = _make_plugin_context(
                pipeline_id=pid,
                messages=[],
                services={"message_queue": shared_queue},
            )
            result = await plugin.execute(ctx)
            assert "messages" in result.state_updates
            injected_msg = result.state_updates["messages"][0]
            assert injected_msg["role"] == "user"
            assert injected_msg["content"] == expected_content

        # 所有消息都被消费
        stats = await shared_queue.get_statistics()
        assert stats["total_messages"] == 0
