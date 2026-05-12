"""
消息模型和转换工具

提供统一的消息模型和 LangChain 消息格式转换功能

注意：ToolCall、Message 等核心类定义已移至 base.py
此模块保留是为了向后兼容，所有符号从 base.py 重导出
"""

# 从 base.py 重导出，保持向后兼容
from src.llm.base import (
    Message,
    ToolCall,
)
from src.llm.base import (
    langchain_to_message as from_langchain,
)
from src.llm.base import (
    langchain_to_messages as batch_from_langchain,
)
from src.llm.base import (
    message_to_langchain as to_langchain,
)
from src.llm.base import (
    messages_to_langchain as batch_to_langchain,
)

__all__ = [
    "ToolCall",
    "Message",
    "to_langchain",
    "from_langchain",
    "batch_to_langchain",
    "batch_from_langchain",
]
