"""
参数名标准化器

基于 VCP 优化的指令参数容错性增强
支持大小写、分隔符不敏感的参数匹配
"""

import re
from typing import Any


class ParameterNormalizer:
    """
    参数名标准化器

    功能：
    1. 标准化参数键名（移除分隔符、转小写）
    2. 容错匹配参数（支持多种格式）
    3. 参数验证

    Examples:
        >>> normalizer = ParameterNormalizer()
        >>> normalizer.normalize_key("Image_Size")
        'imagesize'
        >>> normalizer.normalize_key("FILE-PATH")
        'filepath'
        >>> normalizer.match_param("imagesize", ["image_size", "file_path"])
        'image_size'
    """

    # 标准化模式：移除所有分隔符，转小写
    NORMALIZATION_PATTERN = re.compile(r"[-_\s]")

    @classmethod
    def normalize_key(cls, key: str) -> str:
        """
        标准化参数键名

        移除所有分隔符（-、_、空格），转换为小写

        Args:
            key: 原始键名

        Returns:
            标准化后的键名

        Examples:
            >>> ParameterNormalizer.normalize_key("image_size")
            'imagesize'
            >>> ParameterNormalizer.normalize_key("ImageSize")
            'imagesize'
            >>> ParameterNormalizer.normalize_key("IMAGE-SIZE")
            'imagesize'
            >>> ParameterNormalizer.normalize_key("file path")
            'filepath'
        """
        if not key:
            return ""

        # 移除所有分隔符，转小写
        return cls.NORMALIZATION_PATTERN.sub("", key).lower()

    @classmethod
    def normalize_params(cls, params: dict[str, Any]) -> dict[str, Any]:
        """
        标准化所有参数键名

        Args:
            params: 原始参数字典

        Returns:
            标准化后的参数字典

        Examples:
            >>> params = {
            ...     "Image_Size": "1024x1024",
            ...     "file-path": "/tmp/test",
            ...     "num results": 10
            ... }
            >>> ParameterNormalizer.normalize_params(params)
            {'imagesize': '1024x1024', 'filepath': '/tmp/test', 'numresults': 10}
        """
        normalized = {}

        for key, value in params.items():
            normalized_key = cls.normalize_key(key)
            normalized[normalized_key] = value

        return normalized

    @classmethod
    def match_param(cls, provided_key: str, expected_keys: list[str]) -> str | None:
        """
        匹配参数键名（容错）

        尝试将提供的键名与期望的键名列表进行匹配，
        忽略大小写和分隔符差异

        Args:
            provided_key: 提供的键名
            expected_keys: 期望的键名列表

        Returns:
            匹配到的键名，如果没有匹配则返回 None

        Examples:
            >>> expected = ["image_size", "file_path", "num_results"]
            >>> ParameterNormalizer.match_param("Image_Size", expected)
            'image_size'
            >>> ParameterNormalizer.match_param("FILE-PATH", expected)
            'file_path'
            >>> ParameterNormalizer.match_param("unknown", expected) is None
            True
        """
        if not provided_key:
            return None

        normalized_provided = cls.normalize_key(provided_key)

        # 创建期望键名的标准化映射
        expected_map = {cls.normalize_key(key): key for key in expected_keys}

        # 查找匹配
        if normalized_provided in expected_map:
            return expected_map[normalized_provided]

        return None

    @classmethod
    def match_params(
        cls, provided_params: dict[str, Any], expected_keys: list[str]
    ) -> dict[str, Any]:
        """
        批量匹配参数（容错）

        Args:
            provided_params: 提供的参数字典
            expected_keys: 期望的键名列表

        Returns:
            匹配后的参数字典（使用期望的键名）

        Examples:
            >>> provided = {
            ...     "Image_Size": "1024x1024",
            ...     "file-path": "/tmp/test"
            ... }
            >>> expected = ["image_size", "file_path", "num_results"]
            >>> ParameterNormalizer.match_params(provided, expected)
            {'image_size': '1024x1024', 'file_path': '/tmp/test'}
        """
        matched = {}

        for provided_key, value in provided_params.items():
            expected_key = cls.match_param(provided_key, expected_keys)
            if expected_key:
                matched[expected_key] = value

        return matched
