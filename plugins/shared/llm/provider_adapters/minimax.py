"""MiniMax 适配器。

MiniMax API 专有限制：
    1. 仅允许首位消息为 system role（非首位 system 必须转为 user）
    2. tool 消息 content 需清理（\x00 字符、超长截断）

rc 回放（2026-10-21 策略化）：MiniMax 对 assistant 回传 reasoning_content
容忍（实机探针 HTTP 200 正常应答，MiniMax-M3.1-Flash-Preview；探针转正为
test_reasoning_replay_contract.py 实机档）。旧口径「MiniMax 不识别需剥离」经
实测推翻——无条件剥离对思考模型是结构性失忆（可见正文与思考内容可差两个数量
级），是无进展循环的放大器。内容级窗口控制（只回放最近 N 轮）由 llm_core 出站
回放预算负责，本适配器只做整字段级策略与 system 角色修复。
"""

from __future__ import annotations

from typing import Any

from .base import ProviderAdapter


class MiniMaxAdapter(ProviderAdapter):
    """MiniMax：回放 reasoning_content + 修复 system 角色。

    与 _message_normalizer.py 的 Phase 1-5 互补：
        - _message_normalizer：JSON 修复、tool_call_id 标准化、配对校验
        - MiniMaxAdapter：system 角色兜底（rc 按生效档位回放或剥离）
    """

    #: 实测容忍 rc 回传（见模块 docstring 探针依据），原样回放。
    replay_reasoning: bool = True

    def adapt_messages_before_send(  # noqa: D102
        self,
        messages: list[dict[str, Any]],
        *,
        replay_reasoning: bool | str | None = None,
        **kwargs: object,
    ) -> list[dict[str, Any]]:
        effective = self._effective_replay(replay_reasoning)
        result: list[dict[str, Any]] = []
        for i, m in enumerate(messages):
            # 逐条浅拷贝（契约：返回新列表新 dict，不改写 state["messages"]）。
            new_m = dict(m)

            # 剥离档：去掉 reasoning_content（字段级策略，与角色修复正交）。
            if not effective:
                new_m.pop("reasoning_content", None)

            # MiniMax 不允许非首位 system 消息
            if i > 0 and new_m.get("role") == "system":
                new_m["role"] = "user"
                new_m.pop("name", None)

            result.append(new_m)
        return result
