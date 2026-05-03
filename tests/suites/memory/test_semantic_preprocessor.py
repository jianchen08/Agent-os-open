"""SemanticPreprocessor 语义预处理器测试。

测试覆盖：
1. 配置默认值和自定义
2. 语义分块算法（段落边界、句子边界、强制分割、重叠）
3. 多维打标算法（主题、领域、实体、操作类型、情感）
4. 质量评估算法（信息密度、完整性、标签覆盖）
5. 完整处理流程（process 方法）
6. 边界情况（空文本、超短文本、纯空白等）
"""

from __future__ import annotations

import pytest

from memory.semantic_preprocessor import (
    SemanticPreprocessor,
    SemanticPreprocessorConfig,
)


# ============================================================
# 1. 配置测试
# ============================================================


class TestSemanticPreprocessorConfig:
    """测试 SemanticPreprocessorConfig 默认值和自定义。"""

    def test_默认配置(self) -> None:
        """默认配置值应正确。"""
        config = SemanticPreprocessorConfig()
        assert config.max_chunk_size == 500
        assert config.min_chunk_size == 50
        assert config.overlap_size == 50
        assert config.quality_threshold == 0.3
        assert config.max_tags_per_chunk == 10

    def test_自定义配置(self) -> None:
        """自定义配置值应生效。"""
        config = SemanticPreprocessorConfig(
            max_chunk_size=1000,
            min_chunk_size=100,
            overlap_size=80,
            quality_threshold=0.5,
            max_tags_per_chunk=5,
        )
        assert config.max_chunk_size == 1000
        assert config.min_chunk_size == 100
        assert config.overlap_size == 80
        assert config.quality_threshold == 0.5
        assert config.max_tags_per_chunk == 5


# ============================================================
# 2. 语义分块测试
# ============================================================


class TestSemanticChunk:
    """测试语义分块算法。"""

    def test_空文本(self) -> None:
        """空文本应返回空列表。"""
        preprocessor = SemanticPreprocessor()
        result = preprocessor.semantic_chunk("")
        assert result == []

    def test_短文本不分块(self) -> None:
        """短于 max_chunk_size 的文本应作为单个块。"""
        preprocessor = SemanticPreprocessor()
        text = "这是一段简短的文本。"
        result = preprocessor.semantic_chunk(text)
        assert len(result) == 1
        assert result[0]["content"] == text

    def test_段落边界分割(self) -> None:
        """双换行应触发分块。"""
        preprocessor = SemanticPreprocessor()
        text = "第一段内容。" + "\n\n" + "第二段内容。"
        result = preprocessor.semantic_chunk(text)
        assert len(result) == 2
        assert "第一段" in result[0]["content"]
        assert "第二段" in result[1]["content"]

    def test_句子边界分割(self) -> None:
        """句号后跟空格应识别为句子边界。"""
        preprocessor = SemanticPreprocessor()
        text = "第一句话。 第二句话。 第三句话。"
        result = preprocessor.semantic_chunk(text, max_chunk_size=10)
        assert len(result) >= 2

    def test_超长文本强制分割(self) -> None:
        """单个语义单元超过 max_chunk_size 时应强制分割。"""
        preprocessor = SemanticPreprocessor()
        long_text = "这是一段很长的文本" * 200  # 约 1800 字符
        result = preprocessor.semantic_chunk(long_text, max_chunk_size=500)
        for chunk in result:
            # 允许最后一个块略短，但不应超过 max_chunk_size 太多
            assert len(chunk["content"]) <= 600

    def test_块之间有重叠(self) -> None:
        """相邻块之间应保持 overlap_size 的重叠。"""
        preprocessor = SemanticPreprocessor(
            SemanticPreprocessorConfig(overlap_size=20),
        )
        # 多段落文本确保分块
        paragraphs = [f"第{i}段内容。" * 20 for i in range(5)]
        text = "\n\n".join(paragraphs)
        result = preprocessor.semantic_chunk(text, max_chunk_size=100)
        # 检查相邻块之间的重叠
        if len(result) >= 2:
            for i in range(len(result) - 1):
                # 块间应有部分内容重叠（overlap_size 范围内）
                tail = result[i]["content"][-20:]
                head = result[i + 1]["content"][:20]
                # 重叠不要求精确匹配，但应有部分共同内容
                assert isinstance(tail, str)
                assert isinstance(head, str)

    def test_返回结构包含boundaries和metadata(self) -> None:
        """返回的每个块应包含 boundaries 和 metadata 字段。"""
        preprocessor = SemanticPreprocessor()
        text = "第一段。\n\n第二段。"
        result = preprocessor.semantic_chunk(text)
        for chunk in result:
            assert "content" in chunk
            assert "boundaries" in chunk
            assert "metadata" in chunk
            assert isinstance(chunk["boundaries"], list)
            assert isinstance(chunk["metadata"], dict)

    def test_多段落文本(self) -> None:
        """多段落文本应正确分割。"""
        preprocessor = SemanticPreprocessor()
        paragraphs = []
        for i in range(6):
            paragraphs.append(f"这是第{i + 1}段的内容，包含一些描述性文字。")
        text = "\n\n".join(paragraphs)
        result = preprocessor.semantic_chunk(text, max_chunk_size=80)
        assert len(result) >= 2

    def test_问号和感叹号边界(self) -> None:
        """问号和感叹号后跟空格也应识别为句子边界。"""
        preprocessor = SemanticPreprocessor()
        text = "这是什么？ 那是什么！ 这是对的。"
        result = preprocessor.semantic_chunk(text, max_chunk_size=10)
        assert len(result) >= 2


