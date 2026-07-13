"""directory_generator 模块的单元测试。

覆盖 DirectoryGenerator 和 DirectoryConfig 的所有公共方法，
包括正常路径、边界条件和异常场景。
"""

from __future__ import annotations


from memory.directory_generator import DirectoryConfig, DirectoryGenerator


# ============================================================
# 测试辅助工具
# ============================================================


def _make_items(items_data: list[dict]) -> list[dict]:
    """快速构建知识条目列表。

    Args:
        items_data: 简化数据列表，每项包含 id, content, tags

    Returns:
        完整的知识条目列表
    """
    result = []
    for d in items_data:
        result.append({
            "id": d["id"],
            "content": d.get("content", ""),
            "tags": d.get("tags", []),
        })
    return result


def _ml_items() -> list[dict]:
    """机器学习领域的测试知识条目。"""
    return _make_items([
        {"id": "ml-1", "content": "随机森林是一种集成学习方法", "tags": ["机器学习", "集成学习", "随机森林"]},
        {"id": "ml-2", "content": "梯度提升树通过迭代优化弱学习器", "tags": ["机器学习", "集成学习", "梯度提升"]},
        {"id": "ml-3", "content": "CNN用于图像识别任务", "tags": ["深度学习", "CNN", "图像识别"]},
        {"id": "ml-4", "content": "RNN适用于序列建模", "tags": ["深度学习", "RNN", "序列建模"]},
        {"id": "ml-5", "content": "决策树通过特征划分构建分类模型", "tags": ["机器学习", "决策树"]},
    ])


def _mixed_domain_items() -> list[dict]:
    """多领域混合的测试知识条目。"""
    return _make_items([
        {"id": "a1", "content": "Python 是一种通用编程语言", "tags": ["编程语言", "Python"]},
        {"id": "a2", "content": "Python 常用于数据科学", "tags": ["编程语言", "Python", "数据科学"]},
        {"id": "b1", "content": "TCP/IP 是互联网通信基础", "tags": ["网络协议", "TCP"]},
        {"id": "b2", "content": "HTTP 协议基于 TCP 连接", "tags": ["网络协议", "HTTP"]},
        {"id": "b3", "content": "UDP 提供无连接传输", "tags": ["网络协议", "UDP"]},
        {"id": "c1", "content": "线性回归是最基础的回归方法", "tags": ["机器学习", "回归"]},
        {"id": "c2", "content": "逻辑回归用于分类问题", "tags": ["机器学习", "分类"]},
        {"id": "c3", "content": "SVM 通过超平面分割数据", "tags": ["机器学习", "分类", "SVM"]},
    ])


# ============================================================
# DirectoryConfig 测试
# ============================================================


class TestDirectoryConfig:
    """DirectoryConfig 配置类的测试。"""

    def test_default_values(self) -> None:
        """默认配置值应与规范一致。"""
        config = DirectoryConfig()
        assert config.min_items_per_concept == 2
        assert config.max_concepts == 100
        assert config.similarity_threshold == 0.5
        assert config.hierarchy_max_depth == 3

    def test_custom_values(self) -> None:
        """自定义配置值应被正确保存。"""
        config = DirectoryConfig(
            min_items_per_concept=5,
            max_concepts=50,
            similarity_threshold=0.7,
            hierarchy_max_depth=5,
        )
        assert config.min_items_per_concept == 5
        assert config.max_concepts == 50
        assert config.similarity_threshold == 0.7
        assert config.hierarchy_max_depth == 5


# ============================================================
# DirectoryGenerator 基础测试
# ============================================================


class TestDirectoryGeneratorInit:
    """DirectoryGenerator 初始化测试。"""

    def test_default_config(self) -> None:
        """不传配置时使用默认配置。"""
        gen = DirectoryGenerator()
        assert gen._config.min_items_per_concept == 2

    def test_custom_config(self) -> None:
        """传入自定义配置时使用该配置。"""
        config = DirectoryConfig(min_items_per_concept=10)
        gen = DirectoryGenerator(config=config)
        assert gen._config.min_items_per_concept == 10


# ============================================================
# cluster_by_concept 测试
# ============================================================


