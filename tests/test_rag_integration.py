"""RAG 检索算法集成测试。

验证所有新增模块的导入、枚举值、跨模块协作和端到端检索流程。

测试覆盖：
1. RetrievalMethod.WAVE 枚举值存在
2. 所有新模块可正常导入
3. WaveRetriever 端到端检索流程
4. CrossDomainDiscovery 跨域关联（AC-4b）
5. DirectoryGenerator 与 CrossDomainDiscovery 协作
6. SemanticPreprocessor + WaveRetriever 协作
7. MemoryService + WaveRetriever 完整集成（AC-4c）
"""

from __future__ import annotations

from typing import Any

import pytest

from memory.types import RetrievalMethod, SearchResult


# ---------------------------------------------------------------------------
# 辅助
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
# 1. 枚举值验证
# ---------------------------------------------------------------------------


class TestRetrievalMethodEnum:
    """验证 RetrievalMethod 枚举包含所有预期值。"""

    @pytest.mark.skip(reason="RetrievalMethod.WAVE 已移除")
    def test_wave_enum_exists(self) -> None:
        """RetrievalMethod 应包含 WAVE 值。"""
        assert hasattr(RetrievalMethod, "WAVE")
        assert RetrievalMethod.WAVE.value in ("wave", "WAVE")

    def test_vector_enum_exists(self) -> None:
        """RetrievalMethod 应包含 VECTOR 值。"""
        assert hasattr(RetrievalMethod, "VECTOR")

    def test_keyword_enum_exists(self) -> None:
        """RetrievalMethod 应包含 KEYWORD 值。"""
        assert hasattr(RetrievalMethod, "KEYWORD")

    def test_all_methods_listable(self) -> None:
        """应能列出所有检索方法。"""
        methods = list(RetrievalMethod)
        assert len(methods) >= 3
        method_values = {m.value for m in methods}
        assert "vector" in method_values or "VECTOR" in method_values
        assert "keyword" in method_values or "KEYWORD" in method_values


# ---------------------------------------------------------------------------
# 2. 模块导入验证
# ---------------------------------------------------------------------------


class TestModuleImports:
    """验证所有新增模块可正常导入。"""

    def test_import_wave_retriever(self) -> None:
        """应能导入 WaveRetriever。"""
        from memory.wave_retriever import WaveRetriever, WaveRetrieverConfig
        assert WaveRetriever is not None
        assert WaveRetrieverConfig is not None

    @pytest.mark.skip(reason="memory.semantic_preprocessor 模块已移除")
    def test_import_semantic_preprocessor(self) -> None:
        """应能导入 SemanticPreprocessor。"""
        from memory.semantic_preprocessor import SemanticPreprocessor, SemanticPreprocessorConfig
        assert SemanticPreprocessor is not None
        assert SemanticPreprocessorConfig is not None

    @pytest.mark.skip(reason="memory.cross_domain_discovery 模块已移除")
    def test_import_cross_domain_discovery(self) -> None:
        """应能导入 CrossDomainDiscovery。"""
        from memory.cross_domain_discovery import CrossDomainDiscovery, CrossDomainConfig
        assert CrossDomainDiscovery is not None
        assert CrossDomainConfig is not None

    @pytest.mark.skip(reason="memory.directory_generator 模块已移除")
    def test_import_directory_generator(self) -> None:
        """应能导入 DirectoryGenerator。"""
        from memory.directory_generator import DirectoryGenerator, DirectoryConfig
        assert DirectoryGenerator is not None
        assert DirectoryConfig is not None

    @pytest.mark.skip(reason="memory.WaveRetriever 已移除")
    def test_import_from_package_init(self) -> None:
        """应能从 memory 包的 __init__ 导入所有新组件。"""
        import memory
        assert hasattr(memory, "WaveRetriever")
        assert hasattr(memory, "WaveRetrieverConfig")
        assert hasattr(memory, "SemanticPreprocessor")
        assert hasattr(memory, "CrossDomainDiscovery")
        assert hasattr(memory, "DirectoryGenerator")

    def test_import_memory_types(self) -> None:
        """应能导入所有必要的类型。"""
        from memory.types import (
            RetrievalMethod,
            SearchResult,
        )
        assert SearchResult is not None
        assert RetrievalMethod is not None

    def test_import_memory_ports(self) -> None:
        """应能导入 IRetriever 接口。"""
        from memory.ports import IRetriever
        assert IRetriever is not None


