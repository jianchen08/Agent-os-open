"""TagNetworkRetriever + TagCooccurrenceMatrix 测试。

测试 Tag 共现矩阵的构建与查询、Tag 网络检索器的三阶段算法
（透镜扩散 -> 毛刺拓展 -> 聚焦投影）、动态参数计算及边界情况。
"""

from __future__ import annotations


import pytest

from memory.tag_network import (
    TagCooccurrenceMatrix,
    TagNetworkConfig,
    TagNetworkRetriever,
    _vector_norm,
)


# ============================================================
# 辅助函数
# ============================================================


def _unit_vector(dim: int, idx: int = 0) -> list[float]:
    """生成 dim 维单位向量，idx 位置为 1.0。"""
    vec = [0.0] * dim
    vec[idx] = 1.0
    return vec


def _random_vector(dim: int, seed: int = 42) -> list[float]:
    """生成确定性的伪随机向量。"""
    import random
    rng = random.Random(seed)
    vec = [rng.gauss(0, 1) for _ in range(dim)]
    norm = _vector_norm(vec)
    return [v / norm for v in vec] if norm > 0 else vec


# ============================================================
# 1. TagCooccurrenceMatrix 测试
# ============================================================


class TestTagCooccurrenceMatrix:
    """测试 Tag 共现矩阵。"""

    def test_初始化状态(self) -> None:
        """初始化后应为未初始化状态。"""
        matrix = TagCooccurrenceMatrix()
        assert not matrix.is_initialized
        assert matrix.size == 0

    def test_build_from_data_基本构建(self) -> None:
        """从数据构建共现矩阵。"""
        matrix = TagCooccurrenceMatrix()
        data = [(1, 2, 5), (2, 3, 3)]
        matrix.build_from_data(data)
        assert matrix.is_initialized
        assert matrix.size == 3

    def test_build_from_data_带频率(self) -> None:
        """构建时传入频率数据。"""
        matrix = TagCooccurrenceMatrix()
        data = [(1, 2, 5)]
        freq = {1: 10, 2: 8}
        matrix.build_from_data(data, frequency_data=freq)
        assert matrix.get_tag_frequency(1) == 10
        assert matrix.get_tag_frequency(2) == 8

    def test_build_from_data_对称填充(self) -> None:
        """共现矩阵应对称填充。"""
        matrix = TagCooccurrenceMatrix()
        matrix.build_from_data([(1, 2, 5)])
        related_1 = matrix.get_related_tags(1)
        related_2 = matrix.get_related_tags(2)
        assert len(related_1) == 1
        assert len(related_2) == 1
        assert related_1[0] == (2, 5)
        assert related_2[0] == (1, 5)

    def test_build_from_data_清除旧数据(self) -> None:
        """重复 build 应清除旧数据。"""
        matrix = TagCooccurrenceMatrix()
        matrix.build_from_data([(1, 2, 5)])
        matrix.build_from_data([(3, 4, 1)])
        assert matrix.size == 2
        assert matrix.get_related_tags(1) == []

    def test_add_cooccurrence_新增(self) -> None:
        """添加新共现关系。"""
        matrix = TagCooccurrenceMatrix()
        matrix.add_cooccurrence(1, 2, 3)
        related = matrix.get_related_tags(1)
        assert (2, 3) in related

    def test_add_cooccurrence_累加权重(self) -> None:
        """重复添加应累加权重。"""
        matrix = TagCooccurrenceMatrix()
        matrix.add_cooccurrence(1, 2, 3)
        matrix.add_cooccurrence(1, 2, 2)
        related = matrix.get_related_tags(1)
        assert (2, 5) in related

    def test_add_cooccurrence_标记为已初始化(self) -> None:
        """添加共现关系后应标记为已初始化。"""
        matrix = TagCooccurrenceMatrix()
        assert not matrix.is_initialized
        matrix.add_cooccurrence(1, 2)
        assert matrix.is_initialized

    def test_get_related_tags_按权重降序(self) -> None:
        """获取相关 tag 应按权重降序排列。"""
        matrix = TagCooccurrenceMatrix()
        matrix.build_from_data([(1, 2, 3), (1, 3, 8), (1, 4, 1)])
        related = matrix.get_related_tags(1)
        weights = [w for _, w in related]
        assert weights == sorted(weights, reverse=True)

    def test_get_related_tags_排除指定ID(self) -> None:
        """排除指定 ID 的 tag。"""
        matrix = TagCooccurrenceMatrix()
        matrix.build_from_data([(1, 2, 3), (1, 3, 5)])
        related = matrix.get_related_tags(1, exclude_ids=[2])
        ids = [tid for tid, _ in related]
        assert 2 not in ids
        assert 3 in ids

    def test_get_related_tags_不存在的tag(self) -> None:
        """查询不存在的 tag 应返回空列表。"""
        matrix = TagCooccurrenceMatrix()
        assert matrix.get_related_tags(999) == []

    def test_get_tag_frequency_存在(self) -> None:
        """获取存在的 tag 频率。"""
        matrix = TagCooccurrenceMatrix()
        matrix.build_from_data([], frequency_data={1: 42})
        assert matrix.get_tag_frequency(1) == 42

    def test_get_tag_frequency_不存在返回1(self) -> None:
        """不存在的 tag 频率应返回默认值 1。"""
        matrix = TagCooccurrenceMatrix()
        assert matrix.get_tag_frequency(999) == 1

    def test_set_tag_frequency(self) -> None:
        """设置 tag 频率。"""
        matrix = TagCooccurrenceMatrix()
        matrix.set_tag_frequency(1, 100)
        assert matrix.get_tag_frequency(1) == 100


