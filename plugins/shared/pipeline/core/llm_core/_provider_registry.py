"""提供者适配插件注册表——按模型名分发到 llm_provider_* 插件（可选挂载）。

task_kernel_cleanup_and_split 3a：llm_core 不绑定任何具体提供者。发送前的
提供者适配（MiniMax 角色修正 / DeepSeek 专有参数透传）与响应侧的
<think/> 提取由独立插件承载，本模块按模型名启发式分发——与拆分前的
adapter.py 内联判断完全等价（零行为回归）。

- 未命中任何 provider 规则 → 什么都不做，llm_core 内置 LiteLLM 直调（默认行为）
- 插件均为惰性导入：不依赖本模块的环境（测试直连 adapter 等）也可导入 adapter

规则（与拆分前 adapter.py 的启发式一致）：
- 模型名含 `minimax` → llm_provider_minimax（消息角色安全修正）
- 模型名以 `openai/` 开头 → llm_provider_deepseek（reasoning_effort/thinking
  经 extra_body 透传——DeepSeek 等经 OpenAI 兼容中转的模型）
- <think/> 提取与模型无关（DeepSeek/o1 类推理内容）→ llm_provider_deepseek
"""

from __future__ import annotations

from typing import Any

# 发送前适配的模型名 → provider 判断（与拆分前 adapter.completion 的内联逻辑一致）。
_DEEPSEEK_MATCHERS = (
    lambda m: m.lower().startswith("openai/"),
)
_MINIMAX_MATCHERS = (
    lambda m: "minimax" in m.lower(),
)


def apply_pre_send(
    model: str,
    messages: list[dict[str, Any]],
    kwargs: dict[str, Any],
) -> list[dict[str, Any]]:
    """completion() 发送前的提供者适配，返回（可能被修正的）messages。

    原地修改 kwargs（reasoning_effort/thinking 挪进 extra_body）。未命中任何
    provider 规则时原样返回，行为与内置 LiteLLM 直调完全一致。
    """
    # MiniMax：消息角色安全修正（仅允许首位 system）
    if any(match(model) for match in _MINIMAX_MATCHERS):
        from llm_provider_minimax import ensure_role_safety  # noqa: PLC0415

        messages = ensure_role_safety(model, messages)

    # DeepSeek 类（openai/ 兼容中转）：reasoning_effort/thinking → extra_body 透传
    if any(match(model) for match in _DEEPSEEK_MATCHERS):
        from llm_provider_deepseek import move_to_extra_body  # noqa: PLC0415

        move_to_extra_body(kwargs, ("reasoning_effort", "thinking"))

    return messages


def extract_thinking_from_content(content: str | None) -> tuple[str | None, str | None]:
    """<think/> 标签 reasoning 提取（DeepSeek/o1 类推理内容，与模型无关）。

    分发到 llm_provider_deepseek 插件；返回 (thinking_text, cleaned_content)。
    """
    from llm_provider_deepseek import extract_thinking_from_content as _impl  # noqa: PLC0415

    return _impl(content)