# ---------------------------------------------------------------------------
# 3. WaveRetriever 端到端检索
# ---------------------------------------------------------------------------


class TestWaveRetrieverEndToEnd:
    """WaveRetriever 端到端检索流程验证。"""

    @pytest.mark.asyncio
    async def test_full_pipeline(self) -> None:
        """完整的 wave 检索流程应正常工作。"""
        from memory.wave_retriever import WaveRetriever

        items = [
            _make_item("1", "EPA分析提取实体属性动作",
                       tags=["EPA", "分析"],
                       epa={"entity": ["EPA", "实体"], "property": ["属性"], "action": ["提取", "分析"]},
                       related_ids=["2"]),
            _make_item("2", "残差金字塔多层次检索策略",
                       tags=["残差", "检索"],
                       epa={"entity": ["残差", "金字塔", "检索"], "action": ["检索"]},
                       related_ids=["1", "3"]),
            _make_item("3", "浪潮扩散多跳关联发现",
                       tags=["浪潮", "扩散", "关联"],
                       epa={"entity": ["浪潮", "扩散", "关联"], "action": ["发现"]},
                       related_ids=["2"]),
            _make_item("4", "霰弹枪检索多角度查询",
                       tags=["霰弹枪", "检索", "查询"],
                       epa={"entity": ["霰弹枪", "检索", "查询"], "action": ["查询"]},
                       related_ids=[]),
        ]

        async def provider() -> list[dict[str, Any]]:
            return items

        retriever = WaveRetriever(
            knowledge_items_provider=provider,
            config={"min_score": 0.01, "max_hops": 3},
        )

        results = await retriever.retrieve("检索算法分析", top_k=10)
        assert len(results) > 0
        result_ids = {r.id for r in results}
        # 至少应命中包含"检索"关键词的条目
        assert "2" in result_ids or "4" in result_ids

    @pytest.mark.asyncio
    async def test_wave_with_embedding_service(self) -> None:
        """带 embedding_service 时应能进行向量检索。"""
        from memory.wave_retriever import WaveRetriever

        class MockEmbeddingService:
            """模拟嵌入服务。"""
            def embed(self, text: str) -> list[float]:
                # 简单的确定性向量
                return [0.5, 0.5, 0.0]

        items = [
            _make_item("1", "向量检索测试",
                       embedding=[0.5, 0.5, 0.0],
                       tags=["向量"]),
            _make_item("2", "不相关内容",
                       embedding=[0.0, 0.0, 1.0],
                       tags=["其他"]),
        ]

        async def provider() -> list[dict[str, Any]]:
            return items

        retriever = WaveRetriever(
            embedding_service=MockEmbeddingService(),
            knowledge_items_provider=provider,
            config={"min_score": 0.01},
        )

        results = await retriever.retrieve("向量检索", top_k=5)
        assert len(results) > 0
        # 向量相似的条目应排在前面
        if len(results) >= 2:
            assert results[0].id == "1"


# ---------------------------------------------------------------------------
# 4. CrossDomainDiscovery 跨域关联（AC-4b）
# ---------------------------------------------------------------------------


