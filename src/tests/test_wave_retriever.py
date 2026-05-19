"""WaveRetriever 单元测试。

测试波浪算法检索器的各个组件：
- EPA 分析
- 残差金字塔
- 浪潮扩散
- 霰弹枪检索
- 结果合并
- 余弦相似度
- retrieve 主方法端到端
"""

from __future__ import annotations

from typing import Any

import pytest

from memory.types import MemoryType, SearchResult


# ---------------------------------------------------------------------------
# 辅助：构建测试用知识条目
# ---------------------------------------------------------------------------

def _make_item(
    item_id: str,
    content: str,
    tags: list[str] | None = None,
    embedding: list[float] | None = None,
    epa: dict[str, list[str]] | None = None,
    related_ids: list[str] | None = None,
) -> dict[str, Any]:
    """构造一条知识条目字典。"""
    return {
        "id": item_id,
        "content": content,
        "tags": tags or [],
        "embedding": embedding,
        "epa": epa or {},
        "related_ids": related_ids or [],
    }


# ---------------------------------------------------------------------------
# EPA 分析测试
# ---------------------------------------------------------------------------

class TestExtractEPA:
    """测试 _extract_epa 方法。"""

    @pytest.fixture()
    def retriever(self) -> Any:
        """创建无外部依赖的 WaveRetriever 实例。"""
        from memory.wave_retriever import WaveRetriever

        return WaveRetriever()

    def test_basic_sentence(self, retriever: Any) -> None:
        """简单句子应提取出实体、属性和动作。"""
        result = retriever._extract_epa("智能助手快速处理用户请求")
        assert "entity" in result
        assert "property" in result
        assert "action" in result
        # 至少识别出"助手""请求"等实体
        assert len(result["entity"]) > 0 or len(result["action"]) > 0

    def test_empty_string(self, retriever: Any) -> None:
        """空字符串应返回空列表。"""
        result = retriever._extract_epa("")
        assert result["entity"] == []
        assert result["property"] == []
        assert result["action"] == []

    def test_only_entities(self, retriever: Any) -> None:
        """纯实体文本应只提取实体。"""
        result = retriever._extract_epa("数据库 服务器 客户端")
        assert len(result["entity"]) >= 2  # 至少提取出名词

    def test_with_adjectives(self, retriever: Any) -> None:
        """含形容词的文本应提取属性。"""
        result = retriever._extract_epa("高性能的数据库系统")
        assert "property" in result

    def test_english_text(self, retriever: Any) -> None:
        """英文文本也应能提取 EPA。"""
        result = retriever._extract_epa("The fast server processes user requests")
        assert isinstance(result["entity"], list)
        assert isinstance(result["action"], list)

    def test_chinese_with_numbers(self, retriever: Any) -> None:
        """中文含数字应作为属性提取。"""
        result = retriever._extract_epa("3个服务处理100个请求")
        # 数字应被识别为 property
        properties = result.get("property", [])
        assert any(
            any(ch.isdigit() for ch in p)
            for p in properties
        )


# ---------------------------------------------------------------------------
# 余弦相似度测试
# ---------------------------------------------------------------------------

class TestCosineSimilarity:
    """测试 _cosine_similarity 方法。"""

    @pytest.fixture()
    def retriever(self) -> Any:
        from memory.wave_retriever import WaveRetriever

        return WaveRetriever()

    def test_identical_vectors(self, retriever: Any) -> None:
        """相同向量的相似度应为 1.0。"""
        vec = [1.0, 2.0, 3.0]
        sim = retriever._cosine_similarity(vec, vec)
        assert abs(sim - 1.0) < 1e-6

    def test_orthogonal_vectors(self, retriever: Any) -> None:
        """正交向量的相似度应为 0.0。"""
        vec_a = [1.0, 0.0]
        vec_b = [0.0, 1.0]
        sim = retriever._cosine_similarity(vec_a, vec_b)
        assert abs(sim) < 1e-6

    def test_opposite_vectors(self, retriever: Any) -> None:
        """相反向量的相似度应为 -1.0。"""
        vec_a = [1.0, 0.0]
        vec_b = [-1.0, 0.0]
        sim = retriever._cosine_similarity(vec_a, vec_b)
        assert abs(sim - (-1.0)) < 1e-6

    def test_zero_vector(self, retriever: Any) -> None:
        """零向量应返回 0.0 避免除零。"""
        vec_a = [0.0, 0.0, 0.0]
        vec_b = [1.0, 2.0, 3.0]
        sim = retriever._cosine_similarity(vec_a, vec_b)
        assert sim == 0.0

    def test_different_dimensions(self, retriever: Any) -> None:
        """不同维度向量应返回 0.0。"""
        vec_a = [1.0, 2.0]
        vec_b = [1.0, 2.0, 3.0]
        sim = retriever._cosine_similarity(vec_a, vec_b)
        assert sim == 0.0


