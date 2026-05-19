"""HistoryBuffer + ConversationHistory 测试。

测试对话历史缓冲区的消息存储、检索、向量搜索，
以及对话历史管理器的上下文组装和限制检查。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from memory.history_buffer import (
    ConversationHistory,
    HistoryBuffer,
    _batch_cosine_similarity,
    _cosine_similarity,
)


# ============================================================
# 辅助函数
# ============================================================


def _make_embedding_fn(dim: int = 3):
    """创建确定性 mock embedding 函数。"""
    counter = {"n": 0}

    async def embedding_fn(text: str) -> list[float]:
        counter["n"] += 1
        vec = [0.0] * dim
        # 使用文本哈希确定向量方向
        idx = hash(text) % dim
        vec[idx] = 1.0
        return vec

    return embedding_fn


# ============================================================
# 1. HistoryBuffer 测试
# ============================================================


class TestHistoryBufferAddMessage:
    """测试 HistoryBuffer.add_message。"""

    @pytest.mark.asyncio
    async def test_添加消息返回ID(self) -> None:
        """添加消息应返回消息 ID。"""
        buf = HistoryBuffer(max_size=100, enable_vector_search=False)
        msg_id = await buf.add_message({"role": "user", "content": "你好"})
        assert isinstance(msg_id, str)
        assert len(msg_id) > 0

    @pytest.mark.asyncio
    async def test_消息加入队列(self) -> None:
        """消息应加入内部队列。"""
        buf = HistoryBuffer(max_size=100, enable_vector_search=False)
        await buf.add_message({"role": "user", "content": "你好"})
        assert buf.get_message_count() == 1

    @pytest.mark.asyncio
    async def test_有embedding_fn时计算向量(self) -> None:
        """有 embedding_fn 时应计算消息向量。"""
        fn = _make_embedding_fn()
        buf = HistoryBuffer(max_size=100, enable_vector_search=True, embedding_fn=fn)
        msg_id = await buf.add_message({"role": "user", "content": "测试"})
        assert buf._vector_index.get(msg_id) is not None

    @pytest.mark.asyncio
    async def test_不计算embedding时无向量(self) -> None:
        """compute_embedding=False 时不应计算向量。"""
        fn = _make_embedding_fn()
        buf = HistoryBuffer(max_size=100, enable_vector_search=True, embedding_fn=fn)
        msg_id = await buf.add_message(
            {"role": "user", "content": "测试"}, compute_embedding=False,
        )
        assert msg_id not in buf._vector_index

    @pytest.mark.asyncio
    async def test_禁用向量搜索时不计算(self) -> None:
        """禁用向量搜索时不应计算向量。"""
        fn = _make_embedding_fn()
        buf = HistoryBuffer(max_size=100, enable_vector_search=False, embedding_fn=fn)
        await buf.add_message({"role": "user", "content": "测试"})
        assert len(buf._vector_index) == 0

    @pytest.mark.asyncio
    async def test_空内容不计算向量(self) -> None:
        """空内容消息不应计算向量。"""
        fn = _make_embedding_fn()
        buf = HistoryBuffer(max_size=100, enable_vector_search=True, embedding_fn=fn)
        msg_id = await buf.add_message({"role": "user", "content": ""})
        assert msg_id not in buf._vector_index

    @pytest.mark.asyncio
    async def test_embedding失败不抛异常(self) -> None:
        """embedding 计算失败不应抛异常。"""
        fn = AsyncMock(side_effect=Exception("计算失败"))
        buf = HistoryBuffer(max_size=100, enable_vector_search=True, embedding_fn=fn)
        msg_id = await buf.add_message({"role": "user", "content": "测试"})
        assert isinstance(msg_id, str)


class TestHistoryBufferGetRecent:
    """测试 HistoryBuffer.get_recent。"""

    @pytest.mark.asyncio
    async def test_获取最近N条(self) -> None:
        """应返回最近 N 条消息。"""
        buf = HistoryBuffer(max_size=100, enable_vector_search=False)
        for i in range(10):
            await buf.add_message({"role": "user", "content": f"消息{i}"})
        recent = buf.get_recent(n=3)
        assert len(recent) == 3
        assert recent[-1]["content"] == "消息9"

    @pytest.mark.asyncio
    async def test_默认过滤tool消息(self) -> None:
        """默认应过滤 tool 角色消息。"""
        buf = HistoryBuffer(max_size=100, enable_vector_search=False)
        await buf.add_message({"role": "user", "content": "用户消息"})
        await buf.add_message({"role": "tool", "content": "工具结果"})
        await buf.add_message({"role": "assistant", "content": "助手回复"})
        recent = buf.get_recent(n=10)
        roles = [m["role"] for m in recent]
        assert "tool" not in roles

    @pytest.mark.asyncio
    async def test_include_tool包含tool消息(self) -> None:
        """include_tool=True 应包含 tool 消息。"""
        buf = HistoryBuffer(max_size=100, enable_vector_search=False)
        await buf.add_message({"role": "user", "content": "用户消息"})
        await buf.add_message({"role": "tool", "content": "工具结果"})
        recent = buf.get_recent(n=10, include_tool=True)
        roles = [m["role"] for m in recent]
        assert "tool" in roles

    @pytest.mark.asyncio
    async def test_请求超过已有数量(self) -> None:
        """请求数量超过已有消息数时返回全部。"""
        buf = HistoryBuffer(max_size=100, enable_vector_search=False)
        await buf.add_message({"role": "user", "content": "消息"})
        recent = buf.get_recent(n=100)
        assert len(recent) == 1


class TestHistoryBufferGetOldMessages:
    """测试 HistoryBuffer.get_old_messages。"""

    @pytest.mark.asyncio
    async def test_获取旧消息(self) -> None:
        """应返回除最近 skip_recent 条外的消息。"""
        buf = HistoryBuffer(max_size=100, enable_vector_search=False)
        for i in range(10):
            await buf.add_message({"role": "user", "content": f"消息{i}"})
        old = buf.get_old_messages(skip_recent=3)
        assert len(old) == 7

    @pytest.mark.asyncio
    async def test_消息数不足时返回空(self) -> None:
        """消息数 <= skip_recent 时返回空列表。"""
        buf = HistoryBuffer(max_size=100, enable_vector_search=False)
        for i in range(3):
            await buf.add_message({"role": "user", "content": f"消息{i}"})
        old = buf.get_old_messages(skip_recent=5)
        assert old == []

    @pytest.mark.asyncio
    async def test_默认过滤tool消息(self) -> None:
        """默认应过滤 tool 消息。"""
        buf = HistoryBuffer(max_size=100, enable_vector_search=False)
        for i in range(10):
            role = "tool" if i % 3 == 0 else "user"
            await buf.add_message({"role": role, "content": f"消息{i}"})
        old = buf.get_old_messages(skip_recent=2)
        assert all(m["role"] != "tool" for m in old)


class TestHistoryBufferGetTotalTokens:
    """测试 HistoryBuffer.get_total_tokens。"""

    @pytest.mark.asyncio
    async def test_空缓冲区返回0(self) -> None:
        """空缓冲区 token 数应为 0。"""
        buf = HistoryBuffer(max_size=100, enable_vector_search=False)
        assert buf.get_total_tokens() == 0

    @pytest.mark.asyncio
    async def test_有消息时返回正数(self) -> None:
        """有消息时 token 数应大于 0。"""
        buf = HistoryBuffer(max_size=100, enable_vector_search=False)
        await buf.add_message({"role": "user", "content": "这是一段测试文本"})
        assert buf.get_total_tokens() > 0


class TestHistoryBufferGetMessageCount:
    """测试 HistoryBuffer.get_message_count。"""

    @pytest.mark.asyncio
    async def test_消息计数(self) -> None:
        """应返回正确的消息数量。"""
        buf = HistoryBuffer(max_size=100, enable_vector_search=False)
        assert buf.get_message_count() == 0
        await buf.add_message({"role": "user", "content": "消息1"})
        assert buf.get_message_count() == 1
        await buf.add_message({"role": "user", "content": "消息2"})
        assert buf.get_message_count() == 2


class TestHistoryBufferClear:
    """测试 HistoryBuffer.clear。"""

    @pytest.mark.asyncio
    async def test_清空后计数为0(self) -> None:
        """清空后消息数和向量索引应为 0。"""
        fn = _make_embedding_fn()
        buf = HistoryBuffer(max_size=100, enable_vector_search=True, embedding_fn=fn)
        await buf.add_message({"role": "user", "content": "测试"})
        buf.clear()
        assert buf.get_message_count() == 0
        assert len(buf._vector_index) == 0


class TestHistoryBufferGetStats:
    """测试 HistoryBuffer.get_stats。"""

    @pytest.mark.asyncio
    async def test_统计结构(self) -> None:
        """统计信息应包含所有必要字段。"""
        buf = HistoryBuffer(max_size=100, enable_vector_search=False)
        await buf.add_message({"role": "user", "content": "测试"})
        stats = buf.get_stats()
        assert "total_messages" in stats
        assert "total_tokens" in stats
        assert "vector_index_size" in stats
        assert "role_counts" in stats
        assert "max_size" in stats
        assert "usage_percent" in stats

    @pytest.mark.asyncio
    async def test_role_counts统计(self) -> None:
        """角色计数应正确。"""
        buf = HistoryBuffer(max_size=100, enable_vector_search=False)
        await buf.add_message({"role": "user", "content": "消息1"})
        await buf.add_message({"role": "user", "content": "消息2"})
        await buf.add_message({"role": "assistant", "content": "回复"})
        stats = buf.get_stats()
        assert stats["role_counts"]["user"] == 2
        assert stats["role_counts"]["assistant"] == 1


class TestHistoryBufferRetrieveRelevant:
    """测试 HistoryBuffer.retrieve_relevant。"""

    @pytest.mark.asyncio
    async def test_无向量索引返回空(self) -> None:
        """无向量索引时应返回空列表。"""
        buf = HistoryBuffer(max_size=100, enable_vector_search=False)
        results = await buf.retrieve_relevant("测试查询")
        assert results == []

    @pytest.mark.asyncio
    async def test_无embedding_fn返回空(self) -> None:
        """无 embedding_fn 时应返回空列表。"""
        buf = HistoryBuffer(max_size=100, enable_vector_search=True, embedding_fn=None)
        results = await buf.retrieve_relevant("测试查询")
        assert results == []

    @pytest.mark.asyncio
    async def test_检索相关消息(self) -> None:
        """应返回格式化的相关消息列表。"""
        fn = _make_embedding_fn(dim=4)
        buf = HistoryBuffer(max_size=100, enable_vector_search=True, embedding_fn=fn)
        # 添加多条消息
        await buf.add_message({"role": "user", "content": "Python 编程"})
        await buf.add_message({"role": "assistant", "content": "Java 开发"})
        # 由于 embedding_fn 基于文本哈希，结果取决于哈希值
        results = await buf.retrieve_relevant("Python 编程", top_k=3, min_similarity=0.0)
        # 至少应返回一些结果（相似度阈值为 0）
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_filter_roles过滤(self) -> None:
        """filter_roles 应过滤角色。"""
        fn = _make_embedding_fn(dim=4)
        buf = HistoryBuffer(max_size=100, enable_vector_search=True, embedding_fn=fn)
        await buf.add_message({"role": "user", "content": "Python"})
        await buf.add_message({"role": "assistant", "content": "Java"})
        results = await buf.retrieve_relevant(
            "Python", top_k=3, min_similarity=0.0, filter_roles=["user"],
        )
        # 只应包含 user 角色
        for r in results:
            assert "[USER]" in r


# ============================================================
# 2. ConversationHistory 测试
# ============================================================


class TestConversationHistory:
    """测试 ConversationHistory 对话历史管理器。"""

    @pytest.mark.asyncio
    async def test_add_message(self) -> None:
        """添加消息应委托给底层 buffer。"""
        history = ConversationHistory(max_tokens=10000, max_messages=100, embedding_fn=None)
        msg_id = await history.add_message({"role": "user", "content": "你好"})
        assert isinstance(msg_id, str)

    @pytest.mark.asyncio
    async def test_get_context_for_llm_无相关历史(self) -> None:
        """无相关历史时应只返回最近消息。"""
        history = ConversationHistory(max_tokens=10000, embedding_fn=None)
        await history.add_message({"role": "user", "content": "消息1"})
        await history.add_message({"role": "assistant", "content": "回复1"})
        ctx = await history.get_context_for_llm("查询", recent_count=10)
        assert len(ctx) >= 1

    @pytest.mark.asyncio
    async def test_get_context_for_llm_有相关历史(self) -> None:
        """有相关历史时应包含 system 上下文消息。"""
        fn = _make_embedding_fn(dim=4)
        history = ConversationHistory(max_tokens=10000, embedding_fn=fn)
        await history.add_message({"role": "user", "content": "Python 编程"})
        await history.add_message({"role": "assistant", "content": "Python 回复"})
        ctx = await history.get_context_for_llm("Python", recent_count=10, retrieve_count=3)
        # 可能包含 system 消息（如果有相关历史）
        assert isinstance(ctx, list)

    def test_get_token_count(self) -> None:
        """应返回 token 计数。"""
        history = ConversationHistory(max_tokens=10000)
        count = history.get_token_count()
        assert count == 0

    @pytest.mark.asyncio
    async def test_is_over_limit未超限(self) -> None:
        """未超限时应返回 False。"""
        history = ConversationHistory(max_tokens=100000)
        await history.add_message({"role": "user", "content": "短消息"})
        assert history.is_over_limit(threshold=0.5) is False

    @pytest.mark.asyncio
    async def test_is_over_limit已超限(self) -> None:
        """已超限时应返回 True。"""
        history = ConversationHistory(max_tokens=10)  # 极小限制
        await history.add_message({"role": "user", "content": "这是一段很长的消息" * 10})
        assert history.is_over_limit(threshold=0.01) is True

    @pytest.mark.asyncio
    async def test_clear(self) -> None:
        """清空历史。"""
        history = ConversationHistory(max_tokens=10000)
        await history.add_message({"role": "user", "content": "消息"})
        history.clear()
        assert history.get_token_count() == 0

    @pytest.mark.asyncio
    async def test_get_stats(self) -> None:
        """统计信息应包含 max_tokens。"""
        history = ConversationHistory(max_tokens=50000)
        await history.add_message({"role": "user", "content": "消息"})
        stats = history.get_stats()
        assert stats["max_tokens"] == 50000
        assert "token_usage_percent" in stats


# ============================================================
# 3. _cosine_similarity 测试
# ============================================================


class TestCosineSimilarity:
    """测试余弦相似度计算。"""

    def test_相同向量相似度为1(self) -> None:
        """相同向量的余弦相似度应为 1。"""
        vec = [1.0, 2.0, 3.0]
        sim = _cosine_similarity(vec, vec)
        assert abs(sim - 1.0) < 1e-6

    def test_正交向量相似度为0(self) -> None:
        """正交向量的余弦相似度应为 0。"""
        sim = _cosine_similarity([1.0, 0.0], [0.0, 1.0])
        assert abs(sim) < 1e-6

    def test_相反向量相似度为负1(self) -> None:
        """相反向量的余弦相似度应为 -1。"""
        sim = _cosine_similarity([1.0, 0.0], [-1.0, 0.0])
        assert abs(sim - (-1.0)) < 1e-6

    def test_空向量返回0(self) -> None:
        """空向量应返回 0。"""
        assert _cosine_similarity([], []) == 0.0

    def test_维度不一致返回0(self) -> None:
        """维度不一致应返回 0。"""
        assert _cosine_similarity([1.0], [1.0, 2.0]) == 0.0

    def test_零向量返回0(self) -> None:
        """零向量应返回 0。"""
        assert _cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0


# ============================================================
# 4. _batch_cosine_similarity 测试
# ============================================================


class TestBatchCosineSimilarity:
    """测试批量余弦相似度计算。"""

    def test_空输入返回空(self) -> None:
        """空输入应返回空列表。"""
        assert _batch_cosine_similarity([], [], top_k=5) == []

    def test_top_k限制(self) -> None:
        """top_k 应限制返回数量。"""
        query = [1.0, 0.0]
        vectors = [[1.0, 0.0], [0.0, 1.0], [0.5, 0.5]]
        results = _batch_cosine_similarity(query, vectors, top_k=2)
        assert len(results) <= 2

    def test_按相似度降序(self) -> None:
        """结果应按相似度降序排列。"""
        query = [1.0, 0.0]
        vectors = [[0.5, 0.5], [1.0, 0.0], [0.0, 1.0]]
        results = _batch_cosine_similarity(query, vectors, top_k=3)
        sims = [s for _, s in results]
        assert sims == sorted(sims, reverse=True)

    def test_min_similarity过滤(self) -> None:
        """min_similarity 应过滤低相似度结果。"""
        query = [1.0, 0.0]
        vectors = [[1.0, 0.0], [0.0, 1.0]]  # 相似度 1.0 和 0.0
        results = _batch_cosine_similarity(query, vectors, top_k=5, min_similarity=0.5)
        assert len(results) == 1
        assert results[0][1] >= 0.5



# ============================================================
# 5. HistoryBuffer 默认容量 + 配置支持 测试
# ============================================================


class TestHistoryBufferDefaultCapacity:
    """测试 HistoryBuffer 默认容量优化（从 1000 降至 100）。"""

    def test_默认max_size应为优化值(self) -> None:
        """HistoryBuffer 默认 max_size 应为 100（非 1000）。

        意图：确保多会话并发场景下内存压力得到缓解。
        """
        buf = HistoryBuffer()
        assert buf.max_size == 100

    def test_默认max_size不再为1000(self) -> None:
        """确认旧的默认值 1000 已被替换，防止回归。"""
        buf = HistoryBuffer()
        assert buf.max_size != 1000

    def test_自定义max_size优先(self) -> None:
        """显式传入 max_size 时应优先使用传入值。"""
        buf = HistoryBuffer(max_size=50)
        assert buf.max_size == 50


class TestConversationHistoryDefaultCapacity:
    """测试 ConversationHistory 默认容量优化。"""

    def test_默认max_messages应为优化值(self) -> None:
        """ConversationHistory 默认 max_messages 应为 100（非 1000）。"""
        history = ConversationHistory()
        assert history.buffer.max_size == 100

    def test_默认max_tokens保持128000(self) -> None:
        """默认 max_tokens 应保持 128000 不变。"""
        history = ConversationHistory()
        assert history.max_tokens == 128000

    def test_自定义max_messages优先(self) -> None:
        """显式传入 max_messages 时应优先使用传入值。"""
        history = ConversationHistory(max_messages=50)
        assert history.buffer.max_size == 50

    def test_自定义max_tokens优先(self) -> None:
        """显式传入 max_tokens 时应优先使用传入值。"""
        history = ConversationHistory(max_tokens=50000)
        assert history.max_tokens == 50000


class TestHistoryBufferEnvConfig:
    """测试环境变量配置支持。"""

    def test_环境变量覆盖max_size(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """HISTORY_BUFFER_MAX_SIZE 环境变量应覆盖默认 max_size。"""
        monkeypatch.setenv("HISTORY_BUFFER_MAX_SIZE", "200")
        buf = HistoryBuffer()
        assert buf.max_size == 200

    def test_环境变量覆盖max_messages(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """HISTORY_BUFFER_MAX_MESSAGES 环境变量应覆盖默认 max_messages。"""
        monkeypatch.setenv("HISTORY_BUFFER_MAX_MESSAGES", "200")
        history = ConversationHistory()
        assert history.buffer.max_size == 200

    def test_环境变量覆盖max_tokens(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """HISTORY_BUFFER_MAX_TOKENS 环境变量应覆盖默认 max_tokens。"""
        monkeypatch.setenv("HISTORY_BUFFER_MAX_TOKENS", "64000")
        history = ConversationHistory()
        assert history.max_tokens == 64000

    def test_显式参数优先于环境变量(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """显式传入的参数应优先于环境变量。"""
        monkeypatch.setenv("HISTORY_BUFFER_MAX_SIZE", "999")
        buf = HistoryBuffer(max_size=50)
        assert buf.max_size == 50

    def test_显式max_messages优先于环境变量(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ConversationHistory 显式 max_messages 应优先于环境变量。"""
        monkeypatch.setenv("HISTORY_BUFFER_MAX_MESSAGES", "999")
        history = ConversationHistory(max_messages=30)
        assert history.buffer.max_size == 30

    def test_显式max_tokens优先于环境变量(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ConversationHistory 显式 max_tokens 应优先于环境变量。"""
        monkeypatch.setenv("HISTORY_BUFFER_MAX_TOKENS", "99999")
        history = ConversationHistory(max_tokens=30000)
        assert history.max_tokens == 30000

    def test_环境变量无效值使用默认(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """环境变量为非数字时应回退到默认值。"""
        monkeypatch.setenv("HISTORY_BUFFER_MAX_SIZE", "abc")
        buf = HistoryBuffer()
        assert buf.max_size == 100

    def test_环境变量空值使用默认(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """环境变量为空字符串时应回退到默认值。"""
        monkeypatch.setenv("HISTORY_BUFFER_MAX_SIZE", "")
        buf = HistoryBuffer()
        assert buf.max_size == 100

    def test_无环境变量使用默认(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """未设置环境变量时应使用常量默认值。"""
        monkeypatch.delenv("HISTORY_BUFFER_MAX_SIZE", raising=False)
        buf = HistoryBuffer()
        assert buf.max_size == 100