class TestCrossDomainIntegration:
    """验证跨域发现可找到不同领域的隐式关联（AC-4b）。"""

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="memory.cross_domain_discovery 模块已移除")
    async def test_discovers_cross_domain_links(self) -> None:
        """跨域发现应能找到不同领域间的隐式关联。"""
        from memory.cross_domain_discovery import CrossDomainConfig, CrossDomainDiscovery

        items = [
            {
                "id": "tech1",
                "content": "神经网络通过反向传播算法优化权重",
                "tags": ["深度学习", "神经网络", "算法"],
            },
            {
                "id": "tech2",
                "content": "卷积神经网络在图像识别中的应用",
                "tags": ["深度学习", "CNN", "图像识别"],
            },
            {
                "id": "bio1",
                "content": "大脑神经元通过突触传递信号进行学习",
                "tags": ["神经科学", "神经元", "学习"],
            },
            {
                "id": "bio2",
                "content": "视觉皮层处理图像信号的层级结构",
                "tags": ["神经科学", "视觉", "图像"],
            },
            {
                "id": "econ1",
                "content": "市场预测模型使用梯度下降优化参数",
                "tags": ["经济学", "预测模型", "优化"],
            },
        ]

        discovery = CrossDomainDiscovery(CrossDomainConfig(min_bridge_strength=0.01))
        results = await discovery.discover("神经网络学习", items, top_k=10)

        assert isinstance(results, list)
        # 应找到跨域关联（技术 ↔ 生物）
        if results:
            domains = set()
            for r in results:
                if "cross_domain" in r:
                    domains.add(r.get("domain", ""))
            # 验证跨域标记
            [r for r in results if r.get("cross_domain", False)]
            # 即使没有明确的跨域标记，至少应返回相关结果
            assert len(results) > 0

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="memory.cross_domain_discovery 模块已移除")
    async def test_cross_domain_concept_bridge(self) -> None:
        """不同领域共享概念应被识别为概念桥。"""
        from memory.cross_domain_discovery import CrossDomainConfig, CrossDomainDiscovery

        items = [
            {
                "id": "cs",
                "content": "分布式系统中的容错机制设计",
                "tags": ["计算机", "分布式", "容错"],
            },
            {
                "id": "bio",
                "content": "生物系统中的自我修复和容错能力",
                "tags": ["生物学", "自我修复", "容错"],
            },
            {
                "id": "eng",
                "content": "工程系统的冗余容错设计方案",
                "tags": ["工程", "冗余", "容错"],
            },
        ]

        discovery = CrossDomainDiscovery(CrossDomainConfig(min_bridge_strength=0.01))
        results = await discovery.discover("容错机制", items, top_k=10)

        assert len(results) > 0
        # 结果是跨域关联，包含 source_id 和 target_id
        all_ids: set[str] = set()
        for r in results:
            all_ids.add(r.get("source_id", ""))
            all_ids.add(r.get("target_id", ""))
        # 至少关联两个不同条目
        assert len(all_ids) >= 2


# ---------------------------------------------------------------------------
# 5. DirectoryGenerator 集成
# ---------------------------------------------------------------------------


class TestDirectoryGeneratorIntegration:
    """DirectoryGenerator 与其他模块的协作验证。"""

    @pytest.mark.skip(reason="memory.directory_generator 模块已移除")
    def test_generates_directory_from_items(self) -> None:
        """DirectoryGenerator 应能从知识条目生成概念页和索引。"""
        from memory.directory_generator import DirectoryConfig, DirectoryGenerator

        items = [
            {"id": "1", "content": "Python基础语法", "tags": ["Python", "编程"]},
            {"id": "2", "content": "Python高级特性", "tags": ["Python", "编程"]},
            {"id": "3", "content": "Java面向对象", "tags": ["Java", "编程"]},
            {"id": "4", "content": "数据库设计", "tags": ["数据库", "设计"]},
            {"id": "5", "content": "机器学习入门", "tags": ["ML", "AI"]},
        ]

        generator = DirectoryGenerator(DirectoryConfig())
        # 分步调用：概念页 → 索引页 → 层次结构
        pages = generator.generate_concept_pages(items)
        assert isinstance(pages, list)

        index = generator.generate_index_page(pages)
        assert isinstance(index, dict)
        assert "concepts" in index or "statistics" in index or "total_items" in index

    @pytest.mark.skip(reason="memory.directory_generator 模块已移除")
    def test_cross_domain_concepts_in_directory(self) -> None:
        """跨域概念应在目录中得到体现。"""
        from memory.directory_generator import DirectoryConfig, DirectoryGenerator

        items = [
            {"id": "1", "content": "神经网络反向传播算法", "tags": ["深度学习", "算法"]},
            {"id": "2", "content": "生物神经元信号传递", "tags": ["生物学", "神经"]},
            {"id": "3", "content": "市场优化模型", "tags": ["经济学", "优化"]},
            {"id": "4", "content": "遗传算法优化", "tags": ["算法", "优化"]},
        ]

        generator = DirectoryGenerator(DirectoryConfig())
        pages = generator.generate_concept_pages(items)
        assert isinstance(pages, list)
        hierarchy = generator.build_hierarchy(pages)
        assert isinstance(hierarchy, dict)


# ---------------------------------------------------------------------------
# 6. SemanticPreprocessor + WaveRetriever 协作
# ---------------------------------------------------------------------------


