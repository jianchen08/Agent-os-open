"""Minimax 角色转换模块。

在管道层将消息角色在 Minimax API 和内部格式之间转换。
Minimax API 使用特定的角色标识（如 "user" / "assistant" / "system"），
需要确保发送到 Minimax 的消息角色正确。
"""

from __future__ import annotations

from typing import Any


# Minimax 支持的角色映射
MINIMAX_ROLE_MAP: dict[str, str] = {
    "user": "user",
    "assistant": "assistant",
    "system": "system",
    "function": "function",
    "tool": "function",  # tool 角色映射为 function
}

# 内部角色到 Minimax 角色的映射
INTERNAL_TO_MINIMAX: dict[str, str] = {
    "user": "user",
    "assistant": "assistant",
    "system": "system",
    "function_call": "function",
    "tool_call": "function",
    "observation": "user",  # observation 角色作为 user 发送给 Minimax
}

# Minimax 角色到内部角色的映射
MINIMAX_TO_INTERNAL: dict[str, str] = {
    "user": "user",
    "assistant": "assistant",
    "system": "system",
    "function": "function_call",
}


def normalize_role_to_minimax(role: str) -> str:
    """将内部角色转换为 Minimax 角色。

    Args:
        role: 内部角色标识。

    Returns:
        Minimax 角色标识，未知角色默认返回 "user"。
    """
    return INTERNAL_TO_MINIMAX.get(role, "user")


def normalize_role_from_minimax(role: str) -> str:
    """将 Minimax 角色转换为内部角色。

    Args:
        role: Minimax 角色标识。

    Returns:
        内部角色标识，未知角色默认返回 "user"。
    """
    return MINIMAX_TO_INTERNAL.get(role, "user")


def normalize_messages_for_minimax(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """将消息列表的角色全部转换为 Minimax 格式。

    Args:
        messages: 内部格式的消息列表，每条消息包含 "role" 和 "content" 字段。

    Returns:
        角色已转换的消息列表（浅拷贝）。
    """
    normalized = []
    for msg in messages:
        normalized_msg = dict(msg)
        if "role" in normalized_msg:
            normalized_msg["role"] = normalize_role_to_minimax(normalized_msg["role"])
        normalized.append(normalized_msg)
    return normalized


def validate_minimax_messages(
    messages: list[dict[str, Any]],
) -> list[str]:
    """验证消息列表是否符合 Minimax API 要求。

    Args:
        messages: 待验证的消息列表。

    Returns:
        错误信息列表，为空表示验证通过。
    """
    errors: list[str] = []
    if not messages:
        errors.append("消息列表不能为空")
        return errors

    for i, msg in enumerate(messages):
        if "role" not in msg:
            errors.append(f"消息 {i}: 缺少 role 字段")
        elif msg["role"] not in MINIMAX_ROLE_MAP:
            errors.append(f"消息 {i}: 无效角色 '{msg["role"]}'")

        if "content" not in msg and msg.get("role") != "function":
            errors.append(f"消息 {i}: 缺少 content 字段")

    return errors


def ensure_alternating_roles(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """确保消息列表中角色交替出现（Minimax API 要求）。

    如果连续出现相同角色，则合并其内容。

    Args:
        messages: 消息列表。

    Returns:
        角色交替的消息列表。
    """
    if not messages:
        return []

    result: list[dict[str, Any]] = [dict(messages[0])]
    for msg in messages[1:]:
        if msg.get("role") == result[-1].get("role"):
            # 合并相同角色的消息
            result[-1]["content"] = (
                f"{result[-1].get('content', '')}\n{msg.get('content', '')}"
            )
        else:
            result.append(dict(msg))
    return result
