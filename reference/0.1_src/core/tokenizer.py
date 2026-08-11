"""
Token 计数工具

提供文本和消息的 Token 计数功能
"""

from typing import Any

import tiktoken


class TokenCounter:
    """
    Token 计数器

    支持多种 Tokenizer，用于计算文本和消息的 Token 数量
    """

    # 模型到编码的映射
    MODEL_ENCODINGS = {
        "gpt-4": "cl100k_base",
        "gpt-4-turbo": "cl100k_base",
        "gpt-4o": "o200k_base",
        "gpt-4o-mini": "o200k_base",
        "gpt-3.5-turbo": "cl100k_base",
        "claude": "cl100k_base",
        "glm": "cl100k_base",  # GLM 模型使用 cl100k_base
        "deepseek": "cl100k_base",
        "default": "cl100k_base",
    }

    def __init__(self, encoding_name: str = "cl100k_base"):
        """
        初始化 Token 计数器

        Args:
            encoding_name: Tokenizer 名称，默认 cl100k_base（GPT-4/Claude 兼容）
        """
        try:
            self.encoding = tiktoken.get_encoding(encoding_name)
        except KeyError:
            # 如果指定的 encoding 不存在，使用默认的
            self.encoding = tiktoken.encoding_for_model("gpt-4")

    def count_text(self, text: str, model: str = "gpt-4") -> int:
        """
        计算文本的 Token 数量（兼容旧接口）

        Args:
            text: 输入文本
            model: 模型名称，用于选择编码器

        Returns:
            Token 数量
        """
        if not text:
            return 0

        try:
            # 查找匹配的编码
            encoding_name = self.MODEL_ENCODINGS.get("default")
            for prefix, enc in self.MODEL_ENCODINGS.items():
                if model.startswith(prefix):
                    encoding_name = enc
                    break

            encoding = tiktoken.get_encoding(encoding_name)
            tokens = encoding.encode(text)
            return len(tokens)
        except Exception:
            # 如果编码失败，使用快速估算
            return self.estimate_tokens(text)

    def count_tokens(self, text: str) -> int:
        """
        计算文本的 Token 数量

        Args:
            text: 输入文本

        Returns:
            Token 数量
        """
        if not text:
            return 0
        try:
            tokens = self.encoding.encode(text)
            return len(tokens)
        except Exception:
            # 如果编码失败，使用快速估算
            return self.estimate_tokens(text)

    def count_messages(self, messages: list[dict[str, Any]], model: str = "gpt-4") -> int:
        """
        计算消息列表的总 Token 数量

        Args:
            messages: 消息列表，格式：[{"role": "...", "content": "..."}]
            model: 模型名称，用于选择编码器

        Returns:
            总 Token 数量
        """
        # 查找匹配的编码
        encoding_name = None
        for prefix, enc in self.MODEL_ENCODINGS.items():
            if model.startswith(prefix):
                encoding_name = enc
                break

        if encoding_name is None:
            raise ValueError(f"不支持的模型名称: {model}，支持的模型前缀: {list(self.MODEL_ENCODINGS.keys())}")

        try:
            encoding = tiktoken.get_encoding(encoding_name)
        except KeyError as e:
            raise ValueError(f"无法获取编码器 {encoding_name}，错误: {e}") from e

        total = 0

        # 每条消息的开销（role, content 等）
        # 参考 OpenAI 的计算方式
        per_message_tokens = 4  # 每条消息约 4 tokens
        total += len(messages) * per_message_tokens

        for message in messages:
            role = message.get("role", "")
            content = message.get("content", "")

            # 计算每个字段的 token 数
            if role:
                total += len(encoding.encode(role))
            if content:
                total += len(encoding.encode(content))

            # 每个字段的键名也要算
            total += 2  # "role" + ": "
            total += 2  # "content" + ": "

        # 回复的开销（<|im_start|>assistant 等）
        total += 3  # 辅助字符

        return total

    def count_message(self, message: dict[str, Any]) -> int:
        """
        计算单条消息的 Token 数量

        Args:
            message: 单条消息，格式：{"role": "...", "content": "..."}

        Returns:
            Token 数量
        """
        total = 4  # 每条消息的基础开销

        role = message.get("role", "")
        content = message.get("content", "")

        # 计算每个字段的 token 数
        total += self.count_tokens(role)
        total += self.count_tokens(content)

        # 每个字段的键名
        total += 2  # "role" + ": "
        total += 2  # "content" + ": "

        return total

    def estimate_tokens(self, text: str) -> int:
        """
        快速估算 Token 数量（不使用 Tokenizer）

        估算规则：
        - 英文：约 4 字符/token
        - 中文：约 2 字符/token
        - 混合：取平均

        Args:
            text: 输入文本

        Returns:
            估算的 Token 数量
        """
        if not text:
            return 0

        # 统计中文字符
        chinese_chars = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")

        # 统计英文字符
        english_chars = sum(1 for c in text if c.isascii() and c.isalpha())

        # 其他字符（标点、数字、空格等）
        other_chars = len(text) - chinese_chars - english_chars

        # 估算
        chinese_tokens = chinese_chars / 2  # 中文约 2 字符/token
        english_tokens = english_chars / 4  # 英文约 4 字符/token
        other_tokens = other_chars / 3  # 其他约 3 字符/token

        return int(chinese_tokens + english_tokens + other_tokens)

    def truncate_text(self, text: str, max_tokens: int) -> str:
        """
        截断文本到指定 Token 数量

        Args:
            text: 输入文本
            max_tokens: 最大 Token 数量

        Returns:
            截断后的文本
        """
        current_tokens = self.count_tokens(text)

        if current_tokens <= max_tokens:
            return text

        # 需要截断
        try:
            # 使用 Tokenizer 精确截断
            tokens = self.encoding.encode(text)
            truncated_tokens = tokens[:max_tokens]
            return self.encoding.decode(truncated_tokens)
        except Exception:
            # 如果失败，按字符比例截断
            ratio = max_tokens / current_tokens
            target_length = int(len(text) * ratio * 0.9)  # 保留 10% 余量
            return text[:target_length]

    def truncate_messages(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int,
        keep_first: int = 0,
        keep_last: int = 0,
        model: str = "gpt-4",
    ) -> list[dict[str, Any]]:
        """
        截断消息列表到指定 Token 数量

        Args:
            messages: 消息列表
            max_tokens: 最大 Token 数量
            keep_first: 保留前 N 条消息（完整保留）
            keep_last: 保留后 N 条消息（完整保留）
            model: 模型名称，用于选择编码器

        Returns:
            截断后的消息列表
        """
        current_tokens = self.count_messages(messages, model)

        if current_tokens <= max_tokens:
            return messages

        # 如果要保留首尾，优先处理
        if keep_first > 0 or keep_last > 0:
            result = []

            # 保留前 N 条
            if keep_first > 0:
                result.extend(messages[:keep_first])

            # 计算中间部分可以保留多少
            first_part = messages[:keep_first] if keep_first > 0 else []
            last_part = messages[-keep_last:] if keep_last > 0 else []

            first_tokens = self.count_messages(first_part, model)
            last_tokens = self.count_messages(last_part, model)
            remaining_tokens = max_tokens - first_tokens - last_tokens

            if remaining_tokens > 0:
                # 从中间截断
                middle_start = keep_first
                middle_end = len(messages) - keep_last
                middle_messages = messages[middle_start:middle_end]

                # 反向截断中间部分（保留最新的）
                truncated_middle = []
                tokens_used = 0

                for msg in reversed(middle_messages):
                    msg_tokens = self.count_messages([msg], model)
                    if tokens_used + msg_tokens <= remaining_tokens:
                        truncated_middle.insert(0, msg)
                        tokens_used += msg_tokens
                    else:
                        break

                result.extend(truncated_middle)

            # 保留后 N 条
            if keep_last > 0:
                result.extend(messages[-keep_last:])

            return result

        # 否则，从开头截断（保留最新的）
        result = []
        tokens_used = 0

        for msg in reversed(messages):
            msg_tokens = self.count_messages([msg], model)
            if tokens_used + msg_tokens <= max_tokens:
                result.insert(0, msg)
                tokens_used += msg_tokens
            else:
                break

        return result


# 全局单例
_default_counter: TokenCounter = None


def get_token_counter(encoding_name: str = "cl100k_base") -> TokenCounter:
    """
    获取全局 Token 计数器单例

    Args:
        encoding_name: Tokenizer 名称

    Returns:
        Token 计数器实例
    """
    global _default_counter  # noqa: PLW0603

    if _default_counter is None or _default_counter.encoding.name != encoding_name:
        _default_counter = TokenCounter(encoding_name)

    return _default_counter