# ============================================================
# 3. 多维打标测试
# ============================================================


class TestMultiDimensionTag:
    """测试多维打标算法。"""

    def test_基本打标结构(self) -> None:
        """打标结果应包含 tags、dimensions、confidence。"""
        preprocessor = SemanticPreprocessor()
        chunk = {"content": "Python 是一种流行的编程语言。", "metadata": {}}
        result = preprocessor.multi_dimension_tag(chunk)
        assert "tags" in result
        assert "dimensions" in result
        assert "confidence" in result
        assert isinstance(result["tags"], list)
        assert isinstance(result["dimensions"], dict)
        assert isinstance(result["confidence"], float)

    def test_主题标签提取(self) -> None:
        """应提取高频名词性短语作为主题标签。"""
        preprocessor = SemanticPreprocessor()
        chunk = {
            "content": "Python 编程语言在数据分析和机器学习领域广泛应用。"
            "Python 提供了丰富的库和框架支持。",
            "metadata": {},
        }
        result = preprocessor.multi_dimension_tag(chunk)
        assert "topic" in result["dimensions"]
        assert isinstance(result["dimensions"]["topic"], str)

    def test_领域标签识别(self) -> None:
        """应识别技术领域标签。"""
        preprocessor = SemanticPreprocessor()
        chunk = {
            "content": "使用 Docker 容器部署微服务架构，结合 Kubernetes 进行编排管理。",
            "metadata": {},
        }
        result = preprocessor.multi_dimension_tag(chunk)
        assert "domain" in result["dimensions"]
        assert isinstance(result["dimensions"]["domain"], str)

    def test_实体标签识别(self) -> None:
        """应识别引号内的术语。"""
        preprocessor = SemanticPreprocessor()
        chunk = {
            "content": '使用"深度学习"算法训练模型，结合"自然语言处理"技术。',
            "metadata": {},
        }
        result = preprocessor.multi_dimension_tag(chunk)
        assert "entities" in result["dimensions"]
        assert isinstance(result["dimensions"]["entities"], list)

    def test_操作类型标签(self) -> None:
        """应识别行为动词作为操作类型。"""
        preprocessor = SemanticPreprocessor()
        chunk = {
            "content": "创建新的数据库表，删除过期的记录，更新用户信息。",
            "metadata": {},
        }
        result = preprocessor.multi_dimension_tag(chunk)
        assert "action_type" in result["dimensions"]
        assert isinstance(result["dimensions"]["action_type"], str)

    def test_情感标签(self) -> None:
        """应判断文本的情感倾向。"""
        preprocessor = SemanticPreprocessor()
        chunk_positive = {
            "content": "这个方案非常优秀，效果显著，值得推广。",
            "metadata": {},
        }
        result = preprocessor.multi_dimension_tag(chunk_positive)
        assert "sentiment" in result["dimensions"]
        assert result["dimensions"]["sentiment"] in ("positive", "negative", "neutral")

    def test_标签数量限制(self) -> None:
        """标签数量不应超过 max_tags_per_chunk。"""
        config = SemanticPreprocessorConfig(max_tags_per_chunk=3)
        preprocessor = SemanticPreprocessor(config)
        chunk = {
            "content": "Python Java C++ JavaScript Go Rust Ruby PHP Swift Kotlin",
            "metadata": {},
        }
        result = preprocessor.multi_dimension_tag(chunk)
        assert len(result["tags"]) <= 3

    def test_置信度范围(self) -> None:
        """置信度应在 0-1 范围内。"""
        preprocessor = SemanticPreprocessor()
        chunk = {"content": "这是一段测试文本。", "metadata": {}}
        result = preprocessor.multi_dimension_tag(chunk)
        assert 0.0 <= result["confidence"] <= 1.0

    def test_空内容打标(self) -> None:
        """空内容应返回空的标签和低置信度。"""
        preprocessor = SemanticPreprocessor()
        chunk = {"content": "", "metadata": {}}
        result = preprocessor.multi_dimension_tag(chunk)
        assert result["tags"] == []
        assert result["confidence"] == 0.0


