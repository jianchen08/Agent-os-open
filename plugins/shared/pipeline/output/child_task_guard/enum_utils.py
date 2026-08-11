"""枚举安全提取工具——从 utils/enum_utils.py 扁平化复制。

迁移适配：原 from utils.enum_utils import safe_enum_value → from enum_utils import safe_enum_value
"""
from __future__ import annotations

from enum import Enum
from typing import Any

__all__ = ["safe_enum_value"]


def safe_enum_value(obj: Any) -> Any:
    """安全提取枚举成员的原始值。"""
    if isinstance(obj, Enum):
        return obj.value
    return obj
