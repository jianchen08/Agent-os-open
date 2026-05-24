"""正则表达式测试工具，支持常用模式的匹配。

提供邮箱、手机号、URL、IP 地址、身份证号、日期、HTML 标签等
常用正则模式的匹配函数，每个模式封装为独立函数。
"""

from __future__ import annotations

import re

__all__ = [
    "match_email",
    "match_phone",
    "match_url",
    "match_ip_address",
    "match_id_card",
    "match_date",
    "match_html_tag",
]


def match_email(text: str) -> list[str]:
    """从文本中匹配所有邮箱地址。

    Args:
        text: 待匹配的文本。

    Returns:
        匹配到的邮箱地址列表。
    """
    pattern = r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"
    return re.findall(pattern, text)


def match_phone(text: str) -> list[str]:
    """从文本中匹配所有中国大陆手机号（11 位，1 开头）。

    Args:
        text: 待匹配的文本。

    Returns:
        匹配到的手机号列表。
    """
    pattern = r"1[3-9]\d{9}"
    return re.findall(pattern, text)


def match_url(text: str) -> list[str]:
    """从文本中匹配所有 URL。

    Args:
        text: 待匹配的文本。

    Returns:
        匹配到的 URL 列表。
    """
    pattern = r"https?://[^\s<>\"']+|ftp://[^\s<>\"']+"
    return re.findall(pattern, text)


def match_ip_address(text: str) -> list[str]:
    """从文本中匹配所有有效 IPv4 地址。

    每段数值范围 0-255，排除超范围匹配。

    Args:
        text: 待匹配的文本。

    Returns:
        匹配到的 IPv4 地址列表。
    """
    pattern = r"(?<!\d)(?:25[0-5]|2[0-4]\d|1\d{2}|[1-9]?\d)(?:\.(?:25[0-5]|2[0-4]\d|1\d{2}|[1-9]?\d)){3}(?!\d)"
    return re.findall(pattern, text)


def match_id_card(text: str) -> list[str]:
    """从文本中匹配所有 18 位身份证号。

    前 17 位为数字，最后一位为数字或 X/x。

    Args:
        text: 待匹配的文本。

    Returns:
        匹配到的身份证号列表。
    """
    pattern = r"[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]"
    return re.findall(pattern, text)


def match_date(text: str) -> list[str]:
    """从文本中匹配所有 YYYY-MM-DD 格式的日期。

    Args:
        text: 待匹配的文本。

    Returns:
        匹配到的日期列表。
    """
    pattern = r"\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])"
    return re.findall(pattern, text)


def match_html_tag(text: str) -> list[str]:
    """从文本中匹配所有 HTML 标签。

    Args:
        text: 待匹配的文本。

    Returns:
        匹配到的 HTML 标签列表。
    """
    pattern = r"</?[a-zA-Z][^>]*>"
    return re.findall(pattern, text)