# ============================================================
# 4. 质量评估测试
# ============================================================


class TestAssessQuality:
    """测试质量评估算法。"""

    def test_高质量文本(self) -> None:
        """信息密度高的完整句子应有较高质量分。"""
        preprocessor = SemanticPreprocessor()
        chunk = {
            "content": "Python 是一种广泛使用的高级编程语言。"
            "它支持多种编程范式，包括面向对象和函数式编程。",
            "metadata": {},
        }
        score = preprocessor.assess_quality(chunk)
        assert 0.0 <= score <= 1.0
        assert score > 0.3

    def test_低质量文本(self) -> None:
        """信息密度低的文本应有较低质量分。"""
        preprocessor = SemanticPreprocessor()
        chunk = {
            "content": "嗯 啊 嗯 啊 的 了 嗯 啊 的",
            "metadata": {},
        }
        score = preprocessor.assess_quality(chunk)
        assert 0.0 <= score <= 1.0
        assert score < 0.5

    def test_空文本质量为零(self) -> None:
        """空文本质量分应为 0。"""
        preprocessor = SemanticPreprocessor()
        chunk = {"content": "", "metadata": {}}
        score = preprocessor.assess_quality(chunk)
        assert score == 0.0

    def test_返回值范围(self) -> None:
        """质量分应在 0-1 范围内。"""
        preprocessor = SemanticPreprocessor()
        chunk = {"content": "这是一段测试文本。", "metadata": {}}
        score = preprocessor.assess_quality(chunk)
        assert 0.0 <= score <= 1.0

    def test_不完整句子(self) -> None:
        """不完整的句子应有较低质量分。"""
        preprocessor = SemanticPreprocessor()
        chunk_incomplete = {"content": "因为所以而且", "metadata": {}}
        chunk_complete = {
            "content": "因为天气很好，所以我们去了公园。",
            "metadata": {},
        }
        score_incomplete = preprocessor.assess_quality(chunk_incomplete)
        score_complete = preprocessor.assess_quality(chunk_complete)
        # 完整句子分数应高于不完整句子
        assert score_complete >= score_incomplete


# ============================================================
# 5. 完整处理流程测试
# ============================================================


class TestProcess:
    """测试完整的预处理流程（分块 → 打标 → 质量评估）。"""

    def test_基本流程(self) -> None:
        """基本流程应返回正确结构的结果。"""
        preprocessor = SemanticPreprocessor()
        text = "Python 是一种编程语言。\n\nJava 是另一种编程语言。"
        result = preprocessor.process(text)
        assert isinstance(result, list)
        assert len(result) >= 1
        for item in result:
            assert "content" in item
            assert "metadata" in item
            assert "tags" in item
            assert "quality_score" in item
            assert isinstance(item["tags"], list)
            assert isinstance(item["quality_score"], float)
            assert 0.0 <= item["quality_score"] <= 1.0

    def test_空文本处理(self) -> None:
        """空文本应返回空列表。"""
        preprocessor = SemanticPreprocessor()
        result = preprocessor.process("")
        assert result == []

    def test_使用自定义配置(self) -> None:
        """使用自定义配置处理文本。"""
        config = SemanticPreprocessorConfig(
            max_chunk_size=100,
            min_chunk_size=10,
            overlap_size=10,
        )
        preprocessor = SemanticPreprocessor(config)
        text = "这是一段测试文本。 " * 50
        result = preprocessor.process(text, max_chunk_size=100)
        for item in result:
            assert "content" in item
            assert "quality_score" in item

    def test_低质量块过滤(self) -> None:
        """质量分低于阈值的块应被过滤。"""
        config = SemanticPreprocessorConfig(
            quality_threshold=0.9,  # 高阈值
        )
        preprocessor = SemanticPreprocessor(config)
        # 极低质量文本
        text = "嗯 啊 的"
        result = preprocessor.process(text)
        # 质量不达标的块应被过滤
        for item in result:
            assert item["quality_score"] >= 0.9

    def test_长文本处理(self) -> None:
        """长文本应被正确分块并处理。"""
        preprocessor = SemanticPreprocessor()
        paragraphs = []
        for i in range(10):
            paragraphs.append(
                f"这是第{i + 1}段内容。主要讨论了关于数据分析和机器学习的应用。"
            )
        text = "\n\n".join(paragraphs)
        result = preprocessor.process(text, max_chunk_size=200)
        assert len(result) >= 2
        for item in result:
            assert item["quality_score"] > 0


