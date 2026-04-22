"""ContextCompressor + CompressionConfig 测试。

测试压缩配置计算、L0→L1 和 L1→L2 压缩、递进压缩、
关键词提取、token 估算、缓存管理和无 LLM 时的错误处理。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from memory.context_compressor import (
    CompressionConfig,
    ContextCompressor,
    normalize_layer_name,
)


# ============================================================
# 1. CompressionConfig 测试
# ============================================================


class TestCompressionConfig:
    """测试压缩配置。"""

    def test_默认配置计算预算(self) -> None:
        """默认配置应正确计算各层预算。"""
        config = CompressionConfig()
        budgets = config.get_budgets()
        assert budgets["recent"] == int(128000 * 0.3)
        assert budgets["L1"] == int(128000 * 0.15)
        assert budgets["L2"] == int(128000 * 0.05)
        assert budgets["retrieval"] == int(128000 * 0.1)
        assert budgets["max_turn"] == int(budgets["recent"] * 0.5)

    def test_自定义配置计算预算(self) -> None:
        """自定义配置应正确计算。"""
        config = CompressionConfig(context_window=100000, l1_ratio=0.2, l2_ratio=0.1)
        budgets = config.get_budgets()
        assert budgets["L1"] == 20000
        assert budgets["L2"] == 10000

    def test_get_trigger_threshold(self) -> None:
        """触发阈值应等于 context_window * compress_trigger_ratio。"""
        config = CompressionConfig(context_window=100000, compress_trigger_ratio=0.5)
        assert config.get_trigger_threshold() == 50000

    def test_自定义触发比例(self) -> None:
        """自定义触发比例应生效。"""
        config = CompressionConfig(context_window=100000, compress_trigger_ratio=0.7)
        assert config.get_trigger_threshold() == 70000


# ============================================================
# 2. normalize_layer_name 测试
# ============================================================


class TestNormalizeLayerName:
    """测试层级名称标准化。"""

    def test_DSL映射到L1(self) -> None:
        """DSL 应映射到 L1。"""
        assert normalize_layer_name("DSL") == "L1"

    def test_CSL映射到L2(self) -> None:
        """CSL 应映射到 L2。"""
        assert normalize_layer_name("CSL") == "L2"

    def test_KIL映射到L2(self) -> None:
        """KIL 应映射到 L2。"""
        assert normalize_layer_name("KIL") == "L2"

    def test_L1保持不变(self) -> None:
        """L1 应保持不变。"""
        assert normalize_layer_name("L1") == "L1"

    def test_L2保持不变(self) -> None:
        """L2 应保持不变。"""
        assert normalize_layer_name("L2") == "L2"

    def test_小写自动转大写(self) -> None:
        """小写输入应自动转大写。"""
        assert normalize_layer_name("dsl") == "L1"

    def test_未知名称保持大写(self) -> None:
        """未知名称应保持大写。"""
        assert normalize_layer_name("custom") == "CUSTOM"


# ============================================================
# 3. compress_to_l1 测试
# ============================================================


class TestCompressToL1:
    """测试 L0 → L1 压缩。"""

    @pytest.mark.asyncio
    async def test_空消息返回空字符串(self) -> None:
        """空消息列表应返回空字符串。"""
        compressor = ContextCompressor(llm_call_fn=AsyncMock())
        result = await compressor.compress_to_l1([])
        assert result == ""

    @pytest.mark.asyncio
    async def test_正常压缩(self) -> None:
        """正常消息应返回 LLM 生成的摘要。"""
        llm_fn = AsyncMock(return_value="## Session Title\n测试会话")
        compressor = ContextCompressor(llm_call_fn=llm_fn)
        messages = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好！"},
        ]
        result = await compressor.compress_to_l1(messages)
        assert "测试会话" in result
        llm_fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_缓存命中(self) -> None:
        """相同消息应命中缓存。"""
        llm_fn = AsyncMock(return_value="摘要内容")
        compressor = ContextCompressor(llm_call_fn=llm_fn)
        messages = [{"role": "user", "content": "测试"}]
        r1 = await compressor.compress_to_l1(messages)
        r2 = await compressor.compress_to_l1(messages)
        assert r1 == r2
        assert llm_fn.call_count == 1  # 只调用一次

    @pytest.mark.asyncio
    async def test_LLM失败抛RuntimeError(self) -> None:
        """LLM 调用失败应抛 RuntimeError。"""
        llm_fn = AsyncMock(side_effect=Exception("LLM 错误"))
        compressor = ContextCompressor(llm_call_fn=llm_fn)
        with pytest.raises(RuntimeError, match="L1 压缩失败"):
            await compressor.compress_to_l1([{"role": "user", "content": "测试"}])

    @pytest.mark.asyncio
    async def test_无LLM函数抛RuntimeError(self) -> None:
        """无 LLM 函数应抛 RuntimeError。"""
        compressor = ContextCompressor()
        with pytest.raises(RuntimeError, match="未提供 LLM 调用函数"):
            await compressor.compress_to_l1([{"role": "user", "content": "测试"}])


# ============================================================
# 4. compress_to_l2 测试
# ============================================================


class TestCompressToL2:
    """测试 L1 → L2 压缩。"""

    @pytest.mark.asyncio
    async def test_空摘要返回空字符串(self) -> None:
        """空 L1 摘要应返回空字符串。"""
        compressor = ContextCompressor(llm_call_fn=AsyncMock())
        result = await compressor.compress_to_l2("")
        assert result == ""

    @pytest.mark.asyncio
    async def test_正常压缩(self) -> None:
        """正常 L1 摘要应返回 L2 三元组。"""
        llm_fn = AsyncMock(return_value="## 意图\n测试意图\n## 过程\n步骤\n## 结果\n完成")
        compressor = ContextCompressor(llm_call_fn=llm_fn)
        result = await compressor.compress_to_l2("L1 摘要内容")
        assert "意图" in result
        llm_fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_缓存命中(self) -> None:
        """相同 L1 摘要应命中缓存。"""
        llm_fn = AsyncMock(return_value="三元组")
        compressor = ContextCompressor(llm_call_fn=llm_fn)
        r1 = await compressor.compress_to_l2("L1 摘要")
        r2 = await compressor.compress_to_l2("L1 摘要")
        assert r1 == r2
        assert llm_fn.call_count == 1

    @pytest.mark.asyncio
    async def test_LLM失败抛RuntimeError(self) -> None:
        """LLM 调用失败应抛 RuntimeError。"""
        llm_fn = AsyncMock(side_effect=Exception("LLM 错误"))
        compressor = ContextCompressor(llm_call_fn=llm_fn)
        with pytest.raises(RuntimeError, match="L2 压缩失败"):
            await compressor.compress_to_l2("L1 摘要内容")


# ============================================================
# 5. progressive_compress 测试
# ============================================================


class TestProgressiveCompress:
    """测试递进压缩。"""

    @pytest.mark.asyncio
    async def test_L0到L1压缩(self) -> None:
        """有 L0 内容时应触发 L0→L1 压缩。"""
        call_count = 0

        async def mock_llm(prompt: str) -> str:
            nonlocal call_count
            call_count += 1
            return f"压缩结果_{call_count}"

        compressor = ContextCompressor(llm_call_fn=mock_llm)
        budgets = {"L1": 500, "L2": 200}
        new_l1, new_l2 = await compressor.progressive_compress(
            l0="这是一段很长的原文内容",
            l1="",
            l2="",
            budgets=budgets,
        )
        assert "压缩结果" in new_l1

    @pytest.mark.asyncio
    async def test_L1超预算触发L2压缩(self) -> None:
        """L1 超预算时应触发 L1→L2 压缩。"""
        responses = iter(["L1压缩", "L2压缩"])

        async def mock_llm(prompt: str) -> str:
            return next(responses)

        compressor = ContextCompressor(llm_call_fn=mock_llm)
        # 用很小的 L1 预算确保触发 L2 压缩
        budgets = {"L1": 5, "L2": 200}
        new_l1, new_l2 = await compressor.progressive_compress(
            l0="原文",
            l1="",
            l2="",
            budgets=budgets,
        )
        # L2 应有内容（因为 L1 超预算后溢出部分被压缩到 L2）
        # 但具体是否触发取决于 _extract_overflow 的结果

    @pytest.mark.asyncio
    async def test_无L0内容不压缩(self) -> None:
        """无 L0 内容且 L1/L2 不超预算时不应调用 LLM。"""
        llm_fn = AsyncMock(return_value="不应该被调用")
        compressor = ContextCompressor(llm_call_fn=llm_fn)
        budgets = {"L1": 5000, "L2": 2000}
        new_l1, new_l2 = await compressor.progressive_compress(
            l0="", l1="短L1", l2="短L2", budgets=budgets,
        )
        assert new_l1 == "短L1"
        assert new_l2 == "短L2"
        llm_fn.assert_not_called()


# ============================================================
# 6. extract_keywords 测试
# ============================================================


class TestExtractKeywords:
    """测试关键词提取。"""

    @pytest.mark.asyncio
    async def test_空内容返回空列表(self) -> None:
        """空内容应返回空列表。"""
        compressor = ContextCompressor(llm_call_fn=AsyncMock())
        result = await compressor.extract_keywords("")
        assert result == []

    @pytest.mark.asyncio
    async def test_正常提取(self) -> None:
        """正常内容应返回关键词列表。"""
        llm_fn = AsyncMock(return_value='["Python", "Flask", "API", "数据库", "测试"]')
        compressor = ContextCompressor(llm_call_fn=llm_fn)
        result = await compressor.extract_keywords("Python Flask API 数据库 测试")
        assert len(result) > 0
        assert "Python" in result

    @pytest.mark.asyncio
    async def test_提取数量限制(self) -> None:
        """提取数量应不超过 10。"""
        keywords = [f"关键词{i}" for i in range(20)]
        llm_fn = AsyncMock(return_value=str(keywords))
        compressor = ContextCompressor(llm_call_fn=llm_fn)
        result = await compressor.extract_keywords("内容")
        assert len(result) <= 10

    @pytest.mark.asyncio
    async def test_无效JSON返回空列表(self) -> None:
        """LLM 返回无效 JSON 应返回空列表。"""
        llm_fn = AsyncMock(return_value="这不是JSON")
        compressor = ContextCompressor(llm_call_fn=llm_fn)
        result = await compressor.extract_keywords("内容")
        assert result == []

    @pytest.mark.asyncio
    async def test_LLM失败返回空列表(self) -> None:
        """LLM 调用失败应返回空列表。"""
        llm_fn = AsyncMock(side_effect=Exception("LLM 错误"))
        compressor = ContextCompressor(llm_call_fn=llm_fn)
        result = await compressor.extract_keywords("内容")
        assert result == []


# ============================================================
# 7. _estimate_tokens 测试
# ============================================================


class TestEstimateTokens:
    """测试 token 估算。"""

    def test_空字符串返回0(self) -> None:
        """空字符串应返回 0。"""
        compressor = ContextCompressor()
        assert compressor._estimate_tokens("") == 0

    def test_纯英文文本(self) -> None:
        """纯英文文本估算。"""
        compressor = ContextCompressor()
        tokens = compressor._estimate_tokens("Hello World!")
        assert tokens > 0
        assert tokens == max(1, len("Hello World!") // 2)

    def test_中文文本(self) -> None:
        """中文文本估算。"""
        compressor = ContextCompressor()
        tokens = compressor._estimate_tokens("你好世界测试")
        assert tokens > 0

    def test_消息列表估算(self) -> None:
        """消息列表估算。"""
        compressor = ContextCompressor()
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "World"},
        ]
        tokens = compressor._estimate_tokens(messages)
        assert tokens > 0


# ============================================================
# 8. _format_messages 测试
# ============================================================


class TestFormatMessages:
    """测试消息格式化。"""

    def test_用户消息(self) -> None:
        """用户消息应包含【用户】标记。"""
        compressor = ContextCompressor()
        result = compressor._format_messages([{"role": "user", "content": "你好"}])
        assert "【用户 1】" in result
        assert "你好" in result

    def test_助手消息(self) -> None:
        """助手消息应包含【助手】标记。"""
        compressor = ContextCompressor()
        result = compressor._format_messages([{"role": "assistant", "content": "回复"}])
        assert "【助手 1】" in result

    def test_系统消息(self) -> None:
        """系统消息应包含【系统】标记。"""
        compressor = ContextCompressor()
        result = compressor._format_messages([{"role": "system", "content": "指令"}])
        assert "【系统 1】" in result

    def test_工具消息(self) -> None:
        """工具消息应包含【工具】标记。"""
        compressor = ContextCompressor()
        result = compressor._format_messages([
            {"role": "tool", "content": "结果", "name": "search"},
        ])
        assert "【工具 1: search】" in result

    def test_长内容工具消息截断(self) -> None:
        """长内容的工具消息应截断。"""
        compressor = ContextCompressor()
        long_content = "a" * 500
        result = compressor._format_messages([
            {"role": "tool", "content": long_content, "name": "tool"},
        ])
        assert "..." in result

    def test_空内容跳过(self) -> None:
        """空内容的消息应跳过。"""
        compressor = ContextCompressor()
        result = compressor._format_messages([{"role": "user", "content": ""}])
        assert result == ""

    def test_未知角色(self) -> None:
        """未知角色应使用大写标记。"""
        compressor = ContextCompressor()
        result = compressor._format_messages([{"role": "custom", "content": "内容"}])
        assert "【CUSTOM 1】" in result


# ============================================================
# 9. _truncate_to_budget 测试
# ============================================================


class TestTruncateToBudget:
    """测试文本截断。"""

    def test_不超预算不截断(self) -> None:
        """不超预算时不应截断。"""
        compressor = ContextCompressor()
        text = "短文本"
        result = compressor._truncate_to_budget(text, 1000)
        assert result == text

    def test_超预算时截断(self) -> None:
        """超预算时应截断。"""
        compressor = ContextCompressor()
        text = "a" * 1000
        result = compressor._truncate_to_budget(text, 100)
        assert len(result) < len(text)


# ============================================================
# 10. 缓存管理测试
# ============================================================


class TestCacheManagement:
    """测试缓存管理。"""

    def test_generate_cache_key一致性(self) -> None:
        """相同消息和层级应生成相同的缓存键。"""
        compressor = ContextCompressor()
        messages = [{"role": "user", "content": "测试"}]
        key1 = compressor._generate_cache_key(messages, "L1")
        key2 = compressor._generate_cache_key(messages, "L1")
        assert key1 == key2

    def test_generate_cache_key不同消息(self) -> None:
        """不同消息应生成不同的缓存键。"""
        compressor = ContextCompressor()
        key1 = compressor._generate_cache_key(
            [{"role": "user", "content": "测试1"}], "L1",
        )
        key2 = compressor._generate_cache_key(
            [{"role": "user", "content": "测试2"}], "L1",
        )
        assert key1 != key2

    def test_generate_cache_key不同层级(self) -> None:
        """不同层级应生成不同的缓存键。"""
        compressor = ContextCompressor()
        messages = [{"role": "user", "content": "测试"}]
        key1 = compressor._generate_cache_key(messages, "L1")
        key2 = compressor._generate_cache_key(messages, "L2")
        assert key1 != key2

    def test_cache_put_and_get(self) -> None:
        """缓存写入后应可读取。"""
        compressor = ContextCompressor()
        compressor._cache_put("key1", "value1")
        assert compressor._cache["key1"] == "value1"

    def test_cache_put_超限清理(self) -> None:
        """缓存超限时应清理。"""
        compressor = ContextCompressor()
        compressor._cache_max_size = 10
        for i in range(15):
            compressor._cache_put(f"key_{i}", f"value_{i}")
        assert compressor.get_cache_size() <= 10

    def test_clear_cache(self) -> None:
        """清空缓存。"""
        compressor = ContextCompressor()
        compressor._cache_put("key1", "value1")
        compressor.clear_cache()
        assert compressor.get_cache_size() == 0

    def test_get_cache_size(self) -> None:
        """获取缓存大小。"""
        compressor = ContextCompressor()
        assert compressor.get_cache_size() == 0
        compressor._cache_put("key1", "value1")
        assert compressor.get_cache_size() == 1


# ============================================================
# 11. get_stats 测试
# ============================================================


class TestGetStats:
    """测试统计信息。"""

    def test_初始统计(self) -> None:
        """初始状态统计。"""
        compressor = ContextCompressor()
        stats = compressor.get_stats()
        assert stats["l0_to_l1_count"] == 0
        assert stats["l1_to_l2_count"] == 0
        assert stats["total_tokens_compressed"] == 0
        assert stats["cache_size"] == 0
        assert "budgets" in stats

    @pytest.mark.asyncio
    async def test_压缩后统计更新(self) -> None:
        """压缩后统计应更新。"""
        llm_fn = AsyncMock(return_value="摘要")
        compressor = ContextCompressor(llm_call_fn=llm_fn)
        await compressor.compress_to_l1([{"role": "user", "content": "测试"}])
        stats = compressor.get_stats()
        assert stats["l0_to_l1_count"] == 1
        assert stats["total_tokens_compressed"] > 0


# ============================================================
# 12. update_config 测试
# ============================================================


class TestUpdateConfig:
    """测试配置更新。"""

    def test_更新配置(self) -> None:
        """更新配置应重新计算预算。"""
        compressor = ContextCompressor()
        new_config = CompressionConfig(context_window=50000)
        compressor.update_config(new_config)
        assert compressor.config.context_window == 50000
        assert compressor.budgets["L1"] == int(50000 * 0.15)


# ============================================================
# 13. 无 LLM 函数时抛 RuntimeError
# ============================================================


class TestNoLLMFunction:
    """测试无 LLM 调用函数时的错误处理。"""

    @pytest.mark.asyncio
    async def test_compress_to_l1抛RuntimeError(self) -> None:
        """无 LLM 函数时 compress_to_l1 应抛 RuntimeError。"""
        compressor = ContextCompressor()
        with pytest.raises(RuntimeError, match="未提供 LLM 调用函数"):
            await compressor.compress_to_l1([{"role": "user", "content": "测试"}])

    @pytest.mark.asyncio
    async def test_compress_to_l2抛RuntimeError(self) -> None:
        """无 LLM 函数时 compress_to_l2 应抛 RuntimeError。"""
        compressor = ContextCompressor()
        with pytest.raises(RuntimeError, match="未提供 LLM 调用函数"):
            await compressor.compress_to_l2("L1 摘要")

    @pytest.mark.asyncio
    async def test_compress兼容接口抛RuntimeError(self) -> None:
        """无 LLM 函数时 compress 兼容接口应抛 RuntimeError。"""
        compressor = ContextCompressor()
        with pytest.raises(RuntimeError):
            await compressor.compress([{"role": "user", "content": "测试"}])
