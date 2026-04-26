"""MemoryContextService 记忆上下文服务测试。

测试 MemoryContextService 的构造函数、消息添加、上下文提示词生成、
统计信息、清空记忆以及内部 token 计算和预算计算。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from memory.context_compressor import CompressionConfig
from memory.memory_context_service import MemoryContextService


# ============================================================
# 辅助
# ============================================================


def _make_compressor() -> MagicMock:
    """创建 mock ContextCompressor。"""
    compressor = MagicMock()
    compressor.progressive_compress = AsyncMock(return_value=("L1内容", "L2内容"))
    return compressor


# ============================================================
# 1. 构造函数测试
# ============================================================


class TestMemoryContextServiceInit:
    """测试 MemoryContextService 初始化。"""

    def test_无配置时使用默认值(self) -> None:
        """不传配置时应使用默认值。"""
        svc = MemoryContextService()
        assert svc._config["context_window"] == 128000
        assert svc._config["compress_trigger_ratio"] == 0.5

    def test_有配置时覆盖默认值(self) -> None:
        """传入配置应覆盖默认值。"""
        config = {"context_window": 50000, "compress_trigger_ratio": 0.7}
        svc = MemoryContextService(config=config)
        assert svc._config["context_window"] == 50000
        assert svc._config["compress_trigger_ratio"] == 0.7

    def test_配置校验_缺少context_window(self) -> None:
        """配置缺少 context_window 时应抛 KeyError。"""
        with pytest.raises(KeyError, match="context_window"):
            MemoryContextService(config={"compress_trigger_ratio": 0.5})

    def test_配置校验_缺少compress_trigger_ratio(self) -> None:
        """配置缺少 compress_trigger_ratio 时应抛 KeyError。"""
        with pytest.raises(KeyError, match="compress_trigger_ratio"):
            MemoryContextService(config={"context_window": 128000})

    def test_注入compressor(self) -> None:
        """注入自定义压缩器。"""
        compressor = _make_compressor()
        svc = MemoryContextService(compressor=compressor)
        assert svc._compressor is compressor

    def test_注入token估算函数(self) -> None:
        """注入自定义 token 估算函数。"""
        fn = lambda text: len(text)
        svc = MemoryContextService(token_estimate_fn=fn)
        assert svc._token_estimate_fn is fn


# ============================================================
# 2. add_message 测试
# ============================================================


class TestAddMessage:
    """测试消息添加。"""

    @pytest.mark.asyncio
    async def test_追加消息到L0(self) -> None:
        """消息应追加到 L0。"""
        svc = MemoryContextService()
        msg = {"role": "user", "content": "你好"}
        await svc.add_message("session-1", msg)
        data = svc._get_session_data("session-1")
        assert len(data["L0"]) == 1
        assert data["L0"][0] == msg

    @pytest.mark.asyncio
    async def test_多条消息追加(self) -> None:
        """多条消息应依次追加。"""
        svc = MemoryContextService()
        for i in range(5):
            await svc.add_message("s1", {"role": "user", "content": f"消息{i}"})
        data = svc._get_session_data("s1")
        assert len(data["L0"]) == 5

    @pytest.mark.asyncio
    async def test_不同会话隔离(self) -> None:
        """不同会话的消息应隔离。"""
        svc = MemoryContextService()
        await svc.add_message("s1", {"role": "user", "content": "会话1"})
        await svc.add_message("s2", {"role": "user", "content": "会话2"})
        data1 = svc._get_session_data("s1")
        data2 = svc._get_session_data("s2")
        assert len(data1["L0"]) == 1
        assert len(data2["L0"]) == 1
        assert data1["L0"][0]["content"] == "会话1"

    @pytest.mark.asyncio
    async def test_超过阈值触发压缩(self) -> None:
        """token 数超过阈值时应触发压缩。"""
        compressor = _make_compressor()
        llm_fn = AsyncMock(return_value="压缩摘要")
        # 使用很小的 context_window 和触发比例
        config = {"context_window": 100, "compress_trigger_ratio": 0.5}
        svc = MemoryContextService(compressor=compressor, config=config, llm_call_fn=llm_fn)
        # 添加足够长的消息以触发压缩
        for i in range(10):
            await svc.add_message("s1", {"role": "user", "content": "这是一段很长的消息内容用于触发压缩" * 5})
        compressor.progressive_compress.assert_called()

    @pytest.mark.asyncio
    async def test_压缩后L0清空(self) -> None:
        """压缩后 L0 消息应被清空。"""
        compressor = _make_compressor()
        llm_fn = AsyncMock(return_value="压缩摘要")
        config = {"context_window": 100, "compress_trigger_ratio": 0.1}
        svc = MemoryContextService(compressor=compressor, config=config, llm_call_fn=llm_fn)
        await svc.add_message("s1", {"role": "user", "content": "很长的消息" * 50})
        data = svc._get_session_data("s1")
        assert len(data["L0"]) == 0
        assert data["L1"] == "L1内容"
        assert data["L2"] == "L2内容"


# ============================================================
# 3. get_context_prompt 测试
# ============================================================


class TestGetContextPrompt:
    """测试上下文提示词生成。"""

    @pytest.mark.asyncio
    async def test_空会话返回空字符串(self) -> None:
        """空会话应返回空字符串。"""
        svc = MemoryContextService()
        prompt = await svc.get_context_prompt("s1")
        assert prompt == ""

    @pytest.mark.asyncio
    async def test_只有L0消息(self) -> None:
        """只有 L0 消息时应包含消息内容。"""
        svc = MemoryContextService()
        await svc.add_message("s1", {"role": "user", "content": "你好"})
        prompt = await svc.get_context_prompt("s1")
        assert "你好" in prompt

    @pytest.mark.asyncio
    async def test_L1和L2拼接(self) -> None:
        """有 L1 和 L2 时应正确拼接。"""
        svc = MemoryContextService()
        data = svc._get_session_data("s1")
        data["L1"] = "这是L1摘要"
        data["L2"] = "这是L2三元组"
        prompt = await svc.get_context_prompt("s1")
        assert "历史摘要" in prompt
        assert "详细历史" in prompt
        assert "L2三元组" in prompt
        assert "L1摘要" in prompt

    @pytest.mark.asyncio
    async def test_三层全部拼接(self) -> None:
        """L0 + L1 + L2 全部存在时应全部拼接。"""
        svc = MemoryContextService()
        data = svc._get_session_data("s1")
        data["L1"] = "L1内容"
        data["L2"] = "L2内容"
        data["L0"] = [{"role": "user", "content": "最新消息"}]
        prompt = await svc.get_context_prompt("s1")
        assert "L2内容" in prompt
        assert "L1内容" in prompt
        assert "最新消息" in prompt


# ============================================================
# 4. get_memory_stats 测试
# ============================================================


class TestGetMemoryStats:
    """测试记忆统计信息。"""

    @pytest.mark.asyncio
    async def test_空会话统计(self) -> None:
        """空会话统计。"""
        svc = MemoryContextService()
        stats = await svc.get_memory_stats("s1")
        assert stats["session_id"] == "s1"
        assert stats["total_tokens"] == 0
        assert stats["layers"]["L0"]["messages_count"] == 0
        assert stats["layers"]["L1"]["tokens"] == 0
        assert stats["layers"]["L2"]["tokens"] == 0

    @pytest.mark.asyncio
    async def test_有消息时统计(self) -> None:
        """有消息时统计应正确计算。"""
        svc = MemoryContextService()
        await svc.add_message("s1", {"role": "user", "content": "你好世界"})
        stats = await svc.get_memory_stats("s1")
        assert stats["total_tokens"] > 0
        assert stats["layers"]["L0"]["messages_count"] == 1
        assert stats["layers"]["L0"]["tokens"] > 0

    @pytest.mark.asyncio
    async def test_usage_ratio计算(self) -> None:
        """usage_ratio 应正确计算。"""
        svc = MemoryContextService(config={"context_window": 1000, "compress_trigger_ratio": 0.5})
        await svc.add_message("s1", {"role": "user", "content": "测试"})
        stats = await svc.get_memory_stats("s1")
        assert 0 <= stats["usage_ratio"] <= 1.0

    @pytest.mark.asyncio
    async def test_parent_record_id(self) -> None:
        """parent_record_id 应正确传递。"""
        svc = MemoryContextService()
        stats = await svc.get_memory_stats("s1", parent_record_id="pr-1")
        assert stats["parent_record_id"] == "pr-1"

    @pytest.mark.asyncio
    async def test_默认parent_record_id(self) -> None:
        """无 parent_record_id 时应使用实例属性。"""
        svc = MemoryContextService()
        svc.parent_record_id = "default-pr"
        stats = await svc.get_memory_stats("s1")
        assert stats["parent_record_id"] == "default-pr"


# ============================================================
# 5. clear_memory 测试
# ============================================================


class TestClearMemory:
    """测试清空记忆。"""

    @pytest.mark.asyncio
    async def test_清空存在的会话(self) -> None:
        """清空已存在的会话。"""
        svc = MemoryContextService()
        await svc.add_message("s1", {"role": "user", "content": "内容"})
        await svc.clear_memory("s1")
        assert "s1" not in svc._layers

    @pytest.mark.asyncio
    async def test_清空不存在的会话不报错(self) -> None:
        """清空不存在的会话不应报错。"""
        svc = MemoryContextService()
        await svc.clear_memory("nonexistent")  # 不应抛异常

    @pytest.mark.asyncio
    async def test_清空后get_context_prompt为空(self) -> None:
        """清空后获取上下文应为空。"""
        svc = MemoryContextService()
        await svc.add_message("s1", {"role": "user", "content": "内容"})
        await svc.clear_memory("s1")
        prompt = await svc.get_context_prompt("s1")
        assert prompt == ""


# ============================================================
# 6. _get_total_tokens 测试
# ============================================================


class TestGetTotalTokens:
    """测试 token 总数计算。"""

    @pytest.mark.asyncio
    async def test_只有L0(self) -> None:
        """只有 L0 消息时。"""
        svc = MemoryContextService()
        await svc.add_message("s1", {"role": "user", "content": "你好"})
        total = svc._get_total_tokens("s1")
        assert total > 0

    @pytest.mark.asyncio
    async def test_L0_L1_L2全部(self) -> None:
        """三层都有内容时。"""
        svc = MemoryContextService()
        data = svc._get_session_data("s1")
        data["L0"] = [{"role": "user", "content": "消息"}]
        data["L1"] = "L1摘要内容"
        data["L2"] = "L2三元组"
        total = svc._get_total_tokens("s1")
        l0_tokens = svc._token_estimate_fn("消息")
        l1_tokens = svc._token_estimate_fn("L1摘要内容")
        l2_tokens = svc._token_estimate_fn("L2三元组")
        assert total == l0_tokens + l1_tokens + l2_tokens

    def test_空会话返回0(self) -> None:
        """空会话 token 数应为 0。"""
        svc = MemoryContextService()
        total = svc._get_total_tokens("nonexistent")
        assert total == 0


# ============================================================
# 7. _calculate_budgets 测试
# ============================================================


class TestCalculateBudgets:
    """测试预算计算。"""

    def test_默认预算计算(self) -> None:
        """默认配置应正确计算预算。"""
        svc = MemoryContextService()
        budgets = svc._calculate_budgets()
        assert "L1" in budgets
        assert "L2" in budgets
        assert budgets["L1"] == int(128000 * 0.15)
        assert budgets["L2"] == int(128000 * 0.05)

    def test_自定义预算配置(self) -> None:
        """自定义 budgets 配置应生效。"""
        config = {
            "context_window": 100000,
            "compress_trigger_ratio": 0.5,
            "budgets": {"l1": 0.2, "l2": 0.1},
        }
        svc = MemoryContextService(config=config)
        budgets = svc._calculate_budgets()
        assert budgets["L1"] == 20000
        assert budgets["L2"] == 10000


# ============================================================
# 8. _default_token_estimate 测试
# ============================================================


class TestDefaultTokenEstimate:
    """测试默认 token 估算。"""

    def test_空字符串返回0(self) -> None:
        """空字符串应返回 0。"""
        assert MemoryContextService._default_token_estimate("") == 0

    def test_None返回0(self) -> None:
        """None 应返回 0。"""
        assert MemoryContextService._default_token_estimate(None) == 0

    def test_正常文本(self) -> None:
        """正常文本应返回正整数。"""
        result = MemoryContextService._default_token_estimate("你好世界")
        assert result > 0
        assert result == max(1, len("你好世界") // 2)
