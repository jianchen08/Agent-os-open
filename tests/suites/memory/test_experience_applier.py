"""ExperienceApplier 单元测试。

测试要点：
- find_relevant_experience 调用 memory_service.retrieve
- should_apply 有高相关度匹配 → True
- should_apply 无匹配或低相关度 → False
"""

from __future__ import annotations

from typing import Any

import pytest

from memory.experience_applier import ExperienceApplier, ExperienceMatch
from memory.service import MemoryService
from memory.types import MemoryType, SearchResult


# ============================================================
# Helpers
# ============================================================


class MockRetriever:
    """模拟检索器，返回预设结果。"""

    def __init__(self, results: list[SearchResult] | None = None) -> None:
        self._results = results or []
        self.retrieve_called = False
        self.retrieve_kwargs: dict[str, Any] = {}

    async def retrieve(
        self,
        query: str,
        user_id: str | None = None,
        top_k: int = 5,
        memory_type: str = "semantic",
        filters: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        self.retrieve_called = True
        self.retrieve_kwargs = {
            "query": query,
            "user_id": user_id,
            "top_k": top_k,
            "memory_type": memory_type,
            "filters": filters,
        }
        return self._results


class FailingRetriever:
    """模拟检索器，检索时抛异常。"""

    async def retrieve(
        self,
        query: str,
        user_id: str | None = None,
        top_k: int = 5,
        memory_type: str = "semantic",
        filters: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        raise RuntimeError("检索服务不可用")


def _make_memory_service(retriever: Any = None) -> MemoryService:
    """创建带模拟检索器的 MemoryService。"""
    service = MemoryService()
    if retriever is not None:
        service.register_retriever("vector", retriever)
    return service


# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def sample_results() -> list[SearchResult]:
    """创建示例搜索结果。"""
    return [
        SearchResult(
            id="k1",
            content="意图: 分析数据\n执行摘要: 使用 pandas 分析完成",
            score=0.85,
            memory_type=MemoryType.SEMANTIC,
            metadata={"source_type": "experience", "source_episode_id": "ep1"},
        ),
        SearchResult(
            id="k2",
            content="意图: 生成报告\n执行摘要: 使用模板生成报告",
            score=0.72,
            memory_type=MemoryType.SEMANTIC,
            metadata={"source_type": "experience", "source_episode_id": "ep2"},
        ),
        SearchResult(
            id="k3",
            content="意图: 清理缓存\n执行摘要: 清理临时文件",
            score=0.45,
            memory_type=MemoryType.SEMANTIC,
            metadata={"source_type": "experience", "source_episode_id": "ep3"},
        ),
    ]


# ============================================================
# find_relevant_experience 测试
# ============================================================


class TestFindRelevantExperience:
    """find_relevant_experience 方法测试。"""

    @pytest.mark.asyncio
    async def test_calls_memory_service_retrieve(self, sample_results: list[SearchResult]) -> None:
        """find_relevant_experience 调用 memory_service.retrieve。"""
        retriever = MockRetriever(sample_results)
        memory_service = _make_memory_service(retriever)
        applier = ExperienceApplier(memory_service=memory_service)

        await applier.find_relevant_experience(
            intent="帮我分析数据",
            user_id="user1",
        )

        assert retriever.retrieve_called is True
        assert retriever.retrieve_kwargs["query"] == "帮我分析数据"
        assert retriever.retrieve_kwargs["user_id"] == "user1"

    @pytest.mark.asyncio
    async def test_returns_matching_experiences(self, sample_results: list[SearchResult]) -> None:
        """返回符合条件的经验匹配列表。"""
        retriever = MockRetriever(sample_results)
        memory_service = _make_memory_service(retriever)
        applier = ExperienceApplier(memory_service=memory_service)

        results = await applier.find_relevant_experience(
            intent="分析数据",
            user_id="user1",
            min_score=70.0,  # 0.85*100=85, 0.72*100=72, 0.45*100=45 → 只有前两个
        )

        assert len(results) == 2
        assert results[0].knowledge_id == "k1"
        assert results[0].relevance == 0.85
        assert results[0].source_episode_id == "ep1"

    @pytest.mark.asyncio
    async def test_respects_max_results(self, sample_results: list[SearchResult]) -> None:
        """max_results 限制返回数量。"""
        retriever = MockRetriever(sample_results)
        memory_service = _make_memory_service(retriever)
        applier = ExperienceApplier(memory_service=memory_service)

        results = await applier.find_relevant_experience(
            intent="分析数据",
            user_id="user1",
            max_results=1,
            min_score=0.0,  # 不设阈值
        )

        assert len(results) == 1
        assert results[0].relevance >= results[0].relevance  # 最高的

    @pytest.mark.asyncio
    async def test_no_results_when_score_below_threshold(self) -> None:
        """所有结果分数低于阈值 → 返回空列表。"""
        low_results = [
            SearchResult(
                id="k1",
                content="低分内容",
                score=0.3,
                memory_type=MemoryType.SEMANTIC,
                metadata={"source_type": "experience"},
            ),
        ]
        retriever = MockRetriever(low_results)
        memory_service = _make_memory_service(retriever)
        applier = ExperienceApplier(memory_service=memory_service)

        results = await applier.find_relevant_experience(
            intent="查询",
            user_id="user1",
            min_score=70.0,  # 0.3*100=30 < 70
        )

        assert results == []

    @pytest.mark.asyncio
    async def test_retrieve_failure_returns_empty(self) -> None:
        """检索失败时返回空列表。"""
        retriever = FailingRetriever()
        memory_service = _make_memory_service(retriever)
        applier = ExperienceApplier(memory_service=memory_service)

        results = await applier.find_relevant_experience(
            intent="查询",
            user_id="user1",
        )

        assert results == []

    @pytest.mark.asyncio
    async def test_no_retriever_returns_empty(self) -> None:
        """无检索器时返回空列表。"""
        memory_service = _make_memory_service()  # 不注册检索器
        applier = ExperienceApplier(memory_service=memory_service)

        results = await applier.find_relevant_experience(
            intent="查询",
            user_id="user1",
        )

        assert results == []

    @pytest.mark.asyncio
    async def test_results_sorted_by_relevance(self) -> None:
        """结果按相关度降序排列。"""
        results = [
            SearchResult(id="k2", content="中", score=0.6, memory_type=MemoryType.SEMANTIC, metadata={}),
            SearchResult(id="k1", content="高", score=0.9, memory_type=MemoryType.SEMANTIC, metadata={}),
            SearchResult(id="k3", content="低", score=0.4, memory_type=MemoryType.SEMANTIC, metadata={}),
        ]
        retriever = MockRetriever(results)
        memory_service = _make_memory_service(retriever)
        applier = ExperienceApplier(memory_service=memory_service)

        matches = await applier.find_relevant_experience(
            intent="查询",
            user_id="user1",
            min_score=0.0,
        )

        assert len(matches) == 3
        assert matches[0].relevance >= matches[1].relevance
        assert matches[1].relevance >= matches[2].relevance


# ============================================================
# should_apply 测试
# ============================================================


class TestShouldApply:
    """should_apply 方法测试。"""

    def test_high_relevance_returns_true(self) -> None:
        """有高相关度匹配 → 返回 True。"""
        applier = ExperienceApplier(memory_service=MemoryService())
        matches = [
            ExperienceMatch(knowledge_id="k1", content="内容", relevance=0.8),
        ]

        assert applier.should_apply(matches, relevance_threshold=0.6) is True

    def test_low_relevance_returns_false(self) -> None:
        """所有匹配相关度低于阈值 → 返回 False。"""
        applier = ExperienceApplier(memory_service=MemoryService())
        matches = [
            ExperienceMatch(knowledge_id="k1", content="内容", relevance=0.4),
            ExperienceMatch(knowledge_id="k2", content="内容2", relevance=0.5),
        ]

        assert applier.should_apply(matches, relevance_threshold=0.6) is False

    def test_empty_matches_returns_false(self) -> None:
        """无匹配 → 返回 False。"""
        applier = ExperienceApplier(memory_service=MemoryService())

        assert applier.should_apply([], relevance_threshold=0.6) is False

    def test_exactly_at_threshold_returns_false(self) -> None:
        """恰好等于阈值（不大于）→ 返回 False。"""
        applier = ExperienceApplier(memory_service=MemoryService())
        matches = [
            ExperienceMatch(knowledge_id="k1", content="内容", relevance=0.6),
        ]

        assert applier.should_apply(matches, relevance_threshold=0.6) is False

    def test_custom_threshold(self) -> None:
        """自定义阈值生效。"""
        applier = ExperienceApplier(memory_service=MemoryService())
        matches = [
            ExperienceMatch(knowledge_id="k1", content="内容", relevance=0.7),
        ]

        # 阈值 0.8，0.7 < 0.8 → False
        assert applier.should_apply(matches, relevance_threshold=0.8) is False
        # 阈值 0.5，0.7 > 0.5 → True
        assert applier.should_apply(matches, relevance_threshold=0.5) is True

    def test_mixed_matches_one_high_enough(self) -> None:
        """混合匹配中只要有一个高质量即返回 True。"""
        applier = ExperienceApplier(memory_service=MemoryService())
        matches = [
            ExperienceMatch(knowledge_id="k1", content="低", relevance=0.3),
            ExperienceMatch(knowledge_id="k2", content="高", relevance=0.8),
            ExperienceMatch(knowledge_id="k3", content="中", relevance=0.5),
        ]

        assert applier.should_apply(matches, relevance_threshold=0.6) is True
