"""模板系统测试。

覆盖：
1. types.py — 数据类创建、默认值、枚举值
2. loader.py — Markdown 解析、元数据提取、占位符提取、章节解析、评估维度
3. registry.py — 注册/查找/筛选/批量加载
"""

import tempfile
from pathlib import Path

import pytest

from templates import (
    EvaluationDimension,
    TemplateLoader,
    TemplateRegistry,
    TemplateSection,
    TemplateSpec,
    TemplateType,
)


# ============================================================
# types.py 测试
# ============================================================


class TestTemplateType:
    """TemplateType 枚举测试。"""

    def test_consumable_value(self) -> None:
        """A 型消费型枚举值为 'A'。"""
        assert TemplateType.CONSUMABLE.value == "A"

    def test_deliverable_value(self) -> None:
        """B 型报告型枚举值为 'B'。"""
        assert TemplateType.DELIVERABLE.value == "B"

    def test_from_value(self) -> None:
        """从字符串值创建枚举。"""
        assert TemplateType("A") == TemplateType.CONSUMABLE
        assert TemplateType("B") == TemplateType.DELIVERABLE


class TestTemplateSection:
    """TemplateSection 数据类测试。"""

    def test_default_values(self) -> None:
        """默认值测试。"""
        section = TemplateSection()
        assert section.title == ""
        assert section.required == "optional"
        assert section.description == ""
        assert section.fields == []
        assert section.content_template == ""

    def test_custom_values(self) -> None:
        """自定义值测试。"""
        section = TemplateSection(
            title="基本信息",
            required="required",
            description="记录基本信息",
            fields=[{"name": "task_id", "type": "string"}],
            content_template="- **任务ID**: {task_id}",
        )
        assert section.title == "基本信息"
        assert section.required == "required"
        assert len(section.fields) == 1


class TestEvaluationDimension:
    """EvaluationDimension 数据类测试。"""

    def test_default_values(self) -> None:
        """默认值测试。"""
        dim = EvaluationDimension()
        assert dim.name == ""
        assert dim.check_content == ""
        assert dim.required is True
        assert dim.pass_criteria == ""

    def test_custom_values(self) -> None:
        """自定义值测试。"""
        dim = EvaluationDimension(
            name="完整性",
            check_content="所有必填章节已填写",
            required=True,
            pass_criteria="无遗漏",
        )
        assert dim.name == "完整性"
        assert dim.required is True


class TestTemplateSpec:
    """TemplateSpec 数据类测试。"""

    def test_default_values(self) -> None:
        """默认值测试。"""
        spec = TemplateSpec()
        assert spec.template_id == ""
        assert spec.template_type == TemplateType.DELIVERABLE
        assert spec.sections == []
        assert spec.evaluation_dimensions == []
        assert spec.placeholders == []

    def test_custom_values(self) -> None:
        """自定义值测试。"""
        spec = TemplateSpec(
            template_id="research_report",
            name="调研报告",
            template_type=TemplateType.DELIVERABLE,
            description="调研结果文档",
            purpose=["结果结构化"],
            placeholders=["research_type", "date"],
        )
        assert spec.template_id == "research_report"
        assert spec.template_type == TemplateType.DELIVERABLE
        assert len(spec.purpose) == 1
        assert len(spec.placeholders) == 2


# ============================================================
# loader.py 测试
# ============================================================


