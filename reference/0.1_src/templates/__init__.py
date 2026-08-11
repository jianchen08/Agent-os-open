"""模板系统公共 API。

提供模板加载和注册管理的统一入口。
模板本质是上下文注入——把模板内容读到管道 state 中，
由 prompt_build 组装到系统提示词里，Agent 按模板格式输出。

公共 API:
    TemplateType: 模板类型枚举
    TemplateSection: 模板章节数据类
    EvaluationDimension: 评估维度数据类
    TemplateSpec: 模板完整规格数据类
    TemplateLoader: 模板加载器
    TemplateRegistry: 模板注册表
"""

from .loader import TemplateLoader
from .registry import TemplateRegistry
from .types import (
    EvaluationDimension,
    TemplateSection,
    TemplateSpec,
    TemplateType,
)

__all__ = [
    "EvaluationDimension",
    "TemplateLoader",
    "TemplateRegistry",
    "TemplateSection",
    "TemplateSpec",
    "TemplateType",
]
