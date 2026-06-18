"""LLM 客户端实现。

注：OpenAI/Anthropic/Ollama/Zhipu 的 LangChain 客户端已移除。
生产 LLM 调用统一走 LiteLLMAdapter（见 src/llm/adapter.py），
后者原生支持上述全部 provider 且能力更全（socket 超时、key pool 限流、
<think/> 状态机等）。

保留 reasoning/mock 客户端供特定场景使用：
- reasoning: 思考模型的 httpx 直连实现（reasoning_content 解析）
- mock: 单元测试用桩
"""

from src.llm.clients.mock import MockClient
from src.llm.clients.reasoning import (
    AnthropicReasoningClient,
    DeepSeekReasoningClient,
    OpenAIReasoningClient,
)

__all__ = [
    "MockClient",
    "DeepSeekReasoningClient",
    "OpenAIReasoningClient",
    "AnthropicReasoningClient",
]