# ---------------------------------------------------------------------------
# 残差金字塔测试
# ---------------------------------------------------------------------------

class TestResidualPyramid:
    """测试 _residual_pyramid 方法。"""

    @pytest.fixture()
    def retriever_with_items(self) -> Any:
        """创建带知识条目的 WaveRetriever。"""
        from memory.wave_retriever import WaveRetriever

        items = [
            _make_item("1", "数据库查询优化技巧", tags=["数据库", "优化"],
                       embedding=[0.9, 0.1, 0.0]),
            _make_item("2", "Web服务器性能调优", tags=["服务器", "性能"],
                       embedding=[0.1, 0.9, 0.0]),
            _make_item("3", "缓存策略提升响应速度", tags=["缓存", "性能"],
                       embedding=[0.5, 0.5, 0.0]),
            _make_item("4", "完全不相关的烹饪食谱", tags=["烹饪"],
                       embedding=[0.0, 0.0, 1.0]),
        ]
        async def provider() -> list[dict[str, Any]]:
            return items

        return WaveRetriever(knowledge_items_provider=provider)

    @pytest.mark.asyncio
    async def test_returns_search_results(
        self, retriever_with_items: Any,
    ) -> None:
        """残差金字塔应返回 SearchResult 列表。"""
        items = await retriever_with_items._knowledge_items_provider()
        results = retriever_with_items._residual_pyramid("数据库查询", items)
        assert isinstance(results, list)
        if results:
            assert isinstance(results[0], SearchResult)

    @pytest.mark.asyncio
    async def test_scores_ordered(self, retriever_with_items: Any) -> None:
        """返回结果应按分数降序排列。"""
        items = await retriever_with_items._knowledge_items_provider()
        results = retriever_with_items._residual_pyramid("数据库优化", items)
        scores = [r.score for r in results]
        assert scores == sorted(scores, reverse=True)

    @pytest.mark.asyncio
    async def test_empty_candidates(self, retriever_with_items: Any) -> None:
        """空候选集应返回空列表。"""
        results = retriever_with_items._residual_pyramid("查询", [])
        assert results == []


# ---------------------------------------------------------------------------
# 浪潮扩散测试
# ---------------------------------------------------------------------------

class TestWaveDiffusion:
    """测试 _wave_diffusion 方法。"""

    @pytest.fixture()
    def retriever_with_graph(self) -> Any:
        """创建带知识图谱的 WaveRetriever。"""
        from memory.wave_retriever import WaveRetriever

        items = [
            _make_item("1", "Python基础", tags=["编程", "Python"],
                       related_ids=["2", "3"]),
            _make_item("2", "Python高级特性", tags=["编程", "Python"],
                       related_ids=["1", "3"]),
            _make_item("3", "装饰器模式", tags=["设计模式", "Python"],
                       related_ids=["1", "2"]),
            _make_item("4", "Java基础", tags=["编程", "Java"],
                       related_ids=["5"]),
            _make_item("5", "Spring框架", tags=["框架", "Java"],
                       related_ids=["4"]),
        ]
        async def provider() -> list[dict[str, Any]]:
            return items

        config = {"max_hops": 3, "decay_factor": 0.7}
        retriever = WaveRetriever(knowledge_items_provider=provider, config=config)
        return retriever

    @pytest.mark.asyncio
    async def test_diffusion_finds_related(
        self, retriever_with_graph: Any,
    ) -> None:
        """浪潮扩散应能发现关联条目。"""
        items = await retriever_with_graph._knowledge_items_provider()
        initial = [SearchResult(id="1", content="Python基础", score=0.9)]
        results = retriever_with_graph._wave_diffusion(initial, items)
        # 应包含初始结果
        result_ids = {r.id for r in results}
        assert "1" in result_ids
        # 通过关联应找到 2 或 3
        assert "2" in result_ids or "3" in result_ids

    @pytest.mark.asyncio
    async def test_decay_reduces_score(self, retriever_with_graph: Any) -> None:
        """远距离条目得分应低于近距离条目。"""
        items = await retriever_with_graph._knowledge_items_provider()
        initial = [SearchResult(id="1", content="Python基础", score=1.0)]
        results = retriever_with_graph._wave_diffusion(initial, items)
        # ID=1 的得分应最高
        score_map = {r.id: r.score for r in results}
        assert score_map.get("1", 0) >= score_map.get("2", 0)

    @pytest.mark.asyncio
    async def test_empty_initial(self, retriever_with_graph: Any) -> None:
        """空初始结果应返回空列表。"""
        items = await retriever_with_graph._knowledge_items_provider()
        results = retriever_with_graph._wave_diffusion([], items)
        assert results == []