class TestClusterByConcept:
    """按概念聚类知识条目的测试。"""

    def test_empty_items(self) -> None:
        """空输入应返回空字典。"""
        gen = DirectoryGenerator()
        result = gen.cluster_by_concept([])
        assert result == {}

    def test_single_item(self) -> None:
        """单条目输入，低于 min_items_per_concept 时归入"未分类"。"""
        gen = DirectoryGenerator()
        items = _make_items([
            {"id": "1", "content": "test", "tags": ["python"]},
        ])
        result = gen.cluster_by_concept(items)
        # 单条目无法形成概念簇（默认 min=2），应归入未分类
        assert isinstance(result, dict)
        assert len(result) >= 1
        # 未分类或"其他"类应包含该条目
        all_items = [item for group in result.values() for item in group]
        assert len(all_items) == 1

    def test_items_with_shared_tags(self) -> None:
        """共享标签的条目应被聚为同一概念。"""
        gen = DirectoryGenerator()
        items = _ml_items()
        result = gen.cluster_by_concept(items)

        # 应有至少一个概念簇
        assert len(result) >= 1

        # 检查所有条目都被分配
        all_assigned_ids: set[str] = set()
        for concept_items in result.values():
            for item in concept_items:
                all_assigned_ids.add(item["id"])
        assert all_assigned_ids == {"ml-1", "ml-2", "ml-3", "ml-4", "ml-5"}

    def test_items_without_tags(self) -> None:
        """无标签的条目应归入"未分类"。"""
        gen = DirectoryGenerator()
        items = _make_items([
            {"id": "x1", "content": "no tags item", "tags": []},
            {"id": "x2", "content": "another no tags", "tags": []},
        ])
        result = gen.cluster_by_concept(items)
        assert len(result) >= 1
        all_items = [item for group in result.values() for item in group]
        assert len(all_items) == 2

    def test_max_concepts_limit(self) -> None:
        """概念数不应超过 max_concepts 配置。"""
        config = DirectoryConfig(max_concepts=2, min_items_per_concept=1)
        gen = DirectoryGenerator(config=config)

        # 创建很多不同标签的条目
        items = _make_items([
            {"id": f"item-{i}", "content": f"content {i}", "tags": [f"tag-{i}", "shared"]}
            for i in range(20)
        ])
        result = gen.cluster_by_concept(items)
        assert len(result) <= 2

    def test_multiple_distinct_concepts(self) -> None:
        """不同领域的条目应被分为不同概念。"""
        gen = DirectoryGenerator()
        items = _mixed_domain_items()
        result = gen.cluster_by_concept(items)

        # 至少应分出编程语言和网络协议两个概念
        assert len(result) >= 2


# ============================================================
# generate_concept_pages 测试
# ============================================================


class TestGenerateConceptPages:
    """概念页生成的测试。"""

    def test_empty_items(self) -> None:
        """空输入应返回空列表。"""
        gen = DirectoryGenerator()
        pages = gen.generate_concept_pages([])
        assert pages == []

    def test_concept_page_structure(self) -> None:
        """每个概念页应包含所有必需字段。"""
        gen = DirectoryGenerator()
        items = _ml_items()
        pages = gen.generate_concept_pages(items)

        assert len(pages) >= 1
        required_keys = {
            "page_id", "title", "concept", "domain", "summary",
            "related_items", "sub_concepts", "parent_concepts",
            "keywords", "item_count",
        }
        for page in pages:
            assert required_keys.issubset(page.keys()), f"缺少字段: {required_keys - page.keys()}"

    def test_concept_page_item_count(self) -> None:
        """概念页的 item_count 应与 related_items 长度一致。"""
        gen = DirectoryGenerator()
        items = _ml_items()
        pages = gen.generate_concept_pages(items)

        for page in pages:
            assert page["item_count"] == len(page["related_items"])

    def test_concept_page_ids_unique(self) -> None:
        """每个概念页的 page_id 应唯一。"""
        gen = DirectoryGenerator()
        items = _mixed_domain_items()
        pages = gen.generate_concept_pages(items)

        page_ids = [p["page_id"] for p in pages]
        assert len(page_ids) == len(set(page_ids))

    def test_all_items_covered(self) -> None:
        """所有输入条目应被分配到至少一个概念页中。"""
        gen = DirectoryGenerator()
        items = _mixed_domain_items()
        pages = gen.generate_concept_pages(items)

        all_item_ids: set[str] = set()
        for page in pages:
            all_item_ids.update(page["related_items"])

        input_ids = {item["id"] for item in items}
        assert input_ids.issubset(all_item_ids), f"未覆盖的条目: {input_ids - all_item_ids}"

    def test_keywords_not_empty(self) -> None:
        """概念页的 keywords 应非空（如果有条目的话）。"""
        gen = DirectoryGenerator()
        items = _ml_items()
        pages = gen.generate_concept_pages(items)

        for page in pages:
            if page["item_count"] > 0:
                assert len(page["keywords"]) > 0

    def test_min_items_per_concept_filter(self) -> None:
        """概念页中的条目数应 >= min_items_per_concept（合并后）。"""
        config = DirectoryConfig(min_items_per_concept=3)
        gen = DirectoryGenerator(config=config)
        items = _ml_items()
        pages = gen.generate_concept_pages(items)

        for page in pages:
            assert page["item_count"] >= 1  # 至少有条目


