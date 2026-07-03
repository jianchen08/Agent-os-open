"""模板注册表。

管理模板的注册、查找和筛选。

公共 API:
    TemplateRegistry: 模板注册表类
"""

from pathlib import Path

from .loader import TemplateLoader
from .types import TemplateSpec, TemplateType


class TemplateRegistry:
    """模板注册表。

    支持：
    - 注册/注销模板
    - 按 ID、类型、标签查找模板
    - 批量加载目录下所有模板
    """

    def __init__(self) -> None:
        """初始化注册表。"""
        self._templates: dict[str, TemplateSpec] = {}
        self._loader = TemplateLoader()

    def register(self, template: TemplateSpec) -> None:
        """注册模板。

        如果 template_id 已存在，则覆盖。

        Args:
            template: 模板规格对象。
        """
        self._templates[template.template_id] = template

    def unregister(self, template_id: str) -> bool:
        """注销模板。

        Args:
            template_id: 模板 ID。

        Returns:
            是否成功注销（False 表示模板不存在）。
        """
        if template_id in self._templates:
            del self._templates[template_id]
            return True
        return False

    def get(self, template_id: str) -> TemplateSpec | None:
        """按 ID 获取模板。

        Args:
            template_id: 模板 ID。

        Returns:
            模板规格，不存在时返回 None。
        """
        return self._templates.get(template_id)

    def find_by_type(self, template_type: TemplateType) -> list[TemplateSpec]:
        """按类型筛选模板。

        Args:
            template_type: 模板类型。

        Returns:
            匹配的模板列表。
        """
        return [t for t in self._templates.values() if t.template_type == template_type]

    def find_by_tag(self, tag: str) -> list[TemplateSpec]:
        """按标签筛选模板。

        在模板的 name、description、purpose、scenarios 中搜索标签。

        Args:
            tag: 搜索标签。

        Returns:
            匹配的模板列表。
        """
        tag_lower = tag.lower()
        results: list[TemplateSpec] = []
        for t in self._templates.values():
            searchable = " ".join(
                [
                    t.name,
                    t.description,
                    " ".join(t.purpose),
                    " ".join(t.scenarios),
                ]
            ).lower()
            if tag_lower in searchable:
                results.append(t)
        return results

    def load_directory(self, dir_path: str | Path) -> int:
        """批量加载目录下所有模板并注册。

        Args:
            dir_path: 模板目录路径。

        Returns:
            成功加载并注册的模板数量。
        """
        templates = self._loader.load_from_directory(dir_path)
        for t in templates:
            self.register(t)
        return len(templates)

    def list_all(self) -> list[TemplateSpec]:
        """列出所有已注册模板。

        Returns:
            所有模板列表。
        """
        return list(self._templates.values())

    def count(self) -> int:
        """获取已注册模板数量。

        Returns:
            模板数量。
        """
        return len(self._templates)