# ---------------------------------------------------------------------------
# 霰弹枪检索测试
# ---------------------------------------------------------------------------

class TestShotgunRetrieve:
    """测试 _shotgun_retrieve 方法。"""

    @pytest.fixture()
    def retriever_with_items(self) -> Any:
        """创建带知识条目的 WaveRetriever。"""
        from memory.wave_retriever import WaveRetriever

        items = [
            _make_item("1", "机器学习算法优化", tags=["机器学习", "算法"]),
            _make_item("2", "深度学习模型训练技巧", tags=["深度学习", "模型"]),
            _make_item("3", "数据预处理流程", tags=["数据", "预处理"]),
        ]
        async def provider() -> list[dict[str, Any]]:
            return items

        return WaveRetriever(knowledge_items_provider=provider)

    @pytest.mark.asyncio
    async def test_multi_angle_results(
        self, retriever_with_items: Any,
    ) -> None:
        """霰弹枪检索应从多个角度返回结果。"""
        items = await retriever_with_items._knowledge_items_provider()
        results = retriever_with_items._shotgun_retrieve("机器学习训练", items)
        assert isinstance(results, list)
        # 至少应命中一条相关结果
        assert len(results) > 0

    @pytest.mark.asyncio
    async def test_deduplication(self, retriever_with_items: Any) -> None:
        """霰弹枪检索结果不应有重复 ID。"""
        items = await retriever_with_items._knowledge_items_provider()
        results = retriever_with_items._shotgun_retrieve("学习", items)
        ids = [r.id for r in results]
        assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# 结果合并测试
# ---------------------------------------------------------------------------

class TestMergeResults:
    """测试 _merge_results 方法。"""

    @pytest.fixture()
    def retriever(self) -> Any:
        from memory.wave_retriever import WaveRetriever

        return WaveRetriever()

    def test_merge_deduplicates(self, retriever: Any) -> None:
        """合并应按 ID 去重。"""
        group1 = [
            SearchResult(id="1", content="A", score=0.8),
            SearchResult(id="2", content="B", score=0.6),
        ]
        group2 = [
            SearchResult(id="1", content="A", score=0.9),
            SearchResult(id="3", content="C", score=0.7),
        ]
        merged = retriever._merge_results([group1, group2])
        ids = [r.id for r in merged]
        assert len(ids) == len(set(ids))
        assert set(ids) == {"1", "2", "3"}

    def test_merge_uses_highest_score(self, retriever: Any) -> None:
        """重复 ID 应使用最高得分。"""
        group1 = [SearchResult(id="1", content="A", score=0.5)]
        group2 = [SearchResult(id="1", content="A", score=0.9)]
        merged = retriever._merge_results([group1, group2])
        assert len(merged) == 1
        assert merged[0].score == 0.9

    def test_merge_ordered_by_score(self, retriever: Any) -> None:
        """合并结果应按得分降序排列。"""
        groups = [
            [SearchResult(id="1", content="A", score=0.3)],
            [SearchResult(id="2", content="B", score=0.8)],
            [SearchResult(id="3", content="C", score=0.5)],
        ]
        merged = retriever._merge_results(groups)
        scores = [r.score for r in merged]
        assert scores == sorted(scores, reverse=True)

    def test_merge_empty_groups(self, retriever: Any) -> None:
        """空组合并应返回空列表。"""
        merged = retriever._merge_results([[], []])
        assert merged == []


# ---------------------------------------------------------------------------
# retrieve 主方法端到端测试
# ---------------------------------------------------------------------------

