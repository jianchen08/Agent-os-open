"""单位换算 + 二进制转换工具——从 0.1 提取核心逻辑为纯函数。

[来源: src/tools/builtin/unit_converter/tool.py, src/tools/builtin/binary_converter/tool.py]
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# ── 单位换算 ──────────────────────────────────────────

UNIT_CONVERTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "value": {"type": "number", "description": "要转换的值"},
        "from_unit": {"type": "string", "description": "源单位"},
        "to_unit": {"type": "string", "description": "目标单位"},
        "category": {
            "type": "string",
            "enum": ["length", "weight", "temperature"],
            "default": "length",
            "description": "单位类别",
        },
    },
    "required": ["value", "from_unit", "to_unit"],
}

_LENGTH_TO_METER = {
    "m": 1.0, "km": 1000.0, "cm": 0.01, "mm": 0.001,
    "mi": 1609.344, "yd": 0.9144, "ft": 0.3048, "in": 0.0254,
}

_WEIGHT_TO_KG = {
    "kg": 1.0, "g": 0.001, "mg": 0.000001,
    "lb": 0.453592, "oz": 0.0283495, "t": 1000.0,
}


def _convert_length(value: float, from_unit: str, to_unit: str) -> float:
    from_unit = from_unit.lower()
    to_unit = to_unit.lower()
    if from_unit not in _LENGTH_TO_METER:
        raise ValueError(f"不支持的长度单位: {from_unit}")
    if to_unit not in _LENGTH_TO_METER:
        raise ValueError(f"不支持的长度单位: {to_unit}")
    meters = value * _LENGTH_TO_METER[from_unit]
    return meters / _LENGTH_TO_METER[to_unit]


def _convert_weight(value: float, from_unit: str, to_unit: str) -> float:
    from_unit = from_unit.lower()
    to_unit = to_unit.lower()
    if from_unit not in _WEIGHT_TO_KG:
        raise ValueError(f"不支持的重量单位: {from_unit}")
    if to_unit not in _WEIGHT_TO_KG:
        raise ValueError(f"不支持的重量单位: {to_unit}")
    kg = value * _WEIGHT_TO_KG[from_unit]
    return kg / _WEIGHT_TO_KG[to_unit]


def _convert_temperature(value: float, from_unit: str, to_unit: str) -> float:
    from_unit = from_unit.upper()
    to_unit = to_unit.upper()
    if from_unit == "C":
        celsius = value
    elif from_unit == "F":
        celsius = (value - 32) * 5 / 9
    elif from_unit == "K":
        celsius = value - 273.15
    else:
        raise ValueError(f"不支持的温度单位: {from_unit}")
    if to_unit == "C":
        return celsius
    if to_unit == "F":
        return celsius * 9 / 5 + 32
    if to_unit == "K":
        return celsius + 273.15
    raise ValueError(f"不支持的温度单位: {to_unit}")


async def unit_converter(
    value: float, from_unit: str, to_unit: str, category: str = "length"
) -> dict[str, Any]:
    """单位换算。"""
    try:
        if category == "length":
            result = _convert_length(value, from_unit, to_unit)
        elif category == "weight":
            result = _convert_weight(value, from_unit, to_unit)
        elif category == "temperature":
            result = _convert_temperature(value, from_unit, to_unit)
        else:
            return {"error": f"不支持的类别: {category}"}
        return {
            "value": value,
            "from_unit": from_unit,
            "to_unit": to_unit,
            "category": category,
            "result": round(result, 10) if isinstance(result, float) else result,
        }
    except ValueError as e:
        return {"error": str(e)}


# ── 二进制文件转 Markdown ─────────────────────────────

BINARY_CONVERTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "file_path": {
            "type": "string",
            "description": "要转换的二进制文件路径（PDF/DOCX/XLSX/PPTX/图片等）",
        },
    },
    "required": ["file_path"],
}

_DOCUMENT_EXTENSIONS = frozenset({".pdf", ".docx", ".doc", ".xlsx", ".xls", ".csv", ".pptx", ".ppt"})
_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff", ".tif", ".svg"})
_REJECTED_EXTENSIONS = frozenset({
    ".mp3", ".mp4", ".wav", ".avi", ".mkv", ".mov", ".flv", ".wmv", ".webm",
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz",
    ".exe", ".dll", ".so", ".dylib",
    ".bin", ".dat", ".db", ".sqlite", ".pyc",
})
_MAX_BINARY_FILE_SIZE = 10 * 1024 * 1024  # 10MB


def _get_file_category(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in _DOCUMENT_EXTENSIONS:
        return "document"
    if suffix in _IMAGE_EXTENSIONS:
        return "image"
    if suffix in _REJECTED_EXTENSIONS:
        return "rejected"
    return "text"


async def binary_converter(file_path: str) -> dict[str, Any]:
    """将二进制文件转换为 Markdown 文本。"""
    path = Path(file_path)
    if not path.exists():
        return {"error": f"文件不存在: {file_path}"}

    category = _get_file_category(path)
    if category not in ("document", "image"):
        return {"error": f"不支持转换此类型文件: {path.name}"}

    file_size = path.stat().st_size
    if file_size > _MAX_BINARY_FILE_SIZE:
        return {"error": f"文件过大 ({file_size} bytes)，超过限制 ({_MAX_BINARY_FILE_SIZE} bytes)"}

    try:
        from markitdown import MarkItDown  # noqa: PLC0415
    except ImportError:
        return {"error": "需要安装 markitdown 库: pip install markitdown"}

    try:
        md = MarkItDown()
        result = md.convert(str(path))
        content = result.text_content
        if content and content.strip():
            return {
                "file": str(path),
                "content": content,
                "format": category,
            }
        return {"file": str(path), "format": category, "content": ""}
    except Exception as e:
        return {"error": f"转换文件失败 ({path.name}): {e}"}
