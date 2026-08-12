"""MiniMax 适配器。

MiniMax API 专有限制：
    1. 仅允许首位消息为 system role（非首位 system 必须转为 user）
    2. tool 消息 content 需清理（\x00 字符、超长截断）
    3. 不识别 reasoning_content 字段（需剥离）
"""

from __future__ import annotations

from typing import Any

from .base import ProviderAdapter


class MiniMaxAdapter(ProviderAdapter):
    """MiniMax：剥离 reasoning_content + 修复 system 角色。

    与 _message_normalizer.py 的 Phase 1-5 互补：
        - _message_normalizer：JSON 修复、tool_call_id 标准化、配对校验
        - MiniMaxAdapter：system 角色兜底 + rc 剥离
    """

    def adapt_messages_before_send(  # noqa: D102
        self,
        messages: list[dict[str, Any]],
        **kwargs: object,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for i, m in enumerate(messages):
            # 剥离 reasoning_content（基类职责 + 非首位 system 修复）
            new_m = {k: v for k, v in m.items() if k != "reasoning_content"}

            # MiniMax 不允许非首位 system 消息
            if i > 0 and new_m.get("role") == "system":
                new_m["role"] = "user"
                new_m.pop("name", None)

            result.append(new_m)
        return result
