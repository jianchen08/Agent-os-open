"""输入适配器基类模块。

定义所有输入适配器的抽象接口，负责从外部系统接收请求
并转换为管道可处理的初始 state。
"""

from abc import ABC, abstractmethod
from typing import Any


class IInputAdapter(ABC):
    """输入适配器抽象基类。

    负责从特定外部源（CLI、API、消息队列等）接收请求，
    并将其转换为管道引擎可处理的初始 state 字典。

    Subclasses:
        CLIInputAdapter: 命令行输入适配器
    """

    @abstractmethod
    async def receive(self) -> dict[str, Any]:
        """接收外部请求，返回初始 state。

        从外部源读取输入，将其封装为管道引擎的初始 state 字典。
        该方法应为异步非阻塞调用。

        Returns:
            dict[str, Any]: 包含初始管道状态的字典，通常包括：
                - user_input: 用户输入文本
                - core_type: 请求的核心处理类型
                - session_id: 会话唯一标识
                - should_stop: 是否应停止管道循环
                - iteration: 当前迭代次数（初始为 1）
        """
        ...
