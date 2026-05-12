"""
数据转换工具

提供通用的数据转换功能，减少重复代码
"""

import json
from datetime import datetime
from typing import Any
from uuid import UUID


class DataConverter:
    """通用数据转换器"""

    @staticmethod
    def to_dict(
        obj: Any, exclude_none: bool = True, exclude_private: bool = True
    ) -> dict[str, Any]:
        """
        将对象转换为字典

        Args:
            obj: 要转换的对象
            exclude_none: 是否排除None值
            exclude_private: 是否排除私有属性（以_开头）

        Returns:
            转换后的字典
        """
        if obj is None:
            return {}

        if isinstance(obj, dict):
            result = {}
            for key, value in obj.items():
                if exclude_none and value is None:
                    continue
                if exclude_private and isinstance(key, str) and key.startswith("_"):
                    continue
                result[key] = DataConverter._convert_value(
                    value, exclude_none, exclude_private
                )
            return result

        if hasattr(obj, "__dict__"):
            result = {}
            for key, value in obj.__dict__.items():
                if exclude_none and value is None:
                    continue
                if exclude_private and key.startswith("_"):
                    continue
                result[key] = DataConverter._convert_value(
                    value, exclude_none, exclude_private
                )
            return result

        # 对于其他类型，尝试转换为基本类型
        return DataConverter._convert_value(obj, exclude_none, exclude_private)

    @staticmethod
    def _convert_value(value: Any, exclude_none: bool, exclude_private: bool) -> Any:
        """转换单个值"""
        if value is None:
            return None

        if isinstance(value, (str, int, float, bool)):
            return value

        if isinstance(value, UUID):
            return str(value)

        if isinstance(value, datetime):
            return value.isoformat()

        if isinstance(value, (list, tuple)):
            return [
                DataConverter._convert_value(item, exclude_none, exclude_private)
                for item in value
            ]

        if isinstance(value, dict):
            return DataConverter.to_dict(value, exclude_none, exclude_private)

        if hasattr(value, "__dict__"):
            return DataConverter.to_dict(value, exclude_none, exclude_private)

        # 对于其他类型，尝试转换为字符串
        try:
            return str(value)
        except Exception:
            return repr(value)

    @staticmethod
    def to_json(obj: Any, **kwargs) -> str:
        """
        将对象转换为JSON字符串

        Args:
            obj: 要转换的对象
            **kwargs: json.dumps的额外参数

        Returns:
            JSON字符串
        """
        dict_obj = DataConverter.to_dict(obj)
        return json.dumps(dict_obj, ensure_ascii=False, **kwargs)

    @staticmethod
    def from_json(json_str: str) -> Any:
        """
        从JSON字符串解析对象

        Args:
            json_str: JSON字符串

        Returns:
            解析后的对象
        """
        return json.loads(json_str)

    @staticmethod
    def merge_dicts(*dicts: dict[str, Any], deep: bool = True) -> dict[str, Any]:
        """
        合并多个字典

        Args:
            *dicts: 要合并的字典
            deep: 是否深度合并

        Returns:
            合并后的字典
        """
        result = {}

        for d in dicts:
            if not isinstance(d, dict):
                continue

            for key, value in d.items():
                if (
                    key in result
                    and deep
                    and isinstance(result[key], dict)
                    and isinstance(value, dict)
                ):
                    result[key] = DataConverter.merge_dicts(
                        result[key], value, deep=True
                    )
                else:
                    result[key] = value

        return result

    @staticmethod
    def filter_dict(
        data: dict[str, Any],
        include_keys: list[str] | None = None,
        exclude_keys: list[str] | None = None,
        exclude_none: bool = False,
    ) -> dict[str, Any]:
        """
        过滤字典

        Args:
            data: 原始字典
            include_keys: 包含的键列表
            exclude_keys: 排除的键列表
            exclude_none: 是否排除None值

        Returns:
            过滤后的字典
        """
        result = {}

        for key, value in data.items():
            # 检查是否应该包含这个键
            if include_keys is not None and key not in include_keys:
                continue

            if exclude_keys is not None and key in exclude_keys:
                continue

            if exclude_none and value is None:
                continue

            result[key] = value

        return result

    @staticmethod
    def flatten_dict(
        data: dict[str, Any], separator: str = ".", prefix: str = ""
    ) -> dict[str, Any]:
        """
        扁平化嵌套字典

        Args:
            data: 嵌套字典
            separator: 键分隔符
            prefix: 键前缀

        Returns:
            扁平化后的字典
        """
        result = {}

        for key, value in data.items():
            new_key = f"{prefix}{separator}{key}" if prefix else key

            if isinstance(value, dict):
                result.update(DataConverter.flatten_dict(value, separator, new_key))
            else:
                result[new_key] = value

        return result

    @staticmethod
    def unflatten_dict(data: dict[str, Any], separator: str = ".") -> dict[str, Any]:
        """
        反扁平化字典

        Args:
            data: 扁平化的字典
            separator: 键分隔符

        Returns:
            嵌套字典
        """
        result = {}

        for key, value in data.items():
            keys = key.split(separator)
            current = result

            for k in keys[:-1]:
                if k not in current:
                    current[k] = {}
                current = current[k]

            current[keys[-1]] = value

        return result


# 便捷函数
def to_dict(obj: Any, **kwargs) -> dict[str, Any]:
    """便捷的to_dict函数"""
    return DataConverter.to_dict(obj, **kwargs)


def to_json(obj: Any, **kwargs) -> str:
    """便捷的to_json函数"""
    return DataConverter.to_json(obj, **kwargs)


def from_json(json_str: str) -> Any:
    """便捷的from_json函数"""
    return DataConverter.from_json(json_str)


def merge_dicts(*dicts: dict[str, Any], **kwargs) -> dict[str, Any]:
    """便捷的merge_dicts函数"""
    return DataConverter.merge_dicts(*dicts, **kwargs)
