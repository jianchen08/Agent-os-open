"""ContextCompressor + CompressionConfig 测试。

测试压缩配置计算、一次性压缩、递进压缩、
长期记忆提取、token 估算、缓存管理和无 LLM 时的错误处理。
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from memory.context_compressor import (
    CompressionConfig,
    ContextCompressor,
    normalize_layer_name,
)

# 模拟 LLM 返回的合法压缩 JSON
_MOCK_COMPRESS_JSON = json.dumps(
    {
        "l1": {
            "session_title": "测试会话",
            "current_state": "进行中",
            "task_specification": "测试任务",
            "key_entities": "测试实体",
            "workflow": "测试流程",
            "errors_and_corrections": "",
            "domain_knowledge": "",
            "decisions": "",
            "key_results": "测试结果",
            "pending": "",
        },
        "l2": {
            "intent": "测试意图",
            "process": "测试步骤",
            "results": "测试成果",
        },
        "keywords": ["Python", "Flask", "API"],
    },
    ensure_ascii=False,
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
        assert budgets["recent"] == int(128000 * 0.10)
        assert budgets["L1"] == int(128000 * 0.08)
        assert budgets["L2"] == int(128000 * 0.03)
        assert budgets["retrieval"] == int(128000 * 0.03)
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
# 3. compress_all 测试
# ============================================================


class TestCompressAll:
    """测试一次性压缩（L1 + L2 + 关键词）。"""

    @pytest.mark.asyncio
    async def test_空消息返回空结果(self) -> None:
        """空消息列表应返回空字典。"""
        compressor = ContextCompressor(llm_call_fn=AsyncMock())
        result = await compressor.compress_all([])
        assert result == {"l1": "", "l2": "", "keywords": []}

    @pytest.mark.asyncio
    async def test_正常压缩(self) -> None:
        """正常消息应返回包含 L1、L2 和 keywords 的字典。"""
        llm_fn = AsyncMock(return_value=_MOCK_COMPRESS_JSON)
        compressor = ContextCompressor(llm_call_fn=llm_fn)
        messages = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好！"},
        ]
        result = await compressor.compress_all(messages)
        assert "测试会话" in result["l1"]
        assert "测试意图" in result["l2"]
        assert len(result["keywords"]) > 0
        llm_fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_LLM失败抛RuntimeError(self) -> None:
        """LLM 调用失败应抛 RuntimeError。"""
        llm_fn = AsyncMock(side_effect=Exception("LLM 错误"))
        compressor = ContextCompressor(llm_call_fn=llm_fn)
        with pytest.raises(RuntimeError, match="压缩失败"):
            await compressor.compress_all([{"role": "user", "content": "测试"}])

    @pytest.mark.asyncio
    async def test_无LLM函数抛RuntimeError(self) -> None:
        """无 LLM 函数应抛 RuntimeError。"""
        compressor = ContextCompressor()
        with pytest.raises(RuntimeError, match="未提供 LLM 调用函数"):
            await compressor.compress_all([{"role": "user", "content": "测试"}])

    @pytest.mark.asyncio
    async def test_无效JSON返回空结果(self) -> None:
        """LLM 返回无效 JSON 应返回空结果。"""
        llm_fn = AsyncMock(return_value="这不是JSON")
        compressor = ContextCompressor(llm_call_fn=llm_fn)
        result = await compressor.compress_all([{"role": "user", "content": "测试"}])
        assert result == {"l1": "", "l2": "", "keywords": []}


# ============================================================
# 4. progressive_compress 测试
# ============================================================


class TestProgressiveCompress:
    """测试递进压缩。"""

    @pytest.mark.asyncio
    async def test_L0触发压缩(self) -> None:
        """有 L0 内容时应触发一次性压缩。"""
        llm_fn = AsyncMock(return_value=_MOCK_COMPRESS_JSON)
        compressor = ContextCompressor(llm_call_fn=llm_fn)
        budgets = {"L1": 500, "L2": 200}
        new_l1, new_l2 = await compressor.progressive_compress(
            l0="这是一段很长的原文内容",
            l1="",
            l2="",
            budgets=budgets,
        )
        assert "测试会话" in new_l1
        assert "测试意图" in new_l2

    @pytest.mark.asyncio
    async def test_L1超预算时裁剪(self) -> None:
        """L1 超预算时应裁剪到预算内。"""
        llm_fn = AsyncMock(return_value=_MOCK_COMPRESS_JSON)
        compressor = ContextCompressor(llm_call_fn=llm_fn)
        budgets = {"L1": 5, "L2": 200}
        new_l1, new_l2 = await compressor.progressive_compress(
            l0="原文",
            l1="",
            l2="",
            budgets=budgets,
        )
        assert compressor._estimate_tokens(new_l1) <= budgets["L1"]

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
# 5. extract_long_term_memory 测试
# ============================================================


class TestExtractLongTermMemory:
    """测试长期记忆提取。"""

    @pytest.mark.asyncio
    async def test_空消息返回空实例(self) -> None:
        """空消息列表应返回空 MemoryExtraction。"""
        compressor = ContextCompressor(llm_call_fn=AsyncMock())
        result = await compressor.extract_long_term_memory([])
        assert result.user_profile_updates == ""
        assert result.project_knowledge_updates == ""
        assert result.experience_updates == ""

    @pytest.mark.asyncio
    async def test_正常提取(self) -> None:
        """正常消息应返回提取的记忆。"""
        memory_json = json.dumps({
            "user_profile_updates": "用户喜欢用 Python",
            "project_knowledge_updates": "项目使用 FastAPI",
            "experience_updates": "发现某个 API 有 bug",
        }, ensure_ascii=False)
        llm_fn = AsyncMock(return_value=memory_json)
        compressor = ContextCompressor(llm_call_fn=llm_fn)
        result = await compressor.extract_long_term_memory(
            [{"role": "user", "content": "我用 Python 开发"}],
        )
        assert "Python" in result.user_profile_updates
        assert "FastAPI" in result.project_knowledge_updates
        llm_fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_无效JSON返回空实例(self) -> None:
        """LLM 返回无效 JSON 应返回空实例。"""
        llm_fn = AsyncMock(return_value="这不是JSON")
        compressor = ContextCompressor(llm_call_fn=llm_fn)
        result = await compressor.extract_long_term_memory(
            [{"role": "user", "content": "测试"}],
        )
        assert result.user_profile_updates == ""

    @pytest.mark.asyncio
    async def test_LLM失败返回空实例(self) -> None:
        """LLM 调用失败应返回空实例（不抛异常）。"""
        llm_fn = AsyncMock(side_effect=Exception("LLM 错误"))
        compressor = ContextCompressor(llm_call_fn=llm_fn)
        result = await compressor.extract_long_term_memory(
            [{"role": "user", "content": "测试"}],
        )
        assert result.user_profile_updates == ""


# ============================================================
# 6. _estimate_tokens 测试
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
# 7. _format_messages 测试
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
# 8. _truncate_to_budget 测试
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
# 9. 缓存管理测试
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
# 10. get_stats 测试
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
        llm_fn = AsyncMock(return_value=_MOCK_COMPRESS_JSON)
        compressor = ContextCompressor(llm_call_fn=llm_fn)
        await compressor.compress_all([{"role": "user", "content": "测试"}])
        stats = compressor.get_stats()
        assert stats["l0_to_l1_count"] == 1
        assert stats["l1_to_l2_count"] == 1
        assert stats["total_tokens_compressed"] > 0


# ============================================================
# 11. update_config 测试
# ============================================================


class TestUpdateConfig:
    """测试配置更新。"""

    def test_更新配置(self) -> None:
        """更新配置应重新计算预算。"""
        compressor = ContextCompressor()
        new_config = CompressionConfig(context_window=50000)
        compressor.update_config(new_config)
        assert compressor.config.context_window == 50000
        assert compressor.budgets["L1"] == int(50000 * 0.08)


# ============================================================
# 12. 无 LLM 函数时抛 RuntimeError
# ============================================================


class TestNoLLMFunction:
    """测试无 LLM 调用函数时的错误处理。"""

    @pytest.mark.asyncio
    async def test_compress_all抛RuntimeError(self) -> None:
        """无 LLM 函数时 compress_all 应抛 RuntimeError。"""
        compressor = ContextCompressor()
        with pytest.raises(RuntimeError, match="未提供 LLM 调用函数"):
            await compressor.compress_all([{"role": "user", "content": "测试"}])
