"""
能力适配器工具

将外部 MCP Server 封装为统一的 BuiltinTool 接口，
支持 YAML 配置驱动的后端选择和自动回退。

工具列表：
- DesignGenerateTool: 从文字描述/截图生成前端 UI 代码
- DesignReviewTool: 对比设计参考与实现，识别视觉问题
"""

from .design_generate import DesignGenerateTool
from .design_review import DesignReviewTool

__all__ = [
    "DesignGenerateTool",
    "DesignReviewTool",
]
