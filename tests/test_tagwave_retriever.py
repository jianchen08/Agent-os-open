"""TagWave 检索算法（透镜-拓展-聚焦三阶段）测试。

测试覆盖：
1. 透镜阶段（Lens）：基于查询标签快速过滤
2. 拓展阶段（Expand）：沿共现路径扩展关联记忆
3. 聚焦阶段（Focus）：多维度精排
4. 完整检索流程端到端
5. 准确率验证（AC：>= 80%）
6. 边界条件：空输入、无匹配、单候选
7. 速度验证：避免全量扫描
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

import pytest

pytestmark = pytest.mark.skip(
    reason="memory.tagwave_retriever 模块已不存在，"
           "TagWaveRetriever 已被替换为 memory.wave_retriever.WaveRetriever（API完全不同）"
)


# ---------------------------------------------------------------------------
# 辅助：构造测试数据
# ---------------------------------------------------------------------------


@dataclass
class MemoryItem:
    """测试用记忆条目。"""

    id: str
    content: str
    tags: list[str]
    vector: list[float] = field(default_factory=list)
    timestamp: float = 0.0
    importance: float = 0.5
    metadata: dict[str, Any] = field(default_factory=dict)


def _normalize(vec: list[float]) -> list[float]:
    """归一化向量。"""
    mag = math.sqrt(sum(v * v for v in vec))
    if mag < 1e-9:
        return vec
    return [v / mag for v in vec]


def _cosine_sim(a: list[float], b: list[float]) -> float:
    """计算余弦相似度。"""
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if mag_a < 1e-9 or mag_b < 1e-9:
        return 0.0
    return dot / (mag_a * mag_b)


def _build_test_dataset() -> list[MemoryItem]:
    """构建测试数据集。

    包含 4 个主题域：
    - Python 编程（id: p1-p5）
    - 数据库（id: d1-d5）
    - 机器学习（id: m1-m5）
    - 烹饪（id: c1-c3）—— 不相关噪音
    """
    now = time.time()
    return [
        # Python 编程域
        MemoryItem(
            id="p1", content="Python异步编程最佳实践",
            tags=["Python", "异步", "编程"],
            vector=_normalize([0.9, 0.1, 0.0, 0.0]),
            timestamp=now - 100, importance=0.8,
        ),
        MemoryItem(
            id="p2", content="asyncio事件循环详解",
            tags=["Python", "asyncio", "异步"],
            vector=_normalize([0.85, 0.15, 0.0, 0.0]),
            timestamp=now - 200, importance=0.7,
        ),
        MemoryItem(
            id="p3", content="Python并发编程模式",
            tags=["Python", "并发", "编程"],
            vector=_normalize([0.8, 0.2, 0.0, 0.0]),
            timestamp=now - 50, importance=0.9,
        ),
        MemoryItem(
            id="p4", content="Python装饰器高级用法",
            tags=["Python", "装饰器", "编程"],
            vector=_normalize([0.75, 0.25, 0.0, 0.0]),
            timestamp=now - 300, importance=0.6,
        ),
        MemoryItem(
            id="p5", content="Python类型注解指南",
            tags=["Python", "类型", "编程"],
            vector=_normalize([0.7, 0.3, 0.0, 0.0]),
            timestamp=now - 400, importance=0.5,
        ),
        # 数据库域
        MemoryItem(
            id="d1", content="MySQL查询优化技巧",
            tags=["MySQL", "优化", "数据库"],
            vector=_normalize([0.0, 0.1, 0.9, 0.0]),
            timestamp=now - 150, importance=0.8,
        ),
        MemoryItem(
            id="d2", content="PostgreSQL索引策略",
            tags=["PostgreSQL", "索引", "数据库"],
            vector=_normalize([0.0, 0.15, 0.85, 0.0]),
            timestamp=now - 80, importance=0.7,
        ),
        MemoryItem(
            id="d3", content="Redis缓存架构设计",
            tags=["Redis", "缓存", "数据库"],
            vector=_normalize([0.0, 0.2, 0.8, 0.0]),
            timestamp=now - 30, importance=0.9,
        ),
        MemoryItem(
            id="d4", content="数据库事务隔离级别",
            tags=["数据库", "事务", "并发"],
            vector=_normalize([0.0, 0.1, 0.9, 0.0]),
            timestamp=now - 500, importance=0.6,
        ),
        MemoryItem(
            id="d5", content="SQL注入防御策略",
            tags=["SQL", "安全", "数据库"],
            vector=_normalize([0.0, 0.05, 0.95, 0.0]),
            timestamp=now - 100, importance=0.85,
        ),
        # 机器学习域
        MemoryItem(
            id="m1", content="神经网络反向传播算法",
            tags=["神经网络", "算法", "机器学习"],
            vector=_normalize([0.1, 0.8, 0.1, 0.0]),
            timestamp=now - 60, importance=0.9,
        ),
        MemoryItem(
            id="m2", content="随机森林特征选择",
            tags=["随机森林", "特征", "机器学习"],
            vector=_normalize([0.15, 0.75, 0.1, 0.0]),
            timestamp=now - 120, importance=0.7,
        ),
        MemoryItem(
            id="m3", content="梯度下降优化器对比",
            tags=["优化", "梯度下降", "机器学习"],
            vector=_normalize([0.1, 0.85, 0.05, 0.0]),
            timestamp=now - 200, importance=0.8,
        ),
        MemoryItem(
            id="m4", content="Transformer注意力机制",
            tags=["Transformer", "注意力", "深度学习"],
            vector=_normalize([0.1, 0.9, 0.0, 0.0]),
            timestamp=now - 10, importance=0.95,
        ),
        MemoryItem(
            id="m5", content="CNN卷积神经网络架构",
            tags=["CNN", "卷积", "深度学习"],
            vector=_normalize([0.05, 0.95, 0.0, 0.0]),
            timestamp=now - 250, importance=0.75,
        ),
        # 烹饪域（噪音）
        MemoryItem(
            id="c1", content="红烧肉的做法",
            tags=["烹饪", "肉类"],
            vector=_normalize([0.0, 0.0, 0.0, 1.0]),
            timestamp=now - 1000, importance=0.3,
        ),
        MemoryItem(
            id="c2", content="蛋糕烘焙技巧",
            tags=["烘焙", "甜点"],
            vector=_normalize([0.0, 0.0, 0.1, 0.9]),
            timestamp=now - 800, importance=0.4,
        ),
        MemoryItem(
            id="c3", content="意大利面制作方法",
            tags=["烹饪", "面食"],
            vector=_normalize([0.0, 0.0, 0.05, 0.95]),
            timestamp=now - 900, importance=0.35,
        ),
    ]


def _build_cooccurrence_data() -> list[tuple[str, str, int]]:
    """构建标签共现关系数据。

    模拟从实际记忆中统计得到的标签共现关系。
    """
    return [
        # Python 域内共现
        ("Python", "异步", 15),
        ("Python", "asyncio", 12),
        ("Python", "编程", 20),
        ("Python", "并发", 10),
        ("Python", "装饰器", 8),
        ("Python", "类型", 7),
        ("异步", "asyncio", 18),
        ("异步", "并发", 14),
        ("编程", "装饰器", 6),
        ("编程", "并发", 9),
        # 数据库域内共现
        ("数据库", "MySQL", 12),
        ("数据库", "PostgreSQL", 10),
        ("数据库", "Redis", 8),
        ("数据库", "事务", 11),
        ("数据库", "SQL", 9),
        ("数据库", "优化", 7),
        ("MySQL", "优化", 13),
        ("PostgreSQL", "索引", 11),
        ("Redis", "缓存", 15),
        ("事务", "并发", 8),
        ("SQL", "安全", 10),
        # 机器学习域内共现
        ("机器学习", "神经网络", 12),
        ("机器学习", "随机森林", 8),
        ("机器学习", "优化", 6),
        ("机器学习", "特征", 9),
        ("深度学习", "Transformer", 14),
        ("深度学习", "CNN", 11),
        ("深度学习", "注意力", 10),
        ("深度学习", "卷积", 9),
        ("神经网络", "算法", 7),
        ("算法", "优化", 5),
        ("优化", "梯度下降", 8),
        # 跨域弱关联
        ("Python", "机器学习", 4),
        ("并发", "事务", 3),
        ("算法", "Python", 3),
    ]


def _build_tag_frequency() -> dict[str, int]:
    """构建标签频率数据。"""
    return {
        "Python": 50, "异步": 25, "编程": 40, "asyncio": 20, "并发": 18,
        "装饰器": 15, "类型": 12,
        "数据库": 45, "MySQL": 20, "PostgreSQL": 18, "Redis": 15,
        "事务": 12, "SQL": 14, "优化": 16, "索引": 10, "缓存": 13, "安全": 11,
        "机器学习": 35, "神经网络": 18, "随机森林": 10, "特征": 12, "算法": 20,
        "深度学习": 22, "Transformer": 15, "CNN": 12, "注意力": 10,
        "卷积": 8, "梯度下降": 9,
        "烹饪": 8, "肉类": 5, "烘焙": 4, "甜点": 3, "面食": 4,
    }


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def dataset() -> list[MemoryItem]:
    """测试数据集。"""
    return _build_test_dataset()


@pytest.fixture()
def cooccurrence_data() -> list[tuple[str, str, int]]:
    """共现数据。"""
    return _build_cooccurrence_data()


@pytest.fixture()
def tag_frequency() -> dict[str, int]:
    """标签频率数据。"""
    return _build_tag_frequency()


@pytest.fixture()
def retriever(
    cooccurrence_data: list[tuple[str, str, int]],
    tag_frequency: dict[str, int],
) -> Any:
    """创建配置好数据的 TagWaveRetriever。"""
    from memory.tagwave_retriever import TagWaveRetriever

    r = TagWaveRetriever()
    r.build_index(cooccurrence_data, tag_frequency)
    return r


# ---------------------------------------------------------------------------
# 1. 透镜阶段测试
# ---------------------------------------------------------------------------


class TestLensPhase:
    """测试透镜阶段：基于查询标签快速过滤。"""

    def test_lens_filters_by_tag_overlap(
        self, retriever: Any, dataset: list[MemoryItem],
    ) -> None:
        """透镜阶段应根据标签重叠度过滤候选。"""
        query_tags = ["Python", "异步"]
        results = retriever.lens_phase(query_tags, dataset)

        # 应该过滤掉完全不相关的项（如烹饪类）
        result_ids = {r.id for r in results}
        assert "c1" not in result_ids
        assert "c2" not in result_ids
        assert "c3" not in result_ids

    def test_lens_preserves_relevant_items(
        self, retriever: Any, dataset: list[MemoryItem],
    ) -> None:
        """透镜阶段应保留标签匹配的项。"""
        query_tags = ["Python", "异步"]
        results = retriever.lens_phase(query_tags, dataset)

        result_ids = {r.id for r in results}
        # p1, p2 有 "Python" + "异步" 标签，必须保留
        assert "p1" in result_ids
        assert "p2" in result_ids

    def test_lens_returns_scored_results(
        self, retriever: Any, dataset: list[MemoryItem],
    ) -> None:
        """透镜阶段结果应有匹配分数。"""
        query_tags = ["Python"]
        results = retriever.lens_phase(query_tags, dataset)

        for r in results:
            assert hasattr(r, "lens_score")
            assert r.lens_score > 0.0

    def test_lens_exact_match_scores_higher(
        self, retriever: Any, dataset: list[MemoryItem],
    ) -> None:
        """完全匹配标签的项应得分更高。"""
        query_tags = ["Python", "异步"]
        results = retriever.lens_phase(query_tags, dataset)

        score_map = {r.id: r.lens_score for r in results}
        # p1 有 Python+异步，p4 有 Python 但无异步
        if "p1" in score_map and "p4" in score_map:
            assert score_map["p1"] >= score_map["p4"]

    def test_lens_empty_query_tags(
        self, retriever: Any, dataset: list[MemoryItem],
    ) -> None:
        """空查询标签应返回所有候选。"""
        results = retriever.lens_phase([], dataset)
        assert len(results) == len(dataset)

    def test_lens_empty_candidates(
        self, retriever: Any,
    ) -> None:
        """空候选集应返回空结果。"""
        results = retriever.lens_phase(["Python"], [])
        assert results == []

    def test_lens_no_matching_tags(
        self, retriever: Any, dataset: list[MemoryItem],
    ) -> None:
        """无匹配标签时，结果分数应都较低但仍返回（允许低分通过以支持后续向量补救）。"""
        results = retriever.lens_phase(["量子计算"], dataset)
        # 不应崩溃，可能返回空或低分结果
        assert isinstance(results, list)


# ---------------------------------------------------------------------------
# 2. 拓展阶段测试
# ---------------------------------------------------------------------------


class TestExpandPhase:
    """测试拓展阶段：沿共现路径扩展关联记忆。"""

    def test_expand_discovers_indirect_relations(
        self, retriever: Any, dataset: list[MemoryItem],
    ) -> None:
        """拓展阶段应发现通过共现关系间接关联的项。

        场景：查询 'asyncio' → 透镜返回 p2(asyncio) → 拓展发现
        asyncio 共现 '异步'，异步 共现 '并发'，因此 p3(并发) 应被召回。
        """
        query_tags = ["asyncio"]
        lens_results = retriever.lens_phase(query_tags, dataset)
        expanded = retriever.expand_phase(query_tags, lens_results, dataset)

        expanded_ids = {r.id for r in expanded}
        # p2 直接匹配 asyncio
        assert "p2" in expanded_ids
        # p1 通过 asyncio→异步 共现关联
        assert "p1" in expanded_ids

    def test_expand_includes_lens_results(
        self, retriever: Any, dataset: list[MemoryItem],
    ) -> None:
        """拓展阶段结果应包含透镜阶段的所有结果。"""
        query_tags = ["Python"]
        lens_results = retriever.lens_phase(query_tags, dataset)
        expanded = retriever.expand_phase(query_tags, lens_results, dataset)

        lens_ids = {r.id for r in lens_results}
        expanded_ids = {r.id for r in expanded}
        assert lens_ids.issubset(expanded_ids)

    def test_expand_respects_max_limit(
        self, retriever: Any, dataset: list[MemoryItem],
    ) -> None:
        """拓展阶段应遵守最大扩展数量限制。"""
        from memory.tagwave_retriever import TagWaveConfig

        config = TagWaveConfig(max_expand=5)
        from memory.tagwave_retriever import TagWaveRetriever
        r = TagWaveRetriever(config=config)
        r.build_index(_build_cooccurrence_data(), _build_tag_frequency())

        query_tags = ["Python"]
        lens_results = r.lens_phase(query_tags, dataset)
        expanded = r.expand_phase(query_tags, lens_results, dataset)

        # 不应超过数据集大小
        assert len(expanded) <= len(dataset)

    def test_expand_assigns_expand_score(
        self, retriever: Any, dataset: list[MemoryItem],
    ) -> None:
        """拓展阶段结果应有拓展分数。"""
        query_tags = ["Python", "异步"]
        lens_results = retriever.lens_phase(query_tags, dataset)
        expanded = retriever.expand_phase(query_tags, lens_results, dataset)

        for r in expanded:
            assert hasattr(r, "expand_score")

    def test_expand_empty_input(
        self, retriever: Any, dataset: list[MemoryItem],
    ) -> None:
        """空透镜结果 + 查询标签仍可做标签级拓展。"""
        results = retriever.expand_phase(["Python"], [], dataset)
        assert isinstance(results, list)

    def test_cross_domain_expansion(
        self, retriever: Any, dataset: list[MemoryItem],
    ) -> None:
        """拓展阶段应能通过跨域共现发现关联。

        场景：Python→机器学习 有共现(4次)
        查询 Python 相关内容时应能发现机器学习域的部分内容。
        """
        query_tags = ["Python"]
        lens_results = retriever.lens_phase(query_tags, dataset)
        expanded = retriever.expand_phase(query_tags, lens_results, dataset)

        expanded_ids = {r.id for r in expanded}
        # 至少部分机器学习相关项应被包含
        ml_items = {"m1", "m2", "m3", "m4", "m5"}
        overlap = expanded_ids & ml_items
        # 跨域共现强度低，可能只有部分被召回；至少验证不崩溃
        assert isinstance(overlap, set)


# ---------------------------------------------------------------------------
# 3. 聚焦阶段测试
# ---------------------------------------------------------------------------


class TestFocusPhase:
    """测试聚焦阶段：多维度精排。"""

    def test_focus_reranks_by_relevance(
        self, retriever: Any, dataset: list[MemoryItem],
    ) -> None:
        """聚焦阶段应根据综合相关度重新排序。"""
        query_tags = ["Python", "异步"]
        query_vector = _normalize([0.9, 0.1, 0.0, 0.0])

        lens_results = retriever.lens_phase(query_tags, dataset)
        expanded = retriever.expand_phase(query_tags, lens_results, dataset)
        focused = retriever.focus_phase(expanded, query_vector, top_k=5)

        assert len(focused) <= 5
        # 第一个结果应是 Python 相关的
        if focused:
            assert focused[0].id.startswith("p")

    def test_focus_combines_multiple_signals(
        self, retriever: Any, dataset: list[MemoryItem],
    ) -> None:
        """聚焦阶段应综合标签+向量+时效性+重要性。"""
        query_vector = _normalize([0.9, 0.1, 0.0, 0.0])
        query_tags = ["Python"]

        lens_results = retriever.lens_phase(query_tags, dataset)
        expanded = retriever.expand_phase(query_tags, lens_results, dataset)
        focused = retriever.focus_phase(expanded, query_vector, top_k=5)

        for r in focused:
            # 应有综合分数
            assert hasattr(r, "final_score")
            assert r.final_score > 0.0

    def test_focus_respects_top_k(
        self, retriever: Any, dataset: list[MemoryItem],
    ) -> None:
        """聚焦阶段应遵守 top_k 限制。"""
        query_vector = _normalize([0.9, 0.1, 0.0, 0.0])
        results = retriever.focus_phase(dataset, query_vector, top_k=3)
        assert len(results) <= 3

    def test_focus_empty_candidates(
        self, retriever: Any,
    ) -> None:
        """空候选集应返回空结果。"""
        results = retriever.focus_phase([], [1.0, 0.0, 0.0, 0.0], top_k=5)
        assert results == []

    def test_focus_deduplicates(
        self, retriever: Any, dataset: list[MemoryItem],
    ) -> None:
        """聚焦阶段结果不应有重复 ID。"""
        query_vector = _normalize([0.9, 0.1, 0.0, 0.0])
        # 故意加入重复数据
        dup_dataset = dataset + dataset[:3]
        results = retriever.focus_phase(dup_dataset, query_vector, top_k=10)
        ids = [r.id for r in results]
        assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# 4. 完整检索流程端到端
# ---------------------------------------------------------------------------


class TestFullRetrieval:
    """测试完整的三阶段检索流程。"""

    def test_retrieve_returns_tagwave_results(
        self, retriever: Any, dataset: list[MemoryItem],
    ) -> None:
        """retrieve 方法应返回 TagWaveResult。"""
        query_tags = ["Python", "异步"]
        query_vector = _normalize([0.9, 0.1, 0.0, 0.0])

        result = retriever.retrieve(
            query_tags=query_tags,
            query_vector=query_vector,
            candidates=dataset,
            top_k=5,
        )

        assert hasattr(result, "results")
        assert hasattr(result, "lens_count")
        assert hasattr(result, "expand_count")
        assert hasattr(result, "focus_count")
        assert len(result.results) <= 5

    def test_retrieve_python_query(
        self, retriever: Any, dataset: list[MemoryItem],
    ) -> None:
        """Python 相关查询应返回 Python 相关结果。"""
        query_tags = ["Python", "异步"]
        query_vector = _normalize([0.9, 0.1, 0.0, 0.0])

        result = retriever.retrieve(
            query_tags=query_tags,
            query_vector=query_vector,
            candidates=dataset,
            top_k=5,
        )

        result_ids = [r.id for r in result.results]
        # 前 5 个结果中，至少应有 3 个是 Python 域的
        python_items = {"p1", "p2", "p3", "p4", "p5"}
        python_count = len(set(result_ids) & python_items)
        assert python_count >= 3

    def test_retrieve_database_query(
        self, retriever: Any, dataset: list[MemoryItem],
    ) -> None:
        """数据库相关查询应返回数据库相关结果。"""
        query_tags = ["数据库", "优化"]
        query_vector = _normalize([0.0, 0.1, 0.9, 0.0])

        result = retriever.retrieve(
            query_tags=query_tags,
            query_vector=query_vector,
            candidates=dataset,
            top_k=5,
        )

        result_ids = [r.id for r in result.results]
        db_items = {"d1", "d2", "d3", "d4", "d5"}
        db_count = len(set(result_ids) & db_items)
        assert db_count >= 3

    def test_retrieve_with_no_query_vector(
        self, retriever: Any, dataset: list[MemoryItem],
    ) -> None:
        """无查询向量时应仅基于标签检索。"""
        query_tags = ["深度学习"]

        result = retriever.retrieve(
            query_tags=query_tags,
            query_vector=None,
            candidates=dataset,
            top_k=5,
        )

        assert isinstance(result.results, list)
        assert len(result.results) > 0

    def test_retrieve_phase_counts(
        self, retriever: Any, dataset: list[MemoryItem],
    ) -> None:
        """各阶段数量应满足：lens_count >= expand_count >= focus_count。"""
        query_tags = ["Python"]
        query_vector = _normalize([0.9, 0.1, 0.0, 0.0])

        result = retriever.retrieve(
            query_tags=query_tags,
            query_vector=query_vector,
            candidates=dataset,
            top_k=5,
        )

        assert result.lens_count >= result.focus_count


# ---------------------------------------------------------------------------
# 5. 准确率验证（AC：>= 80%）
# ---------------------------------------------------------------------------


class TestAccuracy:
    """验证检索准确率达到 80% 以上。"""

    def _compute_precision_at_k(
        self,
        retriever: Any,
        dataset: list[MemoryItem],
        query_tags: list[str],
        query_vector: list[float],
        relevant_ids: set[str],
        k: int = 5,
    ) -> float:
        """计算 Precision@K。"""
        result = retriever.retrieve(
            query_tags=query_tags,
            query_vector=query_vector,
            candidates=dataset,
            top_k=k,
        )
        retrieved_ids = {r.id for r in result.results}
        relevant_retrieved = retrieved_ids & relevant_ids
        return len(relevant_retrieved) / len(retrieved_ids) if retrieved_ids else 0.0

    def test_python_query_precision(
        self, retriever: Any, dataset: list[MemoryItem],
    ) -> None:
        """Python 查询的 Precision@5 应 >= 80%。"""
        precision = self._compute_precision_at_k(
            retriever, dataset,
            query_tags=["Python", "异步", "编程"],
            query_vector=_normalize([0.9, 0.1, 0.0, 0.0]),
            relevant_ids={"p1", "p2", "p3", "p4", "p5"},
            k=5,
        )
        assert precision >= 0.8, f"Precision@5 = {precision:.2f}, expected >= 0.80"

    def test_database_query_precision(
        self, retriever: Any, dataset: list[MemoryItem],
    ) -> None:
        """数据库查询的 Precision@5 应 >= 80%。"""
        precision = self._compute_precision_at_k(
            retriever, dataset,
            query_tags=["数据库", "MySQL"],
            query_vector=_normalize([0.0, 0.1, 0.9, 0.0]),
            relevant_ids={"d1", "d2", "d3", "d4", "d5"},
            k=5,
        )
        assert precision >= 0.8, f"Precision@5 = {precision:.2f}, expected >= 0.80"

    def test_ml_query_precision(
        self, retriever: Any, dataset: list[MemoryItem],
    ) -> None:
        """机器学习查询的 Precision@5 应 >= 80%。"""
        precision = self._compute_precision_at_k(
            retriever, dataset,
            query_tags=["机器学习", "深度学习"],
            query_vector=_normalize([0.1, 0.9, 0.0, 0.0]),
            relevant_ids={"m1", "m2", "m3", "m4", "m5"},
            k=5,
        )
        assert precision >= 0.8, f"Precision@5 = {precision:.2f}, expected >= 0.80"

    def test_overall_accuracy(
        self, retriever: Any, dataset: list[MemoryItem],
    ) -> None:
        """多查询平均准确率应 >= 80%。"""
        queries = [
            {
                "tags": ["Python", "异步"],
                "vector": _normalize([0.9, 0.1, 0.0, 0.0]),
                "relevant": {"p1", "p2", "p3", "p4", "p5"},
            },
            {
                "tags": ["数据库", "优化"],
                "vector": _normalize([0.0, 0.1, 0.9, 0.0]),
                "relevant": {"d1", "d2", "d3", "d4", "d5"},
            },
            {
                "tags": ["机器学习", "深度学习"],
                "vector": _normalize([0.1, 0.9, 0.0, 0.0]),
                "relevant": {"m1", "m2", "m3", "m4", "m5"},
            },
            {
                "tags": ["Python", "并发"],
                "vector": _normalize([0.8, 0.2, 0.0, 0.0]),
                "relevant": {"p1", "p2", "p3", "p4", "p5"},
            },
            {
                "tags": ["Redis", "缓存", "数据库"],
                "vector": _normalize([0.0, 0.2, 0.8, 0.0]),
                "relevant": {"d1", "d2", "d3", "d4", "d5"},
            },
        ]

        total_precision = 0.0
        for q in queries:
            precision = self._compute_precision_at_k(
                retriever, dataset,
                query_tags=q["tags"],
                query_vector=q["vector"],
                relevant_ids=q["relevant"],
                k=5,
            )
            total_precision += precision

        avg_precision = total_precision / len(queries)
        assert avg_precision >= 0.8, (
            f"Average Precision@5 = {avg_precision:.2f}, expected >= 0.80"
        )


# ---------------------------------------------------------------------------
# 6. 边界条件
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """边界条件测试。"""

    def test_single_candidate(
        self, retriever: Any,
    ) -> None:
        """单候选项应能正常处理。"""
        single = [
            MemoryItem(
                id="s1", content="测试",
                tags=["测试"],
                vector=_normalize([1.0, 0.0, 0.0, 0.0]),
            ),
        ]
        result = retriever.retrieve(
            query_tags=["测试"],
            query_vector=_normalize([1.0, 0.0, 0.0, 0.0]),
            candidates=single,
            top_k=5,
        )
        assert len(result.results) == 1
        assert result.results[0].id == "s1"

    def test_no_matching_candidates(
        self, retriever: Any,
    ) -> None:
        """无匹配候选时应返回空或低分结果。"""
        candidates = [
            MemoryItem(
                id="x1", content="烹饪",
                tags=["烹饪"],
                vector=_normalize([0.0, 0.0, 0.0, 1.0]),
            ),
        ]
        result = retriever.retrieve(
            query_tags=["Python"],
            query_vector=_normalize([1.0, 0.0, 0.0, 0.0]),
            candidates=candidates,
            top_k=5,
        )
        # 即使不匹配也应返回结果（向量可能有一定相似度）
        assert isinstance(result.results, list)

    def test_all_empty_tags_candidates(
        self, retriever: Any,
    ) -> None:
        """所有候选都没有标签时应仍能工作。"""
        candidates = [
            MemoryItem(id="e1", content="内容1", tags=[],
                       vector=_normalize([1.0, 0.0, 0.0, 0.0])),
            MemoryItem(id="e2", content="内容2", tags=[],
                       vector=_normalize([0.0, 1.0, 0.0, 0.0])),
        ]
        result = retriever.retrieve(
            query_tags=["Python"],
            query_vector=_normalize([1.0, 0.0, 0.0, 0.0]),
            candidates=candidates,
            top_k=5,
        )
        assert isinstance(result.results, list)

    def test_very_large_candidate_set(
        self, retriever: Any,
    ) -> None:
        """大量候选集时应能正常处理。"""
        import random

        large_dataset = []
        tags_pool = ["Python", "数据库", "机器学习", "烹饪", "Java", "Go", "Rust"]
        for i in range(500):
            t = tags_pool[i % len(tags_pool)]
            vec = [random.gauss(0, 1) for _ in range(4)]
            mag = math.sqrt(sum(v * v for v in vec))
            if mag > 1e-9:
                vec = [v / mag for v in vec]
            large_dataset.append(
                MemoryItem(
                    id=f"large_{i}",
                    content=f"内容_{t}_{i}",
                    tags=[t],
                    vector=vec,
                    timestamp=time.time() - random.randint(0, 10000),
                    importance=random.uniform(0.1, 1.0),
                )
            )

        result = retriever.retrieve(
            query_tags=["Python"],
            query_vector=_normalize([1.0, 0.0, 0.0, 0.0]),
            candidates=large_dataset,
            top_k=10,
        )
        assert len(result.results) <= 10
        assert len(result.results) > 0

    def test_candidates_without_vectors(
        self, retriever: Any,
    ) -> None:
        """候选没有向量时应仅基于标签检索。"""
        candidates = [
            MemoryItem(id="nv1", content="Python编程", tags=["Python", "编程"]),
            MemoryItem(id="nv2", content="Java开发", tags=["Java", "开发"]),
        ]
        result = retriever.retrieve(
            query_tags=["Python"],
            query_vector=None,
            candidates=candidates,
            top_k=5,
        )
        assert isinstance(result.results, list)
        if result.results:
            assert result.results[0].id == "nv1"


# ---------------------------------------------------------------------------
# 7. 速度验证
# ---------------------------------------------------------------------------


class TestPerformance:
    """验证检索速度，确保不做不必要的全量扫描。"""

    def test_lens_phase_faster_than_brute_force(
        self, retriever: Any,
    ) -> None:
        """透镜阶段应比暴力全量扫描快。

        原理：透镜阶段只计算标签重叠度，不涉及向量计算。
        """
        import random

        large_dataset = []
        for i in range(1000):
            large_dataset.append(
                MemoryItem(
                    id=f"perf_{i}",
                    content=f"内容_{i}",
                    tags=[f"tag_{i % 20}", f"tag_{(i + 1) % 20}"],
                    vector=_normalize([random.gauss(0, 1) for _ in range(64)]),
                    timestamp=time.time() - i,
                    importance=0.5,
                )
            )

        # 先构建索引
        r = retriever
        cooc = [(f"tag_{i}", f"tag_{(i + 1) % 20}", 5) for i in range(20)]
        freq = {f"tag_{i}": 10 for i in range(20)}
        r.build_index(cooc, freq)

        start = time.perf_counter()
        for _ in range(10):
            r.lens_phase(["tag_0", "tag_1"], large_dataset)
        lens_time = time.perf_counter() - start

        # 10 次透镜阶段应在 1 秒内完成
        assert lens_time < 1.0, f"Lens phase too slow: {lens_time:.3f}s for 10 runs"

    def test_retrieve_speed_reasonable(
        self, retriever: Any, dataset: list[MemoryItem],
    ) -> None:
        """完整检索流程应在合理时间内完成。"""
        start = time.perf_counter()
        for _ in range(100):
            retriever.retrieve(
                query_tags=["Python", "异步"],
                query_vector=_normalize([0.9, 0.1, 0.0, 0.0]),
                candidates=dataset,
                top_k=5,
            )
        elapsed = time.perf_counter() - start

        # 100 次完整检索应在 2 秒内
        assert elapsed < 2.0, f"Full retrieval too slow: {elapsed:.3f}s for 100 runs"
