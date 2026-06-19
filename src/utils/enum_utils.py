"""枚举安全提取工具。

提供统一的 enum 成员值提取函数，消除散布在各模块中的
``x.value if hasattr(x, "value") else x`` 样板代码。

暴露接口：
- safe_enum_value(obj) -> Any：Enum 成员返回 .value，否则原样返回
"""

from __future__ import annotations

from enum import Enum
from typing import Any

__all__ = ["safe_enum_value"]


def safe_enum_value(obj: Any) -> Any:
    """安全提取枚举成员的原始值。

    如果 *obj* 是 Enum 成员（拥有 ``value`` 属性），返回 ``obj.value``；
    否则原样返回 *obj*。

    Args:
        obj: 可能是 Enum 成员或普通值的对象。

    Returns:
        Enum 成员的 ``.value``，或 *obj* 本身。

    Examples:
        >>> from enum import Enum
        >>> class Color(Enum):
        ...     RED = "red"
        >>> safe_enum_value(Color.RED)
        'red'
        >>> safe_enum_value("plain_string")
        'plain_string'
        >>> safe_enum_value(42)
        42
    """
    if isinstance(obj, Enum):
        return obj.value
    return obj
