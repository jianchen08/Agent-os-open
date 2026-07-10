"""模板数据类型定义。

定义模板系统的核心数据结构，包括模板类型、章节、评估维度和模板规格。

公共 API:
    TemplateType: 模板类型枚举（A 型消费型 / B 型报告型）
    TemplateSection: 模板章节数据类
    EvaluationDimension: 评估维度数据类
    TemplateSpec: 模板完整规格数据类
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TemplateType(Enum):
    """模板类型。

    Attributes:
        CONSUMABLE: A 型 — 消费型，被下游 Agent 解析执行，不需要评估指南。
        DELIVERABLE: B 型 — 报告型，作为最终交付物，必须有评估指南。
    """

    CONSUMABLE = "A"
    DELIVERABLE = "B"


@dataclass
class TemplateSection:
    """模板章节。

    Attributes:
        title: 章节标题。
        required: 章节标注（required, optional, conditional, on_demand）。
        description: 章节说明。
        fields: 章节字段列表，每项为字段描述字典。
        content_template: 章节内容模板文本。
    """

    title: str = ""
    required: str = "optional"
    description: str = ""
    fields: list[dict[str, Any]] = field(default_factory=list)
    content_template: str = ""


@dataclass
class EvaluationDimension:
    """评估维度。

    Attributes:
        name: 维度名称。
        check_content: 检查内容描述。
        required: 是否必填维度。
        pass_criteria: 通过标准。
    """

    name: str = ""
    check_content: str = ""
    required: bool = True
    pass_criteria: str = ""


@dataclass
class TemplateSpec:
    """模板完整规格。

    Attributes:
        template_id: 模板唯一标识。
        name: 模板名称。
        template_type: 模板类型（A 型 / B 型）。
        description: 模板描述（模板是什么）。
        purpose: 模板作用列表。
        usage: 如何使用本模板。
        scenarios: 适用场景列表。
        upstream_templates: 上游模板 ID 列表。
        downstream_templates: 下游模板 ID 列表。
        sections: 模板章节列表。
        evaluation_dimensions: 评估维度列表。
        placeholders: 占位符列表。
        raw_content: 原始 Markdown 内容。
        source_path: 源文件路径。
    """

    template_id: str = ""
    name: str = ""
    template_type: TemplateType = TemplateType.DELIVERABLE
    description: str = ""
    purpose: list[str] = field(default_factory=list)
    usage: list[str] = field(default_factory=list)
    scenarios: list[str] = field(default_factory=list)
    upstream_templates: list[str] = field(default_factory=list)
    downstream_templates: list[str] = field(default_factory=list)
    sections: list[TemplateSection] = field(default_factory=list)
    evaluation_dimensions: list[EvaluationDimension] = field(default_factory=list)
    placeholders: list[str] = field(default_factory=list)
    raw_content: str = ""
    source_path: str = ""