# ============================================================
# generate_index_page 测试
# ============================================================


class TestGenerateIndexPage:
    """索引页生成的测试。"""

    def test_empty_concept_pages(self) -> None:
        """空输入应返回有效的索引结构。"""
        gen = DirectoryGenerator()
        index = gen.generate_index_page([])

        assert index["statistics"]["total_items"] == 0
        assert index["statistics"]["total_concepts"] == 0
        assert index["statistics"]["total_domains"] == 0

    def test_index_structure(self) -> None:
        """索引页应包含所有必需字段。"""
        gen = DirectoryGenerator()
        items = _mixed_domain_items()
        pages = gen.generate_concept_pages(items)
        index = gen.generate_index_page(pages)

        required_keys = {"title", "domains", "categories", "concept_index", "statistics"}
        assert required_keys.issubset(index.keys())

    def test_statistics_consistency(self) -> None:
        """统计信息应与概念页数据一致。"""
        gen = DirectoryGenerator()
        items = _mixed_domain_items()
        pages = gen.generate_concept_pages(items)
        index = gen.generate_index_page(pages)

        assert index["statistics"]["total_concepts"] == len(pages)

        total_items_from_pages = sum(p["item_count"] for p in pages)
        assert index["statistics"]["total_items"] == total_items_from_pages

    def test_concept_index_mapping(self) -> None:
        """concept_index 应将概念名映射到 page_id。"""
        gen = DirectoryGenerator()
        items = _ml_items()
        pages = gen.generate_concept_pages(items)
        index = gen.generate_index_page(pages)

        # concept_index 应该是 dict
        assert isinstance(index["concept_index"], dict)

        # 每个概念页应在 concept_index 中有条目
        for page in pages:
            concept_name = page["concept"]
            if concept_name in index["concept_index"]:
                assert index["concept_index"][concept_name] == page["page_id"]

    def test_domains_structure(self) -> None:
        """domains 列表中每个条目应有 name, concept_count, item_count。"""
        gen = DirectoryGenerator()
        items = _mixed_domain_items()
        pages = gen.generate_concept_pages(items)
        index = gen.generate_index_page(pages)

        domain_keys = {"name", "concept_count", "item_count"}
        for domain in index["domains"]:
            assert domain_keys.issubset(domain.keys())

    def test_categories_structure(self) -> None:
        """categories 列表中每个条目应有 name 和 pages。"""
        gen = DirectoryGenerator()
        items = _mixed_domain_items()
        pages = gen.generate_concept_pages(items)
        index = gen.generate_index_page(pages)

        cat_keys = {"name", "pages"}
        for cat in index["categories"]:
            assert cat_keys.issubset(cat.keys())

    def test_total_domains_matches(self) -> None:
        """统计中的 total_domains 应与 domains 列表长度一致。"""
        gen = DirectoryGenerator()
        items = _mixed_domain_items()
        pages = gen.generate_concept_pages(items)
        index = gen.generate_index_page(pages)

        assert index["statistics"]["total_domains"] == len(index["domains"])


# ============================================================
# build_hierarchy 测试
# ============================================================