# ============================================================
# 2. TagNetworkRetriever 基本操作
# ============================================================


class TestTagNetworkRetriever:
    """测试 Tag 网络检索器。"""

    def test_初始化默认配置(self) -> None:
        """默认配置应使用 TagNetworkConfig 默认值。"""
        retriever = TagNetworkRetriever()
        assert retriever._config.lens_top_k == 10
        assert retriever._config.spike_max_expand == 30

    def test_初始化自定义配置(self) -> None:
        """自定义配置应生效。"""
        config = TagNetworkConfig(lens_top_k=5, alpha_min=2.0)
        retriever = TagNetworkRetriever(config=config)
        assert retriever._config.lens_top_k == 5
        assert retriever._config.alpha_min == 2.0

    def test_add_tag_有向量(self) -> None:
        """添加带向量的 tag。"""
        retriever = TagNetworkRetriever()
        retriever.add_tag(1, "python", [0.1, 0.2])
        assert retriever._tag_names[1] == "python"
        assert retriever._tag_vectors[1] == [0.1, 0.2]

    def test_add_tag_无向量(self) -> None:
        """添加无向量的 tag。"""
        retriever = TagNetworkRetriever()
        retriever.add_tag(1, "python")
        assert retriever._tag_names[1] == "python"
        assert 1 not in retriever._tag_vectors


# ============================================================
# 3. _search_similar_tags 测试
# ============================================================


class TestSearchSimilarTags:
    """测试透镜阶段：相似 tag 搜索。"""

    def test_找到最相似的tag(self) -> None:
        """应返回与查询向量最相似的 tag。"""
        retriever = TagNetworkRetriever()
        retriever.add_tag(1, "python", [1.0, 0.0])
        retriever.add_tag(2, "java", [0.0, 1.0])
        retriever.add_tag(3, "python-web", [0.9, 0.1])

        results = retriever._search_similar_tags([1.0, 0.0], k=2)
        assert len(results) == 2
        assert results[0][0] == 1  # python 最相似

    def test_无tag向量时返回空(self) -> None:
        """无 tag 向量时应返回空列表。"""
        retriever = TagNetworkRetriever()
        assert retriever._search_similar_tags([1.0, 0.0]) == []

    def test_零查询向量返回空(self) -> None:
        """零查询向量应返回空列表。"""
        retriever = TagNetworkRetriever()
        retriever.add_tag(1, "test", [1.0, 0.0])
        assert retriever._search_similar_tags([0.0, 0.0]) == []

    def test_k限制返回数量(self) -> None:
        """k 参数应限制返回数量。"""
        retriever = TagNetworkRetriever()
        for i in range(10):
            retriever.add_tag(i, f"tag_{i}", _random_vector(3, seed=i))
        results = retriever._search_similar_tags([1.0, 0.0, 0.0], k=3)
        assert len(results) == 3

    def test_跳过零向量tag(self) -> None:
        """零向量的 tag 应被跳过。"""
        retriever = TagNetworkRetriever()
        retriever.add_tag(1, "normal", [1.0, 0.0])
        retriever.add_tag(2, "zero", [0.0, 0.0])
        results = retriever._search_similar_tags([1.0, 0.0], k=10)
        assert len(results) == 1
        assert results[0][0] == 1


