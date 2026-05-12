"""
Token 计数器

计算消息的 Token 数量
"""

import tiktoken

from src.llm.base import Message


class TokenCounter:
    """Token 计数器"""

    # 模型到编码的映射
    MODEL_ENCODINGS = {
        "gpt-4": "cl100k_base",
        "gpt-4-turbo": "cl100k_base",
        "gpt-4o": "o200k_base",
        "gpt-4o-mini": "o200k_base",
        "gpt-3.5-turbo": "cl100k_base",
        "claude": "cl100k_base",  # Claude 使用类似的 tokenizer
        "default": "cl100k_base",
    }

    def __init__(self):
        """初始化 Token 计数器"""
        self._encodings = {}

    def _get_encoding(self, model: str) -> tiktoken.Encoding:
        """获取模型对应的编码器"""
        # 查找匹配的编码
        encoding_name = self.MODEL_ENCODINGS.get("default")
        for prefix, enc in self.MODEL_ENCODINGS.items():
            if model.startswith(prefix):
                encoding_name = enc
                break

        # 缓存编码器
        if encoding_name not in self._encodings:
            self._encodings[encoding_name] = tiktoken.get_encoding(encoding_name)

        return self._encodings[encoding_name]

    def count_text(self, text: str, model: str = "gpt-4") -> int:
        """
        计算文本的 Token 数

        Args:
            text: 文本内容
            model: 模型名称

        Returns:
            Token 数量
        """
        if not text:
            return 0

        encoding = self._get_encoding(model)
        return len(encoding.encode(text))

    def count_messages(self, messages: list[Message], model: str = "gpt-4") -> int:
        """
        计算消息列表的 Token 数

        Args:
            messages: 消息列表
            model: 模型名称

        Returns:
            Token 数量
        """
        total = 0
        encoding = self._get_encoding(model)

        for message in messages:
            # 每条消息有固定开销
            total += 4  # <|im_start|>{role}\n ... <|im_end|>\n

            # 计算内容
            if message.content:
                total += len(encoding.encode(message.content))

            # 计算角色
            total += len(encoding.encode(message.role))

            # 工具调用
            if message.tool_calls:
                for tool_call in message.tool_calls:
                    total += len(encoding.encode(tool_call.name))
                    total += len(encoding.encode(str(tool_call.arguments)))

        # 回复的开头
        total += 2

        return total

    def truncate_messages(
        self,
        messages: list[Message],
        max_tokens: int,
        model: str = "gpt-4",
        preserve_system: bool = True,
    ) -> list[Message]:
        """
        截断消息以适应 Token 限制

        Args:
            messages: 消息列表
            max_tokens: 最大 Token 数
            model: 模型名称
            preserve_system: 是否保留系统消息

        Returns:
            截断后的消息列表
        """
        if not messages:
            return []

        # 分离系统消息和其他消息
        system_messages = []
        other_messages = []

        for msg in messages:
            if msg.role == "system" and preserve_system:
                system_messages.append(msg)
            else:
                other_messages.append(msg)

        # 计算系统消息的 Token 数
        system_tokens = (
            self.count_messages(system_messages, model) if system_messages else 0
        )
        remaining_tokens = max_tokens - system_tokens

        if remaining_tokens <= 0:
            return system_messages

        # 从最新的消息开始保留
        result = []
        current_tokens = 0

        for msg in reversed(other_messages):
            msg_tokens = self.count_messages([msg], model)
            if current_tokens + msg_tokens <= remaining_tokens:
                result.insert(0, msg)
                current_tokens += msg_tokens
            else:
                break

        return system_messages + result


# 模块级单例
_token_counter: TokenCounter | None = None


def get_token_counter() -> TokenCounter:
    """获取 Token 计数器单例"""
    global _token_counter
    if _token_counter is None:
        _token_counter = TokenCounter()
    return _token_counter