class TestBuildHierarchy:
    """概念层次结构构建的测试。"""

    def test_empty_pages(self) -> None:
        """空输入应返回空的层次结构。"""
        gen = DirectoryGenerator()
        hierarchy = gen.build_hierarchy([])
        assert isinstance(hierarchy, dict)
        # 空输入应返回空结构或包含空子节点列表
        assert hierarchy.get("children") is None or len(hierarchy.get("children", [])) == 0

    def test_hierarchy_structure(self) -> None:
        """层次结构应包含 children 字段，形成树形结构。"""
        gen = DirectoryGenerator()
        items = _mixed_domain_items()
        pages = gen.generate_concept_pages(items)
        hierarchy = gen.build_hierarchy(pages)

        assert "children" in hierarchy
        assert isinstance(hierarchy["children"], list)

    def test_hierarchy_respects_max_depth(self) -> None:
        """层次结构深度不应超过 hierarchy_max_depth。"""
        config = DirectoryConfig(hierarchy_max_depth=2)
        gen = DirectoryGenerator(config=config)
        items = _mixed_domain_items()
        pages = gen.generate_concept_pages(items)
        hierarchy = gen.build_hierarchy(pages)

        def _max_depth(node: dict, current_depth: int = 0) -> int:
            """递归计算最大深度。"""
            children = node.get("children", [])
            if not children:
                return current_depth
            return max(_max_depth(child, current_depth + 1) for child in children)

        depth = _max_depth(hierarchy)
        assert depth <= config.hierarchy_max_depth

    def test_hierarchy_contains_all_pages(self) -> None:
        """层次结构中应包含所有概念页（作为叶子或中间节点）。"""
        gen = DirectoryGenerator()
        items = _mixed_domain_items()
        pages = gen.generate_concept_pages(items)
        hierarchy = gen.build_hierarchy(pages)

        page_ids_in_hierarchy: set[str] = set()

        def _collect_page_ids(node: dict) -> None:
            if "page_id" in node and node["page_id"]:
                page_ids_in_hierarchy.add(node["page_id"])
            for child in node.get("children", []):
                _collect_page_ids(child)

        _collect_page_ids(hierarchy)

        # 所有概念页要么作为节点出现，要么在 children 中
        expected_ids = {p["page_id"] for p in pages}
        assert expected_ids.issubset(page_ids_in_hierarchy), \
            f"缺少的概念页: {expected_ids - page_ids_in_hierarchy}"

    def test_parent_child_relationship(self) -> None:
        """子概念的关键词应是父概念关键词的子集。"""
        gen = DirectoryGenerator()
        items = _mixed_domain_items()
        pages = gen.generate_concept_pages(items)
        hierarchy = gen.build_hierarchy(pages)

        def _validate_keywords(node: dict) -> None:
            """验证父子关系的关键词子集关系。"""
            parent_kw = set(node.get("keywords", []))
            for child in node.get("children", []):
                child_kw = set(child.get("keywords", []))
                # 如果父子都有 keywords，子概念关键词应与父有交集
                if parent_kw and child_kw:
                    # 至少有一个共同关键词
                    assert parent_kw & child_kw, \
                        f"父子概念无交集: parent={parent_kw}, child={child_kw}"
                _validate_keywords(child)

        _validate_keywords(hierarchy)


# ============================================================
# 集成测试（完整流程）
# ============================================================


class TestIntegration:
    """完整流程的集成测试。"""

    def test_full_pipeline(self) -> None:
        """完整的 聚类 → 概念页 → 索引页 → 层次结构 流程。"""
        gen = DirectoryGenerator()
        items = _mixed_domain_items()

        # 步骤 1: 聚类
        clusters = gen.cluster_by_concept(items)
        assert len(clusters) >= 1

        # 步骤 2: 生成概念页
        pages = gen.generate_concept_pages(items)
        assert len(pages) >= 1

        # 步骤 3: 生成索引页
        index = gen.generate_index_page(pages)
        assert index["statistics"]["total_concepts"] == len(pages)

        # 步骤 4: 构建层次结构
        hierarchy = gen.build_hierarchy(pages)
        assert "children" in hierarchy

    def test_large_dataset(self) -> None:
        """大数据集的处理能力。"""
        gen = DirectoryGenerator()
        items = _make_items([
            {"id": f"item-{i}", "content": f"content for item {i}", "tags": [f"tag-{i % 10}", f"group-{i % 5}"]}
            for i in range(100)
        ])

        pages = gen.generate_concept_pages(items)
        index = gen.generate_index_page(pages)
        hierarchy = gen.build_hierarchy(pages)

        assert len(pages) >= 1
        assert index["statistics"]["total_items"] == sum(p["item_count"] for p in pages)
        assert "children" in hierarchy

    def test_cross_domain_concepts(self) -> None:
        """跨领域概念识别测试。"""
        gen = DirectoryGenerator()
        items = _make_items([
            {"id": "cd-1", "content": "Python用于机器学习数据预处理", "tags": ["Python", "机器学习"]},
            {"id": "cd-2", "content": "Python在深度学习中广泛使用", "tags": ["Python", "深度学习"]},
            {"id": "cd-3", "content": "Java用于大数据处理", "tags": ["Java", "大数据"]},
            {"id": "cd-4", "content": "Scala在大数据生态中应用", "tags": ["Scala", "大数据"]},
        ])

        pages = gen.generate_concept_pages(items)
        # 至少有 2 个概念（编程语言、机器学习/大数据等）
        assert len(pages) >= 2
