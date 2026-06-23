"""输出适配器基类模块。

定义所有输出适配器的抽象接口，负责将管道最终 state
或流式 chunk 转换为外部系统可识别的响应格式。

标准接口：
- send(): 输出管道最终 state（抽象）
- send_stream(): 流式输出 chunk（抽象）
- health_check(): 检查适配器是否健康
- is_connected: 适配器是否已连接
- get_status(): 获取适配器状态信息
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

    async def health_check(self) -> bool:
        """检查输出适配器是否健康。

        默认实现返回 True。网络类适配器应重写此方法，
        执行实际的连接检查。

        Returns:
            True 表示适配器可正常发送消息
        """
        return True

    @property
    def is_connected(self) -> bool:
        """输出适配器是否已连接。

        默认返回 True（如 CLI 适配器始终可用）。
        网络类适配器应重写此属性以反映实际连接状态。

        Returns:
            True 表示适配器已连接并可发送消息
        """
        return True

    def get_status(self) -> dict[str, Any]:
        """获取输出适配器状态信息。

        Returns:
            包含适配器状态的字典，至少包含：
                - type: 适配器类名
                - connected: 是否已连接
                - healthy: 是否健康
        """
        return {
            "type": self.__class__.__name__,
            "connected": self.is_connected,
            "healthy": True,
        }