class TestTemplateLoader:
    """TemplateLoader 测试。"""

    @pytest.fixture
    def loader(self) -> TemplateLoader:
        """创建加载器实例。"""
        return TemplateLoader()

    @pytest.fixture
    def research_template_path(self) -> str:
        """调研报告模板路径。"""
        return str(
            Path(__file__).parent.parent
            / "config"
            / "templates"
            / "research_report_template.md"
        )

    @pytest.fixture
    def env_template_path(self) -> str:
        """环境状态模板路径。"""
        return str(
            Path(__file__).parent.parent
            / "config"
            / "templates"
            / "environment_status_template.md"
        )

    @pytest.fixture
    def templates_dir(self) -> str:
        """模板目录路径。"""
        return str(Path(__file__).parent.parent / "config" / "templates")

    def test_load_from_markdown_file_not_found(
        self, loader: TemplateLoader
    ) -> None:
        """加载不存在的文件抛出 FileNotFoundError。"""
        with pytest.raises(FileNotFoundError):
            loader.load_from_markdown("/nonexistent/path.md")

    def test_load_from_markdown_empty_file(
        self, loader: TemplateLoader
    ) -> None:
        """加载空文件抛出 ValueError。"""
        tmp_path = Path(tempfile.gettempdir()) / "test_empty_template.md"
        try:
            tmp_path.write_text("", encoding="utf-8")
            with pytest.raises(ValueError, match="模板文件为空"):
                loader.load_from_markdown(str(tmp_path))
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_load_research_template(
        self,
        loader: TemplateLoader,
        research_template_path: str,
    ) -> None:
        """加载调研报告模板。"""
        spec = loader.load_from_markdown(research_template_path)
        assert spec.template_id == "research_report"
        assert spec.name == "调研报告模板"
        assert spec.source_path == research_template_path

    def test_load_research_template_has_description(
        self,
        loader: TemplateLoader,
        research_template_path: str,
    ) -> None:
        """调研报告模板有描述。"""
        spec = loader.load_from_markdown(research_template_path)
        assert "调研报告" in spec.description

    def test_load_research_template_has_purpose(
        self,
        loader: TemplateLoader,
        research_template_path: str,
    ) -> None:
        """调研报告模板有作用列表。"""
        spec = loader.load_from_markdown(research_template_path)
        assert len(spec.purpose) > 0

    def test_load_research_template_has_usage(
        self,
        loader: TemplateLoader,
        research_template_path: str,
    ) -> None:
        """调研报告模板有使用方法。"""
        spec = loader.load_from_markdown(research_template_path)
        assert len(spec.usage) > 0

    def test_load_research_template_has_scenarios(
        self,
        loader: TemplateLoader,
        research_template_path: str,
    ) -> None:
        """调研报告模板有适用场景。"""
        spec = loader.load_from_markdown(research_template_path)
        assert len(spec.scenarios) > 0

    def test_load_research_template_has_placeholders(
        self,
        loader: TemplateLoader,
        research_template_path: str,
    ) -> None:
        """调研报告模板提取了占位符。"""
        spec = loader.load_from_markdown(research_template_path)
        assert "research_type" in spec.placeholders
        assert "date" in spec.placeholders

    def test_load_research_template_has_sections(
        self,
        loader: TemplateLoader,
        research_template_path: str,
    ) -> None:
        """调研报告模板有章节。"""
        spec = loader.load_from_markdown(research_template_path)
        assert len(spec.sections) > 0
        # 找到基本信息章节
        info_sections = [
            s for s in spec.sections if s.title == "基本信息"
        ]
        assert len(info_sections) == 1
        assert info_sections[0].required == "required"

    def test_load_research_template_section_markers(
        self,
        loader: TemplateLoader,
        research_template_path: str,
    ) -> None:
        """调研报告模板章节标注正确。"""
        spec = loader.load_from_markdown(research_template_path)
        section_map = {s.title: s.required for s in spec.sections}
        assert section_map.get("基本信息") == "required"
        assert section_map.get("摘要") == "required"
        assert section_map.get("方案对比") == "optional"

    def test_load_research_template_type_b(
        self,
        loader: TemplateLoader,
        research_template_path: str,
    ) -> None:
        """调研报告模板（有评估维度）为 B 型。"""
        spec = loader.load_from_markdown(research_template_path)
        assert spec.template_type == TemplateType.DELIVERABLE

    def test_load_env_template_type_a(
        self,
        loader: TemplateLoader,
        env_template_path: str,
    ) -> None:
        """环境状态模板（无评估维度）为 A 型。"""
        spec = loader.load_from_markdown(env_template_path)
        assert spec.template_type == TemplateType.CONSUMABLE

    def test_load_research_template_evaluation(
        self,
        loader: TemplateLoader,
        research_template_path: str,
    ) -> None:
        """调研报告模板有评估维度。"""
        spec = loader.load_from_markdown(research_template_path)
        assert len(spec.evaluation_dimensions) > 0
        dim_names = [d.name for d in spec.evaluation_dimensions]
        assert "完整性" in dim_names

    def test_load_from_directory(
        self,
        loader: TemplateLoader,
        templates_dir: str,
    ) -> None:
        """批量加载目录。"""
        templates = loader.load_from_directory(templates_dir)
        assert len(templates) >= 2  # 至少有 research_report 和 environment_status

    def test_load_from_directory_not_found(
        self, loader: TemplateLoader
    ) -> None:
        """加载不存在的目录抛出 FileNotFoundError。"""
        with pytest.raises(FileNotFoundError):
            loader.load_from_directory("/nonexistent/dir")

    def test_placeholder_dedup(self, loader: TemplateLoader) -> None:
        """占位符去重测试。"""
        content = "# Test\n{name} and {name} and {age}"
        placeholders = loader._extract_placeholders(content)
        assert placeholders.count("name") == 1
        assert len(placeholders) == 2