class TestRetrieve:
    """测试 retrieve 主方法。"""

    @pytest.fixture()
    def retriever_full(self) -> Any:
        """创建完整的 WaveRetriever。"""
        from memory.wave_retriever import WaveRetriever

        items = [
            _make_item(
                "k1", "Python异步编程最佳实践",
                tags=["Python", "异步", "编程"],
                embedding=[0.8, 0.2, 0.0],
                epa={"entity": ["Python", "编程"], "property": ["异步"], "action": ["实践"]},
                related_ids=["k2"],
            ),
            _make_item(
                "k2", "asyncio事件循环详解",
                tags=["Python", "asyncio", "异步"],
                embedding=[0.7, 0.3, 0.0],
                epa={"entity": ["asyncio", "事件循环"], "action": ["详解"]},
                related_ids=["k1", "k3"],
            ),
            _make_item(
                "k3", "Python并发编程模式",
                tags=["Python", "并发", "编程"],
                embedding=[0.6, 0.4, 0.0],
                epa={"entity": ["Python", "并发", "编程"], "action": ["编程"]},
                related_ids=["k2"],
            ),
            _make_item(
                "k4", "Java多线程开发指南",
                tags=["Java", "多线程"],
                embedding=[0.1, 0.1, 0.9],
                epa={"entity": ["Java", "多线程"], "action": ["开发"]},
                related_ids=[],
            ),
        ]
        async def provider() -> list[dict[str, Any]]:
            return items

        return WaveRetriever(
            knowledge_items_provider=provider,
            config={"min_score": 0.05},
        )

    @pytest.mark.asyncio
    async def test_returns_search_results(
        self, retriever_full: Any,
    ) -> None:
        """retrieve 应返回 SearchResult 列表。"""
        results = await retriever_full.retrieve("Python异步编程")
        assert isinstance(results, list)
        assert len(results) > 0
        assert all(isinstance(r, SearchResult) for r in results)

    @pytest.mark.asyncio
    async def test_relevant_results_first(
        self, retriever_full: Any,
    ) -> None:
        """相关结果应排在前面。"""
        results = await retriever_full.retrieve("Python异步编程")
        if len(results) >= 2:
            # Python 相关的应排在 Java 前面
            ids = [r.id for r in results]
            if "k4" in ids and "k1" in ids:
                assert ids.index("k1") < ids.index("k4")

    @pytest.mark.asyncio
    async def test_top_k_limit(self, retriever_full: Any) -> None:
        """top_k 应限制返回数量。"""
        results = await retriever_full.retrieve("Python编程", top_k=2)
        assert len(results) <= 2

    @pytest.mark.asyncio
    async def test_min_score_filter(self, retriever_full: Any) -> None:
        """低于 min_score 的结果应被过滤。"""
        results = await retriever_full.retrieve("Python异步编程")
        for r in results:
            assert r.score >= 0.05

    @pytest.mark.asyncio
    async def test_empty_query(self, retriever_full: Any) -> None:
        """空查询应返回空列表。"""
        results = await retriever_full.retrieve("")
        assert results == []

    @pytest.mark.asyncio
    async def test_no_items_provider(self) -> None:
        """无 knowledge_items_provider 应返回空列表。"""
        from memory.wave_retriever import WaveRetriever

        retriever = WaveRetriever()
        results = await retriever.retrieve("任何查询")
        assert results == []

    @pytest.mark.asyncio
    async def test_memory_type_passed(self, retriever_full: Any) -> None:
        """memory_type 应传递到结果中。"""
        results = await retriever_full.retrieve(
            "Python异步编程", memory_type="episode",
        )
        # 结果应标记为对应 memory_type
        if results:
            assert results[0].memory_type == MemoryType.EPISODE


# ---------------------------------------------------------------------------
# 配置测试
# ---------------------------------------------------------------------------

class TestWaveRetrieverConfig:
    """测试 WaveRetrieverConfig。"""

    def test_default_config(self) -> None:
        """默认配置应有合理值。"""
        from memory.wave_retriever import WaveRetrieverConfig

        config = WaveRetrieverConfig()
        assert config.max_hops == 3
        assert config.decay_factor == 0.7
        assert config.shotgun_angles == 3
        assert config.epa_weight == 0.3
        assert config.residual_weight == 0.3
        assert config.wave_weight == 0.2
        assert config.shotgun_weight == 0.2
        assert config.min_score == 0.1

    def test_custom_config(self) -> None:
        """自定义配置应覆盖默认值。"""
        from memory.wave_retriever import WaveRetrieverConfig

        config = WaveRetrieverConfig(max_hops=5, decay_factor=0.5)
        assert config.max_hops == 5
        assert config.decay_factor == 0.5

    def test_weights_sum_approximately_one(self) -> None:
        """各权重之和应接近 1.0。"""
        from memory.wave_retriever import WaveRetrieverConfig

        config = WaveRetrieverConfig()
        total = (
            config.epa_weight
            + config.residual_weight
            + config.wave_weight
            + config.shotgun_weight
        )
        assert abs(total - 1.0) < 0.01


# ---------------------------------------------------------------------------
# IRetriever 接口兼容性测试
# ---------------------------------------------------------------------------

class TestIRetrieverInterface:
    """测试 WaveRetriever 是否正确实现 IRetriever 接口。"""

    def test_implements_interface(self) -> None:
        """WaveRetriever 应是 IRetriever 的子类。"""
        from memory.ports import IRetriever
        from memory.wave_retriever import WaveRetriever

        assert issubclass(WaveRetriever, IRetriever)

    def test_instance_check(self) -> None:
        """WaveRetriever 实例应通过 isinstance 检查。"""
        from memory.ports import IRetriever
        from memory.wave_retriever import WaveRetriever

        retriever = WaveRetriever()
        assert isinstance(retriever, IRetriever)