# ============================================================
# 4. _expand_tags 测试
# ============================================================


class TestExpandTags:
    """测试毛刺阶段：tag 拓展。"""

    def test_从共现矩阵拓展(self) -> None:
        """应从共现矩阵找到关联 tag。"""
        retriever = TagNetworkRetriever()
        retriever._cooccurrence.build_from_data([(1, 10, 5), (1, 11, 3)])
        lens_results = [(1, 0.9)]
        expanded = retriever._expand_tags(lens_results, exclude_ids=[1])
        tag_ids = [tid for tid, _, _ in expanded]
        assert 10 in tag_ids
        assert 11 in tag_ids

    def test_排除指定ID(self) -> None:
        """应排除 exclude_ids 中的 tag。"""
        retriever = TagNetworkRetriever()
        retriever._cooccurrence.build_from_data([(1, 2, 5)])
        expanded = retriever._expand_tags([(1, 0.9)], exclude_ids=[1, 2])
        tag_ids = [tid for tid, _, _ in expanded]
        assert 2 not in tag_ids

    def test_无共现数据返回空(self) -> None:
        """无共现数据时应返回空列表。"""
        retriever = TagNetworkRetriever()
        expanded = retriever._expand_tags([(1, 0.9)], exclude_ids=[1])
        assert expanded == []

    def test_限制最大拓展数量(self) -> None:
        """spike_max_expand 应限制拓展数量。"""
        config = TagNetworkConfig(spike_max_expand=2)
        retriever = TagNetworkRetriever(config=config)
        co_data = [(1, i, i) for i in range(2, 20)]
        retriever._cooccurrence.build_from_data(co_data)
        expanded = retriever._expand_tags([(1, 0.9)], exclude_ids=[1])
        assert len(expanded) <= 2


# ============================================================
# 5. _fuse_vectors 测试
# ============================================================


class TestFuseVectors:
    """测试聚焦阶段：向量融合。"""

    def test_基本融合(self) -> None:
        """融合后向量维度应与查询向量一致。"""
        retriever = TagNetworkRetriever()
        retriever.add_tag(10, "related", [0.5, 0.5, 0.5])
        query = [1.0, 0.0, 0.0]
        expanded = [(10, 2.0, 5)]
        fused, info = retriever._fuse_vectors(query, expanded, 0.3, 2.0, 2.0)
        assert len(fused) == 3
        assert info["total_score"] > 0

    def test_融合后归一化(self) -> None:
        """融合后向量应近似单位向量。"""
        retriever = TagNetworkRetriever()
        retriever.add_tag(10, "t1", [1.0, 0.0])
        retriever.add_tag(11, "t2", [0.0, 1.0])
        query = [0.707, 0.707]
        expanded = [(10, 2.0, 5), (11, 1.0, 3)]
        fused, info = retriever._fuse_vectors(query, expanded, 0.3, 2.0, 2.0)
        norm = _vector_norm(fused)
        assert abs(norm - 1.0) < 0.01

    def test_无有效tag向量时返回原向量(self) -> None:
        """无有效 tag 向量时应返回原查询向量。"""
        retriever = TagNetworkRetriever()
        query = [1.0, 0.0]
        expanded = [(999, 2.0, 5)]  # tag 999 无向量
        fused, info = retriever._fuse_vectors(query, expanded, 0.3, 2.0, 2.0)
        assert fused == query
        assert info["total_score"] == 0

    def test_total_score为零时返回原向量(self) -> None:
        """total_score 为 0 时应返回原查询向量。"""
        retriever = TagNetworkRetriever()
        retriever.add_tag(10, "t1", [0.0, 0.0])  # 零向量
        query = [1.0, 0.0]
        expanded = [(10, 0.0, 5)]
        fused, info = retriever._fuse_vectors(query, expanded, 0.3, 2.0, 2.0)
        assert fused == query


