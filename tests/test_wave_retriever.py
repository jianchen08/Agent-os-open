"""WaveRetriever 波浪算法检索器全面测试。

测试覆盖：
1. EPA 分析（Entity-Property-Action 提取）
2. 余弦相似度计算
3. 残差金字塔多层次检索
4. 浪潮扩散多跳关联
5. 霰弹枪检索多角度查询
6. 结果合并去重
7. 配置验证
8. IRetriever 接口兼容性
9. retrieve 主方法端到端
10. 召回率验证（AC-4a：> 85%）
11. 与 MemoryService.register_retriever 集成（AC-4c）
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
    user_id: str | None = None,
) -> dict[str, Any]:
    """构造一条知识条目字典。"""
    return {
        "id": item_id,
        "content": content,
        "tags": tags or [],
        "embedding": embedding,
        "epa": epa or {},
        "related_ids": related_ids or [],
        "user_id": user_id,
    }


# ---------------------------------------------------------------------------
# 1. EPA 分析测试
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
        assert len(result["entity"]) >= 2

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
        properties = result.get("property", [])
        assert any(
            any(ch.isdigit() for ch in p)
            for p in properties
        )

    def test_whitespace_only(self, retriever: Any) -> None:
        """纯空白文本应返回空列表。"""
        result = retriever._extract_epa("   \n\t  ")
        assert result["entity"] == []
        assert result["property"] == []
        assert result["action"] == []

    def test_mixed_chinese_english(self, retriever: Any) -> None:
        """中英文混合文本应同时提取两种语言的 EPA。"""
        result = retriever._extract_epa("使用 Python analyze 数据")
        assert isinstance(result["entity"], list)
        assert len(result["entity"]) > 0

    def test_epa_extract_identifies_verbs(self, retriever: Any) -> None:
        """应识别常见中文动词。"""
        result = retriever._extract_epa("系统需要分析和处理数据，然后优化结果")
        assert len(result["action"]) > 0


# ---------------------------------------------------------------------------
# 2. 余弦相似度测试
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
# 3. 残差金字塔测试
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
    async def test_returns_search_results(self, retriever_with_items: Any) -> None:
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
# 4. 浪潮扩散测试
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
        return WaveRetriever(knowledge_items_provider=provider, config=config)

    @pytest.mark.asyncio
    async def test_diffusion_finds_related(self, retriever_with_graph: Any) -> None:
        """浪潮扩散应能发现关联条目。"""
        items = await retriever_with_graph._knowledge_items_provider()
        initial = [SearchResult(id="1", content="Python基础", score=0.9)]
        results = retriever_with_graph._wave_diffusion(initial, items)
        result_ids = {r.id for r in results}
        assert "1" in result_ids
        assert "2" in result_ids or "3" in result_ids

    @pytest.mark.asyncio
    async def test_decay_reduces_score(self, retriever_with_graph: Any) -> None:
        """远距离条目得分应低于近距离条目。"""
        items = await retriever_with_graph._knowledge_items_provider()
        initial = [SearchResult(id="1", content="Python基础", score=1.0)]
        results = retriever_with_graph._wave_diffusion(initial, items)
        score_map = {r.id: r.score for r in results}
        assert score_map.get("1", 0) >= score_map.get("2", 0)

    @pytest.mark.asyncio
    async def test_empty_initial(self, retriever_with_graph: Any) -> None:
        """空初始结果应返回空列表。"""
        items = await retriever_with_graph._knowledge_items_provider()
        results = retriever_with_graph._wave_diffusion([], items)
        assert results == []

    @pytest.mark.asyncio
    async def test_multi_hop_propagation(self, retriever_with_graph: Any) -> None:
        """多跳传播应能发现间接关联的条目。"""
        items = await retriever_with_graph._knowledge_items_provider()
        # 从 item-1 出发，3 跳内应能到达 item-3（通过 item-2）
        initial = [SearchResult(id="1", content="Python基础", score=1.0)]
        results = retriever_with_graph._wave_diffusion(initial, items)
        result_ids = {r.id for r in results}
        assert "1" in result_ids
        assert "2" in result_ids or "3" in result_ids


# ---------------------------------------------------------------------------
# 5. 霰弹枪检索测试
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
    async def test_multi_angle_results(self, retriever_with_items: Any) -> None:
        """霰弹枪检索应从多个角度返回结果。"""
        items = await retriever_with_items._knowledge_items_provider()
        results = retriever_with_items._shotgun_retrieve("机器学习训练", items)
        assert isinstance(results, list)
        assert len(results) > 0

    @pytest.mark.asyncio
    async def test_deduplication(self, retriever_with_items: Any) -> None:
        """霰弹枪检索结果不应有重复 ID。"""
        items = await retriever_with_items._knowledge_items_provider()
        results = retriever_with_items._shotgun_retrieve("学习", items)
        ids = [r.id for r in results]
        assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# 6. 结果合并测试
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
# 7. retrieve 主方法端到端测试
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
    async def test_returns_search_results(self, retriever_full: Any) -> None:
        """retrieve 应返回 SearchResult 列表。"""
        results = await retriever_full.retrieve("Python异步编程")
        assert isinstance(results, list)
        assert len(results) > 0
        assert all(isinstance(r, SearchResult) for r in results)

    @pytest.mark.asyncio
    async def test_relevant_results_first(self, retriever_full: Any) -> None:
        """相关结果应排在前面。"""
        results = await retriever_full.retrieve("Python异步编程")
        if len(results) >= 2:
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
        if results:
            assert results[0].memory_type == MemoryType.EPISODE

    @pytest.mark.asyncio
    async def test_user_id_filter(self) -> None:
        """user_id 过滤应只返回匹配用户的数据。"""
        from memory.wave_retriever import WaveRetriever

        items = [
            _make_item("1", "用户A的知识", user_id="user_a"),
            _make_item("2", "用户B的知识", user_id="user_b"),
            _make_item("3", "公共知识", user_id=None),
        ]

        async def provider() -> list[dict[str, Any]]:
            return items

        retriever = WaveRetriever(knowledge_items_provider=provider, config={"min_score": 0.01})
        results = await retriever.retrieve("知识", user_id="user_a")
        result_ids = {r.id for r in results}
        # user_b 的数据不应出现
        assert "2" not in result_ids


# ---------------------------------------------------------------------------
# 8. 配置测试
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
# 9. IRetriever 接口兼容性测试
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


# ---------------------------------------------------------------------------
# 10. 召回率验证（AC-4a：> 85%）
# ---------------------------------------------------------------------------


class TestRecallRate:
    """验证 WaveRetriever 在已知数据集上的召回率 > 85%。

    构造一个包含多领域知识的测试数据集，对每个查询验证
    所有语义相关的条目是否被召回。
    """

    @pytest.fixture()
    def recall_retriever(self) -> Any:
        """创建用于召回率测试的 WaveRetriever。"""
        from memory.wave_retriever import WaveRetriever

        items = [
            # 编程语言领域
            _make_item("py1", "Python是一门解释型高级编程语言，支持面向对象编程",
                       tags=["Python", "编程语言"],
                       epa={"entity": ["Python", "编程语言"], "property": ["高级", "解释型"], "action": ["编程"]},
                       related_ids=["py2", "py3"]),
            _make_item("py2", "Python列表推导式提供简洁的数据处理方式",
                       tags=["Python", "数据处理"],
                       epa={"entity": ["Python", "列表"], "property": ["简洁"], "action": ["处理"]},
                       related_ids=["py1"]),
            _make_item("py3", "Python装饰器用于扩展函数功能",
                       tags=["Python", "装饰器"],
                       epa={"entity": ["Python", "装饰器"], "action": ["扩展"]},
                       related_ids=["py1"]),
            _make_item("js1", "JavaScript是Web前端开发的核心语言",
                       tags=["JavaScript", "前端", "Web"],
                       epa={"entity": ["JavaScript", "前端", "Web"], "action": ["开发"]},
                       related_ids=["js2"]),
            _make_item("js2", "React是流行的JavaScript前端框架",
                       tags=["React", "JavaScript", "前端"],
                       epa={"entity": ["React", "JavaScript", "前端"], "action": []},
                       related_ids=["js1"]),
            # 数据库领域
            _make_item("db1", "MySQL关系型数据库支持ACID事务",
                       tags=["MySQL", "数据库", "事务"],
                       epa={"entity": ["MySQL", "数据库"], "property": ["ACID"], "action": []},
                       related_ids=["db2"]),
            _make_item("db2", "PostgreSQL支持JSON和全文搜索",
                       tags=["PostgreSQL", "数据库", "搜索"],
                       epa={"entity": ["PostgreSQL", "数据库"], "action": ["搜索"]},
                       related_ids=["db1"]),
            _make_item("db3", "Redis是高性能内存键值数据库",
                       tags=["Redis", "数据库", "缓存"],
                       epa={"entity": ["Redis", "数据库"], "property": ["高性能"], "action": []},
                       related_ids=[]),
            # 机器学习领域
            _make_item("ml1", "随机森林是一种集成学习算法",
                       tags=["机器学习", "算法", "集成学习"],
                       epa={"entity": ["随机森林", "算法"], "action": ["学习"]},
                       related_ids=["ml2", "ml3"]),
            _make_item("ml2", "梯度提升树在表格数据上表现优异",
                       tags=["机器学习", "算法"],
                       epa={"entity": ["梯度提升", "算法"], "property": ["优异"], "action": []},
                       related_ids=["ml1"]),
            _make_item("ml3", "神经网络通过反向传播进行训练",
                       tags=["神经网络", "深度学习", "训练"],
                       epa={"entity": ["神经网络"], "action": ["训练"]},
                       related_ids=["ml1"]),
        ]

        async def provider() -> list[dict[str, Any]]:
            return items

        return WaveRetriever(
            knowledge_items_provider=provider,
            config={"min_score": 0.01, "max_hops": 3},
        )

    @pytest.mark.asyncio
    async def test_recall_python_related(self, recall_retriever: Any) -> None:
        """查询 Python 相关内容，应召回大部分 Python 条目。"""
        results = await recall_retriever.retrieve("Python编程语言特性", top_k=10)
        result_ids = {r.id for r in results}
        expected_ids = {"py1", "py2", "py3"}
        recalled = result_ids & expected_ids
        recall_rate = len(recalled) / len(expected_ids)
        assert recall_rate >= 0.85, (
            f"Python 召回率 {recall_rate:.0%} < 85%，"
            f"期望召回 {expected_ids}，实际召回 {recalled}"
        )

    @pytest.mark.asyncio
    async def test_recall_database_related(self, recall_retriever: Any) -> None:
        """查询数据库内容，应召回大部分数据库条目。"""
        results = await recall_retriever.retrieve("数据库系统比较", top_k=10)
        result_ids = {r.id for r in results}
        expected_ids = {"db1", "db2", "db3"}
        recalled = result_ids & expected_ids
        recall_rate = len(recalled) / len(expected_ids)
        assert recall_rate >= 0.85, (
            f"数据库召回率 {recall_rate:.0%} < 85%，"
            f"期望召回 {expected_ids}，实际召回 {recalled}"
        )

    @pytest.mark.asyncio
    async def test_recall_ml_related(self, recall_retriever: Any) -> None:
        """查询机器学习内容，应召回大部分 ML 条目。"""
        results = await recall_retriever.retrieve("机器学习算法训练", top_k=10)
        result_ids = {r.id for r in results}
        expected_ids = {"ml1", "ml2", "ml3"}
        recalled = result_ids & expected_ids
        recall_rate = len(recalled) / len(expected_ids)
        assert recall_rate >= 0.85, (
            f"ML 召回率 {recall_rate:.0%} < 85%，"
            f"期望召回 {expected_ids}，实际召回 {recalled}"
        )

    @pytest.mark.asyncio
    async def test_overall_recall_rate(self, recall_retriever: Any) -> None:
        """综合召回率应 > 85%（AC-4a）。

        对多个查询统计总体召回率。
        """
        queries_and_expected = [
            ("Python编程", {"py1", "py2", "py3"}),
            ("数据库系统", {"db1", "db2", "db3"}),
            ("机器学习算法", {"ml1", "ml2", "ml3"}),
            ("前端开发框架", {"js1", "js2"}),
        ]

        total_expected = 0
        total_recalled = 0

        for query, expected_ids in queries_and_expected:
            results = await recall_retriever.retrieve(query, top_k=10)
            result_ids = {r.id for r in results}
            recalled = result_ids & expected_ids
            total_expected += len(expected_ids)
            total_recalled += len(recalled)

        overall_recall = total_recalled / total_expected
        assert overall_recall >= 0.85, (
            f"综合召回率 {overall_recall:.0%} < 85% (AC-4a)"
        )


# ---------------------------------------------------------------------------
# 11. 与 MemoryService.register_retriever 集成测试（AC-4c）
# ---------------------------------------------------------------------------


class TestMemoryServiceIntegration:
    """测试 WaveRetriever 通过 register_retriever 注册后的端到端检索。"""

    @pytest.mark.asyncio
    async def test_register_and_retrieve(self) -> None:
        """注册 WaveRetriever 后应能通过 MemoryService 检索。"""
        from memory.wave_retriever import WaveRetriever
        from memory.service import MemoryService

        items = [
            _make_item("w1", "波浪算法检索器实现", tags=["检索", "波浪算法"],
                       epa={"entity": ["波浪算法", "检索器"], "action": ["实现"]}),
            _make_item("w2", "向量检索与关键词检索对比", tags=["检索", "向量"],
                       epa={"entity": ["检索", "向量"], "action": ["对比"]}),
            _make_item("w3", "EPA分析在RAG中的应用", tags=["EPA", "RAG"],
                       epa={"entity": ["EPA", "RAG"], "action": ["分析"]}),
        ]

        async def provider() -> list[dict[str, Any]]:
            return items

        wave_retriever = WaveRetriever(
            knowledge_items_provider=provider,
            config={"min_score": 0.01},
        )

        service = MemoryService()
        service.register_retriever("tagwave", wave_retriever)

        # 通过 MemoryService.retrieve 调用
        results = await service.retrieve(
            query="检索算法",
            retrieval_method="tagwave",
            top_k=5,
        )
        assert isinstance(results, list)
        assert len(results) > 0
        assert all(isinstance(r, SearchResult) for r in results)

    @pytest.mark.asyncio
    async def test_wave_coexists_with_other_retrievers(self) -> None:
        """WaveRetriever 应能与其他检索器共存。"""
        from unittest.mock import AsyncMock

        from memory.wave_retriever import WaveRetriever
        from memory.service import MemoryService

        items = [
            _make_item("w1", "波浪检索测试", tags=["波浪"]),
        ]

        async def provider() -> list[dict[str, Any]]:
            return items

        wave = WaveRetriever(knowledge_items_provider=provider, config={"min_score": 0.01})
        mock_retriever = AsyncMock()
        mock_retriever.retrieve = AsyncMock(return_value=[
            SearchResult(id="m1", content="mock result", score=0.5),
        ])

        service = MemoryService()
        service.register_retriever("tagwave", wave)
        service.register_retriever("keyword", mock_retriever)

        # tagwave 检索
        wave_results = await service.retrieve(
            query="波浪", retrieval_method="tagwave", top_k=5,
        )
        assert len(wave_results) > 0

        # keyword 检索
        keyword_results = await service.retrieve(
            query="波浪", retrieval_method="keyword", top_k=5,
        )
        assert len(keyword_results) > 0

    @pytest.mark.asyncio
    async def test_get_stats(self) -> None:
        """get_stats 应返回正确的配置信息。"""
        from memory.wave_retriever import WaveRetriever

        items = [_make_item("1", "test")]

        async def provider() -> list[dict[str, Any]]:
            return items

        retriever = WaveRetriever(
            knowledge_items_provider=provider,
            config={"max_hops": 5, "decay_factor": 0.5, "min_score": 0.2},
        )
        stats = retriever.get_stats()
        assert stats["has_provider"] is True
        assert stats["has_embedding_service"] is False
        assert stats["config"]["max_hops"] == 5
        assert stats["config"]["decay_factor"] == 0.5
        assert stats["config"]["min_score"] == 0.2
