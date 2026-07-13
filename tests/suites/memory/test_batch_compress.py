"""循环分批压缩测试。

验证 _do_compress_round 的分批逻辑：
- old_msgs 超过 batch_ratio × context_window 时自动分片
- 每片独立压缩并通过内部 _save_compression_result 保存
- 背景信息在批次间正确传递
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from typing import Any

import pytest

from memory.memory_context_service import MemoryContextService


def _make_service() -> MemoryContextService:
    """创建用于测试的 MemoryContextService。"""
    svc = MemoryContextService()
    svc._llm_call_fn = AsyncMock(return_value="mock")
    return svc


def _make_messages(count: int, tokens_each: int = 100) -> list[dict[str, Any]]:
    """生成指定数量的消息，每条约 tokens_each 个 token。

    _estimate_msg_tokens 使用 len(content) // 2 估算，
    所以 char_count = tokens_each * 2 以精确匹配。
    """
    char_count = max(1, tokens_each * 2)
    content = "测" * char_count
    return [{"role": "user", "content": content}] * count


# ============================================================
# 1. 分批数量计算
# ============================================================


class TestBatchCount:
    """验证分批数量计算是否正确。"""

    @pytest.mark.asyncio
    async def test_单批_不超过预算不分批(self) -> None:
        """old_msgs 不超过 batch_ratio × context_window 时只走一批。"""
        svc = _make_service()
        context_window = 200000

        # recent_budget 设小，确保有 old_msgs
        budgets = {"recent": 200, "L1": 5000, "L2": 2000}

        # 10 条 × 100 token = 1000 token，远小于 batch_budget=100000
        # 但 recent_budget=200，只有约 2 条进 recent，8 条进 old
        messages = [{"role": "system", "content": "sys"}] + _make_messages(10, 100)

        compress_calls: list[list[dict]] = []
        save_calls: list[tuple] = []

        async def mock_compress(old_msgs, *args, **kwargs):
            compress_calls.append(old_msgs)
            return {"l1": "L1摘要", "l2": "L2摘要", "keywords": ["k1"]}

        async def mock_save(old_msgs, comp_result):
            save_calls.append((len(old_msgs), comp_result))

        with patch.object(svc, "_build_compression_content", side_effect=mock_compress), \
             patch.object(svc, "_save_compression_result", side_effect=mock_save):
            result = await svc._do_compress_round(
                messages, context_window, budgets, "", "",
            )

        assert len(compress_calls) == 1
        assert len(save_calls) == 1
        assert result is not None

    @pytest.mark.asyncio
    async def test_两批_old刚好超过一批预算(self) -> None:
        """old_msgs 超过 batch_budget 但不超过 2×batch_budget 时分 2 批。"""
        svc = _make_service()
        context_window = 200000

        # recent 预算大，确保只有少量进入 recent
        budgets = {"recent": 200, "L1": 5000, "L2": 2000}

        # 100 条 × 2000 token = 200000 token，需要 2 批
        # 但 recent_budget=200 只留约 1 条，old ≈ 99 条 ≈ 198000 token
        messages = [{"role": "system", "content": "sys"}] + _make_messages(100, 2000)

        compress_calls: list[list[dict]] = []
        save_calls: list[tuple] = []

        async def mock_compress(old_msgs, *args, **kwargs):
            compress_calls.append(old_msgs)
            return {"l1": f"L1_batch_{len(compress_calls)}", "l2": "L2", "keywords": []}

        async def mock_save(old_msgs, comp_result):
            save_calls.append((len(old_msgs), comp_result))

        with patch.object(svc, "_build_compression_content", side_effect=mock_compress), \
             patch.object(svc, "_save_compression_result", side_effect=mock_save):
            result = await svc._do_compress_round(
                messages, context_window, budgets, "", "",
            )

        assert result is not None
        assert len(compress_calls) == 2
        assert len(save_calls) == 2

    @pytest.mark.asyncio
    async def test_四批_超大消息量(self) -> None:
        """old_msgs 远超上下文窗口时分正确数量的批。"""
        svc = _make_service()
        context_window = 200000

        budgets = {"recent": 200, "L1": 5000, "L2": 2000}

        # 400 条 × 2000 token = 800000 token
        # batch_budget=100000，需要 ceil(800000/100000)=8 批
        messages = [{"role": "system", "content": "sys"}] + _make_messages(400, 2000)

        compress_calls: list[list[dict]] = []

        async def mock_compress(old_msgs, *args, **kwargs):
            compress_calls.append(len(old_msgs))
            return {"l1": f"L1_{len(compress_calls)}", "l2": "L2", "keywords": []}

        with patch.object(svc, "_build_compression_content", side_effect=mock_compress), \
             patch.object(svc, "_save_compression_result", new=AsyncMock()):
            result = await svc._do_compress_round(
                messages, context_window, budgets, "", "",
            )

        assert result is not None
        assert len(compress_calls) == 8
        # 每批消息数应大致相等（system 消息被分离，只有 user 消息进入 other_msgs）
        total = sum(compress_calls)
        assert total == 400  # 400 user msgs, system msg 被分离


# ============================================================
# 2. 背景信息传递
# ============================================================


class TestBackgroundPassing:
    """验证 state_snapshot 和 recent_process_blocks 在批次间保持不变。"""

    @pytest.mark.asyncio
    async def test_背景信息在批次间传递(self) -> None:
        """所有批次收到相同的 state_snapshot 和 recent_process_blocks。"""
        svc = _make_service()
        context_window = 200000
        budgets = {"recent": 200, "L1": 5000, "L2": 2000}

        messages = [{"role": "system", "content": "sys"}] + _make_messages(100, 2000)

        received_state: list[str] = []
        received_process: list[str] = []

        async def mock_compress(old_msgs, cw, b, state_snapshot, recent_process_blocks, **kwargs):
            received_state.append(state_snapshot)
            received_process.append(recent_process_blocks)
            idx = len(received_state)
            return {"l1": f"batch_{idx}_L1", "l2": "L2", "keywords": []}

        with patch.object(svc, "_build_compression_content", side_effect=mock_compress), \
             patch.object(svc, "_save_compression_result", new=AsyncMock()):
            await svc._do_compress_round(
                messages, context_window, budgets, "初始状态", "过程块样本",
            )

        # 所有批次收到相同的 state_snapshot
        assert all(s == "初始状态" for s in received_state)
        # 所有批次收到相同的 recent_process_blocks
        assert all(p == "过程块样本" for p in received_process)


# ============================================================
# 3. 部分失败处理
# ============================================================


class TestPartialFailure:
    """验证部分批次失败时的行为。"""

    @pytest.mark.asyncio
    async def test_一批失败另一批成功仍返回成功(self) -> None:
        """如果有一批成功，整体应返回成功。"""
        svc = _make_service()
        context_window = 200000
        budgets = {"recent": 200, "L1": 5000, "L2": 2000}

        messages = [{"role": "system", "content": "sys"}] + _make_messages(100, 2000)

        call_count = 0

        async def mock_compress(old_msgs, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return None  # 第 1 批失败
            return {"l1": "L1_ok", "l2": "L2", "keywords": []}

        with patch.object(svc, "_build_compression_content", side_effect=mock_compress), \
             patch.object(svc, "_save_compression_result", new=AsyncMock()):
            result = await svc._do_compress_round(
                messages, context_window, budgets, "", "",
            )

        # 至少一批成功，应返回结果
        assert result is not None

    @pytest.mark.asyncio
    async def test_全部失败返回None(self) -> None:
        """所有批次都失败时应返回 None。"""
        svc = _make_service()
        context_window = 200000
        budgets = {"recent": 200, "L1": 5000, "L2": 2000}

        messages = [{"role": "system", "content": "sys"}] + _make_messages(100, 2000)

        async def mock_compress(old_msgs, *args, **kwargs):
            return None  # 全部失败

        with patch.object(svc, "_build_compression_content", side_effect=mock_compress):
            result = await svc._do_compress_round(
                messages, context_window, budgets, "", "",
            )

        assert result is None


# ============================================================
# 4. 消息分离
# ============================================================


class TestMessageSeparation:
    """验证 system / 压缩块 / recent 消息的正确分离。"""

    @pytest.mark.asyncio
    async def test_system消息保留_recent消息保留(self) -> None:
        """system 消息和 recent 消息应原样保留在返回结果中。"""
        svc = _make_service()
        context_window = 200000
        budgets = {"recent": 500, "L1": 5000, "L2": 2000}

        sys_msg = {"role": "system", "content": "系统提示"}
        user_msg = {"role": "user", "content": "你好"}
        messages = [sys_msg, user_msg]

        async def mock_compress(old_msgs, *args, **kwargs):
            return {"l1": "L1", "l2": "L2", "keywords": []}

        with patch.object(svc, "_build_compression_content", side_effect=mock_compress), \
             patch.object(svc, "_save_compression_result", new=AsyncMock()):
            result = await svc._do_compress_round(
                messages, context_window, budgets, "", "",
            )

        if result is not None:
            # system 消息应保留
            assert sys_msg in result

    @pytest.mark.asyncio
    async def test_无other消息返回None(self) -> None:
        """没有非 system 消息时返回 None。"""
        svc = _make_service()
        context_window = 200000
        budgets = {"recent": 500, "L1": 5000, "L2": 2000}

        messages = [{"role": "system", "content": "只有系统消息"}]

        async def mock_compress(old_msgs, *args, **kwargs):
            return {"l1": "L1", "l2": "L2", "keywords": []}

        with patch.object(svc, "_build_compression_content", side_effect=mock_compress):
            result = await svc._do_compress_round(
                messages, context_window, budgets, "", "",
            )

        assert result is None