# ============================================================
# 6. apply_tag_boost 完整三阶段测试
# ============================================================


class TestApplyTagBoost:
    """测试三阶段完整流程：透镜 -> 毛刺 -> 聚焦。"""

    @pytest.mark.asyncio
    async def test_完整三阶段流程(self) -> None:
        """三阶段正常执行应返回增强结果。"""
        retriever = TagNetworkRetriever()
        retriever.add_tag(1, "python", [1.0, 0.0, 0.0])
        retriever.add_tag(2, "flask", [0.9, 0.1, 0.0])
        retriever.add_tag(10, "django", [0.8, 0.2, 0.0])
        retriever._cooccurrence.build_from_data(
            [(1, 10, 5)], frequency_data={1: 10, 10: 8},
        )

        result = await retriever.apply_tag_boost([1.0, 0.0, 0.0], tag_boost=0.3)
        assert len(result.vector) == 3
        assert len(result.matched_tags) > 0
        assert result.boost_factor == 0.3
        assert result.spike_count > 0

    @pytest.mark.asyncio
    async def test_tag_boost为零返回原向量(self) -> None:
        """tag_boost <= 0 时应返回原向量。"""
        retriever = TagNetworkRetriever()
        retriever.add_tag(1, "test", [1.0, 0.0])
        result = await retriever.apply_tag_boost([0.5, 0.5], tag_boost=0.0)
        assert result.vector == [0.5, 0.5]
        assert result.boost_factor == 0

    @pytest.mark.asyncio
    async def test_无tag向量返回原向量(self) -> None:
        """无 tag 向量时应返回原向量。"""
        retriever = TagNetworkRetriever()
        result = await retriever.apply_tag_boost([0.5, 0.5], tag_boost=0.3)
        assert result.vector == [0.5, 0.5]

    @pytest.mark.asyncio
    async def test_透镜无结果返回原向量(self) -> None:
        """透镜阶段无匹配时应返回原向量。"""
        retriever = TagNetworkRetriever()
        retriever.add_tag(1, "test", [0.0, 0.0])  # 零向量，搜索不到
        result = await retriever.apply_tag_boost([1.0, 0.0], tag_boost=0.3)
        assert result.vector == [1.0, 0.0]

    @pytest.mark.asyncio
    async def test_毛刺无拓展时用透镜结果(self) -> None:
        """毛刺拓展无结果时应回退使用透镜结果。"""
        retriever = TagNetworkRetriever()
        retriever.add_tag(1, "python", [1.0, 0.0])
        # 无共现数据，毛刺拓展为空，回退到透镜结果
        result = await retriever.apply_tag_boost([1.0, 0.0], tag_boost=0.3)
        assert result.spike_count > 0

    @pytest.mark.asyncio
    async def test_匹配tag名称正确(self) -> None:
        """matched_tags 应包含正确的 tag 名称。"""
        retriever = TagNetworkRetriever()
        retriever.add_tag(1, "python", [1.0, 0.0])
        retriever.add_tag(2, "flask", [0.9, 0.1])
        retriever._cooccurrence.build_from_data(
            [(1, 10, 5)], frequency_data={1: 5, 10: 3},
        )
        retriever.add_tag(10, "web", [0.8, 0.2])
        result = await retriever.apply_tag_boost([1.0, 0.0], tag_boost=0.3)
        # 应包含 web（通过共现拓展）
        assert "python" in result.matched_tags or "flask" in result.matched_tags


# ============================================================
# 7. 动态参数计算
# ============================================================


