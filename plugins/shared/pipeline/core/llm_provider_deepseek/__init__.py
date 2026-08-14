"""DeepSeek/o1 类提供者适配插件（llm_provider_deepseek）。

自 llm_core/adapter.py 拆出（task_kernel_cleanup_and_split 3a）：
- `extract_thinking_from_content`：<think/> 标签 reasoning 提取（DeepSeek/o1 类
  推理模型把思考内容混在 content 返回时手动分离）
- `move_to_extra_body`：reasoning_effort / thinking 等专有参数透传
  （openai/ 兼容中转端点时 litellm openai provider 不认这些参数，经 extra_body
  原样透传给上游）

作为 llm_core 的可选适配插件：由 `llm_core/_provider_registry.py` 按模型名
（`openai/` 前缀）挂载；不挂载时 llm_core 内置 LiteLLM 直调，行为不变。
"""

from __future__ import annotations

import re
from typing import Any

# <think>...</think> 标签模式：标准 XML（含 type="x"）与 MiniMax 变体（开始标签无 >）
_THINK_PATTERN = re.compile(
    r"<think[^>]*>(.*?)</think[^>]*>",
    re.DOTALL,
)
_THINK_PATTERN_NO_GT = re.compile(
    r"<think\s(.*?)</think[^>]*>",
    re.DOTALL,
)


def extract_thinking_from_content(content: str | None) -> tuple[str | None, str | None]:
    """从 content 中提取 <think/> 标签内容，返回 (thinking_text, cleaned_content)。

    MiniMax-M2.7 等推理模型把思考内容包裹在 <think/> 标签中混在 content 字段返回，
    litellm 不会自动映射到 reasoning_content，因此这里手动解析 <think/> 标签，
    将思考内容与正文分离。

    支持两种标签格式：
    1. 标准 XML: <think\\n...\\n</think/> 或 <think type="x">...</think...>
    2. MiniMax: <think\\n...\\n</think/> (开始标签无 >)

    Args:
        content: LLM 返回的原始 content 文本

    Returns:
        (thinking_text, cleaned_content) 元组
    """
    if not content:
        return None, content

    pattern, matches = _THINK_PATTERN, _THINK_PATTERN.findall(content)
    if not matches:
        pattern, matches = _THINK_PATTERN_NO_GT, _THINK_PATTERN_NO_GT.findall(content)
    if not matches:
        return None, content

    thinking = "\n".join(m.strip() for m in matches if m.strip())
    cleaned = pattern.sub("", content).strip()
    return thinking if thinking else None, cleaned if cleaned else None


def move_to_extra_body(kwargs: dict[str, Any], keys: tuple[str, ...]) -> None:
    """把指定的 kwargs 挪进 extra_body，让 litellm/OpenAI SDK 原样透传给上游。

    litellm 的 openai provider 对部分参数（reasoning_effort、thinking 等）会
    主动拦截或丢弃，但这些参数经 OpenAI 兼容中转端（如 apigo）时上游能接受。
    extra_body 是 OpenAI SDK 的官方透传通道，litellm 把它原样合并进请求 body。

    仅移动 kwargs 中已存在的 key；不存在的跳过。原地修改 kwargs。

    Args:
        kwargs: litellm 调用参数字典（原地修改）
        keys: 需要挪进 extra_body 的参数名
    """
    extra = dict(kwargs.get("extra_body") or {})
    for k in keys:
        if k in kwargs:
            extra[k] = kwargs.pop(k)
    if extra:
        kwargs["extra_body"] = extra
