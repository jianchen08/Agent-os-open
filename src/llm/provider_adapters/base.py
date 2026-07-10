"""Provider 适配器抽象基类。

职责边界：
    在消息发送给 API 前，按 provider 特定规则适配消息格式。
    只处理"对外发送"的差异，不修改系统内部的消息结构。

设计原则：
    - 系统内部 state["messages"] 统一保留所有字段（含 reasoning_content）
    - 发送给 API 前，由 ProviderAdapter 按 provider 规则裁剪/转换
    - 每个 provider 一个子类，独立文件，互不影响
    - 加新 provider：1. 实现 ProviderAdapter 子类；2. 在 __init__.py 注册

与 _message_normalizer.py 的边界：
    - _message_normalizer：消息结构标准化（provider 无关，如 tool_calls 配对校验）
    - ProviderAdapter：provider 特定适配（如 DeepSeek 保留 rc、MiniMax system 角色）
"""

from __future__ import annotations

from typing import Any


class ProviderAdapter:
    """Provider 适配器基类。

    默认实现：剥离 reasoning_content 字段（OpenAI/GLM/Anthropic 等
    不识别该字段，发送前需移除）。需要保留 rc 的 provider（如 DeepSeek
    thinking 模式）覆盖 adapt_messages_before_send 方法。

    所有适配方法返回新列表，不修改原数据（保护 state["messages"]）。
    """

    def adapt_messages_before_send(
        self,
        messages: list[dict[str, Any]],
        **kwargs: object,
    ) -> list[dict[str, Any]]:
        """发送给 API 前适配消息。

        默认实现：剥离 reasoning_content（非标字段，多数 provider 不识别）。
        DeepSeek 等需要保留 rc 的 provider 覆盖此方法。

        Args:
            messages: 标准化后的消息列表（含 reasoning_content 等内部字段）
            **kwargs: 模型的 API 参数（即 default_params），子类可按需读取

        Returns:
            适配后的消息列表（新列表，原数据不变）
        """
        return [{k: v for k, v in m.items() if k != "reasoning_content"} for m in messages]
