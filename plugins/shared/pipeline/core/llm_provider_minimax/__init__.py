"""MiniMax 提供者适配插件（llm_provider_minimax）。

自 llm_core/adapter.py 拆出（task_kernel_cleanup_and_split 3a）：
`ensure_role_safety`——MiniMax 消息角色安全修正（API 仅允许首位消息为 system）。

作为 llm_core 的可选适配插件：由 `llm_core/_provider_registry.py` 按模型名
（模型名含 `minimax`）挂载；不挂载时 llm_core 内置 LiteLLM 直调，行为不变。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def ensure_role_safety(
    model: str,
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """防御性兜底：确保 minimax 模型不会收到非首位 system 消息。

    根因：MiniMax API 仅允许首位消息为 system role。管道中的
    StreamRepetitionGuard、ThinkingTruncationGuard 等会注入 system 消息，
    _normalize_messages_for_provider 的 Phase 1-4 已做转换，但极端边界
    情况可能遗漏。此方法作为最后一道防线，在 adapter 层拦截。

    Args:
        model: LiteLLM 模型标识字符串（如 "minimax/MiniMax-M2.7"）
        messages: 对话消息列表

    Returns:
        修正后的消息列表（原地修改 + 返回引用）
    """
    # 检测是否为 minimax 模型
    if "minimax" not in model.lower():
        return messages

    needs_fix = False
    for i, msg in enumerate(messages):
        if i > 0 and msg.get("role") == "system":
            needs_fix = True
            break

    if not needs_fix:
        return messages

    for i, msg in enumerate(messages):
        if i > 0 and msg.get("role") == "system":
            msg["role"] = "user"
            msg.pop("name", None)
            logger.warning(
                "[llm_provider_minimax] Minimax 兜底: 非首位 system→user idx=%d content=%s",
                i,
                str(msg.get("content", ""))[:100],
            )
    return messages
