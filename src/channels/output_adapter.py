"""输出适配器基类模块。

定义所有输出适配器的抽象接口，负责将管道最终 state
或流式 chunk 转换为外部系统可识别的响应格式。
"""

from abc import ABC, abstractmethod
from typing import Any


class IOutputAdapter(ABC):
    """输出适配器抽象基类。

    负责将管道引擎的处理结果转换为特定外部系统（CLI、API、
    消息队列等）可识别的响应格式。支持一次性输出和流式输出。

    Subclasses:
        CLIOutputAdapter: 命令行输出适配器（支持 rich 彩色输出）
    """

    @abstractmethod
    async def send(self, state: dict[str, Any]) -> None:
        """输出管道最终 state。

        将管道引擎处理完毕的最终 state 转换为外部系统的
        响应格式并输出。

        Args:
            state: 管道引擎的最终 state 字典，通常包括：
                - raw_result: 核心插件的处理结果
                - should_stop: 是否应停止管道循环
                - error: 错误信息（如存在）
        """
        ...

    @abstractmethod
    async def send_stream(self, chunk: dict[str, Any]) -> None:
        """流式输出一个 chunk。

        在管道处理过程中，逐 chunk 输出中间结果，
        适用于 LLM 逐 token 生成等场景。

        Args:
            chunk: 流式输出的一个数据块，通常包括：
                - text: 当前 chunk 的文本内容
                - type: chunk 类型（如 "token"、"error"、"system"）
        """
        ...