# ============================================================
# registry.py 测试
# ============================================================


class TestTemplateRegistry:
    """TemplateRegistry 测试。"""

    @pytest.fixture
    def registry(self) -> TemplateRegistry:
        """创建空注册表。"""
        return TemplateRegistry()

    @pytest.fixture
    def sample_spec(self) -> TemplateSpec:
        """样例模板规格。"""
        return TemplateSpec(
            template_id="test_report",
            name="测试报告",
            template_type=TemplateType.DELIVERABLE,
            description="测试用报告模板",
        )

    def test_register_and_get(
        self, registry: TemplateRegistry, sample_spec: TemplateSpec
    ) -> None:
        """注册后可按 ID 获取。"""
        registry.register(sample_spec)
        result = registry.get("test_report")
        assert result is not None
        assert result.template_id == "test_report"

    def test_get_nonexistent(
        self, registry: TemplateRegistry
    ) -> None:
        """获取不存在的模板返回 None。"""
        assert registry.get("nonexistent") is None

    def test_unregister(
        self, registry: TemplateRegistry, sample_spec: TemplateSpec
    ) -> None:
        """注销模板。"""
        registry.register(sample_spec)
        assert registry.unregister("test_report") is True
        assert registry.get("test_report") is None

    def test_unregister_nonexistent(
        self, registry: TemplateRegistry
    ) -> None:
        """注销不存在的模板返回 False。"""
        assert registry.unregister("nonexistent") is False

    def test_find_by_type(
        self, registry: TemplateRegistry
    ) -> None:
        """按类型筛选模板。"""
        registry.register(
            TemplateSpec(
                template_id="a1",
                template_type=TemplateType.CONSUMABLE,
            )
        )
        registry.register(
            TemplateSpec(
                template_id="b1",
                template_type=TemplateType.DELIVERABLE,
            )
        )
        consumables = registry.find_by_type(TemplateType.CONSUMABLE)
        deliverables = registry.find_by_type(TemplateType.DELIVERABLE)
        assert len(consumables) == 1
        assert len(deliverables) == 1

    def test_find_by_tag(
        self, registry: TemplateRegistry
    ) -> None:
        """按标签搜索模板。"""
        registry.register(
            TemplateSpec(
                template_id="r1",
                name="调研报告",
                description="技术调研模板",
            )
        )
        registry.register(
            TemplateSpec(
                template_id="e1",
                name="环境报告",
                description="环境检查模板",
            )
        )
        results = registry.find_by_tag("调研")
        assert len(results) == 1
        assert results[0].template_id == "r1"

    def test_load_directory(
        self, registry: TemplateRegistry
    ) -> None:
        """批量加载目录。"""
        templates_dir = str(
            Path(__file__).parent.parent / "config" / "templates"
        )
        count = registry.load_directory(templates_dir)
        assert count >= 2
        assert registry.get("research_report") is not None
        assert registry.get("environment_status") is not None

    def test_register_overwrite(
        self, registry: TemplateRegistry
    ) -> None:
        """重复注册覆盖旧模板。"""
        registry.register(
            TemplateSpec(
                template_id="dup",
                name="版本1",
            )
        )
        registry.register(
            TemplateSpec(
                template_id="dup",
                name="版本2",
            )
        )
        assert registry.get("dup").name == "版本2"

    def test_list_all(
        self, registry: TemplateRegistry
    ) -> None:
        """列出所有模板。"""
        registry.register(TemplateSpec(template_id="t1"))
        registry.register(TemplateSpec(template_id="t2"))
        all_templates = registry.list_all()
        assert len(all_templates) == 2

    def test_count(
        self, registry: TemplateRegistry
    ) -> None:
        """模板数量。"""
        assert registry.count() == 0
        registry.register(TemplateSpec(template_id="t1"))
        assert registry.count() == 1