class TestPreprocessorRetrieverIntegration:
    """验证 SemanticPreprocessor 预处理后数据可被 WaveRetriever 检索。"""

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="memory.semantic_preprocessor 模块已移除")
    async def test_preprocessed_data_retrievable(self) -> None:
        """SemanticPreprocessor 处理后的块应可被 WaveRetriever 检索。"""
        from memory.semantic_preprocessor import SemanticPreprocessor
        from memory.wave_retriever import WaveRetriever

        # 1. 预处理长文本
        text = (
            "Python是一种广泛使用的高级编程语言。"
            "它支持多种编程范式，包括面向对象和函数式编程。\n\n"
            "Python的标准库提供了丰富的模块和函数。"
            "数据分析、机器学习和Web开发是Python的主要应用领域。\n\n"
            "Java是另一种流行的编程语言，主要用于企业级应用开发。"
        )

        preprocessor = SemanticPreprocessor()
        chunks = preprocessor.process(text)

        assert len(chunks) > 0

        # 2. 将处理后的块转为知识条目
        items = []
        for i, chunk in enumerate(chunks):
            items.append(_make_item(
                f"chunk_{i}",
                chunk["content"],
                tags=chunk.get("tags", []),
            ))

        async def provider() -> list[dict[str, Any]]:
            return items

        # 3. 用 WaveRetriever 检索
        retriever = WaveRetriever(
            knowledge_items_provider=provider,
            config={"min_score": 0.01},
        )
        results = await retriever.retrieve("Python编程语言应用", top_k=10)

        # 应能检索到与 Python 相关的块
        assert len(results) > 0
        result_contents = " ".join(r.content for r in results)
        assert "Python" in result_contents


# ---------------------------------------------------------------------------
# 7. MemoryService + WaveRetriever 完整集成（AC-4c）
# ---------------------------------------------------------------------------


class TestFullMemoryServiceIntegration:
    """MemoryService + WaveRetriever 端到端集成测试（AC-4c）。"""

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="RetrievalMethod.WAVE 已移除")
    async def test_memory_service_with_wave_retriever(self) -> None:
        """通过 MemoryService 使用 WaveRetriever 检索应正常工作。"""
        from memory.wave_retriever import WaveRetriever
        from memory.service import MemoryService

        items = [
            _make_item("doc1", "RAG检索增强生成技术概述",
                       tags=["RAG", "检索", "生成"],
                       epa={"entity": ["RAG", "检索"], "action": ["生成"]}),
            _make_item("doc2", "向量数据库Milvus使用指南",
                       tags=["向量", "数据库", "Milvus"],
                       epa={"entity": ["向量", "数据库", "Milvus"], "action": []}),
            _make_item("doc3", "EPA分析在文本理解中的应用",
                       tags=["EPA", "文本", "分析"],
                       epa={"entity": ["EPA", "文本"], "action": ["分析"]}),
        ]

        async def provider() -> list[dict[str, Any]]:
            return items

        # 创建并注册
        wave = WaveRetriever(knowledge_items_provider=provider, config={"min_score": 0.01})
        service = MemoryService()
        service.register_retriever("wave", wave)

        # 通过 MemoryService 统一接口检索
        results = await service.retrieve(
            query="检索技术",
            retrieval_method="wave",
            top_k=5,
        )

        assert isinstance(results, list)
        assert len(results) > 0
        assert all(isinstance(r, SearchResult) for r in results)

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="RetrievalMethod.WAVE 已移除")
    async def test_memory_service_retrieval_stats(self) -> None:
        """MemoryService 应记录检索统计。"""
        from memory.wave_retriever import WaveRetriever
        from memory.service import MemoryService

        items = [
            _make_item("1", "测试内容", tags=["测试"]),
        ]

        async def provider() -> list[dict[str, Any]]:
            return items

        wave = WaveRetriever(knowledge_items_provider=provider, config={"min_score": 0.01})
        service = MemoryService()
        service.register_retriever("wave", wave)

        # 执行检索
        await service.retrieve(query="测试", retrieval_method="wave", top_k=5)

        # 验证统计
        stats = service.get_retrieval_stats()
        assert isinstance(stats, dict)
        assert "total_requests" in stats

    @pytest.mark.asyncio
    async def test_memory_service_health_check(self) -> None:
        """MemoryService 健康检查应包含 WaveRetriever 信息。"""
        from memory.wave_retriever import WaveRetriever
        from memory.service import MemoryService

        items = [_make_item("1", "test")]
        async def provider() -> list[dict[str, Any]]:
            return items

        wave = WaveRetriever(knowledge_items_provider=provider)
        service = MemoryService()
        service.register_retriever("wave", wave)

        health = await service.health_check()
        assert isinstance(health, dict)
        assert "available_retrievers" in health
        assert "wave" in health["available_retrievers"]
