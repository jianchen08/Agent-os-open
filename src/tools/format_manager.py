"""统一格式管理器。

提供统一的格式转换接口，支持 JSON / YAML / XML 等多种输出格式。

暴露接口：
- ToolFormat：格式枚举（JSON / YAML / XML）
- FormatManager：格式管理器
- get_format_manager()：获取全局单例
"""

from __future__ import annotations

import json
import logging
import xml.etree.ElementTree as ET
from enum import Enum
from typing import Any

import yaml

logger = logging.getLogger(__name__)


class ToolFormat(Enum):
    """工具输出格式枚举。"""

    JSON = "json"
    YAML = "yaml"
    XML = "xml"


class FormatManager:
    """格式管理器 —— 将 Python 对象序列化为指定格式字符串。"""

    _default_format: ToolFormat = ToolFormat.YAML

    def __init__(self, default_format: ToolFormat = ToolFormat.YAML) -> None:
        self._default_format = default_format

    @property
    def default_format(self) -> ToolFormat:
        return self._default_format

    def set_default_format(self, fmt: ToolFormat) -> None:
        self._default_format = fmt

    def serialize(self, data: Any, fmt: ToolFormat | None = None) -> str:
        """将 data 序列化为目标格式字符串。"""
        target = fmt or self._default_format
        if target == ToolFormat.JSON:
            return self._to_json(data)
        if target == ToolFormat.YAML:
            return self._to_yaml(data)
        if target == ToolFormat.XML:
            return self._to_xml(data)
        return self._to_json(data)

    def get_tools_for_llm(
        self,
        json_tools: list[dict[str, Any]],
        fmt: ToolFormat | None = None,
        names: list[str] | None = None,
    ) -> list[dict[str, Any]] | str:
        """将 OpenAI function calling 格式的工具列表转换为目标格式。

        被 registry.get_tools_for_llm_format() 调用。
        """
        target = fmt or self._default_format

        if target == ToolFormat.JSON:
            return json_tools

        if target == ToolFormat.YAML:
            filtered = [t for t in json_tools if t.get("function", {}).get("name") in names] if names else json_tools

            return yaml.dump({"tools": filtered}, default_flow_style=False, allow_unicode=True)

        if target == ToolFormat.XML:
            root = ET.Element("tools")
            for t in json_tools:
                func = t.get("function", {})
                tool_el = ET.SubElement(root, "tool")
                ET.SubElement(tool_el, "name").text = func.get("name", "")
                ET.SubElement(tool_el, "description").text = func.get("description", "")
            return ET.tostring(root, encoding="unicode")

        return json_tools

    @staticmethod
    def _to_json(data: Any) -> str:
        return json.dumps(data, ensure_ascii=False, default=str)

    @staticmethod
    def _to_yaml(data: Any) -> str:
        payload = data if isinstance(data, dict) else {"result": data}
        return yaml.dump(payload, default_flow_style=False, allow_unicode=True)

    @staticmethod
    def _to_xml(data: Any) -> str:
        root = ET.Element("result")
        FormatManager._build_xml(root, data)
        return ET.tostring(root, encoding="unicode")

    @staticmethod
    def _build_xml(parent: ET.Element, data: Any) -> None:  # noqa: PLR0911
        if data is None:
            parent.set("nil", "true")
            return
        if isinstance(data, bool):
            parent.text = "true" if data else "false"
            return
        if isinstance(data, (int, float)):
            parent.text = str(data)
            return
        if isinstance(data, str):
            parent.text = data
            return
        if isinstance(data, dict):
            for key, value in data.items():
                child = ET.SubElement(parent, _safe_xml_tag(key))
                FormatManager._build_xml(child, value)
            return
        if isinstance(data, (list, tuple)):
            for item in data:
                child = ET.SubElement(parent, "item")
                FormatManager._build_xml(child, item)
            return
        parent.text = str(data)
        return


def _safe_xml_tag(name: str) -> str:
    """将字符串转换为合法 XML 标签名。"""
    tag = name.replace(" ", "_").replace("-", "_")
    if not tag or tag[0].isdigit():
        tag = f"_{tag}"
    return tag


_instance: FormatManager | None = None


def get_format_manager() -> FormatManager:
    global _instance  # noqa: PLW0603
    if _instance is None:
        _instance = FormatManager()
    return _instance
