"""模板加载器。

从 Markdown 文件加载模板，解析 HTML 注释块中的元数据，
提取占位符、章节标记和评估维度。

公共 API:
    TemplateLoader: 模板加载器类
"""

import re
from pathlib import Path

from .types import (
    EvaluationDimension,
    TemplateSection,
    TemplateSpec,
    TemplateType,
)


class TemplateLoader:
    """从 Markdown 文件加载模板。

    支持：
    - 解析 HTML 注释块中的元数据（模板是什么、作用、使用方法、场景、关系）
    - 提取占位符 ``{xxx}``
    - 识别章节标记 ``[必填]/[可选]/[按需]/[条件必填]``
    - 识别评估维度和通过标准
    """

    # 章节标注映射
    SECTION_MARKERS: dict[str, str] = {
        "必填": "required",
        "可选": "optional",
        "按需": "on_demand",
        "条件必填": "conditional",
    }

    def load_from_markdown(self, path: str | Path) -> TemplateSpec:
        """从 Markdown 文件加载模板。

        Args:
            path: Markdown 文件路径。

        Returns:
            解析后的 TemplateSpec 对象。

        Raises:
            FileNotFoundError: 文件不存在。
            ValueError: 文件内容无法解析为有效模板。
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"模板文件不存在: {path}")

        content = path.read_text(encoding="utf-8")
        if not content.strip():
            raise ValueError(f"模板文件为空: {path}")

        return self._parse_template(content, str(path))

    def load_from_directory(self, dir_path: str | Path) -> list[TemplateSpec]:
        """批量加载目录下所有 ``*_template.md`` 文件。

        跳过以 ``_`` 开头的文件（如 ``_template_spec.md``）和解析失败的文件。

        Args:
            dir_path: 目录路径。

        Returns:
            成功加载的 TemplateSpec 列表。

        Raises:
            FileNotFoundError: 目录不存在。
        """
        dir_path = Path(dir_path)
        if not dir_path.exists():
            raise FileNotFoundError(f"模板目录不存在: {dir_path}")

        templates: list[TemplateSpec] = []
        for md_file in sorted(dir_path.glob("*_template.md")):
            if md_file.name.startswith("_"):
                continue
            try:
                spec = self.load_from_markdown(md_file)
                templates.append(spec)
            except (ValueError, Exception):
                # 跳过解析失败的文件
                continue
        return templates

    def _parse_template(self, content: str, source_path: str) -> TemplateSpec:
        """解析 Markdown 模板内容。

        Args:
            content: Markdown 文件完整内容。
            source_path: 源文件路径。

        Returns:
            解析后的 TemplateSpec。
        """
        spec = TemplateSpec(raw_content=content, source_path=source_path)

        # 提取 template_id（从文件名）
        path = Path(source_path)
        spec.template_id = path.stem.replace("_template", "")

        # 解析头部 HTML 注释块
        self._parse_header_comment(content, spec)

        # 提取占位符
        spec.placeholders = self._extract_placeholders(content)

        # 解析章节
        spec.sections = self._parse_sections(content)

        # 解析评估维度
        spec.evaluation_dimensions = self._parse_evaluation(content)

        # 推断模板类型：有评估维度则为 B 型，否则为 A 型
        if spec.evaluation_dimensions:
            spec.template_type = TemplateType.DELIVERABLE
        else:
            spec.template_type = TemplateType.CONSUMABLE

        return spec

    def _parse_header_comment(self, content: str, spec: TemplateSpec) -> None:
        """解析头部 HTML 注释块中的元数据。

        按旧文件规范，头部注释块包含：
        【模板是什么】【模板的作用】【如何使用本模板】【适用场景】【与其他模板的关系】

        Args:
            content: Markdown 内容。
            spec: 待填充的 TemplateSpec。
        """
        # 查找第一个 HTML 注释块
        match = re.search(r"<!--\s*(.*?)\s*-->", content, re.DOTALL)
        if not match:
            return

        comment_text = match.group(1)

        # 提取模板名称（从第一个 # 标题）
        title_match = re.search(r"^\s*#\s+(.+)", content, re.MULTILINE)
        if title_match:
            spec.name = title_match.group(1).strip()

        # 【模板是什么】
        desc_match = re.search(r"【模板是什么】\s*\n(.*?)(?=【|$)", comment_text, re.DOTALL)
        if desc_match:
            spec.description = desc_match.group(1).strip()

        # 【模板的作用】
        purpose_match = re.search(r"【模板的作用】\s*\n(.*?)(?=【|$)", comment_text, re.DOTALL)
        if purpose_match:
            purpose_text = purpose_match.group(1).strip()
            spec.purpose = self._parse_numbered_list(purpose_text)

        # 【如何使用本模板】
        usage_match = re.search(r"【如何使用本模板】\s*\n(.*?)(?=【|$)", comment_text, re.DOTALL)
        if usage_match:
            usage_text = usage_match.group(1).strip()
            spec.usage = self._parse_numbered_list(usage_text)

        # 【适用场景】
        scenarios_match = re.search(r"【适用场景】\s*\n(.*?)(?=【|$)", comment_text, re.DOTALL)
        if scenarios_match:
            scenarios_text = scenarios_match.group(1).strip()
            spec.scenarios = self._parse_scenarios(scenarios_text)

        # 【与其他模板的关系】
        relation_match = re.search(r"【与其他模板的关系】\s*\n(.*?)(?=【|$)", comment_text, re.DOTALL)
        if relation_match:
            relation_text = relation_match.group(1).strip()
            spec.upstream_templates = self._parse_template_refs(relation_text, "上游")
            spec.downstream_templates = self._parse_template_refs(relation_text, "下游")

    def _parse_numbered_list(self, text: str) -> list[str]:
        """解析编号列表（``1. xxx — 说明`` 格式）。

        Args:
            text: 列表文本。

        Returns:
            列表项字符串列表。
        """
        items: list[str] = []
        for line in text.split("\n"):
            line = line.strip()  # noqa: PLW2901
            if not line:
                continue
            # 匹配 "1. xxx" 或 "- xxx" 格式
            match = re.match(r"(?:\d+\.|[-*])\s+(.+)", line)
            if match:
                items.append(match.group(1).strip())
        return items

    def _parse_scenarios(self, text: str) -> list[str]:
        """解析适用场景。

        支持表格格式和列表格式。

        Args:
            text: 场景文本。

        Returns:
            场景描述列表。
        """
        scenarios: list[str] = []
        lines = text.split("\n")
        for line in lines:
            line = line.strip()  # noqa: PLW2901
            if not line or line.startswith(">"):
                continue
            # 跳过表格分隔行
            if re.match(r"^\|[\s\-|]+\|$", line):
                continue
            # 表格行：取第一列作为场景名，第二列作为说明
            if line.startswith("|"):
                cells = [c.strip() for c in line.split("|")]
                cells = [c for c in cells if c]
                if len(cells) >= 2:
                    scenarios.append(f"{cells[0]}：{cells[1]}")
                continue
            match = re.match(r"[-*]\s+(.+)", line)
            if match:
                scenarios.append(match.group(1).strip())
        return scenarios

    def _parse_template_refs(self, text: str, direction: str) -> list[str]:
        """解析上下游模板引用。

        Args:
            text: 关系文本。
            direction: "上游" 或 "下游"。

        Returns:
            模板 ID 列表。
        """
        refs: list[str] = []
        # 匹配 "上游：基于 xxx_template.md" 格式
        pattern = rf"{direction}[：:]\s*(.+?)(?:\n|$)"
        match = re.search(pattern, text)
        if match:
            ref_text = match.group(1).strip()
            # 提取所有 _template.md 引用
            template_refs = re.findall(r"(\w+_template)", ref_text)
            refs.extend(template_refs)
        return refs

    def _extract_placeholders(self, content: str) -> list[str]:
        """提取 ``{xxx}`` 格式占位符。

        去重并保持出现顺序。

        Args:
            content: Markdown 内容。

        Returns:
            占位符名称列表（不含花括号）。
        """
        seen: set[str] = set()
        placeholders: list[str] = []
        for match in re.finditer(r"\{(\w+)\}", content):
            name = match.group(1)
            if name not in seen:
                seen.add(name)
                placeholders.append(name)
        return placeholders

    def _parse_sections(self, content: str) -> list[TemplateSection]:
        """解析章节。

        识别 ``## 章节名称 [标注]`` 格式的章节标题，
        提取章节标注和内容。

        Args:
            content: Markdown 内容。

        Returns:
            TemplateSection 列表。
        """
        sections: list[TemplateSection] = []

        # 匹配 ## 标题（不包含评估指南）
        pattern = r"^##\s+(.+?)(?:\s*\[([^\]]+)\])?\s*$"
        lines = content.split("\n")

        current_section: TemplateSection | None = None
        content_lines: list[str] = []

        for line in lines:
            match = re.match(pattern, line)
            if match:
                # 保存前一个章节
                if current_section is not None:
                    current_section.content_template = "\n".join(content_lines).strip()
                    sections.append(current_section)

                title = match.group(1).strip()
                marker = match.group(2) or ""

                # 跳过评估指南章节
                if "评估指南" in title:
                    current_section = None
                    content_lines = []
                    continue

                required = self.SECTION_MARKERS.get(marker, "optional")

                current_section = TemplateSection(
                    title=title,
                    required=required,
                )
                content_lines = []
            elif current_section is not None:
                content_lines.append(line)

        # 保存最后一个章节
        if current_section is not None:
            current_section.content_template = "\n".join(content_lines).strip()
            sections.append(current_section)

        return sections

    def _parse_evaluation(self, content: str) -> list[EvaluationDimension]:
        """解析评估维度。

        从评估指南注释块中提取检查维度表格。

        Args:
            content: Markdown 内容。

        Returns:
            EvaluationDimension 列表。
        """
        dimensions: list[EvaluationDimension] = []

        # 查找评估指南部分
        eval_match = re.search(r"评估指南.*?检查维度.*?\|(.*?)-->", content, re.DOTALL)
        if not eval_match:
            return dimensions

        eval_text = eval_match.group(1)

        # 解析表格行
        lines = eval_text.split("\n")
        for line in lines:
            line = line.strip()  # noqa: PLW2901
            if not line.startswith("|"):
                continue
            # 跳过分隔行
            if re.match(r"^\|[\s\-|]+\|$", line):
                continue

            cells = [c.strip() for c in line.split("|")]
            # 过滤空单元格
            cells = [c for c in cells if c]
            if len(cells) >= 3:
                name = cells[0].strip().strip("*").strip()
                # 跳过表头
                if name == "维度":
                    continue
                check_content = cells[1].strip() if len(cells) > 1 else ""
                required_str = cells[2].strip() if len(cells) > 2 else "是"
                pass_criteria = cells[3].strip() if len(cells) > 3 else ""

                dimensions.append(
                    EvaluationDimension(
                        name=name,
                        check_content=check_content,
                        required=required_str == "是",
                        pass_criteria=pass_criteria,
                    )
                )

        return dimensions