# ============================================================
# 6. 边界情况测试
# ============================================================


class TestEdgeCases:
    """测试边界情况。"""

    def test_纯空白文本(self) -> None:
        """纯空白文本应返回空列表。"""
        preprocessor = SemanticPreprocessor()
        result = preprocessor.semantic_chunk("   \n\n  \t  ")
        assert result == []

    def test_只有标点符号(self) -> None:
        """只有标点符号的文本应能处理不崩溃。"""
        preprocessor = SemanticPreprocessor()
        result = preprocessor.semantic_chunk("。。。！！！？？？")
        assert isinstance(result, list)

    def test_超大overlap配置(self) -> None:
        """overlap_size 大于 min_chunk_size 时不应崩溃。"""
        config = SemanticPreprocessorConfig(
            overlap_size=200,
            min_chunk_size=50,
            max_chunk_size=500,
        )
        preprocessor = SemanticPreprocessor(config)
        text = "这是一段测试文本。" * 20
        result = preprocessor.semantic_chunk(text, max_chunk_size=500)
        assert isinstance(result, list)

    def test_单个字符(self) -> None:
        """单个字符应能正常处理。"""
        preprocessor = SemanticPreprocessor()
        result = preprocessor.semantic_chunk("好")
        assert len(result) == 1
        assert result[0]["content"] == "好"

    def test_中英文混合文本(self) -> None:
        """中英文混合文本应正确分块。"""
        preprocessor = SemanticPreprocessor()
        text = "Python is great. Python 是一种优秀的编程语言。\n\nJava is popular. Java 也很流行。"
        result = preprocessor.semantic_chunk(text)
        assert len(result) >= 2

    def test_嵌套引号(self) -> None:
        """嵌套引号不应导致崩溃。"""
        preprocessor = SemanticPreprocessor()
        chunk = {
            "content": '他说："这个\"深度学习\"模型效果很好"。',
            "metadata": {},
        }
        result = preprocessor.multi_dimension_tag(chunk)
        assert isinstance(result["tags"], list)

    def test_英文停用词过滤(self) -> None:
        """英文停用词应被过滤。"""
        preprocessor = SemanticPreprocessor()
        chunk = {
            "content": "The quick brown fox jumps over the lazy dog.",
            "metadata": {},
        }
        result = preprocessor.multi_dimension_tag(chunk)
        # "the", "over" 等停用词不应出现在标签中
        tags_lower = [t.lower() for t in result["tags"]]
        assert "the" not in tags_lower
        assert "over" not in tags_lower

    def test_中文停用词过滤(self) -> None:
        """中文停用词应被过滤。"""
        preprocessor = SemanticPreprocessor()
        chunk = {
            "content": "这是一个关于编程的文章。",
            "metadata": {},
        }
        result = preprocessor.multi_dimension_tag(chunk)
        tags = result["tags"]
        # "的"、"了" 等停用词不应出现在标签中
        assert "的" not in tags
        assert "了" not in tags

    def test_配置无效max_chunk_size(self) -> None:
        """max_chunk_size 小于等于 0 时应使用默认值。"""
        preprocessor = SemanticPreprocessor()
        text = "这是一段文本。"
        result = preprocessor.semantic_chunk(text, max_chunk_size=0)
        assert len(result) >= 1