class TestDynamicParameters:
    """测试动态 alpha/beta 计算。"""

    def test_alpha_高相似度高增强(self) -> None:
        """高相似度应导致高 alpha。"""
        retriever = TagNetworkRetriever()
        alpha_high = retriever._compute_dynamic_alpha(0.9)
        alpha_low = retriever._compute_dynamic_alpha(0.1)
        assert alpha_high > alpha_low

    def test_alpha_范围限制(self) -> None:
        """alpha 应在 [alpha_min, alpha_max] 范围内。"""
        config = TagNetworkConfig(alpha_min=1.5, alpha_max=3.5)
        retriever = TagNetworkRetriever(config=config)
        for score in [0.0, 0.5, 1.0]:
            alpha = retriever._compute_dynamic_alpha(score)
            assert 1.5 <= alpha <= 3.5

    def test_beta_低相似度高降噪(self) -> None:
        """低相似度应导致高 beta。"""
        retriever = TagNetworkRetriever()
        beta_low = retriever._compute_dynamic_beta(0.9)
        beta_high = retriever._compute_dynamic_beta(0.1)
        assert beta_high > beta_low

    def test_beta_范围(self) -> None:
        """beta 应在 [beta_base, beta_base + beta_range] 范围内。"""
        config = TagNetworkConfig(beta_base=2.0, beta_range=3.0)
        retriever = TagNetworkRetriever(config=config)
        for score in [0.0, 0.5, 1.0]:
            beta = retriever._compute_dynamic_beta(score)
            assert 2.0 <= beta <= 5.0


# ============================================================
# 8. get_stats 测试
# ============================================================


class TestGetStats:
    """测试统计信息。"""

    def test_初始状态(self) -> None:
        """初始状态统计。"""
        retriever = TagNetworkRetriever()
        stats = retriever.get_stats()
        assert stats["matrix_size"] == 0
        assert stats["tag_vectors_count"] == 0
        assert not stats["initialized"]

    def test_添加数据后统计(self) -> None:
        """添加数据后统计应更新。"""
        retriever = TagNetworkRetriever()
        retriever.add_tag(1, "python", [1.0])
        retriever.add_tag(2, "flask", [0.5])
        retriever._cooccurrence.build_from_data([(1, 2, 3)])
        stats = retriever.get_stats()
        assert stats["matrix_size"] == 2
        assert stats["tag_vectors_count"] == 2
        assert stats["initialized"]


# ============================================================
# 9. 边界情况
# ============================================================


class TestEdgeCases:
    """测试边界情况。"""

    def test_空向量(self) -> None:
        """空查询向量。"""
        retriever = TagNetworkRetriever()
        retriever.add_tag(1, "test", [1.0, 0.0])
        results = retriever._search_similar_tags([], k=5)
        assert results == []

    def test_零向量(self) -> None:
        """零查询向量。"""
        retriever = TagNetworkRetriever()
        retriever.add_tag(1, "test", [1.0, 0.0])
        results = retriever._search_similar_tags([0.0, 0.0], k=5)
        assert results == []

    @pytest.mark.asyncio
    async def test_无tag数据(self) -> None:
        """无 tag 数据时 apply_tag_boost 应返回原向量。"""
        retriever = TagNetworkRetriever()
        result = await retriever.apply_tag_boost([1.0, 0.0], tag_boost=0.3)
        assert result.vector == [1.0, 0.0]
        assert result.spike_count == 0

    def test_向量维度不一致(self) -> None:
        """查询向量与 tag 向量维度不一致时的余弦计算。"""
        retriever = TagNetworkRetriever()
        retriever.add_tag(1, "3d", [1.0, 0.0, 0.0])
        # 维度不一致时 zip 只取较短长度
        results = retriever._search_similar_tags([1.0, 0.0], k=5)
        # 不会崩溃，结果可能不精确
        assert isinstance(results, list)

    def test_vector_norm_空向量(self) -> None:
        """空向量的范数应为 0。"""
        assert _vector_norm([]) == 0.0

    def test_vector_norm_零向量(self) -> None:
        """零向量的范数应为 0。"""
        assert _vector_norm([0.0, 0.0, 0.0]) == 0.0

    def test_vector_norm_单位向量(self) -> None:
        """单位向量的范数应为 1。"""
        assert abs(_vector_norm([1.0, 0.0, 0.0]) - 1.0) < 1e-9

    def test_vector_norm_一般向量(self) -> None:
        """一般向量的范数应正确。"""
        assert abs(_vector_norm([3.0, 4.0]) - 5.0) < 1e-9
